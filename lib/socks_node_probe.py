"""Check SOCKS5 authentication, HTTPS egress, and DNS forwarding."""

import http.client
import ipaddress
import json
import os
from pathlib import Path
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


def _check_dns(response, ident):
    answer_start = 12 + len(DNS_QUESTION)
    if len(response) < answer_start:
        raise ProbeError('DNS response was truncated')
    response_id, flags, questions, answers, _, _ = struct.unpack_from('!6H', response)
    if (response_id != ident or questions != 1 or not (flags & 0x8000)
            or response[12:answer_start].lower() != DNS_QUESTION):
        raise ProbeError('DNS response did not match the query')
    if flags & 0x0200:
        raise ProbeError('DNS response was truncated')
    if flags & 0x000F:
        raise ProbeError(f'DNS resolver returned RCODE {flags & 0x000F}')
    if not answers:
        raise ProbeError('DNS resolver returned no answers')
    if len(response) < answer_start + 11:
        raise ProbeError('DNS answer was truncated')


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


def _probe_upstream(outbound_file):
    outbound = _read_config(outbound_file)
    if outbound.get('protocol') != 'socks':
        raise ProbeError('expected a SOCKS5 outbound')
    try:
        upstream = outbound['settings']['servers'][0]
        host = str(ipaddress.ip_address(upstream['address']))
        port = upstream['port']
        account = upstream['users'][0]
        username = account['user'].encode('utf-8')
        password = account['pass'].encode('utf-8')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError
        if not (1 <= len(username) <= 255 and 1 <= len(password) <= 255):
            raise ValueError
    except (KeyError, IndexError, AttributeError, TypeError, ValueError):
        raise ProbeError('SOCKS5 outbound IP, port, username, or password is invalid') from None

    try:
        exit_ip = _https_exit(host, port, username, password)
    except _PROBE_FAILURES as exc:
        raise ProbeError(f'HTTPS through the SOCKS5 exit failed: {_describe(exc)}') from None
    try:
        _tcp_dns(host, port, username, password)
    except _PROBE_FAILURES as exc:
        raise ProbeError(f'DNS over TCP through the SOCKS5 exit failed: {_describe(exc)}') from None
    return exit_ip


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


def _probe_node(binary, workdir, outbound_file, expected_exit):
    outbound = _read_config(outbound_file)
    if outbound.get('protocol') != 'vless':
        raise ProbeError('expected a VLESS test client outbound')
    try:
        expected = ipaddress.ip_address(expected_exit)
    except ValueError:
        raise ProbeError('expected exit IP is invalid') from None

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
                try:
                    _udp_dns('127.0.0.1', port, b'', b'')
                except _PROBE_FAILURES as exc:
                    raise ProbeError(f'UDP DNS through the new node failed: {_describe(exc)}') from None
                try:
                    actual = ipaddress.ip_address(_https_exit('127.0.0.1', port, b'', b''))
                except _PROBE_FAILURES as exc:
                    raise ProbeError(f'HTTPS through the new node failed: {_describe(exc)}') from None
                if actual != expected:
                    raise ProbeError(f'new node exited via {actual}; expected {expected}')
            finally:
                _stop(process)


def main(args):
    try:
        if len(args) == 2 and args[0] == 'upstream':
            print(_probe_upstream(args[1]))
        elif len(args) == 5 and args[0] == 'node':
            _probe_node(*args[1:])
        else:
            print('usage: socks_node_probe.py upstream OUTBOUND | node XRAY WORKDIR CLIENT_OUTBOUND EXPECTED_EXIT',
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
