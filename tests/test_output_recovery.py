"""Exercise the real output writer through existing-panel deployment cleanup."""
from pathlib import Path
import tempfile
import unittest

import test_existing_installation as existing
from test_installer import REPO, run_bash


class OutputRecovery(unittest.TestCase):
    def test_each_output_failure_rolls_back_nodes_and_restores_old_files(self):
        helper = existing.ExistingInstallation()
        names = ("3x-ui-node-profiles.json", "3x-ui-node-links.txt",
                 "3x-ui-nodes-info.txt", "3x-ui-xhttp-info.txt")
        for name in names:
            with self.subTest(file=name), tempfile.TemporaryDirectory(
                    prefix="installer-existing-", dir=REPO) as temp:
                directory = Path(temp)
                helper.fixture(directory)
                original_rows = helper.rows(directory)
                info = directory / "info"
                original_files = {p.name: p.read_bytes() for p in info.iterdir()}
                source = existing.SHELL_FIXTURE + r'''
install() {
    if [[ "$1" == -d && "$2" == -m && "$3" == 700 ]]; then
        mkdir -p "$4"
    elif [[ "${*: -1}" == "$XUI_INFO_DIR/$TEST_FAIL_FILE" &&
            ! -f "$TEST_MOCK_DIR/output-failed" ]]; then
        : >"$TEST_MOCK_DIR/output-failed"
        return 42
    else
        command install "$@"
    fi
}
trap cleanup_install EXIT
add_nodes_to_existing
printf 'UNEXPECTED_SUCCESS\n'
'''
                result = run_bash(source, extra_env=helper.env(directory, TEST_FAIL_FILE=name))
                self.assertNotEqual(result.returncode, 0, result.stdout[-500:])
                self.assertNotIn("UNEXPECTED_SUCCESS", result.stdout)
                self.assertTrue((directory / "output-failed").exists())
                self.assertEqual(helper.rows(directory), original_rows)
                self.assertEqual({n: (info / n).read_bytes() for n in names if (info / n).exists()},
                                 original_files)


if __name__ == "__main__":
    unittest.main()
