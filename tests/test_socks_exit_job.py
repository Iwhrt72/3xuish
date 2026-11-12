"""Exercise real detached sessions and inherited locks without a panel."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

from test_installer import bash_directory, run_bash


REPO = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("socks_exit_job", REPO / "lib/socks_exit_job.py")
jobs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jobs)
NAME = "job-20261112T010101Z-0123456789ab"


class StatusTests(unittest.TestCase):
    def test_no_job_does_not_create_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            info = Path(temp) / "absent"
            self.assertEqual(jobs.read_latest(info)["status"], "no_previous_job")
            self.assertFalse(info.exists())

    def test_terminal_result_preserves_identity_and_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "3xuish-socks-exit"
            job = root / NAME
            job.mkdir(parents=True)
            jobs.write_state(job, "complete", inbound_id="8", exit_ip="203.0.113.10")
            jobs.atomic_json(root / "latest.json", {"job": NAME})
            state = jobs.read_latest(temp)
            self.assertEqual(state["status"], "complete")
            self.assertEqual(state["inbound_id"], "8")
            self.assertNotIn("password", json.dumps(state))

    def test_pointer_cannot_escape_job_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "3xuish-socks-exit"
            root.mkdir()
            jobs.atomic_json(root / "latest.json", {"job": "../../outside"})
            with self.assertRaises(jobs.JobError):
                jobs.read_latest(temp)


class RollbackTests(unittest.TestCase):
    def exercise(self, *, listener=False, template_written=True, inbound_id="8"):
        with tempfile.TemporaryDirectory(prefix="socks-rollback-", dir=REPO) as temp:
            directory = Path(temp)
            plan = {"inboundTag": "new-inbound", "outbounds": [{"tag": "new-socks"}, {"tag": "new-dns"}]}
            template = {"xraySetting": {"outbounds": [{"tag": "direct"}, *plan["outbounds"]],
                "routing": {"rules": [
                    {"inboundTag": ["new-inbound"], "outboundTag": "blocked", "ruleTag": "new-inbound-deny-0"},
                    {"inboundTag": ["new-inbound"], "outboundTag": "new-dns"},
                    {"inboundTag": ["new-inbound"], "outboundTag": "new-socks"},
                    {"inboundTag": ["old-inbound"], "outboundTag": "direct"},
                ]}}, "outboundTestUrl": "https://example.test"}
            (directory / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
            (directory / "template-state.json").write_text(json.dumps(template), encoding="utf-8")
            script = r'''
source ./install.sh
source ./lib/socks_exit.sh
work="$REVIEW_DIRECTORY"
SOCKS_EXIT_JOB_DIR="$work"
exit_id="$REVIEW_ID"
exit_remark=fixture
exit_backup=fixture-backup
EXIT_NODE_PORT=26600
attempted_add=1
attempted_template="$REVIEW_TEMPLATE"
proxy_exit_job_phase() { :; }
api_request() { printf '{"success":true}\n'; }
proxy_exit_find_inbound() { printf '{}\n'; }
proxy_exit_get_template() { cat "$work/template-state.json"; }
proxy_exit_write_template() {
 jq '{xraySetting:.,outboundTestUrl:"https://example.test"}' "$1" >"$work/template-state.json"
}
ss() { if [[ "$REVIEW_LISTENER" == 1 ]]; then printf 'LISTEN 26600\n'; fi; }
sleep() { :; }
set +e
proxy_exit_rollback 1
exit "$?"
'''
            result = run_bash(script, extra_env={"REVIEW_DIRECTORY": bash_directory(directory),
                "REVIEW_ID": inbound_id, "REVIEW_TEMPLATE": str(int(template_written)),
                "REVIEW_LISTENER": str(int(listener))})
            self.assertEqual(result.returncode, 1, result.stderr)
            return (json.loads((directory / "outcome.json").read_text(encoding="utf-8")),
                    json.loads((directory / "template-state.json").read_text(encoding="utf-8")))

    def test_compensation_removes_own_deny_guards_and_preserves_other_inbounds(self):
        state, template = self.exercise()
        self.assertEqual(state["status"], "rolled_back")
        self.assertEqual(template["xraySetting"]["outbounds"], [{"tag": "direct"}])
        self.assertEqual(template["xraySetting"]["routing"]["rules"],
                         [{"inboundTag": ["old-inbound"], "outboundTag": "direct"}])

    def test_still_listening_inbound_keeps_routes_and_requires_recovery(self):
        state, template = self.exercise(listener=True)
        self.assertEqual(state["status"], "manual_recovery_required")
        self.assertEqual(len(template["xraySetting"]["outbounds"]), 3)
        self.assertEqual(len(template["xraySetting"]["routing"]["rules"]), 4)

    def test_unconfirmed_add_with_confirmed_absence_needs_no_manual_recovery(self):
        state, _ = self.exercise(template_written=False, inbound_id="")
        self.assertEqual(state["status"], "rolled_back")


@unittest.skipUnless(os.name == "posix", "detached SOCKS jobs require POSIX sessions and flock")
class DetachedJobTests(unittest.TestCase):
    def setUp(self):
        import fcntl
        self.fcntl = fcntl
        self.temp = tempfile.TemporaryDirectory(prefix="socks-job-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.info = self.root / "info"
        locks = self.info / "3xuish-reinstall"
        locks.mkdir(parents=True)
        self.lock = locks / "reinstall.lock"
        self.fd = os.open(self.lock, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(lambda: os.close(self.fd))
        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.source = self.root / "download"
        self.source.mkdir()
        self.installer = self.source / "installer.sh"
        self.installer.write_text("set -euo pipefail\numask 077\n", encoding="utf-8")
        self.helper = self.source / "helper.sh"
        self.probe = self.source / "probe.py"
        self.probe.write_text("# fixture\n", encoding="utf-8")
        self.request = self.source / "request.json"
        self.request.write_text('{"secret":"fixture-secret"}', encoding="utf-8")
        self.args = argparse.Namespace(info_dir=str(self.info), lock_fd=self.fd,
            installer=str(self.installer), helper=str(self.helper), probe=str(self.probe),
            request=str(self.request), database=str(self.root / "database"),
            install_dir=str(self.root / "panel"), service="fixture")

    def script(self, outcome="complete", exit_code=0, gate=True):
        self.helper.write_text('''create_socks_exit_node_worker() {
  test -f "$1"
  test -f "$SOCKS_EXIT_PROBE"
  test -d "$RUN_DIR"
  printf ready >"$SOCKS_EXIT_JOB_DIR/ready"
''' + ('''  while [[ ! -f "$SOCKS_EXIT_JOB_DIR/release" ]]; do sleep .05; done
''' if gate else "") + f'''  printf '%s' '{{"status":"{outcome}","inbound_id":"8"}}' >"$SOCKS_EXIT_JOB_DIR/outcome.json"
  return {exit_code}
}}
''', encoding="utf-8")

    def await_ready(self, job):
        deadline = time.monotonic() + 8
        while not (job / "ready").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue((job / "ready").exists(), (job / "worker.log").read_text())

    def assert_busy(self):
        fd = os.open(self.lock, os.O_RDWR)
        try:
            with self.assertRaises(BlockingIOError):
                self.fcntl.flock(fd, self.fcntl.LOCK_EX | self.fcntl.LOCK_NB)
        finally:
            os.close(fd)

    def release(self, job):
        (job / "release").touch()
        state = jobs.wait_job(job, 8)
        self.assertNotIn(state["status"], jobs.ACTIVE)
        return state

    def test_worker_survives_parent_hup_and_source_removal_and_keeps_lock(self):
        self.script()
        # The launcher is in a throw-away foreground session; only its child
        # worker is detached. Neither this test runner nor a real service is signalled.
        code = '''import importlib.util,json,sys,time
spec=importlib.util.spec_from_file_location("jobs",sys.argv[1]); m=importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
from argparse import Namespace
r=m.launch(Namespace(**json.loads(sys.argv[2])))
print(json.dumps(r),flush=True)
time.sleep(30)
'''
        proc = subprocess.Popen([sys.executable, "-c", code, str(REPO / "lib/socks_exit_job.py"),
                                 json.dumps(vars(self.args))], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True, pass_fds=(self.fd,))
        result = json.loads(proc.stdout.readline())
        job = Path(result["job_directory"])
        self.await_ready(job)
        # Closing this holder lets the test prove the worker owns the inherited lock.
        os.close(self.fd)
        self.fd = os.open(os.devnull, os.O_RDONLY)
        os.killpg(proc.pid, signal.SIGHUP)
        proc.communicate(timeout=5)
        shutil.rmtree(self.source)
        self.assert_busy()
        self.assertIn(jobs.read_latest(self.info)["status"], jobs.ACTIVE)
        state = self.release(job)
        self.assertEqual(state["status"], "complete")
        deadline = time.monotonic() + 3
        while (job / "work").exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertFalse((job / "work").exists())
        self.assertFalse((job / "upstream.json").exists())
        self.assertFalse((job / "context.json").exists())
        self.assertNotIn("fixture-secret", (job / "state.json").read_text())
        self.assertEqual((job.stat().st_mode & 0o777), 0o700)

    def test_real_shell_frontend_passes_lock_snapshots_helper_and_waits_for_result(self):
        import pty
        import select
        required = ("bash", "python3", "jq", "curl", "sqlite3", "ss", "openssl", "sha256sum")
        if any(shutil.which(command) is None for command in required):
            self.skipTest("the interactive frontend requires its normal Linux tools")
        # This case enters through the real Bash lock and frontend, rather than
        # calling launch() in Python. Only the panel-facing worker body is stubbed.
        os.close(self.fd)
        self.fd = os.open(os.devnull, os.O_RDONLY)
        self.installer.write_bytes((REPO / "install.sh").read_bytes())
        self.helper.write_bytes((REPO / "lib/socks_exit.sh").read_bytes() + b'''
create_socks_exit_node_worker() {
    [[ "$INSTALLER_SOURCE_FILE" == "$SOCKS_EXIT_JOB_DIR/installer.sh" ]]
    [[ "$RUN_DIR" == "$SOCKS_EXIT_JOB_DIR/work" ]]
    [[ -f "$SOCKS_EXIT_PROBE" ]]
    jq -e '.settings.servers[0] | .address == "192.0.2.40" and .port == 1080 and
        .users[0].user == "fixture-user" and .users[0].pass == "private-frontend-password"' "$1" >/dev/null
    printf '%s' '{"status":"complete","inbound_id":"8","exit_ip":"203.0.113.10"}' >"$SOCKS_EXIT_JOB_DIR/outcome.json"
}
''')
        database = self.root / "database"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY, remark TEXT, protocol TEXT, "
                               "port INTEGER, enable INTEGER, settings TEXT, stream_settings TEXT)")
            connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
        script = self.root / "frontend.sh"
        script.write_text(r'''
source "$REVIEW_SOURCE/installer.sh"
XUI_INFO_DIR="$REVIEW_INFO"
XUI_DB_PATH="$REVIEW_DATABASE"
RUN_DIR=$(mktemp -d /tmp/3xuish.frontend.XXXXXXXX)
trap cleanup_install EXIT
SOCKS_EXIT_PROBE="$REVIEW_SOURCE/probe.py"
SOCKS_EXIT_JOB_HELPER="$REVIEW_JOB_HELPER"
PROXY_INPUT_HELPER="$REVIEW_INPUT_HELPER"
NODE_OVERVIEW_HELPER="$REVIEW_OVERVIEW_HELPER"
source "$REVIEW_SOURCE/helper.sh"
acquire_maintenance_lock
create_socks_exit_node
socks_exit_status_command
''', encoding="utf-8")
        environment = os.environ.copy()
        environment.update(REVIEW_SOURCE=str(self.source), REVIEW_INFO=str(self.info),
                           REVIEW_DATABASE=str(database), REVIEW_JOB_HELPER=str(REPO / "lib/socks_exit_job.py"),
                           REVIEW_INPUT_HELPER=str(REPO / "lib/proxy_input.py"),
                           REVIEW_OVERVIEW_HELPER=str(REPO / "lib/node_overview.py"))
        master, slave = pty.openpty()
        self.addCleanup(lambda: os.close(master))
        process = subprocess.Popen(["bash", str(script)], env=environment,
                                   stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
        os.close(slave)
        transcript = bytearray()
        def read_until(needle, timeout=10):
            deadline = time.monotonic() + timeout
            while needle not in transcript and time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], .1)
                if ready:
                    try:
                        transcript.extend(os.read(master, 65536))
                    except OSError:
                        break
            self.assertIn(needle, transcript, transcript.decode("utf-8", errors="replace"))
        try:
            for prompt, value in (("输入方式 [1 分项填写 / 2 整行粘贴，默认1]：", "1"),
                                  ("代理协议 [1 SOCKS5 / 2 HTTP CONNECT / 3 HTTPS CONNECT，默认1]：", "1"),
                                  ("代理地址（IP 或域名）：", "192.0.2.40"), ("代理端口：", "1080"),
                                  ("代理账号（无认证留空）：", "fixture-user"),
                                  ("代理密码（无认证留空，不回显）：", "private-frontend-password")):
                read_until(prompt.encode("utf-8"))
                os.write(master, (value + "\n").encode("utf-8"))
            read_until('创建完成'.encode('utf-8'))
            process.wait(timeout=10)
            while select.select([master], [], [], 0)[0]:
                try:
                    transcript.extend(os.read(master, 65536))
                except OSError:
                    break
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=10)
        self.assertEqual(process.returncode, 0, transcript.decode("utf-8", errors="replace"))
        self.assertNotIn(b"private-frontend-password", transcript)
        state = jobs.read_latest(self.info)
        self.assertEqual(state["status"], "complete")
        self.assertEqual(state["inbound_id"], "8")
        job = Path(state["job_directory"])
        self.assertEqual((job / "socks_exit.sh").read_bytes(), self.helper.read_bytes())
        self.assertEqual((job / "installer.sh").read_bytes(), self.installer.read_bytes())
        self.assertFalse((job / "upstream.json").exists())
        self.assertFalse((job / "context.json").exists())
        self.assertNotIn("private-frontend-password", (job / "state.json").read_text())
        self.assertNotIn("private-frontend-password", (job / "worker.log").read_text())

    def test_worker_preserves_confirmed_rollback_result(self):
        self.script("rolled_back", 1, gate=False)
        job = Path(jobs.launch(self.args)["job_directory"])
        state = jobs.wait_job(job, 8)
        self.assertEqual(state["status"], "rolled_back")
        self.assertEqual(state["exit_code"], 1)

    def test_failed_mutating_process_requires_recovery_and_blocks_next_launch(self):
        self.helper.write_text('''create_socks_exit_node_worker() {
 : >"$SOCKS_EXIT_JOB_DIR/mutating"
 return 1
}
''', encoding="utf-8")
        job = Path(jobs.launch(self.args)["job_directory"])
        self.assertEqual(jobs.wait_job(job, 8)["status"], "manual_recovery_required")
        self.assertTrue((job / "upstream.json").exists())
        with self.assertRaisesRegex(jobs.JobError, "previous_socks_job"):
            jobs.launch(self.args)

    def test_failure_before_mutation_is_failed_not_manual_recovery(self):
        self.helper.write_text('create_socks_exit_node_worker() { return 1; }\n', encoding="utf-8")
        job = Path(jobs.launch(self.args)["job_directory"])
        self.assertEqual(jobs.wait_job(job, 8)["status"], "failed")

    def test_orphaned_active_state_is_reported_read_only_and_blocks_launch(self):
        root = self.info / "3xuish-socks-exit"
        job = root / NAME
        job.mkdir(parents=True)
        jobs.write_state(job, "running", phase="applying")
        jobs.atomic_json(root / "latest.json", {"job": NAME})
        before = (job / "state.json").read_bytes()
        self.assertEqual(jobs.read_latest(self.info)["status"], "manual_recovery_required")
        self.assertEqual((job / "state.json").read_bytes(), before)
        with self.assertRaises(jobs.JobError):
            jobs.launch(self.args)


if __name__ == "__main__":
    unittest.main()
