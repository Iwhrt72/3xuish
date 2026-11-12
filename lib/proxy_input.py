"""Parse private proxy connection data without guessing its wire protocol.

URI credentials are percent-decoded once. Credentials in the three plain text
layouts and separate fields are literal. IPv6 endpoints in a line use brackets.
"""
from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import re
import sys
import unicodedata
from urllib.parse import unquote_to_bytes


MAX_INPUT = 65536
SCHEMES = {"socks5": "socks5", "socks5h": "socks5", "http": "http", "https": "https"}
LAYOUTS = ("auto", "colon", "url", "userinfo", "endpoint")


class InputError(ValueError):
    """A fixed, credential-free explanation suitable for terminal output."""


def _no_controls(value):
    if not isinstance(value, str) or any(unicodedata.category(c) == "Cc" for c in value):
        raise InputError("代理资料不能包含换行、控制字符或非文本字段。")
    try:
        if len(value.encode("utf-8")) > MAX_INPUT:
            raise InputError("代理资料超过长度限制。")
    except UnicodeError:
        raise InputError("代理资料必须是有效的 UTF-8 文本。") from None
    return value


def _scheme(value):
    if value is None or value == "auto":
        return None
    if not isinstance(value, str) or value.lower() not in SCHEMES:
        raise InputError("仅支持 SOCKS5、HTTP CONNECT 和 HTTPS CONNECT。")
    return SCHEMES[value.lower()]


def normalize_host(host):
    """Canonicalize IP/IDNA hostnames, allowing bare IPv6 in separate fields."""
    _no_controls(host)
    if host.startswith("[") and host.endswith("]"):
        if ":" not in host:
            raise InputError("方括号只用于 IPv6 地址。")
        host = host[1:-1]
    if not host or any(c.isspace() for c in host) or any(c in host for c in "[]/@%?#\\"):
        raise InputError("代理地址必须是有效 IP 或域名；IPv6 不支持作用域标识。")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if ":" in host or re.fullmatch(r"[0-9.]+", host):
            raise InputError("代理 IP 地址无效。") from None
        try:
            host = host.encode("idna").decode("ascii").lower()
            if host.endswith("."):
                host = host[:-1]
        except UnicodeError:
            raise InputError("代理域名无效。") from None
        labels = host.split(".")
        if (not host or len(host) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in labels)):
            raise InputError("代理域名无效。")
        return host
    if address.is_unspecified or address.is_multicast:
        raise InputError("代理地址不能是未指定地址或组播地址。")
    return str(address)


def _port(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InputError("代理端口必须是 1–65535 的整数。")
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,5}", value) or not 1 <= int(value) <= 65535:
        raise InputError("代理端口必须是 1–65535 的整数。")
    return int(value)


def _config(scheme, host, port, username="", password=""):
    scheme = _scheme(scheme)
    if scheme is None:
        raise InputError("这份资料没有协议前缀，请先选择 SOCKS5、HTTP 或 HTTPS。")
    host, port = normalize_host(host), _port(port)
    username, password = _no_controls(username), _no_controls(password)
    if bool(username) != bool(password):
        raise InputError("账号和密码必须同时填写，或同时留空使用免认证代理。")
    if scheme == "socks5" and any(len(x.encode("utf-8")) > 255 for x in (username, password)):
        raise InputError("SOCKS5 账号和密码各自不能超过 255 个 UTF-8 字节。")
    if scheme in ("http", "https") and ":" in username:
        raise InputError("HTTP Basic 认证的账号不能包含冒号；密码可以包含冒号。")
    return {"scheme": scheme, "host": host, "port": port,
            "username": username, "password": password}


def _endpoint(text):
    if text.startswith("["):
        match = re.fullmatch(r"\[([^\[\]]+)\]:([0-9]+)", text)
        if not match or ":" not in match.group(1):
            raise InputError("IPv6 入口必须使用 [IPv6]:端口。")
        host, port = match.groups()
    else:
        if text.count(":") != 1:
            raise InputError("入口必须使用 地址:端口，IPv6 地址需要方括号。")
        host, port = text.split(":")
    return normalize_host(host), _port(port)


def _auth(text):
    if ":" not in text:
        raise InputError("认证资料必须同时包含账号和密码。")
    return text.split(":", 1)


def _decode(value):
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise InputError("URI 的百分号转义无效；特殊字符请使用百分号编码。")
    try:
        return unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeError:
        raise InputError("URI 认证资料必须编码为有效 UTF-8。") from None


def _uri(text, requested):
    if any(c.isspace() for c in text):
        raise InputError("URI 中的空白字符必须使用百分号编码。")
    scheme, authority = text.split("://", 1)
    scheme = _scheme(scheme)
    if scheme is None:
        raise InputError("代理 URI 必须包含支持的协议。")
    if requested is not None and scheme != requested:
        raise InputError("URI 协议与所选协议不一致，请修改选择后重试。")
    if authority.endswith("/"):
        authority = authority[:-1]
    if any(c in authority for c in "/?#") or authority.count("@") > 1:
        raise InputError("代理 URI 不能包含路径、查询或片段；账号密码中的特殊字符请编码。")
    username = password = ""
    if "@" in authority:
        credentials, authority = authority.split("@")
        username, password = (_decode(value) for value in _auth(credentials))
    host, port = _endpoint(authority)
    return _config(scheme, host, port, username, password)


def _plain(text, scheme, layout):
    if layout == "colon":
        match = re.fullmatch(r"(\[[^\[\]]+\]|[^:]+):([0-9]+):(.*)", text)
        if not match:
            raise InputError("该格式需要 地址:端口:账号:密码。")
        host, port, credentials = match.groups()
        if host.startswith("[") and ":" not in host:
            raise InputError("方括号只用于 IPv6 地址。")
        username, password = _auth(credentials)
    elif layout == "userinfo":
        if "@" not in text:
            raise InputError("该格式需要 账号:密码@地址:端口。")
        credentials, endpoint = text.rsplit("@", 1)
        host, port = _endpoint(endpoint)
        username, password = _auth(credentials)
    else:
        if "@" in text:
            endpoint, credentials = text.split("@", 1)
            host, port = _endpoint(endpoint)
            username, password = _auth(credentials)
        else:
            host, port = _endpoint(text)
            username = password = ""
    return _config(scheme, host, port, username, password)


def parse_proxy(text, protocol=None, layout="auto"):
    """Return a canonical configuration, rejecting ambiguous plain layouts."""
    _no_controls(text)
    if not text or text != text.strip():
        raise InputError("整行资料不能为空或带有首尾空白；含空白的凭据请使用分项输入。")
    requested = _scheme(protocol)
    if layout not in LAYOUTS:
        raise InputError("不支持该代理输入格式。")
    if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", text):
        if layout not in ("auto", "url"):
            raise InputError("包含协议前缀的资料请选择 URI 格式。")
        return _uri(text, requested)
    if layout == "url":
        raise InputError("URI 格式需要 socks5://、http:// 或 https:// 前缀。")
    if requested is None:
        raise InputError("这份资料没有协议前缀，请先选择 SOCKS5、HTTP 或 HTTPS。")
    if layout != "auto":
        return _plain(text, requested, layout)
    candidates = {}
    for candidate_layout in ("colon", "userinfo", "endpoint"):
        try:
            value = _plain(text, requested, candidate_layout)
        except InputError:
            continue
        candidates[tuple(value.items())] = value
    if not candidates:
        raise InputError("无法识别代理资料，请检查格式或改用分项输入。")
    if len(candidates) != 1:
        raise InputError("这行资料存在多种有效解释，请指定平台的输出格式或改用分项输入。")
    return next(iter(candidates.values()))


def parse_fields(value, protocol):
    if not isinstance(value, dict):
        raise InputError("分项输入需要地址、端口、账号和密码字段。")
    return _config(protocol, value.get("host"), value.get("port"),
                   value.get("username", ""), value.get("password", ""))


def build_outbound(config):
    """Build Xray settings and preserve existing SOCKS credential identities."""
    config = _config(config.get("scheme"), config.get("host"), config.get("port"),
                     config.get("username", ""), config.get("password", ""))
    parts = [config["host"], str(config["port"]), config["username"], config["password"]]
    if config["scheme"] != "socks5":
        parts.insert(0, config["scheme"])
    identity = hashlib.sha256(("\0".join(parts) + "\0").encode("utf-8")).hexdigest()[:16]
    server = {"address": config["host"], "port": config["port"]}
    if config["username"]:
        server["users"] = [{"user": config["username"], "pass": config["password"]}]
    result = {"tag": "exitnode-" + identity + "-socks",
              "protocol": "socks" if config["scheme"] == "socks5" else "http",
              "settings": {"servers": [server]}}
    if config["scheme"] == "https":
        result["streamSettings"] = {"network": "tcp", "security": "tls",
                                    "tlsSettings": {"serverName": config["host"]}}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("parse", "fields"):
        command = commands.add_parser(name)
        command.add_argument("--protocol", choices=("socks5", "http", "https", "auto"), default="auto")
        if name == "parse":
            command.add_argument("--layout", choices=LAYOUTS, default="auto")
    args = parser.parse_args(argv)
    try:
        data = sys.stdin.read(MAX_INPUT + 2)
        if len(data) > MAX_INPUT + 1:
            raise InputError("代理资料超过长度限制。")
        # The CLI accepts the single line ending added by printf/echo, not extra
        # lines or whitespace belonging to a password.
        if args.command == "parse":
            if data.endswith("\r\n"):
                data = data[:-2]
            elif data.endswith("\n"):
                data = data[:-1]
            value = parse_proxy(data, args.protocol, args.layout)
        else:
            if len(data.encode("utf-8")) > MAX_INPUT:
                raise InputError("代理资料超过长度限制。")
            value = parse_fields(json.loads(data), args.protocol)
        print(json.dumps(build_outbound(value), ensure_ascii=False))
        return 0
    except InputError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ValueError, TypeError, AttributeError, OSError, UnicodeError):
        print("代理资料无效，请检查格式或改用分项输入。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
