"""Exercise panel-only upgrade/rollback with real official 3x-ui and Xray.

Run only in a disposable official image with /repo mounted read-only. The
service controller is replaced by a real process fixture; no host service or
port is touched. Latest 3x-ui and the CI image currently have the same version,
so this intentionally tests the prepared transaction rather than claiming a
cross-version migration. The normal latest-version no-op is also exercised;
release selection and archive failure cases have separate unit coverage.
"""
from __future__ import annotations

from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import ssl
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import panel_release as release
import panel_upgrade as upgrade

CLIENT_ID = "5a535c69-bd0f-40df-9bf6-e79ee6713b55"
EXTERNAL_ID = "50cb3dab-4359-4bfb-82b1-c115c6729df3"
EXTERNAL_LINK = ("vless://" + EXTERNAL_ID + "@198.51.100.2:24443"
                 "?type=tcp&encryption=none&security=none#External-upgrade-fixture")
SUB_ID = "panel-upgrade-fixture-subscription"
BASE_PATH = "panel-upgrade-smoke"
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def checked(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=30, **kwargs)
    if result.returncode:
        raise RuntimeError("official_panel_fixture_command_failed")
    return result


def paths_for(root):
    return {"database": str(root / "etc/x-ui/x-ui.db"), "db_dir": str(root / "etc/x-ui"),
            "install_dir": str(root / "usr/local/x-ui"), "info_dir": str(root / "root"),
            "command": str(root / "usr/bin/x-ui"), "unit": str(root / "etc/systemd/system/x-ui.service"),
            "dropins": str(root / "etc/systemd/system/x-ui.service.d"),
            "openrc": str(root / "etc/init.d/x-ui"),
            "backup_root": str(root / "root/3xuish-panel-update")}


def identity(database):
    """Independent assertions, excluding background traffic/online counters."""
    selected = {
        "users": ("id", "username", "password"),
        "inbounds": ("id", "user_id", "tag", "remark", "port", "protocol", "listen", "enable",
                     "settings", "stream_settings", "sniffing", "total", "expiry_time"),
        "clients": ("id", "email", "uuid", "password", "sub_id", "enable", "expiry_time", "total_gb"),
        "client_inbounds": ("client_id", "inbound_id", "flow_override"),
        "client_external_links": ("id", "client_id", "kind", "value", "remark", "sort_order", "enabled"),
        "settings": ("key", "value"),
    }
    with closing(sqlite3.connect(Path(database).as_uri() + "?mode=ro", uri=True)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        result = {}
        for table, fields in selected.items():
            columns = {row[1] for row in db.execute('PRAGMA table_info("' + table + '")')}
            fields = [name for name in fields if name in columns]
            if fields:
                result[table] = db.execute("SELECT " + ",".join('"' + name + '"' for name in fields)
                                          + ' FROM "' + table + '" ORDER BY 1,2').fetchall()
        return result


class LivePanel:
    """A native service-shaped adapter backed by actual panel/core processes."""
    def __init__(self, config, paths, panel_port, sub_port, node_port):
        self.config, self.paths = config, paths
        self.panel_port, self.sub_port, self.node_port = panel_port, sub_port, node_port
        self.process = self.log = None
        self.fail_next_start = False
        self.injected_failure = False
        self.starts = self.stops = 0
        self.last_pid = None

    def environment(self):
        result = {key: value for key, value in os.environ.items() if not key.startswith("XUI_")}
        result.update(XUI_DB_TYPE="sqlite", XUI_DB_FOLDER=self.paths["db_dir"],
                      XUI_MAIN_FOLDER=self.paths["install_dir"],
                      XUI_BIN_FOLDER=str(Path(self.paths["install_dir"]) / "bin"),
                      XUI_LOG_FOLDER=str(Path(self.paths["info_dir"]) / "fixture-logs"))
        return result

    def preflight(self):
        # Only native service inspection is substituted. All paths are mapped
        # inside this private container fixture by patching DEFAULT_PATHS.
        pass

    def is_active(self):
        return self.process is not None and self.process.poll() is None

    def start(self):
        assert not self.is_active(), "fixture panel was already running"
        self.starts += 1
        binary = Path(self.paths["install_dir"]) / "x-ui"
        self.log = (Path(self.paths["info_dir"]) / "fixture-panel.log").open("ab")
        self.process = subprocess.Popen([str(binary), "run"], cwd=binary.parent,
                                        env=self.environment(), stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=self.log, start_new_session=True)
        self.last_pid = self.process.pid
        if self.fail_next_start:
            self.fail_next_start = False
            self.injected_failure = True
            self.wait_ready()
            # Model a candidate that migrated SQLite and then failed health.
            # There is no production bypass or simulated panel response here.
            with closing(sqlite3.connect(self.paths["database"])) as db:
                db.execute("CREATE TABLE upgrade_smoke_migration (value TEXT)")
                db.execute("INSERT INTO upgrade_smoke_migration VALUES ('candidate-only')")
                db.execute("UPDATE users SET username='candidate-damaged-user'")
                db.execute("UPDATE client_external_links SET value='candidate-damaged-external-link'")
                db.commit()

    def stop(self):
        self.stops += 1
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process = None
        if self.log:
            self.log.close()
            self.log = None

    def quiescent(self):
        return not self.is_active() and not self.listener_snapshot()

    def panel_ready(self):
        # Exercise the production loopback HTTPS health implementation too.
        try:
            return self.is_active() and upgrade.NativeAdapter.panel_ready(self)
        except (OSError, upgrade.UpgradeError) as exc:
            self.health_error = str(exc)
            return False

    def listener_snapshot(self):
        return upgrade.NativeAdapter.listener_snapshot(self)

    def sleep(self, seconds):
        time.sleep(seconds)

    def wait_ready(self, require_node=True):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise RuntimeError("real panel stopped before readiness")
            if self.panel_ready() and (not require_node or ("tcp", self.node_port) in self.listener_snapshot()):
                return
            time.sleep(.1)
        raise RuntimeError(f"real panel readiness timed out: web={self.panel_ready()}, "
                           f"health_error={getattr(self, 'health_error', '')}, "
                           f"expected_node={self.node_port}, listeners={self.listener_snapshot()}")


def setup_panel(adapter):
    binary = str(Path(adapter.paths["install_dir"]) / "x-ui")
    checked([binary, "setting", "-username", "upgrade-smoke", "-password", "fixture-private-password",
             "-port", str(adapter.panel_port), "-webBasePath", BASE_PATH], env=adapter.environment())
    cert = Path(adapter.paths["info_dir"]) / "cert/panel.pem"
    key = cert.with_name("panel.key")
    checked([binary, "cert", "-webCert", str(cert), "-webCertKey", str(key)], env=adapter.environment())
    # Existing native installations already have this lazily generated panel
    # identity. Seed it before the baseline instead of racing first-start work.
    with closing(sqlite3.connect(adapter.paths["database"])) as db:
        guid = "439e59c8-7318-4a99-84f0-2540ce5635d5"
        if not db.execute("UPDATE settings SET value=? WHERE key='panelGuid'", (guid,)).rowcount:
            db.execute("INSERT INTO settings(key,value) VALUES('panelGuid',?)", (guid,))
        db.commit()
    token = checked([binary, "setting", "-getApiToken", "-tokenName", "upgrade-smoke",
                     "-tokenScope", "admin"], env=adapter.environment())
    match = re.search(rb"(?m)^\s*apiToken:\s*(\S+)", token.stdout)
    assert match, "fixture token unavailable"
    adapter.start()
    adapter.wait_ready(require_node=False)
    base = f"https://127.0.0.1:{adapter.panel_port}/{BASE_PATH}"
    def request(path, payload):
        req = urllib.request.Request(base + path, data=json.dumps(payload).encode(), method="POST",
            headers={"Authorization": "Bearer " + match[1].decode(), "Content-Type": "application/json"})
        with OPENER.open(req, timeout=10) as response:
            value = json.load(response)
        assert value.get("success") is True, "real API rejected fixture setup"
        return value.get("obj")
    payload = {"enable": True, "port": adapter.node_port, "listen": "127.0.0.1", "protocol": "vless",
               "tag": "upgrade-existing-inbound", "remark": "Existing upgrade fixture",
               "settings": json.dumps({"clients": [{"id": CLIENT_ID, "email": "upgrade-existing-client",
                                      "subId": SUB_ID, "enable": True, "expiryTime": 0, "totalGB": 0}],
                                       "decryption": "none"}),
               "streamSettings": json.dumps({"network": "tcp", "security": "none"}),
               "sniffing": json.dumps({"enabled": False})}
    request("/panel/api/inbounds/add", payload)
    request("/panel/api/clients/upgrade-existing-client/externalLinks", {
        "externalLinks": [{"kind": "link", "value": EXTERNAL_LINK,
                           "remark": "Additional subscription fixture"}]})
    settings = request("/panel/api/setting/all", {})
    settings.update(subEnable=True, subListen="127.0.0.1", subPort=adapter.sub_port,
                    subPath="/upgrade-sub/", subCertFile="", subKeyFile="", subURI="")
    request("/panel/api/setting/update", settings)
    adapter.stop()
    adapter.start()
    try:
        upgrade.snapshot_identity(adapter.paths["database"])
    except upgrade.UpgradeError:
        with closing(sqlite3.connect(adapter.paths["database"])) as db:
            for key, value in db.execute("SELECT key,value FROM settings"):
                if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
                    try:
                        json.loads(value)
                    except ValueError:
                        print("FIXTURE_NON_JSON_SETTING:", key, flush=True)
        raise
    adapter.wait_ready()


def subscription(adapter):
    with OPENER.open(f"http://127.0.0.1:{adapter.sub_port}/upgrade-sub/{SUB_ID}", timeout=5) as response:
        body = response.read().decode()
    try:
        body = base64.b64decode(body, validate=True).decode()
    except (ValueError, UnicodeError):
        pass
    assert CLIENT_ID in body, "original client UUID missing from real subscription"
    assert EXTERNAL_ID in body, "unified subscription lost its external node link"
    return body


def candidate(directory):
    stage = directory / "candidate"
    stage.mkdir()
    archive = os.environ.get("PANEL_UPGRADE_SMOKE_ARCHIVE")
    if archive:
        # The optional official v3.11.0 asset is independently pinned by its
        # published GitHub digest, then uses the production archive validator.
        assert digest(archive) == "0ddfa4702f595442e2962acd81addc768fdacaa4ff2672c971a0463dae744916"
        stage = release.extract_archive(Path(archive), stage)
    else:
        shutil.copyfile("/app/x-ui", stage / "x-ui")
        shutil.copyfile("/usr/bin/x-ui", stage / "x-ui.sh")
        (stage / "x-ui").chmod(0o755)
        (stage / "x-ui.sh").chmod(0o755)
    return {"version": release.installed_version(stage / "x-ui"), "binary": str(stage / "x-ui"),
            "cli": str(stage / "x-ui.sh")}


def main():
    global OPENER
    if ROOT != Path("/repo") or not Path("/app/x-ui").is_file() or Path("/etc/x-ui/x-ui.db").exists():
        raise SystemExit("Run only inside a fresh disposable official panel container.")
    os.umask(0o077)
    # The official container's /app overrides are not a native installation;
    # only this isolated fixture maps the updater's known paths below.
    for name in list(os.environ):
        if name.startswith("XUI_"):
            os.environ.pop(name)
    with tempfile.TemporaryDirectory(prefix="3xuish-panel-upgrade-smoke-") as temporary:
        directory = Path(temporary)
        paths = paths_for(directory)
        for key in ("db_dir", "info_dir", "backup_root"):
            Path(paths[key]).mkdir(parents=True, exist_ok=True)
        shutil.copytree("/app", paths["install_dir"])
        for key in ("command", "unit", "openrc"):
            path = Path(paths[key]); path.parent.mkdir(parents=True, exist_ok=True)
            if key == "unit":
                content = ("[Unit]\nDescription=Panel upgrade fixture\n[Service]\nUser=root\n"
                           f"WorkingDirectory={paths['install_dir']}\n"
                           f"ExecStart={paths['install_dir']}/x-ui run\nKillMode=control-group\n")
            elif key == "openrc":
                content = f'#!/sbin/openrc-run\ncommand="{paths["install_dir"]}/x-ui"\ncommand_args="run"\n'
            else:
                content = "#!/bin/sh\n# old fixture admin command\nexit 0\n"
            path.write_text(content, encoding="utf-8")
            path.chmod(0o755)
        cert = Path(paths["info_dir"]) / "cert/panel.pem"
        key = cert.with_name("panel.key")
        cert.parent.mkdir()
        checked(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
                 "-keyout", str(key), "-out", str(cert)])
        certificate_before = cert.read_bytes(), key.read_bytes()
        OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=cert)))
        configuration = {"database": paths["database"], "install_dir": paths["install_dir"],
                         "info_dir": paths["info_dir"], "service": "x-ui",
                         "verify_attempts": 100, "verify_delay": .1}
        ports = [port() for _ in range(3)]
        while len(set(ports)) != 3:
            ports = [port() for _ in range(3)]
        adapter = LivePanel(configuration, paths, *ports)
        try:
            setup_panel(adapter)
            before_subscription = subscription(adapter)
            before = identity(paths["database"])
            assert len(before.get("client_external_links", [])) == 1, "external link fixture was not persisted"
            binary = Path(paths["install_dir"]) / "x-ui"
            core = Path(paths["install_dir"]) / "bin/xray-linux-amd64"
            old_hash, core_hash = digest(binary), digest(core)
            old_cli = Path(paths["command"]).read_bytes()
            original_resources = {key: Path(paths[key]).read_bytes() for key in ("unit", "openrc")}
            prepared = candidate(directory)
            with mock.patch.object(upgrade, "DEFAULT_PATHS", paths):
                adapter.fail_next_start = True
                failed_job = Path(paths["backup_root"]) / "job-20261112T010101Z-123456abcdef"
                failed_job.mkdir(mode=0o700)
                failed = upgrade.execute_upgrade(configuration, failed_job, adapter=adapter, prepared=prepared)
                assert adapter.injected_failure, ("failure scenario never reached candidate start", failed)
                assert failed["status"] == "rolled_back", failed
                adapter.wait_ready()
                restored = identity(paths["database"])
                if restored != before:
                    for table in before:
                        if before[table] != restored.get(table):
                            if table == "settings":
                                left, right = dict(before[table]), dict(restored[table])
                                print("FIXTURE_SETTINGS_DIFFERENCE", "added", sorted(right.keys()-left.keys()),
                                      "removed", sorted(left.keys()-right.keys()),
                                      "changed", [key for key in left.keys() & right.keys() if left[key] != right[key]], flush=True)
                            else:
                                print("FIXTURE_IDENTITY_DIFFERENCE", table, flush=True)
                assert restored == before, "rollback did not restore old identities/settings"
                with closing(sqlite3.connect(paths["database"])) as db:
                    assert not db.execute("SELECT name FROM sqlite_master WHERE name='upgrade_smoke_migration'").fetchall(), \
                        "candidate schema survived rollback"
                assert digest(binary) == old_hash, "old panel binary was not restored"
                assert Path(paths["command"]).read_bytes() == old_cli, "old admin CLI was not restored"
                assert digest(core) == core_hash, "rollback changed the selected Xray core"
                assert subscription(adapter) == before_subscription, "rollback changed real subscription output"
                old_inode = binary.stat().st_ino
                stops_before = adapter.stops
                good_job = Path(paths["backup_root"]) / "job-20261112T010102Z-abcdef123456"
                good_job.mkdir(mode=0o700)
                success = upgrade.execute_upgrade(configuration, good_job, adapter=adapter, prepared=prepared)
                assert success["status"] == "complete", success
                adapter.wait_ready()
                assert adapter.stops > stops_before and binary.stat().st_ino != old_inode, \
                    "prepared transaction did not replace the panel binary"
                assert release.installed_version(binary) == prepared["version"], "candidate version did not apply"
                assert digest(binary) == digest(prepared["binary"]), "installed binary differs from prepared candidate"
                assert Path(paths["command"]).read_bytes() == Path(prepared["cli"]).read_bytes(), "admin CLI not updated"
                assert digest(core) == core_hash, "panel upgrade replaced the selected Xray core"
                assert identity(paths["database"]) == before, "upgrade changed existing identities/settings"
                assert subscription(adapter) == before_subscription, "upgrade changed real subscription output"
                assert (cert.read_bytes(), key.read_bytes()) == certificate_before
                for key, data in original_resources.items():
                    assert Path(paths[key]).read_bytes() == data, "service configuration changed"
                no_op_job = Path(paths["backup_root"]) / "job-20261112T010103Z-654321abcdef"
                no_op_job.mkdir(mode=0o700)
                service_counts = adapter.starts, adapter.stops
                no_op = upgrade.execute_upgrade(configuration, no_op_job, adapter=adapter,
                                                release={"version": prepared["version"]})
                assert no_op["status"] == "no_update", no_op
                assert (adapter.starts, adapter.stops) == service_counts, "latest-version check restarted the panel"
                assert identity(paths["database"]) == before, "no-op changed existing settings"
                assert subscription(adapter) == before_subscription
            print("PANEL_UPGRADE_SMOKE_OK: real panel transaction and HTTPS/subscription/listeners; "
                  "identity/core/service and unified external-link preservation; "
                  "migrated-database failure restores old database and program; "
                  "latest-version no-op does not restart; same-version prepared transaction")
        except BaseException:
            log = Path(paths["info_dir"]) / "fixture-panel.log"
            if log.is_file():
                print(log.read_text(errors="replace")[-6000:], file=sys.stderr)
            raise
        finally:
            adapter.stop()


if __name__ == "__main__":
    main()
