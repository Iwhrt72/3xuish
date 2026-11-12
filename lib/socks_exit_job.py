"""Private, detached SOCKS provisioning jobs; no credentials in public state."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
import uuid


ACTIVE = {"starting", "running"}
TERMINAL = {"complete", "failed", "rolled_back", "manual_recovery_required"}
JOB_NAME = r"job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}"
CONTEXT = ("XUI_DOMAIN", "XUI_SERVER_IP", "XUI_USERNAME", "XUI_PASSWORD",
           "EXIT_NODE_PORT", "SOCKS_EXIT_EXPECTED_IP")
# Keep detached Popen handles alive in library callers; the CLI itself exits
# immediately after launch. wait_job reaps owned children when they finish.
_CHILDREN = {}


class JobError(RuntimeError):
    """Fixed identifiers safe to display in status output."""


def safe_path(path):
    path = Path(path).absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise JobError("symlinked_job_path")
    return path


def private_write(path, data):
    if isinstance(data, str):
        data = data.encode("utf-8")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as target:
        target.write(data)
        target.flush()
        os.fsync(target.fileno())


def atomic_json(path, value):
    path = safe_path(path)
    temporary = path.with_name(path.name + ".next-" + uuid.uuid4().hex[:8])
    private_write(temporary, json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


def write_state(job, status, **fields):
    result = {"status": status, "operation": "socks_exit", "job_directory": str(job),
              "log_file": str(job / "worker.log"),
              "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(), **fields}
    atomic_json(job / "state.json", result)
    return result


def read_job(job):
    job = safe_path(job)
    if not re.fullmatch(JOB_NAME, job.name):
        raise JobError("invalid_job_name")
    state = json.loads(safe_path(job / "state.json").read_text(encoding="utf-8"))
    if state.get("status") not in ACTIVE | TERMINAL:
        raise JobError("invalid_job_state")
    if state["status"] not in ACTIVE:
        return state
    import fcntl
    try:
        fd = os.open(safe_path(job / "worker.lock"), os.O_RDONLY)
    except FileNotFoundError:
        return {**state, "status": "manual_recovery_required", "error": "worker_interrupted"}
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return state
        # The worker may have completed between reading state and taking the lock.
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        if state.get("status") in ACTIVE:
            return {**state, "status": "manual_recovery_required", "error": "worker_interrupted"}
        return state
    finally:
        os.close(fd)


def read_latest(info_dir):
    root = safe_path(Path(info_dir) / "3xuish-socks-exit")
    pointer = safe_path(root / "latest.json")
    if not pointer.exists():
        return {"status": "no_previous_job", "operation": "socks_exit"}
    name = json.loads(pointer.read_text(encoding="utf-8")).get("job", "")
    if not re.fullmatch(JOB_NAME, name):
        raise JobError("invalid_job_pointer")
    return read_job(root / name)


def launch(args):
    import fcntl
    root = safe_path(Path(args.info_dir) / "3xuish-socks-exit")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    # The caller already owns the shared maintenance lock. Validate that the
    # inherited descriptor really refers to it, then keep the same open file
    # description in the independent worker and shell until the transaction ends.
    lock_path = safe_path(Path(args.info_dir) / "3xuish-reinstall/reinstall.lock")
    expected = lock_path.stat()
    supplied = os.fstat(args.lock_fd)
    if (expected.st_dev, expected.st_ino) != (supplied.st_dev, supplied.st_ino):
        raise JobError("invalid_maintenance_lock")
    try:
        fcntl.flock(args.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise JobError("maintenance_busy") from None
    previous = read_latest(args.info_dir)
    if previous["status"] in ACTIVE | {"manual_recovery_required"}:
        raise JobError("previous_socks_job_needs_attention")
    name = "job-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    job = root / name
    job.mkdir(mode=0o700)
    worker_fd = os.open(job / "worker.lock", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        fcntl.flock(worker_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for source, target in ((args.installer, "installer.sh"), (args.helper, "socks_exit.sh"),
                               (args.probe, "socks_node_probe.py"), (__file__, "worker.py"),
                               (args.request, "upstream.json")):
            private_write(job / target, Path(source).read_bytes())
        environment = {key: os.environ[key] for key in CONTEXT if key in os.environ}
        environment.update(XUI_INFO_DIR=str(Path(args.info_dir).absolute()),
                           XUI_DB_PATH=args.database, XUI_INSTALL_DIR=args.install_dir,
                           XUI_SERVICE=args.service)
        private_write(job / "context.json", json.dumps(environment))
        (job / "work").mkdir(mode=0o700)
        write_state(job, "starting", phase="queued")
        atomic_json(root / "latest.json", {"job": name})
        log_fd = os.open(job / "worker.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            process = subprocess.Popen(
                [sys.executable, str(job / "worker.py"), "_worker", str(job),
                 str(args.lock_fd), str(worker_fd)], stdin=subprocess.DEVNULL,
                stdout=log_fd, stderr=log_fd, start_new_session=True,
                close_fds=True, pass_fds=(args.lock_fd, worker_fd))
        except Exception:
            write_state(job, "failed", phase="launch", error="worker_start_failed")
            raise
        finally:
            os.close(log_fd)
        _CHILDREN[str(job)] = process
        return {"status": "started", "job_directory": str(job), "pid": process.pid}
    finally:
        os.close(worker_fd)


def worker(job, descriptors):
    job = safe_path(job)
    environment = os.environ.copy()
    environment.update(json.loads((job / "context.json").read_text(encoding="utf-8")))
    environment["SOCKS_EXIT_JOB_DIR"] = str(job)
    write_state(job, "running", phase="preflight", pid=os.getpid())
    # This shell never invokes the installer's parent EXIT cleanup. All paths
    # point into the private job, independently of a downloaded entrypoint.
    command = r'''source "$SOCKS_EXIT_JOB_DIR/installer.sh"
RUN_DIR="$SOCKS_EXIT_JOB_DIR/work"
SOCKS_EXIT_PROBE="$SOCKS_EXIT_JOB_DIR/socks_node_probe.py"
SOCKS_EXIT_JOB_HELPER="$SOCKS_EXIT_JOB_DIR/worker.py"
source "$SOCKS_EXIT_JOB_DIR/socks_exit.sh"
create_socks_exit_node_worker "$SOCKS_EXIT_JOB_DIR/upstream.json"
'''
    child = None
    previous = {}
    def stop(signum, _frame):
        if child is not None and child.poll() is None:
            child.send_signal(signum)
    for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, stop)
    try:
        child = subprocess.Popen(["bash", "-c", command], env=environment,
                                 stdin=subprocess.DEVNULL, close_fds=True,
                                 pass_fds=tuple(descriptors))
        rc = child.wait()
        outcome_path = job / "outcome.json"
        outcome = json.loads(outcome_path.read_text(encoding="utf-8")) if outcome_path.exists() else {}
        status = outcome.pop("status", "")
        if status not in TERMINAL or (rc != 0 and status == "complete"):
            status = "manual_recovery_required" if (job / "mutating").exists() else "failed"
            outcome = {"error": "transaction_interrupted" if rc < 0 else "transaction_failed"}
        reasons = {"complete": "节点已通过验证", "failed": "创建前检查失败，尚未发布新节点",
                   "rolled_back": "创建失败，已确认移除本次节点和路由",
                   "manual_recovery_required": "未能确认恢复完成，请查看私有日志和数据库备份"}
        return write_state(job, status, phase="finished", exit_code=rc,
                           message=reasons[status], **outcome)
    except BaseException:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait()
        write_state(job, "manual_recovery_required" if (job / "mutating").exists() else "failed",
                    phase="finished", error="worker_failed")
        raise
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        # Keep private evidence for incomplete recovery. Terminal successful or
        # fully compensated jobs no longer need copies of credentials/API tokens.
        state = json.loads((job / "state.json").read_text(encoding="utf-8"))
        if state.get("status") in {"complete", "failed", "rolled_back"}:
            try:
                for name in ("upstream.json", "context.json"):
                    (job / name).unlink(missing_ok=True)
                import shutil
                shutil.rmtree(job / "work")
            except OSError:
                # Cleanup failure must not turn a verified committed result
                # into a reported provisioning failure. Paths remain private.
                state["cleanup_warning"] = "private_work_files_remain"
                atomic_json(job / "state.json", state)


def wait_job(job, timeout):
    deadline = time.monotonic() + timeout
    while True:
        state = read_job(job)
        if state["status"] not in ACTIVE or time.monotonic() >= deadline:
            process = _CHILDREN.get(str(job))
            if process is not None and state["status"] not in ACTIVE:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
                else:
                    del _CHILDREN[str(job)]
            return state
        time.sleep(0.2)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_worker":
        worker(Path(argv[1]), [int(value) for value in argv[2:]])
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("launch")
    for name in ("installer", "helper", "probe", "request", "info-dir", "database", "install-dir", "service"):
        start.add_argument("--" + name, required=True)
    start.add_argument("--lock-fd", type=int, required=True)
    for name in ("status", "guard"):
        commands.add_parser(name).add_argument("--info-dir", required=True)
    wait = commands.add_parser("wait")
    wait.add_argument("job")
    wait.add_argument("--timeout", type=float, default=60)
    phase = commands.add_parser("phase")
    phase.add_argument("job")
    phase.add_argument("phase", choices=("checking_upstream", "preparing_panel",
        "creating_disabled_inbound", "applying_template", "enabling_inbound", "verifying_node", "rolling_back"))
    args = parser.parse_args(argv)
    try:
        if args.command == "phase":
            job = safe_path(args.job)
            state = json.loads((job / "state.json").read_text(encoding="utf-8"))
            if state.get("status") != "running":
                raise JobError("job_not_running")
            state["phase"] = args.phase
            state["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
            atomic_json(job / "state.json", state)
            return 0
        if args.command == "launch":
            result = launch(args)
        elif args.command == "wait":
            result = wait_job(args.job, max(0, args.timeout))
        else:
            result = read_latest(args.info_dir)
        if args.command == "guard":
            return int(result["status"] in ACTIVE | {"manual_recovery_required"})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (JobError, OSError, ValueError, KeyError) as exc:
        error = str(exc) if isinstance(exc, JobError) else "job_state_or_files_unavailable"
        print(json.dumps({"status": "error", "error": error}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
