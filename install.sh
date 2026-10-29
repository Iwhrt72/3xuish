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
    valid_domain "${REALITY_SERVER_NAME:-www.microsoft.com}" || { fail "REALITY_SERVER_NAME 必须为有效域名。"; return 1; }
    local target="${REALITY_TARGET:-${REALITY_SERVER_NAME:-www.microsoft.com}:443}"
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
    REALITY_SERVER_NAME="${REALITY_SERVER_NAME:-www.microsoft.com}"
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

    curl -fsSL --connect-timeout 10 --max-time 90 \
        https://raw.githubusercontent.com/MHSanaei/3x-ui/main/install.sh \
        -o "$RUN_DIR/upstream-install.sh"
    bash "$RUN_DIR/upstream-install.sh" </dev/null

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
    if ! timeout 12 openssl s_client -connect "$REALITY_TARGET" -servername "$REALITY_SERVER_NAME" \
        -tls1_3 -groups X25519 -verify_return_error -brief </dev/null 2>&1 \
        | grep -Eiq 'Protocol([[:space:]]+version)?[[:space:]]*:[[:space:]]*TLSv1\.3'; then
        fail "REALITY_TARGET 无法完成 TLS 1.3 握手；请选可达的握手域名。"
        return 1
    fi
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
    REALITY_SERVER_NAME="${REALITY_SERVER_NAME:-www.microsoft.com}"
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


main() {
    init_platform
    if [[ "${1:-}" == "--add-nodes" ]]; then
        add_nodes_to_existing
        return 0
    fi
    if [[ "${1:-}" == "--switch-domain" ]]; then
        switch_existing_to_domain "${2:-}"
        return 0
    fi

    install_deps
    detect_server_ip
    init_config
    if [[ -e "$XUI_DB_PATH" ]]; then
        fail "检测到已有 3x-ui 数据库。添加长期节点请运行 --add-nodes；域名迁移使用 --switch-domain。"
        return 1
    fi
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

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
