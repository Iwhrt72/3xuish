"""Disposable real-Xray transfer test for the server-side speed diagnostic.

Run XRAY_BIN=/path/to/xray python3 tests/server_speed_smoke.py. This creates
temporary loopback listeners and credentials; it never opens the live panel DB.
"""
from contextlib import ExitStack
import copy
import hashlib
import http.server
import json
import os
from pathlib import Path
import socket
import ssl
import sys
import tempfile
import threading

import runtime_smoke as fixture

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from server_speed_probe import probe_download
from server_speed_test import build_outbound, temporary_client


class BinaryHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(len(fixture.BODY)))
        self.end_headers()
        self.wfile.write(fixture.BODY)

    def log_message(self, *_):
        pass


def main():
    core = os.environ.get("XRAY_BIN")
    if not core:
        raise SystemExit("Set XRAY_BIN to the installed test Xray executable")
    curl = fixture.executable("curl")
    with tempfile.TemporaryDirectory(prefix="3xuish-speed-smoke-") as temp, ExitStack() as resources:
        directory = Path(temp)
        cert, key = directory / "cert.pem", directory / "key.pem"
        result = fixture.run([
            fixture.executable("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(key), "-out", str(cert),
        ], env={**os.environ, "MSYS_NO_PATHCONV": "1"})
        if result.returncode:
            raise RuntimeError("test_certificate_generation_failed")
        pin = hashlib.sha256(ssl.PEM_cert_to_DER_cert(cert.read_text())).hexdigest()
        source = http.server.ThreadingHTTPServer(("127.0.0.1", 0), BinaryHandler)
        thread = threading.Thread(target=source.serve_forever, daemon=True)
        thread.start()
        resources.callback(thread.join, 5)
        resources.callback(source.server_close)
        resources.callback(source.shutdown)
        url = "http://127.0.0.1:%d/payload" % source.server_address[1]

        profiles = fixture.profiles(core, cert, key, 1)
        items = [item for item in profiles if item["key"] in {"xhttp", "hy2"}]
        inbounds, records = [], []
        for ident, item in enumerate(items, 1):
            payload = item["payload"]
            database_settings = json.loads(payload["settings"])
            live = {name: copy.deepcopy(payload[name]) for name in ("tag", "port", "protocol")}
            live.update(listen="127.0.0.1", streamSettings=json.loads(payload["streamSettings"]))
            if item["key"] == "hy2":
                # Manually added Hy2 nodes often omit SNI. The diagnostic must
                # derive a verified name from the existing certificate SAN.
                live["streamSettings"]["tlsSettings"].pop("serverName", None)
                stored_stream = json.loads(payload["streamSettings"])
                stored_stream["tlsSettings"].pop("serverName", None)
                payload["streamSettings"] = json.dumps(stored_stream)
                live["settings"] = {"version": 2, "users": [
                    {"auth": client["auth"]} for client in database_settings["clients"]
                ]}
            else:
                live["settings"] = {"decryption": database_settings["decryption"], "clients": [
                    {name: client[name] for name in ("id", "flow", "email")}
                    for client in database_settings["clients"]
                ]}
            row = {"id": ident, "tag": payload["tag"], "remark": payload["remark"],
                   "port": payload["port"], "protocol": payload["protocol"], "enable": 1,
                   "listen": "127.0.0.1", "settings": payload["settings"],
                   "stream_settings": payload["streamSettings"]}
            records.append((item["key"], row, live))
            inbounds.append(live)

        server_path = fixture.write(directory / "server.json", {
            "log": {"loglevel": "none"}, "inbounds": inbounds,
            "outbounds": [{"protocol": "freedom", "settings": {"finalRules": [
                {"action": "allow", "network": "tcp", "ip": ["127.0.0.1/32"],
                 "port": str(source.server_address[1])}
            ]}}],
        })
        server_before = server_path.read_bytes()
        fixture.validate(core, server_path, "speed diagnostic fixture server")
        with fixture.Core(core, server_path, directory / "server.log"):
            for label, row, live in records:
                row_before, live_before = copy.deepcopy(row), copy.deepcopy(live)
                outbound = build_outbound(row, live)
                assert outbound["streamSettings"]["tlsSettings"]["serverName"] == "localhost"
                if label == "xhttp":
                    # The diagnostic must not silently bypass certificate trust.
                    with temporary_client(core, outbound, install_dir=str(directory)) as proxy:
                        rejected = probe_download(url, label="untrusted_certificate", connections=1,
                                                  seconds=2, curl=curl, proxy=proxy, allow_http=True)
                        assert rejected["status"] == "failed", "untrusted_certificate_was_accepted"
                # The pin is restricted to this disposable self-signed fixture.
                outbound["streamSettings"]["tlsSettings"]["pinnedPeerCertSha256"] = pin
                with temporary_client(core, outbound, install_dir=str(directory)) as proxy:
                    assert isinstance(proxy, str) and proxy.startswith("socks5h://127.0.0.1:")
                    result = probe_download(url, label=label, connections=1, seconds=5,
                                            curl=curl, proxy=proxy, allow_http=True)
                    assert result["status"] == "ok", result
                    assert result["bytes"] == len(fixture.BODY), result
                    assert result["valid_connections"] == 1, result
                # Context exit must release its listener, not leave a test proxy.
                socks_port = int(proxy.rsplit(":", 1)[1])
                with socket.socket() as probe:
                    probe.settimeout(.3)
                    assert probe.connect_ex(("127.0.0.1", socks_port)) != 0, "temporary_client_not_stopped"
                assert row == row_before and live == live_before, "source_configuration_modified"
                assert server_path.read_bytes() == server_before, "server_configuration_modified"
                print("PASS: %s existing-inbound mapping and real loopback transfer; temporary client stopped" % label)
        print("SERVER_SPEED_SMOKE_OK")


if __name__ == "__main__":
    main()
