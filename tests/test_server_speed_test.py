"""Existing-node diagnostics must preserve live identity and keep secrets local."""
from contextlib import closing, contextmanager, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import server_speed_test as diagnostic


IDENTITY = "fixture-client-secret-do-not-print"
ENCRYPTION = "fixture-client-encryption-do-not-print"
DECRYPTION = "fixture-server-private-decryption"
PRIVATE = "fixture-server-private-key"


def fixture_node(protocol="vless"):
    clients = [{"id": IDENTITY, "flow": "xtls-rprx-vision", "enable": True, "expiryTime": 0}]
    settings = {"clients": clients, "decryption": DECRYPTION, "encryption": ENCRYPTION}
    stream = {"network": "xhttp", "security": "tls",
        "xhttpSettings": {"path": "/preserved-path", "mode": "auto"},
        "tlsSettings": {"serverName": "fixture.example", "alpn": ["h2", "http/1.1"],
            "minVersion": "1.2", "maxVersion": "1.3", "allowInsecure": True,
            "settings": {"fingerprint": "chrome", "allowInsecure": True},
            "certificates": [{"key": PRIVATE, "certificate": "fixture-cert",
                              "keyFile": "/private/server.key", "certificateFile": "/private/server.pem"}]},
        "sockopt": {"tcpWindowClamp": 0, "tcpKeepAliveIdle": 300}}
    if protocol == "hysteria":
        settings = {"version": 2, "clients": [
            {"auth": "disabled-auth", "enable": False, "expiryTime": 0},
            {"auth": "expired-auth", "enable": True, "expiryTime": int(time.time() * 1000) - 10000},
            {"auth": IDENTITY, "enable": True, "expiryTime": 0}]}
        stream["network"] = "hysteria"
        stream.pop("xhttpSettings")
        stream["hysteriaSettings"] = {"version": 2}
    row = {"id": 1, "tag": "fixture-tag", "port": 443 if protocol == "vless" else 42710,
           "protocol": protocol, "enable": 1, "listen": "", "settings": json.dumps(settings),
           "stream_settings": json.dumps(stream)}
    runtime_settings = copy.deepcopy(settings)
    if protocol == "hysteria":
        runtime_settings = {"version": 2, "users": [{"auth": item["auth"]} for item in settings["clients"]]}
    else:
        runtime_settings.pop("encryption")
    live = {"tag": row["tag"], "port": row["port"], "protocol": protocol,
            "listen": "0.0.0.0", "settings": runtime_settings, "streamSettings": copy.deepcopy(stream)}
    return row, live


class ExistingNodeMapping(unittest.TestCase):
    def test_vless_preserves_stored_client_identity_and_tls_verification_without_server_keys(self):
        row, live = fixture_node()
        before = copy.deepcopy((row, live))
        outbound = diagnostic.build_outbound(row, live)
        self.assertEqual((row, live), before)
        self.assertEqual(outbound["settings"], {"address": "127.0.0.1", "port": 443,
            "id": IDENTITY, "encryption": ENCRYPTION, "flow": "xtls-rprx-vision"})
        tls = outbound["streamSettings"]["tlsSettings"]
        self.assertFalse(tls["allowInsecure"])
        self.assertEqual(tls["serverName"], "fixture.example")
        self.assertEqual(tls["alpn"], ["h2", "http/1.1"])
        self.assertEqual(outbound["streamSettings"]["xhttpSettings"], {"path": "/preserved-path", "mode": "auto"})
        serialized = json.dumps(outbound)
        for private in (DECRYPTION, PRIVATE, "/private/server.key", "/private/server.pem"):
            self.assertNotIn(private, serialized)
        self.assertNotIn("certificates", tls)
        self.assertNotIn("sockopt", outbound["streamSettings"])

    def test_hy2_uses_an_enabled_nonexpired_auth_present_in_runtime_users(self):
        row, live = fixture_node("hysteria")
        outbound = diagnostic.build_outbound(row, live)
        self.assertEqual(outbound["settings"], {"version": 2, "address": "127.0.0.1", "port": 42710})
        self.assertEqual(outbound["streamSettings"]["hysteriaSettings"], {"version": 2, "auth": IDENTITY})
        self.assertFalse(outbound["streamSettings"]["tlsSettings"]["allowInsecure"])
        self.assertNotIn(PRIVATE, json.dumps(outbound))
        live["settings"]["users"] = [{"auth": "different-runtime-auth"}]
        with self.assertRaisesRegex(diagnostic.DiagnosticError, "no_enabled_runtime_client"):
            diagnostic.build_outbound(row, live)

    def test_missing_hy2_sni_uses_public_certificate_dns_or_ip_san_and_keeps_verification(self):
        for san, expected in (("DNS:node.example.test, IP Address:203.0.113.7", "node.example.test"),
                              ("IP Address:203.0.113.7", "203.0.113.7"),
                              ("IP Address:2001:db8::7", "2001:db8::7")):
            with self.subTest(san=san):
                row, live = fixture_node("hysteria")
                stream = json.loads(row["stream_settings"])
                stream["tlsSettings"].pop("serverName")
                row["stream_settings"] = json.dumps(stream)
                live["streamSettings"]["tlsSettings"].pop("serverName")
                before = copy.deepcopy((row, live))
                openssl_result = mock.Mock(returncode=0, stdout="X509v3 Subject Alternative Name:\n    " + san,
                                           stderr=PRIVATE)
                with mock.patch.object(diagnostic.shutil, "which", return_value="/fixture/openssl"), \
                        mock.patch.object(diagnostic.subprocess, "run", return_value=openssl_result) as run:
                    outbound = diagnostic.build_outbound(row, live)
                self.assertEqual((row, live), before)
                tls = outbound["streamSettings"]["tlsSettings"]
                self.assertEqual(tls["serverName"], expected)
                self.assertFalse(tls["allowInsecure"])
                for private in (PRIVATE, "/private/server.key", "/private/server.pem"):
                    self.assertNotIn(private, json.dumps(outbound))
                self.assertNotIn("certificates", tls)
                args = run.call_args.args[0]
                self.assertEqual(args, ["/fixture/openssl", "x509", "-in", "/private/server.pem",
                                        "-noout", "-ext", "subjectAltName"])
                self.assertNotIn("/private/server.key", args)

    def test_certificate_sni_fallback_rejects_wildcard_invalid_sans_or_failed_openssl(self):
        tls = fixture_node("hysteria")[1]["streamSettings"]["tlsSettings"]
        for result in (mock.Mock(returncode=0, stdout="DNS:*.example.test"),
                       mock.Mock(returncode=0, stdout="IP Address:not-an-ip"),
                       mock.Mock(returncode=1, stdout=PRIVATE, stderr=DECRYPTION)):
            with self.subTest(result=result), \
                    mock.patch.object(diagnostic.shutil, "which", return_value="/fixture/openssl"), \
                    mock.patch.object(diagnostic.subprocess, "run", return_value=result), \
                    self.assertRaisesRegex(diagnostic.DiagnosticError, "^tls_server_name_missing$") as error:
                diagnostic.certificate_server_name(tls)
            self.assertNotIn(PRIVATE, str(error.exception))
            self.assertNotIn(DECRYPTION, str(error.exception))
        with mock.patch.object(diagnostic.shutil, "which", return_value=None), \
                mock.patch.object(diagnostic.subprocess, "run") as run, \
                self.assertRaisesRegex(diagnostic.DiagnosticError, "^tls_server_name_missing$"):
            diagnostic.certificate_server_name(tls)
        run.assert_not_called()

    def test_existing_sni_does_not_read_certificate_or_invoke_openssl(self):
        row, live = fixture_node("hysteria")
        with mock.patch.object(diagnostic, "certificate_server_name", side_effect=AssertionError("unexpected SAN read")):
            outbound = diagnostic.build_outbound(row, live)
        self.assertEqual(outbound["streamSettings"]["tlsSettings"]["serverName"], "fixture.example")

    def test_disabled_or_expired_clients_and_disabled_inbounds_are_not_tested(self):
        for updates in ({"enable": False}, {"expiryTime": -1},
                        {"expiryTime": int(time.time() * 1000) - 10000}):
            with self.subTest(updates=updates):
                row, live = fixture_node()
                settings = json.loads(row["settings"])
                settings["clients"][0].update(updates)
                row["settings"] = json.dumps(settings)
                with self.assertRaisesRegex(diagnostic.DiagnosticError, "no_enabled_runtime_client"):
                    diagnostic.build_outbound(row, live)
        row, live = fixture_node()
        row["enable"] = 0
        with self.assertRaisesRegex(diagnostic.DiagnosticError, "inbound_disabled"):
            diagnostic.build_outbound(row, live)

    def test_runtime_transport_encryption_tls_and_identity_changes_fail_closed(self):
        cases = [
            lambda live: live.update(port=8443),
            lambda live: live["settings"]["clients"][0].update(id="different-id"),
            lambda live: live["settings"].update(decryption="different-private-decryption"),
            lambda live: live["streamSettings"]["xhttpSettings"].update(path="/different"),
            lambda live: live["streamSettings"]["tlsSettings"].update(serverName="different.example"),
        ]
        for change in cases:
            with self.subTest(change=cases.index(change)):
                row, live = fixture_node()
                change(live)
                with self.assertRaises(diagnostic.DiagnosticError):
                    diagnostic.build_outbound(row, live)

    def test_client_flow_and_explicit_listener_drift_are_not_silently_accepted(self):
        row, live = fixture_node()
        live["settings"]["clients"][0]["flow"] = ""
        with self.assertRaises(diagnostic.DiagnosticError):
            diagnostic.build_outbound(row, live)
        row, live = fixture_node()
        row["listen"] = "127.0.0.2"
        with self.assertRaises(diagnostic.DiagnosticError):
            diagnostic.build_outbound(row, live)

    def test_unknown_transports_and_specific_remote_bindings_fail_without_credential_output(self):
        for network in ("grpc", "ws", [], {}):
            with self.subTest(network=network):
                row, live = fixture_node()
                stream = json.loads(row["stream_settings"])
                stream["network"] = network
                row["stream_settings"] = json.dumps(stream)
                live["streamSettings"]["network"] = network
                with self.assertRaises(diagnostic.DiagnosticError) as error:
                    diagnostic.build_outbound(row, live)
                self.assertNotIn(IDENTITY, str(error.exception))
        for bind in ("203.0.113.7", "fixture.example", "/tmp/unix.sock"):
            with self.subTest(bind=bind), self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.local_address(bind)
        self.assertEqual(diagnostic.local_address("::"), "::1")

    def test_malformed_security_fails_with_a_sanitized_diagnostic_error(self):
        for security in ([], {}):
            row, live = fixture_node()
            stream = json.loads(row["stream_settings"])
            stream["security"] = security
            row["stream_settings"] = json.dumps(stream)
            live["streamSettings"]["security"] = security
            with self.subTest(security=security), self.assertRaises(diagnostic.DiagnosticError):
                diagnostic.build_outbound(row, live)


class DiagnosticCli(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="server-speed-fixture-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "x-ui.db"
        self.install = self.root / "install"
        (self.install / "bin").mkdir(parents=True)
        self.runtime = self.install / "bin/config.json"

    def measured(self, label, seconds, curl, proxy=None):
        return [{"status": "ok", "route": label, "label": "OVH", "mbps": 42.0,
                 "connections": count, "valid_connections": count} for count in (1, 4)]

    def run_main(self, extra=()):
        output = io.StringIO()
        previous = os.umask(0o077)
        try:
            with mock.patch.object(diagnostic.shutil, "which", return_value="fixture-curl"), \
                    mock.patch.object(diagnostic, "resolve_xray", return_value=Path("/fixture/xray")), \
                    mock.patch.object(diagnostic, "run_measurements", side_effect=self.measured), \
                    mock.patch.object(diagnostic, "host_snapshot", return_value={"cpu_count": 2}), \
                    redirect_stdout(output):
                result = diagnostic.main(["--database", str(self.database), "--install-dir", str(self.install),
                    "--report-root", str(self.root / "reports"), *extra])
        finally:
            os.umask(previous)
        files = list((self.root / "reports").glob("*/report.json"))
        self.assertEqual(len(files), 1)
        return result, output.getvalue(), json.loads(files[0].read_text(encoding="utf-8"))

    def test_missing_database_only_reports_baseline_without_creating_database(self):
        result, output, report = self.run_main()
        self.assertEqual(result, 1)
        self.assertEqual(report["status"], "partial")
        self.assertFalse(self.database.exists())
        self.assertIn("node_inspection_error", report)
        self.assertEqual(report["nodes"], [])
        self.assertEqual({item["route"] for item in report["tests"]}, {"direct"})
        self.assertFalse(report["configuration_modified"])
        self.assertFalse(report["panel_restarted"])
        self.assertNotIn(IDENTITY, output)

    def test_direct_only_explicitly_skips_node_inspection_and_completes(self):
        with mock.patch.object(diagnostic, "load_nodes", side_effect=AssertionError("must not inspect nodes")) as load:
            result, output, report = self.run_main(["--direct-only"])
        load.assert_not_called()
        self.assertEqual(result, 0)
        self.assertEqual(report["status"], "complete")
        self.assertFalse(self.database.exists())
        self.assertNotIn("node_inspection_error", report)

    def test_database_with_no_enabled_nodes_cannot_report_complete_from_baseline_only(self):
        row, live = fixture_node()
        row["enable"] = 0
        with mock.patch.object(diagnostic, "load_nodes", return_value=([row], {"inbounds": [live]})), \
                mock.patch.object(diagnostic, "temporary_client") as client:
            result, output, report = self.run_main()
        client.assert_not_called()
        self.assertEqual(result, 1)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["node_inspection_error"], "no_enabled_supported_inbounds")
        self.assertEqual(report["nodes"], [])
        self.assertEqual({test["route"] for test in report["tests"]}, {"direct"})

    def test_unavailable_requested_id_cannot_claim_its_node_was_tested(self):
        row, live = fixture_node()
        with mock.patch.object(diagnostic, "load_nodes", return_value=([row], {"inbounds": [live]})), \
                mock.patch.object(diagnostic, "temporary_client") as client:
            result, output, report = self.run_main(["--inbound-id", "99"])
        client.assert_not_called()
        self.assertEqual(result, 1)
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["node_inspection_error"], "requested_inbound_unavailable")
        self.assertEqual(report["nodes"], [])
        self.assertEqual({test["route"] for test in report["tests"]}, {"direct"})

    def test_existing_database_runtime_and_secret_material_are_unchanged_and_reports_are_sanitized(self):
        row, live = fixture_node()
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute("CREATE TABLE inbounds(id INTEGER,tag TEXT,port INTEGER,protocol TEXT,"
                               "enable INTEGER,listen TEXT,settings TEXT,stream_settings TEXT)")
            connection.execute("INSERT INTO inbounds VALUES(?,?,?,?,?,?,?,?)", tuple(row.values()))
            connection.commit()
        self.runtime.write_text(json.dumps({"inbounds": [live]}), encoding="utf-8")
        before = self.database.read_bytes(), self.runtime.read_bytes()
        @contextmanager
        def local_client(*args, **kwargs):
            yield "socks5h://127.0.0.1:45678"
        with mock.patch.object(diagnostic, "temporary_client", side_effect=local_client) as client:
            result, output, report = self.run_main()
        self.assertEqual(result, 0)
        self.assertEqual(client.call_count, 1)
        self.assertEqual(report["nodes"][0]["status"], "tested")
        self.assertEqual((self.database.read_bytes(), self.runtime.read_bytes()), before)
        combined = output + json.dumps(report)
        for secret in (IDENTITY, ENCRYPTION, DECRYPTION, PRIVATE, "/preserved-path", "fixture.example"):
            self.assertNotIn(secret, combined)
        self.assertEqual({item["route"] for item in report["tests"]}, {"direct", "inbound-1-xhttp"})


class TemporaryClientLifecycle(unittest.TestCase):
    def test_interrupt_terminates_only_own_child_and_removes_private_temporary_config(self):
        row, live = fixture_node()
        outbound = diagnostic.build_outbound(row, live)
        child = mock.Mock()
        child.poll.return_value = None
        with mock.patch.object(diagnostic.subprocess, "run", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(diagnostic.subprocess, "Popen", return_value=child) as start, \
                mock.patch.object(diagnostic.socket, "create_connection", return_value=mock.MagicMock()):
            with self.assertRaises(KeyboardInterrupt):
                with diagnostic.temporary_client("/fixture/xray", outbound) as proxy:
                    self.assertTrue(proxy.startswith("socks5h://127.0.0.1:"))
                    arguments = start.call_args.args[0]
                    self.assertNotIn(IDENTITY, " ".join(arguments))
                    path = Path(arguments[-1])
                    self.assertTrue(path.exists())
                    if os.name == "posix":
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                    config = json.loads(path.read_text())
                    self.assertEqual(config["inbounds"][0]["listen"], "127.0.0.1")
                    self.assertEqual(config["outbounds"][0]["settings"]["id"], IDENTITY)
                    raise KeyboardInterrupt
        child.terminate.assert_called_once_with()
        child.wait.assert_called_once_with(timeout=5)
        child.kill.assert_not_called()
        self.assertFalse(path.exists())

    def test_raw_core_validation_errors_never_escape_as_credential_details(self):
        row, live = fixture_node()
        with mock.patch.object(diagnostic.subprocess, "run", return_value=mock.Mock(
                returncode=23, stdout=IDENTITY, stderr=DECRYPTION)):
            with self.assertRaises(diagnostic.DiagnosticError) as error:
                with diagnostic.temporary_client("/fixture/xray", diagnostic.build_outbound(row, live)):
                    self.fail("rejected config must not start")
        self.assertEqual(str(error.exception), "temporary_client_configuration_rejected")
        self.assertNotIn(IDENTITY, str(error.exception))
        self.assertNotIn(DECRYPTION, str(error.exception))


if __name__ == "__main__":
    unittest.main()
