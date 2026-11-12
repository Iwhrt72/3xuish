"""Exercise menu collection and the actual parser without API mutations."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from test_installer import REPO, bash_directory, run_bash


class ProxyCollection(unittest.TestCase):
    def collect(self, mode, protocol="1", layout="0", raw="", host="proxy.example", user="u", password="p"):
        with tempfile.TemporaryDirectory(prefix="proxy-input-", dir=REPO) as temp:
            directory = Path(temp)
            alias = 'python3() { python "$@"; }\n' if os.name == "nt" else ""
            result = run_bash(alias + r'''
source ./install.sh
source ./lib/socks_exit.sh
PROXY_INPUT_HELPER=./lib/proxy_input.py
prompt_read() {
    case "$2" in
        mode) printf -v "$2" '%s' "$TEST_MODE" ;;
        protocol_choice) printf -v "$2" '%s' "$TEST_PROTOCOL" ;;
        layout_choice) printf -v "$2" '%s' "$TEST_LAYOUT" ;;
        upstream_host) printf -v "$2" '%s' "$TEST_HOST" ;;
        upstream_port) printf -v "$2" '%s' 1080 ;;
        upstream_user) printf -v "$2" '%s' "$TEST_USER" ;;
        *) return 93 ;;
    esac
}
proxy_exit_read_private() {
    case "$2" in
        raw) printf -v "$2" '%s' "$TEST_RAW" ;;
        upstream_password) printf -v "$2" '%s' "$TEST_PASSWORD" ;;
    esac
}
proxy_exit_collect_upstream "$TEST_ROOT/outbound.json"
''', extra_env={"TEST_ROOT": bash_directory(directory), "TEST_MODE": mode,
                "TEST_PROTOCOL": protocol, "TEST_LAYOUT": layout, "TEST_RAW": raw,
                "TEST_HOST": host, "TEST_USER": user, "TEST_PASSWORD": password})
            path = directory / "outbound.json"
            outbound = json.loads(path.read_text()) if result.returncode == 0 else None
        return result, outbound

    def test_separate_fields_accept_http_domain_and_special_password(self):
        result, outbound = self.collect("1", "2", password="secret:p@ss%word")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outbound["protocol"], "http")
        self.assertEqual(outbound["settings"]["servers"][0]["users"][0]["pass"], "secret:p@ss%word")
        self.assertNotIn("secret:p@ss%word", result.stdout + result.stderr)

    def test_uri_scheme_selects_https_without_protocol_prompt(self):
        result, outbound = self.collect("2", "INVALID", raw="https://u:secret%40pass@proxy.example:8443")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(outbound["streamSettings"]["security"], "tls")
        self.assertNotIn("secret", result.stdout + result.stderr)

    def test_screenshot_bare_formats_use_explicit_protocol(self):
        for layout, value in (("1", "proxy.example:1080:u:private"),
                              ("3", "u:private@proxy.example:1080"),
                              ("4", "proxy.example:1080@u:private")):
            with self.subTest(layout=layout):
                result, outbound = self.collect("2", "2", layout, value)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(outbound["protocol"], "http")
                self.assertEqual(outbound["settings"]["servers"][0]["address"], "proxy.example")

    def test_ambiguous_paste_fails_without_exposing_credentials(self):
        result, _ = self.collect("2", "1", "0", "a:1234@b:5678")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("a:1234@b:5678", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
