#!/usr/bin/env bash
# Existing server diagnostics; no installation or service/configuration changes.
set -euo pipefail
umask 077

PROBE_SHA256="8e6d0714098ae422ceb986df1a406e647621bf6deb70f428fd8787250de882df"
TEST_SHA256="4b330afe8189176c2a9d801bd2a8dd40c29b3d1aa5275706832c13adc16c8ea6"
BASE_URL="https://raw.githubusercontent.com/Iwhrt72/3xuish/main/scripts"
SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SPEED_RUN_DIR=""
SPEED_PID=""

cleanup() {
    local result=$?
    trap - EXIT INT TERM HUP
    if [[ -n "$SPEED_PID" ]] && kill -0 "$SPEED_PID" 2>/dev/null; then
        kill -TERM "$SPEED_PID" 2>/dev/null || true
        wait "$SPEED_PID" 2>/dev/null || true
    fi
    if [[ -n "$SPEED_RUN_DIR" && "$SPEED_RUN_DIR" == /tmp/3xuish-speed.* && -d "$SPEED_RUN_DIR" ]]; then
        rm -rf -- "$SPEED_RUN_DIR"
    fi
    exit "$result"
}

for dependency in python3 curl sha256sum; do
    command -v "$dependency" >/dev/null 2>&1 || {
        printf '缺少 %s；请先安装依赖，脚本不会自动安装。\n' "$dependency" >&2
        exit 1
    }
done
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || {
    printf '需要 Python 3.8 或更新版本。\n' >&2
    exit 1
}
SPEED_RUN_DIR=$(mktemp -d /tmp/3xuish-speed.XXXXXXXX)
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

for name in server_speed_probe.py server_speed_test.py; do
    if [[ -f "$SOURCE_DIR/$name" ]]; then
        cp -- "$SOURCE_DIR/$name" "$SPEED_RUN_DIR/$name"
    else
        curl -q -fLsS --connect-timeout 10 --max-time 60 "$BASE_URL/$name" -o "$SPEED_RUN_DIR/$name"
    fi
    expected="$PROBE_SHA256"
    [[ "$name" != server_speed_test.py ]] || expected="$TEST_SHA256"
    actual=$(sha256sum "$SPEED_RUN_DIR/$name")
    [[ "${actual%% *}" == "$expected" ]] || {
        printf '测速组件校验失败；请重新下载同一版本脚本后再试。\n' >&2
        exit 1
    }
done
python3 "$SPEED_RUN_DIR/server_speed_test.py" "$@" &
SPEED_PID=$!
result=0
wait "$SPEED_PID" || result=$?
SPEED_PID=""
exit "$result"
