#!/usr/bin/env bash
# Disposable official 3x-ui container, /repo read-only; never a live server.
# shellcheck disable=SC2034
set -euo pipefail
source /repo/install.sh
XUI_INSTALL_DIR=/app
XUI_DB_PATH=/etc/x-ui/x-ui.db
RUN_DIR=$(mktemp -d /tmp/3xuish.performance-smoke.XXXXXXXX)
XUI_INFO_DIR="$RUN_DIR/info"
PANEL_PORT=20530
WEB_BASE_PATH=performance-smoke
PANEL_USER=fixture-performance
PANEL_PASS=fixture-performance-password
SERVER_IP=127.0.0.1
CERT_HOST=example.test
CERT_FILE="$RUN_DIR/cert.pem"
KEY_FILE="$RUN_DIR/key.pem"
RAW_PORT=30443
REALITY_PORT=31443
HY2_PORT=32443
XHTTP_PORT=33443
XHTTP_PATH=/keep-existing-path
PRIMARY_NODE=hy2
INBOUND_TLS_MODE=1
VLESS_ENC_TYPE=1
REALITY_TARGET=www.microsoft.com:443
REALITY_SERVER_NAME=www.microsoft.com
ARCH=$(get_arch)
panel_pid=''
finish() {
    local result=$?
    trap - EXIT
    [[ -z "$panel_pid" ]] || kill "$panel_pid" 2>/dev/null || true
    exit "$result"
}
trap finish EXIT
mkdir -p /etc/x-ui "$XUI_INFO_DIR"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=example.test \
    -addext 'subjectAltName=DNS:example.test,IP:127.0.0.1' \
    -keyout "$KEY_FILE" -out "$CERT_FILE" >/dev/null 2>&1
"$XUI_INSTALL_DIR/x-ui" setting -username "$PANEL_USER" -password "$PANEL_PASS" \
    -port "$PANEL_PORT" -webBasePath "$WEB_BASE_PATH" >"$RUN_DIR/settings.log" 2>&1
"$XUI_INSTALL_DIR/x-ui" cert -webCert "$CERT_FILE" -webCertKey "$KEY_FILE" >>"$RUN_DIR/settings.log" 2>&1
(
    cd "$XUI_INSTALL_DIR"
    exec ./x-ui run
) >"$RUN_DIR/panel.log" 2>&1 &
panel_pid=$!
ready=0
for _ in {1..30}; do
    if curl --noproxy '*' -fsk "https://127.0.0.1:$PANEL_PORT/$WEB_BASE_PATH/" -o /dev/null; then
        ready=1
        break
    fi
    kill -0 "$panel_pid"
    sleep 1
done
[[ "$ready" == 1 ]]
generate_vlessenc_keys
generate_reality_keys
prepare_node_profiles
api_init
while IFS= read -r payload; do
    response=$(api_request POST '/panel/api/inbounds/add' "$payload")
    jq -e '.success == true' <<<"$response" >/dev/null
done < <(jq -c '
    .[] | select(.key == "xhttp" or .key == "raw") | . as $profile | .payload |
    .tag = (if $profile.key == "xhttp" then "legacy-performance" else "unrelated-performance" end) |
    .remark = (if $profile.key == "xhttp" then "VLESS-XHTTP-Enc-Vision" else "Unrelated user node" end) |
    .streamSettings |= (fromjson |
        .sockopt = {tcpWindowClamp:600,tcpMaxSeg:1440,tcpUserTimeout:10000,
                    tcpcongestion:"bbr",tcpKeepAliveIdle:300,domainStrategy:"UseIP"} | tojson)
' <<<"$NODE_PROFILES")
api_request POST '/panel/api/server/restartXrayService' '{}' | jq -e '.success == true' >/dev/null
for _ in {1..15}; do
    if ss -Hlntp '( sport = :33443 )' | grep -q xray; then break; fi
    sleep 1
done
ss -Hlntp '( sport = :33443 )' | grep -q xray
printf 'unchanged-existing-import-links\n' >"$XUI_INFO_DIR/3x-ui-node-links.txt"
printf 'unchanged-existing-panel-information\n' >"$XUI_INFO_DIR/3x-ui-nodes-info.txt"
export XUI_INSTALL_DIR XUI_DB_PATH XUI_INFO_DIR RUN_DIR API_BASE_URL
export PERFORMANCE_PANEL_PID="$panel_pid"
python3 - <<'PY'
import copy,json,os,pathlib,re,sqlite3,ssl,subprocess,sys,time,urllib.request
from contextlib import closing
sys.path.insert(0,'/repo/lib')
import performance_repair as repair
info=pathlib.Path(os.environ['XUI_INFO_DIR'])
runtime=pathlib.Path(os.environ['XUI_INSTALL_DIR'])/'bin/config.json'
database=pathlib.Path(os.environ['XUI_DB_PATH'])
xray=str(pathlib.Path(os.environ['XUI_INSTALL_DIR'])/'bin/xray-linux-amd64')
auth=pathlib.Path(os.environ['RUN_DIR'])/'api-auth.conf'
match=re.search(r'Authorization: Bearer ([^"\r\n]+)',auth.read_text())
if not match: raise SystemExit('SMOKE_AUTH_NOT_AVAILABLE')
token=match[1]
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),
    urllib.request.HTTPSHandler(context=ssl._create_unverified_context()))
def rows():
    with closing(sqlite3.connect(database)) as con:
        con.row_factory=sqlite3.Row
        return {row['id']:dict(row) for row in con.execute('SELECT * FROM inbounds ORDER BY id')}
before=rows()
legacy=next(row for row in before.values() if row['tag']=='legacy-performance')
links=(info/'3x-ui-node-links.txt').read_bytes()
details=(info/'3x-ui-nodes-info.txt').read_bytes()
class LivePanel:
    def __init__(self): self.restart_count=0
    def is_active(self):
        try: os.kill(int(os.environ['PERFORMANCE_PANEL_PID']),0); return True
        except OSError: return False
    def validate(self,path):
        p=subprocess.run([xray,'run','-test','-c',str(path)],cwd=os.environ['XUI_INSTALL_DIR'],
                         capture_output=True,timeout=20)
        if p.returncode: raise RuntimeError('real core rejected candidate')
    def restart(self):
        self.restart_count+=1
        request=urllib.request.Request(os.environ['API_BASE_URL']+'/panel/api/server/restartXrayService',
            data=b'{}',method='POST',headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
        with opener.open(request,timeout=20) as response: data=json.load(response)
        if data.get('success') is not True: raise RuntimeError('real panel restart rejected')
    def listeners(self):
        p=subprocess.run(['ss','-H','-lntp'],capture_output=True,text=True,check=True,timeout=10)
        result=set()
        for line in p.stdout.splitlines():
            if 'xray' not in line: continue
            fields=line.split()
            try: result.add(int(fields[3].rsplit(':',1)[1]))
            except (ValueError,IndexError): pass
        return result
    def sleep(self,seconds): time.sleep(seconds)
adapter=LivePanel()
config={'database':str(database),'install_dir':os.environ['XUI_INSTALL_DIR'],
        'info_dir':str(info),'service':'fixture-panel','xray':xray,'inbound_id':None,
        'verify_attempts':40,'verify_delay':0.25}
plan=repair.make_plan(database,info/'3x-ui-node-profiles.json')
assert {row['id'] for row in plan['rows']}=={legacy['id']}
result=repair.execute_repair(config,info/'performance-job',adapter=adapter)
assert result['status'] in {'complete','completed','success'}, result
after=rows()
assert set(after)==set(before)
for ident,old in before.items():
    if ident==legacy['id']:
        old=copy.deepcopy(old)
        stream=json.loads(old['stream_settings'])
        stream['sockopt']={'tcpWindowClamp':0,'tcpKeepAliveIdle':300,'domainStrategy':'UseIP'}
        actual=copy.deepcopy(after[ident])
        assert json.loads(actual.pop('stream_settings'))==stream
        old.pop('stream_settings')
        assert actual==old
    else:
        assert after[ident]==old
assert (info/'3x-ui-node-links.txt').read_bytes()==links
assert (info/'3x-ui-nodes-info.txt').read_bytes()==details
assert {30443,33443}<=adapter.listeners()
assert adapter.restart_count==1
second=repair.execute_repair(config,info/'repeat-job',adapter=adapter)
assert second['status'] in {'no_changes','unchanged','noop','not_needed','complete'},second
assert adapter.restart_count==1
print('PERFORMANCE_PANEL_SMOKE_OK: original IDs/ports/credentials/links preserved; legacy tuning repaired; unrelated node unchanged; real Xray verified; repeat does not restart')
PY
