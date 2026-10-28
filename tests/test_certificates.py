"""Certificate lifecycle failures must not reuse stale files as success."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from test_installer import REPO, bash_directory, run_bash


FIXTURE = r'''
source ./install.sh
RUN_DIR="$TEST_MOCK_DIR/run"
SERVER_IP=203.0.113.42
prepare_acme_port80() { :; }
# Redirect only the hard-coded production certificate directory; never touch it.
mkdir() {
    if [[ "$1" == -p && "$2" == /root/cert/* ]]; then
        CERT_DIR="$TEST_MOCK_DIR/issued"
        CERT_FILE="$CERT_DIR/fullchain.pem"
        KEY_FILE="$CERT_DIR/privkey.pem"
        command mkdir -p "$CERT_DIR"
    else command mkdir "$@"; fi
}
run_acme() {
    printf '%s\n' "$1" >>"$TEST_MOCK_DIR/acme-events"
    case "$1" in
        --issue) [[ "$TEST_FAILURE" != issue ]] ;;
        --installcert)
            [[ "$TEST_FAILURE" != install ]] || return 4
            cp "$TEST_MOCK_DIR/$TEST_CERTIFICATE" "$CERT_FILE"
            cp "$TEST_MOCK_DIR/$TEST_KEY" "$KEY_FILE"
            ;;
        --upgrade) return 0 ;;
        *) return 8 ;;
    esac
}
# Also exercise the published implementation before run_acme was extracted.
acme_path=~/.acme.sh/acme.sh
eval "${acme_path}() { run_acme \"\$@\"; }"
if [[ "$TEST_CERT_KIND" == ip ]]; then
    if issue_ip_cert; then printf 'RESULT_SUCCESS\n'; else printf 'RESULT_FAILURE\n'; fi
else
    if issue_domain_cert example.test; then printf 'RESULT_SUCCESS\n'; else printf 'RESULT_FAILURE\n'; fi
fi
'''


class CertificateLifecycle(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures = tempfile.TemporaryDirectory(prefix="3xuish-cert-fixtures-")
        cls.fixture_dir = Path(cls.fixtures.name)
        openssl = shutil.which("openssl")
        git_openssl = Path(r"C:\Program Files\Git\mingw64\bin\openssl.exe")
        if os.name == "nt" and git_openssl.is_file():
            openssl = str(git_openssl)
        if not openssl:
            raise RuntimeError("OpenSSL is required")
        cls.openssl = openssl
        for name, domain in (("valid", "example.test"), ("other", "other.example.test")):
            subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-days", "1", "-subj", "/CN=" + domain,
                            "-addext", f"subjectAltName=DNS:{domain},IP:203.0.113.42",
                            "-keyout", str(cls.fixture_dir / (name + ".key")),
                            "-out", str(cls.fixture_dir / (name + ".pem"))],
                           check=True, capture_output=True)
        subprocess.run([openssl, "x509", "-in", str(cls.fixture_dir / "valid.pem"),
                        "-signkey", str(cls.fixture_dir / "valid.key"), "-days", "-1",
                        "-out", str(cls.fixture_dir / "expired.pem")],
                       check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.fixtures.cleanup()

    def run_case(self, *, kind="domain", failure="none", certificate="valid.pem",
                 key="valid.key", succeeds=False):
        with tempfile.TemporaryDirectory(prefix="certificate-test-", dir=REPO) as tmp:
            directory = Path(tmp)
            (directory / "run").mkdir()
            (directory / "issued").mkdir()
            for path in self.fixture_dir.iterdir():
                shutil.copyfile(path, directory / path.name)
            # Existing files are deliberately present to catch stale-file success.
            shutil.copyfile(self.fixture_dir / "valid.pem", directory / "issued/fullchain.pem")
            shutil.copyfile(self.fixture_dir / "valid.key", directory / "issued/privkey.pem")
            result = run_bash(FIXTURE, extra_env={
                "TEST_MOCK_DIR": bash_directory(directory), "TEST_FAILURE": failure,
                "TEST_CERTIFICATE": certificate, "TEST_KEY": key, "TEST_CERT_KIND": kind,
            })
            self.assertEqual(result.returncode, 0, result.stderr[-1800:])
            self.assertIn("RESULT_SUCCESS" if succeeds else "RESULT_FAILURE", result.stdout)
            events = (directory / "acme-events").read_text().splitlines()
            if failure == "issue":
                self.assertEqual(events, ["--issue"], "failed issuance must stop before installing stale files")
            if failure == "install":
                self.assertEqual(events, ["--issue", "--installcert"])

    def test_domain_issuance_failure_does_not_reuse_old_certificate(self):
        self.run_case(failure="issue")

    def test_ip_issuance_failure_does_not_reuse_old_certificate(self):
        self.run_case(kind="ip", failure="issue")

    def test_certificate_install_failure_is_not_reported_as_success(self):
        self.run_case(failure="install")

    def test_wrong_domain_certificate_is_rejected(self):
        self.run_case(certificate="other.pem", key="other.key")

    def test_expired_certificate_is_rejected(self):
        self.run_case(certificate="expired.pem")

    def test_mismatched_private_key_is_rejected(self):
        self.run_case(key="other.key")

    def test_valid_domain_certificate_and_key_are_accepted(self):
        self.run_case(succeeds=True)

    def test_valid_ip_certificate_and_key_are_accepted(self):
        self.run_case(kind="ip", succeeds=True)


if __name__ == "__main__":
    unittest.main()
