#!/usr/bin/env bash
# Run only inside a disposable official 3x-ui container with /repo mounted read-only.
# Fixture globals are consumed by the sourced installer functions.
# shellcheck disable=SC2034
set -euo pipefail
source /repo/install.sh

XUI_INSTALL_DIR="${PANEL_SMOKE_DIR:-/app}"
XUI_DB_PATH=/etc/x-ui/x-ui.db
RUN_DIR=$(mktemp -d /tmp/3xuish.existing-smoke.XXXXXXXX)
XUI_INFO_DIR="$RUN_DIR/info"
PANEL_PORT=20530
WEB_BASE_PATH=installer-smoke
PANEL_USER=installer-fixture
PANEL_PASS=fixture-password-for-isolated-container
SERVER_IP=127.0.0.1
CERT_HOST=example.test
CERT_FILE="$RUN_DIR/cert.pem"
KEY_FILE="$RUN_DIR/key.pem"
RAW_PORT=30443
REALITY_PORT=31443
HY2_PORT=32443
XHTTP_PORT=443
XHTTP_PATH=/existing-node
PRIMARY_NODE=hy2
INBOUND_TLS_MODE=1
VLESS_ENC_TYPE=1
REALITY_TARGET=www.microsoft.com:443
REALITY_SERVER_NAME=www.microsoft.com
ARCH=$(get_arch)
panel_pid=''
stage=setup

finish() {
    local result=$?
    trap - EXIT
    if [[ "$result" -ne 0 ]]; then
        printf 'EXISTING_PANEL_SMOKE_FAILED stage=%s exit=%s\n' "$stage" "$result" >&2
        if [[ -s "$RUN_DIR/add.log" ]]; then
            grep -E '\[(INFO|WARN|ERROR)\]' "$RUN_DIR/add.log" |
                sed -E 's/[A-Za-z0-9_+=\/-]{36,}/<redacted>/g' >&2 || true
        fi
    fi
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
    if curl -fsk "https://127.0.0.1:$PANEL_PORT/$WEB_BASE_PATH/" -o /dev/null; then
        ready=1
        break
    fi
    kill -0 "$panel_pid"
    sleep 1
done
[[ "$ready" == 1 ]]

stage=original-node
generate_vlessenc_keys
generate_reality_keys
prepare_node_profiles
api_init
original_payload=$(jq -c '.[] | select(.key == "xhttp") | .payload |
    .tag="user-existing-xhttp443" | .remark="Original user XHTTP"' <<<"$NODE_PROFILES")
original_response=$(api_request POST '/panel/api/inbounds/add' "$original_payload")
jq -e '.success == true and (.obj.id | type == "number")' <<<"$original_response" >/dev/null
api_request POST '/panel/api/server/restartXrayService' '{}' | jq -e '.success == true' >/dev/null
for _ in {1..15}; do
    if ss -Hlntp '( sport = :443 )' | grep -q xray; then break; fi
    sleep 1
done
ss -Hlntp '( sport = :443 )' | grep -q xray

sqlite3 -json "$XUI_DB_PATH" "SELECT id,tag,port,settings,stream_settings FROM inbounds WHERE tag='user-existing-xhttp443';" >"$RUN_DIR/original-inbound.json"
sqlite3 -json "$XUI_DB_PATH" "SELECT key,value FROM settings WHERE key IN ('webPort','webBasePath','webCertFile','webKeyFile') ORDER BY key;" >"$RUN_DIR/original-settings.json"
sqlite3 -json "$XUI_DB_PATH" "SELECT * FROM users ORDER BY id;" >"$RUN_DIR/original-users.json"
cat >"$XUI_INFO_DIR/3x-ui-xhttp-info.txt" <<EOF
[面板信息]
面板地址:     https://${CERT_HOST}:${PANEL_PORT}/${WEB_BASE_PATH}/
管理员账号:   ${PANEL_USER}
管理员密码:   ${PANEL_PASS}
EOF

# These narrow overrides prevent network/package operations; all panel, database,
# configuration generation, authentication and listener checks are the real code.
install_deps() { :; }
detect_server_ip() { SERVER_IP=127.0.0.1; }
verify_reality_target() { :; }
AUTO_YES=1
XUI_USERNAME="$PANEL_USER"
XUI_PASSWORD="$PANEL_PASS"
unset RAW_PORT REALITY_PORT HY2_PORT XHTTP_PORT XHTTP_PATH
NODE_PROFILES='[]'
API_READY=0
API_AUTH=()
ATTEMPTED_TAGS=()
CREATED_IDS=()
stage=add-existing
add_nodes_to_existing >"$RUN_DIR/add.log" 2>&1

stage=preservation-check
[[ $(sqlite3 "$XUI_DB_PATH" 'SELECT count(*) FROM inbounds;') == 5 ]]
sqlite3 -json "$XUI_DB_PATH" "SELECT id,tag,port,settings,stream_settings FROM inbounds WHERE tag='user-existing-xhttp443';" >"$RUN_DIR/after-inbound.json"
sqlite3 -json "$XUI_DB_PATH" "SELECT key,value FROM settings WHERE key IN ('webPort','webBasePath','webCertFile','webKeyFile') ORDER BY key;" >"$RUN_DIR/after-settings.json"
sqlite3 -json "$XUI_DB_PATH" "SELECT * FROM users ORDER BY id;" >"$RUN_DIR/after-users.json"
cmp "$RUN_DIR/original-inbound.json" "$RUN_DIR/after-inbound.json"
cmp "$RUN_DIR/original-settings.json" "$RUN_DIR/after-settings.json"
cmp "$RUN_DIR/original-users.json" "$RUN_DIR/after-users.json"
jq -e 'length == 4 and ([.[].key] | sort) == ["hy2","raw","reality","xhttp"] and
    (map(select(.key == "raw"))[0].port != 443)' "$XUI_INFO_DIR/3x-ui-node-profiles.json" >/dev/null
cp "$XUI_INFO_DIR/3x-ui-node-profiles.json" "$RUN_DIR/first-profiles.json"
[[ $(wc -l <"$XUI_INFO_DIR/3x-ui-node-links.txt") == 4 ]]

stage=repeat-existing
add_nodes_to_existing >>"$RUN_DIR/add.log" 2>&1
[[ $(sqlite3 "$XUI_DB_PATH" 'SELECT count(*) FROM inbounds;') == 5 ]]
cmp "$RUN_DIR/first-profiles.json" "$XUI_INFO_DIR/3x-ui-node-profiles.json"
printf 'EXISTING_PANEL_SMOKE_OK: original XHTTP/account/cert preserved; four permanent API nodes validated; rerun creates no duplicates\n'
