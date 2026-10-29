"""Interactive dispatch must never implicitly install or weaken action failures."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest

from test_installer import REPO, bash_binary, bash_directory, run_bash


def script_fixture(directory, overrides=""):
    """Keep real dispatch/menu code and replace action effects in the child too."""
    source = (REPO / "install.sh").read_text(encoding="utf-8")
    footer = source.rfind("\nif [[")
    if footer < 0 or '    main "$@"' not in source[footer:]:
        raise AssertionError("Expected executable entry guard")
    source = source[:footer]
    source += r'''
init_platform() {
    RUN_DIR=$(mktemp -d "$TEST_MOCK_DIR/run.XXXXXXXX")
}
install_deps() { printf 'UNEXPECTED_DEPENDENCIES\n'; return 77; }
''' + overrides + r'''
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
'''
    path = directory / "install.sh"
    path.write_text(source, encoding="utf-8", newline="\n")
    return path


def execute_fixture(directory, path, arguments=(), input_text=""):
    environment = os.environ.copy()
    environment.update(TEST_MOCK_DIR=bash_directory(directory), TEST_SCRIPT=bash_directory(path),
                       XUI_DB_PATH=bash_directory(directory / "live.db"), AUTO_YES="0")
    result = subprocess.run([bash_binary(), "-lc", 'bash "$TEST_SCRIPT" "$@"', "fixture", *arguments],
                            cwd=REPO, input=input_text.encode("utf-8"), capture_output=True,
                            timeout=15, env=environment)
    result.stdout = result.stdout.decode("utf-8", errors="replace")
    result.stderr = result.stderr.decode("utf-8", errors="replace")
    return result


class InstallerDispatch(unittest.TestCase):
    def test_unknown_option_is_rejected_before_platform_or_dependencies(self):
        result = run_bash(r'''
source ./install.sh
init_platform() { printf 'UNEXPECTED_PLATFORM\n'; }
install_deps() { printf 'UNEXPECTED_DEPENDENCIES\n'; return 71; }
main --not-a-real-option
''')
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("UNEXPECTED_PLATFORM", result.stdout + result.stderr)
        self.assertNotIn("UNEXPECTED_DEPENDENCIES", result.stdout + result.stderr)

    def test_existing_database_is_rejected_before_dependencies_or_configuration(self):
        with tempfile.TemporaryDirectory(prefix="installer-menu-", dir=REPO) as temp:
            directory = Path(temp)
            database = directory / "live.db"
            database.write_bytes(b"existing-panel-database")
            result = run_bash(r'''
source ./install.sh
XUI_DB_PATH="$TEST_MOCK_DIR/live.db"
init_platform() { RUN_DIR="$TEST_MOCK_DIR"; }
install_deps() { printf 'UNEXPECTED_DEPENDENCIES\n'; return 72; }
init_config() { printf 'UNEXPECTED_CONFIGURATION\n'; return 73; }
main --install
''', extra_env={"TEST_MOCK_DIR": bash_directory(directory)})
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("UNEXPECTED_DEPENDENCIES", result.stdout + result.stderr)
            self.assertNotIn("UNEXPECTED_CONFIGURATION", result.stdout + result.stderr)
            self.assertEqual(database.read_bytes(), b"existing-panel-database")

    def test_unattended_no_arguments_still_selects_fresh_install(self):
        with tempfile.TemporaryDirectory(prefix="installer-menu-", dir=REPO) as temp:
            result = run_bash(r'''
source ./install.sh
XUI_DB_PATH="$TEST_MOCK_DIR/does-not-exist.db"
AUTO_YES=1
init_platform() { RUN_DIR="$TEST_MOCK_DIR"; }
install_deps() { printf 'FRESH_INSTALL_SELECTED\n'; return 74; }
prompt_read() { printf 'UNEXPECTED_PROMPT\n'; return 75; }
main
''', extra_env={"TEST_MOCK_DIR": bash_directory(Path(temp))})
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FRESH_INSTALL_SELECTED", result.stdout)
            self.assertNotIn("UNEXPECTED_PROMPT", result.stdout + result.stderr)


class InstallerMenu(unittest.TestCase):
    def test_no_arguments_with_existing_panel_only_opens_menu_and_exits(self):
        with tempfile.TemporaryDirectory(prefix="installer-menu-", dir=REPO) as temp:
            directory = Path(temp)
            database = directory / "live.db"
            database.write_bytes(b"existing-panel-database")
            path = script_fixture(directory)
            result = execute_fixture(directory, path, input_text="0\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("3xuish 管理菜单", result.stdout)
            self.assertIn("测速", result.stdout)
            self.assertNotIn("UNEXPECTED_DEPENDENCIES", result.stdout + result.stderr)
            self.assertEqual(database.read_bytes(), b"existing-panel-database")

    def test_explicit_menu_eof_exits_without_installation_or_looping(self):
        with tempfile.TemporaryDirectory(prefix="installer-menu-", dir=REPO) as temp:
            directory = Path(temp)
            result = execute_fixture(directory, script_fixture(directory), ("--menu",))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertLessEqual(result.stdout.count("3xuish 管理菜单"), 2)
            self.assertNotIn("UNEXPECTED_DEPENDENCIES", result.stdout + result.stderr)

    def test_menu_dispatches_each_existing_action_and_returns_to_menu(self):
        overrides = r'''
speed_test_command() { printf 'ACTION_SPEED:%s\n' "$*"; }
performance_command() { printf 'ACTION_PERFORMANCE:%s\n' "$*"; }
add_nodes_to_existing() { printf 'ACTION_ADD_NODES\n'; }
switch_existing_to_domain() { printf 'ACTION_DOMAIN:%s\n' "$1"; }
'''
        with tempfile.TemporaryDirectory(prefix="installer-menu-", dir=REPO) as temp:
            directory = Path(temp)
            result = execute_fixture(directory, script_fixture(directory, overrides), ("--menu",),
                                     "2\n3\n4\n5\n6\nexample.test\n7\n0\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            for marker in ("ACTION_SPEED:", "ACTION_PERFORMANCE:check", "ACTION_PERFORMANCE:repair",
                           "ACTION_ADD_NODES", "ACTION_DOMAIN:example.test", "ACTION_PERFORMANCE:status"):
                self.assertIn(marker, result.stdout, result.stdout)
            self.assertGreaterEqual(result.stdout.count("3xuish 管理菜单"), 7)
            self.assertNotIn("UNEXPECTED_DEPENDENCIES", result.stdout + result.stderr)

    def test_failed_mutating_child_keeps_errexit_and_menu_survives(self):
        overrides = r'''
fresh_install() {
    printf 'MUTATING_ACTION_STARTED\n'
    false
    printf 'UNSAFE_CONTINUED_AFTER_FAILURE\n'
}
'''
        with tempfile.TemporaryDirectory(prefix="installer-menu-", dir=REPO) as temp:
            directory = Path(temp)
            result = execute_fixture(directory, script_fixture(directory, overrides), ("--menu",), "1\n0\n")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("MUTATING_ACTION_STARTED", result.stdout)
            self.assertNotIn("UNSAFE_CONTINUED_AFTER_FAILURE", result.stdout + result.stderr)
            self.assertIn("退出码", result.stdout + result.stderr)
            self.assertGreaterEqual(result.stdout.count("3xuish 管理菜单"), 2)

    @unittest.skipUnless(os.name == "posix", "PTY menu check requires POSIX")
    def test_no_argument_terminal_input_uses_the_menu_with_existing_panel(self):
        import pty
        with tempfile.TemporaryDirectory(prefix="installer-menu-", dir=REPO) as temp:
            directory = Path(temp)
            path = script_fixture(directory)
            (directory / "live.db").write_bytes(b"existing-panel-database")
            environment = dict(os.environ, TEST_MOCK_DIR=str(directory), AUTO_YES="0",
                               XUI_DB_PATH=str(directory / "live.db"))
            master, slave = pty.openpty()
            try:
                process = subprocess.Popen([bash_binary(), str(path)], stdin=slave, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE, env=environment, text=True)
                os.close(slave)
                slave = None
                os.write(master, b"0\n")
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
                self.assertIn("3xuish 管理菜单", stdout)
                self.assertNotIn("UNEXPECTED_DEPENDENCIES", stdout + stderr)
            finally:
                if slave is not None:
                    os.close(slave)
                os.close(master)
                if 'process' in locals() and process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


class SpeedMenuEntrypoint(unittest.TestCase):
    def test_installer_pins_exact_speed_bootstrap_bytes(self):
        script = (REPO / "install.sh").read_text(encoding="utf-8")
        match = re.search(r'^SPEED_TEST_SHA256="([a-f0-9]{64})"$', script, re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match[1], hashlib.sha256((REPO / "scripts/server-speed-test.sh").read_bytes()).hexdigest())

    def test_speed_entrypoint_forwards_arguments_and_existing_paths_without_installation(self):
        bootstrap = r'''#!/usr/bin/env bash
printf 'FORWARDED_DB:%s\n' "$XUI_DB_PATH"
printf 'FORWARDED_INSTALL:%s\n' "$XUI_INSTALL_DIR"
printf 'FORWARDED_INFO:%s\n' "$XUI_INFO_DIR"
printf 'FORWARDED_XRAY:%s\n' "${XRAY_BIN:-}"
printf 'FORWARDED_ARG:%s\n' "$@"
'''
        with tempfile.TemporaryDirectory(prefix="installer-menu-", dir=REPO) as temp:
            directory = Path(temp)
            scripts = directory / "scripts"
            scripts.mkdir()
            (scripts / "server-speed-test.sh").write_text(bootstrap, encoding="utf-8", newline="\n")
            digest = hashlib.sha256((scripts / "server-speed-test.sh").read_bytes()).hexdigest()
            overrides = 'SPEED_TEST_SHA256="' + digest + '"\n' + r'''
XUI_INSTALL_DIR="$TEST_MOCK_DIR/custom-install"
XUI_INFO_DIR="$TEST_MOCK_DIR/custom-info"
XRAY_BIN="$TEST_MOCK_DIR/custom-xray"
'''
            result = execute_fixture(directory, script_fixture(directory, overrides),
                                     ("--speed-test", "--seconds", "5", "--direct-only"))
            self.assertEqual(result.returncode, 0, result.stderr)
            for name, suffix in (("DB", "live.db"), ("INSTALL", "custom-install"),
                                 ("INFO", "custom-info"), ("XRAY", "custom-xray")):
                self.assertIn("FORWARDED_" + name + ":" + bash_directory(directory / suffix), result.stdout)
            for argument in ("--seconds", "5", "--direct-only"):
                self.assertIn("FORWARDED_ARG:" + argument, result.stdout)
            self.assertNotIn("UNEXPECTED_DEPENDENCIES", result.stdout + result.stderr)

    def test_changed_speed_bootstrap_is_rejected_before_it_executes(self):
        with tempfile.TemporaryDirectory(prefix="installer-menu-", dir=REPO) as temp:
            directory = Path(temp)
            scripts = directory / "scripts"
            scripts.mkdir()
            (scripts / "server-speed-test.sh").write_text("printf 'UNEXPECTED_HELPER_EXECUTED\\n'\n",
                                                           encoding="utf-8")
            result = execute_fixture(directory, script_fixture(directory), ("--speed-test", "--help"))
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertNotIn("UNEXPECTED_HELPER_EXECUTED", result.stdout + result.stderr)
            self.assertNotIn("UNEXPECTED_DEPENDENCIES", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
