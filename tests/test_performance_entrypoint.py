"""Verify published helper pinning and the shell's read-only repair entry."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import unittest

from test_installer import REPO, run_bash, bash_directory


class PerformanceEntrypoint(unittest.TestCase):
    def test_published_script_pins_exact_helper_bytes(self):
        script=(REPO/"install.sh").read_text(encoding="utf-8")
        match=re.search(r'^PERFORMANCE_HELPER_SHA256="([a-f0-9]{64})"$',script,re.M)
        self.assertIsNotNone(match)
        self.assertEqual(match[1],hashlib.sha256((REPO/"lib/performance_repair.py").read_bytes()).hexdigest())


    def test_unsupported_python_aborts_before_copy_or_download(self):
        with tempfile.TemporaryDirectory(prefix="performance-python-",dir=REPO) as tmp:
            directory=Path(tmp)
            database=directory/'live.db'
            database.write_bytes(b'unchanged-database')
            source=r"""
source ./install.sh
RUN_DIR="$TEST_MOCK_DIR"
XUI_DB_PATH="$TEST_NATIVE_DIR/live.db"
python3() { return 1; }
curl() { printf 'UNEXPECTED_DOWNLOAD\n'; return 99; }
performance_command check
"""
            result=run_bash(source,extra_env={'TEST_MOCK_DIR':bash_directory(directory),
                                             'TEST_NATIVE_DIR':directory.as_posix()})
            self.assertNotEqual(result.returncode,0)
            self.assertIn('Python 3.8',result.stdout+result.stderr)
            self.assertNotIn('UNEXPECTED_DOWNLOAD',result.stdout+result.stderr)
            self.assertEqual(database.read_bytes(),b'unchanged-database')
            self.assertFalse((directory/'performance_repair.py').exists())

    def test_shell_check_is_read_only_and_does_not_require_running_core(self):
        with tempfile.TemporaryDirectory(prefix="performance-entry-",dir=REPO) as tmp:
            directory=Path(tmp)
            database=directory/"live.db"
            info=directory/"info"
            info.mkdir()
            private="fixture-secret-must-not-appear"
            stream={"network":"xhttp","security":"tls","sockopt":{"tcpWindowClamp":600,
                    "tcpMaxSeg":1440,"tcpUserTimeout":10000,"tcpcongestion":"bbr"}}
            with closing(sqlite3.connect(database)) as con:
                con.execute("CREATE TABLE inbounds(id INTEGER PRIMARY KEY,tag TEXT,remark TEXT,port INTEGER,"
                            "protocol TEXT,enable INTEGER,listen TEXT,settings TEXT,stream_settings TEXT,sniffing TEXT)")
                con.execute("INSERT INTO inbounds VALUES(1,'old-tag','VLESS-XHTTP-Enc-Vision',443,'vless',1,'',?,?,'{}')",
                            (json.dumps({"clients":[{"id":private}],"decryption":"none"}),json.dumps(stream)))
                con.commit()
            before=database.read_bytes()
            source=r'''
source ./install.sh
RUN_DIR="$TEST_MOCK_DIR"
XUI_DB_PATH="$TEST_NATIVE_DIR/live.db"
XUI_INSTALL_DIR="$TEST_NATIVE_DIR/missing-panel"
XUI_INFO_DIR="$TEST_NATIVE_DIR/info"
python3() { "$TEST_PYTHON" "$@"; }
performance_command check
'''
            result=run_bash(source,extra_env={"TEST_MOCK_DIR":bash_directory(directory),
                                             "TEST_NATIVE_DIR":directory.as_posix(),"TEST_PYTHON":sys.executable})
            self.assertEqual(result.returncode,0,result.stderr[-1000:])
            self.assertEqual(database.read_bytes(),before)
            self.assertEqual(list(info.iterdir()),[])
            self.assertNotIn(private,result.stdout+result.stderr)
            self.assertIn("600",result.stdout)


if __name__=="__main__":
    unittest.main()
