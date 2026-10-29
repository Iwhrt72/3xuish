"""Published diagnostic components must match before any helper is executed."""
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SpeedBootstrap(unittest.TestCase):
    def test_published_component_hashes_match_exact_bytes(self):
        script = (ROOT / "scripts/server-speed-test.sh").read_text(encoding="utf-8")
        for variable, name in (("PROBE_SHA256", "server_speed_probe.py"),
                               ("TEST_SHA256", "server_speed_test.py")):
            match = re.search(r'^' + variable + r'="([a-f0-9]{64})"$', script, re.M)
            self.assertIsNotNone(match)
            self.assertEqual(match[1], hashlib.sha256((ROOT / "scripts" / name).read_bytes()).hexdigest())

    @unittest.skipUnless(os.name == "posix", "published server bootstrap requires POSIX Bash")
    def test_help_does_not_require_live_database_or_run_probes(self):
        result = subprocess.run(["bash", str(ROOT / "scripts/server-speed-test.sh"), "--help"],
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--direct-only", result.stdout)
        self.assertNotIn("Mbps", result.stdout)

    @unittest.skipUnless(os.name == "posix", "published server bootstrap requires POSIX Bash")
    def test_changed_helper_is_rejected_without_execution(self):
        with tempfile.TemporaryDirectory(prefix="speed-bootstrap-") as temp:
            directory = Path(temp)
            shutil.copyfile(ROOT / "scripts/server-speed-test.sh", directory / "server-speed-test.sh")
            marker = directory / "unexpected-execution"
            (directory / "server_speed_probe.py").write_text(
                "from pathlib import Path\nPath(" + repr(str(marker)) + ").touch()\n", encoding="utf-8")
            result = subprocess.run(["bash", str(directory / "server-speed-test.sh"), "--help"],
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
