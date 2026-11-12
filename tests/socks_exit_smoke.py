"""Run ONLY in a fresh, disposable official 3x-ui container with /repo mounted.

Exercises the production creation worker, API, core and probe entirely through
loopback fixtures. No public proxy, DNS resolver or HTTPS endpoint is contacted.
Requires bash, jq, sqlite, openssl, iproute2, curl and python3 in the container.
Set SMOKE_PROXY_SCHEME to socks5 (default), http or https; each case requires
its own fresh container. HTTP cases also reject CONNECT53 before any mutation.
"""
from contextlib import ExitStack
import base64
import json
import copy
import os
from pathlib import Path
import socket
import socketserver
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'lib'))
import socks_node_probe as probe

OLD_UUID = '72e3cf00-8a19-4819-9489-83ed7ad7b6f0'
OLD_PORT = 24443


def checked(args, **kwargs):
    result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=30, **kwargs)
    if result.returncode:
        raise RuntimeError('fixture command failed: ' + args[0])
    return result


def certificates(directory):
    ca_key, ca = directory / 'ca.key', directory / 'ca.pem'
    key, csr, cert = directory / 'leaf.key', directory / 'leaf.csr', directory / 'leaf.pem'
    extensions = directory / 'extensions.cnf'
    extensions.write_text('basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
                          'extendedKeyUsage=serverAuth\n'
                          'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n'
                          'subjectAltName=DNS:example.test,DNS:www.cloudflare.com,IP:127.0.0.1\n')
    checked(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1',
             '-subj', '/CN=SOCKS smoke CA', '-keyout', str(ca_key), '-out', str(ca),
             '-addext', 'basicConstraints=critical,CA:TRUE',
             '-addext', 'keyUsage=critical,keyCertSign,cRLSign'])
    checked(['openssl', 'req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=example.test',
             '-keyout', str(key), '-out', str(csr)])
    checked(['openssl', 'x509', '-req', '-in', str(csr), '-CA', str(ca), '-CAkey', str(ca_key),
             '-CAcreateserial', '-days', '1', '-extfile', str(extensions), '-out', str(cert)])
    return ca, cert, key


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __enter__(self):
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        with self.server.lock:
            self.server.connections += 1
        self.request.settimeout(30)
        try:
            while data := self.request.recv(4096):
                self.request.sendall(data)
        except OSError:
            pass


def echo_server():
    server = Server(('127.0.0.1', 0), Echo)
    server.lock = threading.Lock()
    server.connections = 0
    return server


class Socks(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(10)
        read = lambda size: probe._read(sock, size)
        try:
            version, methods = read(2)
            offered = read(methods)
            if version != 5 or 2 not in offered:
                sock.sendall(b'\x05\xff')
                return
            sock.sendall(b'\x05\x02')
            auth_version, size = read(2)
            user = read(size)
            password = read(read(1)[0])
            if (auth_version, user, password) != (1, b'smoke-user', b'smoke-password'):
                sock.sendall(b'\x01\x01')
                return
            sock.sendall(b'\x01\x00')
            version, command, reserved, kind = read(4)
            host, port = probe._read_address(read, kind)
            if version != 5 or reserved or command != 1:
                sock.sendall(b'\x05\x07\x00\x01' + b'\0' * 6)
                return
            with self.server.lock:
                self.server.connects.append((host, port))
            sock.sendall(b'\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00')
            if host == '1.1.1.1' and port == 53:
                while True:
                    query = read(struct.unpack('!H', read(2))[0])
                    if len(query) < 12 or query[12:] != probe.DNS_QUESTION:
                        raise ValueError('unexpected DNS question')
                    answer = (query[:2] + struct.pack('!5H', 0x8180, 1, 1, 0, 0) + query[12:]
                              + b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + b'\xc0\x00\x02\x01')
                    sock.sendall(struct.pack('!H', len(answer)) + answer)
            elif host == 'www.cloudflare.com' and port == 443:
                with self.server.context.wrap_socket(sock, server_side=True) as tls:
                    request = bytearray()
                    while b'\r\n\r\n' not in request and len(request) < 16384:
                        data = tls.recv(4096)
                        if not data:
                            return
                        request.extend(data)
                    if not request.startswith(b'GET /cdn-cgi/trace '):
                        raise ValueError('unexpected HTTPS request')
                    with self.server.lock:
                        self.server.traces += 1
                        number = self.server.traces
                    body = f'ip=203.0.113.{9 + number}\n'.encode('ascii')
                    tls.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: '
                                + str(len(body)).encode('ascii') + b'\r\n\r\n' + body)
        except (OSError, ValueError, probe.ProbeError):
            # Closing an idle DNS connection is normal; observations are checked
            # by the caller, so failures cannot silently turn into test success.
            pass


class DnsTarget(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            while True:
                query = probe._read(self.request, struct.unpack('!H', probe._read(self.request, 2))[0])
                if len(query) < 12 or query[12:] != probe.DNS_QUESTION:
                    raise ValueError('unexpected DNS question')
                answer = (query[:2] + struct.pack('!5H', 0x8180, 1, 1, 0, 0) + query[12:]
                          + b'\xc0\x0c' + struct.pack('!HHIH', 1, 1, 60, 4) + b'\xc0\x00\x02\x01')
                self.request.sendall(struct.pack('!H', len(answer)) + answer)
        except (OSError, ValueError, probe.ProbeError):
            pass


class HttpsTarget(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(10)
        try:
            with self.server.context.wrap_socket(self.request, server_side=True) as tls:
                request = bytearray()
                while b'\r\n\r\n' not in request and len(request) < 16384:
                    data = tls.recv(4096)
                    if not data:
                        return
                    request.extend(data)
                if not request.startswith(b'GET /cdn-cgi/trace '):
                    raise ValueError('unexpected HTTPS request')
                with self.server.proxy.lock:
                    self.server.proxy.traces += 1
                    number = self.server.proxy.traces
                body = f'ip=203.0.113.{9 + number}\n'.encode('ascii')
                tls.sendall(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: '
                            + str(len(body)).encode('ascii') + b'\r\n\r\n' + body)
        except (OSError, ValueError):
            pass


def relay(client, target):
    """Forward a CONNECT stream, including TLS inside an HTTPS proxy's TLS."""
    def copy_to_client():
        try:
            while data := target.recv(16384):
                client.sendall(data)
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    reader = threading.Thread(target=copy_to_client, daemon=True)
    reader.start()
    try:
        while data := client.recv(16384):
            target.sendall(data)
    except OSError:
        pass
    finally:
        try:
            target.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        reader.join(timeout=2)


class HttpConnect(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        sock.settimeout(10)
        try:
            if self.server.outer_tls:
                sock = self.server.context.wrap_socket(sock, server_side=True)
            with sock:
                request = bytearray()
                # Read exactly the header; application TLS follows CONNECT.
                while b'\r\n\r\n' not in request and len(request) < 16384:
                    request.extend(probe._read(sock, 1))
                lines = bytes(request).split(b'\r\n')
                method, authority, version = lines[0].split(b' ')
                headers = dict(line.split(b':', 1) for line in lines[1:] if b':' in line)
                headers = {name.lower(): value.strip() for name, value in headers.items()}
                expected = b'Basic ' + base64.b64encode(b'smoke-user:smoke-password')
                if headers.get(b'proxy-authorization') != expected:
                    sock.sendall(b'HTTP/1.1 407 Proxy Authentication Required\r\nContent-Length: 0\r\n\r\n')
                    return
                if method != b'CONNECT' or not version.startswith(b'HTTP/1.'):
                    raise ValueError('unexpected proxy request')
                host, port_text = authority.decode('ascii').rsplit(':', 1)
                port = int(port_text)
                with self.server.lock:
                    self.server.connects.append((host, port))
                if host == '1.1.1.1' and port == 53:
                    if self.server.deny_dns:
                        sock.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                        return
                    destination = self.server.dns.server_address
                elif host == 'www.cloudflare.com' and port == 443:
                    destination = self.server.https.server_address
                else:
                    sock.sendall(b'HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n')
                    return
                with socket.create_connection(destination, timeout=10) as target:
                    sock.sendall(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                    relay(sock, target)
        except (OSError, ValueError, probe.ProbeError):
            pass


class Client:
    def __init__(self, directory, outbound, environment, label):
        self.directory, self.outbound, self.environment, self.label = directory, outbound, environment, label

    def __enter__(self):
        self.port = probe._local_port()
        outbound = copy.deepcopy(self.outbound)
        outbound['streamSettings']['tlsSettings']['useSystemCA'] = True
        config = {'log': {'loglevel': 'debug'}, 'inbounds': [{'listen': '127.0.0.1', 'port': self.port,
                  'protocol': 'socks', 'settings': {'auth': 'noauth', 'udp': True}}], 'outbounds': [outbound]}
        path = self.directory / (self.label + '.json')
        path.write_text(json.dumps(config))
        self.log = (self.directory / (self.label + '.log')).open('w')
        self.process = subprocess.Popen(['/app/bin/xray-linux-amd64', 'run', '-c', str(path)],
                                        cwd='/app', env=self.environment, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=subprocess.STDOUT)
        try:
            probe._await_listener(self.process, self.port)
        except BaseException:
            probe._stop(self.process)
            self.log.close()
            raise
        return self

    def __exit__(self, *_):
        probe._stop(self.process)
        self.log.close()


SHELL = r'''
set -euo pipefail
source /repo/install.sh
source /repo/lib/socks_exit.sh
XUI_INSTALL_DIR=/app
XUI_DB_PATH=/etc/x-ui/x-ui.db
XUI_INFO_DIR="$SMOKE_ROOT/info"
RUN_DIR="$SMOKE_ROOT/setup"
mkdir -p "$RUN_DIR" "$XUI_INFO_DIR" /etc/x-ui
PANEL_PORT=20530
WEB_BASE_PATH=socks-smoke
PANEL_USER=smoke-user
PANEL_PASS=smoke-password
PANEL_API_SCHEME=http
CERT_HOST=example.test
CERT_FILE="$SMOKE_ROOT/leaf.pem"
KEY_FILE="$SMOKE_ROOT/leaf.key"
ARCH=$(get_arch)
/app/x-ui setting -username "$PANEL_USER" -password "$PANEL_PASS" -port "$PANEL_PORT" -webBasePath "$WEB_BASE_PATH" >"$SMOKE_ROOT/settings.log" 2>&1
(cd /app && exec ./x-ui run) >"$SMOKE_ROOT/panel.log" 2>&1 &
panel_pid=$!
trap 'kill "$panel_pid" 2>/dev/null || true' EXIT
for _ in {1..30}; do
    if curl -fs http://127.0.0.1:20530/socks-smoke/ -o /dev/null; then break; fi
    kill -0 "$panel_pid"
    sleep 1
done
api_init
response=$(api_request POST /panel/api/inbounds/add "$(cat "$SMOKE_ROOT/old-inbound.json")")
old_id=$(jq -er 'select(.success == true) | .obj.id' <<<"$response")
jq -S '.obj | {id,tag,port,enable,listen,protocol,settings,streamSettings,sniffing}' <<<"$response" >"$SMOKE_ROOT/old-before.json"
proxy_exit_get_template >"$SMOKE_ROOT/template-before.json"
jq --arg port "$SMOKE_BLOCK_PORT" --arg echo "$SMOKE_ECHO_PORT" '.xraySetting |
  .log.loglevel="debug" |
  .outbounds |= map(if .tag == "direct" then
    .settings.finalRules=[{action:"allow",ip:["127.0.0.1"],port:$echo},{action:"block"}]
    else . end) |
  .routing.domainStrategy="AsIs" |
  .routing.rules=([.routing.rules[] | select(.inboundTag == ["api"])] + [
    {type:"field", port:$port, outboundTag:"blocked"},
    {type:"field", network:"udp",port:"53",outboundTag:"direct"},
    {type:"field", domain:["full:www.cloudflare.com"],outboundTag:"direct"},
    {type:"field", network:"tcp",outboundTag:"direct"}])' "$SMOKE_ROOT/template-before.json" >"$SMOKE_ROOT/base-template.json"
jq -jr '.outboundTestUrl // ""' "$SMOKE_ROOT/template-before.json" >"$SMOKE_ROOT/test-url"
proxy_exit_write_template "$SMOKE_ROOT/base-template.json" "$SMOKE_ROOT/test-url"
# Establish the existing deployment and a startup file before testing the new
# node transaction. No further explicit core restart is permitted below.
api_request POST /panel/api/server/restartXrayService '{}' | jq -e '.success' >/dev/null
panel_core_pid() {
    ss -H -lntp 'sport = :24443' | sed -nE 's/.*pid=([0-9]+).*/\1/p'
}
for _ in {1..15}; do
    pid_before=$(panel_core_pid)
    if [[ -n "$pid_before" && -n "$(ss -H -lnt 'sport = :24443')" ]]; then break; fi
    sleep 1
done
[[ -n "$pid_before" ]]
printf '%s\n' "$pid_before" >"$SMOKE_ROOT/ready"
read -r proceed
[[ "$proceed" == run ]]
SOCKS_EXIT_PROBE="$SMOKE_ROOT/trusted-probe.py"
SOCKS_EXIT_JOB_HELPER=/repo/lib/socks_exit_job.py
SOCKS_EXIT_JOB_DIR="$SMOKE_ROOT/job"
RUN_DIR="$SMOKE_ROOT/worker"
mkdir "$SOCKS_EXIT_JOB_DIR" "$RUN_DIR"
printf '{"status":"running"}\n' >"$SOCKS_EXIT_JOB_DIR/state.json"
export XUI_SERVER_IP=127.0.0.1 XUI_DOMAIN=example.test
export XUI_USERNAME="$PANEL_USER" XUI_PASSWORD="$PANEL_PASS"
create_socks_exit_node_worker "$SMOKE_ROOT/upstream.json"
[[ "$pid_before" == "$(panel_core_pid)" ]]
# The worker's api_init may rotate the installer's named token; refresh this
# fixture parent's authentication before its independent preservation checks.
api_init
api_request GET /panel/api/inbounds/list | jq -S --argjson id "$old_id" '.obj[] | select(.id==$id) |
  {id,tag,port,enable,listen,protocol,settings,streamSettings,sniffing}' >"$SMOKE_ROOT/old-after.json"
cmp "$SMOKE_ROOT/old-before.json" "$SMOKE_ROOT/old-after.json"
proxy_exit_get_template >"$SMOKE_ROOT/template-after.json"
jq -e --slurpfile before "$SMOKE_ROOT/base-template.json" '
  (.xraySetting.routing.rules | map(select(.inboundTag != ["exitnode-smoke"])))
    == $before[0].routing.rules
' "$SMOKE_ROOT/template-after.json" >/dev/null
printf '%s\n' "$pid_before" >"$SMOKE_ROOT/created"
if [[ "$SMOKE_PROXY_SCHEME" != socks5 ]]; then
    read -r proceed
    [[ "$proceed" == reject ]]
    proxy_exit_get_template | jq -S '.xraySetting | del(.inbounds)' >"$SMOKE_ROOT/before-rejected-template.json"
    api_request GET /panel/api/inbounds/list | jq -S '[.obj[] | {id,tag,port,enable,settings,streamSettings}]' >"$SMOKE_ROOT/before-rejected-inbounds.json"
    SOCKS_EXIT_JOB_DIR="$SMOKE_ROOT/job-rejected"
    RUN_DIR="$SMOKE_ROOT/worker-rejected"
    mkdir "$SOCKS_EXIT_JOB_DIR" "$RUN_DIR"
    printf '{"status":"running"}\n' >"$SOCKS_EXIT_JOB_DIR/state.json"
    jq '.tag="exitnode-denied-socks"' "$SMOKE_ROOT/upstream.json" >"$SMOKE_ROOT/rejected-upstream.json"
    export EXIT_NODE_PORT=26601
    set +e
    create_socks_exit_node_worker "$SMOKE_ROOT/rejected-upstream.json"
    rejected_rc=$?
    set -e
    [[ "$rejected_rc" != 0 ]]
    jq -e '.status == "failed"' "$SOCKS_EXIT_JOB_DIR/outcome.json" >/dev/null
    [[ ! -e "$SOCKS_EXIT_JOB_DIR/mutating" ]]
    proxy_exit_get_template | jq -S '.xraySetting | del(.inbounds)' >"$SMOKE_ROOT/after-rejected-template.json"
    api_request GET /panel/api/inbounds/list | jq -S '[.obj[] | {id,tag,port,enable,settings,streamSettings}]' >"$SMOKE_ROOT/after-rejected-inbounds.json"
    cmp "$SMOKE_ROOT/before-rejected-template.json" "$SMOKE_ROOT/after-rejected-template.json"
    cmp "$SMOKE_ROOT/before-rejected-inbounds.json" "$SMOKE_ROOT/after-rejected-inbounds.json"
    [[ "$pid_before" == "$(panel_core_pid)" ]]
    printf 'rejected-before-mutation\n' >"$SMOKE_ROOT/rejected"
fi
read -r proceed
[[ "$proceed" == finish ]]
'''


def wait_marker(marker, process, timeout=70):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if marker.exists():
            return
        if process.poll() is not None:
            raise RuntimeError('panel fixture stopped before ' + marker.name)
        time.sleep(.1)
    raise RuntimeError('timed out waiting for ' + marker.name)


def main():
    scheme = os.environ.get('SMOKE_PROXY_SCHEME', 'socks5')
    if scheme not in ('socks5', 'http', 'https'):
        raise RuntimeError('unsupported smoke scheme')
    if not Path('/.dockerenv').exists() or not Path('/app/x-ui').is_file():
        raise RuntimeError('requires a fresh disposable official 3x-ui container')
    if Path('/etc/x-ui/x-ui.db').exists():
        raise RuntimeError('refusing to use an existing panel database')
    with tempfile.TemporaryDirectory(prefix='socks-panel-smoke-') as name, ExitStack() as stack:
        directory = Path(name)
        os.chmod(directory, 0o700)
        ca, cert, key = certificates(directory)
        environment = os.environ.copy()
        environment.update(SSL_CERT_FILE=str(ca), SMOKE_ROOT=str(directory), SOCKS_EXIT_EXPECTED_IP='',
                           SMOKE_PROXY_SCHEME=scheme)
        # Current Xray uses bundled public roots by default. Opt the temporary
        # smoke clients into the system trust store, which reads SSL_CERT_FILE.
        # All production probe code still runs; certificate checking stays on.
        (directory / 'trusted-probe.py').write_text('''import sys
sys.path.insert(0, '/repo/lib')
import socks_node_probe as probe
read_config = probe._read_config
def fixture_trust(path):
    outbound = read_config(path)
    if outbound.get('protocol') == 'vless':
        outbound['streamSettings']['tlsSettings']['useSystemCA'] = True
    return outbound
probe._read_config = fixture_trust
raise SystemExit(probe.main(sys.argv[1:]))
''')
        echo = stack.enter_context(echo_server())
        blocked = stack.enter_context(echo_server())
        environment['SMOKE_BLOCK_PORT'] = str(blocked.server_address[1])
        environment['SMOKE_ECHO_PORT'] = str(echo.server_address[1])
        socks = Server(('127.0.0.1', 0), Socks if scheme == 'socks5' else HttpConnect)
        socks.lock, socks.connects, socks.traces = threading.Lock(), [], 0
        socks.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        socks.context.load_cert_chain(cert, key)
        if scheme != 'socks5':
            socks.outer_tls = scheme == 'https'
            socks.deny_dns = False
            socks.dns = stack.enter_context(Server(('127.0.0.1', 0), DnsTarget))
            socks.https = Server(('127.0.0.1', 0), HttpsTarget)
            socks.https.context = socks.context
            socks.https.proxy = socks
            stack.enter_context(socks.https)
        stack.enter_context(socks)
        stream = {'network': 'tcp', 'security': 'tls', 'tlsSettings': {'serverName': 'example.test',
                  'certificates': [{'certificateFile': str(cert), 'keyFile': str(key)}]}}
        inbound = {'enable': True, 'port': OLD_PORT, 'protocol': 'vless', 'listen': '',
                   'tag': 'smoke-existing', 'remark': 'smoke-existing',
                   'settings': json.dumps({'clients': [{'id': OLD_UUID, 'email': 'smoke-existing',
                                  'flow': 'xtls-rprx-vision', 'enable': True}], 'decryption': 'none'}),
                   'streamSettings': json.dumps(stream), 'sniffing': json.dumps({'enabled': False})}
        (directory / 'old-inbound.json').write_text(json.dumps(inbound))
        upstream = {'tag': 'exitnode-smoke-socks', 'protocol': 'socks' if scheme == 'socks5' else 'http', 'settings': {'servers': [{
                    'address': '127.0.0.1', 'port': socks.server_address[1],
                    'users': [{'user': 'smoke-user', 'pass': 'smoke-password'}]}]}}
        if scheme == 'https':
            # Trust only the temporary fixture CA, with certificate verification
            # enabled for both the proxy TLS and the tunneled target TLS.
            upstream['streamSettings'] = {'security': 'tls', 'tlsSettings': {
                'serverName': '127.0.0.1', 'useSystemCA': True}}
        (directory / 'upstream.json').write_text(json.dumps(upstream))
        script = directory / 'fixture.sh'
        script.write_text(SHELL)
        log = stack.enter_context((directory / 'fixture.log').open('w+'))
        process = subprocess.Popen(['bash', str(script)], env=environment, stdin=subprocess.PIPE,
                                   stdout=log, stderr=subprocess.STDOUT, text=True)
        try:
            wait_marker(directory / 'ready', process)
            outbound = {'protocol': 'vless', 'settings': {'address': '127.0.0.1', 'port': OLD_PORT,
                        'id': OLD_UUID, 'encryption': 'none', 'flow': 'xtls-rprx-vision'},
                        'streamSettings': {'network': 'tcp', 'security': 'tls',
                                           'tlsSettings': {'serverName': 'example.test', 'fingerprint': 'chrome'}}}
            old = stack.enter_context(Client(directory, outbound, environment, 'old-client'))
            connection = stack.enter_context(probe._connect('127.0.0.1', old.port, b'', b''))
            probe._command(connection, 1, '127.0.0.1', echo.server_address[1])
            connection.sendall(b'before')
            assert probe._read(connection, 6) == b'before', 'old node did not reach direct echo'
            process.stdin.write('run\n')
            process.stdin.flush()
            wait_marker(directory / 'created', process)
            connection.sendall(b'after')
            assert probe._read(connection, 5) == b'after', 'existing TCP session was interrupted'
            outcome = json.loads((directory / 'job/outcome.json').read_text())
            assert outcome['status'] == 'complete', 'worker did not complete'
            assert outcome['exit_ip'] == '203.0.113.11', 'worker did not accept/report rotated exit'
            assert socks.connects.count(('1.1.1.1', 53)) >= 2, 'node DNS did not traverse SOCKS TCP'
            assert socks.connects.count(('www.cloudflare.com', 443)) >= 2, 'node HTTPS did not traverse SOCKS'
            created = json.loads((directory / 'worker/exit-node/client.json').read_text())
            node = stack.enter_context(Client(directory, created, environment, 'new-client'))
            with probe._connect('127.0.0.1', node.port, b'', b'') as attempt:
                probe._command(attempt, 1, '127.0.0.1', blocked.server_address[1])
                attempt.sendall(b'must-be-blocked')
                attempt.settimeout(1)
                try:
                    assert not attempt.recv(64), 'global deny policy was bypassed'
                except (TimeoutError, ConnectionResetError):
                    pass
            assert ('127.0.0.1', blocked.server_address[1]) not in socks.connects, 'denied traffic reached SOCKS'
            assert blocked.connections == 0, 'denied traffic reached direct destination'
            if scheme != 'socks5':
                socks.deny_dns = True
                process.stdin.write('reject\n')
                process.stdin.flush()
                wait_marker(directory / 'rejected', process)
                assert socks.connects.count(('1.1.1.1', 53)) >= 3, 'CONNECT53 rejection was not exercised'
                connection.sendall(b'still-alive')
                assert probe._read(connection, 11) == b'still-alive', 'failed preflight interrupted old TCP session'
            process.stdin.write('finish\n')
            process.stdin.flush()
            assert process.wait(timeout=15) == 0, 'fixture failed final checks'
            print(f'PROXY_EXIT_SMOKE_OK scheme={scheme}: full worker; real TLS/VLESS; TCP DNS through proxy; '
                  'rotating exit; unchanged core PID and old TCP session; old rules and deny policy preserved'
                  + ('; CONNECT53 rejection before mutation' if scheme != 'socks5' else ''))
        except BaseException:
            log.flush()
            log.seek(0)
            # Fixture credentials and addresses are synthetic, and the helper
            # logs no generated client secrets. Keep diagnostics bounded.
            print(log.read()[-8000:], file=sys.stderr)
            for document in ('updated.json', 'applied.json'):
                path = directory / 'worker/exit-node' / document
                if path.exists():
                    value = json.loads(path.read_text())
                    value = value.get('xraySetting', value)
                    print(document + ' rules: ' + json.dumps(value.get('routing', {})), file=sys.stderr)
                    print(document + ' outbound keys: ' + json.dumps([
                        {'tag': item.get('tag'), 'keys': sorted(item)} for item in value.get('outbounds', [])]), file=sys.stderr)
            for diagnostic in ('old-client.log', 'new-client.log', 'panel.log'):
                path = directory / diagnostic
                if path.exists():
                    print(diagnostic + ':\n' + path.read_text()[-5000:], file=sys.stderr)
            raise
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == '__main__':
    main()
