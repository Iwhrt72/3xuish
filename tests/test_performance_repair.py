"""Exercise in-place tuning against SQLite, including rollback and concurrency."""
from __future__ import annotations

import copy
from contextlib import closing
import hashlib
import importlib.util
import os
import time
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import performance_repair as repair


OLD = {"tcpWindowClamp": 600, "tcpMaxSeg": 1440, "tcpUserTimeout": 10000,
       "tcpcongestion": "bbr", "tcpKeepAliveIdle": 300, "domainStrategy": "UseIP"}
FIXED = {"tcpWindowClamp": 0, "tcpKeepAliveIdle": 300, "domainStrategy": "UseIP"}
SUCCESS = {"complete", "completed", "success"}
NO_CHANGE = {"no_changes", "unchanged", "noop", "not_needed"}
FAILURE = {"failed", "rolled_back", "manual_recovery_required"}


class PanelAdapter:
    """Simulate the panel regenerating runtime from its actual SQLite rows."""
    def __init__(self, fixture):
        self.fixture = fixture
        self.active = True
        self.restart_count = 0
        self.validations = []
        self.validation_hook = None
        self.restart_hook = None
        self.listener_hook = None

    def is_active(self):
        return self.active

    def validate(self, path):
        path = Path(path)
        self.validations.append(path)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data.get("inbounds"), list) or data.get("outbounds") != self.fixture.runtime["outbounds"]:
            raise RuntimeError("fixture rejected incomplete runtime")
        if self.validation_hook:
            self.validation_hook(path, len(self.validations))

    def restart(self):
        self.restart_count += 1
        if self.restart_hook:
            self.restart_hook(self.restart_count)
        self.fixture.regenerate_runtime()

    def listeners(self):
        if self.listener_hook:
            return self.listener_hook()
        value = json.loads(self.fixture.runtime_path.read_text(encoding="utf-8"))
        return {entry["port"] for entry in value["inbounds"]
                if entry["protocol"] != "hysteria"}

    def sleep(self, seconds):
        pass


class PerformanceRepair(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="3xuish-performance-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.info = self.root / "info"
        self.info.mkdir()
        self.install_dir = self.root / "server"
        (self.install_dir / "bin").mkdir(parents=True)
        self.database = self.root / "x-ui.db"
        self.runtime_path = self.install_dir / "bin/config.json"
        self.manifest_path = self.info / "3x-ui-node-profiles.json"
        self.job = self.info / "fixture-job"
        self.config = {
            "database": str(self.database), "install_dir": str(self.install_dir),
            "info_dir": str(self.info), "service": "fixture-x-ui",
            "xray": str(self.install_dir / "bin/fixture-xray"),
            "inbound_id": None, "verify_attempts": 2, "verify_delay": 0,
        }
        records = [
            (1, "legacy-panel-generated-tag", "VLESS-XHTTP-Enc-Vision", 443, 1, OLD),
            (2, "unrelated-node", "User custom node", 24442, 1, OLD),
            (3, "3xuish-fixture-raw", "3xuish-RAW-TLS-Vision", 24445, 1, OLD),
            (4, "custom-tuning", "VLESS-XHTTP-Enc-Vision", 24446, 1,
             {"tcpWindowClamp": 12000, "tcpMaxSeg": 1380, "tcpUserTimeout": 25000,
              "tcpcongestion": "cubic", "tcpKeepAliveIdle": 300}),
            (5, "disabled-legacy", "VLESS-XHTTP-Enc-Vision", 24447, 0, OLD),
        ]
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                "CREATE TABLE inbounds(id INTEGER PRIMARY KEY,tag TEXT UNIQUE,remark TEXT,"
                "port INTEGER,protocol TEXT,enable INTEGER,listen TEXT,settings TEXT,"
                "stream_settings TEXT,sniffing TEXT,up INTEGER,down INTEGER,total INTEGER,expiry_time INTEGER);"
                "CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,password TEXT);"
                "CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);")
            connection.execute("INSERT INTO users VALUES (1,'fixture-admin','unchanged-password-hash')")
            connection.execute("INSERT INTO settings VALUES ('webPort','20530')")
            for ident, tag, remark, port, enable, sockopt in records:
                stream = {"network": "tcp" if ident == 3 else "xhttp", "security": "tls",
                          "tlsSettings": {"serverName": "fixture.example", "minVersion": "1.0",
                                          "maxVersion": "1.3", "certificates": [{
                                              "certificateFile": "/fixture/fullchain.pem",
                                              "keyFile": "/fixture/privkey.pem"}]},
                          "sockopt": copy.deepcopy(sockopt)}
                if ident != 3:
                    stream["xhttpSettings"] = {"path": "/keep-this-path", "mode": "auto"}
                settings = {"clients": [{"id": f"00000000-0000-4000-8000-{ident:012d}",
                                        "flow": "xtls-rprx-vision", "email": f"fixture-{ident}",
                                        "subId": f"keep-subscription-{ident}", "enable": True}],
                            "decryption": "none", "encryption": "none"}
                connection.execute("INSERT INTO inbounds VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    ident, tag, remark, port, "vless", enable, "", json.dumps(settings, indent=1),
                    json.dumps(stream, indent=1), '{"enabled":false}', 11, 22, 0, 0))
            connection.commit()
        rows = self.rows()
        managed = rows[3]
        self.manifest = [{
            "key": "raw", "port": managed["port"], "credential": "fixture-credential-preserved",
            "link": "vless://fixture-do-not-regenerate-link",
            "clientOutbound": {"protocol": "vless", "settings": {"address": "fixture.example",
                                "port": managed["port"], "id": "preserved-client-id"}},
            "payload": {"tag": managed["tag"], "port": managed["port"], "protocol": "vless",
                        "settings": managed["settings"], "streamSettings": managed["stream_settings"]},
        }]
        self.manifest_path.write_text(json.dumps(self.manifest, indent=2), encoding="utf-8")
        self.links = self.info / "3x-ui-node-links.txt"
        self.links.write_bytes(b"preserve-exact-import-links\n")
        self.info_file = self.info / "3x-ui-nodes-info.txt"
        self.info_file.write_bytes(b"preserve-existing-panel-information\n")
        self.runtime = {
            "log": {"loglevel": "warning"},
            "inbounds": [],
            "outbounds": [{"tag": "direct", "protocol": "freedom", "settings": {}}],
            "routing": {"domainStrategy": "AsIs", "rules": []},
        }
        self.regenerate_runtime()
        self.before_rows = self.rows()
        self.before_db = self.database.read_bytes()
        self.before_runtime = self.runtime_path.read_bytes()
        self.before_manifest = self.manifest_path.read_bytes()
        self.adapter = PanelAdapter(self)

    def rows(self):
        with closing(sqlite3.connect(self.database)) as connection:
            connection.row_factory = sqlite3.Row
            return {row["id"]: dict(row) for row in connection.execute("SELECT * FROM inbounds ORDER BY id")}

    def regenerate_runtime(self):
        runtime = copy.deepcopy(self.runtime)
        for row in self.rows().values():
            if row["enable"]:
                runtime["inbounds"].append({
                    "tag": row["tag"], "port": row["port"], "protocol": row["protocol"],
                    "listen": row["listen"], "settings": json.loads(row["settings"]),
                    "streamSettings": json.loads(row["stream_settings"]),
                    "sniffing": json.loads(row["sniffing"]),
                })
        self.runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

    def update_stream(self, ident, callback):
        row = self.rows()[ident]
        stream = json.loads(row["stream_settings"])
        callback(stream)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE inbounds SET stream_settings=? WHERE id=?",
                               (json.dumps(stream), ident))
            connection.commit()

    def execute(self, *, inbound_id=None, job=None):
        config = dict(self.config, inbound_id=inbound_id)
        return repair.execute_repair(config, job or self.job, adapter=self.adapter)

    def assert_preserved_except_tuning(self):
        after = self.rows()
        for ident, before in self.before_rows.items():
            original = copy.deepcopy(before)
            actual = copy.deepcopy(after[ident])
            original_stream = json.loads(original.pop("stream_settings"))
            actual_stream = json.loads(actual.pop("stream_settings"))
            self.assertEqual(original, actual, f"non-stream fields changed on {ident}")
            if ident in {1, 3, 5}:
                original_stream["sockopt"] = FIXED
            self.assertEqual(original_stream, actual_stream, f"unexpected stream change on {ident}")
        self.assertEqual(self.links.read_bytes(), b"preserve-exact-import-links\n")
        self.assertEqual(self.info_file.read_bytes(), b"preserve-existing-panel-information\n")
        with closing(sqlite3.connect(self.database)) as connection:
            self.assertEqual(connection.execute("SELECT * FROM users").fetchall(),
                             [(1, "fixture-admin", "unchanged-password-hash")])
            self.assertEqual(connection.execute("SELECT * FROM settings").fetchall(), [("webPort", "20530")])

    def test_check_is_read_only_and_default_scope_is_precise(self):
        plan = repair.make_plan(self.database, self.manifest_path)
        self.assertEqual({row["id"] for row in plan["rows"]}, {1, 3, 5})
        self.assertEqual(self.database.read_bytes(), self.before_db)
        self.assertEqual(self.runtime_path.read_bytes(), self.before_runtime)
        self.assertEqual(self.manifest_path.read_bytes(), self.before_manifest)
        self.assertFalse(self.job.exists())

    def test_repair_keeps_all_identity_fields_and_links_then_is_idempotent(self):
        result = self.execute()
        self.assertIn(result["status"], SUCCESS, result)
        self.assert_preserved_except_tuning()
        self.assertEqual(self.adapter.restart_count, 1)
        self.assertGreaterEqual(len(self.adapter.validations), 2)
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        expected = copy.deepcopy(self.manifest)
        stream = json.loads(expected[0]["payload"]["streamSettings"])
        stream["sockopt"] = FIXED
        expected[0]["payload"]["streamSettings"] = json.dumps(stream, separators=(",", ":"))
        self.assertEqual(manifest[0]["clientOutbound"], expected[0]["clientOutbound"])
        self.assertEqual(manifest[0]["link"], expected[0]["link"])
        self.assertEqual(json.loads(manifest[0]["payload"]["streamSettings"]), stream)
        second = self.execute(job=self.info / "second-job")
        self.assertIn(second["status"], NO_CHANGE | SUCCESS, second)
        self.assertEqual(self.adapter.restart_count, 1)
        backups = list(self.job.rglob("*.db"))
        self.assertTrue(backups, "a durable database backup is required before mutation")
        with closing(sqlite3.connect(backups[0])) as connection:
            old = json.loads(connection.execute("SELECT stream_settings FROM inbounds WHERE id=1").fetchone()[0])
            self.assertEqual(old["sockopt"], OLD)

    def test_explicit_id_targets_only_requested_custom_node(self):
        result = self.execute(inbound_id=2)
        self.assertIn(result["status"], SUCCESS, result)
        after = self.rows()
        for ident in self.before_rows:
            if ident != 2:
                self.assertEqual(after[ident], self.before_rows[ident])
        self.assertEqual(json.loads(after[2]["stream_settings"])["sockopt"], FIXED)

    def test_custom_values_and_non_tcp_protocols_are_not_changed(self):
        self.update_stream(1, lambda value: value.update(sockopt={
            "tcpWindowClamp": "600", "tcpMaxSeg": "1440", "tcpUserTimeout": "10000",
            "tcpcongestion": "cubic"}))
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("UPDATE inbounds SET protocol='hysteria' WHERE id=5")
            connection.commit()
        plan = repair.make_plan(self.database, self.manifest_path)
        self.assertEqual({row["id"] for row in plan["rows"]}, {3})
        custom = repair.make_plan(self.database, self.manifest_path, inbound_id=4)
        self.assertEqual(custom["rows"], [])

    def test_candidate_rejection_changes_nothing_and_does_not_restart(self):
        def reject(path, count):
            raise RuntimeError("fixture candidate rejected")
        self.adapter.validation_hook = reject
        result = self.execute()
        self.assertIn(result["status"], FAILURE, result)
        self.assertEqual(self.rows(), self.before_rows)
        self.assertEqual(self.manifest_path.read_bytes(), self.before_manifest)
        self.assertEqual(self.adapter.restart_count, 0)


    def test_panel_default_ipv4_listener_is_accepted_without_changing_database_listen(self):
        runtime=json.loads(self.runtime_path.read_text())
        for entry in runtime['inbounds']:
            if entry['listen']=='': entry['listen']='0.0.0.0'
        self.runtime_path.write_text(json.dumps(runtime))
        result=self.execute()
        self.assertEqual(result['status'],'complete',result)
        self.assert_preserved_except_tuning()

    def test_unexpected_runtime_listener_address_aborts_before_mutation(self):
        for address in ('127.0.0.1','::'):
            with self.subTest(address=address):
                runtime=json.loads(self.runtime_path.read_text())
                runtime['inbounds'][0]['listen']=address
                self.runtime_path.write_text(json.dumps(runtime))
                result=self.execute(job=self.info/('rejected-address-'+str(len(address))))
                self.assertEqual(result['status'],'failed',result)
                self.assertEqual(self.rows(),self.before_rows)
                self.assertEqual(self.adapter.restart_count,0)

    def test_service_must_be_active_before_mutation(self):
        self.adapter.active = False
        result = self.execute()
        self.assertIn(result["status"], FAILURE, result)
        self.assertEqual(self.rows(), self.before_rows)
        self.assertEqual(self.adapter.restart_count, 0)

    def test_restart_failure_rolls_back_tuning_and_manifest(self):
        def fail_once(count):
            if count == 1:
                raise RuntimeError("fixture restart failed")
        self.adapter.restart_hook = fail_once
        result = self.execute()
        self.assertIn(result["status"], FAILURE, result)
        self.assertEqual(self.rows(), self.before_rows)
        self.assertEqual(self.manifest_path.read_bytes(), self.before_manifest)
        self.assertEqual(self.links.read_bytes(), b"preserve-exact-import-links\n")
        self.assertGreaterEqual(self.adapter.restart_count, 2)

    def test_missing_listeners_after_restart_triggers_rollback(self):
        self.adapter.listener_hook = lambda: set() if self.adapter.restart_count == 1 else {
            row["port"] for row in self.rows().values() if row["enable"]}
        result = self.execute()
        self.assertIn(result["status"], FAILURE, result)
        self.assertEqual(self.rows(), self.before_rows)
        self.assertEqual(self.manifest_path.read_bytes(), self.before_manifest)

    def test_runtime_config_validation_failure_triggers_rollback(self):
        def reject_generated(path, count):
            if count == 2:
                raise RuntimeError("fixture generated runtime rejected")
        self.adapter.validation_hook = reject_generated
        result = self.execute()
        self.assertIn(result["status"], FAILURE, result)
        self.assertEqual(self.rows(), self.before_rows)
        self.assertEqual(self.manifest_path.read_bytes(), self.before_manifest)

    def test_target_cas_conflict_has_no_partial_database_commit(self):
        def concurrent(path, count):
            if count == 1:
                self.update_stream(3, lambda value: value.update(userConcurrent="preserve-this"))
        self.adapter.validation_hook = concurrent
        result = self.execute()
        self.assertIn(result["status"], FAILURE, result)
        after = self.rows()
        self.assertEqual(after[1], self.before_rows[1])
        self.assertEqual(after[5], self.before_rows[5])
        self.assertEqual(json.loads(after[3]["stream_settings"])["userConcurrent"], "preserve-this")
        self.assertEqual(json.loads(after[3]["stream_settings"])["sockopt"], OLD)
        self.assertEqual(self.adapter.restart_count, 0)

    def test_rollback_does_not_overwrite_concurrent_target_change(self):
        def concurrent_and_fail(count):
            if count == 1:
                self.update_stream(1, lambda value: value.update(userConcurrent="retain"))
                raise RuntimeError("fixture restart failed after user edit")
        self.adapter.restart_hook = concurrent_and_fail
        result = self.execute()
        self.assertEqual(result["status"], "manual_recovery_required", result)
        after = self.rows()
        self.assertEqual(json.loads(after[1]["stream_settings"])["userConcurrent"], "retain")
        self.assertEqual(json.loads(after[1]["stream_settings"])["sockopt"], FIXED)
        self.assertEqual(after[3], self.before_rows[3])
        self.assertEqual(after[5], self.before_rows[5])

    def test_rollback_preserves_unrelated_user_edit(self):
        def concurrent_and_fail(count):
            if count == 1:
                with closing(sqlite3.connect(self.database)) as connection:
                    connection.execute("UPDATE inbounds SET remark='User edited concurrently' WHERE id=2")
                    connection.commit()
                raise RuntimeError("fixture restart failed")
        self.adapter.restart_hook = concurrent_and_fail
        result = self.execute()
        self.assertIn(result["status"], FAILURE, result)
        after = self.rows()
        self.assertEqual(after[2]["remark"], "User edited concurrently")
        for ident in (1, 3, 4, 5):
            self.assertEqual(after[ident], self.before_rows[ident])

    def test_manifest_concurrent_edit_is_not_overwritten(self):
        def concurrent(path, count):
            if count == 1:
                self.manifest_path.write_bytes(b'{"user_changed_file":true}')
        self.adapter.validation_hook = concurrent
        result = self.execute()
        self.assertIn(result["status"], FAILURE, result)
        self.assertEqual(self.manifest_path.read_bytes(), b'{"user_changed_file":true}')
        self.assertEqual(self.rows(), self.before_rows)
        self.assertEqual(self.adapter.restart_count, 0)


@unittest.skipUnless(os.name == "posix", "detached workers require POSIX locks and sessions")
class DetachedPerformanceRepair(unittest.TestCase):
    def setUp(self):
        self.fixture = PerformanceRepair("test_repair_keeps_all_identity_fields_and_links_then_is_idempotent")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bin = self.fixture.root / "shim-bin"
        self.bin.mkdir()
        self.release = self.fixture.root / "allow-validation"
        self.original_env = {key: os.environ.get(key) for key in
                             ("PATH", "REPAIR_FIXTURE_DATABASE", "REPAIR_FIXTURE_RUNTIME", "REPAIR_FIXTURE_RELEASE")}
        os.environ.update(PATH=str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                          REPAIR_FIXTURE_DATABASE=str(self.fixture.database),
                          REPAIR_FIXTURE_RUNTIME=str(self.fixture.runtime_path),
                          REPAIR_FIXTURE_RELEASE=str(self.release))
        self.addCleanup(self.restore_env)
        self.write_shim("rc-service", r"""
import json, os, sqlite3, sys
from pathlib import Path
if 'restart' in sys.argv:
    runtime_path=Path(os.environ['REPAIR_FIXTURE_RUNTIME'])
    runtime=json.loads(runtime_path.read_text())
    runtime['inbounds']=[]
    connection=sqlite3.connect(os.environ['REPAIR_FIXTURE_DATABASE'])
    connection.row_factory=sqlite3.Row
    for row in connection.execute('SELECT * FROM inbounds ORDER BY id'):
        if row['enable']:
            runtime['inbounds'].append({'tag':row['tag'],'port':row['port'],'protocol':row['protocol'],
                'listen':row['listen'],'settings':json.loads(row['settings']),
                'streamSettings':json.loads(row['stream_settings']),'sniffing':json.loads(row['sniffing'])})
    connection.close()
    runtime_path.write_text(json.dumps(runtime))
""")
        self.write_shim("systemctl", (self.bin / "rc-service").read_text().split("\n", 1)[1])
        self.write_shim("ss", r"""
import json, os
runtime=json.load(open(os.environ['REPAIR_FIXTURE_RUNTIME']))
for inbound in runtime['inbounds']:
    print('tcp LISTEN 0 128 0.0.0.0:%d 0.0.0.0:* users:(("xray",pid=99,fd=3))' % inbound['port'])
""")
        xray=self.write_shim("xray", r"""
import json, os, sys, time
from pathlib import Path
path=Path(sys.argv[-1])
if path.name=='candidate.json':
    deadline=time.monotonic()+20
    while not Path(os.environ['REPAIR_FIXTURE_RELEASE']).exists():
        if time.monotonic()>deadline: raise SystemExit(1)
        time.sleep(.05)
json.loads(path.read_text())
""")
        self.config=dict(self.fixture.config,xray=str(xray),verify_attempts=10,verify_delay=.05)

    def write_shim(self, name, code):
        path=self.bin/name
        path.write_text("#!"+sys.executable+"\n"+code.lstrip(),encoding="utf-8")
        path.chmod(0o755)
        return path

    def restore_env(self):
        for key,value in self.original_env.items():
            if value is None: os.environ.pop(key,None)
            else: os.environ[key]=value

    def test_detached_worker_survives_download_removal_and_holds_lock_until_complete(self):
        download=self.fixture.root/"downloaded-helper.py"
        download.write_bytes(Path(repair.__file__).read_bytes())
        spec=importlib.util.spec_from_file_location("downloaded_performance_repair",download)
        helper=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        processes=[]
        create=helper.subprocess.Popen
        def retain_process(*args,**kwargs):
            process=create(*args,**kwargs)
            processes.append(process)
            return process
        with mock.patch.object(helper.subprocess,'Popen',side_effect=retain_process):
            launched=helper.launch_repair(self.config)
        self.addCleanup(lambda: [process.wait(timeout=25) for process in processes])
        self.addCleanup(self.release.touch)
        self.assertEqual(launched['status'],'started',launched)
        download.unlink()
        self.assertEqual(helper.launch_repair(self.config)['status'],'busy')
        state=helper.read_latest(self.config)
        self.assertIn(state['status'],{'starting','running'})
        self.release.touch()
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            state=helper.read_latest(self.config)
            if state['status'] not in {'starting','running'}: break
            time.sleep(.05)
        self.assertEqual(state['status'],'complete',state)
        self.fixture.assert_preserved_except_tuning()
        self.assertEqual(helper.launch_repair(self.config)['status'],'no_changes')
        job=Path(launched['backup_directory'])
        self.assertTrue((job/'worker.py').is_file())
        self.assertTrue((job/'x-ui-before.db').is_file())
        self.assertEqual(processes[0].wait(timeout=5),0)

    def test_orphaned_status_is_read_only_and_cannot_launch_another_repair(self):
        root=self.fixture.info/repair.ROOT_NAME
        job=root/'job-20261029T010203Z-123456abcdef'
        job.mkdir(parents=True)
        repair.atomic_json(root/'latest.json',{'job':job.name})
        repair.write_state(job,'running',nodes=[{'id':1}])
        state_before=(job/'state.json').read_bytes()
        database_before=self.fixture.database.read_bytes()
        for with_lock_file in (False,True):
            with self.subTest(with_lock_file=with_lock_file):
                if with_lock_file: (root/'repair.lock').touch()
                status=repair.read_latest(self.config)
                self.assertEqual(status['status'],'manual_recovery_required',status)
                self.assertEqual(status['error'],'worker_interrupted')
                self.assertEqual((job/'state.json').read_bytes(),state_before)
        launched=repair.launch_repair(self.config)
        self.assertEqual(launched['status'],'manual_recovery_required',launched)
        self.assertEqual(self.fixture.database.read_bytes(),database_before)
        self.assertEqual([path for path in root.iterdir() if path.is_dir()],[job])


if __name__ == "__main__":
    unittest.main()
