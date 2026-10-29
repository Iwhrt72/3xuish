"""Verify REALITY handles a local target TLS record larger than the old 8 KiB cap.

Run XRAY_BIN=/path/to/xray python3 tests/reality_target_smoke.py with a current
core. Only disposable loopback servers, keys, certificates and clients are used.
Debug output stays in the private temporary directory; it is never printed.
"""
from contextlib import ExitStack
import copy
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import socket
import ssl
import tempfile
import threading

import runtime_smoke as fixture


OLD_LIMIT = 8192
CURRENT_LIMIT = 17 * 1024


def ordinary_tls(target, cert):
    context = ssl.create_default_context(cafile=str(cert))
    context.minimum_version = ssl.TLSVersion.TLSv1_3
    context.maximum_version = ssl.TLSVersion.TLSv1_3
    context.set_ecdh_curve("X25519")
    context.set_alpn_protocols(["h2", "http/1.1"])
    with socket.create_connection(target.server_address, timeout=5) as connection:
        with context.wrap_socket(connection, server_hostname="localhost") as secured:
            if secured.version() != "TLSv1.3" or secured.selected_alpn_protocol() != "h2":
                raise RuntimeError("target_tls13_h2_control_failed")
            secured.sendall(b"\n")


def run_case(core, directory, cert, key, chain, source_port, *, label):
    with fixture.CamouflageTarget(chain, key) as target:
        ordinary_tls(target, cert)
        item = next(profile for profile in fixture.profiles(core, cert, key, target.server_address[1])
                    if profile["key"] == "reality")
        payload = item["payload"]
        inbound = {name: copy.deepcopy(payload[name]) for name in ("tag", "port", "protocol")}
        inbound.update(listen="127.0.0.1", settings=json.loads(payload["settings"]),
                       streamSettings=json.loads(payload["streamSettings"]))
        inbound["streamSettings"]["realitySettings"]["show"] = True
        server_path = fixture.write(directory / (label + "-server.json"), {
            "log": {"loglevel": "warning"}, "inbounds": [inbound],
            "outbounds": [{"protocol": "freedom", "settings": {"finalRules": [
                {"action": "allow", "network": "tcp", "ip": ["127.0.0.1/32"], "port": str(source_port)}
            ]}}],
        })
        socks_port = fixture.port()
        client_path = fixture.write(directory / (label + "-client.json"), {
            "log": {"loglevel": "warning"}, "outbounds": [item["clientOutbound"]],
            "inbounds": [{"listen": "127.0.0.1", "port": socks_port,
                          "protocol": "socks", "settings": {"auth": "noauth"}}],
        })
        fixture.validate(core, server_path, label + " server")
        fixture.validate(core, client_path, label + " client")
        log_path = directory / (label + "-server.log")
        with fixture.Core(core, server_path, log_path), \
                fixture.Core(core, client_path, directory / (label + "-client.log")):
            destination = directory / (label + "-payload.bin")
            result = fixture.run([fixture.executable("curl"), "-q", "--silent", "--show-error", "--fail",
                "--proxy", "socks5h://127.0.0.1:" + str(socks_port), "--noproxy", "",
                "--connect-timeout", "5", "--max-time", "12", "--output", str(destination),
                "http://127.0.0.1:%d/payload" % source_port])
            if result.returncode or not destination.is_file() or \
                    hashlib.sha256(destination.read_bytes()).digest() != hashlib.sha256(fixture.BODY).digest():
                raise RuntimeError(label + "_reality_transfer_failed")
        private_log = log_path.read_text(encoding="utf-8", errors="replace")
        certificate_lengths = [int(length) for length in re.findall(r"\bCertificate:\s*(\d+)", private_log)]
        if not certificate_lengths:
            raise RuntimeError("target_certificate_record_lengths_not_observed")
        return max(certificate_lengths)


def main():
    core = os.environ.get("XRAY_BIN")
    if not core:
        raise SystemExit("Set XRAY_BIN to the installed test Xray executable")
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="3xuish-reality-target-") as temporary, ExitStack() as resources:
        directory = Path(temporary)
        cert, key = directory / "leaf.pem", directory / "key.pem"
        result = fixture.run([fixture.executable("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(key), "-out", str(cert)], env={**os.environ, "MSYS_NO_PATHCONV": "1"})
        if result.returncode:
            raise RuntimeError("target_certificate_generation_failed")
        der_length = len(ssl.PEM_cert_to_DER_cert(cert.read_text(encoding="utf-8")))
        # Duplicate public chain entries enlarge the Certificate message without
        # changing the trusted leaf or private key. Keep it below one TLS record.
        repeats = (10 * 1024) // der_length + 1
        chain = directory / "large-chain.pem"
        chain.write_bytes(cert.read_bytes() * repeats)
        source = http.server.ThreadingHTTPServer(("127.0.0.1", 0), fixture.Handler)
        thread = threading.Thread(target=source.serve_forever, daemon=True)
        thread.start()
        resources.callback(thread.join, 5)
        resources.callback(source.server_close)
        resources.callback(source.shutdown)
        small = run_case(core, directory, cert, key, cert, source.server_address[1], label="small")
        large = run_case(core, directory, cert, key, chain, source.server_address[1], label="large")
        if not small < OLD_LIMIT < large < CURRENT_LIMIT:
            raise RuntimeError("target_record_fixture_did_not_cross_old_limit")
        print("PASS: TLS 1.3/h2 and REALITY exact 1 MiB transfers; Certificate record sizes small=%d, large=%d bytes" %
              (small, large))
        print("REALITY_TARGET_SMOKE_OK: current core accepts target TLS records above the former 8192-byte cap")


if __name__ == "__main__":
    main()
