"""Reject saved import links after the live panel configuration has changed."""
from __future__ import annotations

import copy
import json
from pathlib import Path
import tempfile
import unittest

import test_existing_installation as existing
from test_installer import REPO, run_bash


class RuntimeDrift(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temporary = tempfile.TemporaryDirectory(prefix="runtime-drift-", dir=REPO)
        cls.addClassCleanup(temporary.cleanup)
        cls.directory = Path(temporary.name)
        cls.fixture = existing.ExistingInstallation()
        cls.fixture.fixture(cls.directory)
        result = run_bash(existing.SHELL_FIXTURE + "\nadd_nodes_to_existing >/dev/null\n",
                          extra_env=cls.fixture.env(cls.directory))
        if result.returncode:
            raise AssertionError(result.stderr[-1500:])
        cls.runtime_path = cls.directory / "server/bin/config.json"
        cls.runtime = json.loads(cls.runtime_path.read_text(encoding="utf-8"))
        cls.profiles_path = cls.directory / "info/3x-ui-node-profiles.json"
        cls.profiles_bytes = cls.profiles_path.read_bytes()
        cls.profiles = {item["key"]: item for item in json.loads(cls.profiles_bytes)}
        cls.original_rows = cls.fixture.rows(cls.directory)
        cls.original_events = (cls.directory / "api-events").read_bytes()

    def mutate_and_repeat(self, key, transform, *, accepted=False):
        runtime = copy.deepcopy(self.runtime)
        target = next(row for row in runtime["inbounds"]
                      if row["tag"] == self.profiles[key]["payload"]["tag"])
        transform(target)
        self.runtime_path.write_text(json.dumps(runtime), encoding="utf-8")
        source = existing.SHELL_FIXTURE + r'''
print_summary() { printf 'SUMMARY_WAS_PRINTED\n'; }
if add_nodes_to_existing; then
    printf 'REPEAT_ACCEPTED\n'
else
    printf 'REPEAT_REJECTED\n'
fi
'''
        result = run_bash(source, extra_env=self.fixture.env(self.directory))
        self.assertEqual(result.returncode, 0, result.stderr[-1500:])
        if accepted:
            self.assertIn("REPEAT_ACCEPTED", result.stdout)
            self.assertIn("SUMMARY_WAS_PRINTED", result.stdout)
        else:
            self.assertIn("REPEAT_REJECTED", result.stdout)
            self.assertNotIn("REPEAT_ACCEPTED", result.stdout)
            self.assertNotIn("SUMMARY_WAS_PRINTED", result.stdout)
        self.assertEqual(self.fixture.rows(self.directory), self.original_rows)
        self.assertEqual(self.profiles_path.read_bytes(), self.profiles_bytes)
        self.assertEqual((self.directory / "api-events").read_bytes(), self.original_events)

    def test_changed_xhttp_path_rejects_saved_links(self):
        self.mutate_and_repeat("xhttp", lambda node: node["streamSettings"]["xhttpSettings"].update(
            path="/changed-in-panel"))

    def test_changed_tls_identity_rejects_saved_links(self):
        for field in ("serverName", "certificateFile", "keyFile"):
            with self.subTest(field=field):
                def change(node):
                    tls = node["streamSettings"]["tlsSettings"]
                    if field == "serverName":
                        tls[field] = "changed.example.test"
                    else:
                        tls["certificates"][0][field] = "/different/" + field + ".pem"
                self.mutate_and_repeat("raw", change)

    def test_changed_reality_identity_rejects_saved_links(self):
        changes = {"privateKey": "different-private-key", "serverNames": ["changed.example.test"],
                   "shortIds": ["fedcba9876543210"], "target": "changed.example.test:443"}
        for field, value in changes.items():
            with self.subTest(field=field):
                self.mutate_and_repeat("reality", lambda node: node["streamSettings"]["realitySettings"].update(
                    {field: value}))

    def test_changed_xhttp_mode_or_encryption_rejects_saved_links(self):
        with self.subTest(field="mode"):
            self.mutate_and_repeat("xhttp", lambda node: node["streamSettings"]["xhttpSettings"].update(
                mode="stream-one"))
        with self.subTest(field="decryption"):
            self.mutate_and_repeat("xhttp", lambda node: node["settings"].update(
                decryption="changed-encryption-key"))

    def test_equivalent_reality_destination_alias_accepts_saved_links(self):
        def change(node):
            reality = node["streamSettings"]["realitySettings"]
            reality["dest"] = reality.pop("target")
        self.mutate_and_repeat("reality", change, accepted=True)


if __name__ == "__main__":
    unittest.main()
