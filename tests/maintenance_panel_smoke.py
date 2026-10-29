"""Real 3x-ui/Xray maintenance transactions in a disposable mapped layout.

Requires the official test image, Bash, jq, Python, OpenSSL and ss. No live
system service, host directory, public download or ACME account is used.
"""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from unittest import mock

import runtime_smoke as fixture
from test_clean_reinstall import managed_paths

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import clean_reinstall as maintenance


def checked(args, **kwargs):
    result = subprocess.run(args, capture_output=True, timeout=30, **kwargs)
    if result.returncode:
        raise RuntimeError("official_panel_fixture_command_failed")
    return result


class LivePanel(maintenance.DefaultAdapter):
    """Actual panel process with an in-memory stand-in for service enablement."""
    def __init__(self, config, paths, prepared, profiles, panel_port):
        self.config, self.paths = config, paths
        self.prepared, self.profiles, self.panel_port = prepared, profiles, panel_port
        self.process = self.log = None
        self.enabled = True
        self.fail_install = False
        self.installs = 0

    def environment(self, database_directory=None):
        environment = {key: value for key, value in os.environ.items() if not key.startswith("XUI_")}
        environment.update(XUI_DB_TYPE="sqlite", XUI_DB_FOLDER=str(database_directory or self.paths["db_dir"]),
            XUI_BIN_FOLDER=str(Path(self.paths["install_dir"]) / "bin"),
            XUI_LOG_FOLDER=str(Path(self.paths["info_dir"]) / "fixture-logs"))
        return environment

    def preflight(self):
        # The transaction's fixed paths are mapped below; no real init manager.
        pass

    def is_active(self):
        return self.process is not None and self.process.poll() is None

    def is_enabled(self):
        return self.enabled

    def disable(self):
        self.enabled = False

    def enable(self):
        self.enabled = True

    def refresh(self):
        pass

    def start(self, database_directory=None):
        assert not self.is_active(), "fixture_panel_already_active"
        directory = Path(self.paths["install_dir"])
        log_path = Path(self.paths["info_dir"]) / "fixture-panel.log"
        self.log = log_path.open("ab")
        self.process = subprocess.Popen([str(directory / "x-ui"), "run"], cwd=directory,
            stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log,
            env=self.environment(database_directory), start_new_session=True)
        url = "http://127.0.0.1:%d/maintenance-smoke/" % self.panel_port
        deadline = time.monotonic() + 15
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        expected = set()
        with closing(sqlite3.connect(Path(database_directory or self.paths["db_dir"]) / "x-ui.db")) as connection:
            for port, protocol in connection.execute("SELECT port,protocol FROM inbounds WHERE enable=1"):
                expected.add(("udp" if protocol == "hysteria" else "tcp", port))
        while time.monotonic() < deadline:
            if not self.is_active():
                raise RuntimeError("official_fixture_panel_start_failed")
            try:
                with opener.open(url, timeout=.5) as response:
                    if response.status == 200 and expected <= self.listener_snapshot():
                        return
            except OSError:
                time.sleep(.1)
        raise RuntimeError("official_fixture_panel_start_timeout")

    def stop(self):
        if self.process is not None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def run_install(self, installer, environment, log_path):
        self.installs += 1
        install = Path(self.paths["install_dir"])
        shutil.copytree("/app", install)
        shutil.copytree(self.prepared, self.paths["db_dir"])
        info = Path(self.paths["info_dir"])
        (info / maintenance.INFO_FILES[0]).write_text(json.dumps(self.profiles), encoding="utf-8")
        (info / maintenance.INFO_FILES[1]).write_text("new-fixture-panel-information", encoding="utf-8")
        (info / maintenance.INFO_FILES[2]).write_text("\n".join(item["link"] for item in self.profiles), encoding="utf-8")
        for key in ("command", "unit", "openrc"):
            target = Path(self.paths[key])
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("fixture-service-resource", encoding="utf-8")
        Path(self.paths["dropins"]).mkdir(parents=True, exist_ok=True)
        self.enabled = True
        self.start()
        if self.fail_install:
            raise RuntimeError("injected_failure_after_real_panel_start")


def prepare_database(adapter, directory, profiles):
    directory.mkdir()
    binary = str(Path(adapter.paths["install_dir"]) / "x-ui")
    environment = adapter.environment(directory)
    checked([binary, "setting", "-username", "fixture-maintenance", "-password", "fixture-private-password",
             "-port", str(adapter.panel_port), "-webBasePath", "maintenance-smoke"], env=environment)
    token_result = checked([binary, "setting", "-getApiToken", "-tokenName", "maintenance-smoke",
                            "-tokenScope", "admin"], env=environment)
    token = re.search(rb"(?m)^\s*apiToken:\s*(\S+)", token_result.stdout)
    if token is None:
        raise RuntimeError("fixture_panel_api_token_missing")
    adapter.start(directory)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = "http://127.0.0.1:%d/maintenance-smoke" % adapter.panel_port
    try:
        for profile in profiles:
            request = urllib.request.Request(base + "/panel/api/inbounds/add",
                data=json.dumps(profile["payload"]).encode(), method="POST",
                headers={"Authorization": "Bearer " + token[1].decode(), "Content-Type": "application/json"})
            with opener.open(request, timeout=10) as response:
                data = json.load(response)
            if data.get("success") is not True:
                raise RuntimeError("fixture_panel_rejected_inbound")
    finally:
        adapter.stop()


def rows(database):
    with closing(sqlite3.connect(database)) as connection:
        return connection.execute("SELECT id,tag,port,settings FROM inbounds ORDER BY id").fetchall()


def main():
    os.umask(0o077)
    # Container defaults refer to /app and /etc; this smoke maps the entire
    # managed layout to its private temporary fixture instead.
    for name in list(os.environ):
        if name.startswith("XUI_"):
            os.environ.pop(name)
    core = os.environ.get("XRAY_BIN", "/app/bin/xray-linux-amd64")
    with tempfile.TemporaryDirectory(prefix="3xuish-maintenance-smoke-") as temp:
        root = Path(temp)
        paths = managed_paths(root)
        info = Path(paths["info_dir"])
        info.mkdir(parents=True)
        shutil.copytree("/app", paths["install_dir"])
        cert_dir = Path(paths["cert_root"]) / "ip"
        cert_dir.mkdir(parents=True)
        cert, key = cert_dir / "fullchain.pem", cert_dir / "privkey.pem"
        checked(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1",
            "-keyout", str(key), "-out", str(cert)])
        old_profiles = [item for item in fixture.profiles(core, cert, key, 1) if item["key"] == "xhttp"]
        new_profiles = fixture.profiles(core, cert, key, 1)
        installer = root / "installer.sh"
        installer.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        config = {"database": paths["database"], "install_dir": paths["install_dir"],
            "info_dir": paths["info_dir"], "service": "x-ui", "installer": str(installer),
            "install_env": {"CERT_MODE": "1", "XUI_SERVER_IP": "127.0.0.1"},
            "verify_attempts": 30, "verify_delay": .1}
        old_database, new_database = root / "prepared-old-db", root / "prepared-new-db"
        adapter = LivePanel(config, paths, new_database, new_profiles, fixture.port())
        try:
            prepare_database(adapter, old_database, old_profiles)
            prepare_database(adapter, new_database, new_profiles)
            shutil.copytree(old_database, paths["db_dir"])
            (info / maintenance.INFO_FILES[0]).write_text(json.dumps(old_profiles), encoding="utf-8")
            (info / maintenance.INFO_FILES[1]).write_text("original-fixture-panel-information", encoding="utf-8")
            old_links = "\n".join(item["link"] for item in old_profiles)
            (info / maintenance.INFO_FILES[2]).write_text(old_links, encoding="utf-8")
            backup_root = Path(paths["backup_root"])
            backup_root.mkdir()
            original_rows = rows(paths["database"])
            original_cert = cert.read_bytes(), key.read_bytes()
            adapter.start()
            print("PASS: official old/new databases prepared through local panel API", flush=True)
            old_ports = {("tcp", old_profiles[0]["port"])}
            with mock.patch.object(maintenance, "DEFAULT_PATHS", paths):
                # A partially running new real panel must be stopped and rolled back.
                adapter.fail_install = True
                failed_job = backup_root / "job-20261029T050000Z-123456abcdef"
                result = maintenance.execute_reinstall(config, failed_job, adapter)
                assert result["status"] == "rolled_back", result
                assert adapter.is_active() and old_ports <= adapter.listener_snapshot()
                assert rows(paths["database"]) == original_rows
                assert (info / maintenance.INFO_FILES[2]).read_text() == old_links
                adapter.fail_install = False
                fresh_job = backup_root / "job-20261029T050001Z-abcdef123456"
                result = maintenance.execute_reinstall(config, fresh_job, adapter)
                assert result["status"] == "complete", result
                assert len(rows(paths["database"])) == 4
                assert (info / maintenance.INFO_FILES[2]).read_text() != old_links
                assert rows(fresh_job / "database-before.db") == original_rows
                assert (fresh_job / "old/install_dir/x-ui").is_file()
                removed_job = backup_root / "job-20261029T050002Z-654321abcdef"
                result = maintenance.execute_reinstall(dict(config, operation="uninstall"), removed_job, adapter)
                assert result["status"] == "complete", result
                assert not adapter.is_active() and not adapter.is_enabled()
                assert not old_ports & adapter.listener_snapshot()
                assert all(not path.exists() for path in maintenance.managed_paths().values())
                assert (removed_job / "old/install_dir/x-ui").is_file()
                assert len(rows(removed_job / "database-before.db")) == 4
                assert (cert.read_bytes(), key.read_bytes()) == original_cert
            print("MAINTENANCE_PANEL_SMOKE_OK: real panel rollback, fresh four-node regeneration and uninstall verified")
        finally:
            adapter.stop()


if __name__ == "__main__":
    main()
