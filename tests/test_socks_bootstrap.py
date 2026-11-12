"""Published single-file installers carry immutable, verified SOCKS helpers."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from test_installer import REPO, bash_directory, run_bash
from test_installer_menu import script_fixture, execute_fixture


HELPERS = ("socks_exit.sh", "socks_node_probe.py", "socks_exit_job.py", "proxy_input.py", "node_overview.py",
           "panel_release.py", "panel_upgrade.py")


class SocksBootstrap(unittest.TestCase):
    def fixture(self, directory: Path, local: bool = False):
        installer = directory / "install.sh"
        shutil.copyfile(REPO / "install.sh", installer)
        (directory / "run").mkdir()
        if local:
            (directory / "lib").mkdir()
            for name in HELPERS:
                shutil.copyfile(REPO / "lib" / name, directory / "lib" / name)
        return installer

    def load(self, directory: Path):
        # Git Bash on Windows may resolve python3 to a Windows Store alias.
        python_alias = 'python3() { python "$@"; }\n' if os.name == "nt" else ""
        return run_bash(python_alias + r'''
source "$SOCKS_BOOTSTRAP_ROOT/install.sh"
RUN_DIR="$SOCKS_BOOTSTRAP_ROOT/run"
curl() { printf 'UNEXPECTED_NETWORK\n' >&2; return 99; }
socks_exit_load_helpers
printf 'LOADED_HELPERS\n'
''', extra_env={"SOCKS_BOOTSTRAP_ROOT": bash_directory(directory)})

    def assert_extracted(self, directory: Path):
        for name in HELPERS:
            self.assertEqual((directory / "run" / name).read_bytes(),
                             (REPO / "lib" / name).read_bytes(), name)

    def test_standalone_installer_extracts_exact_helpers_without_network(self):
        with tempfile.TemporaryDirectory(prefix="socks-bootstrap-", dir=REPO) as temp:
            directory = Path(temp)
            self.fixture(directory)
            result = self.load(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("LOADED_HELPERS", result.stdout)
            self.assertNotIn("UNEXPECTED_NETWORK", result.stderr)
            self.assert_extracted(directory)

    def test_matching_local_helpers_are_accepted(self):
        with tempfile.TemporaryDirectory(prefix="socks-bootstrap-", dir=REPO) as temp:
            directory = Path(temp)
            self.fixture(directory, local=True)
            result = self.load(directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assert_extracted(directory)

    def test_view_without_job_history_lists_existing_panel_nodes_and_subscription(self):
        with tempfile.TemporaryDirectory(prefix="overview-entry-", dir=REPO) as temp:
            directory = Path(temp)
            self.fixture(directory)
            database = directory / "panel.db"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY,remark TEXT,protocol TEXT,"
                                   "port INTEGER,enable INTEGER,settings TEXT,stream_settings TEXT)")
                connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT)")
                connection.execute("INSERT INTO settings VALUES ('subURI', 'https://sub.example/sub/')")
                connection.execute("INSERT INTO inbounds VALUES (1,?,?,?,?,?,?)", (
                    "Previously-created-node", "vless", 26560, 1,
                    json.dumps({"clients": [{"id": "private-client-uuid", "email": "existing-user",
                                            "enable": True, "subId": "existing-subscription"}]}),
                    json.dumps({"network": "tcp", "security": "reality",
                                "realitySettings": {"privateKey": "private-server-key"}})))
            connection.close()
            before = database.read_bytes()
            alias = 'python3() { python "$@"; }\n' if os.name == "nt" else ""
            result = run_bash(alias + r'''
source "$SOCKS_BOOTSTRAP_ROOT/install.sh"
RUN_DIR="$SOCKS_BOOTSTRAP_ROOT/run"
XUI_DB_PATH="$SOCKS_BOOTSTRAP_ROOT/panel.db"
XUI_INFO_DIR="$SOCKS_BOOTSTRAP_ROOT/info"
curl() { printf 'UNEXPECTED_NETWORK\n'; return 99; }
api_init() { printf 'UNEXPECTED_TOKEN_CREATION\n'; return 98; }
socks_node_status_command
''', extra_env={"SOCKS_BOOTSTRAP_ROOT": bash_directory(directory)})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Previously-created-node", result.stdout)
            self.assertIn("https://sub.example/sub/existing-subscription", result.stdout)
            self.assertIn("不代表面板中没有节点", result.stdout)
            for forbidden in ("no_previous_job", '"operation"', "private-client-uuid",
                              "private-server-key", "UNEXPECTED_NETWORK", "UNEXPECTED_TOKEN_CREATION"):
                self.assertNotIn(forbidden, result.stdout + result.stderr)
            self.assertEqual(database.read_bytes(), before)
            self.assertFalse((directory / "info").exists())

    def test_changed_local_shell_is_rejected_before_execution(self):
        with tempfile.TemporaryDirectory(prefix="socks-bootstrap-", dir=REPO) as temp:
            directory = Path(temp)
            self.fixture(directory, local=True)
            (directory / "lib" / "socks_exit.sh").write_text(
                "printf 'UNEXPECTED_HELPER_EXECUTION\\n'\n", encoding="utf-8")
            result = self.load(directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("校验失败", result.stderr)
            self.assertNotIn("UNEXPECTED_HELPER_EXECUTION", result.stdout + result.stderr)
            self.assertNotIn("UNEXPECTED_NETWORK", result.stderr)

    def test_damaged_embedded_payload_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="socks-bootstrap-", dir=REPO) as temp:
            directory = Path(temp)
            installer = self.fixture(directory)
            source = installer.read_text(encoding="utf-8")
            start = source.index("cat <<'SOCKS_HELPER_PAYLOAD_0'\n")
            start = source.index("\n", start) + 1
            source = source[:start] + "AAAA" + source[start + 4:]
            installer.write_text(source, encoding="utf-8", newline="\n")
            result = self.load(directory)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("LOADED_HELPERS", result.stdout)

    def test_release_check_detects_changed_helper_or_second_entrypoint(self):
        command = [sys.executable, str(REPO / "scripts" / "sync_socks_helpers.py"), "--check"]
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tempfile.TemporaryDirectory(prefix="socks-bundle-") as temp:
            directory = Path(temp)
            (directory / "lib").mkdir()
            for name in HELPERS:
                shutil.copyfile(REPO / "lib" / name, directory / "lib" / name)
            for name in ("install.sh", "install_3xui_xhttp_vlessenc.sh"):
                shutil.copyfile(REPO / name, directory / name)
            changed = directory / "lib" / "socks_node_probe.py"
            changed.write_bytes(changed.read_bytes() + b"\n# Changed release bytes\n")
            result = subprocess.run(command + ["--root", str(directory)], capture_output=True,
                                    text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("stale", result.stderr)
            shutil.copyfile(REPO / "lib" / "socks_node_probe.py", changed)
            second = directory / "install_3xui_xhttp_vlessenc.sh"
            second.write_bytes(second.read_bytes() + b"\n# accidental drift\n")
            result = subprocess.run(command + ["--root", str(directory)], capture_output=True,
                                    text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("install_3xui_xhttp_vlessenc.sh", result.stderr)

    def test_status_command_and_menu_dispatch_without_maintenance_lock(self):
        overrides = r'''
acquire_maintenance_lock() { printf 'UNEXPECTED_LOCK\n'; return 91; }
socks_node_status_command() { printf 'SOCKS_STATUS_SELECTED:%s\n' "$*"; }
'''
        with tempfile.TemporaryDirectory(prefix="socks-status-", dir=REPO) as temp:
            directory = Path(temp)
            path = script_fixture(directory, overrides)
            for args, text in ((("--socks-node-status",), ""), (("--proxy-node-status",), ""),
                               (("--proxy-node-status", "--json"), ""),
                               (("--menu",), "22\n0\n")):
                with self.subTest(args=args):
                    result = execute_fixture(directory, path, args, text)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("SOCKS_STATUS_SELECTED", result.stdout)
                    self.assertNotIn("UNEXPECTED_LOCK", result.stdout + result.stderr)
                    if "--json" in args:
                        self.assertIn("SOCKS_STATUS_SELECTED:--json", result.stdout)

    def test_status_rejects_unknown_arguments_before_platform_setup(self):
        result = run_bash(r'''
source ./install.sh
init_platform() { printf 'UNEXPECTED_PLATFORM\n'; }
main --proxy-node-status --invalid
''')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("UNEXPECTED_PLATFORM", result.stdout)

    def test_proxy_menu_and_legacy_alias_share_the_creation_entry(self):
        overrides = r'''
acquire_maintenance_lock() { printf 'LOCKED\n'; }
socks_exit_command() { printf 'PROXY_CREATE_SELECTED\n'; }
'''
        with tempfile.TemporaryDirectory(prefix="proxy-menu-", dir=REPO) as temp:
            directory = Path(temp)
            path = script_fixture(directory, overrides)
            for args, text in ((("--add-proxy-node",), ""), (("--add-socks-node",), ""),
                               (("--menu",), "21\n0\n")):
                with self.subTest(args=args):
                    result = execute_fixture(directory, path, args, text)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn("PROXY_CREATE_SELECTED", result.stdout)
                    self.assertIn("LOCKED", result.stdout)

    def test_existing_core_menu_actions_keep_their_numbers(self):
        overrides = r'''
menu_xray_status() { :; }
update_xray_core() { printf 'CORE_UPDATE_SELECTED\n'; }
enable_xray_auto_update() { printf 'AUTO_UPDATE_SELECTED\n'; }
socks_exit_command() { printf 'UNEXPECTED_PROXY_CREATE\n'; return 95; }
'''
        with tempfile.TemporaryDirectory(prefix="core-proxy-menu-", dir=REPO) as temp:
            directory = Path(temp)
            result = execute_fixture(directory, script_fixture(directory, overrides),
                                     ("--menu",), "12\n13\n0\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("CORE_UPDATE_SELECTED", result.stdout)
            self.assertIn("AUTO_UPDATE_SELECTED", result.stdout)
            self.assertNotIn("UNEXPECTED_PROXY_CREATE", result.stdout)

    def test_shared_mutation_guard_rejects_unresolved_socks_jobs(self):
        with tempfile.TemporaryDirectory(prefix="socks-guard-", dir=REPO) as temp:
            directory = Path(temp)
            root = directory / "3xuish-socks-exit"
            name = "job-20261112T000000Z-123456789abc"
            job = root / name
            job.mkdir(parents=True)
            (root / "latest.json").write_text(json.dumps({"job": name}), encoding="utf-8")
            alias = 'python3() { python "$@"; }\n' if os.name == "nt" else ""
            for state in ("starting", "running", "manual_recovery_required", "unknown", "complete",
                          "failed", "rolled_back"):
                with self.subTest(state=state):
                    (job / "state.json").write_text(json.dumps({"status": state}), encoding="utf-8")
                    result = run_bash(alias + r'''
source ./install.sh
XUI_INFO_DIR="$SOCKS_GUARD_ROOT"
flock() { :; }
acquire_maintenance_lock
printf 'MUTATION_ALLOWED\n'
''', extra_env={"SOCKS_GUARD_ROOT": bash_directory(directory)})
                    allowed = state in {"complete", "failed", "rolled_back"}
                    self.assertEqual(result.returncode == 0, allowed, result.stderr)
                    self.assertEqual("MUTATION_ALLOWED" in result.stdout, allowed)


if __name__ == "__main__":
    unittest.main()
