"""Detached, recoverable upgrades of a native default SQLite 3x-ui panel."""
from __future__ import annotations

import argparse
from contextlib import closing
import datetime as dt
import hashlib
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import sqlite3
import ssl
import subprocess
import sys
import time
import uuid

import panel_release


DEFAULT_PATHS = {
    "database": "/etc/x-ui/x-ui.db", "db_dir": "/etc/x-ui",
    "install_dir": "/usr/local/x-ui", "info_dir": "/root", "command": "/usr/bin/x-ui",
    "unit": "/etc/systemd/system/x-ui.service", "dropins": "/etc/systemd/system/x-ui.service.d",
    "openrc": "/etc/init.d/x-ui", "backup_root": "/root/3xuish-panel-update",
}
ACTIVE = {"starting", "running"}
FINISHED = {"complete", "no_update", "failed", "rolled_back"}
TERMINAL = FINISHED | {"manual_recovery_required"}
JOB_NAME = r"job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}"
_CHILDREN = {}
ENVIRONMENT_FILES = ("/etc/default/x-ui", "/etc/conf.d/x-ui", "/etc/sysconfig/x-ui")


class UpgradeError(RuntimeError):
    """Non-sensitive, fixed error identifiers only."""


def safe_path(value):
    path = Path(value).absolute()
    if any(item.is_symlink() for item in (path, *path.parents)):
        raise UpgradeError("symlinked_managed_path")
    return path


def private_write(path, data):
    data = data if isinstance(data, bytes) else data.encode("utf-8")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def atomic_json(path, value):
    path = safe_path(path)
    temporary = path.with_name(path.name + ".next-" + uuid.uuid4().hex[:8])
    private_write(temporary, json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


def write_state(job, status, **fields):
    result = {"operation": "panel_update", "status": status, "job_directory": str(job),
              "backup_directory": str(job / "backup"), "log_file": str(job / "worker.log"),
              "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), **fields}
    atomic_json(job / "state.json", result)
    return result


def _state(path):
    value = json.loads(safe_path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        raise UpgradeError("invalid_job_state")
    return value


def read_job(job):
    job = safe_path(job)
    if not re.fullmatch(JOB_NAME, job.name):
        raise UpgradeError("invalid_job_name")
    state = _state(job / "state.json")
    if state["status"] not in ACTIVE | TERMINAL:
        raise UpgradeError("invalid_job_state")
    if state["status"] not in ACTIVE:
        return state
    import fcntl
    try:
        fd = os.open(safe_path(job / "worker.lock"), os.O_RDONLY)
    except FileNotFoundError:
        return {**state, "status": "manual_recovery_required", "error": "worker_interrupted"}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return state
        state = _state(job / "state.json")
        if state["status"] in ACTIVE:
            return {**state, "status": "manual_recovery_required", "error": "worker_interrupted"}
        return state
    finally:
        os.close(fd)


def read_latest(info_dir):
    root = safe_path(Path(info_dir) / "3xuish-panel-update")
    pointer = safe_path(root / "latest.json")
    if not pointer.exists():
        return {"operation": "panel_update", "status": "no_previous_job"}
    value = json.loads(pointer.read_text(encoding="utf-8"))
    name = value.get("job") if isinstance(value, dict) else None
    if not isinstance(name, str) or not re.fullmatch(JOB_NAME, name):
        raise UpgradeError("invalid_job_pointer")
    return read_job(root / name)


def require_finished_jobs(info_dir):
    for directory in ("3xuish-panel-update", "3xuish-reinstall", "3xuish-socks-exit", "3xuish-performance-repair"):
        root = safe_path(Path(info_dir) / directory)
        pointer = safe_path(root / "latest.json")
        if not pointer.exists():
            continue
        value = json.loads(pointer.read_text(encoding="utf-8"))
        name = value.get("job") if isinstance(value, dict) else None
        if not isinstance(name, str) or not re.fullmatch(JOB_NAME, name):
            raise UpgradeError("previous_maintenance_needs_recovery")
        state = _state(root / name / "state.json")
        if state["status"] not in FINISHED | {"noop"}:
            raise UpgradeError("previous_maintenance_needs_recovery")


def _expected_environment():
    return {"XUI_MAIN_FOLDER": DEFAULT_PATHS["install_dir"], "XUI_BIN_FOLDER": str(Path(DEFAULT_PATHS["install_dir"]) / "bin"),
            "XUI_DB_FOLDER": DEFAULT_PATHS["db_dir"], "XUI_DB_TYPE": "sqlite", "XUI_DB_DSN": ""}


def _check_environment(values):
    for name, expected in _expected_environment().items():
        if values.get(name) not in (None, "", expected):
            raise UpgradeError("custom_database_environment_not_supported")


def check_environment_file(path):
    path = Path(path)
    if path.is_file():
        safe_path(path)
        content = path.read_text(encoding="utf-8", errors="replace").lstrip("\ufeff")
        names = "|".join(_expected_environment())
        for line in content.splitlines():
            if not line.strip() or line.lstrip().startswith("#") or not re.search(r"\b(?:" + names + r")\b", line):
                continue
            # godotenv accepts both KEY=value and YAML-style KEY: value.
            # Keep quoted '#' characters, and reject syntax/interpolation we
            # cannot interpret instead of assuming it leaves the DB unchanged.
            assignment = re.fullmatch(r"\s*(?:export\s+)?(" + names + r")\s*[:=]\s*(.*?)\s*", line)
            if not assignment:
                raise UpgradeError("custom_database_environment_not_supported")
            raw = assignment.group(2).strip()
            if raw.startswith(("'", '"')):
                # Accept a single quoted literal followed only by a comment.
                # Shell-style concatenation is not godotenv syntax.
                quoted = re.fullmatch(r'''("(?:[^"\\]|\\.)*"|'[^']*')\s*(?:\#.*)?''', raw)
                if not quoted:
                    raise UpgradeError("custom_database_environment_not_supported")
                try:
                    value = shlex.split(quoted.group(1), comments=False, posix=True)[0]
                except (ValueError, IndexError):
                    raise UpgradeError("custom_database_environment_not_supported") from None
            else:
                # godotenv only starts an unquoted inline comment after space;
                # /etc/x-ui#other is a different database path, not a comment.
                value = re.split(r"\s+#", raw, maxsplit=1)[0].strip()
                if any(c in value for c in "'\"\\") or any(c.isspace() for c in value):
                    raise UpgradeError("custom_database_environment_not_supported")
            if "$" in value or "`" in value:
                raise UpgradeError("custom_database_environment_not_supported")
            _check_environment({assignment.group(1): value})


def validate_layout(config):
    for name in ("database", "install_dir", "info_dir"):
        if safe_path(config[name]) != Path(DEFAULT_PATHS[name]).absolute():
            raise UpgradeError("only_default_native_sqlite_supported")
    if config.get("service", "x-ui") != "x-ui":
        raise UpgradeError("only_default_service_supported")
    for value in DEFAULT_PATHS.values():
        safe_path(value)
    if not Path(config["database"]).is_file() or not (Path(config["install_dir"]) / "x-ui").is_file():
        raise UpgradeError("existing_panel_missing")
    safe_path(Path(config["install_dir"]) / "x-ui")
    for binary in (Path(config["install_dir"]) / "bin").glob("xray-linux-*"):
        safe_path(binary)
    _check_environment(os.environ)
    paths = [Path(DEFAULT_PATHS["unit"]), Path(DEFAULT_PATHS["openrc"])]
    dropins = Path(DEFAULT_PATHS["dropins"])
    if dropins.exists():
        paths.extend(dropins.glob("*.conf"))
    for name in (*ENVIRONMENT_FILES, Path(config["install_dir"]) / ".env"):
        check_environment_file(name)
    for path in paths:
        if not path.is_file():
            continue
        safe_path(path)
        content = path.read_text(encoding="utf-8", errors="replace")
        _check_environment({key: value.strip().strip("\"'") for key, value in re.findall(
            r"(?m)^\s*(?:export\s+)?(XUI_MAIN_FOLDER|XUI_BIN_FOLDER|XUI_DB_FOLDER|XUI_DB_TYPE|XUI_DB_DSN)\s*=\s*([^\n#]*)", content)})
        for directive, value in re.findall(r"(?m)^\s*(ExecStart|Environment|EnvironmentFile|User|WorkingDirectory|RootDirectory|ExecStartPre|ExecStartPost|ExecStopPost|KillMode)\s*=\s*([^\n]*)", content):
            try:
                tokens = shlex.split(value)
            except ValueError:
                raise UpgradeError("custom_service_not_supported") from None
            if directive == "ExecStart" and tokens and (tokens[0] != str(Path(config["install_dir"]) / "x-ui") or tokens[1:] not in ([], ["run"])):
                raise UpgradeError("custom_service_not_supported")
            if directive == "Environment":
                _check_environment(dict(token.split("=", 1) for token in tokens if "=" in token))
            if directive == "EnvironmentFile" and any(token.lstrip("-") not in ENVIRONMENT_FILES for token in tokens):
                raise UpgradeError("custom_service_not_supported")
            if directive in ("RootDirectory", "ExecStartPre", "ExecStartPost", "ExecStopPost") and value.strip():
                raise UpgradeError("custom_service_not_supported")
            if directive == "User" and value.strip() not in ("", "root"):
                raise UpgradeError("custom_service_not_supported")
            if directive == "KillMode" and value.strip() not in ("", "control-group", "mixed"):
                raise UpgradeError("custom_service_not_supported")
            if directive == "WorkingDirectory" and value.strip() and Path(value.strip()) != Path(config["install_dir"]):
                raise UpgradeError("custom_service_not_supported")
        if path == Path(DEFAULT_PATHS["openrc"]):
            command = re.search(r'(?m)^\s*command\s*=\s*[\"\x27]?([^\"\x27\s]+)', content)
            if not command or command.group(1) != str(Path(config["install_dir"]) / "x-ui"):
                raise UpgradeError("custom_service_not_supported")
            directory = re.search(r'(?m)^\s*directory\s*=\s*[\"\x27]?([^\"\x27\s]+)', content)
            if directory and Path(directory.group(1)) != Path(config["install_dir"]):
                raise UpgradeError("custom_service_not_supported")
            if not directory:
                check_environment_file("/.env")


def _json(value):
    try:
        return json.loads(value) if isinstance(value, str) else value
    except (ValueError, TypeError):
        raise UpgradeError("existing_configuration_invalid") from None


def _columns(db, table):
    return {row[1] for row in db.execute('PRAGMA table_info("' + table + '")')}


def _client(value, flow=None):
    identity = value.get("uuid") or (value.get("id") if isinstance(value.get("id"), str) else None) or value.get("password") or value.get("auth") or ""
    # Normalized clients can hold both a UUID and a protocol password. A
    # stable UUID must not hide a changed Trojan/Hy2/Shadowsocks credential.
    # Sparse canonical fields also permit migrations to add unused defaults.
    credentials = {}
    client_uuid = value.get("uuid") or (value.get("id") if isinstance(value.get("id"), str) else None)
    password = value.get("password") or value.get("auth")
    if client_uuid:
        credentials["uuid"] = client_uuid
    if password:
        credentials["password"] = password
    if value.get("auth") and value.get("password") and value["auth"] != value["password"]:
        credentials["auth"] = value["auth"]
    return {"email": value.get("email", ""), "identity": identity, "credentials": credentials,
            "subId": value.get("sub_id", value.get("subId", "")),
            "enable": bool(value.get("enable", True)),
            "expiry": value.get("expiry_time", value.get("expiryTime", 0)),
            "total": value.get("total_gb", value.get("totalGB", 0)),
            "limit": value.get("limit_ip", value.get("limitIp", 0)),
            "flow": flow if flow is not None else value.get("flow", "")}


def snapshot_identity(database):
    """Sensitive snapshot stays in memory; public state contains counts only."""
    with closing(sqlite3.connect(safe_path(database).as_uri() + "?mode=ro", uri=True, timeout=10)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"inbounds", "settings", "users"}.issubset(tables):
            raise UpgradeError("existing_database_not_supported")
        clients = {}
        if {"clients", "client_inbounds"}.issubset(tables):
            assoc = _columns(db, "client_inbounds")
            flow = "ci.flow_override" if "flow_override" in assoc else "NULL"
            for raw in db.execute("SELECT ci.inbound_id," + flow + " AS association_flow,c.* FROM client_inbounds ci JOIN clients c ON c.id=ci.client_id"):
                item = dict(raw)
                clients.setdefault(item["inbound_id"], []).append(_client(item, item["association_flow"]))
        result = {"inbounds": {}, "users": [], "settings": {}}
        for raw in db.execute("SELECT * FROM inbounds ORDER BY id"):
            row = dict(raw)
            settings = _json(row.get("settings") or "{}")
            if not isinstance(settings, dict):
                raise UpgradeError("existing_configuration_invalid")
            node_clients = clients.get(row["id"], []) if "client_inbounds" in tables else [_client(c) for c in settings.get("clients", [])]
            settings.pop("clients", None)
            item = {key: row[key] for key in ("id", "tag", "remark", "port", "protocol", "enable", "listen", "expiry_time", "total", "exclude_from_sub") if key in row}
            item["settings"] = settings
            for key in ("stream_settings", "sniffing", "allocate"):
                if row.get(key):
                    item[key] = _json(row[key])
            item["clients"] = sorted(node_clients, key=lambda c: (str(c["email"]), str(c["identity"])))
            result["inbounds"][str(row["id"])] = item
        user_columns = _columns(db, "users")
        fields = [key for key in ("id", "username", "password", "enable", "two_factor_secret", "two_factor_enabled") if key in user_columns]
        if not {"id", "username", "password"}.issubset(user_columns):
            raise UpgradeError("existing_database_not_supported")
        result["users"] = [dict(row) for row in db.execute("SELECT " + ",".join('"' + key + '"' for key in fields) + " FROM users ORDER BY id")]
        if "client_external_links" in tables:
            # The installer's unified subscription hangs the other nodes off
            # this table. Preserve its content, association and ordering too;
            # only bookkeeping timestamps may legitimately change on startup.
            link_columns = _columns(db, "client_external_links")
            stable = sorted(link_columns - {"created_at", "updated_at"})
            fields = ",".join('"' + key.replace('"', '""') + '"' for key in stable)
            ordering = " ORDER BY id" if "id" in link_columns else ""
            links = [dict(row) for row in db.execute("SELECT " + fields + " FROM client_external_links" + ordering)]
            if not ordering:
                links.sort(key=lambda item: json.dumps(item, sort_keys=True))
            result["external_links"] = links
        for row in db.execute("SELECT key,value FROM settings"):
            value = row["value"]
            if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                try:
                    value = json.loads(value)
                except ValueError:
                    # remarkTemplate and other ordinary panel strings can
                    # begin with brackets; they are not necessarily JSON.
                    if row["key"] == "xrayTemplateConfig":
                        raise UpgradeError("existing_configuration_invalid") from None
            result["settings"][row["key"]] = value
        return result


def _preserved(before, after):
    if isinstance(before, dict):
        return isinstance(after, dict) and all(key in after and _preserved(value, after[key]) for key, value in before.items())
    if isinstance(before, list):
        return isinstance(after, list) and len(before) == len(after) and all(_preserved(a, b) for a, b in zip(before, after))
    return before == after


class NativeAdapter:
    def __init__(self, config):
        self.config = config
        self.systemd = Path("/run/systemd/system").is_dir() and shutil.which("systemctl") is not None

    def preflight(self):
        if Path("/.dockerenv").exists() or Path("/run/.containerenv").exists() or os.environ.get("container"):
            raise UpgradeError("container_update_not_supported")
        if self.systemd:
            for prop, expected in (("FragmentPath", DEFAULT_PATHS["unit"]),):
                r = subprocess.run(["systemctl", "show", "x-ui", "--property=" + prop, "--value"], capture_output=True, text=True, timeout=15)
                if r.returncode or not r.stdout.strip() or safe_path(r.stdout.strip()) != Path(expected):
                    raise UpgradeError("effective_service_not_supported")
            r = subprocess.run(["systemctl", "show", "x-ui", "--property=DropInPaths", "--value"], capture_output=True, text=True, timeout=15)
            if r.returncode or any(safe_path(p).parent != Path(DEFAULT_PATHS["dropins"]) for p in r.stdout.split()):
                raise UpgradeError("effective_service_not_supported")
            r = subprocess.run(["systemctl", "show", "x-ui", "--property=Environment", "--value"], capture_output=True, text=True, timeout=15)
            if r.returncode:
                raise UpgradeError("effective_service_not_supported")
            _check_environment(dict(s.split("=", 1) for s in shlex.split(r.stdout) if "=" in s))
            # Inspect systemd's loaded unit, not just possibly newer files on
            # disk. A daemon-reload may not have happened after an operator edit.
            properties = {}
            for prop in ("ExecStart", "User", "RootDirectory", "WorkingDirectory", "KillMode", "MainPID"):
                r = subprocess.run(["systemctl", "show", "x-ui", "--property=" + prop, "--value"],
                                   capture_output=True, text=True, timeout=15)
                if r.returncode:
                    raise UpgradeError("effective_service_not_supported")
                properties[prop] = r.stdout.strip()
            expected = str(Path(self.config["install_dir"]) / "x-ui")
            paths = re.findall(r'(?:^|[ {;])path=([^ ;}]+)', properties["ExecStart"])
            arguments = re.findall(r'argv\[\]=([^;}]*)', properties["ExecStart"])
            if paths != [expected] or len(arguments) != 1 or shlex.split(arguments[0]) not in ([expected], [expected, "run"]):
                raise UpgradeError("effective_service_not_supported")
            if (properties["User"] not in ("", "root") or properties["RootDirectory"] or
                    properties["KillMode"] not in ("control-group", "mixed") or
                    (properties["WorkingDirectory"] and Path(properties["WorkingDirectory"]) != Path(self.config["install_dir"]))):
                raise UpgradeError("effective_service_not_supported")
            check_environment_file(Path(properties["WorkingDirectory"] or "/") / ".env")
            try:
                process = Path("/proc") / str(int(properties["MainPID"]))
                if not (process / "exe").is_file() or _digest(process / "exe") != _digest(expected):
                    raise UpgradeError("running_panel_does_not_match_installation")
                environment = dict(item.decode("utf-8", errors="replace").split("=", 1)
                                   for item in (process / "environ").read_bytes().split(bytes([0])) if b"=" in item)
                _check_environment(environment)
            except (OSError, ValueError):
                raise UpgradeError("effective_service_not_supported") from None
        elif not shutil.which("rc-service") or not Path(DEFAULT_PATHS["openrc"]).is_file():
            raise UpgradeError("native_service_manager_missing")
        if not self.is_active():
            raise UpgradeError("existing_panel_not_running")

    def is_active(self):
        args = ["systemctl", "is-active", "--quiet", "x-ui"] if self.systemd else ["rc-service", "x-ui", "status"]
        return subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15).returncode == 0

    def stop(self):
        args = ["systemctl", "stop", "x-ui"] if self.systemd else ["rc-service", "x-ui", "stop"]
        result = subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        if result.returncode and self.is_active():
            raise UpgradeError("panel_stop_failed")

    def start(self):
        args = ["systemctl", "start", "x-ui"] if self.systemd else ["rc-service", "x-ui", "start"]
        if subprocess.run(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60).returncode:
            raise UpgradeError("panel_start_failed")

    def quiescent(self):
        if self.is_active():
            return False
        program = Path(self.config["install_dir"])
        for entry in Path("/proc").glob("[0-9]*/exe"):
            try:
                target = os.readlink(entry)
                if target.endswith(" (deleted)"):
                    target = target[:-10]
            except (OSError, PermissionError):
                continue
            if target == str(program / "x-ui") or target.startswith(str(program / "bin") + "/"):
                return False
        return True

    def listener_snapshot(self):
        result = subprocess.run(["ss", "-H", "-lntup"], capture_output=True, text=True, timeout=15)
        if result.returncode:
            raise UpgradeError("listener_check_failed")
        listeners = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            owned = False
            for pid in re.findall(r"pid=([0-9]+)", line):
                try:
                    target = os.readlink("/proc/" + pid + "/exe")
                    if target.endswith(" (deleted)"):
                        target = target[:-10]
                except OSError:
                    continue
                base = Path(self.config["install_dir"])
                if target == str(base / "x-ui") or target.startswith(str(base / "bin") + "/"):
                    owned = True
            if len(fields) >= 5 and fields[0] in ("tcp", "udp") and owned:
                try:
                    listeners.add((fields[0], int(fields[4].rsplit(":", 1)[1])))
                except ValueError:
                    pass
        return listeners

    def panel_ready(self):
        settings = snapshot_identity(self.config["database"])["settings"]
        port = int(settings.get("webPort") or 2053)
        listen = settings.get("webListen") or "127.0.0.1"
        listen = "127.0.0.1" if listen == "0.0.0.0" else "::1" if listen in ("::", "[::]") else listen
        ipaddress.ip_address(listen.strip("[]"))
        path = "/" + str(settings.get("webBasePath") or "").strip("/") + "/"
        if any(ord(c) < 32 for c in path) or not 1 <= port <= 65535:
            return False
        cls = http.client.HTTPSConnection if settings.get("webCertFile") and settings.get("webKeyFile") else http.client.HTTPConnection
        options = {"timeout": 3}
        if cls is http.client.HTTPSConnection:
            # Loopback health check sends no authentication material and does
            # not follow redirects; existing self-issued panel certs are valid.
            options["context"] = ssl._create_unverified_context()
        connection = cls(listen.strip("[]"), port, **options)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            response.read(1024)
            return 200 <= response.status < 400
        finally:
            connection.close()

    def sleep(self, seconds):
        time.sleep(seconds)


def _digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _replace_file(source, target, mode=0o755):
    target = safe_path(target)
    temporary = target.with_name(target.name + ".upgrade-" + uuid.uuid4().hex[:8])
    try:
        with open(source, "rb") as src, open(temporary, "xb") as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _tree_size(path):
    return sum(p.stat().st_size for p in Path(path).rglob("*") if not p.is_symlink() and p.is_file())


def backup_installation(config, job):
    backup = job / "backup"
    backup.mkdir(mode=0o700)
    shutil.copytree(config["install_dir"], backup / "program", symlinks=True)
    shutil.copytree(DEFAULT_PATHS["db_dir"], backup / "database", symlinks=True)
    destination = backup / "database" / Path(config["database"]).name
    destination.unlink()
    with closing(sqlite3.connect(safe_path(config["database"]).as_uri() + "?mode=ro", uri=True, timeout=10)) as source:
        with closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            target.execute("PRAGMA journal_mode=DELETE")
            target.commit()
    os.chmod(destination, 0o600)
    for suffix in ("-wal", "-shm"):
        (destination.with_name(destination.name + suffix)).unlink(missing_ok=True)
    command = Path(DEFAULT_PATHS["command"])
    if command.exists():
        shutil.copy2(command, backup / "command")
    for file in (backup / "program/x-ui", destination):
        with open(file, "rb+") as source:
            os.fsync(source.fileno())
    atomic_json(backup / "complete.json", {"database": destination.name, "command_existed": command.exists()})


def restore_installation(config, job):
    backup = job / "backup"
    manifest = json.loads((backup / "complete.json").read_text(encoding="utf-8"))
    for current, original, failed in ((Path(config["install_dir"]), backup / "program", job / "failed-program"),
                                      (Path(DEFAULT_PATHS["db_dir"]), backup / "database", job / "failed-database")):
        safe_path(current)
        if failed.exists():
            raise UpgradeError("restore_target_exists")
        os.rename(current, failed)
        shutil.copytree(original, current, symlinks=True)
    command = Path(DEFAULT_PATHS["command"])
    if manifest["command_existed"]:
        _replace_file(backup / "command", command, (backup / "command").stat().st_mode & 0o777)
    elif command.exists():
        os.rename(command, job / "failed-command")


def _healthy(config, adapter, version, identity, listeners, core_hashes):
    for _ in range(config.get("verify_attempts", 20)):
        try:
            if (adapter.is_active() and adapter.panel_ready() and listeners.issubset(adapter.listener_snapshot()) and
                    panel_release.installed_version(Path(config["install_dir"]) / "x-ui") == version and
                    identities_preserved(identity, snapshot_identity(config["database"])) and
                    all(Path(path).is_file() and _digest(path) == digest for path, digest in core_hashes.items())):
                return True
        except (Exception,):
            pass
        adapter.sleep(config.get("verify_delay", 1))
    return False


def identities_preserved(before, after):
    return (set(before["inbounds"]) == set(after["inbounds"]) and _preserved(before, after))


def execute_upgrade(config, job, adapter=None, release=None, prepared=None):
    """Transaction entry. Tests may supply an already selected candidate.

    The production CLI never accepts candidate paths or a force/reinstall flag.
    """
    job = safe_path(job)
    adapter = adapter or NativeAdapter(config)
    stop_attempted = backup_ready = False
    old_version = target_version = ""
    identity = listeners = core_hashes = None
    def phase(name):
        write_state(job, "running", phase=name, current_version=old_version, target_version=target_version)
        fd = os.open(safe_path(job / "worker.log"), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as log:
            log.write(dt.datetime.now(dt.timezone.utc).isoformat() + " " + name + "\n")
    try:
        validate_layout(config)
        adapter.preflight()
        if not adapter.is_active():
            raise UpgradeError("existing_panel_not_running")
        old_version = panel_release.installed_version(Path(config["install_dir"]) / "x-ui")
        original_binary_hash = _digest(Path(config["install_dir"]) / "x-ui")
        phase("checking_release")
        if prepared is None:
            release = release or panel_release.latest_release()
            target_version = release["version"]
            action = panel_release.version_action(old_version, target_version)
            if action in ("noop", "newer"):
                return write_state(job, "no_update", current_version=old_version, target_version=target_version,
                                   reason="already_latest" if action == "noop" else "installed_is_newer")
            phase("downloading_and_verifying")
            prepared = panel_release.prepare_release(release, job / "candidate")
        target_version = prepared["version"]
        if panel_release.installed_version(prepared["binary"]) != target_version:
            raise UpgradeError("candidate_version_mismatch")
        if prepared.get("cli"):
            if subprocess.run(["bash", "-n", str(prepared["cli"])], capture_output=True, timeout=15).returncode:
                raise UpgradeError("candidate_cli_invalid")
        adapter.preflight()
        if _digest(Path(config["install_dir"]) / "x-ui") != original_binary_hash:
            raise UpgradeError("panel_changed_during_download")
        baseline = snapshot_identity(config["database"])
        ports = {int(row["port"]) for row in baseline["inbounds"].values() if row.get("enable")}
        ports.add(int(baseline["settings"].get("webPort") or 2053))
        if str(baseline["settings"].get("subEnable", "true")).lower() == "true":
            ports.add(int(baseline["settings"].get("subPort") or 2096))
        listeners = {entry for entry in adapter.listener_snapshot() if entry[1] in ports}
        core_hashes = {str(p): _digest(p) for p in (Path(config["install_dir"]) / "bin").glob("xray-linux-*") if p.is_file()}
        if not core_hashes:
            raise UpgradeError("existing_xray_missing")
        for path in (Path(config["install_dir"]), Path(DEFAULT_PATHS["db_dir"]), Path(DEFAULT_PATHS["command"]).parent):
            if path.stat().st_dev != job.stat().st_dev:
                raise UpgradeError("cross_filesystem_restore_not_supported")
        # Failed trees remain as evidence while old trees are copied back.
        # Reserve both the backup and recovery copies, plus atomic replacements
        # and a database-sized migration allowance, before stopping anything.
        program_size = _tree_size(config["install_dir"])
        database_size = _tree_size(DEFAULT_PATHS["db_dir"])
        needed = 2 * program_size + 3 * database_size + Path(prepared["binary"]).stat().st_size * 2 + 32 * 1024 * 1024
        if shutil.disk_usage(job).free < needed:
            raise UpgradeError("insufficient_backup_space")
        phase("stopping_panel")
        stop_attempted = True
        adapter.stop()
        if not adapter.quiescent():
            raise UpgradeError("panel_not_quiescent")
        identity = snapshot_identity(config["database"])
        phase("backing_up")
        backup_installation(config, job)
        backup_ready = True
        phase("replacing_panel")
        _replace_file(prepared["binary"], Path(config["install_dir"]) / "x-ui")
        if prepared.get("cli"):
            _replace_file(prepared["cli"], Path(config["install_dir"]) / "x-ui.sh")
            _replace_file(prepared["cli"], DEFAULT_PATHS["command"])
        phase("starting_and_verifying")
        adapter.start()
        if not _healthy(config, adapter, target_version, identity, listeners, core_hashes):
            raise UpgradeError("new_panel_health_failed")
        return write_state(job, "complete", current_version=old_version, target_version=target_version,
                           preserved_nodes=len(identity["inbounds"]), xray_preserved=True)
    except BaseException as error:
        code = str(error) if isinstance(error, (UpgradeError, panel_release.ReleaseError)) else "release_or_upgrade_failed"
        if not stop_attempted:
            return write_state(job, "failed", current_version=old_version, target_version=target_version, error=code)
        if not backup_ready:
            # No live files have been replaced. A leftover child can make the
            # quiescence check fail after stopping the panel; do not leave the
            # original panel down merely because it was unsafe to take backups.
            try:
                if not adapter.is_active():
                    adapter.start()
                if _healthy(config, adapter, old_version, baseline, listeners, core_hashes):
                    return write_state(job, "rolled_back", current_version=old_version,
                                       target_version=target_version, error=code)
            except BaseException:
                pass
            return write_state(job, "manual_recovery_required", current_version=old_version,
                               target_version=target_version, error=code)
        try:
            phase("rolling_back")
            adapter.stop()
            if not adapter.quiescent():
                raise UpgradeError("rollback_panel_not_quiescent")
            if backup_ready:
                restore_installation(config, job)
            adapter.start()
            baseline = identity if identity is not None else baseline
            if not _healthy(config, adapter, old_version, baseline, listeners, core_hashes):
                raise UpgradeError("old_panel_recovery_failed")
            return write_state(job, "rolled_back", current_version=old_version, target_version=target_version, error=code)
        except BaseException:
            return write_state(job, "manual_recovery_required", current_version=old_version,
                               target_version=target_version, error=code)


def launch(config):
    import fcntl
    if os.name != "posix" or os.geteuid() != 0:
        raise UpgradeError("root_linux_required")
    validate_layout(config)
    NativeAdapter(config).preflight()
    root = safe_path(Path(config["info_dir"]) / "3xuish-panel-update")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    descriptors = []
    try:
        for path in (Path(config["info_dir"]) / "3xuish-reinstall/reinstall.lock",
                     Path(config["info_dir"]) / "3xuish-performance-repair/repair.lock"):
            path = safe_path(path)
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
            descriptors.append(fd)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise UpgradeError("maintenance_busy") from None
        require_finished_jobs(config["info_dir"])
        name = "job-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
        job = root / name
        job.mkdir(mode=0o700)
        fd = os.open(job / "worker.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        descriptors.append(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        private_write(job / "panel_upgrade.py", Path(__file__).read_bytes())
        private_write(job / "panel_release.py", Path(panel_release.__file__).read_bytes())
        private_write(job / "config.json", json.dumps(config))
        write_state(job, "starting", phase="queued")
        atomic_json(root / "latest.json", {"job": name})
        log = os.open(job / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            child = subprocess.Popen([sys.executable, str(job / "panel_upgrade.py"), "_worker", str(job),
                                      *map(str, descriptors)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                     start_new_session=True, close_fds=True, pass_fds=tuple(descriptors))
        except BaseException:
            write_state(job, "failed", error="worker_start_failed")
            raise
        finally:
            os.close(log)
        _CHILDREN[str(job)] = child
        return {"operation": "panel_update", "status": "starting", "job_directory": str(job), "pid": child.pid}
    finally:
        for fd in descriptors:
            os.close(fd)


def worker(job):
    job = safe_path(job)
    config = json.loads((job / "config.json").read_text(encoding="utf-8"))
    # Frontend disconnection cannot signal this independent session. Explicit
    # termination turns into a transaction exception and takes the recovery path.
    def interrupted(signum, _frame):
        raise UpgradeError("worker_interrupted")
    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, interrupted)
    return execute_upgrade(config, job)


def wait_job(job, timeout=60):
    deadline = time.monotonic() + timeout
    while True:
        result = read_job(job)
        if result["status"] not in ACTIVE or time.monotonic() >= deadline:
            child = _CHILDREN.get(str(job))
            if child is not None and result["status"] not in ACTIVE:
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    del _CHILDREN[str(job)]
            return result
        time.sleep(0.2)


MESSAGES = {
    "no_previous_job": "暂无面板升级记录。", "starting": "面板升级任务已启动，SSH 断开后仍会继续。",
    "running": "面板升级正在执行，SSH 断开后仍会继续。", "complete": "3x-ui 面板升级完成，原节点与账号已核对。",
    "no_update": "当前面板已是官方最新稳定版，或本机版本更新；没有停止服务。",
    "failed": "升级前检查或下载失败，尚未替换现有程序。",
    "rolled_back": "升级未通过验收，已恢复旧程序和数据库。",
    "manual_recovery_required": "恢复尚未确认完成，请检查私有日志和备份。",
    "error": "无法启动或读取面板升级任务。",
}
ERROR_MESSAGES = {
    "only_default_native_sqlite_supported": "仅支持本机默认路径的 SQLite 面板；Docker、自定义路径或外部数据库请按原部署方式升级。",
    "container_update_not_supported": "当前是容器部署，请通过容器镜像更新面板。",
    "only_default_service_supported": "当前服务名称不是默认 x-ui，未开始升级。",
    "root_linux_required": "请在 Linux 的 root 终端运行。",
    "existing_panel_missing": "未找到已安装的面板程序或数据库。",
    "existing_panel_not_running": "原面板未运行，请先恢复原服务后再升级。",
    "maintenance_busy": "已有节点或维护任务运行中，请等待其完成。",
    "previous_maintenance_needs_recovery": "上次维护尚未完成或需要恢复，请先查看对应任务结果。",
    "new_panel_health_failed": "新版未通过面板、节点监听或原数据保留检查。",
    "insufficient_backup_space": "磁盘空间不足以保存升级备份。",
    "cross_filesystem_restore_not_supported": "程序、数据库和备份跨文件系统，无法保证本流程的恢复操作。",
    "worker_interrupted": "升级工作进程被中断。",
    "release_archive_digest_mismatch": "官方发行包 SHA-256 校验不通过。",
    "release_download_failed": "无法下载官方发行包。",
    "release_metadata_unavailable": "无法获取官方稳定版信息。",
    "insufficient_release_space": "剩余空间不足以安全暂存发行包，已在停服前停止准备。",
    "release_space_check_failed": "无法确认剩余磁盘空间，已停止升级前准备。",
    "custom_database_environment_not_supported": "环境配置会把面板指向其他数据库或目录，未开始升级。",
    "custom_service_not_supported": "服务使用自定义启动配置，无法按默认部署方式安全升级。",
    "effective_service_not_supported": "服务实际加载的启动配置与默认部署不一致。",
    "running_panel_does_not_match_installation": "正在运行的面板程序与磁盘上的安装文件不一致。",
    "existing_database_not_supported": "当前数据库结构无法核对，未开始升级。",
    "existing_configuration_invalid": "原有节点或核心配置无法解析，未开始升级。",
    "existing_xray_missing": "没有找到需要保留的现有 Xray 内核。",
    "panel_changed_during_download": "下载期间面板程序被其他操作修改，已停止升级。",
    "panel_not_quiescent": "面板停止后仍有相关进程运行，未替换程序。",
    "panel_stop_failed": "原面板服务未能正常停止。",
    "panel_start_failed": "面板服务未能正常启动。",
    "old_panel_recovery_failed": "旧版服务的恢复验收未通过。",
    "rollback_panel_not_quiescent": "仍有相关进程运行，无法安全恢复数据库。",
    "native_service_manager_missing": "未找到受支持的 systemd 或 OpenRC 面板服务。",
    "unsupported_panel_architecture": "官方升级包暂不支持当前 CPU 架构。",
    "panel_version_check_failed": "无法执行或识别面板版本。",
    "release_download_timed_out": "下载官方发行包超时。",
    "release_checksum_missing": "官方发行包没有可验证的 SHA-256 信息。",
    "candidate_version_mismatch": "候选面板版本与发行信息不一致。",
    "candidate_cli_invalid": "新版管理脚本未通过语法检查。",
}
PHASES = {"queued": "等待执行", "checking_release": "检查官方稳定版本", "downloading_and_verifying": "下载并验证发行包",
          "stopping_panel": "停止面板", "backing_up": "备份程序和数据库", "replacing_panel": "替换面板程序",
          "starting_and_verifying": "启动并核对原节点", "rolling_back": "恢复旧程序和数据库"}


def render(state):
    lines = [MESSAGES.get(state.get("status"), MESSAGES["error"])]
    if state.get("current_version"):
        lines.append("当前/原版本：" + state["current_version"])
    if state.get("target_version"):
        lines.append("目标版本：" + state["target_version"])
    if state.get("phase") in PHASES:
        lines.append("步骤：" + PHASES[state["phase"]])
    if state.get("status") in ACTIVE:
        lines.append("稍后选择菜单24或运行 --panel-update-status 查看结果。")
    for field, label in (("error", "原因"), ("backup_directory", "备份"), ("log_file", "日志")):
        if state.get(field):
            if field == "backup_directory" and not (Path(state[field]) / "complete.json").is_file():
                continue
            value = str(state[field])
            if field == "error":
                value = ERROR_MESSAGES.get(value, "检查未通过（错误代码：" + value + "），请查看日志。")
            lines.append(label + "：" + value)
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_worker":
        worker(Path(argv[1]))
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("launch", "status", "guard"))
    for name in ("database", "install-dir", "info-dir"):
        parser.add_argument("--" + name, default=DEFAULT_PATHS[name.replace("-", "_")])
    parser.add_argument("--service", default="x-ui")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "launch":
            config = {key: getattr(args, key) for key in ("database", "install_dir", "info_dir", "service")}
            state = launch(config)
            if not args.json:
                print(render(state), flush=True)
            state = wait_job(Path(state["job_directory"]), 60)
        else:
            state = read_latest(args.info_dir)
        if args.command == "guard":
            return int(state["status"] not in FINISHED | {"no_previous_job"})
        print(json.dumps(state, ensure_ascii=False, indent=2) if args.json else render(state))
        return int(args.command == "launch" and state["status"] not in ACTIVE | {"complete", "no_update"})
    except (UpgradeError, OSError, ValueError, KeyError) as error:
        state = {"status": "error", "error": str(error) if isinstance(error, UpgradeError) else "upgrade_state_unavailable"}
        print(json.dumps(state) if args.json else render(state), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
