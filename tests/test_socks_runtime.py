"""A hot-added inbound does not have to appear in the core startup file."""
import unittest

from test_installer import run_bash


class SocksRuntimeReadiness(unittest.TestCase):
    def check(self, enabled=True, listening=True):
        return run_bash(r'''
source ./install.sh
source ./lib/socks_exit.sh
exit_id=8
exit_tag=hot-inbound
EXIT_NODE_PORT=26600
# No config file or core process is needed to model this panel response.
XUI_INSTALL_DIR=/does-not-exist
proxy_exit_find_inbound() {
    printf '{"id":8,"tag":"hot-inbound","port":26600,"enable":%s}\n' "$TEST_ENABLED"
}
ss() { if [[ "$TEST_LISTENING" == true ]]; then printf 'LISTEN 0 4096 *:26600 *:*\n'; fi; }
sleep() { :; }
proxy_exit_running
''', extra_env={"TEST_ENABLED": str(enabled).lower(), "TEST_LISTENING": str(listening).lower()})

    def test_hot_enabled_listener_is_ready_without_startup_file(self):
        result = self.check()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_disabled_or_missing_listener_is_not_ready(self):
        for enabled, listening in ((False, True), (True, False)):
            with self.subTest(enabled=enabled, listening=listening):
                self.assertNotEqual(self.check(enabled, listening).returncode, 0)


if __name__ == "__main__":
    unittest.main()
