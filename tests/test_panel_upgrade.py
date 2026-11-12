"""Panel updates preserve identity and recover both program and migrated DB."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import signal
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import panel_upgrade as upgrade


def fixture_paths(root):
    return {"database": str(root / "etc/x-ui/x-ui.db"), "db_dir": str(root / "etc/x-ui"),
            "install_dir": str(root / "usr/local/x-ui"), "info_dir": str(root / "root"),
            "command": str(root / "usr/bin/x-ui"), "unit": str(root / "etc/systemd/system/x-ui.service"),
            "dropins": str(root / "etc/systemd/system/x-ui.service.d"), "openrc": str(root / "etc/init.d/x-ui"),
            "backup_root": str(root / "root/3xuish-panel-update")}


def file_version(path):
    return Path(path).read_text().splitlines()[0]


class FixtureService:
    def __init__(self, case):
        self.case = case
        self.active = True
        self.failure = None
        self.events = []
        self.ports = {("tcp", 443), ("tcp", 2053)}

    def preflight(self):
        self.events.append("preflight")

    def is_active(self):
        return self.active

    def stop(self):
        self.events.append("stop")
        if self.failure == "stop_active":
            raise upgrade.UpgradeError("panel_stop_failed")
        self.active = False
        self.ports = set()
        if self.failure == "stop_inactive":
            self.failure = None
            raise upgrade.UpgradeError("panel_stop_failed")

    def quiescent(self):
        return not self.active and self.failure != "leftover_core"

    def start(self):
        self.events.append("start")
        version = file_version(Path(self.case.paths["install_dir"]) / "x-ui")
        if version == "3.11.0":
            with closing(sqlite3.connect(self.case.paths["database"])) as db:
                db.execute("CREATE TABLE IF NOT EXISTS migration_marker (value TEXT)")
                db.execute("INSERT INTO migration_marker VALUES ('new schema')")
                if self.failure in ("migration", "restore_start"):
                    db.execute("UPDATE users SET password='new-broken-secret'")
                if self.failure == "external_links":
                    db.execute("UPDATE client_external_links SET value='broken-subscription-link'")
                db.commit()
            if self.failure == "start":
                raise upgrade.UpgradeError("panel_start_failed")
        elif self.failure == "restore_start":
            raise upgrade.UpgradeError("old_start_failed")
        self.active = True
        self.ports = {("tcp", 443), ("tcp", 2053)}
        if version == "3.11.0" and self.failure == "listener":
            self.ports.remove(("tcp", 443))

    def panel_ready(self):
        return self.active and not (self.failure == "http" and file_version(Path(self.case.paths["install_dir"]) / "x-ui") == "3.11.0")

    def listener_snapshot(self):
        return set(self.ports)

    def sleep(self, _seconds):
        pass


class PanelUpgrade(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="panel-upgrade-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.paths = fixture_paths(self.root)
        self.patch = mock.patch.object(upgrade, "DEFAULT_PATHS", self.paths)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.environment = mock.patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for key in upgrade._expected_environment():
            os.environ.pop(key, None)
        self.version = mock.patch.object(upgrade.panel_release, "installed_version", side_effect=file_version)
        self.version.start()
        self.addCleanup(self.version.stop)
        self.config = {key: self.paths[key] for key in ("database", "install_dir", "info_dir")}
        self.config.update(service="x-ui", verify_attempts=2, verify_delay=0)
        install = Path(self.paths["install_dir"])
        (install / "bin").mkdir(parents=True)
        (install / "x-ui").write_text("3.10.0\nold binary\n")
        (install / "x-ui.sh").write_text("#!/bin/sh\nexit 0\n")
        (install / "bin/xray-linux-amd64").write_bytes(b"selected-newer-core")
        (install / "bin/geoip.dat").write_bytes(b"operator-geodata")
        (install / "bin/config.json").write_text('{"inbounds":[],"outbounds":[]}')
        command = Path(self.paths["command"])
        command.parent.mkdir(parents=True)
        command.write_text("old cli\n")
        unit = Path(self.paths["unit"])
        unit.parent.mkdir(parents=True)
        unit.write_text(f"[Service]\nWorkingDirectory={install}/\nExecStart={shlex.quote(str(install / 'x-ui'))}\nKillMode=mixed\n")
        database = Path(self.paths["database"])
        database.parent.mkdir(parents=True)
        (database.parent / "encryption.key").write_bytes(b"preserve-panel-secret-key")
        with closing(sqlite3.connect(database)) as db:
            db.executescript("CREATE TABLE inbounds(id INTEGER PRIMARY KEY,tag TEXT,remark TEXT,port INTEGER,"
                             "protocol TEXT,enable INTEGER,listen TEXT,expiry_time INTEGER,total INTEGER,"
                             "settings TEXT,stream_settings TEXT,sniffing TEXT);"
                             "CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,password TEXT);"
                             "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);"
                             "CREATE TABLE client_external_links(id INTEGER PRIMARY KEY,client_id INTEGER,kind TEXT,"
                             "value TEXT,remark TEXT,sort_order INTEGER,created_at TEXT,updated_at TEXT);")
            client = {"id": "existing-secret-uuid", "email": "existing-client", "subId": "existing-sub-id", "enable": True}
            db.execute("INSERT INTO inbounds VALUES(1,'original','Existing node',443,'vless',1,'',0,0,?,?,?)",
                       (json.dumps({"clients": [client], "decryption": "none"}), '{"network":"tcp","security":"tls"}', '{"enabled":false}'))
            db.execute("INSERT INTO users VALUES(1,'existing-admin','old-secret-hash')")
            db.execute("INSERT INTO client_external_links VALUES(1,1,'link','vless://private-linked-uuid@example.test:443',"
                       "'Existing subscription node',0,'old-time','old-time')")
            db.executemany("INSERT INTO settings VALUES(?,?)", (("webPort", "2053"), ("subPath", "/original-sub/"),
                                                                ("remarkTemplate", "[节点] {remark} {protocol}")))
            db.commit()
        self.job = Path(self.paths["backup_root"]) / "job-20261112T120000Z-123456abcdef"
        self.job.mkdir(mode=0o700, parents=True)
        self.candidate = self.root / "candidate"
        self.candidate.mkdir()
        (self.candidate / "x-ui").write_text("3.11.0\nnew binary\n")
        self.prepared = {"version": "3.11.0", "binary": str(self.candidate / "x-ui"), "cli": None}
        self.adapter = FixtureService(self)
        self.before = upgrade.snapshot_identity(database)
        self.program_before = self.program_bytes()

    def program_bytes(self):
        base = Path(self.paths["install_dir"])
        return {str(path.relative_to(base)): path.read_bytes() for path in base.rglob("*") if path.is_file()}

    def execute(self):
        return upgrade.execute_upgrade(self.config, self.job, adapter=self.adapter, prepared=self.prepared)

    def test_success_preserves_nodes_accounts_subscription_core_and_service_files(self):
        before_unit = Path(self.paths["unit"]).read_bytes()
        result = self.execute()
        self.assertEqual(result["status"], "complete", result)
        self.assertEqual(file_version(Path(self.paths["install_dir"]) / "x-ui"), "3.11.0")
        self.assertTrue(upgrade.identities_preserved(self.before, upgrade.snapshot_identity(self.paths["database"])))
        self.assertEqual((Path(self.paths["install_dir"]) / "bin/xray-linux-amd64").read_bytes(), b"selected-newer-core")
        self.assertEqual(Path(self.paths["unit"]).read_bytes(), before_unit)
        self.assertEqual((Path(self.paths["db_dir"]) / "encryption.key").read_bytes(), b"preserve-panel-secret-key")
        self.assertTrue((self.job / "backup/complete.json").is_file())
        self.assertNotIn("existing-secret", (self.job / "state.json").read_text())
        self.assertNotIn("old-secret", (self.job / "worker.log").read_text())

    def test_download_failure_or_digest_error_never_stops_existing_service(self):
        for failure in ("release_download_failed", "release_archive_digest_mismatch"):
            with self.subTest(failure=failure), mock.patch.object(upgrade.panel_release, "latest_release", return_value={"version": "3.11.0"}), \
                    mock.patch.object(upgrade.panel_release, "prepare_release", side_effect=upgrade.panel_release.ReleaseError(failure)):
                result = upgrade.execute_upgrade(self.config, self.job, adapter=self.adapter)
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"], failure)
                self.assertNotIn("stop", self.adapter.events)
                self.assertEqual(self.program_bytes(), self.program_before)
                self.assertEqual(upgrade.snapshot_identity(self.paths["database"]), self.before)

    def test_same_or_newer_version_does_not_download_stop_or_restart(self):
        for latest in ("3.10.0", "3.9.0"):
            with self.subTest(latest=latest), mock.patch.object(upgrade.panel_release, "latest_release", return_value={"version": latest}), \
                    mock.patch.object(upgrade.panel_release, "prepare_release") as prepare:
                result = upgrade.execute_upgrade(self.config, self.job, adapter=self.adapter)
                self.assertEqual(result["status"], "no_update")
                self.assertNotIn("stop", self.adapter.events)
                prepare.assert_not_called()

    def test_migration_or_missing_listener_or_http_failure_restores_program_and_database(self):
        for failure in ("migration", "listener", "http", "start"):
            with self.subTest(failure=failure):
                self.adapter.failure = failure
                result = self.execute()
                self.assertEqual(result["status"], "rolled_back", result)
                self.assertEqual(self.program_bytes(), self.program_before)
                self.assertEqual(upgrade.snapshot_identity(self.paths["database"]), self.before)
                with closing(sqlite3.connect(self.paths["database"])) as db:
                    self.assertFalse(db.execute("SELECT name FROM sqlite_master WHERE name='migration_marker'").fetchone())
                self.assertTrue(self.adapter.active)
                # Each transaction must have its own immutable recovery folder.
                self.job = self.job.with_name("job-20261112T120000Z-" + {"migration": "a", "listener": "b", "http": "c", "start": "d"}[failure] * 12)
                self.job.mkdir()

    def test_failed_old_service_restart_is_manual_recovery_with_backups(self):
        self.adapter.failure = "restore_start"
        result = self.execute()
        self.assertEqual(result["status"], "manual_recovery_required")
        self.assertTrue((self.job / "backup/database/x-ui.db").exists())

    def test_subscription_external_link_changes_trigger_database_rollback(self):
        self.adapter.failure = "external_links"
        result = self.execute()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(upgrade.snapshot_identity(self.paths["database"]), self.before)
        self.assertNotIn("private-linked-uuid", (self.job / "state.json").read_text())
        self.assertNotIn("private-linked-uuid", (self.job / "worker.log").read_text())

    def test_external_link_timestamps_are_ignored_and_new_table_can_be_added(self):
        with closing(sqlite3.connect(self.paths["database"])) as db:
            db.execute("UPDATE client_external_links SET updated_at='new-time'")
            db.commit()
        self.assertEqual(upgrade.snapshot_identity(self.paths["database"]), self.before)
        before = copy.deepcopy(self.before)
        before.pop("external_links")
        self.assertTrue(upgrade.identities_preserved(before, self.before))

    def test_stop_failure_does_not_replace_files_and_stopped_service_is_recovered(self):
        self.adapter.failure = "stop_active"
        result = self.execute()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(self.program_bytes(), self.program_before)
        self.adapter.failure = "stop_inactive"
        result = self.execute()
        self.assertEqual(result["status"], "rolled_back")
        self.assertTrue(self.adapter.active)

    def test_leftover_core_prevents_backup_and_replacement(self):
        self.adapter.failure = "leftover_core"
        result = self.execute()
        self.assertEqual(result["status"], "rolled_back")
        self.assertTrue(self.adapter.active)
        self.assertEqual(self.program_bytes(), self.program_before)
        self.assertFalse((self.job / "backup").exists())

    def test_candidate_cli_is_installed_after_verification(self):
        from types import SimpleNamespace
        cli = self.candidate / "x-ui.sh"
        cli.write_text("#!/bin/sh\nprintf 'updated CLI\\n'\n")
        self.prepared["cli"] = str(cli)
        with mock.patch.object(upgrade.subprocess, "run", return_value=SimpleNamespace(returncode=0)) as check:
            result = self.execute()
        self.assertEqual(result["status"], "complete")
        check.assert_called_once()
        self.assertEqual(Path(self.paths["command"]).read_bytes(), cli.read_bytes())
        self.assertEqual((Path(self.paths["install_dir"]) / "x-ui.sh").read_bytes(), cli.read_bytes())

    def test_failed_upgrade_restores_cli_as_well_as_binary_and_database(self):
        from types import SimpleNamespace
        cli = self.candidate / "x-ui.sh"
        cli.write_text("#!/bin/sh\nprintf 'updated CLI\\n'\n")
        self.prepared["cli"] = str(cli)
        self.adapter.failure = "migration"
        original = Path(self.paths["command"]).read_bytes()
        with mock.patch.object(upgrade.subprocess, "run", return_value=SimpleNamespace(returncode=0)):
            result = self.execute()
        self.assertEqual(result["status"], "rolled_back")
        self.assertEqual(Path(self.paths["command"]).read_bytes(), original)
        self.assertEqual(self.program_bytes(), self.program_before)

    def test_custom_layout_environment_or_unit_is_rejected_before_stop(self):
        bad = {**self.config, "database": str(self.root / "other.db")}
        self.assertEqual(upgrade.execute_upgrade(bad, self.job, self.adapter, prepared=self.prepared)["status"], "failed")
        with mock.patch.dict(os.environ, {"XUI_DB_DSN": "postgres-secret"}):
            self.assertEqual(self.execute()["status"], "failed")
        Path(self.paths["unit"]).write_text("[Service]\nExecStart=/custom/panel\n")
        self.assertEqual(self.execute()["status"], "failed")
        self.assertNotIn("stop", self.adapter.events)

    def test_default_unit_trailing_slash_and_mixed_killmode_are_supported(self):
        upgrade.validate_layout(self.config)

    def test_dotenv_database_redirect_is_rejected_before_stop(self):
        for entry in ("XUI_DB_FOLDER=/outside-data\n", "XUI_DB_FOLDER: /outside-data\n",
                      'XUI_DB_FOLDER="${OTHER_PATH}"\n',
                      "XUI_DB_FOLDER='" + self.paths["db_dir"] + "#outside'\n",
                      "XUI_DB_FOLDER=" + self.paths["db_dir"] + "#outside\n",
                      "XUI_DB_FOLDER=" + self.paths["db_dir"] + "''\n"):
            with self.subTest(entry=entry):
                (Path(self.paths["install_dir"]) / ".env").write_text(entry)
                result = self.execute()
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error"], "custom_database_environment_not_supported")
                self.assertNotIn("stop", self.adapter.events)

    def test_dotenv_default_values_allow_trailing_comments_and_yaml_separator(self):
        path = Path(self.paths["install_dir"]) / ".env"
        path.write_text('XUI_DB_TYPE: sqlite # default database\nXUI_DB_FOLDER="' + self.paths["db_dir"].replace('\\', '\\\\') + '" # unchanged\n')
        upgrade.check_environment_file(path)

    def test_recovery_space_is_reserved_not_just_initial_backup(self):
        from types import SimpleNamespace
        megabyte = 1024 * 1024
        with mock.patch.object(upgrade, "_tree_size", side_effect=(80 * megabyte, 30 * megabyte)), \
                mock.patch.object(upgrade.shutil, "disk_usage", return_value=SimpleNamespace(free=150 * megabyte)):
            result = self.execute()
        self.assertEqual(result["error"], "insufficient_backup_space")
        self.assertNotIn("stop", self.adapter.events)

    def test_effective_systemd_command_is_checked_not_just_disk_unit(self):
        from types import SimpleNamespace
        adapter = upgrade.NativeAdapter(self.config)
        adapter.systemd = True
        original_exists = Path.exists
        values = {"FragmentPath": self.paths["unit"], "DropInPaths": "", "Environment": "",
                  "ExecStart": "{ path=/outside/panel ; argv[]=/outside/panel ; }", "User": "root",
                  "RootDirectory": "", "WorkingDirectory": self.paths["install_dir"],
                  "KillMode": "mixed", "MainPID": "123"}
        def call(args, **kwargs):
            prop = next(item.split("=", 1)[1] for item in args if item.startswith("--property="))
            return SimpleNamespace(returncode=0, stdout=values[prop])
        with mock.patch.object(Path, "exists", lambda path: False if str(path) in ("/.dockerenv", "/run/.containerenv") else original_exists(path)), \
                mock.patch.object(upgrade.subprocess, "run", side_effect=call):
            with self.assertRaisesRegex(upgrade.UpgradeError, "effective_service_not_supported"):
                adapter.preflight()

    def test_status_does_not_claim_a_backup_before_backup_completed(self):
        result = upgrade.write_state(self.job, "no_update", current_version="3.10.0", target_version="3.10.0")
        self.assertNotIn("备份：", upgrade.render(result))

    def test_remark_template_is_not_misparsed_as_json(self):
        self.assertEqual(self.before["settings"]["remarkTemplate"], "[节点] {remark} {protocol}")

    def test_new_inbound_is_not_silently_accepted_by_identity_check(self):
        after = copy.deepcopy(self.before)
        after["inbounds"]["2"] = copy.deepcopy(after["inbounds"]["1"])
        self.assertFalse(upgrade.identities_preserved(self.before, after))

    def test_missing_orphan_status_is_read_only_and_blocks_next_update(self):
        empty = self.root / "empty-info"
        self.assertEqual(upgrade.read_latest(empty)["status"], "no_previous_job")
        self.assertFalse(empty.exists())
        upgrade.write_state(self.job, "running", phase="replacing_panel")
        upgrade.atomic_json(self.job.parent / "latest.json", {"job": self.job.name})
        before = (self.job / "state.json").read_bytes()
        if os.name == "posix":
            self.assertEqual(upgrade.read_latest(self.paths["info_dir"])["status"], "manual_recovery_required")
        with self.assertRaises(upgrade.UpgradeError):
            upgrade.require_finished_jobs(self.paths["info_dir"])
        self.assertEqual((self.job / "state.json").read_bytes(), before)

    @unittest.skipUnless(os.name == "posix", "WAL restore fixture requires POSIX open-file rename")
    def test_committed_wal_data_is_backed_up_and_failed_wal_cannot_replay(self):
        with closing(sqlite3.connect(self.paths["database"])) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("PRAGMA wal_autocheckpoint=0")
            writer.execute("UPDATE users SET username='committed-wal-admin'")
            writer.commit()
            expected = upgrade.snapshot_identity(self.paths["database"])
            self.adapter.failure = "migration"
            result = self.execute()
            self.assertEqual(result["status"], "rolled_back", result)
            self.assertEqual(upgrade.snapshot_identity(self.paths["database"]), expected)
            self.assertFalse(Path(self.paths["database"] + "-wal").exists())

    def test_native_container_detection_refuses_before_service_calls(self):
        adapter = upgrade.NativeAdapter(self.config)
        with mock.patch.object(Path, "exists", return_value=True), mock.patch.object(adapter, "is_active") as active:
            with self.assertRaisesRegex(upgrade.UpgradeError, "container"):
                adapter.preflight()
            active.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "detached worker requires POSIX sessions and flock")
    def test_detached_launcher_survives_hup_and_keeps_both_locks(self):
        worker = self.root / "fixture_worker.py"
        worker.write_text('''import json,pathlib,sys,time
job=pathlib.Path(sys.argv[2])
(job/'ready').touch()
while not (job/'release').exists(): time.sleep(.05)
state=json.loads((job/'state.json').read_text()); state['status']='complete'
(job/'state.json').write_text(json.dumps(state))
''')
        launcher = '''import importlib.util,json,pathlib,sys,time
sys.path.insert(0,sys.argv[1]); import panel_upgrade as m
m.DEFAULT_PATHS=json.loads(sys.argv[2]); m.__file__=sys.argv[3]
m.os.geteuid=lambda:0
class Adapter:
 def __init__(self,c): pass
 def preflight(self): pass
m.NativeAdapter=Adapter
result=m.launch(json.loads(sys.argv[4])); print(json.dumps(result),flush=True)
time.sleep(30)
'''
        config = dict(self.config)
        process = subprocess.Popen([sys.executable, "-c", launcher, str(ROOT / "lib"), json.dumps(self.paths),
                                    str(worker), json.dumps(config)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        try:
            line = process.stdout.readline()
            if not line:
                self.fail(process.stderr.read())
            result = json.loads(line)
            job = Path(result["job_directory"])
            deadline = time.monotonic() + 8
            while not (job / "ready").exists() and time.monotonic() < deadline:
                time.sleep(.05)
            self.assertTrue((job / "ready").exists())
            os.killpg(process.pid, signal.SIGHUP)
            process.communicate(timeout=5)
            worker.unlink()
            import fcntl
            for lock in (Path(self.paths["info_dir"]) / "3xuish-reinstall/reinstall.lock",
                         Path(self.paths["info_dir"]) / "3xuish-performance-repair/repair.lock"):
                with open(lock, "rb") as handle:
                    with self.assertRaises(BlockingIOError):
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            (job / "release").touch()
            self.assertEqual(upgrade.wait_job(job, 8)["status"], "complete")
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.communicate(timeout=5)
            else:
                process.communicate(timeout=5)


if __name__ == "__main__":
    unittest.main()
