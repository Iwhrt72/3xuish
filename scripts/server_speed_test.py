"""Bounded server-side network and existing-node loopback diagnostics."""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
import copy
import datetime as dt
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import time
import uuid

from server_speed_probe import DEFAULT_URL, probe_download

CLOUDFLARE_URL = "https://speed.cloudflare.com/__down?bytes=100000000"


class DiagnosticError(RuntimeError):
    """Fixed, non-sensitive error codes only."""


def object_value(value):
    try:
        result = json.loads(value) if isinstance(value, (str, bytes)) else copy.deepcopy(value)
    except (ValueError, TypeError):
        raise DiagnosticError("invalid_configuration_json") from None
    if not isinstance(result, dict):
        raise DiagnosticError("invalid_configuration_object")
    return result


def network_name(stream):
    network = stream.get("network", stream.get("method"))
    if network in ("tcp", "raw"):
        return "tcp"
    if network in ("xhttp", "splithttp"):
        return "xhttp"
    return network if isinstance(network, str) and network == "hysteria" else "unsupported"


def local_address(listen):
    if listen in (None, "", "0.0.0.0"):
        return "127.0.0.1"
    if listen == "::":
        return "::1"
    try:
        address = ipaddress.ip_address(listen)
    except (ValueError, TypeError):
        raise DiagnosticError("unsupported_listener_address") from None
    if not address.is_loopback:
        raise DiagnosticError("node_bound_to_non_loopback_address")
    return str(address)


def certificate_server_name(tls):
    """Read a public certificate SAN when a manually configured server has no SNI."""
    openssl = shutil.which("openssl")
    certificates = tls.get("certificates", [])
    if not openssl or not isinstance(certificates, list):
        raise DiagnosticError("tls_server_name_missing")
    for certificate in certificates:
        if not isinstance(certificate, dict) or not isinstance(certificate.get("certificateFile"), str):
            continue
        try:
            result = subprocess.run([openssl, "x509", "-in", certificate["certificateFile"],
                "-noout", "-ext", "subjectAltName"], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if result.returncode:
            continue
        for name in re.findall(r"DNS:([^,\s]+)", result.stdout):
            if len(name) <= 253 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", name):
                return name
        for value in re.findall(r"IP Address:([^,\s]+)", result.stdout):
            try:
                return str(ipaddress.ip_address(value))
            except ValueError:
                pass
    raise DiagnosticError("tls_server_name_missing")


def build_outbound(row, live):
    """Read client data already saved by the installer; never generate keys."""
    if not row.get("enable"):
        raise DiagnosticError("inbound_disabled")
    settings = object_value(row.get("settings"))
    stream = object_value(row.get("stream_settings"))
    actual = object_value(live.get("streamSettings"))
    if (live.get("port") != row.get("port") or live.get("protocol") != row.get("protocol")
            or network_name(stream) != network_name(actual)
            or stream.get("security", "none") != actual.get("security", "none")):
        raise DiagnosticError("database_runtime_mismatch")
    address = local_address(live.get("listen", row.get("listen")))
    if local_address(row.get("listen")) != address:
        raise DiagnosticError("database_runtime_listener_mismatch")
    network = network_name(stream)
    security = stream.get("security", "none")
    if (network not in {"tcp", "xhttp", "hysteria"} or not isinstance(security, str)
            or security not in {"tls", "none", "reality"}):
        raise DiagnosticError("unsupported_node_transport")
    clients = settings.get("clients", [])
    if not isinstance(clients, list):
        raise DiagnosticError("invalid_clients")
    current = object_value(live.get("settings", {}))
    live_clients = current.get("clients", []) if row["protocol"] == "vless" else current.get("users", current.get("clients", []))
    if not isinstance(live_clients, list):
        raise DiagnosticError("invalid_runtime_clients")
    credential_key = "id" if row["protocol"] == "vless" else "auth"
    valid = {item.get(credential_key) for item in live_clients if isinstance(item, dict)
             and isinstance(item.get(credential_key), str) and item.get(credential_key)}
    client = None
    for candidate in clients:
        if not isinstance(candidate, dict) or candidate.get("enable") is False:
            continue
        expiry = candidate.get("expiryTime", 0)
        if not isinstance(expiry, (int, float)) or expiry < 0 or (expiry > 0 and expiry <= time.time() * 1000):
            continue
        credential = candidate.get(credential_key)
        if isinstance(credential, str) and credential and credential in valid:
            client = candidate
            break
    if client is None:
        raise DiagnosticError("no_enabled_runtime_client")
    out_stream = {"network": network, "security": security}
    if row["protocol"] == "vless" and network in {"tcp", "xhttp"}:
        matching = [item for item in live_clients if isinstance(item, dict) and item.get("id") == client["id"]]
        if len(matching) != 1 or matching[0].get("flow", "") != client.get("flow", ""):
            raise DiagnosticError("database_runtime_client_flow_mismatch")
        if settings.get("decryption", "none") != current.get("decryption", "none"):
            raise DiagnosticError("database_runtime_encryption_mismatch")
        encryption = settings.get("encryption")
        if encryption is None and settings.get("decryption", "none") == "none":
            encryption = "none"
        if not isinstance(encryption, str) or not encryption:
            raise DiagnosticError("stored_client_encryption_missing")
        out_settings = {"address": address, "port": row["port"], "id": client["id"],
                        "encryption": encryption, "flow": client.get("flow", "")}
        transport_key = "xhttpSettings" if network == "xhttp" else "tcpSettings"
        options = stream.get(transport_key, {})
        if options != actual.get(transport_key, {}):
            raise DiagnosticError("database_runtime_transport_mismatch")
        out_stream[transport_key] = object_value(options)
    elif row["protocol"] == "hysteria" and network == "hysteria" and security == "tls":
        if settings.get("version", 2) != 2 or actual.get("hysteriaSettings", {}).get("version", 2) != 2:
            raise DiagnosticError("unsupported_hysteria_version")
        out_settings = {"version": 2, "address": address, "port": row["port"]}
        out_stream["hysteriaSettings"] = {"version": 2, "auth": client["auth"]}
    else:
        raise DiagnosticError("unsupported_node_protocol")
    if "finalmask" in actual:
        out_stream["finalmask"] = copy.deepcopy(actual["finalmask"])
    if security == "tls":
        tls = object_value(stream.get("tlsSettings"))
        actual_tls = object_value(actual.get("tlsSettings"))
        sni = tls.get("serverName")
        actual_sni = actual_tls.get("serverName")
        if sni and actual_sni and sni != actual_sni:
            raise DiagnosticError("database_runtime_tls_mismatch")
        sni = sni or actual_sni or certificate_server_name(actual_tls)
        if not isinstance(sni, str):
            raise DiagnosticError("tls_server_name_missing")
        client_tls = {"serverName": sni, "allowInsecure": False}
        for key in ("alpn", "minVersion", "maxVersion"):
            if key in tls:
                client_tls[key] = copy.deepcopy(tls[key])
        ui = tls.get("settings", {})
        fingerprint = ui.get("fingerprint") if isinstance(ui, dict) else None
        if fingerprint:
            client_tls["fingerprint"] = fingerprint
        out_stream["tlsSettings"] = client_tls
    elif security == "reality":
        reality = object_value(stream.get("realitySettings"))
        ui = object_value(reality.get("settings", {}))
        names, short_ids = reality.get("serverNames", []), reality.get("shortIds", [])
        public = ui.get("publicKey")
        if not public or not names or not short_ids:
            raise DiagnosticError("stored_reality_client_parameters_missing")
        out_stream["realitySettings"] = {"serverName": ui.get("serverName") or names[0],
            "fingerprint": ui.get("fingerprint", "chrome"), "password": public,
            "shortId": short_ids[0], "spiderX": ui.get("spiderX", "/")}
    return {"tag": "node-under-test", "protocol": row["protocol"],
            "settings": out_settings, "streamSettings": out_stream}


@contextmanager
def temporary_client(xray, outbound, install_dir=None):
    """Use an isolated loopback SOCKS listener; clean up only our own child."""
    with tempfile.TemporaryDirectory(prefix="3xuish-speed-client-") as temp:
        directory = Path(temp)
        os.chmod(directory, 0o700)
        with closing(socket.socket()) as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        config = {"log": {"loglevel": "warning"}, "inbounds": [{"listen": "127.0.0.1", "port": port,
                  "protocol": "socks", "settings": {"auth": "noauth", "udp": True}}], "outbounds": [outbound]}
        path = directory / "client.json"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle)
        try:
            checked = subprocess.run([str(xray), "run", "-test", "-c", str(path)],
                cwd=install_dir, capture_output=True, timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            raise DiagnosticError("temporary_client_validation_failed") from None
        if checked.returncode:
            raise DiagnosticError("temporary_client_configuration_rejected")
        with open(directory / "client.log", "wb") as log:
            process = subprocess.Popen([str(xray), "run", "-c", str(path)], cwd=install_dir,
                stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 8
                while time.monotonic() < deadline:
                    if process.poll() is not None:
                        raise DiagnosticError("temporary_client_start_failed")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=.2):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise DiagnosticError("temporary_client_start_timeout")
                yield "socks5h://127.0.0.1:" + str(port)
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


def load_nodes(database, runtime_path):
    uri = Path(database).resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=10)) as connection:
        connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute("SELECT * FROM inbounds ORDER BY id")]
    runtime = object_value(Path(runtime_path).read_text(encoding="utf-8"))
    return rows, runtime


def host_snapshot():
    snapshot = {"cpu_count": os.cpu_count()}
    try:
        snapshot["load_average_1m"] = round(os.getloadavg()[0], 2)
    except (OSError, AttributeError):
        pass
    for name, filename in (("tcp_congestion_control", "/proc/sys/net/ipv4/tcp_congestion_control"),
                           ("qdisc", "/proc/sys/net/core/default_qdisc")):
        try:
            value = Path(filename).read_text().strip()
            if value and len(value) <= 32 and value.replace("_", "").isalnum():
                snapshot[name] = value
        except OSError:
            pass
    return snapshot


def resolve_xray(install_dir, specified=None):
    if specified:
        candidate = Path(specified).resolve()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
        raise DiagnosticError("xray_binary_missing")
    matches = [path for path in (Path(install_dir) / "bin").glob("xray-linux-*")
               if path.is_file() and os.access(path, os.X_OK)]
    if len(matches) != 1:
        raise DiagnosticError("unique_xray_binary_not_found")
    return matches[0].resolve()


def run_measurements(label, seconds, curl, proxy=None):
    results = []
    for source, url, connections in (("OVH", DEFAULT_URL, 1), ("OVH", DEFAULT_URL, 4),
                                     ("Cloudflare", CLOUDFLARE_URL, 1)):
        print("测试 %s / %s，%d 连接，每连接最多 %d 秒……" % (label, source, connections, seconds), flush=True)
        result = probe_download(url, label=source, connections=connections,
                                seconds=seconds, curl=curl, proxy=proxy)
        result["route"] = label
        results.append(result)
        speed = result.get("mbps")
        print("  %s，%s Mbps" % (result["status"], "无有效结果" if speed is None else speed), flush=True)
    return results


def write_report(directory, report):
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    fd = os.open(directory / "report.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, choices=range(1, 16), default=10)
    parser.add_argument("--direct-only", action="store_true")
    parser.add_argument("--inbound-id", type=int, action="append", default=[])
    parser.add_argument("--database", default=os.environ.get("XUI_DB_PATH", "/etc/x-ui/x-ui.db"))
    parser.add_argument("--install-dir", default=os.environ.get("XUI_INSTALL_DIR", "/usr/local/x-ui"))
    parser.add_argument("--xray", default=os.environ.get("XRAY_BIN"))
    parser.add_argument("--report-root", default="/root/3xuish-speed-test")
    args = parser.parse_args(argv)
    curl = shutil.which("curl")
    if not curl:
        parser.error("需要 curl；脚本不会自动安装软件。")
    if any(ident < 1 for ident in args.inbound_id):
        parser.error("--inbound-id 必须为正整数。")
    os.umask(0o077)
    report = {"started_at": dt.datetime.now(dt.timezone.utc).isoformat(), "host": host_snapshot(),
        "configuration_modified": False, "panel_restarted": False, "tests": [], "nodes": [],
        "limitations": ["服务器下载和本机回环测试，不代表电脑到服务器的实际节点速度。",
                        "测试会消耗流量，并计入所测节点的用量；不测试公网入站可达性。",
                        "单次 HTTP 下载路径不能证明套餐带宽上限；未测试上传速度。"]}
    candidates = []
    if not args.direct_only:
        try:
            xray = resolve_xray(args.install_dir, args.xray)
            rows, runtime = load_nodes(args.database, Path(args.install_dir) / "bin/config.json")
            selected = [row for row in rows if row.get("enable") and row.get("protocol") in {"vless", "hysteria"}
                        and (not args.inbound_id or row["id"] in args.inbound_id)]
            if not selected:
                report["node_inspection_error"] = "no_enabled_supported_inbounds"
            if set(args.inbound_id) - {row["id"] for row in selected}:
                report["node_inspection_error"] = "requested_inbound_unavailable"
            report["omitted_node_count"] = max(len(selected) - 4, 0)
            for row in selected[:4]:
                entry = {"id": row["id"], "port": row["port"], "status": "pending"}
                report["nodes"].append(entry)
                try:
                    matches = [live for live in runtime.get("inbounds", []) if live.get("tag") == row["tag"]]
                    if len(matches) != 1:
                        raise DiagnosticError("runtime_inbound_missing_or_duplicated")
                    outbound = build_outbound(row, matches[0])
                    entry["transport"] = network_name(outbound["streamSettings"])
                    candidates.append((entry, outbound))
                except Exception as exc:
                    entry.update(status="skipped", error=str(exc) if isinstance(exc, DiagnosticError) else type(exc).__name__)
        except Exception as exc:
            report["node_inspection_error"] = str(exc) if isinstance(exc, DiagnosticError) else type(exc).__name__
    print("不修改节点配置，不重启面板。测试期间会消耗流量并增加节点流量统计。", flush=True)
    print("默认测试最多约 %.1f GiB，最多约 %d 秒采样；按 Ctrl+C 停止。" %
          ((len(candidates) + 1) * 600 / 1024, (len(candidates) + 1) * 3 * args.seconds), flush=True)
    report["tests"].extend(run_measurements("direct", args.seconds, curl))
    for entry, outbound in candidates:
        label = "inbound-%d-%s" % (entry["id"], entry["transport"])
        try:
            with temporary_client(xray, outbound, install_dir=args.install_dir) as proxy:
                measured = run_measurements(label, args.seconds, curl, proxy)
            report["tests"].extend(measured)
            entry["status"] = "tested" if any(item["status"] in {"ok", "partial"} for item in measured) else "failed"
        except Exception as exc:
            entry.update(status="failed", error=str(exc) if isinstance(exc, DiagnosticError) else type(exc).__name__)
    report["finished_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    report["host_after"] = host_snapshot()
    valid = any(test["status"] in {"ok", "partial"} for test in report["tests"])
    report["status"] = ("complete" if valid and all(test["status"] == "ok" for test in report["tests"])
        and all(node["status"] == "tested" for node in report["nodes"])
        and not report.get("omitted_node_count")
        and "node_inspection_error" not in report else "partial" if valid else "failed")
    root = Path(args.report_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory = root / (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
    write_report(directory, report)
    print("\n结果（Mbps，十进制；本机回环不是客户端线路测速）：")
    print("%-24s %-12s %4s %12s %8s %8s" % ("route", "source", "conn", "Mbps", "valid", "status"))
    for test in report["tests"]:
        speed = "-" if test.get("mbps") is None else str(test["mbps"])
        print("%-24s %-12s %4d %12s %8s %8s" % (test["route"], test["label"], test["connections"],
              speed, "%d/%d" % (test["valid_connections"], test["connections"]), test["status"]))
    print("节点检查：" + json.dumps(report["nodes"], ensure_ascii=False))
    if "node_inspection_error" in report:
        print("节点检查错误：" + report["node_inspection_error"])
    if report.get("omitted_node_count"):
        print("为控制流量，本次有 %d 个节点未测；可用 --inbound-id 指定。" % report["omitted_node_count"])
    print("总体状态：" + report["status"])
    print("\n脱敏报告：" + str(directory / "report.json"), flush=True)
    return 0 if report["status"] == "complete" else 1


if __name__ == "__main__":
    if hasattr(signal, "SIGTERM"):
        def interrupted(*_):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupted)
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("测试已停止；临时客户端已清理。")
        raise SystemExit(130)
