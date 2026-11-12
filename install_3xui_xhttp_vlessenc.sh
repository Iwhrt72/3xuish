#!/usr/bin/env bash
# ==============================================================================
# 3x-ui (MHSanaei) 一键部署与后期域名平滑切换脚本
# 仓库: https://github.com/Iwhrt72/3xuish
# 默认生成: Hy2 / RAW TCP + TLS + Vision / REALITY / XHTTP + VLESS Encryption
# 证书: 支持 一键 IP 证书 (默认) / 域名证书 / 后期从 IP 无缝切换至域名
# 适用系统: Debian / Ubuntu / CentOS / RHEL / Rocky / AlmaLinux / Alpine / Arch
# ==============================================================================

set -euo pipefail
umask 077

REPO_RAW_URL="https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh"
INSTALLER_SOURCE_FILE="${BASH_SOURCE[0]:-}"
XUI_INSTALL_DIR="${XUI_INSTALL_DIR:-/usr/local/x-ui}"
XUI_DB_PATH="${XUI_DB_PATH:-/etc/x-ui/x-ui.db}"
XUI_INFO_DIR="${XUI_INFO_DIR:-/root}"
XUI_SERVICE="${XUI_SERVICE:-x-ui}"
RUN_DIR=""
API_READY=0
API_BASE_URL=""
API_COMMON=()
API_AUTH=()
CREATED_IDS=()
ATTEMPTED_TAGS=()
ROLLBACK_NEEDED=0
NODE_PROFILES='[]'
PERFORMANCE_HELPER_SHA256="f2bb9385286065ad700754143de8ce5b8541721f583a1a218c8a697bc2a5ed44"
SPEED_TEST_SHA256="ad35625188532e12869684997eb919f928aa9b876e3742e3dff6a277e8a2ec0b"
CLEAN_REINSTALL_SHA256="62dd24504a197c0b23dc9c20340b671cfafb2cc36bac60ca51abea7a468f1424"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
PLAIN='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${PLAIN} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${PLAIN} $*"; }
log_error() { echo -e "${RED}[ERROR]${PLAIN} $*"; }
fail() { log_error "$*" >&2; return 1; }

# 兼容管道执行 (curl ... | bash) 的交互式读取函数
prompt_read() {
    local prompt_text="$1"
    local var_name="$2"
    if [[ -t 0 ]]; then
        # shellcheck disable=SC2229
        read -rp "$prompt_text" "$var_name" || printf -v "$var_name" '%s' ""
    elif [[ -r /dev/tty ]] && { true </dev/tty; } 2>/dev/null; then
        # shellcheck disable=SC2229
        read -rp "$prompt_text" "$var_name" </dev/tty || printf -v "$var_name" '%s' ""
    else
        printf -v "$var_name" '%s' ""
    fi
}

# 只在执行安装时检查系统；source 本文件可测试配置生成，不会安装或重启服务。
init_platform() {
    [[ $EUID -eq 0 ]] || { fail "请使用 root 用户运行：sudo -i"; return 1; }
    [[ -f /etc/os-release ]] || { fail "无法识别系统 /etc/os-release。"; return 1; }
    # shellcheck disable=SC1091
    source /etc/os-release
    RELEASE=$ID
    ARCH=$(get_arch)
    RUN_DIR=$(mktemp -d /tmp/3xuish.XXXXXXXX)
    trap cleanup_install EXIT
}

get_arch() {
    case "$(uname -m)" in
        x86_64 | x64 | amd64) echo 'amd64' ;;
        i*86 | x86) echo '386' ;;
        armv8* | arm64 | aarch64) echo 'arm64' ;;
        armv7* | arm) echo 'armv7' ;;
        s390x) echo 's390x' ;;
        *) log_error "不支持的 CPU 架构: $(uname -m)" && exit 1 ;;
    esac
}

valid_ipv4() {
    local address="$1" octet
    [[ "$address" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || return 1
    local parts=()
    IFS='.' read -r -a parts <<<"$address"
    for octet in "${parts[@]}"; do ((10#$octet <= 255)) || return 1; done
}

valid_domain() {
    local domain="$1" label
    [[ ${#domain} -le 253 && "$domain" == *.* && "$domain" != *..* ]] || return 1
    local labels=()
    IFS='.' read -r -a labels <<<"$domain"
    for label in "${labels[@]}"; do
        [[ ${#label} -ge 1 && ${#label} -le 63 && "$label" =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?$ ]] || return 1
    done
}

valid_port() {
    [[ "$1" =~ ^[0-9]{1,5}$ ]] && ((10#$1 >= 1 && 10#$1 <= 65535))
}

random_panel_port() {
    local candidate
    for _ in {1..30}; do
        candidate=$((10000 + RANDOM % 45001))
        if [[ "$candidate" != "${RAW_PORT:-443}" && "$candidate" != "${REALITY_PORT:-24443}" &&
              "$candidate" != "${XHTTP_PORT:-8443}" ]]; then
            printf '%s\n' "$candidate"; return 0
        fi
    done
    fail "未能生成可用面板端口。"
}

validate_config() {
    case "${PRIMARY_NODE:-}" in hy2|raw|reality|xhttp) ;; *) fail "PRIMARY_NODE 只能是 hy2、raw、reality 或 xhttp。"; return 1 ;; esac
    local name value seen='|80|'
    for name in PANEL_PORT RAW_PORT REALITY_PORT XHTTP_PORT HY2_PORT; do
        value="${!name:-}"
        valid_port "$value" || { fail "${name} 必须是 1–65535 的端口。"; return 1; }
        printf -v "$name" '%d' "$((10#$value))"
        value="${!name}"
        if [[ "$name" != HY2_PORT ]]; then
            [[ "$seen" != *"|${value}|"* ]] || { fail "TCP 端口冲突：${name}=${value}，80/TCP 留给证书验证。"; return 1; }
            seen+="${value}|"
        fi
    done
    case "${CERT_MODE:-1}" in
        1) ;; 2) valid_domain "${DOMAIN_NAME:-}" || { fail "请输入有效域名；不要包含协议、路径或空格。"; return 1; } ;;
        *) fail "CERT_MODE 只能是 1 或 2。"; return 1 ;;
    esac
    case "${VLESS_ENC_TYPE:-1}" in 1|2) ;; *) fail "VLESS_ENC_TYPE 只能是 1 或 2。"; return 1 ;; esac
    case "${INBOUND_TLS_MODE:-1}" in 1|2) ;; *) fail "INBOUND_TLS_MODE 只能是 1 或 2。"; return 1 ;; esac
    [[ "${WEB_BASE_PATH:-}" =~ ^[A-Za-z0-9_-]+$ ]] || { fail "面板路径只允许字母、数字、下划线和短横线。"; return 1; }
    valid_domain "${REALITY_SERVER_NAME:-www.apple.com}" || { fail "REALITY_SERVER_NAME 必须为有效域名。"; return 1; }
    local target="${REALITY_TARGET:-${REALITY_SERVER_NAME:-www.apple.com}:443}"
    [[ "$target" =~ ^([A-Za-z0-9.-]+):([0-9]+)$ ]] && valid_port "${BASH_REMATCH[2]}" || {
        fail "REALITY_TARGET 格式应为 域名:端口。"; return 1;
    }
}

check_ports_available() {
    local name port
    for name in PANEL_PORT RAW_PORT REALITY_PORT XHTTP_PORT; do
        port="${!name}"
        [[ -z "$(ss -H -lnt "sport = :${port}")" ]] || { fail "${name}=${port}/TCP 已被占用，请指定其它端口。"; return 1; }
    done
    [[ -z "$(ss -H -lnu "sport = :${HY2_PORT}")" ]] || { fail "HY2_PORT=${HY2_PORT}/UDP 已被占用。"; return 1; }
}

restart_panel() {
    if command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; then
        systemctl restart "$XUI_SERVICE"
    else
        rc-service "$XUI_SERVICE" restart
    fi
}

resolve_xray_binary() {
    local matches=()
    if [[ -x "${XRAY_BIN:-}" ]]; then return 0; fi
    if [[ -x "${XUI_INSTALL_DIR}/bin/xray-linux-${ARCH:-amd64}" ]]; then
        XRAY_BIN="${XUI_INSTALL_DIR}/bin/xray-linux-${ARCH:-amd64}"
    else
        mapfile -t matches < <(find "${XUI_INSTALL_DIR}/bin" -maxdepth 1 -name 'xray-linux-*' -type f)
        [[ ${#matches[@]} -eq 1 && -x "${matches[0]}" ]] || { fail "未找到唯一可执行的 Xray 内核。"; return 1; }
        XRAY_BIN="${matches[0]}"
    fi
}

gen_random_str() {
    local len="${1:-12}"
    openssl rand -hex "$(((len + 1) / 2))" | cut -c "1-${len}"
}

gen_uuid() {
    if [[ -r /proc/sys/kernel/random/uuid ]]; then
        cat /proc/sys/kernel/random/uuid
    elif command -v uuidgen >/dev/null 2>&1; then
        uuidgen
    else
        openssl rand -hex 16 | sed -E 's/^(.{8})(.{4})(.{4})(.{4})(.{12})$/\1-\2-\3-\4-\5/'
    fi
}

# 3. 安装基础依赖工具 (含 sqlite3 用于后期无损切换域名证书)
install_deps() {
    log_info "正在检查并安装依赖：curl、jq、openssl、sqlite3、网络监听检查及证书续期工具。"
    case "${RELEASE}" in
        ubuntu | debian | armbian)
            apt-get update -y -q
            apt-get install -y -q curl jq socat openssl sqlite3 cron tar ca-certificates tzdata iproute2
            systemctl enable --now cron >/dev/null 2>&1 || true
            ;;
        centos | rhel | almalinux | rocky | fedora | amzn | ol)
            if command -v dnf >/dev/null 2>&1; then
                dnf install -y -q curl jq socat openssl sqlite cronie tar ca-certificates tzdata iproute
            else
                yum install -y -q curl jq socat openssl sqlite cronie tar ca-certificates tzdata iproute
            fi
            systemctl enable --now crond >/dev/null 2>&1 || true
            ;;
        alpine)
            apk update
            apk add --no-cache curl jq socat openssl sqlite dcron tar ca-certificates tzdata bash iproute2
            rc-update add dcron default >/dev/null 2>&1 || true
            rc-service dcron start >/dev/null 2>&1 || true
            ;;
        arch | manjaro)
            pacman -Sy --noconfirm curl jq socat openssl sqlite cronie tar ca-certificates tzdata iproute2
            systemctl enable --now cronie >/dev/null 2>&1 || true
            ;;
        *)
            apt-get update -y -q && apt-get install -y -q curl jq socat openssl sqlite3 cron tar ca-certificates iproute2
            ;;
    esac
}

# 4. 获取服务器公网 IPv4 地址
detect_server_ip() {
    local urls=(
        "https://api4.ipify.org"
        "https://ipv4.icanhazip.com"
        "https://v4.api.ipinfo.io/ip"
        "https://4.ident.me"
    )
    SERVER_IP="${XUI_SERVER_IP:-}"
    if [[ -z "$SERVER_IP" ]]; then
        for u in "${urls[@]}"; do
            local ip
            ip=$(curl -fsS4m 4 "$u" | tr -d '[:space:]') || continue
            if [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
                SERVER_IP="$ip"
                break
            fi
        done
    fi
    if [[ -z "$SERVER_IP" ]]; then
        prompt_read "未能自动检测到公网 IPv4，请手动输入服务器 IPv4 地址: " SERVER_IP
    fi
    valid_ipv4 "$SERVER_IP" || { fail "服务器 IPv4 地址无效。"; return 1; }
    log_info "检测到服务器公网 IPv4: ${CYAN}${SERVER_IP}${PLAIN}"
}

# 5. 释放 80 端口并确保 acme.sh 就绪
run_acme() {
    ~/.acme.sh/acme.sh "$@"
}

validate_certificate_pair() {
    local host="$1" cert="$2" key="$3" match_option cert_public key_public
    [[ -s "$cert" && -s "$key" ]] || { fail "证书或私钥文件不存在或为空。"; return 1; }
    openssl x509 -in "$cert" -checkend 0 -noout >/dev/null 2>&1 || {
        fail "证书已过期或无法读取，未采用该证书。"; return 1;
    }
    if valid_ipv4 "$host"; then
        match_option=-verify_ip
    elif valid_domain "$host"; then
        match_option=-verify_hostname
    else
        fail "证书目标地址无效。"; return 1
    fi
    # x509 -checkhost may return zero even on a name mismatch. verify has a
    # reliable failure status and also rejects certificates not yet valid.
    # Trusting this local leaf is only for identity/validity checks, not CA proof.
    openssl verify -partial_chain -trusted "$cert" "$match_option" "$host" "$cert" >/dev/null 2>&1 || {
        fail "证书不覆盖节点地址 ${host} 或尚未生效，未采用该证书。"; return 1;
    }
    cert_public=$(openssl x509 -in "$cert" -pubkey -noout 2>/dev/null |
        openssl pkey -pubin -outform DER 2>/dev/null | openssl dgst -sha256) || {
        fail "无法读取证书公钥。"; return 1;
    }
    key_public=$(openssl pkey -in "$key" -passin pass: -pubout -outform DER 2>/dev/null |
        openssl dgst -sha256) || { fail "无法读取证书私钥。"; return 1; }
    [[ "$cert_public" == "$key_public" ]] || { fail "证书与私钥不匹配。"; return 1; }
}

prepare_acme_port80() {
    log_info "正在检查 80 端口占用情况（申请证书需要通过 80 端口验证）..."
    if command -v ss >/dev/null 2>&1 && ss -ltn | awk '{print $4}' | grep -Eq ':80$'; then
        fail "TCP/80 已被占用；请先释放该端口后重试证书申请。"
        return 1
    fi

    if [[ ! -f ~/.acme.sh/acme.sh ]]; then
        log_info "正在安装 acme.sh..."
        curl -fsSL --connect-timeout 10 --max-time 60 https://get.acme.sh -o "$RUN_DIR/acme-install.sh" || {
            fail "acme.sh 下载失败。"; return 1;
        }
        sh "$RUN_DIR/acme-install.sh" >/dev/null || { fail "acme.sh 安装失败。"; return 1; }
    fi
    run_acme --set-default-ca --server letsencrypt --force >/dev/null 2>&1 || {
        fail "无法配置 acme.sh 默认 CA。"; return 1;
    }
}

# 6A. 一键申请 Let's Encrypt IP 短期证书 (6天有效期 + acme.sh 自动续期)
issue_ip_cert() {
    prepare_acme_port80 || return 1
    CERT_HOST="${SERVER_IP}"
    CERT_DIR="/root/cert/ip"
    CERT_FILE="${CERT_DIR}/fullchain.pem"
    KEY_FILE="${CERT_DIR}/privkey.pem"
    mkdir -p "$CERT_DIR" || return 1

    log_info "正在向 Let's Encrypt 申请 IP (${SERVER_IP}) SSL 证书 (shortlived 模式，自动续期)..."
    run_acme --issue \
        -d "${SERVER_IP}" \
        --standalone \
        --server letsencrypt \
        --certificate-profile shortlived \
        --days 6 \
        --httpport 80 \
        --force || { fail "IP 证书签发失败，未使用旧文件继续部署。"; return 1; }

    local reload_cmd="systemctl restart x-ui 2>/dev/null || rc-service x-ui restart 2>/dev/null || true"
    run_acme --installcert --force -d "${SERVER_IP}" \
        --key-file "${KEY_FILE}" \
        --fullchain-file "${CERT_FILE}" \
        --reloadcmd "${reload_cmd}" >/dev/null 2>&1 || {
        fail "IP 证书安装或续期配置失败。"; return 1;
    }
    validate_certificate_pair "$CERT_HOST" "$CERT_FILE" "$KEY_FILE" || return 1
    chmod 644 "$CERT_FILE" || return 1
    chmod 600 "$KEY_FILE" || return 1
    run_acme --upgrade --auto-upgrade >/dev/null 2>&1 || true
    log_info "IP 证书申请成功！存放路径: ${CERT_DIR}"
}

# 6B. 一键申请 Let's Encrypt 域名证书 (90天有效期 + acme.sh 自动续期)
issue_domain_cert() {
    local domain="$1"
    if [[ -z "$domain" ]]; then
        prompt_read "请输入已解析到本机 (${SERVER_IP}) 的域名: " domain
        domain="${domain// /}"
    fi
    if [[ -z "$domain" ]]; then
        log_error "域名不能为空！"
        exit 1
    fi
    valid_domain "$domain" || { fail "域名格式无效。"; return 1; }

    prepare_acme_port80 || return 1
    CERT_HOST="${domain}"
    CERT_DIR="/root/cert/${domain}"
    CERT_FILE="${CERT_DIR}/fullchain.pem"
    KEY_FILE="${CERT_DIR}/privkey.pem"
    mkdir -p "$CERT_DIR" || return 1

    log_info "正在向 Let's Encrypt 申请域名 (${domain}) SSL 证书 (90天有效期，自动续期)..."
    run_acme --issue \
        -d "${domain}" \
        --standalone \
        --server letsencrypt \
        --httpport 80 \
        --force || { fail "域名证书签发失败，未使用旧文件继续部署。"; return 1; }

    local reload_cmd="systemctl restart x-ui 2>/dev/null || rc-service x-ui restart 2>/dev/null || true"
    run_acme --installcert --force -d "${domain}" \
        --key-file "${KEY_FILE}" \
        --fullchain-file "${CERT_FILE}" \
        --reloadcmd "${reload_cmd}" >/dev/null 2>&1 || {
        fail "域名证书安装或续期配置失败。"; return 1;
    }
    validate_certificate_pair "$CERT_HOST" "$CERT_FILE" "$KEY_FILE" || return 1
    chmod 644 "$CERT_FILE" || return 1
    chmod 600 "$KEY_FILE" || return 1
    run_acme --upgrade --auto-upgrade >/dev/null 2>&1 || true
    log_info "域名证书申请成功！存放路径: ${CERT_DIR}"
}

# 7. 后期平滑切换：从 IP 证书一键升级/切换为域名证书（保留原节点 UUID / 密钥 / 端口）
domain_recovery_exit() {
    local result=$? name
    trap - EXIT
    if [[ "$result" -ne 0 && "${domain_mutated:-0}" == 1 ]]; then
        log_warn "域名切换未完成，正在恢复本次修改的证书配置。"
        if sqlite3 -bail "$XUI_DB_PATH" <"$domain_rollback_sql"; then
            for name in 3x-ui-node-profiles.json 3x-ui-nodes-info.txt 3x-ui-node-links.txt 3x-ui-xhttp-info.txt; do
                if [[ -f "$domain_state/files/$name" ]]; then
                    install -m 600 "$domain_state/files/$name" "$XUI_INFO_DIR/$name" || true
                elif [[ -f "$domain_state/files/$name.absent" ]]; then
                    rm -f -- "$XUI_INFO_DIR/$name"
                fi
            done
            restart_panel || log_warn "配置已恢复，但服务重启失败，请手动检查 x-ui。"
            log_warn "已恢复本次配置；完整数据库备份：$backup"
        else
            log_warn "恢复失败，请保留数据库备份并检查：$backup；恢复 SQL：$domain_rollback_sql"
        fi
    fi
    return "$result"
}

switch_existing_to_domain() (
    # 子 shell 的退出陷阱覆盖证书 CLI、SQLite、重启、运行检查和结果文件写入失败。
    # 回滚按行比较本次写入值，不覆盖其他入站或并发更改。
    set -euo pipefail
    local target_domain="$1" profile_file="$XUI_INFO_DIR/3x-ui-node-profiles.json"
    [[ -f "$XUI_DB_PATH" ]] || { fail "未找到 3x-ui 数据库，无法切换域名。"; return 1; }
    install -d -m 700 "$XUI_INFO_DIR" || return 1
    install_deps || return 1
    detect_server_ip || return 1
    local backup domain_state domain_rollback_sql domain_mutated=0 name
    backup="$XUI_INFO_DIR/3x-ui-before-domain-$(date -u +%Y%m%dT%H%M%SZ).db"
    domain_state="$RUN_DIR/domain-recovery"
    domain_rollback_sql="$domain_state/rollback.sql"
    mkdir -p "$domain_state/files" || return 1
    sqlite3 "$XUI_DB_PATH" ".backup '$backup'" || return 1
    chmod 600 "$backup" || return 1
    printf 'BEGIN IMMEDIATE;\n' >"$domain_rollback_sql"
    for name in 3x-ui-node-profiles.json 3x-ui-nodes-info.txt 3x-ui-node-links.txt 3x-ui-xhttp-info.txt; do
        if [[ -f "$XUI_INFO_DIR/$name" ]]; then
            cp "$XUI_INFO_DIR/$name" "$domain_state/files/$name" || return 1
        else
            : >"$domain_state/files/$name.absent"
        fi
    done
    log_info "已备份面板数据库：$backup"
    issue_domain_cert "$target_domain" || return 1
    valid_domain "$CERT_HOST" || { fail "新证书域名无效。"; return 1; }
    local id tag raw_stream new_stream changes settings_exist expected existing
    # 面板证书路径只含已验证域名；入站 JSON 使用 SQLite quote() 生成 SQL。
    local cert_sql="${CERT_FILE//\'/\'\'}" key_sql="${KEY_FILE//\'/\'\'}"
    settings_exist=$(sqlite3 "$XUI_DB_PATH" "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='settings';") || return 1
    if [[ "$settings_exist" == 1 ]]; then
        for name in webCertFile webKeyFile; do
            if [[ "$name" == webCertFile ]]; then expected="$cert_sql"; else expected="$key_sql"; fi
            existing=$(sqlite3 "$XUI_DB_PATH" "SELECT count(*) FROM settings WHERE key='$name';") || return 1
            if [[ "$existing" == 0 ]]; then
                printf "DELETE FROM settings WHERE key='%s' AND value='%s';\n" "$name" "$expected" >>"$domain_rollback_sql"
            else
                sqlite3 "$XUI_DB_PATH" "SELECT 'UPDATE settings SET value=' || quote(value) || ' WHERE key=' || quote(key) || ' AND value=' || quote('$expected') || ';' FROM settings WHERE key='$name';" >>"$domain_rollback_sql" || return 1
            fi
        done
    fi
    : >"$domain_state/updates.sql"
    local ids=()
    if [[ -s "$profile_file" ]]; then
        while IFS= read -r tag; do
            [[ "$tag" =~ ^[A-Za-z0-9_-]+$ ]] || { fail "节点标签无效，数据库备份：$backup"; return 1; }
            id=$(sqlite3 "$XUI_DB_PATH" "SELECT id FROM inbounds WHERE tag='$tag' AND json_extract(stream_settings,'$.security')='tls';") || return 1
            [[ "$id" =~ ^[0-9]+$ ]] || { fail "未找到唯一 TLS 节点 $tag；数据库备份：$backup"; return 1; }
            ids+=("$id")
        done < <(jq -er '.[] | select(.security == "tls") | .payload.tag' "$profile_file")
        [[ ${#ids[@]} -gt 0 ]] || { fail "未找到需要更新的 TLS 节点。"; return 1; }
    else
        id=$(sqlite3 "$XUI_DB_PATH" "SELECT id FROM inbounds WHERE protocol='vless' AND remark='VLESS-XHTTP-Enc-Vision' AND json_extract(stream_settings,'$.security')='tls';") || return 1
        [[ "$id" =~ ^[0-9]+$ ]] || { fail "未找到唯一的旧版 XHTTP 入站；数据库备份：$backup"; return 1; }
        ids+=("$id")
    fi
    for id in "${ids[@]}"; do
        raw_stream=$(sqlite3 "$XUI_DB_PATH" "SELECT quote(stream_settings) FROM inbounds WHERE id=$id;") || return 1
        new_stream=$(sqlite3 "$XUI_DB_PATH" "SELECT quote(json_set(stream_settings, '$.tlsSettings.serverName','$CERT_HOST', '$.tlsSettings.certificates[0].certificateFile','$cert_sql', '$.tlsSettings.certificates[0].keyFile','$key_sql')) FROM inbounds WHERE id=$id;") || return 1
        printf 'UPDATE inbounds SET stream_settings=%s WHERE id=%s AND stream_settings=%s;\n' "$raw_stream" "$id" "$new_stream" >>"$domain_rollback_sql"
        printf 'UPDATE inbounds SET stream_settings=%s WHERE id=%s AND stream_settings=%s;\nSELECT changes();\n' "$new_stream" "$id" "$raw_stream" >>"$domain_state/updates.sql"
    done
    printf 'COMMIT;\n' >>"$domain_rollback_sql"
    chmod 600 "$domain_rollback_sql" "$domain_state/updates.sql"
    trap domain_recovery_exit EXIT
    domain_mutated=1
    "${XUI_INSTALL_DIR}/x-ui" cert -webCert "$CERT_FILE" -webCertKey "$KEY_FILE" >/dev/null || return 1
    changes=$(sqlite3 -bail "$XUI_DB_PATH" <"$domain_state/updates.sql") || return 1
    [[ $(printf '%s\n' "$changes" | grep -c '^1$') -eq ${#ids[@]} ]] || { fail "部分入站在更新时被修改，已取消域名切换。"; return 1; }
    restart_panel || { fail "面板重启失败。数据库备份：$backup"; return 1; }

    if [[ -s "$profile_file" ]]; then
        NODE_PROFILES=$(jq -c --arg address "$CERT_HOST" --arg cert "$CERT_FILE" --arg key "$KEY_FILE" '
            map(.clientOutbound.settings.address = $address |
                if .security == "tls" then
                    .clientOutbound.streamSettings.tlsSettings.serverName = $address |
                    .payload.streamSettings |= (fromjson |
                        .tlsSettings.serverName = $address |
                        .tlsSettings.certificates[0].certificateFile = $cert |
                        .tlsSettings.certificates[0].keyFile = $key | tojson)
                else . end)
        ' "$profile_file") || return 1
        local profile link updated='[]'
        while IFS= read -r profile; do
            link=$(profile_share_link "$profile") || return 1
            printf '%s\n' "$updated" >"$domain_state/old.json"
            printf '%s\n' "$profile" >"$domain_state/item.json"
            printf '%s' "$link" >"$domain_state/link.txt"
            updated=$(jq -cn --slurpfile old "$domain_state/old.json" --slurpfile item "$domain_state/item.json" --rawfile link "$domain_state/link.txt" '$old[0] + [$item[0] + {link:$link}]') || return 1
        done < <(jq -c '.[]' <<<"$NODE_PROFILES")
        NODE_PROFILES="$updated"
        printf '%s\n' "$NODE_PROFILES" >"$RUN_DIR/profiles.json"
        verify_running_nodes || return 1
        # 从已保存的说明恢复面板信息；不要执行磁盘上的 shell 环境文件。
        PANEL_USER="${XUI_USERNAME:-$(sed -n 's/^管理员账号：//p' "$XUI_INFO_DIR/3x-ui-nodes-info.txt" | head -n 1)}"
        PANEL_PASS="${XUI_PASSWORD:-$(sed -n 's/^管理员密码：//p' "$XUI_INFO_DIR/3x-ui-nodes-info.txt" | head -n 1)}"
        PANEL_PORT="${XUI_PANEL_PORT:-$(sed -nE 's#^访问地址：https?://[^:]+:([0-9]+)/.*#\1#p' "$XUI_INFO_DIR/3x-ui-nodes-info.txt" | head -n 1)}"
        WEB_BASE_PATH="${XUI_WEB_BASE_PATH:-$(sed -nE 's#^访问地址：https?://[^:]+:[0-9]+/([^/]+)/.*#\1#p' "$XUI_INFO_DIR/3x-ui-nodes-info.txt" | head -n 1)}"
        WEB_BASE_PATH="${WEB_BASE_PATH#/}"; WEB_BASE_PATH="${WEB_BASE_PATH%/}"
        valid_port "$PANEL_PORT" && [[ "$WEB_BASE_PATH" =~ ^[A-Za-z0-9_-]+$ ]] || { fail "无法恢复面板地址信息，已取消域名切换。"; return 1; }
        RAW_PORT=$(jq -r '.[] | select(.key == "raw") | .port' <<<"$NODE_PROFILES")
        REALITY_PORT=$(jq -r '.[] | select(.key == "reality") | .port' <<<"$NODE_PROFILES")
        HY2_PORT=$(jq -r '.[] | select(.key == "hy2") | .port' <<<"$NODE_PROFILES")
        XHTTP_PORT=$(jq -r '.[] | select(.key == "xhttp") | .port' <<<"$NODE_PROFILES")
        write_node_files || return 1
        log_info "全部节点与链接已切换到 $CERT_HOST；原 UUID 和认证凭据已保留。"
        cat "$XUI_INFO_DIR/3x-ui-node-links.txt"
    else
        local legacy="$XUI_INFO_DIR/3x-ui-xhttp-info.txt"
        if [[ -f "$legacy" ]]; then
            sed -i "s#${SERVER_IP}#${CERT_HOST}#g; s#/root/cert/ip/#${CERT_DIR}/#g" "$legacy" || return 1
        fi
        log_info "旧版 XHTTP 节点已切换域名；请在面板确认新证书并更新客户端连接地址。"
    fi
    domain_mutated=0
    log_info "数据库备份：$backup"
)

# 8. 配置参数初始化（支持环境变量覆盖或交互式回车默认）
init_config() {
    echo -e "\n${CYAN}3x-ui 四节点部署：Hy2 / RAW TCP / REALITY / XHTTP${PLAIN}"
    RAW_PORT="${RAW_PORT:-443}"
    REALITY_PORT="${REALITY_PORT:-24443}"
    HY2_PORT="${HY2_PORT:-24444}"
    XHTTP_PORT="${XHTTP_PORT:-${INBOUND_PORT:-8443}}"
    PRIMARY_NODE="${PRIMARY_NODE:-hy2}"
    REALITY_SERVER_NAME="${REALITY_SERVER_NAME:-www.apple.com}"
    REALITY_TARGET="${REALITY_TARGET:-${REALITY_SERVER_NAME}:443}"
    CERT_MODE="${CERT_MODE:-1}"
    DOMAIN_NAME="${XUI_DOMAIN:-}"
    PANEL_USER="${XUI_USERNAME:-admin}"
    PANEL_PASS="${XUI_PASSWORD:-$(gen_random_str 24)}"
    PANEL_PORT="${XUI_PANEL_PORT:-$(random_panel_port)}"
    WEB_BASE_PATH="${XUI_WEB_BASE_PATH:-$(gen_random_str 16)}"
    XHTTP_PATH="${XHTTP_PATH:-/$(gen_random_str 10)}"
    VLESS_ENC_TYPE="${VLESS_ENC_TYPE:-1}"
    INBOUND_TLS_MODE="${INBOUND_TLS_MODE:-1}"
    if [[ "${AUTO_YES:-0}" == "1" ]]; then
        log_info "无人值守模式：主用 ${PRIMARY_NODE}；仍会生成全部四种节点。"
    else
        local answer=""
        echo "所有方案都会生成。请选择默认主用节点："
        echo "  1) Hy2：本轮测试最快，需网络支持 UDP（默认）"
        echo "  2) RAW TCP + TLS + Vision：直连 TCP 备用"
        echo "  3) VLESS + REALITY + Vision：TCP 备用"
        echo "  4) XHTTP + VLESS Encryption + Vision：保留兼容/对照"
        prompt_read "主用选择 [回车保留 ${PRIMARY_NODE}]: " answer
        case "$answer" in
            1|hy2) PRIMARY_NODE=hy2 ;; 2|raw) PRIMARY_NODE=raw ;;
            3|reality) PRIMARY_NODE=reality ;; 4|xhttp) PRIMARY_NODE=xhttp ;;
            "") ;; *) fail "主用选择无效：请输入 1–4。"; return 1 ;;
        esac
        echo "请选择证书：1) IP 短期证书、自动续期；2) 已解析到本机的域名证书。"
        prompt_read "证书类型 [默认 ${CERT_MODE}]: " answer
        CERT_MODE="${answer:-$CERT_MODE}"
        if [[ "$CERT_MODE" == "2" ]]; then
            prompt_read "域名 [${DOMAIN_NAME:-尚未设置}]: " answer
            DOMAIN_NAME="${answer:-$DOMAIN_NAME}"
        fi
        prompt_read "面板账号 [${PANEL_USER}]: " answer; PANEL_USER="${answer:-$PANEL_USER}"
        prompt_read "面板密码 [回车使用随机密码，完成后显示]: " answer; PANEL_PASS="${answer:-$PANEL_PASS}"
        prompt_read "面板 TCP 端口 [${PANEL_PORT}]: " answer; PANEL_PORT="${answer:-$PANEL_PORT}"
        prompt_read "面板安全路径 [${WEB_BASE_PATH}]: " answer; WEB_BASE_PATH="${answer:-$WEB_BASE_PATH}"
        prompt_read "Hy2 UDP 端口 [${HY2_PORT}]: " answer; HY2_PORT="${answer:-$HY2_PORT}"
        prompt_read "RAW TCP 端口 [${RAW_PORT}]: " answer; RAW_PORT="${answer:-$RAW_PORT}"
        prompt_read "REALITY TCP 端口 [${REALITY_PORT}]: " answer; REALITY_PORT="${answer:-$REALITY_PORT}"
        prompt_read "XHTTP TCP 端口 [${XHTTP_PORT}]: " answer; XHTTP_PORT="${answer:-$XHTTP_PORT}"
        prompt_read "XHTTP 路径 [${XHTTP_PATH}]: " answer; XHTTP_PATH="${answer:-$XHTTP_PATH}"
        prompt_read "XHTTP 认证：1) X25519；2) ML-KEM-768 [${VLESS_ENC_TYPE}]: " answer
        VLESS_ENC_TYPE="${answer:-$VLESS_ENC_TYPE}"
        prompt_read "XHTTP 外层 TLS：1) 启用；2) 仅 VLESS Encryption [${INBOUND_TLS_MODE}]: " answer
        INBOUND_TLS_MODE="${answer:-$INBOUND_TLS_MODE}"
        prompt_read "REALITY 握手域名 [${REALITY_SERVER_NAME}]: " answer
        if [[ -n "$answer" ]]; then REALITY_SERVER_NAME="$answer"; REALITY_TARGET="${answer}:443"; fi
    fi
    WEB_BASE_PATH="${WEB_BASE_PATH#/}"; WEB_BASE_PATH="${WEB_BASE_PATH%/}"
    [[ "$XHTTP_PATH" == /* ]] || XHTTP_PATH="/${XHTTP_PATH}"
    validate_config
}

# 9. 安装并配置 MHSanaei/3x-ui
install_and_config_3xui() {
    log_info "正在下载并安装最新版 MHSanaei/3x-ui..."
    export XUI_NONINTERACTIVE=1
    export XUI_USERNAME="${PANEL_USER}"
    export XUI_PASSWORD="${PANEL_PASS}"
    export XUI_PANEL_PORT="${PANEL_PORT}"
    export XUI_WEB_BASE_PATH="${WEB_BASE_PATH}"
    export XUI_DB_TYPE="sqlite"
    export XUI_SSL_MODE="none"

    if [[ -n "${XUI_UPSTREAM_INSTALLER:-}" ]]; then
        [[ -s "$XUI_UPSTREAM_INSTALLER" && -f "$XUI_UPSTREAM_INSTALLER" ]] || {
            fail "预下载的上游安装组件不可用。"; return 1;
        }
        cp -- "$XUI_UPSTREAM_INSTALLER" "$RUN_DIR/upstream-install.sh"
    else
        curl -fsSL --connect-timeout 10 --max-time 90 \
            https://raw.githubusercontent.com/MHSanaei/3x-ui/main/install.sh \
            -o "$RUN_DIR/upstream-install.sh"
    fi
    bash -n "$RUN_DIR/upstream-install.sh"
    # Upstream XUI_SERVICE means the unit directory; ours is a service name.
    env -u XUI_SERVICE bash "$RUN_DIR/upstream-install.sh" </dev/null

    # 确保账号、密码、端口、路径与证书写入面板配置
    log_info "正在配置 3x-ui 面板参数与 SSL 证书..."
    /usr/local/x-ui/x-ui setting \
        -username "${PANEL_USER}" \
        -password "${PANEL_PASS}" \
        -port "${PANEL_PORT}" \
        -webBasePath "${WEB_BASE_PATH}" >/dev/null 2>&1

    /usr/local/x-ui/x-ui cert \
        -webCert "${CERT_FILE}" \
        -webCertKey "${KEY_FILE}" >/dev/null 2>&1

    restart_panel
    sleep 3
}

# 10. 生成 VLESS Encryption (vlessenc) 密钥对
generate_vlessenc_keys() {
    resolve_xray_binary
    log_info "正在调用 Xray 核心生成 VLESS Encryption (vlessenc) 密钥..."
    local vlessenc_out
    vlessenc_out=$("$XRAY_BIN" vlessenc)

    # 提取第 1 组 (X25519) 与第 2 组 (ML-KEM-768) 的 decryption / encryption
    local dec_list enc_list
    mapfile -t dec_list < <(echo "$vlessenc_out" | grep '"decryption"' | awk -F'"' '{print $4}')
    mapfile -t enc_list < <(echo "$vlessenc_out" | grep '"encryption"' | awk -F'"' '{print $4}')

    if [[ "${#dec_list[@]}" -eq 0 || "${#enc_list[@]}" -eq 0 ]]; then
        log_error "当前 Xray 版本未能生成 vlessenc 密钥，请确认 Xray 版本 >= 25.8！"
        exit 1
    fi

    if [[ "$VLESS_ENC_TYPE" == "2" && "${#dec_list[@]}" -ge 2 ]]; then
        VLESS_DECRYPTION="${dec_list[1]}"
        VLESS_ENCRYPTION="${enc_list[1]}"
        VLESS_AUTH_LABEL="ML-KEM-768, Post-Quantum"
    else
        VLESS_DECRYPTION="${dec_list[0]}"
        VLESS_ENCRYPTION="${enc_list[0]}"
        VLESS_AUTH_LABEL="X25519, not Post-Quantum"
    fi

    log_info "已生成 VLESS Encryption (${VLESS_AUTH_LABEL}) 密钥对"
}

generate_reality_keys() {
    local output
    output=$("$XRAY_BIN" x25519) || { fail "Xray 无法生成 REALITY 密钥。"; return 1; }
    REALITY_PRIVATE_KEY=$(sed -nE 's/^[[:space:]]*PrivateKey:[[:space:]]*([^[:space:]]+).*/\1/p' <<<"$output" | head -n 1)
    REALITY_PUBLIC_KEY=$(sed -nE 's/^[[:space:]]*(Password \(PublicKey\)|PublicKey):[[:space:]]*([^[:space:]]+).*/\2/p' <<<"$output" | head -n 1)
    REALITY_SHORT_ID=$(openssl rand -hex 8)
    [[ "$REALITY_PRIVATE_KEY" =~ ^[A-Za-z0-9_-]{43}$ && "$REALITY_PUBLIC_KEY" =~ ^[A-Za-z0-9_-]{43}$ ]] || {
        fail "无法识别 Xray 输出的 REALITY 密钥格式。"; return 1;
    }
}

verify_reality_target() {
    local output
    if ! output=$(timeout 12 openssl s_client -connect "$REALITY_TARGET" -servername "$REALITY_SERVER_NAME" \
        -tls1_3 -groups X25519 -verify_return_error -verify_hostname "$REALITY_SERVER_NAME" \
        -alpn h2 </dev/null 2>&1); then
        fail "REALITY 目标握手或证书名称校验失败；请检查可达性、证书及目标 SNI。"
        return 1
    fi
    if ! grep -Eiq '(Protocol([[:space:]]+version)?[[:space:]]*:[[:space:]]*|New,[[:space:]]*)TLSv1\.3' <<<"$output"; then
        fail "REALITY 目标未协商 TLS 1.3；请选择支持 TLS 1.3 的目标站点。"
        return 1
    fi
    if ! grep -Eiq 'ALPN protocol:[[:space:]]*h2[[:space:]]*$' <<<"$output"; then
        fail "REALITY 目标未协商 HTTP/2（h2）；请选择支持 h2 的目标站点。"
        return 1
    fi
    case "${REALITY_SERVER_NAME,,}" in
        microsoft.com|*.microsoft.com)
            log_warn "微软目标曾触发旧版 REALITY 握手记录长度问题；请核对 Xray 版本并在手机端实测。" ;;
    esac
    log_info "目标已通过 TLS 1.3、证书名称及 h2 基础检查；此检查不能保证小火箭等客户端兼容。"
}

# 11. 一次生成四种节点；主用选择只调整展示顺序，不删除备用节点。
profile_share_link() {
    jq -er '
        def encoded: @uri;
        . as $profile | .clientOutbound as $out |
        $out.settings as $settings | $out.streamSettings as $stream |
        ($settings.address | if contains(":") and (startswith("[") | not)
                              then "[" + . + "]" else . end) as $address |
        ($settings.port | tostring) as $port |
        ($profile.remark | encoded) as $remark |
        if $out.protocol == "hysteria" then
            "hysteria2://" + ($stream.hysteriaSettings.auth | encoded) + "@" + $address + ":" + $port
            + "/?sni=" + ($stream.tlsSettings.serverName | encoded)
            + "&alpn=h3&insecure=0#" + $remark
        elif $out.protocol == "vless" then
            "vless://" + $settings.id + "@" + $address + ":" + $port
            + "?type=" + ($stream.network | encoded)
            + "&encryption=" + ($settings.encryption | encoded)
            + "&security=" + ($stream.security | encoded)
            + "&flow=" + ($settings.flow | encoded)
            + (if $stream.network == "xhttp" then
                 "&path=" + ($stream.xhttpSettings.path | encoded)
                 + "&mode=" + ($stream.xhttpSettings.mode | encoded) else "" end)
            + (if $stream.security == "tls" then
                 "&fp=" + ($stream.tlsSettings.fingerprint | encoded)
                 + "&alpn=" + ($stream.tlsSettings.alpn | join(",") | encoded)
                 + "&sni=" + ($stream.tlsSettings.serverName | encoded)
               elif $stream.security == "reality" then
                 "&fp=" + ($stream.realitySettings.fingerprint | encoded)
                 + "&sni=" + ($stream.realitySettings.serverName | encoded)
                 + "&pbk=" + ($stream.realitySettings.password | encoded)
                 + "&sid=" + ($stream.realitySettings.shortId | encoded)
                 + "&spx=" + ($stream.realitySettings.spiderX | encoded)
               else "" end)
            + "#" + $remark
        else error("unsupported generated profile protocol") end
    ' <<<"$1"
}

prepare_node_profiles() {
    local raw_uuid reality_uuid xhttp_uuid hy2_auth nonce
    raw_uuid=$(gen_uuid)
    reality_uuid=$(gen_uuid)
    xhttp_uuid=$(gen_uuid)
    hy2_auth=$(gen_random_str 32)
    nonce=$(gen_random_str 16)

    NODE_PROFILES=$(
        printf '%s\n' "$raw_uuid" "$reality_uuid" "$xhttp_uuid" "$hy2_auth" "$nonce" \
            "$VLESS_DECRYPTION" "$VLESS_ENCRYPTION" "$REALITY_PRIVATE_KEY" \
            "$REALITY_PUBLIC_KEY" "$REALITY_SHORT_ID" |
        jq -Rn \
        --arg address "$SERVER_IP" \
        --arg sni "$CERT_HOST" \
        --arg cert "$CERT_FILE" \
        --arg cert_key "$KEY_FILE" \
        --argjson raw_port "$RAW_PORT" \
        --argjson reality_port "$REALITY_PORT" \
        --argjson hy2_port "$HY2_PORT" \
        --argjson xhttp_port "$XHTTP_PORT" \
        --arg path "$XHTTP_PATH" \
        --arg auth_label "$VLESS_AUTH_LABEL" \
        --arg reality_sni "$REALITY_SERVER_NAME" \
        --arg reality_target "$REALITY_TARGET" \
        --arg xhttp_tls_mode "${INBOUND_TLS_MODE:-1}" \
        --arg primary "${PRIMARY_NODE:-hy2}" '
        input as $raw_uuid | input as $reality_uuid | input as $xhttp_uuid |
        input as $hy2_auth | input as $nonce | input as $dec | input as $enc |
        input as $reality_private | input as $reality_public | input as $reality_sid |
        def client_common($key): {
            email: ("3xuish_" + $key + "_" + $nonce),
            limitIp: 0, totalGB: 0, expiryTime: 0, enable: true,
            tgId: 0, subId: ($nonce + "_" + $key), comment: "", reset: 0
        };
        def vless_settings($key; $uuid; $decryption; $encryption): {
            clients: [client_common($key) + {id: $uuid, flow: "xtls-rprx-vision"}],
            decryption: $decryption, encryption: $encryption
        };
        def tls_server($alpn): {
            serverName: $sni, minVersion: "1.0", maxVersion: "1.3",
            rejectUnknownSni: false, alpn: $alpn,
            certificates: [{certificateFile: $cert, keyFile: $cert_key,
                            oneTimeLoading: false, usage: "encipherment"}],
            settings: {fingerprint: "chrome"}
        };
        def tls_client($alpn): {
            serverName: $sni, minVersion: "1.0", maxVersion: "1.3",
            fingerprint: "chrome", alpn: $alpn
        };
        def tcp_options: {tcpWindowClamp: 0};
        def tcp_tls_stream: {
            network: "tcp", security: "tls", tcpSettings: {header: {type: "none"}},
            tlsSettings: tls_server(["h2", "http/1.1"]), sockopt: tcp_options
        };
        def xhttp_options: {path: $path, host: "", mode: "auto"};
        def xhttp_stream:
            {network: "xhttp", security: (if $xhttp_tls_mode == "2" then "none" else "tls" end),
             xhttpSettings: xhttp_options, sockopt: tcp_options}
            + (if $xhttp_tls_mode == "2" then {} else
                   {tlsSettings: tls_server(["h2", "http/1.1"])} end);
        def payload($remark; $port; $protocol; $settings; $stream): {
            up: 0, down: 0, total: 0, remark: $remark, enable: true,
            tag: ("3xuish-" + $nonce + "-" + $remark),
            expiryTime: 0, listen: "", port: $port, protocol: $protocol,
            settings: ($settings | tojson), streamSettings: ($stream | tojson),
            sniffing: ({enabled: false, destOverride: ["http", "tls", "quic"],
                        metadataOnly: false, routeOnly: true} | tojson)
        };
        def vless_client($uuid; $port; $encryption; $stream): {
            protocol: "vless",
            settings: {address: $address, port: $port, id: $uuid,
                       encryption: $encryption, flow: "xtls-rprx-vision"},
            streamSettings: $stream
        };
        def profile($key; $display_label; $rank; $port; $transport; $security; $remark; $payload; $link; $outbound; $credential): {
            key: $key, label: $display_label, rank: $rank, primary: ($key == $primary),
            port: $port, transport: $transport, security: $security, remark: $remark,
            payload: $payload, link: $link, clientOutbound: ($outbound + {tag: ("3xuish-" + $key)}),
            credential: $credential
        };
        [
            profile("hy2"; "Hy2（Hysteria2）"; 0; $hy2_port; "hysteria"; "tls"; "3xuish-Hy2";
                payload("3xuish-Hy2"; $hy2_port; "hysteria";
                    {version: 2, clients: [client_common("hy2") + {auth: $hy2_auth}]};
                    {network: "hysteria", security: "tls", hysteriaSettings: {version: 2},
                     tlsSettings: tls_server(["h3"]),
                     finalmask: {quicParams: {congestion: "bbr", bbrProfile: "standard"}}});
                null;
                {protocol: "hysteria", settings: {version: 2, address: $address, port: $hy2_port},
                 streamSettings: {network: "hysteria", security: "tls",
                    hysteriaSettings: {version: 2, auth: $hy2_auth},
                    tlsSettings: tls_client(["h3"]),
                    finalmask: {quicParams: {congestion: "bbr", bbrProfile: "standard"}}}};
                $hy2_auth),
            profile("raw"; "RAW TCP + TLS + Vision"; 1; $raw_port; "tcp"; "tls"; "3xuish-RAW-TLS-Vision";
                payload("3xuish-RAW-TLS-Vision"; $raw_port; "vless";
                    vless_settings("raw"; $raw_uuid; "none"; "none"); tcp_tls_stream);
                null;
                vless_client($raw_uuid; $raw_port; "none";
                    {network: "tcp", security: "tls", tcpSettings: {header: {type: "none"}},
                     tlsSettings: tls_client(["h2", "http/1.1"])});
                $raw_uuid),
            profile("reality"; "VLESS + REALITY + Vision"; 2; $reality_port; "tcp"; "reality"; "3xuish-REALITY-Vision";
                payload("3xuish-REALITY-Vision"; $reality_port; "vless";
                    vless_settings("reality"; $reality_uuid; "none"; "none");
                    {network: "tcp", security: "reality", tcpSettings: {header: {type: "none"}},
                     sockopt: tcp_options,
                     realitySettings: {show: false, target: $reality_target, xver: 0,
                        serverNames: [$reality_sni], privateKey: $reality_private, shortIds: [$reality_sid],
                        settings: {publicKey: $reality_public, fingerprint: "chrome",
                                   serverName: $reality_sni, spiderX: "/"}}});
                null;
                vless_client($reality_uuid; $reality_port; "none";
                    {network: "tcp", security: "reality", tcpSettings: {header: {type: "none"}},
                     realitySettings: {serverName: $reality_sni, fingerprint: "chrome",
                                       password: $reality_public, shortId: $reality_sid, spiderX: "/"}});
                $reality_uuid),
            profile("xhttp"; "XHTTP + VLESS Encryption + Vision"; 3; $xhttp_port; "xhttp";
                (if $xhttp_tls_mode == "2" then "none" else "tls" end); "3xuish-XHTTP-Enc-Vision";
                payload("3xuish-XHTTP-Enc-Vision"; $xhttp_port; "vless";
                    (vless_settings("xhttp"; $xhttp_uuid; $dec; $enc) + {selectedAuth: $auth_label});
                    xhttp_stream);
                null;
                vless_client($xhttp_uuid; $xhttp_port; $enc;
                    {network: "xhttp", security: (if $xhttp_tls_mode == "2" then "none" else "tls" end),
                     xhttpSettings: xhttp_options}
                    + (if $xhttp_tls_mode == "2" then {} else
                          {tlsSettings: tls_client(["h2", "http/1.1"])} end));
                $xhttp_uuid)
        ] | sort_by(if .primary then -1 else .rank end)
    ')

    local profile link rebuilt="[]"
    while IFS= read -r profile; do
        link=$(profile_share_link "$profile")
        profile=$(printf '%s\n%s\n' "$profile" "$link" | jq -Rn 'input | fromjson | .link = input')
        rebuilt=$(printf '%s\n%s\n' "$rebuilt" "$profile" | jq -sc '.[0] + [.[1]]')
    done < <(jq -c '.[]' <<<"$NODE_PROFILES")
    NODE_PROFILES="$rebuilt"
}

# 12. 面板认证与请求；Bearer 使用安装器专用管理员令牌。
api_request() {
    local method="$1" path="$2" payload="${3:-}" url
    url="$API_BASE_URL$path"
    if [[ "$method" == POST ]]; then
        printf '%s' "$payload" | curl "${API_COMMON[@]}" "${API_AUTH[@]}" \
            -H 'Content-Type: application/json' -X POST --data-binary @- "$url"
    else
        curl "${API_COMMON[@]}" "${API_AUTH[@]}" "$url"
    fi
}

api_init() {
    API_BASE_URL="${PANEL_API_SCHEME:-https}://${PANEL_API_HOST:-127.0.0.1}:${PANEL_PORT}${WEB_BASE_PATH:+/$WEB_BASE_PATH}"
    # The panel is on this server; never send its credentials through environment proxies.
    API_COMMON=(-fsSk --noproxy '*' --connect-timeout 5 --max-time 30)
    local token_output token csrf login_response
    token_output=$("${XUI_INSTALL_DIR}/x-ui" setting -getApiToken -tokenName 3xuish-installer -tokenScope admin 2>/dev/null) || true
    token=$(printf '%s\n' "$token_output" | sed -nE 's/^[[:space:]]*apiToken:[[:space:]]*([^[:space:]]+).*/\1/p' | head -n 1)
    if [[ -n "$token" ]]; then
        printf 'header = "Authorization: Bearer %s"\n' "$token" >"$RUN_DIR/api-auth.conf"
        chmod 600 "$RUN_DIR/api-auth.conf"
        API_AUTH=(--config "$RUN_DIR/api-auth.conf")
        if api_request GET '/panel/api/inbounds/list' | jq -e '.success == true' >/dev/null; then
            API_READY=1
            log_info "面板 API 认证成功。"
            return 0
        fi
    fi
    local cookie="$RUN_DIR/api-cookies.txt"
    if [[ "${EXISTING_DEPLOYMENT:-0}" == 1 && "${PANEL_CREDENTIALS_AVAILABLE:-0}" != 1 ]]; then
        fail "当前面板不支持管理员令牌，请用 XUI_USERNAME 和 XUI_PASSWORD 指定原面板账号密码后重试。"
        return 1
    fi
    csrf=$(curl "${API_COMMON[@]}" -c "$cookie" "$API_BASE_URL/csrf-token" | jq -er '.obj') || {
        fail "面板令牌和 CSRF 登录均不可用。"; return 1;
    }
    API_AUTH=(--cookie "$cookie" --cookie-jar "$cookie" --header "X-CSRF-Token: $csrf")
    printf '%s' "$PANEL_PASS" >"$RUN_DIR/panel-password.txt"
    login_response=$(curl "${API_COMMON[@]}" "${API_AUTH[@]}" -X POST "$API_BASE_URL/login" \
        --data-urlencode "username=${PANEL_USER}" --data-urlencode "password@$RUN_DIR/panel-password.txt") || {
        fail "面板登录失败。"; return 1;
    }
    jq -e '.success == true' >/dev/null <<<"$login_response" || { fail "面板拒绝管理员登录。"; return 1; }
    api_request GET '/panel/api/inbounds/list' | jq -e '.success == true' >/dev/null || {
        fail "面板登录成功但入站 API 不可用。"; return 1;
    }
    API_READY=1
}

rollback_created_nodes() {
    [[ "$API_READY" == 1 && ${#ATTEMPTED_TAGS[@]} -gt 0 ]] || return 0
    local list tags response index
    tags=$(printf '%s\n' "${ATTEMPTED_TAGS[@]}" | jq -Rsc 'split("\n") | map(select(length > 0))')
    list=$(api_request GET '/panel/api/inbounds/list') || { log_warn "无法读取入站列表，自动回滚未完成。"; return 1; }
    local ids=()
    mapfile -t ids < <(jq -r --argjson tags "$tags" \
        '.obj[] | select(.tag as $tag | $tags | index($tag)) | .id' <<<"$list")
    for ((index=${#ids[@]}-1; index>=0; index--)); do
        response=$(api_request POST "/panel/api/inbounds/del/${ids[index]}" '{}') || {
            log_warn "本次入站 ID=${ids[index]} 删除失败，请登录面板核查。"; continue;
        }
        jq -e '.success == true' >/dev/null <<<"$response" || log_warn "本次入站 ID=${ids[index]} 删除未被面板确认。"
    done
    api_request POST '/panel/api/server/restartXrayService' '{}' >/dev/null || true
}

cleanup_install() {
    local result=$? name
    trap - EXIT
    if [[ "$result" -ne 0 && "$ROLLBACK_NEEDED" == 1 ]]; then
        log_warn "部署未完成，正在回滚本次新建入站。"
        rollback_created_nodes || true
    fi
    if [[ "$result" -ne 0 && -n "${EXISTING_INFO_BACKUP:-}" && -d "$EXISTING_INFO_BACKUP" ]]; then
        for name in 3x-ui-node-profiles.json 3x-ui-nodes-info.txt 3x-ui-node-links.txt 3x-ui-xhttp-info.txt; do
            if [[ -f "$EXISTING_INFO_BACKUP/$name" ]]; then
                install -m 600 "$EXISTING_INFO_BACKUP/$name" "$XUI_INFO_DIR/$name" || true
            elif [[ -f "$EXISTING_INFO_BACKUP/$name.absent" ]]; then
                rm -f -- "$XUI_INFO_DIR/$name"
            fi
        done
    fi
    if [[ -n "$RUN_DIR" && -d "$RUN_DIR" && "$RUN_DIR" == /tmp/3xuish.* ]]; then
        rm -rf -- "$RUN_DIR"
    fi
}

create_inbound_nodes() {
    local profile payload tag response id candidate
    ROLLBACK_NEEDED=1
    printf '%s\n' "$NODE_PROFILES" >"$RUN_DIR/profiles.json"
    while IFS= read -r profile; do
        tag=$(jq -er '.payload.tag' <<<"$profile")
        payload=$(jq -c '.payload' <<<"$profile")
        ATTEMPTED_TAGS+=("$tag")
        response=$(api_request POST '/panel/api/inbounds/add' "$payload") || {
            fail "创建 ${tag} 时面板 API 无响应。"; return 1;
        }
        if ! jq -e '.success == true' >/dev/null <<<"$response"; then
            fail "面板拒绝创建 ${tag}。请检查端口占用、证书路径或面板日志。"; return 1
        fi
        id=$(jq -er '.obj.id // empty' <<<"$response") || {
            fail "面板创建 ${tag} 后未返回入站 ID，正在核对并回滚本次节点。"; return 1;
        }
        CREATED_IDS+=("$id")
        log_info "已创建 $(jq -r '.label' <<<"$profile")，端口 $(jq -r '.port' <<<"$profile")。"
    done < <(jq -c '.[]' <<<"$NODE_PROFILES")

    candidate="$RUN_DIR/xray-candidate.json"
    api_request GET '/panel/api/server/getConfigJson' | jq -er '.obj | objects' >"$candidate" || {
        fail "无法获取面板生成的 Xray 配置。"; return 1;
    }
    chmod 600 "$candidate"
    if ! (cd "$XUI_INSTALL_DIR" && "$XRAY_BIN" run -test -c "$candidate") >"$RUN_DIR/xray-check.log" 2>&1; then
        fail "完整 Xray 配置未通过校验；正在回滚本次节点。"
        return 1
    fi
    log_info "四个节点组成的完整 Xray 配置校验通过。"
    response=$(api_request POST '/panel/api/server/restartXrayService' '{}') || {
        fail "面板未能重启 Xray 核心。"; return 1;
    }
    jq -e '.success == true' >/dev/null <<<"$response" || { fail "面板拒绝重启 Xray 核心。"; return 1; }
    verify_running_nodes || return 1
}

verify_running_nodes() {
    local runtime="${XUI_INSTALL_DIR}/bin/config.json" profile key port listener
    for _ in {1..15}; do
        if [[ -s "$runtime" ]] && jq -e --slurpfile profiles "$RUN_DIR/profiles.json" '
            def certificate_files:
                [(.certificates // [])[] | {certificateFile, keyFile}]
                | sort_by(.certificateFile, .keyFile);
            def reality_identity:
                {target: (.target // .dest), privateKey,
                 serverNames: ((.serverNames // []) | sort),
                 shortIds: ((.shortIds // []) | sort)};
            def xhttp_identity:
                {path: (.path // "/"), mode: (.mode // "auto"), host: (.host // "")};
            . as $config | all($profiles[0][]; . as $profile |
                ($profile.payload.streamSettings | fromjson) as $expected_stream |
                ($profile.payload.settings | fromjson) as $expected_settings |
                any($config.inbounds[]; .tag == $profile.payload.tag and
                    .port == $profile.port and .protocol == $profile.payload.protocol and
                    .streamSettings.network == $profile.transport and
                    .streamSettings.security == $profile.security and
                    (if $profile.key == "hy2" then
                        any(.settings.clients[]; .auth == $profile.credential)
                     else any(.settings.clients[]; .id == $profile.credential and
                             .flow == "xtls-rprx-vision") end) and
                    (if .streamSettings.security == "tls" then
                        .streamSettings.tlsSettings.minVersion == "1.0" and
                        .streamSettings.tlsSettings.maxVersion == "1.3" and
                        .streamSettings.tlsSettings.serverName == $expected_stream.tlsSettings.serverName and
                        (.streamSettings.tlsSettings | certificate_files) ==
                            ($expected_stream.tlsSettings | certificate_files)
                     elif .streamSettings.security == "reality" then
                        (.streamSettings.realitySettings | reality_identity) ==
                            ($expected_stream.realitySettings | reality_identity)
                     else true end) and
                    (if $profile.key == "xhttp" then
                        (.streamSettings.xhttpSettings | xhttp_identity) ==
                            ($expected_stream.xhttpSettings | xhttp_identity)
                     else true end) and
                    (if .protocol == "vless" then
                        (.settings.decryption // "none") == ($expected_settings.decryption // "none")
                     else true end) and
                    (if .protocol == "vless" and .settings.decryption != "none"
                     then (.settings | has("fallbacks") | not) else true end)))
        ' "$runtime" >/dev/null 2>&1; then
            local all_listening=1
            while IFS= read -r profile; do
                key=$(jq -r '.key' <<<"$profile")
                port=$(jq -r '.port' <<<"$profile")
                if [[ "$key" == hy2 ]]; then
                    listener=$(ss -H -lnup "sport = :${port}")
                else
                    listener=$(ss -H -lntp "sport = :${port}")
                fi
                [[ "$listener" == *xray* ]] || { all_listening=0; break; }
            done < <(jq -c '.[]' <<<"$NODE_PROFILES")
            if [[ "$all_listening" == 1 ]]; then
                log_info "Xray 正在监听全部四个节点，TLS 范围 1.0–1.3 已写入配置。"
                return 0
            fi
        fi
        sleep 1
    done
    fail "配置或监听验证失败：请核对面板中的证书、认证与传输是否和已保存节点一致，并检查 x-ui/Xray 日志与端口。"
}

# 13. 组装客户端分享链接并打印安装结果。
write_node_files() {
    local profiles_file="$XUI_INFO_DIR/3x-ui-node-profiles.json"
    local info_file="$XUI_INFO_DIR/3x-ui-nodes-info.txt"
    local links_file="$XUI_INFO_DIR/3x-ui-node-links.txt"
    local legacy_file="$XUI_INFO_DIR/3x-ui-xhttp-info.txt"
    local profile_file_tmp="$RUN_DIR/profiles.json"
    # Callers use `write_node_files || return`, so every write must propagate
    # its own failure instead of relying on Bash errexit inside this function.
    install -d -m 700 "$XUI_INFO_DIR" || return 1
    printf '%s\n' "$NODE_PROFILES" | jq -e 'length == 4' >/dev/null || return 1
    printf '%s\n' "$NODE_PROFILES" >"$profile_file_tmp" || return 1
    install -m 600 "$profile_file_tmp" "$profiles_file" || return 1
    jq -r '.[].link' "$profiles_file" >"$RUN_DIR/links.txt" || return 1
    install -m 600 "$RUN_DIR/links.txt" "$links_file" || return 1
    {
        printf '============================================================\n'
        printf '  3x-ui 四节点部署完成：连接信息与使用说明\n'
        printf '============================================================\n'
        printf '\n[面板]\n'
        printf '访问地址：%s://%s:%s%s/\n' "${PANEL_API_SCHEME:-https}" "$CERT_HOST" "$PANEL_PORT" "${WEB_BASE_PATH:+/$WEB_BASE_PATH}"
        printf '管理员账号：%s\n管理员密码：%s\n' "$PANEL_USER" "$PANEL_PASS"
        printf '证书文件：%s\n私钥文件：%s\n' "$CERT_FILE" "$KEY_FILE"
        printf '\n[节点与导入链接]\n'
        local key label port kind link primary
        while IFS=$'\t' read -r key label port kind primary link; do
            if [[ "$primary" == true ]]; then
                printf '\n【主用】%s  %s/%s\n' "$label" "$kind" "$port"
            else
                printf '\n【备用】%s  %s/%s\n' "$label" "$kind" "$port"
            fi
            printf '%s\n' "$link"
        done < <(jq -r '.[] | [.key, .label, .port, (if .key == "hy2" then "UDP" else "TCP" end), .primary, .link] | @tsv' "$profiles_file")
        printf '\n[使用步骤]\n'
        printf '1. 在云安全组与系统防火墙开放 Hy2 UDP/%s、RAW TCP/%s、REALITY TCP/%s、XHTTP TCP/%s。\n' \
            "$HY2_PORT" "$RAW_PORT" "$REALITY_PORT" "$XHTTP_PORT"
        printf '2. 在 v2rayN 等客户端逐条导入上方链接，先连接标为“主用”的节点；若 UDP 无法连接，试 RAW/REALITY TCP。\n'
        printf '3. 每个新节点有独立认证凭据。修改端口、UUID、密钥或传输后，客户端需要重新导入对应链接。\n'
        printf '4. 所有 TLS 入站的面板范围为最小 1.0、最大 1.3；Hy2 使用 QUIC，实际握手仍为 TLS 1.3。REALITY 使用独立安全设置。\n'
        printf '5. IP 或域名证书由 acme.sh 续期，服务器需长期开放 TCP/80 以完成验证。\n'
        printf '6. 查看链接：cat %s；查看完整信息：cat %s。文件仅 root 可读。\n' "$links_file" "$info_file"
        printf '7. 域名切换：bash <(curl -fsSL %s) --switch-domain 你的域名.example\n' "$REPO_RAW_URL"
        printf '8. 若节点连不上，先检查云安全组、服务器监听和客户端 TLS SNI；Hy2 必须放行 UDP。\n'
        printf '9. 主用选择只调整推荐顺序，不代表任意网络都能跑满运营商标称带宽；建议按实际线路测速。\n'
        printf '\n[常用排查命令]\nss -lntup\nsystemctl status x-ui --no-pager\njournalctl -u x-ui -n 100 --no-pager\n'
        printf 'OpenRC 系统使用 rc-service x-ui status。面板进程存在时仍需核对节点端口是否由 Xray 监听。\n'
        printf '带宽换算：1000Mbps 理论约 125MB/s；1TB 是流量额度。\n'
        printf '\n[保管文件]\n%s\n%s\n%s\n' "$profiles_file" "$links_file" "$info_file"
    } >"$RUN_DIR/info.txt" || return 1
    install -m 600 "$RUN_DIR/info.txt" "$info_file" || return 1
    install -m 600 "$RUN_DIR/info.txt" "$legacy_file" || return 1
}

print_summary() {
    log_info "部署成功：四个节点已创建且 Xray 正在监听。"
    cat "$XUI_INFO_DIR/3x-ui-nodes-info.txt"
}

# 14. 向已有面板增添长期节点，保留原入站、证书与管理员账户。
existing_panel_setting() {
    local key="$1"
    case "$key" in webPort|webBasePath|webListen|webCertFile|webKeyFile) ;;
        *) fail "不支持读取该面板设置。"; return 1 ;; esac
    sqlite3 -readonly "$XUI_DB_PATH" "SELECT value FROM settings WHERE key='$key' LIMIT 1;"
}

existing_node_port_available() {
    local transport="$1" port="$2" count listener
    if [[ "$transport" == udp ]]; then
        count=$(sqlite3 -readonly "$XUI_DB_PATH" "SELECT count(*) FROM inbounds WHERE port=$port AND protocol IN ('hysteria','wireguard','tuic');") || return 1
        listener=$(ss -H -lnu "sport = :${port}") || return 1
    else
        count=$(sqlite3 -readonly "$XUI_DB_PATH" "SELECT count(*) FROM inbounds WHERE port=$port AND protocol NOT IN ('hysteria','wireguard','tuic');") || return 1
        listener=$(ss -H -lnt "sport = :${port}") || return 1
        [[ "$port" != "$PANEL_PORT" && "$port" != 80 ]] || return 1
        [[ "${EXISTING_SELECTED_TCP:-|}" != *"|${port}|"* ]] || return 1
    fi
    [[ "$count" == 0 && -z "$listener" ]]
}

existing_select_node_port() {
    local name="$1" default_port="$2" transport="$3" selected attempt
    selected="${!name:-}"
    if [[ -n "$selected" ]]; then
        valid_port "$selected" || { fail "${name} 必须是 1–65535 的端口。"; return 1; }
        selected=$((10#$selected))
        existing_node_port_available "$transport" "$selected" || {
            fail "指定端口 ${name}=${selected}/${transport} 已占用或与已有入站冲突，请换一个端口。"; return 1;
        }
    else
        selected="$default_port"
        for attempt in {1..100}; do
            if existing_node_port_available "$transport" "$selected"; then break; fi
            selected=$((selected + 1))
        done
        ((attempt <= 100 && selected <= 65535)) && existing_node_port_available "$transport" "$selected" || {
            fail "未找到 ${name} 可用端口，请通过环境变量指定。"; return 1;
        }
    fi
    printf -v "$name" '%s' "$selected"
    if [[ "$transport" == tcp ]]; then EXISTING_SELECTED_TCP+="${selected}|"; fi
}

load_existing_panel_context() {
    local runtime="${XUI_INSTALL_DIR}/bin/config.json" cert_profile panel_listen old_info
    [[ -s "$runtime" ]] || { fail "未找到现有 Xray 配置，先在面板中启动原节点后重试。"; return 1; }
    PANEL_PORT=$(existing_panel_setting webPort)
    valid_port "$PANEL_PORT" || { fail "无法读取现有面板端口 webPort。"; return 1; }
    WEB_BASE_PATH=$(existing_panel_setting webBasePath)
    WEB_BASE_PATH="${WEB_BASE_PATH#/}"; WEB_BASE_PATH="${WEB_BASE_PATH%/}"
    [[ -z "$WEB_BASE_PATH" || "$WEB_BASE_PATH" =~ ^[A-Za-z0-9_-]+$ ]] || {
        fail "现有面板路径格式暂不支持，请在面板中使用字母、数字、下划线或短横线。"; return 1;
    }
    panel_listen=$(existing_panel_setting webListen)
    case "$panel_listen" in ''|0.0.0.0|::|'[::]') PANEL_API_HOST=127.0.0.1 ;;
        *) PANEL_API_HOST="$panel_listen" ;; esac
    if [[ -n "$(existing_panel_setting webCertFile)" && -n "$(existing_panel_setting webKeyFile)" ]]; then
        PANEL_API_SCHEME=https
    else
        PANEL_API_SCHEME=http
    fi
    cert_profile=$(jq -cer '
        [.inbounds[] | select(.streamSettings.security == "tls") |
         select(any(.streamSettings.tlsSettings.certificates[]?;
                    (.certificateFile // "") != "" and (.keyFile // "") != ""))] |
        sort_by(if .port == 443 then 0 else 1 end) | .[0] // empty
    ' "$runtime") || { fail "原节点没有可复用的 TLS 证书，请先在面板配置有效证书。"; return 1; }
    CERT_FILE=$(jq -er '.streamSettings.tlsSettings.certificates[] | select(.certificateFile and .keyFile) | .certificateFile' <<<"$cert_profile" | head -n 1)
    KEY_FILE=$(jq -er '.streamSettings.tlsSettings.certificates[] | select(.certificateFile and .keyFile) | .keyFile' <<<"$cert_profile" | head -n 1)
    CERT_HOST="${XUI_DOMAIN:-$(jq -r '.streamSettings.tlsSettings.serverName // ""' <<<"$cert_profile")}"
    CERT_HOST="${CERT_HOST:-$SERVER_IP}"
    validate_certificate_pair "$CERT_HOST" "$CERT_FILE" "$KEY_FILE" || {
        fail "现有 TLS 证书校验失败；请先核对证书与私钥，域名不匹配时用 XUI_DOMAIN 指定证书覆盖的域名后重试。"
        return 1
    }
    CERT_DIR="${CERT_FILE%/*}"
    PANEL_USER="${XUI_USERNAME:-}"
    PANEL_PASS="${XUI_PASSWORD:-}"
    PANEL_CREDENTIALS_AVAILABLE=0
    for old_info in "$XUI_INFO_DIR/3x-ui-nodes-info.txt" "$XUI_INFO_DIR/3x-ui-xhttp-info.txt"; do
        [[ -f "$old_info" ]] || continue
        if [[ -z "$PANEL_USER" ]]; then PANEL_USER=$(sed -nE 's/^[[:space:]]*管理员账号[：:][[:space:]]*//p' "$old_info" | head -n 1); fi
        if [[ -z "$PANEL_PASS" ]]; then PANEL_PASS=$(sed -nE 's/^[[:space:]]*管理员密码[：:][[:space:]]*//p' "$old_info" | head -n 1); fi
    done
    if [[ -n "$PANEL_USER" && -n "$PANEL_PASS" && "$PANEL_PASS" != '沿用原密码（未修改）' ]]; then
        PANEL_CREDENTIALS_AVAILABLE=1
    fi
    PANEL_USER="${PANEL_USER:-沿用原面板账号}"
    PANEL_PASS="${PANEL_PASS:-沿用原密码（未修改）}"
    EXISTING_DEPLOYMENT=1
}

add_nodes_to_existing() {
    [[ -f "$XUI_DB_PATH" ]] || { fail "未检测到已有 3x-ui，请使用无参数的新装命令。"; return 1; }
    install_deps || return 1
    detect_server_ip || return 1
    install -d -m 700 "$XUI_INFO_DIR" || return 1
    resolve_xray_binary || return 1
    load_existing_panel_context || return 1
    local profiles_file="$XUI_INFO_DIR/3x-ui-node-profiles.json" tag count primary_answer name
    if [[ -s "$profiles_file" ]]; then
        jq -e 'type == "array" and length == 4 and ([.[].key] | sort) == ["hy2","raw","reality","xhttp"]' \
            "$profiles_file" >/dev/null || { fail "已有节点资料文件不完整，请先核对 $profiles_file；没有新建节点。"; return 1; }
        NODE_PROFILES=$(jq -c '.' "$profiles_file") || return 1
        while IFS= read -r tag; do
            [[ "$tag" =~ ^[A-Za-z0-9_-]+$ ]] || { fail "已有节点标签格式无效。"; return 1; }
            count=$(sqlite3 -readonly "$XUI_DB_PATH" "SELECT count(*) FROM inbounds WHERE tag='$tag';") || return 1
            [[ "$count" == 1 ]] || { fail "已有节点资料与面板不一致，请在面板核对 $tag；没有重复创建。"; return 1; }
        done < <(jq -r '.[].payload.tag' "$profiles_file")
        printf '%s\n' "$NODE_PROFILES" >"$RUN_DIR/profiles.json" || return 1
        verify_running_nodes || return 1
        log_info "四种长期节点已经存在且正在监听，本次直接显示已有导入信息。"
        print_summary || return 1
        return 0
    fi

    # A disconnected SSH session can leave API-created nodes before the final
    # profile file is written. Allocating new ports here would duplicate them.
    count=$(sqlite3 -readonly "$XUI_DB_PATH" "SELECT count(*) FROM inbounds WHERE tag GLOB '3xuish-*';") || return 1
    if [[ "$count" != 0 ]]; then
        fail "发现 ${count} 个本脚本创建的入站，但正式节点资料缺失，可能是上次部署中断。为避免重复创建，已停止；请先核对数据库备份和 /tmp/3xuish.*/profiles.json，恢复资料后再继续。"
        return 1
    fi

    PRIMARY_NODE="${PRIMARY_NODE:-hy2}"
    case "$PRIMARY_NODE" in hy2|raw|reality|xhttp) ;; *) fail "PRIMARY_NODE 只能是 hy2、raw、reality 或 xhttp。"; return 1 ;; esac
    if [[ "${AUTO_YES:-0}" != 1 ]]; then
        printf '将保留全部原入站，并新增四种长期节点。请选择主用：\n1) Hy2（默认）  2) RAW TCP  3) REALITY  4) XHTTP\n'
        primary_answer=''
        prompt_read "主用选择 [回车保留 ${PRIMARY_NODE}]: " primary_answer
        case "$primary_answer" in
            1|hy2) PRIMARY_NODE=hy2 ;; 2|raw) PRIMARY_NODE=raw ;;
            3|reality) PRIMARY_NODE=reality ;; 4|xhttp) PRIMARY_NODE=xhttp ;;
            '') ;; *) fail "主用选择无效，请输入 1–4。"; return 1 ;;
        esac
    fi
    EXISTING_SELECTED_TCP='|'
    existing_select_node_port RAW_PORT 24445 tcp || return 1
    existing_select_node_port REALITY_PORT 24443 tcp || return 1
    existing_select_node_port HY2_PORT 24444 udp || return 1
    existing_select_node_port XHTTP_PORT 8443 tcp || return 1
    REALITY_SERVER_NAME="${REALITY_SERVER_NAME:-www.apple.com}"
    REALITY_TARGET="${REALITY_TARGET:-${REALITY_SERVER_NAME}:443}"
    valid_domain "$REALITY_SERVER_NAME" || { fail "REALITY_SERVER_NAME 无效。"; return 1; }
    XHTTP_PATH="${XHTTP_PATH:-/$(gen_random_str 10)}"
    [[ "$XHTTP_PATH" == /* ]] || XHTTP_PATH="/${XHTTP_PATH}"
    INBOUND_TLS_MODE=1
    VLESS_ENC_TYPE="${VLESS_ENC_TYPE:-1}"
    case "$VLESS_ENC_TYPE" in 1|2) ;; *) fail "VLESS_ENC_TYPE 只能是 1 或 2。"; return 1 ;; esac
    verify_reality_target || return 1
    local backup
    backup="$XUI_INFO_DIR/3x-ui-before-add-nodes-$(date -u +%Y%m%dT%H%M%SZ)-$$.db"
    sqlite3 "$XUI_DB_PATH" ".backup '$backup'" || return 1
    chmod 600 "$backup" || return 1
    log_info "已备份数据库：$backup；原入站保持保留。"
    EXISTING_INFO_BACKUP="${backup%.db}-info"
    install -d -m 700 "$EXISTING_INFO_BACKUP" || return 1
    for name in 3x-ui-node-profiles.json 3x-ui-nodes-info.txt 3x-ui-node-links.txt 3x-ui-xhttp-info.txt; do
        if [[ -f "$XUI_INFO_DIR/$name" ]]; then
            cp "$XUI_INFO_DIR/$name" "$EXISTING_INFO_BACKUP/$name" || return 1
        else
            : >"$EXISTING_INFO_BACKUP/$name.absent" || return 1
        fi
    done
    generate_vlessenc_keys || return 1
    generate_reality_keys || return 1
    prepare_node_profiles || return 1
    api_init || return 1
    create_inbound_nodes || return 1
    write_node_files || return 1
    ROLLBACK_NEEDED=0
    log_info "长期节点已添加到现有面板；原入站继续保留。数据库备份：$backup；旧连接信息：$EXISTING_INFO_BACKUP"
    print_summary
}


performance_command() {
    local mode="$1" helper bundled actual inbound_id=""; shift
    while (($#)); do
        case "$1" in
            --inbound-id)
                [[ $# -ge 2 && "$2" =~ ^[1-9][0-9]{0,17}$ ]] || {
                    fail "--inbound-id 需要一个有效的面板入站 ID。"; return 1;
                }
                [[ -z "$inbound_id" && "$mode" != status ]] || {
                    fail "重复指定入站或在状态查询中指定入站。"; return 1;
                }
                inbound_id="$2"; shift 2 ;;
            *) fail "不支持的性能修复参数：$1"; return 1 ;;
        esac
    done
    command -v python3 >/dev/null 2>&1 || {
        fail "此功能需要 Python 3.8 或更新版本（python3 命令）。Debian/Ubuntu 可执行 apt-get install -y python3；Alpine 执行 apk add python3。"
        return 1
    }
    python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1 || {
        fail "python3 必须为 Python 3.8 或更新版本；请先安装满足版本要求的 Python，并用 python3 --version 确认。未下载修复组件或修改节点。"
        return 1
    }
    command -v sha256sum >/dev/null 2>&1 || { fail "未找到 sha256sum。"; return 1; }
    helper="$RUN_DIR/performance_repair.py"
    bundled="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/lib/performance_repair.py"
    if [[ -f "$bundled" ]]; then
        cp -- "$bundled" "$helper" || return 1
    else
        curl -fsSL --connect-timeout 10 --max-time 60 \
            https://raw.githubusercontent.com/Iwhrt72/3xuish/main/lib/performance_repair.py \
            -o "$helper" || { fail "性能修复组件下载失败；未修改节点。"; return 1; }
    fi
    actual=$(sha256sum "$helper") || return 1
    [[ "${actual%% *}" == "$PERFORMANCE_HELPER_SHA256" ]] || {
        fail "性能修复组件校验不匹配；请重新下载同一版本的 install.sh 后再试，未执行组件。"
        return 1
    }
    local args=("$mode" --database "$XUI_DB_PATH" --install-dir "$XUI_INSTALL_DIR"
                --info-dir "$XUI_INFO_DIR" --service "$XUI_SERVICE")
    if [[ "$mode" == repair ]]; then
        resolve_xray_binary || return 1
        args+=(--xray "$XRAY_BIN")
    fi
    [[ -z "$inbound_id" ]] || args+=(--inbound-id "$inbound_id")
    python3 "$helper" "${args[@]}"
}

speed_test_command() {
    local helper="$RUN_DIR/server-speed-test.sh" source_dir bundled actual name
    command -v sha256sum >/dev/null 2>&1 || { fail "未找到 sha256sum。"; return 1; }
    source_dir="$(cd -- "$(dirname -- "$INSTALLER_SOURCE_FILE")" && pwd)"
    bundled="$source_dir/scripts/server-speed-test.sh"
    if [[ -f "$bundled" ]]; then
        cp -- "$bundled" "$helper" || return 1
        for name in server_speed_probe.py server_speed_test.py; do
            if [[ -f "$source_dir/scripts/$name" ]]; then
                cp -- "$source_dir/scripts/$name" "$RUN_DIR/$name" || return 1
            fi
        done
    else
        command -v curl >/dev/null 2>&1 || { fail "测速需要 curl；请先安装 curl。"; return 1; }
        curl -q -fLsS --connect-timeout 10 --max-time 60 \
            https://raw.githubusercontent.com/Iwhrt72/3xuish/main/scripts/server-speed-test.sh \
            -o "$helper" || { fail "测速组件下载失败；未执行测速。"; return 1; }
    fi
    actual=$(sha256sum "$helper") || return 1
    [[ "${actual%% *}" == "$SPEED_TEST_SHA256" ]] || {
        fail "测速入口校验不匹配；请重新下载同一版本的 install.sh 后再试。"
        return 1
    }
    XUI_DB_PATH="$XUI_DB_PATH" XUI_INSTALL_DIR="$XUI_INSTALL_DIR" XUI_INFO_DIR="$XUI_INFO_DIR" \
        XRAY_BIN="${XRAY_BIN:-}" bash "$helper" "$@"
}

fresh_install() {
    if [[ -e "$XUI_DB_PATH" ]]; then
        fail "检测到已有 3x-ui 数据库，已停止新装。清空后全新部署请选择重装；也可选择测速、旧参数修复或添加节点。"
        return 1
    fi
    acquire_maintenance_lock
    install_deps
    detect_server_ip
    init_config
    check_ports_available
    verify_reality_target
    if [[ "${CERT_MODE:-1}" == "2" ]]; then
        issue_domain_cert "$DOMAIN_NAME"
    else
        issue_ip_cert
    fi
    install_and_config_3xui
    generate_vlessenc_keys
    generate_reality_keys
    prepare_node_profiles
    api_init
    create_inbound_nodes
    write_node_files
    ROLLBACK_NEEDED=0
    print_summary
}

acquire_maintenance_lock() {
    local directory="$XUI_INFO_DIR/3xuish-reinstall" lock="$XUI_INFO_DIR/3xuish-reinstall/reinstall.lock"
    if [[ -n "${XUI_MAINTENANCE_JOB:-}" ]]; then
        [[ "$XUI_MAINTENANCE_JOB" =~ ^/root/3xuish-reinstall/job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ &&
           "$INSTALLER_SOURCE_FILE" == "$XUI_MAINTENANCE_JOB/installer.sh" &&
           -f "$XUI_MAINTENANCE_JOB/state.json" ]] || {
            fail "后台维护任务来源无效，已停止。"; return 1;
        }
        python3 - "$XUI_MAINTENANCE_JOB/state.json" <<'PY' || { fail "后台维护任务状态无效，已停止。"; return 1; }
import json,sys
state=json.load(open(sys.argv[1],encoding='utf-8'))
raise SystemExit(not (state.get('status') == 'running' and state.get('operation') == 'reinstall'
                     and state.get('phase') == 'fresh_installation'))
PY
        return 0
    fi
    [[ -z "${MAINTENANCE_LOCK_FD:-}" ]] || return 0
    [[ ! -L "$directory" && ! -L "$lock" ]] || { fail "维护锁路径异常。"; return 1; }
    mkdir -p "$directory" || return 1
    chmod 700 "$directory" || return 1
    exec {MAINTENANCE_LOCK_FD}>"$lock"
    if command -v flock >/dev/null 2>&1; then
        flock -n "$MAINTENANCE_LOCK_FD" || { fail "已有安装、重装、卸载或迁移任务运行中，请稍后重试。"; return 1; }
    elif command -v python3 >/dev/null 2>&1; then
        python3 -c 'import fcntl,sys; fcntl.flock(int(sys.argv[1]), fcntl.LOCK_EX | fcntl.LOCK_NB)' \
            "$MAINTENANCE_LOCK_FD" 2>/dev/null || {
            fail "无法取得维护锁，可能已有任务运行中。"; return 1;
        }
    else
        fail "需要 flock 或 python3 取得维护锁，请先安装其中一项。"; return 1
    fi
    # A dead worker may have already archived data; do not race its recovery.
    if [[ -f "$directory/latest.json" ]]; then
        command -v python3 >/dev/null 2>&1 || { fail "需 python3 检查上次维护状态，已停止。"; return 1; }
        python3 - "$directory" <<'PY' || { fail "上次维护任务尚未完成或需要恢复，请先查询状态。"; return 1; }
import json,re,sys
from pathlib import Path
root=Path(sys.argv[1])
name=json.loads((root/'latest.json').read_text()).get('job','')
if not re.fullmatch(r'job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}',name): raise SystemExit(1)
state=json.loads((root/name/'state.json').read_text())
raise SystemExit(state.get('status') in {'starting','running','manual_recovery_required'})
PY
    fi
    if [[ -e "$XUI_INFO_DIR/3xuish-socks-exit/latest.json" ]]; then
        command -v python3 >/dev/null 2>&1 || { fail "需 python3 检查上次 SOCKS5 创建状态，已停止。"; return 1; }
        python3 - "$XUI_INFO_DIR/3xuish-socks-exit" <<'PY' || { fail "上次 SOCKS5 创建尚未完成或需要恢复，请先运行 --socks-node-status。"; return 1; }
import json,re,sys
from pathlib import Path
root=Path(sys.argv[1])
pointer=root/'latest.json'
if root.is_symlink() or pointer.is_symlink(): raise SystemExit(1)
name=json.loads(pointer.read_text()).get('job','')
if not re.fullmatch(r'job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}',name): raise SystemExit(1)
job=root/name
if job.is_symlink() or (job/'state.json').is_symlink(): raise SystemExit(1)
state=json.loads((job/'state.json').read_text())
raise SystemExit(state.get('status') not in {'complete','failed','rolled_back'})
PY
    fi
}

validate_reinstall_target() {
    local operation="${1:-reinstall}"
    [[ "$XUI_DB_PATH" == /etc/x-ui/x-ui.db && "$XUI_INSTALL_DIR" == /usr/local/x-ui &&
       "$XUI_INFO_DIR" == /root && "$XUI_SERVICE" == x-ui ]] || {
        fail "重装和卸载仅支持默认 SQLite 路径和 x-ui 服务；自定义安装请先人工核验。"; return 1;
    }
    if [[ "$operation" == uninstall ]]; then
        [[ -e /usr/local/x-ui || -e /etc/x-ui || -e /usr/bin/x-ui ||
           -e /etc/systemd/system/x-ui.service || -e /etc/init.d/x-ui ]] || {
            fail "未找到可卸载的默认面板部署。"; return 1;
        }
    else
        [[ -f "$XUI_DB_PATH" ]] || { fail "未找到旧面板数据库，请选择全新安装。"; return 1; }
    fi
}

maintenance_command() {
    local operation="$1" mode="$2" confirmation="" helper bundled actual expected="REINSTALL"; shift 2
    [[ $# -eq 0 ]] || { fail "重装、卸载及状态查询不接受额外参数。"; return 1; }
    if [[ "$mode" == launch ]]; then
        validate_reinstall_target "$operation" || return 1
        if [[ "$operation" == uninstall ]]; then
            expected="UNINSTALL"
            printf '\n卸载会停止面板并取消开机启动，备份并移走程序、数据库和节点资料。\n'
            printf '现有节点与订阅将不可用；保留私有备份、证书及 acme 账户，不自动重装。\n'
        else
            printf '\n清空重装会备份并移走旧面板数据，重新生成面板账号、四节点和认证。\n'
            printf '旧节点与订阅链接将失效，服务会中断；这不是保留配置的升级。\n'
        fi
        prompt_read "输入 $expected 确认（其他输入取消）：" confirmation
        [[ "$confirmation" == "$expected" ]] || { log_info "已取消操作，未修改原配置。"; return 0; }
    fi
    command -v python3 >/dev/null 2>&1 &&
        python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1 || {
            fail "重装和卸载需要 Python 3.8+，请先安装满足要求的 python3。"; return 1;
        }
    command -v sha256sum >/dev/null 2>&1 || { fail "未找到 sha256sum。"; return 1; }
    helper="$RUN_DIR/clean_reinstall.py"
    bundled="$(cd -- "$(dirname -- "$INSTALLER_SOURCE_FILE")" && pwd)/lib/clean_reinstall.py"
    if [[ -f "$bundled" ]]; then
        cp -- "$bundled" "$helper" || return 1
    else
        curl -q -fLsS --connect-timeout 10 --max-time 60 \
            https://raw.githubusercontent.com/Iwhrt72/3xuish/main/lib/clean_reinstall.py \
            -o "$helper" || { fail "维护组件下载失败，未清空数据。"; return 1; }
    fi
    actual=$(sha256sum "$helper") || return 1
    [[ "${actual%% *}" == "$CLEAN_REINSTALL_SHA256" ]] || {
        fail "维护组件校验不匹配，请重新下载同一版本安装脚本。"; return 1;
    }
    local args=("$mode" --database "$XUI_DB_PATH" --install-dir "$XUI_INSTALL_DIR"
        --info-dir "$XUI_INFO_DIR" --service "$XUI_SERVICE" --operation "$operation")
    if [[ "$mode" == launch && "$operation" == reinstall ]]; then
        # Collect the new installation choices while SSH and the old panel run.
        install_deps
        detect_server_ip
        init_config
        export AUTO_YES=1 XUI_SERVER_IP="$SERVER_IP" PRIMARY_NODE CERT_MODE
        export XUI_DOMAIN="$DOMAIN_NAME" XUI_USERNAME="$PANEL_USER" XUI_PASSWORD="$PANEL_PASS"
        export XUI_PANEL_PORT="$PANEL_PORT" XUI_WEB_BASE_PATH="$WEB_BASE_PATH"
        export RAW_PORT REALITY_PORT HY2_PORT XHTTP_PORT XHTTP_PATH VLESS_ENC_TYPE INBOUND_TLS_MODE
        export REALITY_SERVER_NAME REALITY_TARGET
        curl -q -fLsS --connect-timeout 10 --max-time 90 \
            https://raw.githubusercontent.com/MHSanaei/3x-ui/main/install.sh \
            -o "$RUN_DIR/reinstall-upstream.sh" || {
                fail "上游安装组件预下载失败，旧面板仍在运行。"; return 1;
            }
        bash -n "$RUN_DIR/reinstall-upstream.sh" || { fail "上游安装组件语法检查失败。"; return 1; }
        args+=(--installer "$INSTALLER_SOURCE_FILE" --upstream-installer "$RUN_DIR/reinstall-upstream.sh")
    fi
    python3 "$helper" "${args[@]}"
}

reinstall_command() { maintenance_command reinstall "$@"; }
uninstall_command() { maintenance_command uninstall "$@"; }

# BEGIN GENERATED SOCKS HELPERS
# Generated by scripts/sync_socks_helpers.py; do not edit by hand.
SOCKS_EXIT_SHA256="3c0f0a5b447892ccad399f63cc8faa8839631f8a4a84388cab1e0d950751a846"
SOCKS_EXIT_PROBE_SHA256="cfd1d343de6f3044d3ceb5d2fc04e0c7d83689f0f94c53639a0c4092a96cb9e1"
SOCKS_EXIT_JOB_SHA256="0a31f62833dd82eb08a098c08f3fe85408e652bbb8839b57f2703f59da663a6e"
PROXY_INPUT_SHA256="94a4de635e84fb86b39af316b724c8665ffb6456c6e78954e2dd78a55bea5359"
socks_exit_embedded_helper() {
    case "$1" in
        socks_exit.sh) cat <<'SOCKS_HELPER_PAYLOAD_0'
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
SOCKS_HELPER_PAYLOAD_0
            ;;
        socks_node_probe.py) cat <<'SOCKS_HELPER_PAYLOAD_1'
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
kNe9JK//AWFl7LVMTAAA
SOCKS_HELPER_PAYLOAD_1
            ;;
        socks_exit_job.py) cat <<'SOCKS_HELPER_PAYLOAD_2'
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
huuqnZNHdXcqwPoNTgHSEQYCIf4LoVc4cJAxAAA=
SOCKS_HELPER_PAYLOAD_2
            ;;
        proxy_input.py) cat <<'SOCKS_HELPER_PAYLOAD_3'
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
SOCKS_HELPER_PAYLOAD_3
            ;;
        *) return 1 ;;
    esac
}
# END GENERATED SOCKS HELPERS

socks_exit_load_helpers() {
    local source_dir name expected actual helper
    command -v sha256sum >/dev/null 2>&1 || { fail "未找到 sha256sum。"; return 1; }
    command -v python3 >/dev/null 2>&1 || { fail "未找到 python3。"; return 1; }
    source_dir="$(cd -- "$(dirname -- "$INSTALLER_SOURCE_FILE")" && pwd)"
    for name in socks_exit.sh socks_node_probe.py socks_exit_job.py proxy_input.py; do
        helper="$RUN_DIR/$name"
        if [[ -f "$source_dir/lib/$name" ]]; then
            cp -- "$source_dir/lib/$name" "$helper" || return 1
        else
            socks_exit_embedded_helper "$name" | python3 -c '
import base64,gzip,sys
sys.stdout.buffer.write(gzip.decompress(base64.b64decode(sys.stdin.buffer.read())))
' >"$helper" || { fail "出口组件解包失败：$name"; return 1; }
        fi
        actual=$(sha256sum "$helper") || return 1
        case "$name" in
            socks_exit.sh) expected="$SOCKS_EXIT_SHA256" ;;
            socks_node_probe.py) expected="$SOCKS_EXIT_PROBE_SHA256" ;;
            socks_exit_job.py) expected="$SOCKS_EXIT_JOB_SHA256" ;;
            proxy_input.py) expected="$PROXY_INPUT_SHA256" ;;
        esac
        [[ "${actual%% *}" == "$expected" ]] || { fail "出口组件校验失败：$name；请使用同一版本的入口和组件。"; return 1; }
    done
    # Consumed by the checksum-verified shell helper sourced below.
    # shellcheck disable=SC2034
    SOCKS_EXIT_PROBE="$RUN_DIR/socks_node_probe.py"
    # shellcheck disable=SC2034
    SOCKS_EXIT_JOB_HELPER="$RUN_DIR/socks_exit_job.py"
    # shellcheck disable=SC2034
    PROXY_INPUT_HELPER="$RUN_DIR/proxy_input.py"
    # shellcheck source=lib/socks_exit.sh
    source "$RUN_DIR/socks_exit.sh"
}

socks_exit_command() {
    socks_exit_load_helpers || return 1
    create_socks_exit_node
}

socks_node_status_command() {
    socks_exit_load_helpers || return 1
    socks_exit_status_command
}

print_usage() {
    cat <<'USAGE'
用法：bash install.sh [选项]
  无参数 / --menu             打开管理菜单
  --install                  全新安装四节点（已有面板时停止）
  --reinstall                清空旧数据后全新部署（需输入 REINSTALL 确认）
  --reinstall-status         查询清空重装任务结果
  --uninstall                卸载面板、程序和节点数据（需输入 UNINSTALL 确认）
  --uninstall-status         查询卸载任务结果
  --speed-test [测速参数]     服务器直连与现有节点回环测速
  --check-performance        只读检查旧参数，可选 --inbound-id ID
  --repair-performance       原位修复旧参数，可选 --inbound-id ID
  --repair-performance-status 查询最近一次修复结果
  --add-nodes                保留原入站，另加四种节点
  --add-proxy-node           添加 SOCKS5 / HTTP / HTTPS 代理出口节点
  --proxy-node-status        查询最近一次代理节点创建结果
  --add-socks-node           兼容旧命令，等同 --add-proxy-node
  --socks-node-status        兼容旧命令，等同 --proxy-node-status
  --switch-domain 域名       迁移现有节点证书与连接域名
  --help                     显示帮助
AUTO_YES=1 无参数运行保留无人值守全新安装行为。
USAGE
}

menu_read_choice() {
    local menu_target="$1" menu_prompt="$2"
    if [[ -t 0 ]]; then
        IFS= read -r -p "$menu_prompt" "${menu_target?}"
    elif [[ -r /dev/tty ]] && { true </dev/tty; } 2>/dev/null; then
        IFS= read -r -p "$menu_prompt" "${menu_target?}" </dev/tty
    else
        IFS= read -r "${menu_target?}"
    fi
}

prepare_menu_runner() {
    local source_dir name
    if [[ ! -f "$INSTALLER_SOURCE_FILE" || ! -r "$INSTALLER_SOURCE_FILE" ]]; then
        fail "菜单需要先将脚本保存为文件。请执行：curl -fLso /tmp/3xuish-install.sh $REPO_RAW_URL && bash /tmp/3xuish-install.sh"
        return 1
    fi
    source_dir="$(cd -- "$(dirname -- "$INSTALLER_SOURCE_FILE")" && pwd)"
    mkdir -p "$RUN_DIR/menu/lib" "$RUN_DIR/menu/scripts"
    MENU_RUNNER_PATH="$RUN_DIR/menu/install.sh"
    cp -- "$INSTALLER_SOURCE_FILE" "$MENU_RUNNER_PATH"
    for name in performance_repair.py clean_reinstall.py socks_exit.sh socks_node_probe.py socks_exit_job.py proxy_input.py; do
        if [[ -f "$source_dir/lib/$name" ]]; then
            cp -- "$source_dir/lib/$name" "$RUN_DIR/menu/lib/$name"
        fi
    done
    for name in server-speed-test.sh server_speed_probe.py server_speed_test.py; do
        if [[ -f "$source_dir/scripts/$name" ]]; then
            cp -- "$source_dir/scripts/$name" "$RUN_DIR/menu/scripts/$name"
        fi
    done
}

menu_run_action() {
    local result=0
    # A separate Bash preserves errexit inside installation and rollback code.
    # Capturing a shell function in if/|| would disable its failure propagation.
    if XUI_DB_PATH="$XUI_DB_PATH" XUI_INSTALL_DIR="$XUI_INSTALL_DIR" XUI_INFO_DIR="$XUI_INFO_DIR" \
        XUI_SERVICE="$XUI_SERVICE" XRAY_BIN="${XRAY_BIN:-}" AUTO_YES=0 bash "$MENU_RUNNER_PATH" "$@"; then
        log_info "操作已结束，返回管理菜单。"
    else
        result=$?
        log_warn "操作未完成或部分测试失败（退出码：$result）；请查看上方结果。已返回管理菜单。"
    fi
}

interactive_menu() {
    local choice="" domain=""
    prepare_menu_runner
    while true; do
        printf '\n%b3xuish 管理菜单%b\n' "$CYAN" "$PLAIN"
        if [[ -f "$XUI_DB_PATH" ]]; then
            printf '面板状态：已安装（发现现有数据库）\n'
        else
            printf '面板状态：未发现现有数据库\n'
        fi
        printf '%s\n' \
            '  1) 全新安装四节点（已有面板时停止）' \
            '  2) 现有节点测速（消耗流量，不修改配置）' \
            '  3) 检查旧节点参数（只读）' \
            '  4) 原位修复旧参数（保留原节点与订阅）' \
            '  5) 添加四种节点（保留原入站，另增四个）' \
            '  6) 切换现有节点域名' \
            '  7) 查看最近修复结果' \
            '  8) 清空后全新部署（旧节点与订阅失效）' \
            '  9) 查看清空重装结果' \
            '  10) 卸载面板与节点（保留私有备份和证书）' \
            '  11) 查看卸载结果' \
            '  12) 添加代理出口节点（SOCKS5 / HTTP / HTTPS）' \
            '  13) 查看代理节点创建结果' \
            '  0) 退出'
        choice=""
        if ! menu_read_choice choice '请选择 [0-13]：'; then
            printf '\n输入已结束，退出菜单。\n'
            return 0
        fi
        case "$choice" in
            0) return 0 ;;
            1) menu_run_action --install ;;
            2) menu_run_action --speed-test ;;
            3) menu_run_action --check-performance ;;
            4) menu_run_action --repair-performance ;;
            5) menu_run_action --add-nodes ;;
            6)
                domain=""
                if ! menu_read_choice domain '新域名（留空取消）：'; then return 0; fi
                if [[ -n "$domain" ]]; then
                    if valid_domain "$domain"; then
                        menu_run_action --switch-domain "$domain"
                    else
                        log_warn "域名格式无效；未执行切换。"
                    fi
                fi
                ;;
            7) menu_run_action --repair-performance-status ;;
            8) menu_run_action --reinstall ;;
            9) menu_run_action --reinstall-status ;;
            10) menu_run_action --uninstall ;;
            11) menu_run_action --uninstall-status ;;
            12) menu_run_action --add-proxy-node ;;
            13) menu_run_action --proxy-node-status ;;
            '') ;;
            *) log_warn "请输入 0–13。" ;;
        esac
    done
}

main() {
    local action="${1:-}"
    case "$action" in
        --help|-h) [[ $# -eq 1 ]] || { fail "帮助选项不能附带其他参数。"; return 1; }; print_usage; return 0 ;;
        --menu|--install|--add-nodes|--add-proxy-node|--proxy-node-status|--add-socks-node|--socks-node-status|--reinstall|--reinstall-status|--uninstall|--uninstall-status)
            [[ $# -eq 1 ]] || { fail "${action} 不接受额外参数。"; return 1; } ;;
        --switch-domain)
            [[ $# -le 2 ]] || { fail "--switch-domain 只接受一个域名。"; return 1; } ;;
        --speed-test|--check-performance|--repair-performance|--repair-performance-status|'') ;;
        *) fail "未知选项：$action。使用 --help 查看用法。"; return 1 ;;
    esac
    init_platform
    case "$action" in
        --menu) interactive_menu ;;
        --install) fresh_install ;;
        --reinstall) reinstall_command launch ;;
        --reinstall-status) reinstall_command status ;;
        --uninstall) uninstall_command launch ;;
        --uninstall-status) uninstall_command status ;;
        --speed-test) speed_test_command "${@:2}" ;;
        --check-performance) performance_command check "${@:2}" ;;
        --repair-performance) acquire_maintenance_lock; performance_command repair "${@:2}" ;;
        --repair-performance-status) performance_command status "${@:2}" ;;
        --add-nodes) acquire_maintenance_lock; add_nodes_to_existing ;;
        --add-proxy-node|--add-socks-node) acquire_maintenance_lock; socks_exit_command ;;
        --proxy-node-status|--socks-node-status) socks_node_status_command ;;
        --switch-domain) acquire_maintenance_lock; switch_existing_to_domain "${2:-}" ;;
        '')
            if [[ "${AUTO_YES:-0}" == 1 ]]; then
                fresh_install
            else
                interactive_menu
            fi
            ;;
    esac
}

if [[ -z "${BASH_SOURCE[0]:-}" || "${BASH_SOURCE[0]:-}" == "$0" ]]; then
    main "$@"
fi
