"""Read-only node inventory and existing subscription URL presentation."""
import importlib.util
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("node_overview", REPO / "lib/node_overview.py")
overview = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(overview)


class NodeOverview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="node-overview-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = self.root / "panel.db"
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE settings(key TEXT,value TEXT)")
            db.execute("""CREATE TABLE inbounds(id INTEGER PRIMARY KEY,remark TEXT,protocol TEXT,
                       port INTEGER,enable INTEGER,settings TEXT,stream_settings TEXT,
                       expiry_time INTEGER DEFAULT 0,up INTEGER DEFAULT 0,down INTEGER DEFAULT 0,
                       total INTEGER DEFAULT 0,exclude_from_sub INTEGER DEFAULT 0)""")

    def setting(self, **settings):
        with closing(sqlite3.connect(self.database)) as db, db:
            for key, value in settings.items():
                db.execute("DELETE FROM settings WHERE key=?", (key,))
                db.execute("INSERT INTO settings VALUES(?,?)", (key, str(value)))

    def node(self, identity=1, name="手工节点", protocol="vless", enabled=1, clients=None,
             settings=None, stream=None, **extra):
        if clients is None:
            clients = [{"id": "private-client-uuid", "password": "private-client-password",
                        "email": "user@example.test", "subId": "subscription-one", "enable": True}]
        row = {"id": identity, "remark": name, "protocol": protocol, "port": 26000 + identity,
               "enable": enabled, "settings": settings if settings is not None else json.dumps({"clients": clients}),
               "stream_settings": json.dumps(stream or {"network": "tcp", "security": "reality",
                 "realitySettings": {"serverNames": ["www.apple.com"], "privateKey": "private-server-key"}}),
               **extra}
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO inbounds(" + ",".join(row) + ") VALUES(" + ",".join("?" for _ in row) + ")", tuple(row.values()))

    def report(self, host="203.0.113.42", **kwargs):
        return overview.build_report(self.database, self.root, host=host, now_ms=2_000_000_000_000, **kwargs)

    def test_no_job_still_lists_every_current_panel_node_and_changes_no_file(self):
        for identity, protocol in enumerate(("vless", "vmess", "trojan", "hysteria", "shadowsocks"), 1):
            self.node(identity, name="SOCKS5-proxy" if identity == 5 else "旧节点", protocol=protocol)
        before = {path.name: path.read_bytes() for path in self.root.iterdir()}
        report = self.report(status={"status": "no_previous_job", "operation": "socks_exit"})
        self.assertEqual(len(report["nodes"]), 5)
        self.assertEqual(report["task"]["status"], "no_previous_job")
        rendered = overview.render_report(report)
        self.assertIn("这不代表面板中没有节点", rendered)
        self.assertIn("当前面板共 5 个", rendered)
        self.assertEqual({path.name: path.read_bytes() for path in self.root.iterdir()}, before)

    def test_shared_subscription_is_grouped_without_mutating_client_ids(self):
        self.node(1)
        self.node(2, protocol="hysteria")
        groups = self.report()["subscriptions"]
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["nodes"], [1, 2])
        self.assertEqual(groups[0]["url"], "http://203.0.113.42:2096/sub/subscription-one")
        self.assertEqual(groups[0]["clash_url"], "")

    def test_database_secrets_and_terminal_controls_are_not_exposed(self):
        self.node(name="node\x1b[2J\nnext", clients=[{"id": "private-client-uuid", "password": "private-client-password",
                  "email": "client\x1b[31m", "subId": "subscription-one", "enable": True}])
        report = self.report()
        combined = overview.render_report(report) + json.dumps(report, ensure_ascii=False)
        for secret in ("private-client-uuid", "private-client-password", "private-server-key", "www.apple.com", "\x1b"):
            self.assertNotIn(secret, combined)

    def test_missing_database_is_not_created_and_not_reported_as_empty_panel(self):
        missing = self.root / "missing.db"
        report = overview.build_report(missing, self.root)
        self.assertEqual(report["database_status"], "missing")
        self.assertFalse(missing.exists())
        self.assertNotIn("当前面板共 0 个", overview.render_report(report))

    def test_malformed_single_node_does_not_hide_valid_nodes(self):
        self.node(1, settings="not-json")
        self.node(2)
        report = self.report()
        self.assertEqual(len(report["nodes"]), 2)
        self.assertEqual(report["subscriptions"][0]["nodes"], [2])
        self.assertTrue(report["warnings"])

    def test_disabled_subscription_does_not_print_usable_urls(self):
        self.node()
        self.setting(subEnable="false", subClashEnable="true")
        group = self.report()["subscriptions"][0]
        self.assertEqual(group["state"], "disabled")
        self.assertFalse(group["url"] or group["clash_url"])

    def test_missing_subscription_id_has_actionable_reason(self):
        self.node(clients=[{"id": "secret", "email": "manual-client"}])
        report = self.report()
        self.assertFalse(report["subscriptions"])
        self.assertIn("未设置有效订阅", report["nodes"][0]["clients"][0]["subscription_reason"])

    def test_expired_disabled_and_excluded_are_not_shown_as_active(self):
        self.node(1, enabled=0)
        self.node(2, exclude_from_sub=1, clients=[{"email": "expired", "expiryTime": 1000, "subId": "old-sub"}])
        report = self.report()
        self.assertEqual(report["nodes"][0]["state"], "已停用")
        self.assertTrue(report["nodes"][1]["subscription_excluded"])
        self.assertEqual(report["nodes"][1]["clients"][0]["state"], "已到期")
        self.assertIn("排除订阅", report["nodes"][1]["clients"][0]["subscription_reason"])

    def test_inbound_quota_exhaustion_uses_actual_counters(self):
        self.node(total=100, up=80, down=20)
        self.assertEqual(self.report()["nodes"][0]["state"], "流量已用尽")

    def test_custom_uri_base_path_and_clash_external_origin(self):
        self.node()
        self.setting(subURI="https://public.example:8443/reverse/custom/", subClashEnable="true", subClashPath="/mihomo/")
        group = self.report(host="")["subscriptions"][0]
        self.assertEqual(group["url"], "https://public.example:8443/reverse/custom/subscription-one")
        self.assertEqual(group["clash_url"], "https://public.example:8443/mihomo/subscription-one")

    def test_clash_uri_can_override_public_host_and_path(self):
        self.node()
        self.setting(subClashEnable="true", subClashURI="https://clash.example/custom")
        self.assertEqual(self.report()["subscriptions"][0]["clash_url"], "https://clash.example/custom/subscription-one")

    def test_invalid_clash_address_does_not_hide_valid_general_subscription(self):
        self.node()
        self.setting(subClashEnable="true", subClashURI="javascript:bad")
        report = self.report()
        self.assertTrue(report["subscriptions"][0]["url"])
        self.assertEqual(report["subscriptions"][0]["state"], "configured")
        self.assertFalse(report["subscriptions"][0]["clash_url"])
        self.assertTrue(any("Clash/Mihomo" in value for value in report["warnings"]))

    def test_unsupported_protocol_is_listed_without_claiming_subscription_works(self):
        self.node(protocol="masque")
        report = self.report()
        self.assertFalse(report["nodes"][0]["subscription_supported"])
        self.assertIn("导出能力未确认", report["nodes"][0]["clients"][0]["subscription_reason"])
        self.assertEqual(report["subscriptions"][0]["state"], "configured")

    def test_xdrive_is_listed_without_claiming_raw_subscription_support(self):
        self.node(stream={"network": "xdrive", "security": "none"})
        self.assertFalse(self.report()["nodes"][0]["subscription_supported"])

    def test_ipv6_brackets_tls_and_default_port(self):
        self.node()
        self.setting(subCertFile="/cert.pem", subKeyFile="/key.pem", subPort="443")
        url = self.report(host="2001:db8::10")["subscriptions"][0]["url"]
        self.assertEqual(url, "https://[2001:db8::10]/sub/subscription-one")

    def test_sub_domain_host_restriction_takes_priority_over_detected_ip(self):
        self.node()
        self.setting(subDomain="subscribe.example")
        self.assertEqual(self.report()["subscriptions"][0]["url"], "http://subscribe.example:2096/sub/subscription-one")

    def test_missing_host_does_not_reuse_reality_server_name(self):
        self.node()
        group = self.report(host="")["subscriptions"][0]
        self.assertFalse(group["url"])
        self.assertIn("无法确定", group["reason"])

    def test_saved_profile_uses_server_address_not_sni(self):
        self.node()
        (self.root / "3x-ui-node-profiles.json").write_text(json.dumps([{"clientOutbound": {
            "settings": {"address": "server.example"}, "streamSettings": {"realitySettings": {"serverName": "www.apple.com"}}}}]), encoding="utf-8")
        self.assertEqual(self.report(host="")["subscriptions"][0]["url"], "http://server.example:2096/sub/subscription-one")

    def test_tls_subscription_prefers_saved_tls_certificate_name_over_server_ip(self):
        self.node()
        self.setting(subCertFile="/domain.pem", subKeyFile="/domain.key")
        profiles = [{"clientOutbound": {"settings": {"address": "203.0.113.42"},
                    "streamSettings": {"security": "reality", "realitySettings": {"serverName": "www.apple.com"}}}},
                    {"clientOutbound": {"settings": {"address": "203.0.113.42"},
                    "streamSettings": {"security": "tls", "tlsSettings": {"serverName": "cert.example"}}}}]
        (self.root / "3x-ui-node-profiles.json").write_text(json.dumps(profiles), encoding="utf-8")
        self.assertEqual(self.report(host="")["subscriptions"][0]["url"], "https://cert.example:2096/sub/subscription-one")

    def test_invalid_sub_domain_does_not_fall_back_to_explicit_ip(self):
        self.node()
        self.setting(subDomain="https://not-a-host")
        report = self.report()
        self.assertFalse(report["subscriptions"][0]["url"])
        self.assertTrue(any("域名限制" in warning for warning in report["warnings"]))

    def test_invalid_public_uri_never_becomes_an_available_url(self):
        self.node()
        for uri in ("javascript:alert(1)", "https://user:password@host.example/sub", "https://host.example/sub?token=secret", "https://host.example/\nheader"):
            with self.subTest(uri=uri):
                self.setting(subURI=uri)
                report = self.report()
                self.assertFalse(report["subscriptions"][0]["url"])
                self.assertNotIn("token=secret", overview.render_report(report))

    def test_safe_nonstandard_subscription_id_is_encoded_instead_of_hidden(self):
        self.node(clients=[{"email": "legacy", "subId": "short+id"}])
        self.assertTrue(self.report()["subscriptions"][0]["url"].endswith("/short%2Bid"))

    def test_normalized_clients_and_associations_override_stale_json(self):
        self.node(1)
        self.node(2, settings="broken-legacy-json")
        self.node(3)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("CREATE TABLE clients(id INTEGER PRIMARY KEY,email TEXT,sub_id TEXT,enable INTEGER,expiry_time INTEGER,total_gb INTEGER,password TEXT)")
            db.execute("CREATE TABLE client_inbounds(client_id INTEGER,inbound_id INTEGER)")
            db.execute("INSERT INTO clients VALUES(7,'current-client','current-sub',0,0,0,'normalized-secret')")
            db.executemany("INSERT INTO client_inbounds VALUES(7,?)", ((1,), (2,)))
        report = self.report()
        self.assertEqual(len(report["subscriptions"]), 1)
        self.assertEqual(report["subscriptions"][0]["nodes"], [1, 2])
        self.assertTrue(report["subscriptions"][0]["url"].endswith("/current-sub"))
        self.assertEqual(report["nodes"][0]["clients"][0]["state"], "已停用")
        self.assertFalse(report["nodes"][2]["clients"])
        self.assertNotIn("normalized-secret", json.dumps(report))
        self.assertNotIn("subscription-one", json.dumps(report))

    def test_state_only_does_not_need_a_database_and_never_prints_raw_state(self):
        state = self.root / "state.json"
        state.write_text(json.dumps({"status": "complete", "inbound_id": "9", "port": "26600",
                                    "password": "never-print-this", "exit_ip": "203.0.113.10"}), encoding="utf-8")
        result = subprocess.run([sys.executable, str(REPO / "lib/node_overview.py"),
            "--database", str(self.root / "absent.db"), "--info-dir", str(self.root),
            "--status-file", str(state), "--state-only"], capture_output=True, text=True, encoding="utf-8",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("创建完成", result.stdout)
        self.assertIn("26600", result.stdout)
        self.assertNotIn("never-print-this", result.stdout)
        self.assertNotIn('"status"', result.stdout)

    def test_invalid_task_status_is_safe(self):
        self.assertIn("暂时无法读取", overview.render_task({"status": []}))


if __name__ == "__main__":
    unittest.main()
