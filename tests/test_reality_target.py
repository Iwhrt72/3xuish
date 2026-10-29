"""REALITY target preflight must validate identity and negotiated ALPN."""
from pathlib import Path
import tempfile
import unittest

from test_installer import FIXTURE, REPO, bash_directory, run_bash


TLS13_OK = """CONNECTED(00000003)
Protocol version: TLSv1.3
New, TLSv1.3, Cipher is TLS_AES_128_GCM_SHA256
Verification: OK
ALPN protocol: h2
Verify return code: 0 (ok)
"""


class RealityTargetPreflight(unittest.TestCase):
    def probe(self, output=TLS13_OK, code=0, *, name="edge.example.test",
              target="destination.example.test:443"):
        with tempfile.TemporaryDirectory(prefix="reality-target-", dir=REPO) as temp:
            directory = Path(temp)
            result = run_bash(r'''
source ./install.sh
REALITY_SERVER_NAME="$TEST_SERVER_NAME"
REALITY_TARGET="$TEST_TARGET"
timeout() { shift; "$@"; }
openssl() {
    printf '%s\n' "$@" >"$TEST_MOCK_DIR/probe-arguments"
    printf '%s\n' "$TEST_PROBE_OUTPUT"
    return "$TEST_PROBE_EXIT"
}
if verify_reality_target; then
    printf 'PROBE_ACCEPTED\n'
else
    printf 'PROBE_REJECTED\n'
fi
''', extra_env={"TEST_MOCK_DIR": bash_directory(directory),
                "TEST_SERVER_NAME": name, "TEST_TARGET": target,
                "TEST_PROBE_OUTPUT": output, "TEST_PROBE_EXIT": str(code)})
            path = directory / "probe-arguments"
            arguments = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, arguments

    def test_tls13_verified_h2_target_is_accepted_and_custom_target_preserved(self):
        result, arguments = self.probe()
        self.assertIn("PROBE_ACCEPTED", result.stdout)
        self.assertIn("destination.example.test:443", arguments)
        self.assertIn("edge.example.test", arguments)

    def test_nonbrief_openssl_tls13_output_is_accepted_without_brief_protocol_line(self):
        result, _ = self.probe(TLS13_OK.replace("Protocol version: TLSv1.3\n", ""))
        self.assertIn("PROBE_ACCEPTED", result.stdout)

    def test_explicit_microsoft_is_not_blocked_when_preflight_is_valid(self):
        result, arguments = self.probe(name="www.microsoft.com", target="www.microsoft.com:443")
        self.assertIn("PROBE_ACCEPTED", result.stdout)
        self.assertIn("www.microsoft.com:443", arguments)

    def test_probe_requests_hostname_verification_x25519_and_h2_without_brief(self):
        result, arguments = self.probe()
        self.assertIn("PROBE_ACCEPTED", result.stdout)
        for option, value in (("-connect", "destination.example.test:443"),
                              ("-servername", "edge.example.test"),
                              ("-verify_hostname", "edge.example.test"),
                              ("-groups", "X25519"), ("-alpn", "h2")):
            with self.subTest(option=option):
                self.assertIn(option, arguments)
                self.assertEqual(arguments[arguments.index(option) + 1], value)
        self.assertIn("-tls1_3", arguments)
        self.assertIn("-verify_return_error", arguments)
        self.assertNotIn("-brief", arguments, "brief output hides negotiated ALPN")

    def test_tls13_without_negotiated_h2_is_rejected(self):
        for replacement in ("No ALPN negotiated", "ALPN protocol: http/1.1", ""):
            with self.subTest(replacement=replacement):
                result, _ = self.probe(TLS13_OK.replace("ALPN protocol: h2", replacement))
                self.assertIn("PROBE_REJECTED", result.stdout)
                self.assertNotIn("PROBE_ACCEPTED", result.stdout)

    def test_tls12_is_rejected_even_with_verified_h2(self):
        result, _ = self.probe(TLS13_OK.replace("TLSv1.3", "TLSv1.2"))
        self.assertIn("PROBE_REJECTED", result.stdout)

    def test_failure_exit_is_rejected_even_when_output_contains_success_fields(self):
        for code in (1, 124):
            with self.subTest(code=code):
                result, _ = self.probe(code=code)
                self.assertIn("PROBE_REJECTED", result.stdout)
                self.assertNotIn("PROBE_ACCEPTED", result.stdout)

    def test_hostname_failure_is_rejected(self):
        result, _ = self.probe(
            TLS13_OK.replace("Verification: OK", "Verification error: hostname mismatch")
            .replace("Verify return code: 0 (ok)", "Verify return code: 62 (hostname mismatch)"), code=1)
        self.assertIn("PROBE_REJECTED", result.stdout)


CONFIG_FIXTURE = r'''
source ./install.sh
AUTO_YES=1
XUI_PASSWORD=fixture-password
XUI_PANEL_PORT=20530
XUI_WEB_BASE_PATH=fixture-path
XHTTP_PATH=/fixture-path
CERT_MODE=1
PRIMARY_NODE=hy2
gen_random_str() { printf 'fixture'; }
unset REALITY_SERVER_NAME REALITY_TARGET
'''


class RealityTargetDefaults(unittest.TestCase):
    def test_fresh_install_uses_apple_without_an_explicit_override(self):
        result = run_bash(CONFIG_FIXTURE + r'''
init_config
printf 'SELECTED_REALITY:%s|%s\n' "$REALITY_SERVER_NAME" "$REALITY_TARGET"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SELECTED_REALITY:www.apple.com|www.apple.com:443", result.stdout)

    def test_custom_target_and_server_name_are_not_rewritten_by_defaults(self):
        result = run_bash(CONFIG_FIXTURE + r'''
REALITY_SERVER_NAME=edge.example.test
REALITY_TARGET=destination.example.test:8443
init_config
printf 'SELECTED_REALITY:%s|%s\n' "$REALITY_SERVER_NAME" "$REALITY_TARGET"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("SELECTED_REALITY:edge.example.test|destination.example.test:8443", result.stdout)

    def test_existing_panel_new_nodes_use_apple_without_mutating_before_probe(self):
        with tempfile.TemporaryDirectory(prefix="reality-default-", dir=REPO) as temp:
            directory = Path(temp)
            (directory / "live.db").write_bytes(b"existing-panel-untouched")
            result = run_bash(r'''
source ./install.sh
XUI_DB_PATH="$TEST_MOCK_DIR/live.db"
XUI_INFO_DIR="$TEST_MOCK_DIR/info"
RUN_DIR="$TEST_MOCK_DIR/run"
AUTO_YES=1
PRIMARY_NODE=hy2
unset REALITY_SERVER_NAME REALITY_TARGET
install_deps() { :; }
detect_server_ip() { SERVER_IP=203.0.113.42; }
resolve_xray_binary() { :; }
load_existing_panel_context() { :; }
install() { command mkdir -p "${@: -1}"; }
sqlite3() { printf '0\n'; }
existing_select_node_port() { printf -v "$1" '%s' "$2"; }
gen_random_str() { printf 'fixture'; }
verify_reality_target() {
    printf 'SELECTED_REALITY:%s|%s\n' "$REALITY_SERVER_NAME" "$REALITY_TARGET"
    return 77
}
generate_vlessenc_keys() { printf 'UNSAFE_NODE_CREATION\n'; }
add_nodes_to_existing
''', extra_env={"TEST_MOCK_DIR": bash_directory(directory)})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("SELECTED_REALITY:www.apple.com|www.apple.com:443", result.stdout)
            self.assertNotIn("UNSAFE_NODE_CREATION", result.stdout + result.stderr)
            self.assertEqual((directory / "live.db").read_bytes(), b"existing-panel-untouched")

    def test_explicit_microsoft_keeps_matching_server_and_client_sni_in_share_link(self):
        result = run_bash(FIXTURE + r'''
prepare_node_profiles
jq -r '.[] | select(.key == "reality") |
    [.payload.streamSettings, .clientOutbound.streamSettings.realitySettings.serverName, .link] | @json' \
    <<<"$NODE_PROFILES"
''')
        self.assertEqual(result.returncode, 0, result.stderr)
        import json
        from urllib.parse import parse_qs, urlsplit
        stream_text, sni, link = json.loads(result.stdout.strip())
        stream = json.loads(stream_text)
        self.assertEqual(stream["realitySettings"]["target"], "www.microsoft.com:443")
        self.assertEqual(stream["realitySettings"]["serverNames"], ["www.microsoft.com"])
        self.assertEqual(sni, "www.microsoft.com")
        self.assertEqual(parse_qs(urlsplit(link).query)["sni"], ["www.microsoft.com"])


if __name__ == "__main__":
    unittest.main()
