"""Existing-panel fixtures: preserve the live XHTTP node while adding profiles."""
from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from test_installer import REPO, bash_directory, run_bash


ORIGINAL_ID = 101
ORIGINAL_SETTINGS = {"clients": [{"id": "00000000-0000-4000-8000-000000000001",
                                   "flow": "xtls-rprx-vision"}], "decryption": "legacy-fixture"}


API_HELPER = r'''
import json,os,sqlite3,sys
from pathlib import Path
root=Path(os.environ["TEST_NATIVE_DIR"])
method,path=sys.argv[1:3]
payload=sys.stdin.read()
with sqlite3.connect(root/"x-ui.db") as con:
    con.row_factory=sqlite3.Row
    with (root/"api-events").open("a") as log: log.write(method+" "+path+"\n")
    if method=="POST" and path=="/panel/api/inbounds/add":
        p=json.loads(payload)
        count=con.execute("SELECT count(*) FROM inbounds WHERE id!=101").fetchone()[0]+1
        cur=con.execute("INSERT INTO inbounds(tag,protocol,remark,port,settings,stream_settings) VALUES (?,?,?,?,?,?)",
                        (p["tag"],p["protocol"],p["remark"],p["port"],p["settings"],p["streamSettings"]))
        con.commit()
        if str(count)==os.environ.get("TEST_FAIL_ADD"):
            print(json.dumps({"success":False}))
        else: print(json.dumps({"success":True,"obj":{"id":cur.lastrowid}}))
    elif method=="GET" and path=="/panel/api/inbounds/list":
        print(json.dumps({"success":True,"obj":[dict(x) for x in con.execute("SELECT * FROM inbounds")]}))
    elif method=="GET" and path=="/panel/api/server/getConfigJson":
        rows=[]
        for r in con.execute("SELECT * FROM inbounds"):
            rows.append({"tag":r["tag"],"port":r["port"],"protocol":r["protocol"],
                         "settings":json.loads(r["settings"]),"streamSettings":json.loads(r["stream_settings"])})
        print(json.dumps({"success":True,"obj":{"inbounds":rows}}))
    elif method=="POST" and path=="/panel/api/server/restartXrayService":
        candidate=root/"run/xray-candidate.json"
        if candidate.exists(): (root/"server/bin/config.json").write_bytes(candidate.read_bytes())
        print(json.dumps({"success":True}))
    elif method=="POST" and path.startswith("/panel/api/inbounds/del/"):
        ident=int(path.rsplit("/",1)[1])
        con.execute("DELETE FROM inbounds WHERE id=?",(ident,));con.commit()
        print(json.dumps({"success":True}))
    else: raise SystemExit("Unexpected fixture API operation")
'''


SHELL_FIXTURE = r'''
source ./install.sh
RUN_DIR="$TEST_MOCK_DIR/run"
XUI_DB_PATH="$TEST_MOCK_DIR/x-ui.db"
XUI_INFO_DIR="$TEST_MOCK_DIR/info"
XUI_INSTALL_DIR="$TEST_MOCK_DIR/server"
XRAY_BIN="$TEST_MOCK_DIR/fake-xray"
AUTO_YES=1
PRIMARY_NODE=hy2
ARCH=amd64
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
# Real certificate verification is covered by test_certificates.py. This
# fixture isolates preservation, API failure propagation and port selection.
validate_certificate_pair() { :; }
install_deps() { :; }
detect_server_ip() { SERVER_IP=203.0.113.42; }
verify_reality_target() { :; }
resolve_xray_binary() { :; }
generate_vlessenc_keys() {
    printf 'keys\n' >> "$TEST_MOCK_DIR/key-events"
    VLESS_DECRYPTION=fixture-decryption
    VLESS_ENCRYPTION=fixture-encryption
    VLESS_AUTH_LABEL=X25519
}
generate_reality_keys() {
    REALITY_PRIVATE_KEY=fixture-private
    REALITY_PUBLIC_KEY=fixture-public
    REALITY_SHORT_ID=0123456789abcdef
}
ss() {
    case "$*" in
        *-lntp*|*-lnup*) printf 'LISTEN 0 4096 0.0.0.0:* users:(("xray",pid=7,fd=3))\n' ;;
        *':443'|*':20530') printf 'LISTEN 0 4096 0.0.0.0:* users:(("xray",pid=7,fd=3))\n' ;;
        *) : ;;
    esac
}
sleep() { :; }
api_init() { API_READY=1; printf 'auth\n' >> "$TEST_MOCK_DIR/auth-events"; }
api_request() {
    printf '%s' "${3:-}" | MSYS2_ARG_CONV_EXCL=/panel/api "$TEST_PYTHON" "$TEST_MOCK_DIR/api.py" "$1" "$2"
}
cat > "$XRAY_BIN" <<'FAKE_XRAY'
#!/usr/bin/env bash
[[ "$1" == run && "$2" == -test && "$3" == -c ]] || exit 9
jq -e '.inbounds | length == 5' "$4" >/dev/null
FAKE_XRAY
chmod +x "$XRAY_BIN"
'''


class ExistingInstallation(unittest.TestCase):
    def fixture(self, directory: Path) -> str:
        for name in ("run", "info", "server/bin", "cert"):
            (directory/name).mkdir(parents=True, exist_ok=True)
        certificate=directory/"cert/cert.pem"
        certificate.write_text("fixture certificate",encoding="utf-8")
        private_key=directory/"cert/key.pem"
        private_key.write_text("fixture key",encoding="utf-8")
        # Persist native forward-slash paths. Git Bash's native jq invocation
        # rewrites /c/... arguments to C:/...; using the actual platform path
        # keeps fixture data stable while the shell harness uses bash_directory.
        certificate_files={"certificateFile":certificate.as_posix(),
                           "keyFile":private_key.as_posix()}
        old_stream={"network":"xhttp","security":"tls","xhttpSettings":{"path":"/old"},
                    "tlsSettings":{"serverName":"existing.example.test","minVersion":"1.2","maxVersion":"1.3",
                                   "certificates":[certificate_files]}}
        runtime={"inbounds":[{"tag":"original-xhttp","port":443,"protocol":"vless",
                               "settings":ORIGINAL_SETTINGS,"streamSettings":old_stream}]}
        (directory/"server/bin/config.json").write_text(json.dumps(runtime),encoding="utf-8")
        with closing(sqlite3.connect(directory/"x-ui.db")) as con:
            con.executescript("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);"
                              "CREATE TABLE inbounds(id INTEGER PRIMARY KEY,tag TEXT,protocol TEXT,remark TEXT,"
                              "port INTEGER,settings TEXT,stream_settings TEXT);")
            con.executemany("INSERT INTO settings VALUES (?,?)",[("webPort","20530"),("webBasePath","/fixturepath/"),
                             ("webListen",""),("webCertFile",certificate_files["certificateFile"]),
                             ("webKeyFile",certificate_files["keyFile"])])
            con.execute("INSERT INTO inbounds VALUES (?,?,?,?,?,?,?)",(ORIGINAL_ID,"original-xhttp","vless",
                         "VLESS-XHTTP-Enc-Vision",443,json.dumps(ORIGINAL_SETTINGS),json.dumps(old_stream)))
            con.commit()
        (directory/"api.py").write_text(API_HELPER,encoding="utf-8")
        legacy="访问地址：https://existing.example.test:20530/fixturepath/\n管理员账号：fixture-admin\n管理员密码：fixture-password\n"
        (directory/"info/3x-ui-xhttp-info.txt").write_text(legacy,encoding="utf-8")
        return json.dumps(old_stream)

    def env(self, directory: Path, **extra: str) -> dict[str,str]:
        import sys
        return {"TEST_MOCK_DIR":bash_directory(directory),"TEST_NATIVE_DIR":str(directory),
                "TEST_PYTHON":sys.executable,**extra}

    def rows(self, directory: Path):
        with closing(sqlite3.connect(directory/"x-ui.db")) as con:
            return con.execute("SELECT id,tag,port,settings,stream_settings FROM inbounds ORDER BY id").fetchall()

    def test_add_profiles_preserves_original_node_and_reuses_certificate(self):
        with tempfile.TemporaryDirectory(prefix="installer-existing-",dir=REPO) as temp:
            directory=Path(temp)
            original=self.fixture(directory)
            result=run_bash(SHELL_FIXTURE+"\nadd_nodes_to_existing\n",extra_env=self.env(directory))
            self.assertEqual(result.returncode,0,result.stderr[-1500:])
            rows=self.rows(directory)
            self.assertEqual(len(rows),5)
            self.assertEqual(rows[0],(ORIGINAL_ID,"original-xhttp",443,json.dumps(ORIGINAL_SETTINGS),original))
            profiles=json.loads((directory/"info/3x-ui-node-profiles.json").read_text(encoding="utf-8"))
            by_key={p["key"]:p for p in profiles}
            self.assertEqual(set(by_key),{"hy2","raw","reality","xhttp"})
            self.assertEqual(by_key["raw"]["port"],24445)
            self.assertNotEqual(by_key["xhttp"]["port"],443)
            for p in profiles:
                stream=json.loads(p["payload"]["streamSettings"])
                if p["security"]=="tls":
                    self.assertEqual(stream["tlsSettings"]["serverName"],"existing.example.test")
                    self.assertEqual(stream["tlsSettings"]["minVersion"],"1.0")
                    self.assertEqual(stream["tlsSettings"]["maxVersion"],"1.3")
                    certificate_files=stream["tlsSettings"]["certificates"][0]
                    self.assertEqual({key:certificate_files[key] for key in ("certificateFile","keyFile")},
                                     json.loads(original)["tlsSettings"]["certificates"][0])

    def test_repeat_add_reuses_managed_profiles_without_new_credentials(self):
        with tempfile.TemporaryDirectory(prefix="installer-existing-",dir=REPO) as temp:
            directory=Path(temp)
            self.fixture(directory)
            source=SHELL_FIXTURE+r'''
add_nodes_to_existing
cp "$XUI_INFO_DIR/3x-ui-node-profiles.json" "$TEST_MOCK_DIR/first-profiles.json"
add_nodes_to_existing
'''
            result=run_bash(source,extra_env=self.env(directory))
            self.assertEqual(result.returncode,0,result.stderr[-1500:])
            self.assertEqual(len(self.rows(directory)),5)
            events=(directory/"api-events").read_text().splitlines()
            self.assertEqual(events.count("POST /panel/api/inbounds/add"),4)
            self.assertEqual((directory/"key-events").read_text().splitlines(),["keys"])
            first=json.loads((directory/"first-profiles.json").read_text(encoding="utf-8"))
            after=json.loads((directory/"info/3x-ui-node-profiles.json").read_text(encoding="utf-8"))
            self.assertEqual(first,after)

    def test_partial_add_failure_rolls_back_only_new_profiles(self):
        with tempfile.TemporaryDirectory(prefix="installer-existing-",dir=REPO) as temp:
            directory=Path(temp)
            original=self.fixture(directory)
            source=SHELL_FIXTURE+r'''
if add_nodes_to_existing; then
  printf 'UNEXPECTED_SUCCESS\n'
else
  rollback_created_nodes
  printf 'EXPECTED_FAILURE\n'
fi
'''
            result=run_bash(source,extra_env=self.env(directory,TEST_FAIL_ADD="3"))
            self.assertEqual(result.returncode,0,result.stderr[-1500:])
            self.assertIn("EXPECTED_FAILURE",result.stdout)
            self.assertNotIn("UNEXPECTED_SUCCESS",result.stdout)
            self.assertEqual(self.rows(directory),[(ORIGINAL_ID,"original-xhttp",443,json.dumps(ORIGINAL_SETTINGS),original)])
            self.assertFalse((directory/"info/3x-ui-node-profiles.json").exists())

    def test_missing_final_profiles_never_duplicates_existing_managed_nodes(self):
        with tempfile.TemporaryDirectory(prefix="installer-existing-",dir=REPO) as temp:
            directory=Path(temp)
            self.fixture(directory)
            source=SHELL_FIXTURE+r'''
add_nodes_to_existing >/dev/null
rm "$XUI_INFO_DIR/3x-ui-node-profiles.json"
if add_nodes_to_existing; then
  printf 'UNEXPECTED_SUCCESS\n'
else
  printf 'INCOMPLETE_BATCH_REJECTED\n'
fi
'''
            result=run_bash(source,extra_env=self.env(directory))
            self.assertEqual(result.returncode,0,result.stderr[-1500:])
            self.assertIn("INCOMPLETE_BATCH_REJECTED",result.stdout)
            self.assertNotIn("UNEXPECTED_SUCCESS",result.stdout)
            self.assertEqual(len(self.rows(directory)),5)
            events=(directory/"api-events").read_text().splitlines()
            self.assertEqual(events.count("POST /panel/api/inbounds/add"),4)
            self.assertEqual((directory/"key-events").read_text().splitlines(),["keys"])
            self.assertFalse((directory/"info/3x-ui-node-profiles.json").exists())

    def test_failed_existing_verification_cannot_report_success_in_conditional(self):
        with tempfile.TemporaryDirectory(prefix="installer-existing-",dir=REPO) as temp:
            directory=Path(temp)
            self.fixture(directory)
            source=SHELL_FIXTURE+r'''
add_nodes_to_existing >/dev/null
verify_running_nodes() { return 1; }
print_summary() { printf 'UNEXPECTED_SUMMARY\n'; }
if add_nodes_to_existing; then
  printf 'UNEXPECTED_SUCCESS\n'
else
  printf 'EXPECTED_FAILURE\n'
fi
'''
            result=run_bash(source,extra_env=self.env(directory))
            self.assertEqual(result.returncode,0,result.stderr[-1500:])
            self.assertIn("EXPECTED_FAILURE",result.stdout)
            self.assertNotIn("UNEXPECTED_SUCCESS",result.stdout)
            self.assertNotIn("UNEXPECTED_SUMMARY",result.stdout)
            self.assertEqual(len(self.rows(directory)),5)
            events=(directory/"api-events").read_text().splitlines()
            self.assertEqual(events.count("POST /panel/api/inbounds/add"),4)

    def test_certificate_name_mismatch_stops_before_creating_nodes(self):
        with tempfile.TemporaryDirectory(prefix="installer-existing-",dir=REPO) as temp:
            directory=Path(temp)
            self.fixture(directory)
            source=SHELL_FIXTURE+r'''
validate_certificate_pair() { return 1; }
if add_nodes_to_existing; then
  printf 'UNEXPECTED_SUCCESS\n'
else
  printf 'EXPECTED_FAILURE\n'
fi
'''
            result=run_bash(source,extra_env=self.env(directory))
            self.assertEqual(result.returncode,0,result.stderr[-1500:])
            self.assertIn("EXPECTED_FAILURE",result.stdout)
            self.assertNotIn("UNEXPECTED_SUCCESS",result.stdout)
            self.assertIn("XUI_DOMAIN",result.stderr)
            self.assertEqual(len(self.rows(directory)),1)
            self.assertFalse((directory/"auth-events").exists())
            self.assertFalse((directory/"key-events").exists())


if __name__=="__main__":
    unittest.main()
