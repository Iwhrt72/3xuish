"""Run only in a fresh official 3x-ui container; all endpoints use loopback.

Verify overview URLs against the real subscription server and verify that merely
viewing nodes does not modify accounts, clients, settings or API tokens.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
DATABASE = Path('/etc/x-ui/x-ui.db')
PANEL_PORT = 20530
SUB_PORT = 20960
SHARED = 'overview-shared-subscription'
PROXY = 'overview-proxy-subscription'
IDS = (
    '354fdbb2-8a86-4bfa-bbdd-a1cc3a8a1b01',
    '354fdbb2-8a86-4bfa-bbdd-a1cc3a8a1b02',
    '354fdbb2-8a86-4bfa-bbdd-a1cc3a8a1b03',
    '354fdbb2-8a86-4bfa-bbdd-a1cc3a8a1b04',
)


SHELL = r'''
set -euo pipefail
source /repo/install.sh
XUI_INSTALL_DIR=/app
XUI_DB_PATH=/etc/x-ui/x-ui.db
XUI_INFO_DIR="$SMOKE_ROOT/info"
RUN_DIR="$SMOKE_ROOT/run"
mkdir -p "$RUN_DIR" "$XUI_INFO_DIR" /etc/x-ui
PANEL_PORT=20530
WEB_BASE_PATH=overview-smoke
PANEL_USER=overview-fixture
PANEL_PASS=fixture-panel-password
PANEL_API_SCHEME=http
panel_pid=''
stop_panel() {
    if [[ -n "$panel_pid" ]]; then
        kill "$panel_pid" 2>/dev/null || true
        wait "$panel_pid" 2>/dev/null || true
        panel_pid=''
    fi
}
trap stop_panel EXIT
start_panel() {
    (cd /app && exec ./x-ui run) >>"$SMOKE_ROOT/panel.log" 2>&1 &
    panel_pid=$!
    for _ in {1..30}; do
        if curl -fs http://127.0.0.1:20530/overview-smoke/ -o /dev/null; then return 0; fi
        kill -0 "$panel_pid"
        sleep 1
    done
    return 1
}
/app/x-ui setting -username "$PANEL_USER" -password "$PANEL_PASS" \
    -port "$PANEL_PORT" -webBasePath "$WEB_BASE_PATH" >"$SMOKE_ROOT/settings.log" 2>&1
start_panel
api_init
for file in "$SMOKE_ROOT"/inbound-*.json; do
    api_request POST /panel/api/inbounds/add "$(cat "$file")" | jq -e '.success == true' >/dev/null
done
settings=$(api_request POST /panel/api/setting/all '{}')
updated=$(jq -ce 'select(.success == true) | .obj |
    .subEnable=true | .subListen="127.0.0.1" | .subPort=20960 |
    .subPath="/overview-sub/" | .subURI="" | .subCertFile="" | .subKeyFile="" |
    .subClashEnable=true | .subClashPath="/overview-clash/" | .subClashURI=""' <<<"$settings")
api_request POST /panel/api/setting/update "$updated" | jq -e '.success == true' >/dev/null
stop_panel
start_panel
api_init
# The normalized client records are authoritative. Model old/stale copies in
# inbound JSON without altering the real client identities or subscriptions.
sqlite3 "$XUI_DB_PATH" "UPDATE inbounds SET settings=json_set(settings, '$.clients[0].subId', 'stale-unusable-fixture') WHERE tag='overview-node-0';"
sqlite3 "$XUI_DB_PATH" "UPDATE inbounds SET settings=json_remove(settings, '$.clients') WHERE tag='overview-node-1';"
printf ready >"$SMOKE_ROOT/ready"
read -r action
[[ "$action" == disable ]]
settings=$(api_request POST /panel/api/setting/all '{}')
updated=$(jq -ce 'select(.success == true) | .obj | .subEnable=false' <<<"$settings")
api_request POST /panel/api/setting/update "$updated" | jq -e '.success == true' >/dev/null
stop_panel
start_panel
api_init
printf disabled >"$SMOKE_ROOT/disabled"
read -r action
[[ "$action" == finish ]]
'''


def wait_marker(path, process):
    deadline = time.monotonic() + 50
    while time.monotonic() < deadline:
        if path.exists():
            return
        if process.poll() is not None:
            raise RuntimeError('fixture stopped before ' + path.name)
        time.sleep(.1)
    raise RuntimeError('fixture did not reach ' + path.name)


def snapshot():
    """Ignore background traffic counters, but compare every identity/settings row."""
    with sqlite3.connect(DATABASE.as_uri() + '?mode=ro', uri=True) as database:
        database.execute('PRAGMA query_only=ON')
        return {
            'inbounds': database.execute('SELECT id,user_id,remark,enable,listen,port,protocol,'
                                         'settings,stream_settings,tag,sniffing,exclude_from_sub '
                                         'FROM inbounds ORDER BY id').fetchall(),
            'clients': database.execute('SELECT * FROM clients ORDER BY id').fetchall(),
            'client_inbounds': database.execute('SELECT * FROM client_inbounds ORDER BY client_id,inbound_id').fetchall(),
            'settings': database.execute('SELECT * FROM settings ORDER BY key').fetchall(),
            'users': database.execute('SELECT * FROM users ORDER BY id').fetchall(),
            'api_tokens': database.execute('SELECT * FROM api_tokens ORDER BY id').fetchall(),
        }


def overview(directory, json_output=True):
    command = [sys.executable, str(ROOT / 'lib/node_overview.py'), '--database', str(DATABASE),
               '--info-dir', str(directory / 'info'), '--host', '127.0.0.1']
    if json_output:
        command.append('--json')
    result = subprocess.run(command, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError('overview failed: ' + result.stderr[-2000:])
    return json.loads(result.stdout) if json_output else result.stdout


def fetch(url):
    # Ignore any ambient proxy: every expected URL belongs to this fixture.
    assert url.startswith(f'http://127.0.0.1:{SUB_PORT}/'), 'unexpected subscription destination'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + 10
    while True:
        try:
            with opener.open(url, timeout=3) as response:
                body = response.read().decode('utf-8')
            break
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(.2)
    try:
        decoded = base64.b64decode(body.strip(), validate=True).decode('utf-8')
    except (ValueError, UnicodeError):
        decoded = body
    return decoded


def main():
    if ROOT != Path('/repo') or not Path('/app/x-ui').is_file() or DATABASE.exists():
        raise SystemExit('Run only in a fresh disposable official panel container with /repo mounted read-only.')
    with tempfile.TemporaryDirectory(prefix='3xuish-overview-smoke-') as temporary:
        directory = Path(temporary)
        for index, (name, enabled, sub_id) in enumerate((
                ('Existing manual node', True, SHARED),
                ('Old installer node', True, SHARED),
                ('SOCKS5-existing-proxy', True, PROXY),
                ('Disabled historical node', False, SHARED))):
            inbound = {
                'enable': enabled, 'port': 26610 + index, 'protocol': 'vless', 'listen': '127.0.0.1',
                'tag': 'overview-node-' + str(index), 'remark': name,
                'settings': json.dumps({'clients': [{'id': IDS[index], 'email': 'overview-client-' + str(index),
                                       'enable': True, 'subId': sub_id, 'expiryTime': 0, 'totalGB': 0}],
                                       'decryption': 'none'}),
                'streamSettings': json.dumps({'network': 'tcp', 'security': 'none'}),
                'sniffing': json.dumps({'enabled': False}),
            }
            (directory / f'inbound-{index}.json').write_text(json.dumps(inbound), encoding='utf-8')
        script = directory / 'fixture.sh'
        script.write_text(SHELL, encoding='utf-8')
        environment = os.environ.copy()
        environment['SMOKE_ROOT'] = str(directory)
        with (directory / 'fixture.log').open('w+', encoding='utf-8') as log:
            process = subprocess.Popen(['bash', str(script)], env=environment, stdin=subprocess.PIPE,
                                       stdout=log, stderr=subprocess.STDOUT, text=True)
            try:
                wait_marker(directory / 'ready', process)
                assert not (directory / 'info/3xuish-socks-exit').exists(), 'fixture unexpectedly has job history'
                before = snapshot()
                report = overview(directory)
                text = overview(directory, json_output=False)
                assert snapshot() == before, 'viewing overview modified panel identities/settings/tokens'
                assert report['database_status'] == 'ok'
                assert len(report['nodes']) == 4, 'overview omitted manual, old or disabled nodes'
                assert {node['name'] for node in report['nodes']} == {
                    'Existing manual node', 'Old installer node', 'SOCKS5-existing-proxy', 'Disabled historical node'}
                assert report['task']['status'] == 'no_previous_job'
                assert len(report['subscriptions']) == 2, 'shared subIds were not grouped'
                shared = next(sub for sub in report['subscriptions'] if sub['url'].endswith('/' + SHARED))
                proxy = next(sub for sub in report['subscriptions'] if sub['url'].endswith('/' + PROXY))
                assert len(shared['nodes']) == 3, 'shared subscription coverage omitted a bound inbound'
                shared_body = fetch(shared['url'])
                assert IDS[0] in shared_body and IDS[1] in shared_body, 'shared URL did not return both enabled nodes'
                assert IDS[2] not in shared_body and IDS[3] not in shared_body, 'subscription included unrelated/disabled node'
                assert IDS[2] in fetch(proxy['url']), 'proxy subscription URL did not return the proxy node'
                clash = fetch(shared['clash_url'])
                assert IDS[0] in clash and IDS[1] in clash, 'Clash URL did not export shared nodes'
                assert all(identifier not in text for identifier in IDS), 'overview printed client UUIDs'
                assert 'fixture-panel-password' not in text, 'overview leaked panel password'
                assert 'no_previous_job' not in text and '"operation"' not in text, 'human view printed internal JSON'
                assert 'Existing manual node' in text and 'Old installer node' in text
                process.stdin.write('disable\n')
                process.stdin.flush()
                wait_marker(directory / 'disabled', process)
                before_disabled = snapshot()
                disabled = overview(directory)
                assert snapshot() == before_disabled, 'disabled-service view modified panel state'
                assert len(disabled['nodes']) == 4, 'disabled subscriptions hid panel nodes'
                assert disabled['subscription_service']['enabled'] is False
                assert all(not sub['url'] and not sub['clash_url'] for sub in disabled['subscriptions'])
                process.stdin.write('finish\n')
                process.stdin.flush()
                assert process.wait(timeout=10) == 0, 'fixture failed to stop normally'
                print('NODE_OVERVIEW_SMOKE_OK: all existing nodes without job history; normalized clients; '
                      'real shared/proxy/Clash subscription URLs; disabled service explained; '
                      'no identity/settings/token writes')
            except BaseException:
                log.flush()
                log.seek(0)
                print(log.read()[-6000:], file=sys.stderr)
                panel_log = directory / 'panel.log'
                if panel_log.exists():
                    print(panel_log.read_text(errors='replace')[-3000:], file=sys.stderr)
                raise
            finally:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=8)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == '__main__':
    main()
