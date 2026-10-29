"""Clean redeployment must archive the old installation and restore failures."""
from __future__ import annotations

from contextlib import closing
import ast
import copy
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import clean_reinstall as reinstall

INFO_NAMES = ("3x-ui-node-profiles.json", "3x-ui-nodes-info.txt",
              "3x-ui-node-links.txt", "3x-ui-xhttp-info.txt")


def managed_paths(root):
    return {"database": str(root / "etc/x-ui/x-ui.db"), "db_dir": str(root / "etc/x-ui"),
            "install_dir": str(root / "usr/local/x-ui"), "info_dir": str(root / "root"),
            "command": str(root / "usr/bin/x-ui"), "unit": str(root / "etc/systemd/system/x-ui.service"),
            "dropins": str(root / "etc/systemd/system/x-ui.service.d"),
            "openrc": str(root / "etc/init.d/x-ui"), "cert_root": str(root / "root/cert"),
            "acme_root": str(root / "root/.acme.sh"), "backup_root": str(root / "root/3xuish-reinstall")}


def make_node(key, ident, *, prefix):
    protocol = "hysteria" if key == "hy2" else "vless"
    port = {"hy2": 24444, "raw": 443, "reality": 24443, "xhttp": 8443}[key]
    stream = {"network": "hysteria" if key == "hy2" else "xhttp" if key == "xhttp" else "tcp",
              "security": "reality" if key == "reality" else "tls"}
    if key == "hy2":
        settings = {"version": 2, "clients": [{"auth": prefix + "-auth", "enable": True,
                    "email": prefix + "-hy2", "subId": prefix + "-subscription"}]}
        stream["hysteriaSettings"] = {"version": 2}
    else:
        identity = "00000000-0000-4000-8000-%012d" % (ident + (100 if prefix == "fresh" else 0))
        settings = {"clients": [{"id": identity, "flow": "xtls-rprx-vision",
            "enable": True, "email": prefix + "-" + key, "subId": prefix + "-subscription-" + key}],
            "decryption": "none", "encryption": "none"}
    tag = "3xuish-" + prefix + "-" + key
    payload = {"tag": tag, "port": port, "protocol": protocol, "remark": prefix + " " + key,
        "listen": "", "enable": True, "settings": json.dumps(settings),
        "streamSettings": json.dumps(stream), "sniffing": '{"enabled":false}'}
    profile = {"key": key, "port": port, "credential": prefix + "-credential-" + key,
        "link": ("hysteria2://" if key == "hy2" else "vless://") + prefix + "-link-" + key,
        "payload": copy.deepcopy(payload)}
    row = (ident, tag, payload["remark"], port, protocol, 1, "", payload["settings"],
           payload["streamSettings"], payload["sniffing"])
    runtime = {"tag": tag, "port": port, "protocol": protocol, "listen": "0.0.0.0",
               "settings": settings, "streamSettings": stream, "sniffing": {"enabled": False}}
    return row, profile, runtime


class FixtureAdapter:
    def __init__(self, fixture):
        self.fixture = fixture
        self.active = True
        self.events = []
        self.failure = None
        self.installs = 0
        self.last_environment = None
        self.enabled = True
        self.ports = {("tcp", 443), ("udp", 42710)}

    def is_active(self):
        return self.active

    def stop(self):
        self.events.append("stop")
        if self.failure == "stop":
            raise RuntimeError("fixture stop failed")
        self.active = False
        self.ports = set()
        if self.failure == "stop_after_stopping":
            self.failure = None
            raise RuntimeError("fixture service stopped but command timed out")

    def is_enabled(self):
        return self.enabled

    def disable(self):
        self.events.append("disable")
        self.enabled = False

    def enable(self):
        self.events.append("enable")
        self.enabled = True

    def refresh(self):
        self.events.append("refresh")

    def start(self):
        self.events.append("start")
        if self.failure == "restore_start":
            raise RuntimeError("fixture restore start failed")
        self.active = True
        self.ports = {("tcp", 443), ("udp", 42710)}

    def listener_snapshot(self):
        return set(self.ports)

    def validate(self, path):
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(value.get("inbounds"), list):
            raise RuntimeError("fixture invalid runtime")

    def run_install(self, installer_path, env, log_path):
        self.events.append("install")
        self.installs += 1
        self.last_environment = dict(env)
        self.fixture.create_installation(new=True)
        self.active = True
        self.ports = {("tcp", 443), ("tcp", 24443), ("tcp", 8443), ("udp", 24444)}
        if self.failure in {"install", "restore_start"}:
            raise RuntimeError("fixture fresh install failed after partial writes")
        if self.failure == "missing_node":
            with closing(sqlite3.connect(self.fixture.paths["database"])) as connection:
                connection.execute("DELETE FROM inbounds WHERE id=4")
                connection.commit()
        if self.failure == "listeners":
            self.ports.remove(("udp", 24444))

    def sleep(self, seconds):
        pass


class CleanReinstall(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="clean-reinstall-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = managed_paths(self.root)
        self.environment_patch = mock.patch.dict(os.environ)
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)
        for key in ("XUI_MAIN_FOLDER", "XUI_BIN_FOLDER", "XUI_DB_TYPE", "XUI_DB_FOLDER", "XUI_DB_DSN"):
            os.environ.pop(key, None)
        self.patch = mock.patch.object(reinstall, "DEFAULT_PATHS", self.paths)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.installer = self.root / "installer.sh"
        self.installer.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
        self.job = Path(self.paths["backup_root"]) / "job-20261029T050000Z-123456abcdef"
        self.job.parent.mkdir(parents=True)
        self.config = {"database": self.paths["database"], "install_dir": self.paths["install_dir"],
            "info_dir": self.paths["info_dir"], "service": "x-ui", "installer": str(self.installer),
            "install_env": {"CERT_MODE": "1", "XUI_SERVER_IP": "203.0.113.42", "AUTO_YES": "1"},
            "verify_attempts": 2, "verify_delay": 0}
        self.create_installation(new=False)
        self.unrelated = self.root / "root/unrelated-user-data"
        self.unrelated.write_bytes(b"never remove unrelated root files")
        self.before = self.snapshot_managed()
        self.before_rows = self.read_rows()
        self.adapter = FixtureAdapter(self)

    def create_installation(self, *, new):
        prefix = "fresh" if new else "original"
        directory = Path(self.paths["install_dir"])
        (directory / "bin").mkdir(parents=True, exist_ok=True)
        (directory / "x-ui").write_bytes((prefix + " panel program").encode())
        (directory / "bin/xray-linux-amd64").write_bytes((prefix + " core program").encode())
        database = Path(self.paths["database"])
        database.parent.mkdir(parents=True, exist_ok=True)
        if database.exists():
            database.unlink()
        nodes = [make_node(key, index, prefix=prefix) for index, key in enumerate(
                 ("hy2", "raw", "reality", "xhttp") if new else ("xhttp",), 1)]
        with closing(sqlite3.connect(database)) as connection:
            connection.executescript("CREATE TABLE inbounds(id INTEGER PRIMARY KEY,tag TEXT,remark TEXT,"
                "port INTEGER,protocol TEXT,enable INTEGER,listen TEXT,settings TEXT,stream_settings TEXT,sniffing TEXT);"
                "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);"
                "CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,password TEXT);")
            connection.executemany("INSERT INTO inbounds VALUES(?,?,?,?,?,?,?,?,?,?)", [item[0] for item in nodes])
            connection.execute("INSERT INTO settings VALUES('webPort',?)", ("20531" if new else "20530",))
            connection.execute("INSERT INTO users VALUES(1,?,?)", (prefix + "-admin", prefix + "-password"))
            connection.commit()
        (directory / "bin/config.json").write_text(json.dumps({"inbounds": [item[2] for item in nodes],
            "outbounds": [{"protocol": "freedom"}]}), encoding="utf-8")
        info = Path(self.paths["info_dir"])
        info.mkdir(parents=True, exist_ok=True)
        for name in INFO_NAMES:
            data = json.dumps([item[1] for item in nodes]) if name.endswith("profiles.json") else prefix + " " + name
            (info / name).write_text(data, encoding="utf-8")
        for name in ("command", "unit", "openrc"):
            path = Path(self.paths[name])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prefix + " " + name, encoding="utf-8")
        for name in ("dropins",):
            path = Path(self.paths[name])
            path.mkdir(parents=True, exist_ok=True)
            (path / "fixture.conf").write_text(prefix + " " + name, encoding="utf-8")
        for name in ("cert_root", "acme_root"):
            path = Path(self.paths[name])
            path.mkdir(parents=True, exist_ok=True)
            unrelated = path / "fixture.conf"
            if not unrelated.exists():
                unrelated.write_text("original " + name, encoding="utf-8")
        for name in ("fullchain.pem", "privkey.pem"):
            path = Path(self.paths["cert_root"]) / "ip" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prefix + " " + name, encoding="utf-8")
        for relative in ("account.conf", "203.0.113.42/203.0.113.42.conf",
                         "203.0.113.42_ecc/203.0.113.42.conf"):
            path = Path(self.paths["acme_root"]) / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(prefix + " " + relative, encoding="utf-8")
        return nodes

    def snapshot_managed(self):
        snapshot = {}
        for key in ("install_dir", "command", "unit", "dropins", "openrc", "cert_root", "acme_root"):
            path = Path(self.paths[key])
            for file in ([path] if path.is_file() else path.rglob("*")):
                if file.is_file():
                    snapshot[file.relative_to(self.root).as_posix()] = file.read_bytes()
        for name in INFO_NAMES:
            file = Path(self.paths["info_dir"]) / name
            snapshot[file.relative_to(self.root).as_posix()] = file.read_bytes()
        return snapshot

    def read_rows(self, database=None):
        with closing(sqlite3.connect(database or self.paths["database"])) as connection:
            return connection.execute("SELECT * FROM inbounds ORDER BY id").fetchall()

    def execute(self):
        return reinstall.execute_reinstall(self.config, self.job, self.adapter)

    def assert_original_restored(self):
        self.assertEqual(self.snapshot_managed(), self.before)
        self.assertEqual(self.read_rows(), self.before_rows)
        self.assertTrue(self.adapter.active)
        self.assertTrue(self.adapter.enabled)
        self.assertEqual(self.unrelated.read_bytes(), b"never remove unrelated root files")

    def test_success_creates_fresh_four_nodes_and_archives_original_state(self):
        result = self.execute()
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(self.adapter.installs, 1)
        self.assertLess(self.adapter.events.index("stop"), self.adapter.events.index("install"))
        rows = self.read_rows()
        self.assertEqual(len(rows), 4)
        self.assertEqual({row[4] for row in rows}, {"vless", "hysteria"})
        self.assertTrue(all("fresh" in row[7] and "original" not in row[7] for row in rows))
        self.assertIn("fresh", (Path(self.paths["info_dir"]) / "3x-ui-node-links.txt").read_text())
        self.assertTrue(self.job.is_dir())
        archived_files = [path for path in self.job.rglob("*") if path.is_file()]
        self.assertTrue(any(path.read_bytes() == b"original panel program" for path in archived_files))
        self.assertTrue(any(path.read_bytes() == self.before["root/3x-ui-node-links.txt"] for path in archived_files))
        self.assertEqual(self.unrelated.read_bytes(), b"never remove unrelated root files")

    def test_partial_install_failure_restores_program_database_infos_certificates_and_acme(self):
        self.adapter.failure = "install"
        result = self.execute()
        self.assertEqual(result["status"], "rolled_back", result)
        self.assert_original_restored()
        self.assertTrue(self.job.exists(), "recovery evidence must remain available")

    def test_missing_new_node_triggers_recovery(self):
        self.adapter.failure = "missing_node"
        result = self.execute()
        self.assertEqual(result["status"], "rolled_back", result)
        self.assert_original_restored()

    def test_missing_new_listener_triggers_recovery(self):
        self.adapter.failure = "listeners"
        result = self.execute()
        self.assertEqual(result["status"], "rolled_back", result)
        self.assert_original_restored()

    def test_failed_restore_start_is_explicit_manual_recovery(self):
        self.adapter.failure = "restore_start"
        result = self.execute()
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(self.snapshot_managed(), self.before)
        self.assertEqual(self.read_rows(), self.before_rows)
        self.assertTrue(self.job.exists())

    def test_unsafe_or_custom_paths_fail_before_stopping_service(self):
        for key, value in (("database", str(self.root / "unrelated.db")),
                           ("install_dir", str(self.root)), ("info_dir", str(self.root)),
                           ("service", "other-service")):
            with self.subTest(key=key):
                config = dict(self.config, **{key: value})
                try:
                    result = reinstall.execute_reinstall(config, self.job, self.adapter)
                except reinstall.ReinstallError:
                    result = {"status": "failed"}
                self.assertNotEqual(result["status"], "complete", result)
                self.assertEqual(self.adapter.events, [])
                self.assertEqual(self.snapshot_managed(), self.before)
                self.assertEqual(self.read_rows(), self.before_rows)

    def test_stop_failure_does_not_archive_or_modify_existing_installation(self):
        self.adapter.failure = "stop"
        result = self.execute()
        self.assertEqual(result["status"], "manual_recovery_required", result)
        self.assertEqual(self.adapter.events, ["stop", "stop"])
        self.assert_original_restored()
        self.assertFalse((self.job / "old" / "db_dir").exists())

    def test_stop_command_error_after_actual_stop_restores_original_running_service(self):
        self.adapter.failure = "stop_after_stopping"
        result = self.execute()
        self.assertEqual(result["status"], "rolled_back", result)
        self.assertEqual(self.adapter.installs, 0)
        self.assert_original_restored()

    def test_uninstall_archives_managed_data_and_disables_service_without_installing_or_touching_certificates(self):
        before_certificates = {key: value for key, value in self.before.items()
                               if key.startswith("root/cert/") or key.startswith("root/.acme.sh/")}
        result = reinstall.execute_reinstall(dict(self.config, operation="uninstall"), self.job, self.adapter)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["operation"], "uninstall")
        self.assertEqual(self.adapter.installs, 0)
        self.assertFalse(self.adapter.active)
        self.assertFalse(self.adapter.enabled)
        for path in reinstall.managed_paths().values():
            self.assertFalse(path.exists(), str(path))
        for name, content in before_certificates.items():
            self.assertEqual((self.root / name).read_bytes(), content)
        self.assertEqual(self.read_rows(self.job / "database-before.db"), self.before_rows)
        self.assertEqual(self.unrelated.read_bytes(), b"never remove unrelated root files")
        self.assertTrue((self.job / "old" / "install_dir" / "x-ui").is_file())
        self.assertTrue((self.job / "old" / "info-2").is_file())

    def test_uninstall_archive_failure_restores_original_data_and_service_state(self):
        original_rename = reinstall.os.rename
        failed = False
        def fail_once(source, target):
            nonlocal failed
            if not failed and Path(target).name == "db_dir" and Path(target).parent.name == "old":
                failed = True
                raise OSError("fixture archive interrupted")
            return original_rename(source, target)
        with mock.patch.object(reinstall.os, "rename", side_effect=fail_once):
            result = reinstall.execute_reinstall(dict(self.config, operation="uninstall"), self.job, self.adapter)
        self.assertTrue(failed)
        self.assertEqual(result["status"], "rolled_back", result)
        self.assertEqual(self.adapter.installs, 0)
        self.assert_original_restored()

    def test_uninstall_failure_preserves_original_disabled_and_inactive_state(self):
        self.adapter.active = False
        self.adapter.enabled = False
        self.adapter.ports = set()
        original_rename = reinstall.os.rename
        failed = False
        def fail_once(source, target):
            nonlocal failed
            if not failed and Path(target).name == "db_dir" and Path(target).parent.name == "old":
                failed = True
                raise OSError("fixture archive interrupted")
            return original_rename(source, target)
        with mock.patch.object(reinstall.os, "rename", side_effect=fail_once):
            result = reinstall.execute_reinstall(dict(self.config, operation="uninstall"), self.job, self.adapter)
        self.assertEqual(result["status"], "rolled_back", result)
        self.assertFalse(self.adapter.active)
        self.assertFalse(self.adapter.enabled)
        self.assertNotIn("start", self.adapter.events)
        self.assertNotIn("enable", self.adapter.events)
        self.assertEqual(self.snapshot_managed(), self.before)
        self.assertEqual(self.read_rows(), self.before_rows)

    def test_ambient_database_overrides_are_refused_before_service_stop(self):
        for key, value in (("XUI_DB_FOLDER", "/tmp/other-database"), ("XUI_DB_TYPE", "postgres"),
                           ("XUI_DB_DSN", "fixture-secret-database-dsn"), ("XUI_MAIN_FOLDER", "/tmp/other-program")):
            with self.subTest(key=key), mock.patch.dict(os.environ, {key: value}):
                result = self.execute()
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(self.adapter.events, [])
                self.assertNotIn(value, json.dumps(result))
                self.assertEqual(self.snapshot_managed(), self.before)

    def test_custom_service_directives_are_refused_before_service_stop(self):
        unit = Path(self.paths["unit"])
        before = unit.read_bytes()
        cases = ('Environment="XUI_DB_FOLDER=/tmp/other-database"\n',
                 'Environment="XUI_DB_TYPE=postgres"\n', 'EnvironmentFile=/tmp/unknown-panel.env\n',
                 'ExecStart=/tmp/other-panel run\n')
        for directive in cases:
            with self.subTest(directive=directive):
                unit.write_text("[Service]\n" + directive, encoding="utf-8")
                result = self.execute()
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(self.adapter.events, [])
                self.assertEqual(self.read_rows(), self.before_rows)
                self.assertNotIn("/tmp/other", json.dumps(result))
                unit.write_bytes(before)

    def test_openrc_custom_runlevels_are_refused_before_stopping_or_archiving(self):
        for levels in ("boot", "default boot", "custom"):
            with self.subTest(levels=levels):
                adapter = reinstall.DefaultAdapter(self.config)
                adapter.systemd = False
                response = subprocess.CompletedProcess(["rc-update", "show"], 0,
                    stdout="unrelated | boot\nx-ui | " + levels + "\n", stderr="")
                with mock.patch.object(reinstall.subprocess, "run", return_value=response) as run:
                    result = reinstall.execute_reinstall(self.config, self.job, adapter)
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["error"], "custom_openrc_runlevels_not_supported")
                self.assertEqual([call.args[0] for call in run.call_args_list], [["rc-update", "show"]])
                self.assertEqual(self.snapshot_managed(), self.before)
                self.assertEqual(self.read_rows(), self.before_rows)

    def test_openrc_default_runlevel_is_preserved_explicitly_by_enable_and_disable(self):
        adapter = reinstall.DefaultAdapter(self.config)
        adapter.systemd = False
        def command(args, **kwargs):
            output = "unrelated | boot\nx-ui | default\n" if args == ["rc-update", "show"] else ""
            return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")
        with mock.patch.object(reinstall.subprocess, "run", side_effect=command) as run:
            adapter.preflight()
            self.assertTrue(adapter.is_enabled())
            adapter.disable()
            adapter.enable()
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["rc-update", "del", "x-ui", "default"], commands)
        self.assertIn(["rc-update", "add", "x-ui", "default"], commands)
        self.assertNotIn(["rc-update", "del", "x-ui"], commands)
        self.assertNotIn(["rc-update", "add", "x-ui"], commands)

    @unittest.skipUnless(os.name == "posix", "WAL relocation fixture requires POSIX open-file rename")
    def test_committed_wal_state_is_in_consistent_backup_and_rollback(self):
        connection = sqlite3.connect(self.paths["database"])
        try:
            self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            connection.execute("PRAGMA wal_autocheckpoint=0")
            connection.execute("UPDATE settings SET value='wal-latest-committed' WHERE key='webPort'")
            connection.commit()
            self.assertTrue(Path(self.paths["database"] + "-wal").is_file())
            main_only = Path(self.paths["database"]).as_uri() + "?mode=ro&immutable=1"
            with closing(sqlite3.connect(main_only, uri=True)) as old_main:
                self.assertEqual(old_main.execute("SELECT value FROM settings WHERE key='webPort'").fetchone()[0], "20530")
            self.adapter.failure = "install"
            result = self.execute()
            self.assertEqual(result["status"], "rolled_back", result)
            for database in (self.paths["database"], self.job / "database-before.db"):
                with closing(sqlite3.connect(database)) as checked:
                    self.assertEqual(checked.execute("SELECT value FROM settings WHERE key='webPort'").fetchone()[0],
                                     "wal-latest-committed")
        finally:
            connection.close()

    @unittest.skipUnless(os.name == "posix", "maintenance mutexes require POSIX flock")
    def test_reinstall_or_performance_lock_prevents_a_second_job(self):
        import fcntl
        locks = [Path(self.paths["backup_root"]) / "reinstall.lock",
                 Path(self.paths["info_dir"]) / "3xuish-performance-repair/repair.lock"]
        for path in locks:
            with self.subTest(lock=path.name):
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    with mock.patch.object(reinstall.subprocess, "Popen") as launch:
                        result = reinstall.launch_reinstall(self.config)
                    self.assertEqual(result["status"], "busy", result)
                    launch.assert_not_called()
                    self.assertEqual(self.adapter.events, [])
                    self.assertEqual(self.read_rows(), self.before_rows)
                finally:
                    os.close(descriptor)

    @unittest.skipUnless(os.name == "posix", "worker liveness checks require POSIX flock")
    def test_orphaned_worker_status_is_read_only_and_blocks_another_reset(self):
        self.job.mkdir()
        reinstall.write_state(self.job, "running", phase="fresh_installation")
        reinstall.atomic_json(self.job.parent / "latest.json", {"job": self.job.name})
        before = (self.job / "state.json").read_bytes()
        state = reinstall.read_latest(self.config)
        self.assertEqual(state["status"], "manual_recovery_required", state)
        self.assertEqual(state["error"], "worker_interrupted")
        self.assertEqual((self.job / "state.json").read_bytes(), before)
        with mock.patch.object(reinstall.subprocess, "Popen") as launch:
            result = reinstall.launch_reinstall(self.config)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        launch.assert_not_called()
        self.assertEqual(self.read_rows(), self.before_rows)

    def test_wait_reads_original_job_even_if_latest_pointer_moves(self):
        self.job.mkdir()
        reinstall.write_state(self.job, "complete", phase="complete")
        later = self.job.parent / "job-20261029T060000Z-abcdef123456"
        later.mkdir()
        reinstall.write_state(later, "failed", error="other-job")
        reinstall.atomic_json(self.job.parent / "latest.json", {"job": later.name})
        result = reinstall.wait_for_completion(self.config,
            {"status": "started", "job_directory": str(self.job)}, timeout=0)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["job_directory"], str(self.job))

    @unittest.skipUnless(os.name == "posix", "process-group cleanup requires POSIX sessions")
    def test_install_timeout_quiesces_descendants_before_original_data_can_be_restored(self):
        marker = self.root / "late-install-write"
        started = self.root / "late-writer-started"
        child = self.root / "late_writer.py"
        child.write_text("import signal,time\nfrom pathlib import Path\n"
                         "signal.signal(signal.SIGTERM,signal.SIG_IGN)\nPath(" + repr(str(started)) + ").touch()\n"
                         "time.sleep(1.0)\nPath(" + repr(str(marker)) + ").write_text('unsafe late write')\n",
                         encoding="utf-8")
        installer = self.root / "slow-install.sh"
        installer.write_text("#!/usr/bin/env bash\n" + repr(sys.executable) + " " + repr(str(child)) +
                             " &\nsleep 10\n", encoding="utf-8")
        adapter = reinstall.DefaultAdapter(dict(self.config, install_timeout=.5))
        with self.assertRaises(subprocess.TimeoutExpired):
            adapter.run_install(installer, os.environ.copy(), self.root / "fixture-install.log")
        self.assertTrue(started.exists(), "timeout fixture did not start the independent writer")
        time.sleep(1.1)
        self.assertFalse(marker.exists(), "an installer descendant wrote after rollback could start")

    @unittest.skipUnless(os.name == "posix", "worker liveness checks require POSIX flock")
    def test_orphaned_uninstall_blocks_a_new_reinstall_even_when_status_pointers_differ(self):
        self.job.mkdir()
        reinstall.write_state(self.job, "running", operation="uninstall", phase="archiving_old_installation")
        reinstall.atomic_json(self.job.parent / "latest.json", {"job": self.job.name})
        reinstall.atomic_json(self.job.parent / "latest-uninstall.json", {"job": self.job.name})
        with mock.patch.object(reinstall.subprocess, "Popen") as launch:
            result = reinstall.launch_reinstall(self.config)
        self.assertEqual(result["status"], "manual_recovery_required", result)
        launch.assert_not_called()
        self.assertEqual(self.read_rows(), self.before_rows)

    def test_operation_status_query_does_not_return_another_operations_success(self):
        self.job.mkdir()
        reinstall.write_state(self.job, "complete", operation="reinstall", phase="complete")
        other = self.job.parent / "job-20261029T060000Z-abcdef123456"
        other.mkdir()
        reinstall.write_state(other, "complete", operation="uninstall", phase="complete")
        reinstall.atomic_json(self.job.parent / "latest.json", {"job": other.name})
        reinstall.atomic_json(self.job.parent / "latest-reinstall.json", {"job": self.job.name})
        reinstall.atomic_json(self.job.parent / "latest-uninstall.json", {"job": other.name})
        reset = reinstall.read_latest(self.config)
        uninstall = reinstall.read_latest(dict(self.config, operation="uninstall"))
        self.assertEqual(reset["operation"], "reinstall", reset)
        self.assertEqual(uninstall["operation"], "uninstall", uninstall)

    @unittest.skipUnless(os.name == "posix", "detached maintenance requires POSIX sessions and locks")
    def test_actual_detached_uninstall_survives_source_removal_and_holds_both_locks(self):
        source = Path(reinstall.__file__).read_text(encoding="utf-8")
        assignment = next(node for node in ast.parse(source).body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "DEFAULT_PATHS" for target in node.targets))
        lines = source.splitlines(keepends=True)
        lines[assignment.lineno - 1:assignment.end_lineno] = ["DEFAULT_PATHS = " + repr(self.paths) + "\n"]
        downloaded = self.root / "downloaded-maintenance.py"
        downloaded.write_text("".join(lines), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("fixture_detached_maintenance", downloaded)
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        shim_directory = self.root / "shim-bin"
        shim_directory.mkdir()
        state_path = self.root / "fixture-service.json"
        state_path.write_text(json.dumps({"active": True, "enabled": True}), encoding="utf-8")
        release = self.root / "release-stop"
        stopping = self.root / "service-stopping"
        shim = r'''
import json, os, sys, time
from pathlib import Path
state_path=Path(os.environ['MAINTENANCE_FIXTURE_STATE'])
state=json.loads(state_path.read_text())
name=Path(sys.argv[0]).name
args=sys.argv[1:]
if name=='systemctl' and args and args[0]=='show':
    if '--property=FragmentPath' in args: print(os.environ['MAINTENANCE_FIXTURE_UNIT'])
    raise SystemExit(0)
if name=='ss':
    if state['active']:
        print('tcp LISTEN 0 128 0.0.0.0:443 0.0.0.0:* users:(("xray",pid=99,fd=3))')
        print('udp UNCONN 0 0 0.0.0.0:42710 0.0.0.0:* users:(("xray",pid=99,fd=4))')
    raise SystemExit(0)
if name=='rc-update':
    if 'show' in args:
        if state['enabled']: print('x-ui | default')
    elif 'del' in args: state['enabled']=False
    elif 'add' in args: state['enabled']=True
else:
    command=args[0] if name=='systemctl' else args[-1]
    if command in ('status','is-active'): raise SystemExit(0 if state['active'] else 3)
    if command=='is-enabled':
        print('enabled' if state['enabled'] else 'disabled')
        raise SystemExit(0 if state['enabled'] else 1)
    if command=='stop':
        Path(os.environ['MAINTENANCE_FIXTURE_STOPPING']).touch()
        deadline=time.monotonic()+20
        while not Path(os.environ['MAINTENANCE_FIXTURE_RELEASE']).exists():
            if time.monotonic()>deadline: raise SystemExit(2)
            time.sleep(.03)
        state['active']=False
    elif command=='start': state['active']=True
    elif command=='disable': state['enabled']=False
    elif command=='enable': state['enabled']=True
state_path.write_text(json.dumps(state))
'''
        for name in ("systemctl", "rc-service", "rc-update", "ss"):
            path = shim_directory / name
            path.write_text("#!" + sys.executable + "\n" + shim, encoding="utf-8")
            path.chmod(0o755)
        processes = []
        actual_popen = subprocess.Popen
        def retain_process(*args, **kwargs):
            process = actual_popen(*args, **kwargs)
            processes.append(process)
            return process
        def finish_processes():
            release.touch()
            for process in processes:
                try:
                    process.wait(timeout=25)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
        self.addCleanup(finish_processes)
        environment = {"PATH": str(shim_directory) + os.pathsep + os.environ.get("PATH", ""),
            "MAINTENANCE_FIXTURE_STATE": str(state_path), "MAINTENANCE_FIXTURE_RELEASE": str(release),
            "MAINTENANCE_FIXTURE_STOPPING": str(stopping), "MAINTENANCE_FIXTURE_UNIT": self.paths["unit"]}
        config = dict(self.config, operation="uninstall")
        with mock.patch.dict(os.environ, environment), \
                mock.patch.object(helper.subprocess, "Popen", side_effect=retain_process):
            launched = helper.launch_reinstall(config)
        self.assertEqual(launched["status"], "started", launched)
        self.assertEqual(os.getsid(processes[0].pid), processes[0].pid)
        downloaded.unlink()
        deadline = time.monotonic() + 8
        while not stopping.exists() and time.monotonic() < deadline:
            time.sleep(.03)
        self.assertTrue(stopping.exists(), "detached worker never entered service stop")
        self.assertEqual(helper.launch_reinstall(config)["status"], "busy")
        release.touch()
        result = helper.wait_for_completion(config, launched, timeout=15)
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(result["operation"], "uninstall")
        self.assertEqual(json.loads(state_path.read_text()), {"active": False, "enabled": False})
        self.assertFalse(Path(self.paths["database"]).exists())
        archived = Path(launched["job_directory"]) / "old" / "db_dir" / "x-ui.db"
        self.assertEqual(self.read_rows(archived), self.before_rows)
        self.assertEqual(processes[0].wait(timeout=5), 0)

    @unittest.skipUnless(os.name == "posix", "symlink safety uses POSIX fixture links")
    def test_symlinked_managed_directory_is_rejected_before_stop(self):
        cert_root = Path(self.paths["cert_root"])
        external = self.root / "outside-certificates"
        cert_root.rename(external)
        cert_root.symlink_to(external, target_is_directory=True)
        try:
            result = self.execute()
        except reinstall.ReinstallError:
            result = {"status": "failed"}
        self.assertNotEqual(result["status"], "complete", result)
        self.assertEqual(self.adapter.events, [])
        self.assertEqual((external / "fixture.conf").read_text(), "original cert_root")


if __name__ == "__main__":
    unittest.main()
