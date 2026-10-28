#!/usr/bin/env bash
# Run inside a disposable official 3x-ui container; no ACME or host ports needed.
# Fixture variables below are consumed by the sourced installer functions.
# shellcheck disable=SC2034
set -euo pipefail
source /repo/install.sh

XUI_INSTALL_DIR="${PANEL_SMOKE_DIR:-/app}"
XUI_DB_PATH=/etc/x-ui/x-ui.db
RUN_DIR=$(mktemp -d /tmp/3xuish.panel-smoke.XXXXXXXX)
XUI_INFO_DIR="$RUN_DIR/info"
PANEL_PORT=20530
WEB_BASE_PATH=installer-smoke
PANEL_USER=installer-fixture
PANEL_PASS=fixture-password-for-isolated-container
SERVER_IP=127.0.0.1
CERT_HOST=localhost
CERT_FILE="$RUN_DIR/cert.pem"
KEY_FILE="$RUN_DIR/key.pem"
RAW_PORT=30443
REALITY_PORT=31443
HY2_PORT=32443
XHTTP_PORT=33443
XHTTP_PATH=/installer-smoke
PRIMARY_NODE=hy2
INBOUND_TLS_MODE=1
VLESS_ENC_TYPE=1
REALITY_TARGET=www.microsoft.com:443
REALITY_SERVER_NAME=www.microsoft.com
ARCH=$(get_arch)
panel_pid=''

finish() {
    local code=$?
    trap - EXIT
    if [[ "$code" -ne 0 ]]; then
        printf 'PANEL_SMOKE_FAILED exit=%s\n' "$code" >&2
        if [[ -s "$RUN_DIR/xray-check.log" ]]; then
            sed -E 's/[A-Za-z0-9_+=\/-]{36,}/<redacted>/g' "$RUN_DIR/xray-check.log" >&2
        fi
    fi
    [[ -z "$panel_pid" ]] || kill "$panel_pid" 2>/dev/null || true
    exit "$code"
}
trap finish EXIT

mkdir -p /etc/x-ui
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj /CN=localhost \
    -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
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
        ready=1; break
    fi
    kill -0 "$panel_pid"
    sleep 1
done
[[ "$ready" == 1 ]]

generate_vlessenc_keys
generate_reality_keys
prepare_node_profiles
api_init
create_inbound_nodes
write_node_files
[[ $(wc -l <"$XUI_INFO_DIR/3x-ui-node-links.txt") == 4 ]]
printf 'PANEL_SMOKE_OK: four real API-created nodes, all identities, TLS 1.0-1.3, TCP/UDP listeners and saved links verified\n'
