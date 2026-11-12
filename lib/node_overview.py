"""Read panel nodes and existing subscription addresses without changing them."""
from __future__ import annotations

import argparse
from contextlib import closing
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata
from urllib.parse import quote, urlsplit, urlunsplit


DEFAULTS = {"subEnable": "true", "subPort": "2096", "subPath": "/sub/", "subURI": "",
            "subListen": "", "subDomain": "", "subCertFile": "", "subKeyFile": "",
            "subClashEnable": "false", "subClashPath": "/clash/", "subClashURI": ""}
RAW_PROTOCOLS = {"vmess", "vless", "trojan", "shadowsocks", "hysteria", "mtproto", "wireguard", "amneziawg", "tuic"}
STATUS_TEXT = {
    "no_previous_job": "暂无本脚本保存的代理创建记录；这不代表面板中没有节点。",
    "starting": "任务已启动，正在准备。", "running": "任务正在执行。",
    "complete": "创建完成，已通过服务器本地 DNS 和 HTTPS 验证。",
    "failed": "创建前检查失败，尚未发布新节点。",
    "rolled_back": "创建失败，已确认移除本次新增节点和路由。",
    "manual_recovery_required": "恢复状态尚未确认，需要人工检查。",
    "error": "暂时无法读取创建任务记录；面板节点仍可独立查看。",
}
PHASE_TEXT = {"queued": "等待执行", "preflight": "准备检查", "checking_upstream": "验证上游代理",
              "preparing_panel": "读取面板配置", "creating_disabled_inbound": "创建禁用入站",
              "applying_template": "保存出口与路由", "enabling_inbound": "启用新节点",
              "verifying_node": "验证新节点 DNS 和 HTTPS", "rolling_back": "补偿本次修改",
              "finished": "已结束"}


def text(value, limit=240):
    """Keep arbitrary panel labels and state fields from controlling a terminal."""
    value = str(value if value is not None else "")
    value = "".join(c if unicodedata.category(c) not in {"Cc", "Cf", "Cs"} else " " for c in value)
    return value[:limit] + ("…" if len(value) > limit else "")


def _number(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _enabled(value, default=True):
    if value is None:
        return default
    return value is True or value == 1 or (isinstance(value, str) and value.lower() in {"true", "1"})


def _object(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("not an object")
    return value


def _host(value, explicit=False):
    if not isinstance(value, str) or not value or value != value.strip():
        return ""
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    if any(c.isspace() or unicodedata.category(c).startswith("C") for c in value) or any(c in value for c in "/@?#%\\"):
        return ""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        try:
            value = value.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError:
            return ""
        if (not value or len(value) > 253 or re.fullmatch(r"[0-9.]+", value) or
                any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in value.split("."))):
            return ""
        return value if explicit or value != "localhost" else ""
    if address.is_unspecified or address.is_multicast or (address.is_loopback and not explicit):
        return ""
    return "[" + str(address) + "]" if address.version == 6 else str(address)


def _saved_host(info_dir, tls=False):
    try:
        profiles = json.loads((Path(info_dir) / "3x-ui-node-profiles.json").read_text(encoding="utf-8"))
        if not isinstance(profiles, list):
            return ""
        candidates = []
        for profile in profiles:
            if not isinstance(profile, dict):
                continue
            outbound = profile.get("clientOutbound", {})
            if not isinstance(outbound, dict):
                continue
            stream = outbound.get("streamSettings", {})
            if tls:
                # A saved TLS client hostname is an intentional certificate
                # name. A REALITY disguise hostname never identifies this VPS.
                if not isinstance(stream, dict) or stream.get("security") != "tls":
                    continue
                tls_settings = stream.get("tlsSettings", {})
                host = _host(tls_settings.get("serverName", "")) if isinstance(tls_settings, dict) else ""
                if host:
                    return host
            settings = outbound.get("settings", {})
            host = _host(settings.get("address", "")) if isinstance(settings, dict) else ""
            if host:
                candidates.append(host)
        return candidates[0] if candidates else ""
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return ""


def _base_uri(value):
    if not value:
        return ""
    if not isinstance(value, str) or any(c.isspace() or unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("invalid URL")
    parts = urlsplit(value)
    if (parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or
            parts.password is not None or parts.query or parts.fragment or not _host(parts.hostname, True)):
        raise ValueError("invalid URL")
    if parts.port is not None and not 1 <= parts.port <= 65535:
        raise ValueError("invalid port")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _path(value):
    if not isinstance(value, str) or any(c in value for c in "?#\\") or any(
            c.isspace() or unicodedata.category(c).startswith("C") for c in value):
        raise ValueError("invalid path")
    return "/" + value.strip("/") + "/" if value.strip("/") else "/"


def _url(settings, sub_id, host, clash=False):
    uri_key, path_key = ("subClashURI", "subClashPath") if clash else ("subURI", "subPath")
    custom = settings.get(uri_key, "")
    if custom:
        base = _base_uri(custom)
    elif clash and settings.get("subURI"):
        # A reverse proxy's public origin must also serve the Clash endpoint;
        # falling back to the local subscription listener port would be wrong.
        origin = urlsplit(_base_uri(settings["subURI"]))
        base = urlunsplit((origin.scheme, origin.netloc, _path(settings[path_key]).rstrip("/"), "", ""))
    else:
        if not host:
            return "", "无法确定订阅服务地址；请设置面板订阅地址，或使用 XUI_SERVER_IP / XUI_DOMAIN 指定。"
        port = _number(settings.get("subPort"))
        if not 1 <= port <= 65535:
            return "", "订阅服务端口配置无效。"
        scheme = "https" if settings.get("subCertFile") and settings.get("subKeyFile") else "http"
        port_suffix = "" if (scheme, port) in (("http", 80), ("https", 443)) else f":{port}"
        base = f"{scheme}://{host}{port_suffix}" + _path(settings[path_key]).rstrip("/")
    return base + "/" + quote(sub_id, safe=""), ""


def _subscription_id(value):
    if not isinstance(value, str) or not value:
        return ""
    if any(c in "/\\" or c.isspace() or unicodedata.category(c).startswith("C") for c in value):
        return ""
    return value


def _state(enabled, expiry, now_ms, total=0, used=0):
    if not enabled:
        return "已停用"
    if expiry > 0 and expiry <= now_ms:
        return "已到期"
    if total > 0 and used >= total:
        return "流量已用尽"
    if expiry < 0:
        return "已启用，首次连接后计时"
    return "已启用"


def sanitize_task(value):
    if not isinstance(value, dict):
        return {"status": "error"}
    status = value.get("status")
    if not isinstance(status, str) or status not in STATUS_TEXT:
        status = "error"
    task = {"status": status}
    for key in ("phase", "inbound_id", "port", "name", "exit_ip", "backup", "log_file", "job_directory", "updated_at"):
        if isinstance(value.get(key), (str, int)):
            task[key] = text(value[key], 1000 if key in ("backup", "log_file", "job_directory") else 240)
    return task


def _columns(connection, table):
    return {row[1] for row in connection.execute('PRAGMA table_info("' + table + '")')}


def _read_panel(database):
    path = Path(database).absolute()
    if not path.is_file():
        return None, None, "missing"
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            cols = _columns(connection, "inbounds")
            if not {"id", "protocol", "port", "settings"}.issubset(cols):
                return None, None, "unavailable"
            allowed = ("id", "remark", "protocol", "port", "enable", "settings", "stream_settings",
                       "expiry_time", "up", "down", "total", "exclude_from_sub")
            selected = [name for name in allowed if name in cols]
            rows = [dict(row) for row in connection.execute(
                "SELECT " + ",".join('"' + key + '"' for key in selected) + " FROM inbounds ORDER BY id")]
            # 3x-ui v3.11 stores clients centrally. The JSON client arrays may
            # be stale after a client edit, so associations are authoritative.
            client_cols = _columns(connection, "clients")
            association_cols = _columns(connection, "client_inbounds")
            if {"id", "email", "sub_id"}.issubset(client_cols) and {"client_id", "inbound_id"}.issubset(association_cols):
                client_fields = [key for key in ("id", "email", "sub_id", "enable", "expiry_time", "total_gb") if key in client_cols]
                query = ("SELECT ci.inbound_id," + ",".join('c."' + key + '"' for key in client_fields) +
                         " FROM client_inbounds ci JOIN clients c ON c.id=ci.client_id ORDER BY c.id")
                associated = {}
                for item in connection.execute(query):
                    client = dict(item)
                    client["subId"] = client.pop("sub_id", "")
                    client["expiryTime"] = client.pop("expiry_time", 0)
                    associated.setdefault(client.pop("inbound_id"), []).append(client)
                for row in rows:
                    row["_clients"] = associated.get(row["id"], [])
            settings = dict(DEFAULTS)
            if {"key", "value"}.issubset(_columns(connection, "settings")):
                placeholders = ",".join("?" for _ in DEFAULTS)
                for row in connection.execute("SELECT key,value FROM settings WHERE key IN (" + placeholders + ")", tuple(DEFAULTS)):
                    # Match 3x-ui's defaults for absent/empty scalar settings.
                    settings[row["key"]] = row["value"] if row["value"] not in (None, "") else DEFAULTS[row["key"]]
            return rows, settings, "ok"
    except (sqlite3.Error, OSError, ValueError):
        return None, None, "unavailable"


def build_report(database, info_dir, host="", status=None, now_ms=None):
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    report = {"database_status": "ok", "nodes": [], "subscriptions": [], "subscription_service": {},
              "task": sanitize_task(status if status is not None else {"status": "no_previous_job"}), "warnings": []}
    rows, settings, report["database_status"] = _read_panel(database)
    if report["database_status"] != "ok":
        report["warnings"].append("未找到面板数据库；尚未安装面板或数据库路径不同。" if report["database_status"] == "missing"
                                  else "暂时无法读取面板节点；数据库可能忙碌、损坏或结构不兼容。")
        return report
    # subDomain is a Host restriction on the subscription listener, not merely
    # a label. It must win over a discovered IP address or an old manifest.
    tls = bool(settings["subCertFile"] and settings["subKeyFile"])
    domain = settings.get("subDomain", "")
    chosen_host = _host(domain, True) if domain else (_host(host, True) if host else _saved_host(info_dir, tls))
    if domain and not chosen_host:
        report["warnings"].append("面板的订阅域名限制配置无效，未回退到 IP 地址。")
    if host and not chosen_host:
        report["warnings"].append("指定的订阅主机无效，未自动替换为其他地址。")
    enabled = _enabled(settings["subEnable"], False)
    clash_enabled = _enabled(settings["subClashEnable"], False)
    report["subscription_service"] = {"enabled": enabled, "clash_enabled": clash_enabled, "host": chosen_host,
        "scheme": "https" if tls else "http", "port": _number(settings["subPort"])}
    groups = {}
    for row in rows:
        identity = _number(row["id"])
        is_enabled = _enabled(row.get("enable"))
        state = _state(is_enabled, _number(row.get("expiry_time")), now_ms,
                       _number(row.get("total")), _number(row.get("up")) + _number(row.get("down")))
        node = {"id": identity, "name": text(row.get("remark")) or f"节点 {identity}",
                "protocol": text(row["protocol"], 30), "transport": "", "security": "",
                "port": _number(row["port"]), "enabled": is_enabled, "state": state,
                "subscription_excluded": _enabled(row.get("exclude_from_sub"), False), "clients": []}
        try:
            stream = _object(row.get("stream_settings") or "{}")
            node["transport"] = text(stream.get("network", ""), 30)
            node["security"] = text(stream.get("security", ""), 30)
        except (TypeError, ValueError):
            report["warnings"].append(f"节点 #{identity} 的传输配置无法解析。")
        node["subscription_supported"] = node["protocol"].lower() in RAW_PROTOCOLS and node["transport"] != "xdrive"
        try:
            clients = row["_clients"] if "_clients" in row else _object(row["settings"]).get("clients", [])
            if not isinstance(clients, list):
                raise ValueError
        except (TypeError, ValueError):
            clients = []
            report["warnings"].append(f"节点 #{identity} 的客户端配置无法解析，无法确定订阅。")
        for index, client in enumerate(clients, 1):
            if not isinstance(client, dict):
                report["warnings"].append(f"节点 #{identity} 的第 {index} 项客户端资料无效。")
                continue
            client_enabled = _enabled(client.get("enable"))
            item = {"name": text(client.get("email")) or f"客户端 {index}", "enabled": client_enabled,
                    "state": _state(client_enabled, _number(client.get("expiryTime")), now_ms),
                    "subscription_ref": "", "subscription_reason": ""}
            sub_id = _subscription_id(client.get("subId"))
            if not node["subscription_supported"]:
                item["subscription_reason"] = "此协议或传输的通用订阅导出能力未确认，请在面板查看分享方式。"
            elif node["subscription_excluded"]:
                item["subscription_reason"] = "此入站已在面板中排除订阅。"
            elif not is_enabled:
                item["subscription_reason"] = "此入站已停用，订阅不会返回它。"
            if not sub_id:
                item["subscription_reason"] = "未设置有效订阅 ID，请在面板中检查该客户端的订阅设置。"
            else:
                if sub_id not in groups:
                    group = {"ref": "S" + str(len(groups) + 1), "nodes": [], "clients": [],
                             "url": "", "clash_url": "", "state": "configured", "reason": ""}
                    if not enabled:
                        group.update(state="disabled", reason="面板订阅服务未开启；请在面板的订阅设置中开启后再查看。")
                    else:
                        try:
                            group["url"], group["reason"] = _url(settings, sub_id, chosen_host)
                            if not group["url"]:
                                group["state"] = "unavailable"
                        except ValueError:
                            group.update(state="unavailable", reason="订阅地址或路径配置无效，请在面板订阅设置中检查。")
                        if clash_enabled:
                            try:
                                group["clash_url"], clash_reason = _url(settings, sub_id, chosen_host, True)
                                if clash_reason:
                                    report["warnings"].append("Clash/Mihomo：" + clash_reason)
                            except ValueError:
                                report["warnings"].append("Clash/Mihomo 订阅地址或路径无效；通用订阅独立显示。")
                    groups[sub_id] = group
                group = groups[sub_id]
                if identity not in group["nodes"]:
                    group["nodes"].append(identity)
                label = f"#{identity} {item['name']}"
                if label not in group["clients"]:
                    group["clients"].append(label)
                item["subscription_ref"] = group["ref"]
            node["clients"].append(item)
        report["nodes"].append(node)
    report["subscriptions"] = list(groups.values())
    return report


def render_task(task):
    task = sanitize_task(task)
    lines = ["最近一次代理创建任务", "  " + STATUS_TEXT[task["status"]]]
    if task.get("phase") in PHASE_TEXT and task["status"] in ("starting", "running"):
        lines.append("  当前步骤：" + PHASE_TEXT[task["phase"]])
    if task["status"] == "complete":
        if task.get("inbound_id"):
            lines.append("  新节点：#" + task["inbound_id"] + (" " + task["name"] if task.get("name") else ""))
        if task.get("port"):
            lines.append("  入口端口：TCP/" + task["port"])
        if task.get("exit_ip"):
            lines.append("  实际出口：" + task["exit_ip"])
        lines.append("  请在菜单 22 查看订阅，或从面板导入节点，再验证手机网络。")
    if task["status"] in ("starting", "running"):
        lines.append("  可以断开 SSH；稍后选择菜单 22 或运行 --proxy-node-status 查看结果。")
    if task["status"] in ("failed", "rolled_back", "manual_recovery_required", "error"):
        if task.get("log_file"):
            lines.append("  详细原因见私有日志：" + task["log_file"])
        if task.get("backup"):
            lines.append("  数据库备份：" + task["backup"])
    return "\n".join(lines)


def render_report(report):
    nodes = report["nodes"]
    lines = ["面板节点与订阅", ""]
    if report["database_status"] == "ok":
        lines.append(f"当前面板共 {len(nodes)} 个入站节点，已启用 {sum(n['enabled'] for n in nodes)} 个。")
        lines.append("包含面板中现存的手工、旧脚本和代理节点；已删除的节点无法从当前数据库恢复。")
        lines.append("订阅服务：" + ("配置为开启" if report["subscription_service"].get("enabled") else "未开启"))
        for node in nodes:
            detail = " / ".join(value for value in (node["protocol"].upper(), node["transport"], node["security"]) if value)
            lines.extend(("", f"#{node['id']}  {node['name']}",
                          f"  {detail} · 端口 {node['port']} · {node['state']} · 客户端 {len(node['clients'])} 个"))
            if not node["clients"]:
                lines.append("  该入站没有可列出的客户端订阅。")
            for client in node["clients"]:
                suffix = "订阅 " + client["subscription_ref"] if client["subscription_ref"] else ""
                if client["subscription_reason"]:
                    suffix += (" · " if suffix else "") + client["subscription_reason"]
                lines.append(f"  {client['name']}：{client['state']} · {suffix}")
        if report["subscriptions"]:
            lines.extend(("", "订阅地址（沿用面板现有订阅，不修改客户端或分组）"))
            for group in report["subscriptions"]:
                lines.append(f"  {group['ref']} · 关联节点 " + "、".join("#" + str(i) for i in group["nodes"]))
                if group["url"]:
                    lines.append("    通用订阅：" + group["url"])
                if group["clash_url"]:
                    lines.append("    Clash/Mihomo：" + group["clash_url"])
                if group["reason"]:
                    lines.append("    " + group["reason"])
            lines.append("  地址按面板配置生成，外网可达性未检测；停用或到期的客户端可能没有可用节点。")
    for warning in report["warnings"]:
        lines.append("提示：" + warning)
    lines.extend(("", render_task(report["task"])))
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True)
    parser.add_argument("--info-dir", required=True)
    parser.add_argument("--host", default="")
    parser.add_argument("--status-file")
    parser.add_argument("--state-only", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    status = {"status": "no_previous_job"}
    if args.status_file:
        try:
            status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            status = {"status": "error"}
    if args.state_only:
        task = sanitize_task(status)
        print(json.dumps(task, ensure_ascii=False, indent=2) if args.json else render_task(task))
        return 0
    report = build_report(args.database, args.info_dir, args.host, status)
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else render_report(report))
    return 0 if report["database_status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
