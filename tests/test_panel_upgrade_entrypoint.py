"""Panel upgrade menu integration and shared maintenance protection."""
import json
import os
from pathlib import Path
import tempfile
import unittest

from test_installer import REPO, bash_directory, run_bash
from test_installer_menu import execute_fixture, script_fixture


class PanelUpgradeEntrypoint(unittest.TestCase):
    def test_menu_and_cli_delegate_without_taking_the_worker_lock(self):
        overrides = r'''
menu_xray_status() { :; }
acquire_maintenance_lock() { printf 'UNEXPECTED_PARENT_LOCK\n'; return 97; }
panel_upgrade_command() { printf 'PANEL_ACTION:%s\n' "$*"; }
update_xray_core() { printf 'UNEXPECTED_CORE_UPDATE\n'; return 98; }
'''
        with tempfile.TemporaryDirectory(prefix="panel-menu-", dir=REPO) as temp:
            directory = Path(temp)
            fixture = script_fixture(directory, overrides)
            for args, input_text, expected in (
                (("--update-panel",), "", "PANEL_ACTION:launch"),
                (("--panel-update-status",), "", "PANEL_ACTION:status"),
                (("--panel-update-status", "--json"), "", "PANEL_ACTION:status --json"),
                (("--menu",), "23\n24\n0\n", "PANEL_ACTION:launch"),
            ):
                with self.subTest(args=args):
                    result = execute_fixture(directory, fixture, args, input_text)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(expected, result.stdout)
                    if args == ("--menu",):
                        self.assertIn("PANEL_ACTION:status", result.stdout)
                    self.assertNotIn("UNEXPECTED_", result.stdout + result.stderr)

    def test_unknown_upgrade_arguments_fail_before_platform_setup(self):
        for args in ("--update-panel --force", "--panel-update-status --wrong"):
            result = run_bash(r'''
source ./install.sh
init_platform() { printf 'UNEXPECTED_PLATFORM\n'; }
main ''' + args)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("UNEXPECTED_PLATFORM", result.stdout)

    def test_unresolved_panel_upgrade_blocks_other_mutating_commands(self):
        with tempfile.TemporaryDirectory(prefix="panel-mutex-", dir=REPO) as temp:
            directory = Path(temp)
            root = directory / "3xuish-panel-update"
            name = "job-20261112T010101Z-0123456789ab"
            job = root / name
            job.mkdir(parents=True)
            (root / "latest.json").write_text(json.dumps({"job": name}), encoding="utf-8")
            alias = 'python3() { python "$@"; }\n' if os.name == "nt" else ""
            for status in ("starting", "running", "manual_recovery_required", "unknown",
                           "complete", "no_update", "failed", "rolled_back"):
                (job / "state.json").write_text(json.dumps({"status": status}), encoding="utf-8")
                for action in ("--add-nodes", "--update-xray"):
                    with self.subTest(status=status, action=action):
                        result = run_bash(alias + r'''
source ./install.sh
XUI_INFO_DIR="$PANEL_MUTEX_ROOT"
init_platform() { :; }
flock() { :; }
add_nodes_to_existing() { printf 'MUTATION_ALLOWED\n'; }
update_xray_core() { printf 'MUTATION_ALLOWED\n'; }
main "$PANEL_MUTEX_ACTION"
''', extra_env={"PANEL_MUTEX_ROOT": bash_directory(directory), "PANEL_MUTEX_ACTION": action})
                        allowed = status in {"complete", "no_update", "failed", "rolled_back"}
                        self.assertEqual(result.returncode == 0, allowed, result.stderr)
                        self.assertEqual("MUTATION_ALLOWED" in result.stdout, allowed)


if __name__ == "__main__":
    unittest.main()
