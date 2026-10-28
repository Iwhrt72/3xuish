"""Exercise generated installer profiles without running an installation."""
from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from urllib.parse import parse_qs, unquote, urlsplit


REPO = Path(__file__).resolve().parents[1]
GIT_BASH = Path(r"C:\Program Files\Git\bin\bash.exe")

FIXTURE = r'''
source ./install.sh
SERVER_IP=203.0.113.42
CERT_HOST=example.test
CERT_FILE=/root/cert/example.test/fullchain.pem
KEY_FILE=/root/cert/example.test/privkey.pem
PANEL_PORT=20530
WEB_BASE_PATH=fixturepath
RAW_PORT=443
REALITY_PORT=24443
HY2_PORT=24444
XHTTP_PORT=8443
XHTTP_PATH='/a path/?x=1&y=2'
CERT_MODE=2
DOMAIN_NAME=example.test
VLESS_ENC_TYPE=1
INBOUND_TLS_MODE=1
VLESS_DECRYPTION=fixture-decryption-data
VLESS_ENCRYPTION='fixture.encryption+data/encoded'
VLESS_AUTH_LABEL=X25519
REALITY_PRIVATE_KEY=fixture-reality-private
REALITY_PUBLIC_KEY=fixture-reality-public
REALITY_SHORT_ID=0123456789abcdef
REALITY_SERVER_NAME=www.microsoft.com
REALITY_TARGET=www.microsoft.com:443
PRIMARY_NODE=hy2
'''


def bash_binary() -> str:
    if os.environ.get("TEST_BASH"):
        return os.environ["TEST_BASH"]
    if os.name == "nt" and GIT_BASH.is_file():
        return str(GIT_BASH)
    binary = shutil.which("bash")
    if binary is None:
        raise RuntimeError("Bash is required")
    return binary


def run_bash(source: str, *, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    if extra_env:
        environment.update(extra_env)
    return subprocess.run(
        [bash_binary(), "-lc", source], cwd=REPO, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=45, check=False,
        env=environment,
    )


def generated_profiles(primary: str = "hy2") -> list[dict]:
    result = run_bash(
        FIXTURE + f"\nPRIMARY_NODE={primary}\nprepare_node_profiles\n"
        + 'printf "%s\\n" "$NODE_PROFILES"\n'
    )
    if result.returncode:
        raise AssertionError(f"generation failed: {result.stderr[-1000:]}")
    return json.loads(result.stdout.strip())


class InstallerProfiles(unittest.TestCase):
    def test_sourcing_does_not_start_installation(self) -> None:
        result = run_bash("source ./install.sh\nprintf 'SOURCE_OK\\n'\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "SOURCE_OK")

    def test_four_profiles_and_configuration_invariants(self) -> None:
        items = generated_profiles()
        self.assertEqual([p["key"] for p in items], ["hy2", "raw", "reality", "xhttp"])
        self.assertEqual(len({p["payload"]["tag"] for p in items}), 4)
        self.assertEqual(len({p["port"] for p in items}), 4)
        by_key = {p["key"]: p for p in items}
        for item in items:
            with self.subTest(profile=item["key"]):
                payload = item["payload"]
                settings = json.loads(payload["settings"])
                stream = json.loads(payload["streamSettings"])
                self.assertEqual(payload["port"], item["port"])
                self.assertNotIn("fallbacks", settings)
                self.assertEqual(stream["security"], item["security"])
                self.assertEqual(item["clientOutbound"]["settings"]["port"], item["port"])
                self.assertTrue(item["link"])
                if stream["security"] == "tls":
                    self.assertEqual(stream["tlsSettings"]["minVersion"], "1.0")
                    self.assertEqual(stream["tlsSettings"]["maxVersion"], "1.3")
                if stream["network"] != "hysteria":
                    sockopt = stream.get("sockopt", {})
                    self.assertEqual(sockopt.get("tcpWindowClamp"), 0)
                    for key in ("tcpMaxSeg", "tcpUserTimeout", "tcpcongestion"):
                        self.assertNotIn(key, sockopt)
        hy2_settings = json.loads(by_key["hy2"]["payload"]["settings"])
        hy2_stream = json.loads(by_key["hy2"]["payload"]["streamSettings"])
        self.assertEqual(by_key["hy2"]["payload"]["protocol"], "hysteria")
        self.assertEqual(hy2_settings["version"], 2)
        self.assertEqual(
            hy2_settings["clients"][0]["auth"],
            by_key["hy2"]["clientOutbound"]["streamSettings"]["hysteriaSettings"]["auth"],
        )
        self.assertEqual(hy2_stream["tlsSettings"]["alpn"], ["h3"])
        self.assertEqual(hy2_stream["finalmask"]["quicParams"]["congestion"], "bbr")
        self.assertEqual(json.loads(by_key["raw"]["payload"]["settings"])["decryption"], "none")
        self.assertEqual(json.loads(by_key["raw"]["payload"]["streamSettings"])["network"], "tcp")
        self.assertEqual(
            json.loads(by_key["reality"]["payload"]["streamSettings"])["realitySettings"]["target"],
            "www.microsoft.com:443",
        )
        self.assertEqual(
            json.loads(by_key["xhttp"]["payload"]["settings"])["decryption"],
            "fixture-decryption-data",
        )

    def test_primary_choice_keeps_all_nodes(self) -> None:
        for primary in ("hy2", "raw", "reality", "xhttp"):
            with self.subTest(primary=primary):
                items = generated_profiles(primary)
                self.assertEqual(items[0]["key"], primary)
                self.assertEqual({p["key"] for p in items}, {"hy2", "raw", "reality", "xhttp"})
                self.assertEqual([p["key"] for p in items if p["primary"]], [primary])

    def test_links_encode_fields_and_domain_switch_preserves_auth(self) -> None:
        by_key = {p["key"]: p for p in generated_profiles()}
        hy2 = urlsplit(by_key["hy2"]["link"])
        self.assertEqual((hy2.scheme, hy2.hostname, hy2.port),
                         ("hysteria2", "203.0.113.42", 24444))
        self.assertEqual(parse_qs(hy2.query)["sni"], ["example.test"])
        self.assertEqual(unquote(hy2.username),
                         json.loads(by_key["hy2"]["payload"]["settings"])["clients"][0]["auth"])
        reality = parse_qs(urlsplit(by_key["reality"]["link"]).query)
        self.assertEqual(reality["security"], ["reality"])
        self.assertEqual(reality["pbk"], ["fixture-reality-public"])
        self.assertEqual(reality["sid"], ["0123456789abcdef"])
        xhttp = parse_qs(urlsplit(by_key["xhttp"]["link"]).query)
        self.assertEqual(xhttp["path"], ["/a path/?x=1&y=2"])
        self.assertEqual(xhttp["encryption"], ["fixture.encryption+data/encoded"])
        switched = json.loads(json.dumps(by_key["hy2"]))
        switched["clientOutbound"]["settings"]["address"] = "new.example.test"
        switched["clientOutbound"]["streamSettings"]["tlsSettings"]["serverName"] = "new.example.test"
        result = run_bash(FIXTURE + "\nprofile_share_link '" + json.dumps(switched) + "'\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        new = urlsplit(result.stdout.strip())
        self.assertEqual(new.hostname, "new.example.test")
        self.assertEqual(parse_qs(new.query)["sni"], ["new.example.test"])
        self.assertEqual(unquote(new.username), unquote(hy2.username))

    def test_invalid_settings_rejected_before_install(self) -> None:
        cases = {
            "primary": "PRIMARY_NODE=unknown", "zero": "RAW_PORT=0",
            "overflow": "HY2_PORT=65536", "tcp collision": "REALITY_PORT=443",
            "panel collision": "PANEL_PORT=8443", "acme port": "RAW_PORT=80",
            "domain": "DOMAIN_NAME=''",
        }
        for name, change in cases.items():
            with self.subTest(name=name):
                result = run_bash(FIXTURE + "\n" + change + "\nvalidate_config\n")
                self.assertNotEqual(result.returncode, 0, result.stdout)
        allowed = run_bash(FIXTURE + "\nHY2_PORT=443\nvalidate_config\n")
        self.assertEqual(allowed.returncode, 0, allowed.stderr)


def bash_directory(directory: Path) -> str:
    if os.name == "nt":
        return "/" + directory.drive[0].lower() + directory.as_posix()[2:]
    return directory.as_posix()


API_MOCK = r'''
mkdir -p "$TEST_MOCK_DIR/server/bin"
RUN_DIR="$TEST_MOCK_DIR"
XUI_INSTALL_DIR="$TEST_MOCK_DIR/server"
XRAY_BIN="$TEST_MOCK_DIR/fake-xray"
API_READY=1
cat > "$XRAY_BIN" <<'FAKE_XRAY'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TEST_MOCK_DIR/xray-invocations"
[[ "$1" == run && "$2" == -test && "$3" == -c ]] || exit 9
jq -e '.inbounds | length == 4' "$4" >/dev/null
FAKE_XRAY
chmod +x "$XRAY_BIN"
ss() {
    printf 'LISTEN 0 4096 0.0.0.0:* users:(("xray",pid=7,fd=3))\n'
}
sleep() { :; }
api_request() {
    printf '%s %s\n' "$1" "$2" >> "$TEST_MOCK_DIR/requests"
    case "$1 $2" in
        'POST /panel/api/inbounds/add')
            jq -r '.tag' <<<"$3" >> "$TEST_MOCK_DIR/add-tags"
            local count
            count=$(wc -l < "$TEST_MOCK_DIR/add-tags")
            if [[ "${TEST_FAIL_AT_ADD:-0}" == "$count" ]]; then
                printf '{"success":false}\n'
            else
                printf '{"success":true,"obj":{"id":%d}}\n' "$((100 + count))"
            fi
            ;;
        'GET /panel/api/inbounds/list')
            jq -cn --rawfile tags "$TEST_MOCK_DIR/add-tags" '
                ($tags | split("\n") | map(select(length > 0))) as $names |
                {success:true, obj:(
                    [range(0; $names|length) | {id:(101 + .), tag:$names[.]}]
                    + [{id:999, tag:"existing-unrelated"}]
                )}'
            ;;
        'GET /panel/api/server/getConfigJson')
            jq -cn --argjson profiles "$NODE_PROFILES" '{
                success:true,
                obj:{inbounds:[$profiles[] | .payload | {
                    tag,port,protocol,
                    settings:(.settings|fromjson),
                    streamSettings:(.streamSettings|fromjson)
                }]}
            }'
            ;;
        'POST /panel/api/server/restartXrayService')
            if [[ -s "$TEST_MOCK_DIR/xray-candidate.json" ]]; then
                cp "$TEST_MOCK_DIR/xray-candidate.json" "$XUI_INSTALL_DIR/bin/config.json"
            fi
            printf '{"success":true}\n'
            ;;
        POST\ /panel/api/inbounds/del/*)
            printf '%s\n' "${2##*/}" >> "$TEST_MOCK_DIR/deleted-ids"
            printf '{"success":true}\n'
            ;;
        *)
            printf '{"success":false}\n'
            return 12
            ;;
    esac
}
prepare_node_profiles
'''


class InstallerApiTransactions(unittest.TestCase):
    def test_third_add_failure_rolls_back_only_attempted_tags(self) -> None:
        with tempfile.TemporaryDirectory(prefix="installer-api-", dir=REPO) as temp:
            directory = Path(temp)
            source = FIXTURE + API_MOCK + r'''
if create_inbound_nodes; then
    printf 'UNEXPECTED_SUCCESS\n'
else
    printf 'CREATE_FAILED\n'
    rollback_created_nodes
fi
'''
            result = run_bash(
                source,
                extra_env={"TEST_MOCK_DIR": bash_directory(directory), "TEST_FAIL_AT_ADD": "3"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CREATE_FAILED", result.stdout)
            self.assertNotIn("UNEXPECTED_SUCCESS", result.stdout)
            attempted = (directory / "add-tags").read_text().splitlines()
            self.assertEqual(len(attempted), 3)
            self.assertEqual((directory / "deleted-ids").read_text().splitlines(),
                             ["103", "102", "101"])
            self.assertNotIn("999", (directory / "deleted-ids").read_text())
            self.assertNotIn("getConfigJson", (directory / "requests").read_text())

    def test_success_checks_candidate_and_runtime_before_reporting_done(self) -> None:
        with tempfile.TemporaryDirectory(prefix="installer-api-", dir=REPO) as temp:
            directory = Path(temp)
            source = FIXTURE + API_MOCK + r'''
create_inbound_nodes
printf 'ROLLBACK_NEEDED=%s\n' "$ROLLBACK_NEEDED"
'''
            result = run_bash(source, extra_env={"TEST_MOCK_DIR": bash_directory(directory)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("ROLLBACK_NEEDED=1", result.stdout)
            self.assertEqual(len((directory / "add-tags").read_text().splitlines()), 4)
            self.assertFalse((directory / "deleted-ids").exists())
            self.assertIn("run -test -c", (directory / "xray-invocations").read_text())
            candidate = json.loads((directory / "xray-candidate.json").read_text())
            runtime = json.loads((directory / "server" / "bin" / "config.json").read_text())
            self.assertEqual(candidate, runtime)
            self.assertEqual({row["protocol"] for row in runtime["inbounds"]},
                             {"vless", "hysteria"})
            self.assertEqual(len(runtime["inbounds"]), 4)
            self.assertIn("restartXrayService", (directory / "requests").read_text())


class DomainMigration(unittest.TestCase):
    def test_switch_updates_only_own_tls_rows_and_keeps_node_credentials(self) -> None:
        before = generated_profiles()
        before_by_key = {item["key"]: item for item in before}
        with tempfile.TemporaryDirectory(prefix="installer-domain-", dir=REPO) as temp:
            directory = Path(temp)
            info = directory / "info"
            runner = directory / "run"
            (directory / "server").mkdir()
            info.mkdir()
            runner.mkdir()
            (info / "3x-ui-node-profiles.json").write_text(json.dumps(before), encoding="utf-8")
            (info / "3x-ui-nodes-info.txt").write_text(
                "访问地址：https://203.0.113.42:20530/fixturepath/\n"
                "管理员账号：admin\n管理员密码：fixture-secret\n", encoding="utf-8",
            )
            database = directory / "x-ui.db"
            unrelated = json.dumps({
                "network": "tcp", "security": "tls",
                "tlsSettings": {"serverName": "unrelated.test", "certificates": [{
                    "certificateFile": "/old/unrelated.pem", "keyFile": "/old/unrelated.key",
                }]},
            })
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    "CREATE TABLE inbounds (id INTEGER PRIMARY KEY, tag TEXT, protocol TEXT, "
                    "remark TEXT, stream_settings TEXT)"
                )
                for index, item in enumerate(before, 1):
                    connection.execute(
                        "INSERT INTO inbounds VALUES (?,?,?,?,?)",
                        (index, item["payload"]["tag"], item["payload"]["protocol"],
                         item["remark"], item["payload"]["streamSettings"]),
                    )
                connection.execute(
                    "INSERT INTO inbounds VALUES (?,?,?,?,?)",
                    (999, "unrelated-user-node", "vless", "User node", unrelated),
                )
                connection.commit()
            source = FIXTURE + r'''
RUN_DIR="$TEST_MOCK_DIR/run"
XUI_DB_PATH="$TEST_MOCK_DIR/x-ui.db"
XUI_INFO_DIR="$TEST_MOCK_DIR/info"
XUI_INSTALL_DIR="$TEST_MOCK_DIR/server"
install() {
    if [[ "$1" == -d && "$2" == -m && "$3" == 700 ]]; then
        mkdir -p "$4"
    else
        command install "$@"
    fi
}
sqlite3() {
    if command -v cygpath >/dev/null 2>&1 && [[ "${2:-}" == .backup* ]]; then
        local target
        target=$(printf '%s' "$2" | cut -d"'" -f2)
        command sqlite3 "$1" ".backup '$(cygpath -m "$target")'"
    else
        command sqlite3 "$@"
    fi
}
jq() { command jq "$@" | tr -d '\r'; }
cat > "$XUI_INSTALL_DIR/x-ui" <<'MOCK_XUI'
#!/usr/bin/env bash
exit 0
MOCK_XUI
chmod +x "$XUI_INSTALL_DIR/x-ui"
install_deps() { :; }
detect_server_ip() { SERVER_IP=203.0.113.42; }
issue_domain_cert() {
    CERT_HOST="$1"
    CERT_DIR="$TEST_MOCK_DIR/newcert"
    CERT_FILE="$CERT_DIR/fullchain.pem"
    KEY_FILE="$CERT_DIR/privkey.pem"
    mkdir -p "$CERT_DIR"
    printf 'fixture-cert' > "$CERT_FILE"
    printf 'fixture-key' > "$KEY_FILE"
}
restart_panel() { printf 'restart\n' >> "$TEST_MOCK_DIR/events"; }
verify_running_nodes() { printf 'verify\n' >> "$TEST_MOCK_DIR/events"; }
write_node_files() {
    printf '%s\n' "$NODE_PROFILES" > "$XUI_INFO_DIR/3x-ui-node-profiles.json"
    jq -r '.[].link' <<<"$NODE_PROFILES" > "$XUI_INFO_DIR/3x-ui-node-links.txt"
}
switch_existing_to_domain new.example.test
'''
            result = run_bash(source, extra_env={"TEST_MOCK_DIR": bash_directory(directory)})
            self.assertEqual(result.returncode, 0, result.stderr[-1200:])
            self.assertEqual((directory / "events").read_text().splitlines(),
                             ["restart", "verify"])
            with closing(sqlite3.connect(database)) as connection:
                rows = dict(connection.execute("SELECT tag, stream_settings FROM inbounds"))
            self.assertEqual(rows["unrelated-user-node"], unrelated)
            for item in before:
                stream = json.loads(rows[item["payload"]["tag"]])
                if item["security"] == "tls":
                    self.assertEqual(stream["tlsSettings"]["serverName"], "new.example.test")
                    self.assertEqual(stream["tlsSettings"]["certificates"][0]["certificateFile"],
                                     bash_directory(directory) + "/newcert/fullchain.pem")
                    self.assertEqual(stream["tlsSettings"]["certificates"][0]["keyFile"],
                                     bash_directory(directory) + "/newcert/privkey.pem")
                else:
                    self.assertEqual(stream, json.loads(item["payload"]["streamSettings"]))
            after = json.loads((info / "3x-ui-node-profiles.json").read_text(encoding="utf-8"))
            self.assertEqual({item["key"] for item in after}, set(before_by_key))
            for item in after:
                old = before_by_key[item["key"]]
                self.assertEqual(item["payload"]["tag"], old["payload"]["tag"])
                self.assertEqual(item["credential"], old["credential"])
                self.assertEqual(json.loads(item["payload"]["settings"]),
                                 json.loads(old["payload"]["settings"]))
                self.assertEqual(item["clientOutbound"]["settings"].get("id"),
                                 old["clientOutbound"]["settings"].get("id"))
                self.assertEqual(urlsplit(item["link"]).hostname, "new.example.test")
                if item["security"] == "tls":
                    self.assertEqual(parse_qs(urlsplit(item["link"]).query)["sni"],
                                     ["new.example.test"])
            self.assertEqual(len((info / "3x-ui-node-links.txt").read_text(encoding="utf-8").splitlines()), 4)


if __name__ == "__main__":
    unittest.main()
