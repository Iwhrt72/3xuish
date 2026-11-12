"""Offline coverage for merging one panel-managed node's SOCKS exit routes."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from test_installer import REPO, bash_directory, run_bash


INBOUND_TAG = "managed-node-fixture"
SOCKS_TAG = "managed-exit-fixture"
DNS_TAG = "managed-dns-fixture"
PASSWORD = "fixture-password-must-not-be-logged"


def exit_plan() -> dict:
    return {
        "inboundTag": INBOUND_TAG,
        "outbounds": [
            {
                "tag": SOCKS_TAG,
                "protocol": "socks",
                "settings": {
                    "servers": [{
                        "address": "192.0.2.40",
                        "port": 1080,
                        "users": [{"user": "fixture-user", "pass": PASSWORD}],
                    }],
                },
            },
            {
                "tag": DNS_TAG,
                "protocol": "dns",
                "settings": {"network": "tcp", "address": "1.1.1.1", "port": 53},
                "proxySettings": {"tag": SOCKS_TAG},
            },
        ],
    }


def template(rules: list[dict] | None = None) -> dict:
    return {
        "log": {"loglevel": "warning", "access": "./access.log"},
        "api": {"tag": "api", "services": ["HandlerService", "StatsService"]},
        "inbounds": [{"tag": "api", "listen": "127.0.0.1", "port": 62789}],
        "outbounds": [
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {"tag": "blocked", "protocol": "blackhole", "settings": {}},
            {"tag": "unrelated-exit", "protocol": "freedom", "settings": {}},
        ],
        "dns": {"servers": ["localhost"], "hosts": {"existing.example": "192.0.2.2"}},
        "stats": {},
        "policy": {"system": {"statsOutboundUplink": True}},
        "routing": {
            "domainStrategy": "AsIs",
            "domainMatcher": "hybrid",
            "balancers": [{"tag": "existing-pool", "selector": ["unrelated-"]}],
            "rules": deepcopy(rules if rules is not None else []),
        },
    }


class SocksExitConfigMerge(unittest.TestCase):
    def merge(self, config: dict, plan: dict | None = None):
        """Exercise the public shell function without API, network or service calls."""
        self.assertTrue((REPO / "lib" / "socks_exit.sh").is_file(),
                        "Provisioning implementation lib/socks_exit.sh is missing")
        with tempfile.TemporaryDirectory(prefix="socks-exit-merge-", dir=REPO) as temp:
            directory = Path(temp)
            config_file = directory / "template.json"
            plan_file = directory / "plan.json"
            config_file.write_text(json.dumps(config), encoding="utf-8")
            plan_file.write_text(json.dumps(plan if plan is not None else exit_plan()),
                                 encoding="utf-8")
            before_config, before_plan = config_file.read_bytes(), plan_file.read_bytes()
            result = run_bash(r'''
set -euo pipefail
fail() { printf '%s\n' "$*" >&2; return 1; }
source ./lib/socks_exit.sh
declare -F merge_socks_exit_config >/dev/null
printf 'SOCKS_TEST_MERGE_READY\n' >&2
merge_socks_exit_config "$SOCKS_TEST_ROOT/template.json" "$SOCKS_TEST_ROOT/plan.json"
''', extra_env={"SOCKS_TEST_ROOT": bash_directory(directory)})
            self.assertEqual(config_file.read_bytes(), before_config,
                             "The merge must not mutate the live template input")
            self.assertEqual(plan_file.read_bytes(), before_plan,
                             "The merge must not mutate the provisioning plan")
            self.assertIn("SOCKS_TEST_MERGE_READY", result.stderr,
                          "The merge function was never reached")
        return result

    def merged(self, config: dict, plan: dict | None = None) -> dict:
        result = self.merge(config, plan)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as error:
            self.fail(f"Merge stdout must contain exactly one JSON document: {error}")

    def assert_routes(self, rules: list[dict]) -> None:
        self.assertEqual(len(rules), 2)
        dns, proxy = rules
        self.assertEqual(dns.get("inboundTag"), [INBOUND_TAG])
        self.assertEqual(dns.get("network"), "udp")
        self.assertEqual(str(dns.get("port")), "53")
        self.assertEqual(dns.get("outboundTag"), DNS_TAG)
        self.assertEqual(proxy.get("inboundTag"), [INBOUND_TAG])
        self.assertEqual(proxy.get("outboundTag"), SOCKS_TAG)
        # No destination constraint may silently leave part of this node on direct.
        for field in ("domain", "ip", "port", "protocol", "source", "sourcePort", "user"):
            self.assertNotIn(field, proxy)
        if "network" in proxy:
            self.assertEqual({part.strip() for part in proxy["network"].split(",")},
                             {"tcp", "udp"})

    def assert_rejected(self, config: dict, plan: dict | None = None) -> None:
        result = self.merge(config, plan)
        self.assertNotEqual(result.returncode, 0, "Conflicting provisioning was accepted")
        self.assertNotIn(PASSWORD, result.stdout + result.stderr,
                         "Errors must not disclose the upstream password")
        # A failed merge must not emit a usable replacement document.
        try:
            document = json.loads(result.stdout)
        except json.JSONDecodeError:
            return
        self.assertFalse(isinstance(document, dict) and "outbounds" in document,
                         "A failed merge emitted a replacement config")

    def test_preserves_existing_configuration_and_inserts_before_network_fallback(self):
        existing_rules = [
            {"type": "field", "inboundTag": ["api"], "outboundTag": "api"},
            {"type": "field", "ip": ["geoip:private"], "outboundTag": "blocked"},
            {"type": "field", "protocol": ["bittorrent"], "outboundTag": "blocked"},
            {"type": "field", "domain": ["domain:existing.example"],
             "network": "tcp,udp", "outboundTag": "unrelated-exit"},
            {"type": "field", "inboundTag": ["another-node"], "outboundTag": "unrelated-exit"},
            {"type": "field", "network": "tcp,udp", "outboundTag": "direct"},
        ]
        config = template(existing_rules)
        merged = self.merged(config)
        for key, value in config.items():
            if key not in ("outbounds", "routing"):
                self.assertEqual(merged[key], value, f"Unrelated section {key} changed")
        self.assertEqual(merged["outbounds"], config["outbounds"] + exit_plan()["outbounds"])
        self.assertEqual({k: v for k, v in merged["routing"].items() if k != "rules"},
                         {k: v for k, v in config["routing"].items() if k != "rules"})
        rules = merged["routing"]["rules"]
        self.assertEqual(rules[:5], existing_rules[:5])
        self.assert_routes(rules[5:7])
        self.assertEqual(rules[7:], existing_rules[5:])

    def test_fallback_detection_handles_network_order_and_unconditional_rule(self):
        for fallback in (
            {"type": "field", "network": "udp,tcp", "outboundTag": "direct"},
            {"type": "field", "outboundTag": "direct"},
        ):
            with self.subTest(fallback=fallback):
                trailing = {"type": "field", "domain": ["domain:later.example"],
                            "outboundTag": "unrelated-exit"}
                config = template([fallback, trailing])
                rules = self.merged(config)["routing"]["rules"]
                self.assert_routes(rules[:2])
                self.assertEqual(rules[2:], [fallback, trailing])

    def test_scoped_network_rules_are_not_mistaken_for_global_fallback(self):
        for condition in (
            {"ip": ["geoip:private"]},
            {"domain": ["domain:example.test"]},
            {"protocol": ["bittorrent"]},
            {"inboundTag": ["other-node"]},
            {"port": "25"},
            {"source": ["192.0.2.10"]},
            {"user": ["other-client"]},
        ):
            with self.subTest(condition=condition):
                scoped = {"type": "field", "network": "tcp,udp",
                          "outboundTag": "blocked", **condition}
                config = template([scoped])
                rules = self.merged(config)["routing"]["rules"]
                self.assertEqual(rules[0], scoped)
                self.assert_routes(rules[1:])

    def test_without_fallback_appends_after_existing_rules(self):
        existing = [{"type": "field", "network": "tcp", "outboundTag": "direct"}]
        rules = self.merged(template(existing))["routing"]["rules"]
        self.assertEqual(rules[0], existing[0])
        self.assert_routes(rules[1:])

    def test_empty_rule_list_gets_both_node_scoped_routes(self):
        self.assert_routes(self.merged(template())["routing"]["rules"])

    def test_rejects_collision_with_either_existing_outbound_tag(self):
        for tag in (SOCKS_TAG, DNS_TAG):
            with self.subTest(tag=tag):
                config = template()
                config["outbounds"].append({"tag": tag, "protocol": "freedom"})
                self.assert_rejected(config)

    def test_rejects_duplicate_outbound_tags_inside_plan(self):
        plan = exit_plan()
        plan["outbounds"][1]["tag"] = SOCKS_TAG
        self.assert_rejected(template(), plan)

    def test_rejects_existing_route_for_managed_inbound_in_any_tag_position(self):
        for inbound_tags in ([INBOUND_TAG], ["other-node", INBOUND_TAG],
                             [INBOUND_TAG, "other-node"]):
            for outbound_tag in (SOCKS_TAG, DNS_TAG, "unrelated-exit"):
                with self.subTest(inbound_tags=inbound_tags, outbound_tag=outbound_tag):
                    rule = {"type": "field", "inboundTag": inbound_tags,
                            "outboundTag": outbound_tag}
                    self.assert_rejected(template([rule]))

    def test_rejects_second_merge_instead_of_duplicating_managed_routes(self):
        first = self.merged(template())
        self.assert_rejected(first)


if __name__ == "__main__":
    unittest.main()
