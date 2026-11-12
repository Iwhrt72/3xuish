"""Private proxy input formats, protocol boundaries and credential handling."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import proxy_input as proxy


class ProxyFormats(unittest.TestCase):
    def test_four_platform_formats_produce_the_same_configuration(self):
        expected = {"scheme": "socks5", "host": "proxy.example", "port": 1080,
                    "username": "user", "password": "secret"}
        for text in ("proxy.example:1080:user:secret",
                     "socks5://user:secret@proxy.example:1080",
                     "user:secret@proxy.example:1080",
                     "proxy.example:1080@user:secret"):
            with self.subTest(text=text):
                self.assertEqual(proxy.parse_proxy(text, "socks5"), expected)

    def test_explicit_protocols_and_alias(self):
        for scheme, expected in (("socks5", "socks5"), ("SOCKS5H", "socks5"),
                                 ("http", "http"), ("https", "https")):
            with self.subTest(scheme=scheme):
                parsed = proxy.parse_proxy(scheme + "://u:p@host.example:8080")
                self.assertEqual(parsed["scheme"], expected)

    def test_bare_layout_never_infers_protocol_from_port(self):
        for text in ("host.example:443", "host.example:1080:u:p", "u:p@host.example:80"):
            with self.subTest(text=text):
                with self.assertRaisesRegex(proxy.InputError, "选择"):
                    proxy.parse_proxy(text)
        self.assertEqual(proxy.parse_proxy("host.example:443", "socks5")["scheme"], "socks5")

    def test_selected_protocol_must_agree_with_uri(self):
        with self.assertRaisesRegex(proxy.InputError, "不一致"):
            proxy.parse_proxy("http://u:p@host.example:80", "socks5")

    def test_ambiguous_layout_requires_explicit_choice(self):
        text = "first.example:8080@second.example:9090"
        with self.assertRaisesRegex(proxy.InputError, "多种"):
            proxy.parse_proxy(text, "socks5")
        endpoint = proxy.parse_proxy(text, "socks5", "endpoint")
        userinfo = proxy.parse_proxy(text, "socks5", "userinfo")
        self.assertEqual((endpoint["host"], endpoint["port"], endpoint["username"], endpoint["password"]),
                         ("first.example", 8080, "second.example", "9090"))
        self.assertEqual((userinfo["host"], userinfo["port"], userinfo["username"], userinfo["password"]),
                         ("second.example", 9090, "first.example", "8080"))

    def test_identical_interpretations_are_not_ambiguous(self):
        result = proxy.parse_proxy("same.example:8080@same.example:8080", "socks5")
        self.assertEqual(result["host"], "same.example")

    def test_layout_selection_does_not_silently_switch_formats(self):
        for text, layout in (("user:pass@host.example:1080", "colon"),
                             ("host.example:1080:u:p", "url"),
                             ("http://u:p@host.example:1080", "userinfo")):
            with self.subTest(layout=layout):
                with self.assertRaises(proxy.InputError):
                    proxy.parse_proxy(text, "http", layout)

    def test_ipv6_formats_and_canonical_identity(self):
        for text in ("[2001:db8::1]:1080:u:p", "u:p@[2001:db8::1]:1080",
                     "[2001:db8::1]:1080@u:p", "socks5://u:p@[2001:db8::1]:1080"):
            with self.subTest(text=text):
                self.assertEqual(proxy.parse_proxy(text, "socks5")["host"], "2001:db8::1")
        with self.assertRaises(proxy.InputError):
            proxy.parse_proxy("2001:db8::1:1080", "socks5")
        fields = proxy.parse_fields({"host": "2001:0db8::0001", "port": "1080"}, "socks5")
        self.assertEqual(fields["host"], "2001:db8::1")

    def test_idna_and_case_and_root_dot(self):
        for text in ("BÜCHER.example:8080", "xn--bcher-kva.example.:8080"):
            self.assertEqual(proxy.parse_proxy(text, "http")["host"], "xn--bcher-kva.example")

    def test_noauth_proxy_forms(self):
        for text, protocol in (("proxy.example:80", "http"), ("https://proxy.example:443/", None),
                               ("socks5://[2001:db8::1]:1080", None)):
            parsed = proxy.parse_proxy(text, protocol)
            outbound = proxy.build_outbound(parsed)
            self.assertNotIn("users", outbound["settings"]["servers"][0])

    def test_partial_auth_is_rejected(self):
        for username, password in (("user", ""), ("", "password")):
            with self.subTest(username=username):
                with self.assertRaises(proxy.InputError):
                    proxy.parse_fields({"host": "host.example", "port": 1080,
                                        "username": username, "password": password}, "socks5")

    def test_uri_percent_decode_once_and_literal_plus(self):
        parsed = proxy.parse_proxy("socks5://u%3Aname:p%40ss%3Aword%252F+plus@host.example:1080")
        self.assertEqual(parsed["username"], "u:name")
        self.assertEqual(parsed["password"], "p@ss:word%2F+plus")

    def test_plain_credentials_are_literal(self):
        for text, layout in (("host.example:1080:user:p@ss:word%2F", "colon"),
                             ("user:p@ss:word%2F@host.example:1080", "userinfo"),
                             ("host.example:1080@user:p@ss:word%2F", "endpoint")):
            parsed = proxy.parse_proxy(text, "socks5", layout)
            self.assertEqual(parsed["password"], "p@ss:word%2F")
        self.assertEqual(proxy.parse_proxy("host.example:1080:user:p://word", "socks5", "colon")["password"],
                         "p://word")

    def test_fields_preserve_raw_special_characters_and_spaces(self):
        fields = {"host": "host.example", "port": 1080, "username": "u:@%",
                  "password": " p:@%/word "}
        parsed = proxy.parse_fields(fields, "socks5")
        self.assertEqual(parsed["username"], fields["username"])
        self.assertEqual(parsed["password"], fields["password"])

    def test_http_basic_username_colon_rejected_but_password_colon_allowed(self):
        for scheme in ("http", "https"):
            with self.assertRaisesRegex(proxy.InputError, "账号不能"):
                proxy.parse_proxy(scheme + "://u%3Aname:pass@host.example:80")
            parsed = proxy.parse_proxy(scheme + "://user:pass%3Aword@host.example:80")
            self.assertEqual(parsed["password"], "pass:word")

    def test_invalid_ports_and_hostnames_are_rejected(self):
        for host in ("bad..example", "bad.example..", "-bad.example", "bad_.example", "0.0.0.0",
                     "224.1.2.3", "999.1.2.3", "fe80::1%eth0", "[host.example]", "a b.example"):
            with self.subTest(host=host):
                with self.assertRaises(proxy.InputError):
                    proxy.parse_fields({"host": host, "port": 1080}, "socks5")
        for port in (0, -1, 65536, True, 1.5, "1e3", "+80", " 80", "１２３", None):
            with self.subTest(port=port):
                with self.assertRaises(proxy.InputError):
                    proxy.parse_fields({"host": "host.example", "port": port}, "socks5")

    def test_leading_zero_port_is_decimal(self):
        self.assertEqual(proxy.parse_proxy("host.example:01080", "socks5")["port"], 1080)

    def test_invalid_uri_forms_and_controls_are_rejected(self):
        for text in ("ftp://u:p@host.example:80", "http://u:p@host.example",
                     "http://u:p@host.example:80/path", "http://u:p@host.example:80?x=1",
                     "http://u:p@host.example:80#fragment", "http://u:p@@host.example:80",
                     "http://u:p%ZZ@host.example:80", "http://u:p%FF@host.example:80",
                     "http://u:p%0Aword@host.example:80", "http://u:p ass@host.example:80",
                     "host.example:80:u:p\nword", "host.example:80:u:p\x7fword"):
            with self.subTest(text=text):
                with self.assertRaises(proxy.InputError):
                    proxy.parse_proxy(text, "auto")

    def test_socks_credential_limit_counts_utf8_bytes(self):
        with self.assertRaisesRegex(proxy.InputError, "255"):
            proxy.parse_fields({"host": "host.example", "port": 1080,
                                "username": "u", "password": "密" * 86}, "socks5")


class XrayOutput(unittest.TestCase):
    def test_socks_identity_matches_original_shell_nul_hash(self):
        values = ("192.0.2.3", "1080", "user", "password")
        expected = hashlib.sha256(b"\0".join(v.encode() for v in values) + b"\0").hexdigest()[:16]
        outbound = proxy.build_outbound(proxy.parse_proxy("192.0.2.3:1080:user:password", "socks5"))
        self.assertEqual(outbound["tag"], "exitnode-" + expected + "-socks")
        self.assertEqual(outbound["protocol"], "socks")
        self.assertEqual(outbound["settings"]["servers"][0]["users"], [{"user": "user", "pass": "password"}])

    def test_three_protocols_have_distinct_identity_and_https_verifies_tls(self):
        outputs = [proxy.build_outbound(proxy.parse_proxy(scheme + "://u:p@host.example:8080"))
                   for scheme in ("socks5", "http", "https")]
        self.assertEqual(len({value["tag"] for value in outputs}), 3)
        self.assertEqual(outputs[1]["protocol"], "http")
        self.assertNotIn("streamSettings", outputs[1])
        self.assertEqual(outputs[2]["protocol"], "http")
        self.assertEqual(outputs[2]["streamSettings"], {
            "network": "tcp", "security": "tls", "tlsSettings": {"serverName": "host.example"}})
        self.assertNotIn("allowInsecure", json.dumps(outputs[2]))


class PrivateCli(unittest.TestCase):
    def call(self, args, data):
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch("sys.stdin", io.StringIO(data)), redirect_stdout(stdout), redirect_stderr(stderr):
            rc = proxy.main(args)
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_parse_accepts_only_one_line_ending_and_emits_json(self):
        rc, out, err = self.call(["parse", "--protocol", "http"], "host.example:8080:u:p\n")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["protocol"], "http")
        self.assertEqual(err, "")
        rc, _, _ = self.call(["parse", "--protocol", "http"], "host.example:8080:u:p\n\n")
        self.assertEqual(rc, 1)

    def test_fields_cli_and_empty_auth(self):
        rc, out, err = self.call(["fields", "--protocol", "https"],
                                 json.dumps({"host": "host.example", "port": "443"}))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("users", json.loads(out)["settings"]["servers"][0])

    def test_failures_never_print_secret_or_partial_configuration(self):
        secret = "fixture-SUPER-SECRET"
        for args, text in ((["parse"], "http://user:" + secret + "%ZZ@host.example:80"),
                           (["fields", "--protocol", "http"], '{"password":"' + secret),
                           (["fields", "--protocol", "http"], json.dumps([secret]))):
            with self.subTest(args=args):
                rc, out, err = self.call(args, text)
                self.assertEqual(rc, 1)
                self.assertEqual(out, "")
                self.assertNotIn(secret, err)
                self.assertTrue(err)

    def test_oversized_input_is_rejected_without_echo(self):
        rc, out, err = self.call(["parse", "--protocol", "http"], "x" * (proxy.MAX_INPUT + 1))
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")
        self.assertLess(len(err), 200)
        prefix = "http://u:"
        suffix = "@host.example:80"
        valid_prefix = prefix + "p" * (proxy.MAX_INPUT - len(prefix) - len(suffix)) + suffix
        rc, out, _ = self.call(["parse"], valid_prefix + "\nignored-trailing-data")
        self.assertEqual(rc, 1)
        self.assertEqual(out, "")


if __name__ == "__main__":
    unittest.main()
