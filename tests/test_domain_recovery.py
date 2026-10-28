"""Exercise domain migration rollback against a real temporary SQLite database."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from contextlib import closing

from test_installer import REPO, FIXTURE, generated_profiles, run_bash, bash_directory


class DomainRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.profiles = generated_profiles()

    def run_failure(self, failure):
        with tempfile.TemporaryDirectory(prefix="domain-recovery-", dir=REPO) as tmp:
            directory = Path(tmp)
            (directory / "server").mkdir()
            (directory / "run").mkdir()
            info = directory / "info"
            info.mkdir()
            original_files = {
                "3x-ui-node-profiles.json": json.dumps(self.profiles),
                "3x-ui-nodes-info.txt": "访问地址：https://203.0.113.42:20530/fixturepath/\n管理员账号：admin\n管理员密码：fixture-secret\n",
                "3x-ui-node-links.txt": "old-links\n",
                "3x-ui-xhttp-info.txt": "legacy-old\n",
            }
            for name, data in original_files.items():
                (info / name).write_text(data, encoding="utf-8")
            database = directory / "x-ui.db"
            with closing(sqlite3.connect(database)) as con:
                con.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY,tag TEXT,protocol TEXT,remark TEXT,stream_settings TEXT)")
                for index, item in enumerate(self.profiles, 1):
                    con.execute("INSERT INTO inbounds VALUES (?,?,?,?,?)", (index,item["payload"]["tag"],item["payload"]["protocol"],item["remark"],item["payload"]["streamSettings"]))
                con.execute("CREATE TABLE settings (id INTEGER PRIMARY KEY,key TEXT UNIQUE,value TEXT)")
                con.executemany("INSERT INTO settings (key,value) VALUES (?,?)", [("webCertFile","/old/cert.pem"),("webKeyFile","/old/key.pem"),("unrelated","before")])
                if failure == "update":
                    con.execute("CREATE TRIGGER fail_second BEFORE UPDATE ON inbounds WHEN old.id=2 AND json_extract(new.stream_settings,'$.tlsSettings.serverName')='new.example.test' BEGIN SELECT RAISE(FAIL,'injected update failure'); END")
                con.commit()
            source = FIXTURE + r'''
RUN_DIR="$TEST_MOCK_DIR/run"
XUI_DB_PATH="$TEST_MOCK_DIR/x-ui.db"
XUI_INFO_DIR="$TEST_MOCK_DIR/info"
XUI_INSTALL_DIR="$TEST_MOCK_DIR/server"
export TEST_MOCK_DIR XUI_DB_PATH
install() {
    if [[ "$1" == -d && "$2" == -m && "$3" == 700 ]]; then mkdir -p "$4"; else command install "$@"; fi
}
sqlite3() {
    if command -v cygpath >/dev/null 2>&1 && [[ "${2:-}" == .backup* ]]; then
        local target
        target=$(printf '%s' "$2" | cut -d"'" -f2)
        command sqlite3 "$1" ".backup '$(cygpath -m "$target")'"
    else command sqlite3 "$@"; fi
}
jq() { command jq "$@" | tr -d '\r'; }
cat > "$XUI_INSTALL_DIR/x-ui" <<'MOCK_XUI'
#!/usr/bin/env bash
sqlite3 "$XUI_DB_PATH" "UPDATE settings SET value='$3' WHERE key='webCertFile'; UPDATE settings SET value='$5' WHERE key='webKeyFile';"
MOCK_XUI
chmod +x "$XUI_INSTALL_DIR/x-ui"
install_deps() { :; }
detect_server_ip() { SERVER_IP=203.0.113.42; }
issue_domain_cert() {
    CERT_HOST="$1"; CERT_DIR="$TEST_MOCK_DIR/newcert"
    CERT_FILE="$CERT_DIR/fullchain.pem"; KEY_FILE="$CERT_DIR/privkey.pem"
    mkdir -p "$CERT_DIR"; printf cert >"$CERT_FILE"; printf key >"$KEY_FILE"
}
restart_panel() {
    printf 'restart\n' >> "$TEST_MOCK_DIR/events"
    sqlite3 "$XUI_DB_PATH" "UPDATE settings SET value='concurrent' WHERE key='unrelated';"
    if [[ "$TEST_FAILURE" == restart && ! -e "$TEST_MOCK_DIR/restart-failed" ]]; then
        : >"$TEST_MOCK_DIR/restart-failed"; return 1
    fi
}
verify_running_nodes() { [[ "$TEST_FAILURE" != verify ]]; }
write_node_files() {
    printf 'partial-new-file' >"$XUI_INFO_DIR/3x-ui-node-profiles.json"
    [[ "$TEST_FAILURE" != write ]]
}
if switch_existing_to_domain new.example.test; then
    printf 'UNEXPECTED_SUCCESS\n'; exit 9
else printf 'EXPECTED_FAILURE\n'; fi
'''
            result = run_bash(source, extra_env={"TEST_MOCK_DIR":bash_directory(directory),"TEST_FAILURE":failure})
            self.assertEqual(result.returncode,0,result.stderr[-2000:])
            self.assertIn("EXPECTED_FAILURE",result.stdout)
            with closing(sqlite3.connect(database)) as con:
                rows = dict(con.execute("SELECT tag,stream_settings FROM inbounds"))
                settings = dict(con.execute("SELECT key,value FROM settings"))
            for item in self.profiles:
                self.assertEqual(rows[item["payload"]["tag"]],item["payload"]["streamSettings"])
            self.assertEqual(settings["webCertFile"],"/old/cert.pem")
            self.assertEqual(settings["webKeyFile"],"/old/key.pem")
            self.assertEqual(settings["unrelated"],"concurrent")
            for name, data in original_files.items():
                self.assertEqual((info/name).read_text(encoding="utf-8"),data)

    def test_mid_update_restores_prior_rows_and_panel_settings(self):
        self.run_failure("update")

    def test_restart_failure_restores_configuration(self):
        self.run_failure("restart")

    def test_runtime_verification_failure_restores_configuration(self):
        self.run_failure("verify")

    def test_output_write_failure_restores_files_and_configuration(self):
        self.run_failure("write")


if __name__ == "__main__":
    unittest.main()
