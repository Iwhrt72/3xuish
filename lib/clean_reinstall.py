"""Backed-up, detached clean redeployment of a default SQLite 3x-ui install."""
from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import json
import os
from pathlib import Path
import re
import signal
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid

DEFAULT_PATHS = {
    "database": "/etc/x-ui/x-ui.db", "db_dir": "/etc/x-ui",
    "install_dir": "/usr/local/x-ui", "info_dir": "/root",
    "command": "/usr/bin/x-ui", "unit": "/etc/systemd/system/x-ui.service",
    "dropins": "/etc/systemd/system/x-ui.service.d", "openrc": "/etc/init.d/x-ui",
    "cert_root": "/root/cert", "acme_root": "/root/.acme.sh",
    "backup_root": "/root/3xuish-reinstall",
}
INFO_FILES = ("3x-ui-node-profiles.json", "3x-ui-nodes-info.txt",
              "3x-ui-node-links.txt", "3x-ui-xhttp-info.txt")
INSTALL_ENV = ("PRIMARY_NODE", "CERT_MODE", "XUI_DOMAIN", "XUI_SERVER_IP", "XUI_USERNAME",
    "XUI_PASSWORD", "XUI_PANEL_PORT", "XUI_WEB_BASE_PATH", "RAW_PORT", "REALITY_PORT",
    "HY2_PORT", "XHTTP_PORT", "XHTTP_PATH", "VLESS_ENC_TYPE", "INBOUND_TLS_MODE",
    "REALITY_SERVER_NAME", "REALITY_TARGET")
JOB_NAME = r"job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}"
ACTIVE = {"starting", "running"}
OPERATIONS = {"reinstall", "uninstall"}


class ReinstallError(RuntimeError):
    """Fixed, non-sensitive error identifiers only."""


def private_write(path, value):
    data = value if isinstance(value, bytes) else value.encode("utf-8")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".next-" + uuid.uuid4().hex[:8])
    private_write(temporary, json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


def write_state(job, status, **extra):
    result = {"status": status, "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
              "pid": os.getpid(), "job_directory": str(job), "backup_directory": str(job), **extra}
    atomic_json(Path(job) / "state.json", result)
    return result


def safe_path(path):
    path = Path(path).absolute()
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ReinstallError("symlinked_managed_path")
    return path


def operation_name(config):
    value = config.get("operation", "reinstall")
    if value not in OPERATIONS:
        raise ReinstallError("unsupported_maintenance_operation")
    return value


def managed_paths():
    result = {key: Path(DEFAULT_PATHS[key]) for key in
              ("install_dir", "db_dir", "command", "unit", "dropins", "openrc")}
    result.update({"info-" + str(index): Path(DEFAULT_PATHS["info_dir"]) / name
                   for index, name in enumerate(INFO_FILES)})
    return result


def snapshot_paths(config):
    """Only the certificate target used by this deployment is mutable here."""
    if operation_name(config) == "uninstall":
        return []
    environment = config.get("install_env", {})
    domain = environment.get("XUI_DOMAIN", "")
    if environment.get("CERT_MODE", "1") == "2":
        if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", domain) or "." not in domain:
            raise ReinstallError("invalid_reinstall_certificate_domain")
        target = Path(DEFAULT_PATHS["cert_root"]) / domain
    else:
        target = Path(DEFAULT_PATHS["cert_root"]) / "ip"
        domain = environment.get("XUI_SERVER_IP", "")
    files = [target / "fullchain.pem", target / "privkey.pem"]
    # acme's account/domain configuration may be updated by the issue command.
    # Preserve those exact files; never relocate/delete the acme installation.
    acme = Path(DEFAULT_PATHS["acme_root"])
    files.append(acme / "account.conf")
    if domain and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}", domain):
        files.extend((acme / domain / (domain + ".conf"),
                      acme / (domain + "_ecc") / (domain + ".conf")))
    return files


def preflight_layout(config, job):
    operation = operation_name(config)
    for key in ("database", "install_dir", "info_dir"):
        if Path(config[key]).absolute() != Path(DEFAULT_PATHS[key]).absolute():
            raise ReinstallError("only_default_sqlite_layout_supported")
    if config.get("service", "x-ui") != "x-ui":
        raise ReinstallError("only_default_service_supported")
    expected_environment = {"XUI_MAIN_FOLDER": str(DEFAULT_PATHS["install_dir"]),
        "XUI_DB_FOLDER": str(DEFAULT_PATHS["db_dir"]), "XUI_DB_TYPE": "sqlite",
        "XUI_DB_DSN": "", "XUI_BIN_FOLDER": str(Path(DEFAULT_PATHS["install_dir"]) / "bin")}
    for key, expected in expected_environment.items():
        if os.environ.get(key) not in (None, "", expected):
            raise ReinstallError("custom_database_environment_not_supported")
    paths = managed_paths()
    for path in (*paths.values(), Path(DEFAULT_PATHS["database"]), Path(DEFAULT_PATHS["backup_root"]), *snapshot_paths(config)):
        safe_path(path)
    if operation == "reinstall" and not Path(config["database"]).is_file():
        raise ReinstallError("existing_database_missing")
    if operation == "reinstall" and not Path(config["install_dir"]).is_dir():
        raise ReinstallError("existing_program_missing")
    if operation == "reinstall" and not Path(config.get("installer", "")).is_file():
        raise ReinstallError("installer_snapshot_missing")
    if config.get("upstream_installer"):
        upstream = safe_path(config["upstream_installer"])
        if not upstream.is_file():
            raise ReinstallError("upstream_installer_snapshot_missing")
    if operation == "uninstall" and not any(path.exists() for path in paths.values()):
        raise ReinstallError("existing_installation_missing")
    job = safe_path(job)
    if job.parent != Path(DEFAULT_PATHS["backup_root"]).absolute():
        raise ReinstallError("unexpected_job_directory")
    for path in (*paths.values(), *snapshot_paths(config)):
        if path.exists() and path.stat().st_dev != job.parent.stat().st_dev:
            raise ReinstallError("cross_filesystem_archive_not_supported")
    # These environment files can redirect the panel to an unrelated database.
    environment_files = ("/etc/default/x-ui", "/etc/conf.d/x-ui", "/etc/sysconfig/x-ui")
    for filename in environment_files:
        path = Path(filename)
        if path.is_file():
            safe_path(path)
            text = path.read_text(encoding="utf-8", errors="replace")
            for key, value in re.findall(r"(?m)^\s*(?:export\s+)?(XUI_MAIN_FOLDER|XUI_BIN_FOLDER|XUI_DB_TYPE|XUI_DB_FOLDER|XUI_DB_DSN)\s*=\s*([^\n#]*)", text):
                value = value.strip().strip("\"'")
                if value not in {"", expected_environment[key]}:
                    raise ReinstallError("custom_database_environment_not_supported")
    # Do not act on guessed defaults when the live unit redirects the service.
    # Only inspect known unit files; never source environment/shell content.
    service_files = [paths["unit"], paths["openrc"]]
    if paths["dropins"].is_dir():
        service_files.extend(paths["dropins"].glob("*.conf"))
    expected_binary = str(Path(DEFAULT_PATHS["install_dir"]) / "x-ui")
    for path in service_files:
        if not path.is_file():
            continue
        safe_path(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        for key, value in re.findall(r"(?m)^\s*(?:export\s+)?(XUI_MAIN_FOLDER|XUI_BIN_FOLDER|XUI_DB_TYPE|XUI_DB_FOLDER|XUI_DB_DSN)\s*=\s*([^\n#]*)", text):
            if value.strip().strip("\"'") not in {"", expected_environment[key]}:
                raise ReinstallError("custom_service_environment_not_supported")
        for directive, value in re.findall(r"(?m)^\s*(Environment|EnvironmentFile|ExecStart)\s*=\s*([^\n]*)", text):
            try:
                tokens = shlex.split(value)
            except ValueError:
                raise ReinstallError("custom_service_environment_not_supported") from None
            if directive == "ExecStart":
                if tokens and (tokens[0] != expected_binary or tokens[1:] not in ([], ["run"])):
                    raise ReinstallError("custom_service_program_not_supported")
            elif directive == "EnvironmentFile":
                if any(token.lstrip("-") not in environment_files for token in tokens):
                    raise ReinstallError("custom_service_environment_not_supported")
            else:
                for token in tokens:
                    key, separator, value = token.partition("=")
                    if separator and key in expected_environment and value not in {"", expected_environment[key]}:
                        raise ReinstallError("custom_service_environment_not_supported")
    return paths


class DefaultAdapter:
    def __init__(self, config):
        self.config = config
        self.systemd = shutil.which("systemctl") is not None and Path("/run/systemd/system").is_dir()

    def preflight(self):
        if not self.systemd:
            if self.openrc_runlevels() - {"default"}:
                raise ReinstallError("custom_openrc_runlevels_not_supported")
            if self.is_active() and not Path(DEFAULT_PATHS["openrc"]).is_file():
                raise ReinstallError("custom_service_program_not_supported")
            return
        fragment = subprocess.run(["systemctl", "show", "x-ui", "--property=FragmentPath", "--value"],
                                  capture_output=True, text=True, timeout=20)
        if fragment.returncode:
            raise ReinstallError("effective_service_layout_unavailable")
        unit = fragment.stdout.strip()
        if unit and safe_path(unit) != Path(DEFAULT_PATHS["unit"]):
            raise ReinstallError("custom_service_program_not_supported")
        if not unit and self.is_active():
            raise ReinstallError("effective_service_layout_unavailable")
        dropins = subprocess.run(["systemctl", "show", "x-ui", "--property=DropInPaths", "--value"],
                                 capture_output=True, text=True, timeout=20)
        if dropins.returncode:
            raise ReinstallError("effective_service_layout_unavailable")
        for name in dropins.stdout.strip().split():
            path = safe_path(name)
            if path.parent != Path(DEFAULT_PATHS["dropins"]):
                raise ReinstallError("custom_service_environment_not_supported")

    def is_active(self):
        args = ["systemctl", "is-active", "--quiet", "x-ui"] if self.systemd else ["rc-service", "x-ui", "status"]
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20).returncode == 0

    def stop(self):
        args = ["systemctl", "stop", "x-ui"] if self.systemd else ["rc-service", "x-ui", "stop"]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode and self.is_active():
            raise ReinstallError("panel_stop_failed")

    def start(self):
        self.refresh()
        args = ["systemctl", "start", "x-ui"] if self.systemd else ["rc-service", "x-ui", "start"]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode:
            raise ReinstallError("panel_start_failed")

    def refresh(self):
        if self.systemd:
            result = subprocess.run(["systemctl", "daemon-reload"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=30)
            if result.returncode:
                raise ReinstallError("service_configuration_reload_failed")

    def is_enabled(self):
        if self.systemd:
            result = subprocess.run(["systemctl", "is-enabled", "x-ui"], capture_output=True,
                                    text=True, timeout=20)
            return result.stdout.strip() in {"enabled", "enabled-runtime", "linked", "linked-runtime"}
        return bool(self.openrc_runlevels())

    @staticmethod
    def openrc_runlevels():
        result = subprocess.run(["rc-update", "show"], capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise ReinstallError("service_enable_state_unavailable")
        levels = set()
        for line in result.stdout.splitlines():
            service, separator, runlevels = line.partition("|")
            if separator and service.strip() == "x-ui":
                levels.update(runlevels.split())
        return levels

    def disable(self):
        args = ["systemctl", "disable", "x-ui"] if self.systemd else ["rc-update", "del", "x-ui", "default"]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode and self.is_enabled():
            raise ReinstallError("service_disable_failed")

    def enable(self):
        args = ["systemctl", "enable", "x-ui"] if self.systemd else ["rc-update", "add", "x-ui", "default"]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30)
        if result.returncode:
            raise ReinstallError("service_enable_restore_failed")

    def listener_snapshot(self):
        result = subprocess.run(["ss", "-H", "-lntup"], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise ReinstallError("listener_check_failed")
        ports = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 5 and fields[0] in {"tcp", "udp"} and "xray" in line.lower():
                try:
                    ports.add((fields[0], int(fields[4].rsplit(":", 1)[1])))
                except ValueError:
                    pass
        return ports

    def validate(self, runtime_path):
        binaries = [path for path in (Path(self.config["install_dir"]) / "bin").glob("xray-linux-*")
                    if path.is_file() and os.access(path, os.X_OK)]
        if len(binaries) != 1:
            raise ReinstallError("unique_xray_binary_missing")
        result = subprocess.run([str(binaries[0]), "run", "-test", "-c", str(runtime_path)],
            cwd=self.config["install_dir"], capture_output=True, timeout=30)
        if result.returncode:
            raise ReinstallError("new_runtime_configuration_rejected")

    def run_install(self, installer, environment, log_path):
        with open(log_path, "ab") as log:
            os.chmod(log_path, 0o600)
            process = subprocess.Popen(["bash", str(installer), "--install"], stdin=subprocess.DEVNULL,
                stdout=log, stderr=log, env=environment, start_new_session=True)
            try:
                code = process.wait(timeout=self.config.get("install_timeout", 1800))
                if code:
                    self.stop_install_process(process)
            except BaseException:
                self.stop_install_process(process)
                raise
        if code:
            raise ReinstallError("fresh_install_failed")

    @staticmethod
    def stop_install_process(process):
        # A timed-out shell may leave acme/curl/install descendants writing.
        # Quiesce its entire session before restoring the original filesystem.
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:
                process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
        except ProcessLookupError:
            pass
        process.wait(timeout=5)

    def sleep(self, seconds):
        time.sleep(seconds)


def backup_mutable_files(config, job):
    records = []
    directory = job / "certificate-before"
    directory.mkdir(mode=0o700)
    for index, path in enumerate(snapshot_paths(config)):
        safe_path(path)
        record = {"path": str(path), "name": str(index), "existed": path.is_file()}
        if path.exists() and not path.is_file():
            raise ReinstallError("unexpected_certificate_file_type")
        if record["existed"]:
            private_write(directory / str(index), path.read_bytes())
            record["mode"] = path.stat().st_mode & 0o777
        records.append(record)
    atomic_json(job / "certificate-plan.json", records)
    return records


def restore_mutable_files(records, job):
    failed = job / "failed-certificates"
    failed.mkdir(mode=0o700, exist_ok=True)
    for record in records:
        path = safe_path(record["path"])
        if path.exists():
            if not path.is_file():
                raise ReinstallError("certificate_restore_conflict")
            os.rename(path, failed / record["name"])
        if record["existed"]:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            private_write(path, (job / "certificate-before" / record["name"]).read_bytes())
            os.chmod(path, record["mode"])


def verify_new_install(config, adapter):
    with closing(sqlite3.connect(Path(config["database"]).resolve().as_uri() + "?mode=ro", uri=True)) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute("SELECT * FROM inbounds")]
    profiles = json.loads((Path(config["info_dir"]) / INFO_FILES[0]).read_text(encoding="utf-8"))
    if (len(rows) != 4 or not isinstance(profiles, list) or len(profiles) != 4
            or {item.get("key") for item in profiles} != {"hy2", "raw", "reality", "xhttp"}):
        raise ReinstallError("fresh_four_nodes_not_verified")
    runtime_path = Path(config["install_dir"]) / "bin/config.json"
    runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
    listeners = adapter.listener_snapshot()
    for profile in profiles:
        payload = profile.get("payload", {})
        matches = [row for row in rows if row.get("tag") == payload.get("tag")]
        if len(matches) != 1 or not matches[0].get("enable"):
            raise ReinstallError("fresh_four_nodes_not_verified")
        row = matches[0]
        if (row.get("port") != payload.get("port") or row.get("protocol") != payload.get("protocol")
                or not str(row.get("tag", "")).startswith("3xuish-")):
            raise ReinstallError("fresh_four_nodes_not_verified")
        live = [item for item in runtime.get("inbounds", []) if item.get("tag") == row["tag"]]
        protocol = "udp" if profile["key"] == "hy2" else "tcp"
        if len(live) != 1 or live[0].get("port") != row["port"] or (protocol, row["port"]) not in listeners:
            raise ReinstallError("fresh_listeners_not_verified")
    if not adapter.is_active():
        raise ReinstallError("new_panel_not_active")
    adapter.validate(runtime_path)


def wait_stopped(config, adapter, baseline):
    for _ in range(config.get("verify_attempts", 30)):
        if not adapter.is_active() and not (baseline & adapter.listener_snapshot()):
            return
        adapter.sleep(config.get("verify_delay", .5))
    raise ReinstallError("old_panel_or_listeners_still_active")


def execute_reinstall(config, job_dir, adapter=None):
    job = Path(job_dir)
    adapter = adapter or DefaultAdapter(config)
    moved = []
    certificates = []
    stopped = False
    was_active = False
    was_enabled = None
    install_attempted = False
    operation = "reinstall"
    baseline = set()
    paths = {}
    try:
        operation = operation_name(config)
        root = safe_path(DEFAULT_PATHS["backup_root"])
        if job.absolute().parent != root:
            raise ReinstallError("unexpected_job_directory")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths = preflight_layout(config, job)
        if hasattr(adapter, "preflight"):
            adapter.preflight()
        job.mkdir(mode=0o700, exist_ok=True)
        os.chmod(job, 0o700)
        old = job / "old"
        old.mkdir(mode=0o700)
        (job / "failed").mkdir(mode=0o700)
        records = [{"name": name, "path": str(path), "existed": path.exists()} for name, path in paths.items()]
        atomic_json(job / "archive-plan.json", records)
        if operation == "reinstall":
            certificates = backup_mutable_files(config, job)
        write_state(job, "running", operation=operation, phase="stopping_old_service", old_links_will_change=True)
        was_active = adapter.is_active()
        if hasattr(adapter, "is_enabled"):
            was_enabled = adapter.is_enabled()
        baseline = adapter.listener_snapshot()
        stopped = True
        adapter.stop()
        wait_stopped(config, adapter, baseline)
        if hasattr(adapter, "disable"):
            adapter.disable()
        # The whole stopped SQLite directory is archived, including WAL/SHM.
        if Path(config["database"]).is_file():
            with closing(sqlite3.connect(Path(config["database"]).resolve().as_uri() + "?mode=ro", uri=True)) as source:
                with closing(sqlite3.connect(job / "database-before.db")) as target:
                    source.backup(target)
            os.chmod(job / "database-before.db", 0o600)
        write_state(job, "running", operation=operation, phase="archiving_old_installation", old_links_will_change=True)
        for record in records:
            path = safe_path(record["path"])
            if record["existed"]:
                os.rename(path, old / record["name"])
                moved.append(record["name"])
                atomic_json(job / "moved.json", moved)
        if hasattr(adapter, "refresh"):
            adapter.refresh()
        if operation == "uninstall":
            wait_stopped(config, adapter, baseline)
            if any(path.exists() or path.is_symlink() for path in paths.values()):
                raise ReinstallError("uninstall_paths_still_present")
            if hasattr(adapter, "is_enabled") and adapter.is_enabled():
                raise ReinstallError("uninstall_service_still_enabled")
            return write_state(job, "complete", operation=operation, phase="complete", old_links_changed=True,
                message="已卸载面板程序、服务和旧节点数据；原节点与订阅停止使用，备份保留在任务目录。证书与 acme 账户未删除。")
        write_state(job, "running", operation=operation, phase="fresh_installation", old_links_will_change=True)
        environment = os.environ.copy()
        for key in list(environment):
            if key in INSTALL_ENV or key.startswith("XUI_"):
                environment.pop(key, None)
        environment.update(config.get("install_env", {}))
        environment.update(AUTO_YES="1", XUI_DB_PATH=config["database"],
                           XUI_INSTALL_DIR=config["install_dir"], XUI_INFO_DIR=config["info_dir"], XUI_SERVICE="x-ui")
        environment["XUI_MAINTENANCE_JOB"] = str(job)
        if config.get("upstream_installer"):
            environment["XUI_UPSTREAM_INSTALLER"] = config["upstream_installer"]
        environment.pop("XRAY_BIN", None)
        install_attempted = True
        adapter.run_install(Path(config["installer"]), environment, job / "install.log")
        last_error = None
        for _ in range(config.get("verify_attempts", 30)):
            try:
                verify_new_install(config, adapter)
                break
            except Exception as exc:
                last_error = exc
                adapter.sleep(config.get("verify_delay", .5))
        else:
            raise ReinstallError("fresh_install_verification_failed") from last_error
        return write_state(job, "complete", operation=operation, phase="complete", old_links_changed=True,
            message="已清空旧部署并完成四节点新装；请使用新的面板信息和节点链接。旧数据保留在备份目录。",
            information_file=str(Path(config["info_dir"]) / INFO_FILES[1]),
            links_file=str(Path(config["info_dir"]) / INFO_FILES[2]))
    except Exception as exc:
        error = str(exc) if isinstance(exc, ReinstallError) else type(exc).__name__
        conflicts = []
        if stopped:
            try:
                pending_listeners = baseline | adapter.listener_snapshot()
                adapter.stop()
                wait_stopped(config, adapter, pending_listeners)
                for name, path in reversed(list(paths.items())):
                    safe_path(path)
                    if name in moved:
                        if path.exists():
                            os.rename(path, job / "failed" / name)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        os.rename(job / "old" / name, path)
                    elif not any(record["name"] == name and record["existed"] for record in records) and path.exists():
                        os.rename(path, job / "failed" / name)
                if install_attempted:
                    restore_mutable_files(certificates, job)
                if hasattr(adapter, "refresh"):
                    adapter.refresh()
                if was_enabled is not None:
                    if was_enabled:
                        adapter.enable()
                    elif hasattr(adapter, "disable"):
                        adapter.disable()
                if was_active:
                    adapter.start()
                    for _ in range(config.get("verify_attempts", 30)):
                        if adapter.is_active() and baseline <= adapter.listener_snapshot():
                            break
                        adapter.sleep(config.get("verify_delay", .5))
                    else:
                        raise ReinstallError("old_service_restore_not_verified")
            except Exception:
                conflicts.append("restore_failed")
        result = {"status": "manual_recovery_required" if conflicts else "rolled_back" if stopped else "failed",
                  "operation": operation, "error": error, "conflicts": conflicts,
                  "job_directory": str(job), "backup_directory": str(job)}
        if job.exists():
            return write_state(job, result.pop("status"), **{key: value for key, value in result.items()
                                                          if key not in {"job_directory", "backup_directory"}})
        return result


def read_job(config, job_directory):
    root = Path(DEFAULT_PATHS["backup_root"])
    job = safe_path(job_directory)
    if job.parent != root or not re.fullmatch(JOB_NAME, job.name):
        raise ReinstallError("unexpected_job_directory")
    state_path = safe_path(job / "state.json")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("status") not in ACTIVE:
        return state
    import fcntl
    lock = safe_path(root / "reinstall.lock")
    try:
        descriptor = os.open(lock, os.O_RDONLY)
    except FileNotFoundError:
        return {**state, "status": "manual_recovery_required", "error": "worker_interrupted"}
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return state
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if state.get("status") in ACTIVE:
            return {**state, "status": "manual_recovery_required", "error": "worker_interrupted"}
        return state
    finally:
        os.close(descriptor)


def read_latest(config, *, any_operation=False):
    root = safe_path(DEFAULT_PATHS["backup_root"])
    operation = operation_name(config)
    pointer = safe_path(root / ("latest.json" if any_operation else "latest-" + operation + ".json"))
    legacy_pointer = False
    if not any_operation and not pointer.is_file():
        pointer = safe_path(root / "latest.json")
        legacy_pointer = True
    if not pointer.is_file():
        return {"status": "no_previous_job", "operation": operation}
    name = json.loads(pointer.read_text(encoding="utf-8")).get("job", "")
    if not re.fullmatch(JOB_NAME, name):
        raise ReinstallError("invalid_job_pointer")
    state = read_job(config, root / name)
    if legacy_pointer and state.get("operation", "reinstall") != operation:
        return {"status": "no_previous_job", "operation": operation}
    if not any_operation and state.get("operation", "reinstall") != operation:
        raise ReinstallError("operation_status_pointer_mismatch")
    return state


def launch_reinstall(config):
    import fcntl
    operation = operation_name(config)
    root = safe_path(DEFAULT_PATHS["backup_root"])
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    descriptors = []
    try:
        for lock in (root / "reinstall.lock", Path(DEFAULT_PATHS["info_dir"]) / "3xuish-performance-repair/repair.lock"):
            safe_path(lock)
            lock.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            descriptor = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
            descriptors.append(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "busy", "operation": operation,
                        "message": "已有重装、卸载或修复任务运行中；请等待完成后再重试。"}
        previous = read_latest(config, any_operation=True)
        if previous.get("status") in ACTIVE | {"manual_recovery_required"}:
            return {**previous, "status": "manual_recovery_required", "error": "previous_reinstall_needs_recovery"}
        name = "job-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
        job = root / name
        preflight_layout(config, job)
        job.mkdir(mode=0o700)
        private_write(job / "worker.py", Path(__file__).read_bytes())
        config = {**config, "operation": operation}
        if operation == "reinstall":
            private_write(job / "installer.sh", Path(config["installer"]).read_bytes())
            config["installer"] = str(job / "installer.sh")
            if config.get("upstream_installer"):
                private_write(job / "upstream-install.sh", Path(config["upstream_installer"]).read_bytes())
                config["upstream_installer"] = str(job / "upstream-install.sh")
        private_write(job / "config.json", json.dumps(config))
        write_state(job, "starting", operation=operation, old_links_will_change=True)
        atomic_json(root / "latest.json", {"job": name})
        atomic_json(root / ("latest-" + operation + ".json"), {"job": name})
        log_fd = os.open(job / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            process = subprocess.Popen([sys.executable, str(job / "worker.py"), "_worker", str(job),
                *map(str, descriptors)], stdin=subprocess.DEVNULL, stdout=log_fd, stderr=log_fd,
                start_new_session=True, close_fds=True, pass_fds=tuple(descriptors))
        except Exception:
            write_state(job, "failed", operation=operation, error="worker_start_failed")
            raise
        finally:
            os.close(log_fd)
        return {"status": "started", "operation": operation, "pid": process.pid,
                "job_directory": str(job), "backup_directory": str(job),
                "message": "卸载任务已启动，SSH 断开后仍会继续；正在等待结果。" if operation == "uninstall"
                           else "清空重装任务已启动，SSH 断开后仍会继续；正在等待结果。"}
    finally:
        for descriptor in descriptors:
            os.close(descriptor)


def wait_for_completion(config, result, timeout=60):
    if result.get("status") != "started":
        return result
    deadline = time.monotonic() + max(timeout, 0)
    while True:
        state = read_job(config, result["job_directory"])
        if state.get("status") not in ACTIVE:
            return state
        if time.monotonic() >= deadline:
            return {**state, "wait_timed_out": True,
                    "message": "后台仍在执行，请稍后用 " + ("--uninstall-status" if operation_name(config) == "uninstall"
                                                                  else "--reinstall-status") + " 查询；不要重复启动。"}
        time.sleep(.5)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_worker":
        job = Path(argv[1])
        descriptors = list(map(int, argv[2:]))
        os.umask(0o077)
        config = {}
        try:
            config = json.loads((job / "config.json").read_text(encoding="utf-8"))
            execute_reinstall(config, job)
        except Exception as exc:
            write_state(job, "manual_recovery_required", operation=operation_name(config), error=type(exc).__name__)
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("launch", "status"))
    parser.add_argument("--operation", choices=sorted(OPERATIONS), default="reinstall")
    parser.add_argument("--database", default=DEFAULT_PATHS["database"])
    parser.add_argument("--install-dir", default=DEFAULT_PATHS["install_dir"])
    parser.add_argument("--info-dir", default=DEFAULT_PATHS["info_dir"])
    parser.add_argument("--service", default="x-ui")
    parser.add_argument("--installer", default="")
    parser.add_argument("--upstream-installer", default="")
    args = parser.parse_args(argv)
    config = vars(args).copy()
    config.pop("mode")
    for key in ("database", "install_dir", "info_dir"):
        config[key] = str(Path(config[key]).absolute())
    config["install_env"] = {key: os.environ[key] for key in INSTALL_ENV if key in os.environ}
    if args.mode == "status":
        result = read_latest(config)
    else:
        result = launch_reinstall(config)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        result = wait_for_completion(config, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result["status"] in {"failed", "rolled_back", "manual_recovery_required", "busy"} else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        error = str(exc) if isinstance(exc, ReinstallError) else type(exc).__name__
        print(json.dumps({"status": "failed", "error": error}), file=sys.stderr)
        raise SystemExit(1)
