"""Optional real-Xray validation and loopback transfer checks.

Run ``XRAY_BIN=/path/to/xray python3 tests/runtime_smoke.py``. Requires Bash,
jq, OpenSSL and curl. It uses temporary files and loopback listeners only.
No production configuration, subscription, firewall, or service is modified.
"""
from __future__ import annotations

import copy
from contextlib import ExitStack
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import socket
import socketserver
import ssl
import subprocess
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
GIT = Path(r"C:\Program Files\Git")
LOCAL_CORE = Path(r"D:\v2\v2rayN-windows-64-SelfContained\bin\xray\xray.exe")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
BODY = bytes(range(256)) * 4096


def executable(name: str) -> str:
    if os.name == "nt":
        candidate = GIT / ("bin" if name == "bash" else "mingw64/bin") / (name + ".exe")
        if candidate.is_file():
            return str(candidate)
    value = shutil.which(name)
    if not value:
        raise RuntimeError(name + " is required")
    return value


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          creationflags=NO_WINDOW, timeout=20, **kwargs)


def port(udp: bool = False) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM if udp else socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def validate(core: str, path: Path, label: str) -> None:
    result = run([core, "run", "-test", "-c", str(path)])
    if result.returncode:
        # Never echo raw core diagnostics: config errors can contain credentials.
        raise RuntimeError(label + " configuration was rejected by Xray")


def write(path: Path, value: dict) -> Path:
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


class Core:
    def __init__(self, core: str, config: Path, log: Path):
        self.core, self.config, self.log = core, config, log
        self.proc = self.handle = None

    def __enter__(self):
        self.handle = self.log.open("w", encoding="utf-8")
        self.proc = subprocess.Popen([self.core, "run", "-c", str(self.config)],
                                     stdout=self.handle, stderr=subprocess.STDOUT,
                                     creationflags=NO_WINDOW)
        time.sleep(0.35)
        if self.proc.poll() is not None:
            self.__exit__()
            raise RuntimeError("Temporary Xray process failed to start")
        return self

    def __exit__(self, *_):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        if self.handle:
            self.handle.close()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *_):
        pass


class CamouflageHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.server.connections += 1
        self.request.settimeout(5)
        try:
            with self.server.context.wrap_socket(self.request, server_side=True) as connection:
                # REALITY needs the TLS 1.3 handshake, not an HTTP download from
                # the camouflage endpoint. Wait for its normal close/first byte.
                connection.recv(1)
        except OSError:
            pass


class CamouflageTarget(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, cert: Path, key: Path):
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.minimum_version = ssl.TLSVersion.TLSv1_3
        self.context.maximum_version = ssl.TLSVersion.TLSv1_3
        self.context.set_ecdh_curve("X25519")
        self.context.set_alpn_protocols(["h2", "http/1.1"])
        self.context.load_cert_chain(cert, key)
        self.connections = 0
        super().__init__(("127.0.0.1", 0), CamouflageHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


def profiles(core: str, cert: Path, key: Path, camouflage_port: int) -> list[dict]:
    generated = run([core, "vlessenc"])
    dec = re.findall(r'"decryption"\s*:\s*"([^\"]+)"', generated.stdout)
    enc = re.findall(r'"encryption"\s*:\s*"([^\"]+)"', generated.stdout)
    reality = run([core, "x25519"])
    private = re.search(r"PrivateKey:\s*(\S+)", reality.stdout)
    public = re.search(r"(?:Password \(PublicKey\)|PublicKey):\s*(\S+)", reality.stdout)
    if generated.returncode or reality.returncode or not dec or not enc or not private or not public:
        raise RuntimeError("Could not generate real Xray test keys")
    environment = os.environ.copy()
    environment.update({
        "SMOKE_CERT": cert.as_posix(), "SMOKE_KEY": key.as_posix(),
        "SMOKE_DEC": dec[0], "SMOKE_ENC": enc[0],
        "SMOKE_PRIVATE": private[1], "SMOKE_PUBLIC": public[1],
        "SMOKE_RAW_PORT": str(port()), "SMOKE_REALITY_PORT": str(port()),
        "SMOKE_HY2_PORT": str(port(True)), "SMOKE_XHTTP_PORT": str(port()),
        "SMOKE_CAMOUFLAGE_PORT": str(camouflage_port),
    })
    shell = r'''
source ./install.sh
SERVER_IP=127.0.0.1
CERT_HOST=localhost
CERT_FILE="$SMOKE_CERT"
KEY_FILE="$SMOKE_KEY"
RAW_PORT="$SMOKE_RAW_PORT"
REALITY_PORT="$SMOKE_REALITY_PORT"
HY2_PORT="$SMOKE_HY2_PORT"
XHTTP_PORT="$SMOKE_XHTTP_PORT"
XHTTP_PATH=/smoke
INBOUND_TLS_MODE=1
PRIMARY_NODE=hy2
VLESS_DECRYPTION="$SMOKE_DEC"
VLESS_ENCRYPTION="$SMOKE_ENC"
VLESS_AUTH_LABEL=X25519
REALITY_PRIVATE_KEY="$SMOKE_PRIVATE"
REALITY_PUBLIC_KEY="$SMOKE_PUBLIC"
REALITY_SHORT_ID=0123456789abcdef
REALITY_SERVER_NAME=localhost
REALITY_TARGET="127.0.0.1:$SMOKE_CAMOUFLAGE_PORT"
prepare_node_profiles
printf '%s\n' "$NODE_PROFILES"
'''
    result = run([executable("bash"), "-lc", shell], cwd=ROOT, env=environment)
    if result.returncode:
        raise RuntimeError("Installer profile generation failed")
    return json.loads(result.stdout)


def main() -> None:
    core = os.environ.get("XRAY_BIN")
    if not core and os.name == "nt" and LOCAL_CORE.is_file():
        core = str(LOCAL_CORE)
    if not core:
        raise SystemExit("Set XRAY_BIN to an installed Xray 26.10.21+ executable")
    curl = shutil.which("curl.exe" if os.name == "nt" else "curl")
    if not curl:
        raise RuntimeError("curl is required")
    with tempfile.TemporaryDirectory(prefix="3xuish-runtime-") as temp, ExitStack() as resources:
        directory = Path(temp)
        cert, key = directory / "cert.pem", directory / "key.pem"
        result = run([executable("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                      "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
                      "-keyout", key.as_posix(), "-out", cert.as_posix()],
                     env={**os.environ, "MSYS_NO_PATHCONV": "1"})
        if result.returncode:
            raise RuntimeError("Temporary test certificate generation failed")
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        camouflage = resources.enter_context(CamouflageTarget(cert, key))
        target_tls = ssl.create_default_context(cafile=str(cert))
        target_tls.minimum_version = ssl.TLSVersion.TLSv1_3
        with socket.create_connection(camouflage.server_address, timeout=5) as connection:
            with target_tls.wrap_socket(connection, server_hostname="localhost") as connection:
                if connection.version() != "TLSv1.3":
                    raise RuntimeError("Local camouflage target did not negotiate TLS 1.3")
                connection.sendall(b"\n")
        target_connections = camouflage.connections
        items = profiles(core, cert, key, camouflage.server_address[1])
        source = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=source.serve_forever, daemon=True)
        thread.start()
        source_port = source.server_address[1]
        try:
            inbounds = []
            for item in items:
                payload = item["payload"]
                inbound = {name: copy.deepcopy(payload[name]) for name in ("tag", "port", "protocol")}
                inbound["listen"] = "127.0.0.1"
                inbound["settings"] = json.loads(payload["settings"])
                inbound["streamSettings"] = json.loads(payload["streamSettings"])
                assert "fallbacks" not in inbound["settings"]
                if inbound["streamSettings"]["security"] == "tls":
                    tls = inbound["streamSettings"]["tlsSettings"]
                    assert tls["minVersion"] == "1.0" and tls["maxVersion"] == "1.3"
                inbounds.append(inbound)
            server = {"log": {"loglevel": "warning"}, "inbounds": inbounds,
                      "outbounds": [{"protocol": "freedom", "settings": {"finalRules": [
                          {"action": "allow", "network": "tcp", "ip": ["127.0.0.1/32"],
                           "port": str(source_port)}]}}]}
            server_path = write(directory / "server.json", server)
            validate(core, server_path, "combined server")
            clients = []
            for item in items:
                outbound = copy.deepcopy(item["clientOutbound"])
                tls = outbound["streamSettings"].get("tlsSettings")
                if tls:
                    # Only the disposable fixture pins its self-signed certificate.
                    tls["pinnedPeerCertSha256"] = pin
                socks = port()
                client = {"log": {"loglevel": "warning"}, "outbounds": [outbound],
                          "inbounds": [{"listen": "127.0.0.1", "port": socks,
                                        "protocol": "socks", "settings": {"auth": "noauth"}}]}
                path = write(directory / (item["key"] + ".json"), client)
                validate(core, path, item["key"] + " client")
                clients.append((item["key"], path, socks))
            print("PASS: generated combined server and all four client configurations accepted by Xray")
            with Core(core, server_path, directory / "server.log"):
                for label, path, socks in clients:
                    with Core(core, path, directory / (label + ".log")):
                        destination = directory / (label + ".bin")
                        response = run([curl, "--silent", "--show-error", "--fail", "--proxy",
                                        f"socks5h://127.0.0.1:{socks}", "--noproxy", "",
                                        "--connect-timeout", "5", "--max-time", "12",
                                        "--output", str(destination),
                                        f"http://127.0.0.1:{source_port}/payload"])
                        if response.returncode or not destination.is_file() or \
                                hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(BODY).digest():
                            raise RuntimeError(label + " did not complete the exact loopback payload")
                    print("PASS: " + label + " loopback download, 1 MiB with SHA256 match")
            # REALITY borrows the target's handshake response, then may close
            # before finishing that independent TLS session. Count acceptance,
            # while the local preflight above verifies TLS 1.3 capability.
            if camouflage.connections <= target_connections:
                raise RuntimeError("REALITY did not contact the local TLS 1.3 camouflage target")
        finally:
            source.shutdown()
            source.server_close()


if __name__ == "__main__":
    main()
