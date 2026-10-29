"""Safely migrate legacy per-inbound TCP defaults without changing identity."""
from __future__ import annotations

import argparse
from contextlib import closing
import copy
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

NETWORKS = {"tcp", "raw", "xhttp", "splithttp"}
GUARD_COLUMNS = ("tag", "port", "protocol", "enable", "listen", "settings", "remark", "sniffing")
FIELDS = {"tcpWindowClamp": (600, 0), "tcpMaxSeg": (1440, None),
          "tcpUserTimeout": (10000, None), "tcpcongestion": ("bbr", None)}
ROOT_NAME = "3xuish-performance-repair"
JOB_NAME = r"job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}"


def public_label(value, allowed):
    """Do not echo arbitrary database strings in diagnostic output."""
    return value if isinstance(value, str) and value in allowed else "unknown"


class RepairError(RuntimeError):
    """Only fixed, non-sensitive error codes may be passed here."""


def utc_now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def private_write(path, data, *, exclusive=True):
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))


def atomic_json(path, value):
    path = Path(path)
    temp = path.with_name(path.name + ".next-" + uuid.uuid4().hex[:8])
    private_write(temp, json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temp, path)


def object_json(value, code):
    try:
        parsed = json.loads(value) if isinstance(value, str) else copy.deepcopy(value)
    except (ValueError, TypeError):
        raise RepairError(code) from None
    if not isinstance(parsed, dict):
        raise RepairError(code)
    return parsed


def rows_from_db(database):
    path = Path(database).resolve()
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)) as connection:
        connection.row_factory = sqlite3.Row
        columns = {row[1] for row in connection.execute("PRAGMA table_info(inbounds)")}
        if not {"id", "tag", "remark", "port", "protocol", "enable", "settings", "stream_settings"} <= columns:
            raise RepairError("unsupported_database_schema")
        return {row["id"]: dict(row) for row in connection.execute("SELECT * FROM inbounds ORDER BY id")}


def repair_stream(stream):
    stream = copy.deepcopy(stream)
    options = stream.get("sockopt", {})
    if not isinstance(options, dict):
        raise RepairError("invalid_sockopt")
    changes = []
    for field, (old, new) in FIELDS.items():
        value = options.get(field)
        if type(value) is type(old) and value == old:
            changes.append({"field": field, "before": old, "after": new})
            if new is None:
                del options[field]
            else:
                options[field] = new
    if changes:
        stream["sockopt"] = options
    return stream, changes


def make_plan(database, manifest_path=None, inbound_id=None):
    rows = rows_from_db(database)
    if inbound_id is not None and (type(inbound_id) is not int or inbound_id < 1 or inbound_id not in rows):
        raise RepairError("inbound_id_not_found")
    manifest_before = None
    manifest = None
    managed = {}
    if manifest_path is not None and Path(manifest_path).exists():
        path = Path(manifest_path)
        if path.is_symlink() or not path.is_file():
            raise RepairError("unexpected_manifest_path")
        manifest_before = path.read_bytes()
        try:
            manifest = json.loads(manifest_before)
            if not isinstance(manifest, list):
                raise ValueError()
            for item in manifest:
                tag = item["payload"]["tag"]
                if not isinstance(tag, str) or not tag or tag in managed:
                    raise ValueError()
                matches = [r for r in rows.values() if r["tag"] == tag]
                if len(matches) != 1 or item["payload"]["port"] != matches[0]["port"] or item["payload"]["protocol"] != matches[0]["protocol"]:
                    raise ValueError()
                managed[tag] = item
        except (ValueError, TypeError, KeyError):
            raise RepairError("manifest_database_mismatch") from None
    plan = []
    checked = []
    for row in rows.values():
        explicit = row["id"] == inbound_id
        expected = explicit or (inbound_id is None and row["protocol"] == "vless"
                                and (len(rows) == 1 or row["remark"] == "VLESS-XHTTP-Enc-Vision" or row["tag"] in managed))
        node = {"id": row["id"], "port": row["port"], "enabled": bool(row["enable"]),
                "protocol": public_label(row["protocol"], {"vless", "vmess", "trojan", "shadowsocks",
                    "dokodemo-door", "socks", "http", "wireguard", "hysteria", "mixed", "tun"}),
                "network": "unknown", "security": "unknown", "selected": False,
                "reason": "unrecognized_inbound", "legacy_fields": []}
        checked.append(node)
        try:
            stream = object_json(row["stream_settings"], "invalid_stream_json")
        except RepairError:
            if expected:
                raise
            node["reason"] = "not_requested" if inbound_id is not None and not explicit else "invalid_stream_json"
            continue
        network = stream.get("network", stream.get("method"))
        node["network"] = public_label(network, NETWORKS | {"ws", "httpupgrade", "grpc", "h2", "kcp", "quic", "hysteria"})
        node["security"] = public_label(stream.get("security", "none"), {"tls", "reality", "none"})
        supported = row["protocol"] == "vless" and isinstance(network, str) and network in NETWORKS
        legacy = (supported and row["remark"] == "VLESS-XHTTP-Enc-Vision"
                  and stream.get("network", stream.get("method")) in {"xhttp", "splithttp"}
                  and stream.get("security") == "tls")
        single = len(rows) == 1 and supported
        selected = explicit if inbound_id is not None else (legacy or row["tag"] in managed or single)
        if not supported:
            if explicit:
                raise RepairError("explicit_inbound_is_not_vless_tcp")
            selected = False
        try:
            after, changes = repair_stream(stream)
        except RepairError:
            if selected:
                raise
            node["reason"] = "not_requested" if inbound_id is not None and not explicit else "invalid_sockopt"
            continue
        node["legacy_fields"] = [change["field"] for change in changes]
        node["selected"] = selected
        if inbound_id is not None and not explicit:
            node["reason"] = "not_requested"
        elif not supported:
            node["reason"] = "unsupported_protocol" if row["protocol"] != "vless" else "unsupported_transport"
        elif selected:
            node["reason"] = ("explicit_inbound" if explicit else "managed_profile" if row["tag"] in managed
                              else "legacy_installer" if legacy else "single_inbound")
        if not selected:
            continue
        object_json(row["settings"], "invalid_settings_json")
        if not changes:
            continue
        item = {"id": row["id"], "tag": row["tag"], "port": row["port"], "enable": bool(row["enable"]),
                "before": row["stream_settings"], "after": json.dumps(after, separators=(",", ":"), ensure_ascii=False),
                "changes": changes, "row_before": row}
        plan.append(item)
        if row["tag"] in managed:
            profile = managed[row["tag"]]
            if object_json(profile["payload"]["streamSettings"], "invalid_manifest_stream") != stream:
                raise RepairError("manifest_stream_mismatch")
            profile["payload"]["streamSettings"] = item["after"]
    manifest_after = None
    if manifest_before is not None and any(item["tag"] in managed for item in plan):
        manifest_after = json.dumps(manifest, indent=2, ensure_ascii=False).encode()
    return {"rows": plan, "manifest_before": manifest_before, "manifest_after": manifest_after,
            "checked_nodes": checked}


def plan_details(plan):
    checked = plan.get("checked_nodes", [])
    selected = sum(node["selected"] for node in checked)
    unresolved = [node["id"] for node in checked if not node["selected"] and node["legacy_fields"]
                  and node["protocol"] == "vless" and node["reason"] != "not_requested"]
    review = bool(unresolved) or any(node["protocol"] == "vless" and
                                   node["reason"] in {"invalid_stream_json", "invalid_sockopt"} for node in checked)
    reason = ("legacy_values_found" if plan["rows"] else "selected_nodes_clean" if selected
              else "no_recognized_nodes" if checked else "no_inbounds")
    message = {"legacy_values_found": "已识别需要修复的旧参数。",
               "selected_nodes_clean": "已检查选中的入站，没有命中需要修复的旧参数。",
               "no_recognized_nodes": "已扫描入站，但没有自动选中可修复的旧节点；未修改配置。",
               "no_inbounds": "数据库中没有入站；未修改配置。"}[reason]
    if review:
        message += " 另有未自动处理的旧参数或无法检查的入站，请查看 checked_nodes；不能据此判定全部入站已修复。"
    return {"nodes": [{"id": item["id"], "port": item["port"], "enabled": item["enable"],
                       "changes": item["changes"]} for item in plan["rows"]],
            "checked_nodes": checked, "counts": {"total": len(checked), "selected": selected, "repair": len(plan["rows"])},
            "reason": reason, "message": message, "unselected_legacy_nodes": unresolved,
            "review_required": review, "scope_complete": not review,
            "identity_preserved": True, "system_settings_changed": False}


def summarize(plan):
    return {"status": "changes_available" if plan["rows"] else "no_changes", **plan_details(plan)}


def normalized_stream(stream):
    value = copy.deepcopy(stream)
    value.pop("sockopt", None)
    value.pop("externalProxy", None)
    network = value.get("network", value.get("method"))
    if network in {"raw", "tcp"}:
        value["network"] = "tcp"
    elif network in {"splithttp", "xhttp"}:
        value["network"] = "xhttp"
    value.pop("method", None)
    for key in ("tlsSettings", "realitySettings"):
        if isinstance(value.get(key), dict):
            value[key].pop("settings", None)
    return value


def settings_identity(settings):
    clients = settings.get("clients", [])
    if not isinstance(clients, list):
        raise RepairError("invalid_client_settings")
    identity = []
    for client in clients:
        if not isinstance(client, dict):
            raise RepairError("invalid_client_settings")
        if client.get("enable") is False:
            continue
        identity.append({key: client.get(key, "") for key in ("id", "flow", "email")})
    return {"clients": sorted(identity, key=lambda c: (str(c["id"]), str(c["email"]))),
            "decryption": settings.get("decryption", "none"),
            "fallbacks": settings.get("fallbacks")}


def target_runtime(runtime, item, *, after=False):
    matches = [r for r in runtime.get("inbounds", []) if r.get("tag") == item["tag"]]
    if not item["enable"]:
        if matches:
            raise RepairError("disabled_inbound_present_in_runtime")
        return None
    if len(matches) != 1:
        raise RepairError("runtime_target_missing_or_duplicated")
    live = matches[0]
    row = item["row_before"]
    expected_stream = object_json(item["after"] if after else item["before"], "invalid_planned_stream")
    if (live.get("port") != row["port"] or live.get("protocol") != row["protocol"]
            # The panel emits 0.0.0.0 for an empty database listen address.
            or (live.get("listen") or "0.0.0.0") != (row.get("listen") or "0.0.0.0")
            or settings_identity(live.get("settings", {})) != settings_identity(object_json(row["settings"], "invalid_settings_json"))
            or normalized_stream(live.get("streamSettings", {})) != normalized_stream(expected_stream)
            or live.get("streamSettings", {}).get("sockopt", {}) != expected_stream.get("sockopt", {})):
        raise RepairError("runtime_database_mismatch")
    return live


class DefaultAdapter:
    def __init__(self, config):
        self.config = config
        self.systemd = shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()

    def is_active(self):
        args = ["systemctl", "is-active", "--quiet", self.config["service"]] if self.systemd else ["rc-service", self.config["service"], "status"]
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15).returncode == 0

    def validate(self, path):
        result = subprocess.run([self.config["xray"], "run", "-test", "-c", str(path)],
                                cwd=self.config["install_dir"], capture_output=True, timeout=30)
        if result.returncode:
            raise RepairError("xray_configuration_rejected")

    def restart(self):
        args = ["systemctl", "restart", self.config["service"]] if self.systemd else ["rc-service", self.config["service"], "restart"]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            raise RepairError("panel_restart_failed")

    def listener_snapshot(self):
        result = subprocess.run(["ss", "-H", "-lntup"], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise RepairError("listener_check_failed")
        ports = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 5 and fields[0] in {"tcp", "udp"} and "xray" in line.lower():
                try:
                    ports.add((fields[0], int(fields[4].rsplit(":", 1)[1])))
                except ValueError:
                    continue
        return ports

    def listeners(self):
        return {port for protocol, port in self.listener_snapshot() if protocol == "tcp"}

    def sleep(self, seconds):
        time.sleep(seconds)


def snapshot_listeners(adapter):
    if hasattr(adapter, "listener_snapshot"):
        return adapter.listener_snapshot()
    return adapter.listeners()


def write_state(job, status, **extra):
    result = {"status": status, "updated_at": utc_now(), "pid": os.getpid(),
              "backup_directory": str(job), "job_directory": str(job), **extra}
    atomic_json(Path(job) / "state.json", result)
    return result


def same_guards(actual, before):
    return all(actual.get(key) == before.get(key) for key in GUARD_COLUMNS if key in before)


def apply_database(database, plan):
    with closing(sqlite3.connect(database, timeout=15)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN IMMEDIATE")
        try:
            for item in plan["rows"]:
                row = connection.execute("SELECT * FROM inbounds WHERE id=?", (item["id"],)).fetchone()
                if row is None or row["stream_settings"] != item["before"] or not same_guards(dict(row), item["row_before"]):
                    raise RepairError("concurrent_database_change")
            for item in plan["rows"]:
                changed = connection.execute("UPDATE inbounds SET stream_settings=? WHERE id=? AND stream_settings=?",
                                             (item["after"], item["id"], item["before"])).rowcount
                if changed != 1:
                    raise RepairError("concurrent_database_change")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise


def rollback_database(database, plan):
    conflicts = []
    with closing(sqlite3.connect(database, timeout=15)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        try:
            for item in plan["rows"]:
                row = connection.execute("SELECT stream_settings FROM inbounds WHERE id=?", (item["id"],)).fetchone()
                if row is not None and row[0] == item["before"]:
                    continue
                if row is None or row[0] != item["after"]:
                    conflicts.append(item["id"])
                    continue
                connection.execute("UPDATE inbounds SET stream_settings=? WHERE id=? AND stream_settings=?",
                                   (item["before"], item["id"], item["after"]))
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
    return conflicts


def publish_manifest(path, before, after):
    path = Path(path)
    if after is None:
        return
    if not path.is_file() or path.is_symlink() or path.read_bytes() != before:
        raise RepairError("concurrent_manifest_change")
    temp = path.with_name(path.name + ".repair-" + uuid.uuid4().hex[:8])
    try:
        private_write(temp, after)
        if path.read_bytes() != before:
            raise RepairError("concurrent_manifest_change")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def verify_after(config, plan, adapter, baseline):
    runtime_path = Path(config["install_dir"]) / "bin/config.json"
    failure = None
    for _ in range(config.get("verify_attempts", 20)):
        try:
            if not adapter.is_active():
                raise RepairError("panel_not_active")
            rows = rows_from_db(config["database"])
            for item in plan["rows"]:
                row = rows.get(item["id"])
                if row is None or row["stream_settings"] != item["after"] or not same_guards(row, item["row_before"]):
                    raise RepairError("database_verification_failed")
            runtime = object_json(runtime_path.read_text(encoding="utf-8"), "invalid_runtime_json")
            for item in plan["rows"]:
                target_runtime(runtime, item, after=True)
            if not baseline <= snapshot_listeners(adapter):
                raise RepairError("listeners_not_restored")
        except Exception as exc:
            failure = exc
            adapter.sleep(config.get("verify_delay", 0.5))
            continue
        # A real core rejection is not a readiness delay and must roll back.
        adapter.validate(runtime_path)
        return
    raise RepairError("post_reload_verification_failed") from failure


def verify_rollback(config, plan, adapter, baseline, conflicts):
    runtime_path = Path(config["install_dir"]) / "bin/config.json"
    for _ in range(config.get("verify_attempts", 20)):
        try:
            if not adapter.is_active() or not baseline <= snapshot_listeners(adapter):
                raise RepairError("rollback_service_not_ready")
            current = rows_from_db(config["database"])
            runtime = object_json(runtime_path.read_text(encoding="utf-8"), "invalid_runtime_json")
            for item in plan["rows"]:
                if item["id"] in conflicts:
                    continue
                if current.get(item["id"], {}).get("stream_settings") != item["before"]:
                    raise RepairError("rollback_database_mismatch")
                target_runtime(runtime, item)
            adapter.validate(runtime_path)
            return True
        except Exception:
            adapter.sleep(config.get("verify_delay", 0.5))
    return False


def execute_repair(config, job_dir, adapter=None):
    job = Path(job_dir)
    plan = None
    mutated = False
    manifest_written = False
    adapter = adapter or DefaultAdapter(config)
    manifest_path = Path(config["info_dir"]) / "3x-ui-node-profiles.json"
    try:
        plan = make_plan(config["database"], manifest_path, config.get("inbound_id"))
        if not plan["rows"]:
            return summarize(plan)
        job.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(job, 0o700)
        write_state(job, "running", **plan_details(plan))
        if not adapter.is_active():
            raise RepairError("panel_not_active")
        runtime_path = Path(config["install_dir"]) / "bin/config.json"
        runtime_bytes = runtime_path.read_bytes()
        runtime = object_json(runtime_bytes.decode("utf-8"), "invalid_runtime_json")
        candidate = copy.deepcopy(runtime)
        for item in plan["rows"]:
            live = target_runtime(candidate, item)
            if live is not None:
                options = live["streamSettings"].get("sockopt", {})
                for change in item["changes"]:
                    if change["after"] is None:
                        options.pop(change["field"], None)
                    else:
                        options[change["field"]] = change["after"]
                live["streamSettings"]["sockopt"] = options
        baseline = snapshot_listeners(adapter)
        tcp = adapter.listeners()
        if any(item["enable"] and item["port"] not in tcp for item in plan["rows"]):
            raise RepairError("original_listener_missing")
        private_write(job / "runtime-before.json", runtime_bytes)
        private_write(job / "plan.json", json.dumps(plan["rows"], ensure_ascii=False, indent=2))
        if plan["manifest_before"] is not None:
            private_write(job / "manifest-before.json", plan["manifest_before"])
        with closing(sqlite3.connect(Path(config["database"]).resolve().as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(job / "x-ui-before.db")) as backup:
                source.backup(backup)
        os.chmod(job / "x-ui-before.db", 0o600)
        candidate_path = job / "candidate.json"
        private_write(candidate_path, json.dumps(candidate, ensure_ascii=False))
        adapter.validate(candidate_path)
        if runtime_path.read_bytes() != runtime_bytes:
            raise RepairError("runtime_changed_during_preflight")
        if plan["manifest_before"] is not None and manifest_path.read_bytes() != plan["manifest_before"]:
            raise RepairError("concurrent_manifest_change")
        apply_database(config["database"], plan)
        mutated = True
        if plan["manifest_after"] is not None:
            publish_manifest(manifest_path, plan["manifest_before"], plan["manifest_after"])
            manifest_written = True
        adapter.restart()
        verify_after(config, plan, adapter, baseline)
        details = plan_details(plan)
        details["message"] = "旧参数已修复，生成配置和原监听已验证；原节点与订阅链接保持不变。"
        if details["review_required"]:
            details["message"] += " 另有未自动处理的入站，请查看 checked_nodes。"
        return write_state(job, "complete", **details, subscription_links_unchanged=True)
    except Exception as exc:
        code = str(exc) if isinstance(exc, RepairError) else type(exc).__name__
        conflicts = []
        if mutated and plan is not None:
            try:
                conflicts = rollback_database(config["database"], plan)
            except Exception:
                conflicts = [item["id"] for item in plan["rows"]]
            if manifest_written:
                try:
                    publish_manifest(manifest_path, plan["manifest_after"], plan["manifest_before"])
                except Exception:
                    conflicts.append("manifest")
            try:
                adapter.restart()
                if not verify_rollback(config, plan, adapter, baseline, conflicts):
                    conflicts.append("service")
            except Exception:
                conflicts.append("service")
        result = {"status": "manual_recovery_required" if conflicts else "rolled_back" if mutated else "failed",
                  "error": code, "conflicts": conflicts, "backup_directory": str(job)}
        if job.exists():
            return write_state(job, result.pop("status"), **{k: v for k, v in result.items() if k != "backup_directory"})
        return result


def read_job(config, job_directory):
    root = Path(config["info_dir"]) / ROOT_NAME
    job = Path(job_directory)
    if (root.is_symlink() or job.is_symlink() or job.parent != root
            or not re.fullmatch(JOB_NAME, job.name)):
        raise RepairError("unexpected_status_path")
    path = job / "state.json"
    if path.is_symlink():
        raise RepairError("unexpected_state_path")
    state = json.loads(path.read_text())
    if state.get("status") not in {"starting", "running"}:
        return state
    # The inherited lock, unlike a PID, also covers startup and cannot be reused
    # by an unrelated process. A status query never changes the saved evidence.
    import fcntl
    lock_path = root / "repair.lock"
    if lock_path.is_symlink():
        raise RepairError("unexpected_lock_path")
    try:
        descriptor = os.open(lock_path, os.O_RDONLY)
    except FileNotFoundError:
        return {**state, "status": "manual_recovery_required", "error": "worker_interrupted"}
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return state
        # The worker may have completed between reading state and taking lock.
        state = json.loads(path.read_text())
        if state.get("status") in {"starting", "running"}:
            return {**state, "status": "manual_recovery_required", "error": "worker_interrupted"}
        return state
    finally:
        os.close(descriptor)


def read_latest(config):
    root = Path(config["info_dir"]) / ROOT_NAME
    latest = root / "latest.json"
    if root.is_symlink() or latest.is_symlink():
        raise RepairError("unexpected_status_path")
    if not latest.is_file():
        return {"status": "no_previous_job"}
    value = json.loads(latest.read_text())
    name = value.get("job", "")
    if not re.fullmatch(JOB_NAME, name):
        raise RepairError("invalid_job_pointer")
    return read_job(config, root / name)


def launch_repair(config):
    import fcntl
    root = Path(config["info_dir"]) / ROOT_NAME
    if root.is_symlink():
        raise RepairError("unexpected_job_root")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    lock_path = root / "repair.lock"
    if lock_path.is_symlink():
        raise RepairError("unexpected_lock_path")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            active = read_latest(config)
            if active["status"] not in {"starting", "running"}:
                # Another launcher may hold the lock before publishing its new
                # pointer. The previous terminal job is not that launch's result.
                return {"status": "busy", "message": "已有任务正在准备或收尾；请稍后重新运行本命令。"}
            if active:
                try:
                    active_config = Path(active.get("job_directory") or active.get("backup_directory")) / "config.json"
                    if active_config.is_symlink():
                        raise ValueError()
                    running = json.loads(active_config.read_text())
                    if not isinstance(running, dict):
                        raise ValueError()
                    if any(running.get(key) != config.get(key) for key in
                           ("database", "inbound_id", "install_dir", "service")):
                        raise ValueError()
                except (OSError, ValueError, TypeError, KeyError):
                    return {"status": "busy", "message": "已有任务运行中，无法确认与本次目标一致；请等待结束后重新运行本命令。"}
            return {**active, "status": "busy", "message": "已有修复任务运行中，正在等待其结果。"}
        previous = read_latest(config)
        if previous["status"] in {"starting", "running", "manual_recovery_required"}:
            return {**previous, "status": "manual_recovery_required",
                    "error": previous.get("error", "worker_interrupted")}
        plan = make_plan(config["database"], Path(config["info_dir"]) / "3x-ui-node-profiles.json", config.get("inbound_id"))
        name = "job-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
        job = root / name
        job.mkdir(mode=0o700)
        if not plan["rows"]:
            result = write_state(job, "no_changes", **plan_details(plan))
            atomic_json(root / "latest.json", {"job": name})
            return result
        private_write(job / "worker.py", Path(__file__).read_bytes())
        private_write(job / "config.json", json.dumps(config))
        write_state(job, "starting", **plan_details(plan))
        atomic_json(root / "latest.json", {"job": name})
        log_fd = os.open(job / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            process = subprocess.Popen([sys.executable, str(job / "worker.py"), "_worker", str(job), str(descriptor)],
                                       stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                                       start_new_session=True, close_fds=True, pass_fds=(descriptor,))
        except Exception:
            write_state(job, "failed", error="worker_start_failed")
            raise
        finally:
            os.close(log_fd)
        return {"status": "started", "pid": process.pid, "backup_directory": str(job), "job_directory": str(job),
                "message": "修复任务已启动，正在等待最终结果；SSH 断开后后台任务仍会继续。"}
    finally:
        os.close(descriptor)


def wait_for_completion(config, result, timeout=60, interval=0.5):
    if result["status"] not in {"started", "busy", "starting", "running"}:
        return result
    job_directory = result.get("job_directory", result.get("backup_directory"))
    if not job_directory:
        return result
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        state = read_job(config, job_directory)
        if state["status"] not in {"starting", "running"}:
            return state
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**state, "wait_timed_out": True,
                    "message": "等待最终结果超时，后台任务仍在运行；请用 --repair-performance-status 查询。"}
        time.sleep(min(max(interval, 0.01), remaining))


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_worker":
        job, descriptor = Path(argv[1]), int(argv[2])
        os.umask(0o077)
        try:
            config = json.loads((job / "config.json").read_text())
            result = execute_repair(config, job)
            write_state(job, result["status"], **{key: value for key, value in result.items()
                                                if key not in {"status", "updated_at", "pid", "backup_directory"}})
        except Exception as exc:
            write_state(job, "failed", error=type(exc).__name__)
        finally:
            os.close(descriptor)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "repair", "status"))
    parser.add_argument("--database", required=True)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--info-dir", required=True)
    parser.add_argument("--service", default="x-ui")
    parser.add_argument("--xray", default="")
    parser.add_argument("--inbound-id", type=int)
    args = parser.parse_args(argv)
    config = vars(args).copy()
    mode = config.pop("mode")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]*", config["service"]):
        raise RepairError("invalid_service_name")
    for key in ("database", "install_dir", "info_dir"):
        config[key] = str(Path(config[key]).resolve())
    if config["xray"]:
        config["xray"] = str(Path(config["xray"]).resolve())
    if mode == "check":
        result = summarize(make_plan(config["database"], Path(config["info_dir"]) / "3x-ui-node-profiles.json", config["inbound_id"]))
    elif mode == "status":
        result = read_latest(config)
    else:
        if not config["xray"] or not Path(config["xray"]).is_file():
            raise RepairError("xray_binary_missing")
        result = launch_repair(config)
        if result["status"] in {"started", "busy"}:
            print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
            result = wait_for_completion(config, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if (result["status"] not in {"failed", "rolled_back", "manual_recovery_required"}
                 and not (result["status"] == "no_changes" and result.get("review_required"))) else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        code = str(exc) if isinstance(exc, RepairError) else type(exc).__name__
        print(json.dumps({"status": "failed", "error": code}), file=sys.stderr)
        raise SystemExit(1)
