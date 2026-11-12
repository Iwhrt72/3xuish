"""Check SOCKS5 or HTTP CONNECT exits, HTTPS egress, and DNS forwarding."""

from contextlib import contextmanager
import http.client
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import time


TIMEOUT = 5
RESOLVER = '1.1.1.1'
TRACE_HOST = 'www.cloudflare.com'
DNS_QUESTION = b'\x07example\x03com\x00\x00\x01\x00\x01'
SOCKS_REPLY = {
    1: 'general failure', 2: 'blocked by rules', 3: 'network unreachable',
    4: 'host unreachable', 5: 'connection refused', 6: 'TTL expired',
    7: 'command unsupported', 8: 'address type unsupported',
}


class ProbeError(ValueError):
    """A failure description safe to show without authentication data."""


def _read(sock, size):
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ProbeError('peer closed the connection')
        data.extend(chunk)
    return bytes(data)


def _address(host, port):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        name = host.encode('idna')
        if not 1 <= len(name) <= 255:
            raise ProbeError('SOCKS5 domain length is invalid')
        body = bytes((3, len(name))) + name
    else:
        body = bytes((1 if ip.version == 4 else 4,)) + ip.packed
    return body + struct.pack('!H', port)


def _read_address(read, kind):
    if kind in (1, 4):
        host = str(ipaddress.ip_address(read(4 if kind == 1 else 16)))
    elif kind == 3:
        size = read(1)[0]
        if not size:
            raise ProbeError('SOCKS5 relay returned an empty domain')
        host = read(size).decode('ascii')
    else:
        raise ProbeError('SOCKS5 response has an unknown address type')
    return host, struct.unpack('!H', read(2))[0]


def _connect(host, port, username, password):
    auth = bool(username or password)
    if auth and not (1 <= len(username) <= 255 and 1 <= len(password) <= 255):
        raise ProbeError('SOCKS5 username and password must each be 1–255 UTF-8 bytes')
    sock = socket.create_connection((host, port), timeout=TIMEOUT)
    try:
        method = 2 if auth else 0
        sock.sendall(bytes((5, 1, method)))
        if _read(sock, 2) != bytes((5, method)):
            raise ProbeError('SOCKS5 proxy did not accept the expected authentication method')
        if auth:
            sock.sendall(bytes((1, len(username))) + username + bytes((len(password),)) + password)
            if _read(sock, 2) != b'\x01\x00':
                raise ProbeError('SOCKS5 proxy rejected the supplied credentials')
        return sock
    except BaseException:
        sock.close()
        raise


def _command(sock, command, host, port):
    sock.sendall(bytes((5, command, 0)) + _address(host, port))
    version, status, reserved, kind = _read(sock, 4)
    if version != 5 or reserved:
        raise ProbeError('invalid SOCKS5 response')
    if status:
        detail = SOCKS_REPLY.get(status)
        suffix = f' ({detail})' if detail else ''
        raise ProbeError(f'SOCKS5 returned status {status}{suffix}')
    return _read_address(lambda size: _read(sock, size), kind)


def _https_exit(host, port, username, password):
    with _connect(host, port, username, password) as sock:
        _command(sock, 1, TRACE_HOST, 443)
        context = ssl.create_default_context()
        with context.wrap_socket(sock, server_hostname=TRACE_HOST) as tls:
            request = f'GET /cdn-cgi/trace HTTP/1.1\r\nHost: {TRACE_HOST}\r\nConnection: close\r\n\r\n'
            tls.sendall(request.encode('ascii'))
            with http.client.HTTPResponse(tls) as response:
                response.begin()
                if response.status != 200:
                    raise ProbeError(f'HTTPS trace returned status {response.status}')
                body = response.read(16385)
    if len(body) > 16384:
        raise ProbeError('HTTPS trace response exceeded its size limit')
    for line in body.splitlines():
        if line.startswith(b'ip='):
            try:
                return str(ipaddress.ip_address(line[3:].decode('ascii')))
            except (UnicodeError, ValueError):
                raise ProbeError('HTTPS trace returned an invalid exit IP') from None
    raise ProbeError('HTTPS trace did not report an exit IP')


def _dns_question():
    ident = secrets.randbits(16)
    return ident, struct.pack('!6H', ident, 0x0100, 1, 0, 0, 0) + DNS_QUESTION


def _dns_name(packet, offset, limit=None):
    """Read a bounded DNS name, including backward compression pointers."""
    end = len(packet) if limit is None else limit
    labels = []
    next_offset = None
    seen = set()
    wire_length = 1
    for _ in range(128):
        if offset >= end or offset in seen:
            raise ProbeError('DNS name was truncated or contains a compression loop')
        seen.add(offset)
        size = packet[offset]
        if size & 0xC0 == 0xC0:
            if offset + 2 > end:
                raise ProbeError('DNS compression pointer was truncated')
            pointer = ((size & 0x3F) << 8) | packet[offset + 1]
            # A pointer references a prior occurrence of a name (RFC 1035).
            if pointer < 12 or pointer >= offset:
                raise ProbeError('DNS compression pointer is invalid')
            if next_offset is None:
                next_offset = offset + 2
            offset, end = pointer, len(packet)
            continue
        if size & 0xC0:
            raise ProbeError('DNS label type is invalid')
        offset += 1
        if not size:
            return tuple(labels), next_offset if next_offset is not None else offset
        if offset + size > end:
            raise ProbeError('DNS label was truncated')
        wire_length += size + 1
        if wire_length > 255:
            raise ProbeError('DNS name exceeded its size limit')
        labels.append(packet[offset:offset + size].lower())
        offset += size
    raise ProbeError('DNS name exceeded its compression limit')


def _check_dns(response, ident):
    if len(response) < 12:
        raise ProbeError('DNS response was truncated')
    response_id, flags, questions, answers, authority, additional = struct.unpack_from('!6H', response)
    if response_id != ident or questions != 1 or not (flags & 0x8000) or flags & 0x7800:
        raise ProbeError('DNS response did not match the query')
    if flags & 0x0200:
        raise ProbeError('DNS response was truncated')
    if flags & 0x000F:
        raise ProbeError(f'DNS resolver returned RCODE {flags & 0x000F}')
    if not answers:
        raise ProbeError('DNS resolver returned no answers')
    question, offset = _dns_name(response, 12)
    if offset + 4 > len(response):
        raise ProbeError('DNS question was truncated')
    if question != (b'example', b'com') or response[offset:offset + 4] != b'\x00\x01\x00\x01':
        raise ProbeError('DNS response did not match the query')
    offset += 4
    addresses = set()
    aliases = {}
    for index in range(answers + authority + additional):
        owner, offset = _dns_name(response, offset)
        if offset + 10 > len(response):
            raise ProbeError('DNS resource record was truncated')
        kind, record_class, _, size = struct.unpack_from('!HHIH', response, offset)
        offset += 10
        end = offset + size
        if end > len(response):
            raise ProbeError('DNS resource data was truncated')
        if kind == 1:
            if size != 4:
                raise ProbeError('DNS A record has an invalid size')
            if index < answers and record_class == 1:
                addresses.add(owner)
        elif kind == 5:
            alias, consumed = _dns_name(response, offset, end)
            if consumed != end:
                raise ProbeError('DNS CNAME record has an invalid size')
            if index < answers and record_class == 1:
                if owner in aliases and aliases[owner] != alias:
                    raise ProbeError('DNS response contains conflicting aliases')
                aliases[owner] = alias
        offset = end
    if offset != len(response):
        raise ProbeError('DNS response has unexpected trailing data')
    visited = set()
    target = question
    while target not in addresses:
        if target in visited or target not in aliases:
            raise ProbeError('DNS resolver returned no A record for the requested name')
        visited.add(target)
        target = aliases[target]


def _tcp_dns(host, port, username, password):
    ident, query = _dns_question()
    with _connect(host, port, username, password) as sock:
        _command(sock, 1, RESOLVER, 53)
        sock.sendall(struct.pack('!H', len(query)) + query)
        size = struct.unpack('!H', _read(sock, 2))[0]
        _check_dns(_read(sock, size), ident)


def _udp_dns(host, port, username, password):
    ident, query = _dns_question()
    with _connect(host, port, username, password) as control:
        relay_host, relay_port = _command(control, 3, '0.0.0.0', 0)
        if relay_host in ('0.0.0.0', '::'):
            relay_host = control.getpeername()[0]
        if relay_port == 0:
            raise ProbeError('SOCKS5 returned an invalid UDP relay port')
        endpoints = socket.getaddrinfo(relay_host, relay_port, type=socket.SOCK_DGRAM)
        family, kind, protocol, _, address = endpoints[0]
        with socket.socket(family, kind, protocol) as udp:
            udp.settimeout(TIMEOUT)
            udp.connect(address)
            udp.send(b'\x00\x00\x00' + _address(RESOLVER, 53) + query)
            response = udp.recv(65535)
    if len(response) < 4 or response[:3] != b'\x00\x00\x00':
        raise ProbeError('invalid SOCKS5 UDP reply header')
    offset = 4

    def read_header(size):
        nonlocal offset
        if offset + size > len(response):
            raise ProbeError('SOCKS5 UDP reply header was truncated')
        part = response[offset:offset + size]
        offset += size
        return part

    _read_address(read_header, response[3])
    _check_dns(response[offset:], ident)


def _describe(error):
    if isinstance(error, ProbeError):
        return str(error)
    if isinstance(error, TimeoutError):
        return 'connection or response timed out'
    if isinstance(error, ssl.SSLCertVerificationError):
        return 'HTTPS certificate verification failed'
    if isinstance(error, ssl.SSLError):
        return 'HTTPS TLS handshake or transfer failed'
    if isinstance(error, OSError):
        return 'network connection failed'
    return 'peer returned an invalid response'


_PROBE_FAILURES = (ProbeError, OSError, UnicodeError, http.client.HTTPException, struct.error)


def _read_config(path):
    try:
        with open(path, encoding='utf-8') as source:
            value = json.load(source)
    except (ValueError, OSError):
        raise ProbeError('could not read the Xray outbound JSON file') from None
    if not isinstance(value, dict):
        raise ProbeError('expected a single Xray outbound object')
    return value


def _strict_expected_ip():
    value = os.environ.get('SOCKS_EXIT_EXPECTED_IP', '')
    try:
        if '%' in value:
            raise ValueError
        return ipaddress.ip_address(value) if value else None
    except ValueError:
        raise ProbeError('SOCKS_EXIT_EXPECTED_IP must be a valid IPv4 or IPv6 address') from None


def _upstream_host(value):
    if not isinstance(value, str) or not value or any(ord(char) <= 32 or ord(char) == 127 for char in value):
        raise ValueError
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        hostname = value.encode('idna').decode('ascii').lower()
        labels = hostname.rstrip('.').split('.')
        if (len(hostname.rstrip('.')) > 253 or not all(
                re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                for label in labels)):
            raise ValueError
        return hostname
    if address.is_unspecified or address.is_multicast or '%' in value:
        raise ValueError
    return str(address)


def _upstream_endpoint(outbound):
    protocol = outbound.get('protocol')
    if protocol not in ('socks', 'http'):
        raise ProbeError('expected a SOCKS5 or HTTP CONNECT outbound')
    try:
        servers = outbound['settings']['servers']
        if not isinstance(servers, list) or len(servers) != 1:
            raise ValueError
        upstream = servers[0]
        host = _upstream_host(upstream['address'])
        port = upstream['port']
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        users = upstream.get('users', [])
        if not isinstance(users, list) or len(users) > 1:
            raise ValueError
        username = password = b''
        if users:
            account = users[0]
            username = account['user'].encode('utf-8')
            password = account['pass'].encode('utf-8')
            if not username or not password or any(byte < 32 or byte == 127 for byte in username + password):
                raise ValueError
            if protocol == 'socks' and (len(username) > 255 or len(password) > 255):
                raise ValueError
            if protocol == 'http' and b':' in username:
                raise ValueError
        stream = outbound.get('streamSettings', {})
        if not isinstance(stream, dict) or stream.get('network', 'tcp') not in ('tcp', 'raw'):
            raise ValueError
        security = stream.get('security', 'none')
        if security not in (('none', 'tls') if protocol == 'http' else ('none',)):
            raise ValueError
    except (KeyError, IndexError, AttributeError, TypeError, ValueError, UnicodeError):
        raise ProbeError('proxy outbound hostname, port, credentials, or transport is invalid') from None
    return host, port, username, password


def _upstream_tcp_checks(host, port, username, password, label):
    try:
        exit_ip = _https_exit(host, port, username, password)
    except _PROBE_FAILURES as exc:
        raise ProbeError(f'HTTPS through the {label} exit failed: {_describe(exc)}') from None
    try:
        _tcp_dns(host, port, username, password)
    except _PROBE_FAILURES as exc:
        requirement = ('; this provider must allow CONNECT to 1.1.1.1:53 (TCP DNS)'
                       if label != 'SOCKS5' else '')
        raise ProbeError(f'DNS over TCP through the {label} exit failed: {_describe(exc)}{requirement}') from None
    return exit_ip


def _probe_upstream(outbound_file, binary=None, workdir=None):
    # Validate policy and input before the caller creates a panel-managed node.
    _strict_expected_ip()
    outbound = _read_config(outbound_file)
    endpoint = _upstream_endpoint(outbound)
    if outbound['protocol'] == 'socks':
        return _upstream_tcp_checks(*endpoint, 'SOCKS5')
    if not binary or not workdir:
        raise ProbeError('HTTP CONNECT preflight requires the Xray binary and working directory')
    label = 'HTTPS CONNECT' if outbound.get('streamSettings', {}).get('security') == 'tls' else 'HTTP CONNECT'
    # Use the exact outbound that will be published. Xray handles HTTP proxy
    # authentication and (for HTTPS proxies) the outer TLS transport itself.
    with _temporary_client(binary, workdir, outbound) as port:
        return _upstream_tcp_checks('127.0.0.1', port, b'', b'', label)


def _local_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def _await_listener(process, port):
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ProbeError('temporary Xray client stopped before listening')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=0.2):
                if process.poll() is None:
                    return
        except OSError:
            time.sleep(0.05)
    raise ProbeError('temporary Xray client did not start listening')


def _stop(process):
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@contextmanager
def _temporary_client(binary, workdir, outbound):
    port = _local_port()
    config = {
        'log': {'loglevel': 'none'},
        'inbounds': [{
            'listen': '127.0.0.1',
            'port': port,
            'protocol': 'socks',
            'settings': {'auth': 'noauth', 'udp': True, 'ip': '127.0.0.1'},
        }],
        'outbounds': [outbound],
    }
    with tempfile.TemporaryDirectory(prefix='3xuish-socks-probe-') as directory:
        candidate = Path(directory) / 'client.json'
        fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as destination:
            json.dump(config, destination)

        command = [str(Path(binary).resolve()), 'run', '-c', str(candidate)]
        with subprocess.Popen(command, cwd=workdir, stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
            try:
                _await_listener(process, port)
                yield port
            finally:
                _stop(process)


def _probe_node(binary, workdir, outbound_file, expected_exit):
    outbound = _read_config(outbound_file)
    if outbound.get('protocol') != 'vless':
        raise ProbeError('expected a VLESS test client outbound')
    try:
        # Kept as a positional argument for existing callers. Separate proxy
        # connections may legitimately use different exits in a rotating pool.
        ipaddress.ip_address(expected_exit)
    except ValueError:
        raise ProbeError('previously observed exit IP is invalid') from None
    expected = _strict_expected_ip()
    with _temporary_client(binary, workdir, outbound) as port:
        try:
            _udp_dns('127.0.0.1', port, b'', b'')
        except _PROBE_FAILURES as exc:
            raise ProbeError(f'UDP DNS through the new node failed: {_describe(exc)}') from None
        try:
            actual = ipaddress.ip_address(_https_exit('127.0.0.1', port, b'', b''))
        except _PROBE_FAILURES as exc:
            raise ProbeError(f'HTTPS through the new node failed: {_describe(exc)}') from None
        if expected is not None and actual != expected:
            raise ProbeError(f'new node exited via {actual}; expected {expected}')
        return str(actual)


def main(args):
    try:
        if len(args) in (2, 4) and args[0] == 'upstream':
            print(_probe_upstream(*args[1:]))
        elif len(args) == 5 and args[0] == 'node':
            print(_probe_node(*args[1:]))
        else:
            print('usage: socks_node_probe.py upstream OUTBOUND [XRAY WORKDIR] | node XRAY WORKDIR CLIENT_OUTBOUND OBSERVED_EXIT',
                  file=sys.stderr)
            return 2
    except ProbeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError):
        print('could not open the local probe files or run Xray', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('probe interrupted', file=sys.stderr)
        return 130
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
