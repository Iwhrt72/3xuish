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
CLEAN_REINSTALL_SHA256="9590745818038c839ee124a352ccaef6529bb66e7a39ca1f864d2a89488aac63"
XRAY_RELEASE_API="https://api.github.com/repos/XTLS/Xray-core/releases/latest"
XRAY_DOWNLOAD_BASE="https://github.com/XTLS/Xray-core/releases/download"
XRAY_AUTO_UPDATE_DIR="/usr/local/lib/3xuish"
XRAY_AUTO_UPDATE_LOG="/var/log/3xuish-xray-update.log"
XRAY_AUTO_UPDATE_MARK="# 3xuish-xray-auto-update"
XRAY_UPDATE_CANDIDATE=""

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

# 只接受标准文本形式的 IPv6（不含区域 ID 和内嵌 IPv4），用于服务器地址和证书。
valid_ipv6() {
    local address="${1,,}" part group count=0 groups=()
    [[ "$address" =~ ^[0-9a-f:]{2,39}$ && "$address" == *:* ]] || return 1
    local head="$address" tail=""
    if [[ "$address" == *::* ]]; then
        head="${address%%::*}" tail="${address#*::}"
        [[ "$tail" != *::* ]] || return 1
    fi
    for part in "$head" "$tail"; do
        [[ -n "$part" ]] || continue
        [[ "$part" != :* && "$part" != *: ]] || return 1
        IFS=: read -r -a groups <<<"$part"
        for group in "${groups[@]}"; do
            [[ "$group" =~ ^[0-9a-f]{1,4}$ ]] || return 1
            count=$((count + 1))
        done
    done
    if [[ "$address" == *::* ]]; then ((count <= 7)); else ((count == 8)); fi
}

valid_ip() { valid_ipv4 "$1" || valid_ipv6 "$1"; }

# 纯 IPv6 服务器访问 GitHub（不支持 IPv6）需要 NAT64 或 WARP；下载失败时附上说明。
ipv6_github_hint() {
    if valid_ipv6 "${SERVER_IP:-}" || ! ip -4 route show default 2>/dev/null | grep -q .; then
        printf '%s' "本机没有 IPv4 出口，而 GitHub 不支持 IPv6：请先配置 NAT64/DNS64 或安装 WARP 获得 IPv4 出口后重试。"
    fi
}

check_github_reachable() {
    curl -q -fsS -o /dev/null --connect-timeout 8 --max-time 20 https://github.com && return 0
    fail "无法访问 GitHub，未开始安装。$(ipv6_github_hint)"
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
              "$candidate" != "${XHTTP_PORT:-8443}" && "$candidate" != "${SUB_PORT:-2096}" ]]; then
            printf '%s\n' "$candidate"; return 0
        fi
    done
    fail "未能生成可用面板端口。"
}

validate_config() {
    case "${PRIMARY_NODE:-}" in hy2|raw|reality|xhttp) ;; *) fail "PRIMARY_NODE 只能是 hy2、raw、reality 或 xhttp。"; return 1 ;; esac
    local name value seen='|80|'
    SUB_PORT="${SUB_PORT:-2096}"
    for name in PANEL_PORT RAW_PORT REALITY_PORT XHTTP_PORT SUB_PORT HY2_PORT; do
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
    for name in PANEL_PORT RAW_PORT REALITY_PORT XHTTP_PORT SUB_PORT; do
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
    install_qrencode_optional
}

# 二维码只是便利功能：安装失败（如 RHEL 未启用 EPEL）不影响部署。
install_qrencode_optional() {
    command -v qrencode >/dev/null 2>&1 && return 0
    case "${RELEASE:-}" in
        centos | rhel | almalinux | rocky | fedora | amzn | ol)
            if command -v dnf >/dev/null 2>&1; then dnf install -y -q qrencode; else yum install -y -q qrencode; fi ;;
        alpine) apk add --no-cache libqrencode-tools ;;
        arch | manjaro) pacman -S --noconfirm --needed qrencode ;;
        *) apt-get install -y -q qrencode ;;
    esac >/dev/null 2>&1 || true
    return 0
}

# 4. 获取服务器公网地址：优先 IPv4；没有 IPv4 时使用 IPv6（纯 IPv6 服务器）。
# 双栈服务器要用 IPv6 部署时，设置 XUI_SERVER_IP 为 IPv6 地址。
detect_server_ip() {
    local urls=(
        "https://api4.ipify.org"
        "https://ipv4.icanhazip.com"
        "https://v4.api.ipinfo.io/ip"
        "https://4.ident.me"
    )
    local urls6=(
        "https://api6.ipify.org"
        "https://ipv6.icanhazip.com"
        "https://v6.ident.me"
    )
    local u ip
    SERVER_IP="${XUI_SERVER_IP:-}"
    if [[ -z "$SERVER_IP" ]]; then
        for u in "${urls[@]}"; do
            ip=$(curl -fsS4m 4 "$u" | tr -d '[:space:]') || continue
            if valid_ipv4 "$ip"; then
                SERVER_IP="$ip"
                break
            fi
        done
    fi
    if [[ -z "$SERVER_IP" ]]; then
        for u in "${urls6[@]}"; do
            ip=$(curl -fsS6m 4 "$u" | tr -d '[:space:]') || continue
            if valid_ipv6 "$ip"; then
                SERVER_IP="$ip"
                log_info "未检测到公网 IPv4，将使用 IPv6 地址部署。"
                break
            fi
        done
    fi
    if [[ -z "$SERVER_IP" ]]; then
        prompt_read "未能自动检测到公网 IP，请手动输入服务器 IPv4 或 IPv6 地址: " SERVER_IP
    fi
    SERVER_IP="${SERVER_IP#[}"; SERVER_IP="${SERVER_IP%]}"
    if valid_ipv4 "$SERVER_IP"; then
        log_info "检测到服务器公网 IPv4: ${CYAN}${SERVER_IP}${PLAIN}"
    elif valid_ipv6 "$SERVER_IP"; then
        SERVER_IP="${SERVER_IP,,}"
        log_info "检测到服务器公网 IPv6: ${CYAN}${SERVER_IP}${PLAIN}"
    else
        fail "服务器 IP 地址无效。"; return 1
    fi
}

# acme.sh 的独立验证服务默认只监听 IPv4；IPv6 地址需要 --listen-v6。
acme_listen_args() {
    if valid_ipv6 "${SERVER_IP:-}"; then printf '%s\n' --listen-v6; fi
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
    if valid_ip "$host"; then
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
    local listen=()
    mapfile -t listen < <(acme_listen_args)
    run_acme --issue \
        -d "${SERVER_IP}" \
        --standalone ${listen[@]+"${listen[@]}"} \
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
    local listen=()
    mapfile -t listen < <(acme_listen_args)
    run_acme --issue \
        -d "${domain}" \
        --standalone ${listen[@]+"${listen[@]}"} \
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
    local sub_cert_follow=0 sub_updates=0 old_web_cert old_web_key old_sub_cert old_sub_key
    if [[ "$settings_exist" == 1 ]]; then
        # 订阅服务与面板使用同一证书时（本脚本新装即如此），随面板一起换成新域名证书。
        old_web_cert=$(sqlite3 "$XUI_DB_PATH" "SELECT value FROM settings WHERE key='webCertFile';") || return 1
        old_web_key=$(sqlite3 "$XUI_DB_PATH" "SELECT value FROM settings WHERE key='webKeyFile';") || return 1
        old_sub_cert=$(sqlite3 "$XUI_DB_PATH" "SELECT value FROM settings WHERE key='subCertFile';") || return 1
        old_sub_key=$(sqlite3 "$XUI_DB_PATH" "SELECT value FROM settings WHERE key='subKeyFile';") || return 1
        if [[ -n "$old_sub_cert" && "$old_sub_cert" == "$old_web_cert" && "$old_sub_key" == "$old_web_key" ]]; then
            sub_cert_follow=1
        fi
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
    if [[ "$sub_cert_follow" == 1 ]]; then
        for name in subCertFile subKeyFile; do
            if [[ "$name" == subCertFile ]]; then expected="$cert_sql"; else expected="$key_sql"; fi
            sqlite3 "$XUI_DB_PATH" "SELECT 'UPDATE settings SET value=' || quote(value) || ' WHERE key=' || quote(key) || ' AND value=' || quote('$expected') || ';' FROM settings WHERE key='$name';" >>"$domain_rollback_sql" || return 1
            sqlite3 "$XUI_DB_PATH" "SELECT 'UPDATE settings SET value=' || quote('$expected') || ' WHERE key=' || quote(key) || ' AND value=' || quote(value) || ';' || char(10) || 'SELECT changes();' FROM settings WHERE key='$name';" >>"$domain_state/updates.sql" || return 1
            sub_updates=$((sub_updates + 1))
        done
    fi
    printf 'COMMIT;\n' >>"$domain_rollback_sql"
    chmod 600 "$domain_rollback_sql" "$domain_state/updates.sql"
    trap domain_recovery_exit EXIT
    domain_mutated=1
    "${XUI_INSTALL_DIR}/x-ui" cert -webCert "$CERT_FILE" -webCertKey "$KEY_FILE" >/dev/null || return 1
    changes=$(sqlite3 -bail "$XUI_DB_PATH" <"$domain_state/updates.sql") || return 1
    [[ $(printf '%s\n' "$changes" | grep -c '^1$') -eq $((${#ids[@]} + sub_updates)) ]] || { fail "部分入站在更新时被修改，已取消域名切换。"; return 1; }
    restart_panel || { fail "面板重启失败。数据库备份：$backup"; return 1; }

    if [[ -s "$profile_file" ]]; then
        NODE_PROFILES=$(jq -c --arg address "$CERT_HOST" --arg cert "$CERT_FILE" --arg key "$KEY_FILE" '
            map(.clientOutbound.settings.address = $address |
                if .clientOutbound.streamSettings.network == "xhttp" then
                    .clientOutbound.streamSettings.xhttpSettings.host = "" else . end |
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
        # 主机部分可能是带方括号的 IPv6 地址。
        PANEL_PORT="${XUI_PANEL_PORT:-$(sed -nE 's#^访问地址：https?://(\[[^]]*\]|[^:/]+):([0-9]+)/.*#\2#p' "$XUI_INFO_DIR/3x-ui-nodes-info.txt" | head -n 1)}"
        WEB_BASE_PATH="${XUI_WEB_BASE_PATH:-$(sed -nE 's#^访问地址：https?://(\[[^]]*\]|[^:/]+):[0-9]+/([^/]+)/.*#\2#p' "$XUI_INFO_DIR/3x-ui-nodes-info.txt" | head -n 1)}"
        WEB_BASE_PATH="${WEB_BASE_PATH#/}"; WEB_BASE_PATH="${WEB_BASE_PATH%/}"
        valid_port "$PANEL_PORT" && [[ "$WEB_BASE_PATH" =~ ^[A-Za-z0-9_-]+$ ]] || { fail "无法恢复面板地址信息，已取消域名切换。"; return 1; }
        RAW_PORT=$(jq -r '.[] | select(.key == "raw") | .port' <<<"$NODE_PROFILES")
        REALITY_PORT=$(jq -r '.[] | select(.key == "reality") | .port' <<<"$NODE_PROFILES")
        HY2_PORT=$(jq -r '.[] | select(.key == "hy2") | .port' <<<"$NODE_PROFILES")
        XHTTP_PORT=$(jq -r '.[] | select(.key == "xhttp") | .port' <<<"$NODE_PROFILES")
        write_node_files || return 1
        refresh_subscription_links || log_warn "统一订阅中的链接未能更新；可运行 --enable-subscription 重新同步。"
        log_info "全部节点与链接已切换到 $CERT_HOST；原 UUID 和认证凭据已保留。"
        cat "$XUI_INFO_DIR/3x-ui-node-links.txt"
        load_subscription_urls "$profile_file"
        if [[ "$SUB_STATUS" == ok ]]; then
            print_subscription_block
            log_info "订阅地址已随域名变化，请在客户端改用上面的新订阅地址。"
        fi
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
    SUB_PORT="${XUI_SUB_PORT:-${SUB_PORT:-2096}}"
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
            + "&alpn=h3&insecure=0"
            + (if ($profile.hopPorts // "") != "" then "&mport=" + ($profile.hopPorts | encoded) else "" end)
            + "#" + $remark
        elif $out.protocol == "vless" then
            "vless://" + $settings.id + "@" + $address + ":" + $port
            + "?type=" + ($stream.network | encoded)
            + "&encryption=" + ($settings.encryption | encoded)
            + "&security=" + ($stream.security | encoded)
            + "&flow=" + ($settings.flow | encoded)
            + (if $stream.network == "xhttp" then
                 "&path=" + ($stream.xhttpSettings.path | encoded)
                 + "&mode=" + ($stream.xhttpSettings.mode | encoded)
                 + (if ($stream.xhttpSettings.host // "") != "" then
                      "&host=" + ($stream.xhttpSettings.host | encoded) else "" end)
               elif $stream.network == "ws" then
                 "&path=" + ($stream.wsSettings.path | encoded)
                 + (if ($stream.wsSettings.host // "") != "" then
                      "&host=" + ($stream.wsSettings.host | encoded) else "" end)
               else "" end)
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
    # 3x-ui 要求每个客户端的订阅 ID 唯一，这里全部随机生成。主用节点客户端的订阅作为
    # 统一订阅，其他节点以“外部链接”挂到它下面，各节点仍保留独立认证凭据。
    SUB_ID="${SUB_ID:-$(gen_random_str 20)}"
    [[ "$SUB_ID" =~ ^[A-Za-z0-9_-]{8,64}$ ]] || { fail "订阅 ID 格式无效。"; return 1; }
    local sub_ids
    sub_ids=$(jq -cn --arg owner "${PRIMARY_NODE:-hy2}" --arg sub "$SUB_ID" --arg a "$(gen_random_str 20)" \
        --arg b "$(gen_random_str 20)" --arg c "$(gen_random_str 20)" --arg d "$(gen_random_str 20)" '
        {hy2: $a, raw: $b, reality: $c, xhttp: $d} | .[$owner] = $sub')

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
        --argjson sub_ids "$sub_ids" \
        --arg primary "${PRIMARY_NODE:-hy2}" '
        input as $raw_uuid | input as $reality_uuid | input as $xhttp_uuid |
        input as $hy2_auth | input as $nonce | input as $dec | input as $enc |
        input as $reality_private | input as $reality_public | input as $reality_sid |
        def client_common($key): {
            email: ("3xuish_" + $key + "_" + $nonce),
            limitIp: 0, totalGB: 0, expiryTime: 0, enable: true,
            tgId: 0, subId: $sub_ids[$key], comment: "", reset: 0
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
        # Xray 的 XHTTP 客户端在 host 为空、地址是 IPv6 字面量时会拼出 https://::1/ 而失败，
        # 所以 IPv6 部署的客户端显式带上 [地址]；服务端 host 保持为空。
        def xhttp_client_options:
            xhttp_options + {host: (if ($address | contains(":")) then "[" + $address + "]" else "" end)};
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
                     xhttpSettings: xhttp_client_options}
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
    # 内核更新中断时删除尚未替换的候选文件；已替换的内核保持原状。
    if [[ -n "${XRAY_UPDATE_CANDIDATE:-}" ]]; then
        rm -f -- "$XRAY_UPDATE_CANDIDATE"
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
    if [[ -s "$XUI_INFO_DIR/3x-ui-argo.json" ]]; then
        jq -r '.link' "$XUI_INFO_DIR/3x-ui-argo.json" >>"$RUN_DIR/links.txt" || return 1
    fi
    install -m 600 "$RUN_DIR/links.txt" "$links_file" || return 1
    {
        printf '============================================================\n'
        printf '  3x-ui 四节点部署完成：连接信息与使用说明\n'
        printf '============================================================\n'
        printf '\n[面板]\n'
        printf '访问地址：%s://%s:%s%s/\n' "${PANEL_API_SCHEME:-https}" "$(url_host "$CERT_HOST")" "$PANEL_PORT" "${WEB_BASE_PATH:+/$WEB_BASE_PATH}"
        printf '管理员账号：%s\n管理员密码：%s\n' "$PANEL_USER" "$PANEL_PASS"
        printf '证书文件：%s\n私钥文件：%s\n' "$CERT_FILE" "$KEY_FILE"
        load_subscription_urls "$profiles_file"
        if [[ "$SUB_STATUS" == ok ]]; then
            printf '\n[统一订阅]\n'
            printf '通用订阅（v2rayN、v2rayNG、Shadowrocket 等）：%s\n' "$SUB_URL"
            if [[ -n "$SUB_CLASH_URL" ]]; then printf 'Clash / Mihomo 订阅：%s\n' "$SUB_CLASH_URL"; fi
            printf '订阅包含本脚本创建的节点；需放行 TCP/%s。' "$SUB_LISTEN_PORT"
            if [[ "$SUB_SCHEME" == http ]]; then printf '当前为 HTTP 明文，可运行 --enable-subscription 改为 HTTPS。'; fi
            printf '\n订阅由「%s」的客户端承载，其他节点是它的外部链接（面板中该客户端的“链接”页）；请勿删除这些外部链接。\n' \
                "$(jq -r '.[] | select(.subscriptionOwner == true) | .label' "$profiles_file")"
        elif [[ "$SUB_STATUS" == disabled || "$SUB_STATUS" == inactive ]]; then
            printf '\n统一订阅：尚未开启，可运行 --enable-subscription 一条链接导入全部节点。\n'
        fi
        local argo_file="$XUI_INFO_DIR/3x-ui-argo.json"
        if [[ -s "$argo_file" ]]; then
            printf '\n[Argo 隧道（Cloudflare）]\n'
            printf '模式：%s；域名：%s；本机入站：127.0.0.1:%s\n' \
                "$(jq -r 'if .mode == "quick" then "临时" else "固定" end' "$argo_file")" \
                "$(jq -r '.domain' "$argo_file")" "$(jq -r '.port' "$argo_file")"
            jq -r '.link' "$argo_file"
            if [[ "$(jq -r '.mode' "$argo_file")" == quick ]]; then
                printf '临时隧道的域名在服务重启后会变化，脚本会自动更新统一订阅；单独导入的链接需要重新导入。\n'
            fi
        fi
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
        printf '1. 在云安全组与系统防火墙开放 Hy2 UDP/%s、RAW TCP/%s、REALITY TCP/%s、XHTTP TCP/%s' \
            "$HY2_PORT" "$RAW_PORT" "$REALITY_PORT" "$XHTTP_PORT"
        if [[ "$SUB_STATUS" == ok ]]; then printf '、订阅 TCP/%s' "$SUB_LISTEN_PORT"; fi
        printf '。\n'
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
    load_subscription_urls "$XUI_INFO_DIR/3x-ui-node-profiles.json"
    if [[ "$SUB_STATUS" == ok ]]; then
        printf '\n手机扫码导入统一订阅：\n'
        print_qr "$SUB_URL" || true
    fi
    printf '\n查看每个节点的二维码：运行 --show-links，或在菜单选择 16。\n'
}

# 2. 二维码：超过约 550 个字符的链接在普通终端里扫不出来，改为提示复制链接或用订阅。
QR_MAX_LENGTH=550
print_qr() {
    local text="$1"
    command -v qrencode >/dev/null 2>&1 || {
        printf '（未安装 qrencode，无法显示二维码；Debian/Ubuntu：apt-get install -y qrencode）\n'; return 1;
    }
    if ((${#text} > QR_MAX_LENGTH)); then
        printf '（链接长 %s 个字符，二维码过密无法在终端扫描；请复制链接，或使用统一订阅）\n' "${#text}"
        return 1
    fi
    qrencode -t ANSIUTF8 -m 2 -- "$text"
}

show_links_command() {
    local profiles="$XUI_INFO_DIR/3x-ui-node-profiles.json" argo="$XUI_INFO_DIR/3x-ui-argo.json" label link kind port
    [[ -s "$profiles" ]] || {
        fail "未找到本脚本的节点资料 ${profiles}。旧版单节点部署请查看 $XUI_INFO_DIR/3x-ui-xhttp-info.txt。"; return 1;
    }
    install_qrencode_optional
    # 订阅地址与资料文件写入时相同：使用 TLS 节点证书覆盖的地址。
    CERT_HOST=$(jq -r 'first(.[] | select(.security == "tls") | .clientOutbound.streamSettings.tlsSettings.serverName) // empty' "$profiles")
    SERVER_IP=$(jq -r 'first(.[] | .clientOutbound.settings.address) // empty' "$profiles")
    load_subscription_urls "$profiles"
    print_subscription_block
    if [[ "$SUB_STATUS" == ok ]]; then
        printf '\n统一订阅二维码：\n'
        print_qr "$SUB_URL" || true
    fi
    while IFS=$'\t' read -r label kind port link; do
        printf '\n%b%s%b  %s/%s\n%s\n' "$CYAN" "$label" "$PLAIN" "$kind" "$port" "$link"
        print_qr "$link" || true
    done < <(
        jq -r '.[] | [.label, (if .key == "hy2" then "UDP" else "TCP" end), (.port | tostring), .link] | @tsv' "$profiles"
        if [[ -s "$argo" ]]; then jq -r '[.label, "TCP", "443", .link] | @tsv' "$argo"; fi
    )
}

# 16. 统一订阅：使用 3x-ui 自带的订阅服务。3x-ui 要求每个客户端的订阅 ID 唯一，
# 因此由主用节点的客户端承载统一订阅，其他节点的分享链接作为它的“外部链接”。
# 各节点保留独立认证凭据，订阅里的链接与资料文件中的链接一致。
url_host() {
    if [[ "$1" == *:* ]]; then printf '[%s]\n' "$1"; else printf '%s\n' "$1"; fi
}

# 键不存在时返回 3x-ui 的默认值；键名固定，避免拼接任意 SQL。
panel_setting_or_default() {
    local key="$1" default="$2" value
    case "$key" in
        subEnable|subPort|subPath|subURI|subListen|subCertFile|subKeyFile|\
        subClashEnable|subClashPath|subClashURI) ;;
        *) fail "不支持读取该面板设置。"; return 1 ;;
    esac
    # 面板重启时会短暂写库；等待锁释放，避免把“数据库忙”当成读取失败。
    value=$(sqlite3 -readonly -cmd '.timeout 10000' "$XUI_DB_PATH" \
        "SELECT value FROM settings WHERE key='$key' LIMIT 1;" 2>/dev/null) || {
        fail "无法读取面板设置 ${key}。"; return 1;
    }
    printf '%s\n' "${value:-$default}"
}

normalize_url_path() {
    local path="${1:-/}"
    [[ "$path" == /* ]] || path="/$path"
    [[ "$path" == */ ]] || path="$path/"
    printf '%s\n' "$path"
}

# 资料文件中标记为订阅承载的节点；没有标记时为空。
profiles_sub_owner() {
    jq -er '[.[] | select(.subscriptionOwner == true)] | if length == 1 then .[0].key else empty end' "$1" 2>/dev/null
}

profiles_sub_id() {
    jq -er '[.[] | select(.subscriptionOwner == true)] | if length == 1 then
        (.[0].payload.settings | fromjson | .clients[0].subId // "") else "" end |
        select(test("^[A-Za-z0-9_-]{8,64}$"))' "$1" 2>/dev/null
}

# 订阅承载节点：已有标记优先，否则选主用节点。承载节点的链接由面板生成；IPv6 地址下
# 面板生成的 XHTTP 链接不带 host，Xray 客户端无法连接，因此这时改用 Hy2 承载。
subscription_owner_key() {
    jq -er '
        def v6: (.clientOutbound.settings.address // "") | contains(":");
        ([.[] | select(.subscriptionOwner == true)][0]
         // ([.[] | select(.primary == true and ((.key == "xhttp" and v6) | not))][0])
         // ([.[] | select(.key == "hy2")][0]) // .[0]).key' "$1"
}

# 订阅外部链接：除承载节点外的全部节点，以及已开启的 Argo 节点。
subscription_external_links() {
    local profiles="$1" owner="$2" argo="$XUI_INFO_DIR/3x-ui-argo.json"
    if [[ -s "$argo" ]]; then
        jq -cn --slurpfile p "$profiles" --slurpfile a "$argo" --arg owner "$owner" '
            [($p[0][] | select(.key != $owner)), $a[0] | {kind: "link", value: .link, remark: .remark}]'
    else
        jq -c --arg owner "$owner" '[.[] | select(.key != $owner) | {kind: "link", value: .link, remark: .remark}]' "$profiles"
    fi
}

# 设置 SUB_STATUS（ok、disabled、inactive、unavailable）及订阅地址；读取失败不影响节点信息输出。
load_subscription_urls() {
    local profiles="$1" sub_id enabled port path uri cert key clash clash_path clash_uri scheme host
    SUB_STATUS="unavailable" SUB_URL="" SUB_CLASH_URL="" SUB_SCHEME="" SUB_LISTEN_PORT=""
    [[ -s "$profiles" && -f "$XUI_DB_PATH" ]] || return 0
    sub_id=$(profiles_sub_id "$profiles") || { SUB_STATUS="inactive"; return 0; }
    enabled=$(panel_setting_or_default subEnable true) || return 0
    if [[ "$enabled" != true ]]; then SUB_STATUS="disabled"; return 0; fi
    port=$(panel_setting_or_default subPort 2096) || return 0
    path=$(panel_setting_or_default subPath /sub/) || return 0
    uri=$(panel_setting_or_default subURI '') || return 0
    cert=$(panel_setting_or_default subCertFile '') || return 0
    key=$(panel_setting_or_default subKeyFile '') || return 0
    clash=$(panel_setting_or_default subClashEnable false) || return 0
    clash_path=$(panel_setting_or_default subClashPath /clash/) || return 0
    clash_uri=$(panel_setting_or_default subClashURI '') || return 0
    valid_port "$port" || return 0
    scheme=http
    if [[ -n "$cert" && -n "$key" ]]; then scheme=https; fi
    host=$(url_host "${CERT_HOST:-${SERVER_IP:-}}")
    [[ "$host" != "" && "$host" != "[]" ]] || return 0
    if [[ -n "$uri" ]]; then
        SUB_URL="${uri%/}/$sub_id"
    else
        SUB_URL="$scheme://$host:$port$(normalize_url_path "$path")$sub_id"
    fi
    if [[ "$clash" == true ]]; then
        if [[ -n "$clash_uri" ]]; then
            SUB_CLASH_URL="${clash_uri%/}/$sub_id"
        else
            SUB_CLASH_URL="$scheme://$host:$port$(normalize_url_path "$clash_path")$sub_id"
        fi
    fi
    SUB_SCHEME="$scheme" SUB_LISTEN_PORT="$port" SUB_STATUS="ok"
}

# fresh：新装面板，订阅改为 HTTPS（复用节点证书）并开启 Clash/Mihomo 格式。
# existing：只做增量修改，开启订阅与 Clash；HTTP 改 HTTPS 会让旧地址失效，需要确认。
configure_subscription() {
    local mode="$1" response current updated enabled cert answer changes=() joined
    SUB_SETTINGS_CHANGED=0
    response=$(api_request POST '/panel/api/setting/all' '{}') || { fail "无法读取面板设置。"; return 1; }
    current=$(jq -ce 'select(.success == true) | .obj | objects' <<<"$response") || {
        fail "面板未返回设置，可能版本过旧；未修改订阅设置。"; return 1;
    }
    updated="$current"
    enabled=$(jq -r '.subEnable' <<<"$current") || return 1
    cert=$(jq -r '.subCertFile // ""' <<<"$current") || return 1
    if [[ "$mode" == fresh ]]; then
        updated=$(jq -c --argjson port "$SUB_PORT" --arg cert "$CERT_FILE" --arg key "$KEY_FILE" '
            .subEnable = true | .subClashEnable = true | .subPort = $port |
            .subCertFile = $cert | .subKeyFile = $key' <<<"$current") || return 1
        changes+=("订阅端口 TCP/${SUB_PORT}，HTTPS，Clash/Mihomo 格式")
    else
        if [[ "$enabled" != true ]]; then
            updated=$(jq -c --argjson port "$SUB_PORT" --arg cert "$CERT_FILE" --arg key "$KEY_FILE" '
                .subEnable = true | .subPort = $port | .subCertFile = $cert | .subKeyFile = $key' <<<"$updated") || return 1
            changes+=("开启订阅服务 TCP/${SUB_PORT}（HTTPS）")
        elif [[ -z "$cert" ]]; then
            answer=""
            if [[ "${AUTO_YES:-0}" != 1 ]]; then
                printf '订阅服务当前为 HTTP 明文。改为 HTTPS 会使用节点证书 %s，已分发的 HTTP 订阅地址将失效。\n' "$CERT_FILE"
                prompt_read "改为 HTTPS？[Y/n]: " answer
            fi
            if [[ ! "$answer" =~ ^[Nn] ]]; then
                updated=$(jq -c --arg cert "$CERT_FILE" --arg key "$KEY_FILE" '
                    .subCertFile = $cert | .subKeyFile = $key' <<<"$updated") || return 1
                changes+=("订阅服务改为 HTTPS")
            fi
        fi
        if [[ "$(jq -r '.subClashEnable' <<<"$updated")" != true ]]; then
            updated=$(jq -c '.subClashEnable = true' <<<"$updated") || return 1
            changes+=("开启 Clash/Mihomo 订阅格式")
        fi
    fi
    if [[ ${#changes[@]} -eq 0 ]]; then
        log_info "订阅服务设置无需修改。"
        return 0
    fi
    response=$(api_request POST '/panel/api/setting/update' "$updated") || { fail "保存订阅设置时面板无响应。"; return 1; }
    jq -e '.success == true' >/dev/null <<<"$response" || {
        fail "面板拒绝订阅设置：$(jq -r '.msg // "未知原因"' <<<"$response" 2>/dev/null)"; return 1;
    }
    SUB_SETTINGS_CHANGED=1
    printf -v joined '%s；' "${changes[@]}"
    log_info "已更新订阅设置：${joined%；}"
}

subscription_owner_email() {
    jq -er --arg key "$2" '.[] | select(.key == $key) | .payload.settings | fromjson | .clients[0].email |
        select(test("^[A-Za-z0-9_.@-]+$"))' "$1"
}

# 把其他节点（及 Argo 节点）的当前链接写入承载客户端的外部链接，整组替换。
sync_subscription_links() {
    local profiles="$1" owner="$2" email links response
    email=$(subscription_owner_email "$profiles" "$owner") || { fail "无法确定订阅承载客户端。"; return 1; }
    links=$(subscription_external_links "$profiles" "$owner") || return 1
    response=$(api_request POST "/panel/api/clients/$email/externalLinks" "$(jq -cn --argjson l "$links" '{externalLinks: $l}')") || {
        fail "写入订阅外部链接时面板无响应，面板版本可能过旧。"; return 1;
    }
    jq -e '.success == true' >/dev/null <<<"$response" || {
        fail "面板拒绝订阅外部链接：$(jq -r '.msg // "未知原因"' <<<"$response" 2>/dev/null)"; return 1;
    }
}

# 从本机请求订阅，确认每个节点的认证凭据都在订阅内容中。
verify_subscription() {
    local profiles="$1" sub_id="$2" port path listen scheme cert key base body decoded credential clash clash_path missing=0
    port=$(panel_setting_or_default subPort 2096) || return 1
    path=$(normalize_url_path "$(panel_setting_or_default subPath /sub/)") || return 1
    listen=$(panel_setting_or_default subListen '') || return 1
    cert=$(panel_setting_or_default subCertFile '') || return 1
    key=$(panel_setting_or_default subKeyFile '') || return 1
    clash=$(panel_setting_or_default subClashEnable false) || return 1
    clash_path=$(normalize_url_path "$(panel_setting_or_default subClashPath /clash/)") || return 1
    scheme=http
    if [[ -n "$cert" && -n "$key" ]]; then scheme=https; fi
    case "$listen" in ''|0.0.0.0|::|'[::]') listen=127.0.0.1 ;; esac
    base="$scheme://$(url_host "$listen"):$port"
    body=""
    for _ in {1..30}; do
        body=$(curl -q -fsSk --noproxy '*' --connect-timeout 3 --max-time 10 "$base$path$sub_id" 2>/dev/null) || body=""
        [[ -z "$body" ]] || break
        sleep 1
    done
    [[ -n "$body" ]] || { fail "订阅服务未响应：$base$path"; return 1; }
    decoded=$(printf '%s' "$body" | base64 -d 2>/dev/null) || decoded=""
    [[ "$decoded" == *://* ]] || decoded="$body"
    while IFS= read -r credential; do
        [[ "$decoded" == *"$credential"* ]] || { fail "订阅内容缺少节点（凭据前缀 ${credential:0:6}）。"; return 1; }
    done < <(subscription_credentials "$profiles")
    if [[ "$clash" == true ]]; then
        body=$(curl -q -fsSk --noproxy '*' --connect-timeout 3 --max-time 10 "$base$clash_path$sub_id" 2>/dev/null) || body=""
        [[ "$body" == *proxies:* ]] || { fail "Clash/Mihomo 订阅未返回节点列表。"; return 1; }
        while IFS= read -r credential; do
            [[ "$body" == *"$credential"* ]] || missing=$((missing + 1))
        done < <(subscription_credentials "$profiles")
        if ((missing > 0)); then
            log_warn "Clash/Mihomo 订阅中有 ${missing} 个节点无法用该格式表示，这些节点请使用通用订阅。"
        fi
    fi
    log_info "订阅服务已返回全部 $(subscription_credentials "$profiles" | wc -l | tr -d ' ') 个节点。"
}

subscription_credentials() {
    jq -r '.[].credential' "$1"
    if [[ -s "$XUI_INFO_DIR/3x-ui-argo.json" ]]; then jq -r '.credential' "$XUI_INFO_DIR/3x-ui-argo.json"; fi
}

mark_subscription_owner() {
    NODE_PROFILES=$(jq -c --arg owner "$1" 'map(if .key == $owner then .subscriptionOwner = true else del(.subscriptionOwner) end)' \
        <<<"$NODE_PROFILES") || return 1
    printf '%s\n' "$NODE_PROFILES" >"$RUN_DIR/profiles.json"
}

# 面板客户端记录的 id 是数据库序号，UUID 在 uuid 字段；这里换回客户端结构体的字段名。
sub_id_update_payload() {
    jq -ce --arg sub "$2" '{
        id: (.uuid // ""), password: (.password // ""), auth: (.auth // ""), flow: (.flow // ""),
        security: (.security // ""), email, limitIp: (.limitIp // 0), totalGB: (.totalGB // 0),
        expiryTime: (.expiryTime // 0), enable: (.enable // true), tgId: (.tgId // 0), subId: $sub,
        group: (.group // ""), comment: (.comment // ""), reset: (.reset // 0),
        resetDay: (.resetDay // 0), resetWeekday: (.resetWeekday // 0), resetMax: (.resetMax // 0),
        trafficReset: (.trafficReset // "never"), trafficResetDay: (.trafficResetDay // 1),
        limitHwid: (.limitHwid // 0)
    } | with_entries(select(.value != "" or (.key | IN("email", "subId", "comment", "group"))))' <<<"$1"
}

# 旧版的订阅 ID 由节点标签派生；改为随机值，只影响承载客户端，其他客户端不变。
rotate_owner_sub_id() {
    local email="$1" response record payload
    response=$(api_request GET "/panel/api/clients/get/$email") || { fail "无法读取客户端 $email，面板版本可能过旧。"; return 1; }
    record=$(jq -ce 'select(.success == true) | .obj.client | objects' <<<"$response") || {
        fail "面板未返回客户端 $email。"; return 1;
    }
    SUB_ID=$(gen_random_str 20)
    payload=$(sub_id_update_payload "$record" "$SUB_ID") || { fail "无法生成客户端更新内容。"; return 1; }
    response=$(api_request POST "/panel/api/clients/update/$email" "$payload") || { fail "更新 $email 时面板无响应。"; return 1; }
    jq -e '.success == true' >/dev/null <<<"$response" || {
        fail "面板拒绝更新 ${email}：$(jq -r '.msg // "未知原因"' <<<"$response" 2>/dev/null)"; return 1;
    }
    NODE_PROFILES=$(jq -c --arg email "$email" --arg sub "$SUB_ID" '
        map(if (.payload.settings | fromjson | .clients[0].email) == $email then
            .payload.settings |= (fromjson | .clients |= map(.subId = $sub) | tojson) else . end)' <<<"$NODE_PROFILES") || return 1
    printf '%s\n' "$NODE_PROFILES" >"$RUN_DIR/profiles.json"
}

setup_fresh_subscription() {
    local owner
    owner=$(subscription_owner_key "$RUN_DIR/profiles.json") || return 1
    configure_subscription fresh || return 1
    sync_subscription_links "$RUN_DIR/profiles.json" "$owner" || return 1
    if [[ "$SUB_SETTINGS_CHANGED" == 1 ]]; then
        restart_panel || return 1
    fi
    verify_subscription "$RUN_DIR/profiles.json" "$SUB_ID" || return 1
    mark_subscription_owner "$owner"
}

load_ports_from_profiles() {
    RAW_PORT=$(jq -r '.[] | select(.key == "raw") | .port' <<<"$NODE_PROFILES")
    REALITY_PORT=$(jq -r '.[] | select(.key == "reality") | .port' <<<"$NODE_PROFILES")
    HY2_PORT=$(jq -r '.[] | select(.key == "hy2") | .port' <<<"$NODE_PROFILES")
    XHTTP_PORT=$(jq -r '.[] | select(.key == "xhttp") | .port' <<<"$NODE_PROFILES")
}

# 节点链接变化后（域名、端口跳跃、Argo）刷新订阅外部链接；未开启统一订阅时不做任何事。
refresh_subscription_links() {
    local profiles="$XUI_INFO_DIR/3x-ui-node-profiles.json" owner
    owner=$(profiles_sub_owner "$profiles") || return 0
    if [[ "$API_READY" != 1 ]]; then api_init || return 1; fi
    sync_subscription_links "$profiles" "$owner" || return 1
    log_info "统一订阅中的节点链接已更新。"
}

load_panel_context_for_api() {
    # 开机同步等后台调用传 --no-deps，避免每次都运行包管理器。
    if [[ "${1:-}" != --no-deps ]]; then install_deps || return 1; fi
    detect_server_ip || return 1
    resolve_xray_binary || return 1
    load_existing_panel_context || return 1
}

# 已有部署开启统一订阅：只处理本脚本资料文件中的节点，其他入站和客户端不变。
enable_subscription_command() {
    local profiles_file="$XUI_INFO_DIR/3x-ui-node-profiles.json" owner email
    [[ -f "$XUI_DB_PATH" ]] || { fail "未检测到 3x-ui 数据库。"; return 1; }
    [[ -s "$profiles_file" ]] || {
        fail "未找到本脚本的节点资料 ${profiles_file}；统一订阅只管理本脚本创建的节点。"; return 1;
    }
    load_panel_context_for_api || return 1
    NODE_PROFILES=$(jq -c '.' "$profiles_file") || return 1
    printf '%s\n' "$NODE_PROFILES" >"$RUN_DIR/profiles.json" || return 1
    SUB_PORT="${XUI_SUB_PORT:-2096}"
    valid_port "$SUB_PORT" || { fail "XUI_SUB_PORT 必须是 1–65535 的端口。"; return 1; }
    api_init || return 1
    owner=$(subscription_owner_key "$RUN_DIR/profiles.json") || return 1
    email=$(subscription_owner_email "$RUN_DIR/profiles.json" "$owner") || { fail "无法确定订阅承载客户端。"; return 1; }
    if ! SUB_ID=$(profiles_sub_id "$RUN_DIR/profiles.json"); then
        rotate_owner_sub_id "$email" || return 1
        # 面板中的订阅 ID 已改，资料文件立即同步，避免后续步骤失败时两边不一致。
        install -m 600 "$RUN_DIR/profiles.json" "$profiles_file" || return 1
    fi
    configure_subscription existing || return 1
    sync_subscription_links "$RUN_DIR/profiles.json" "$owner" || return 1
    if [[ "$SUB_SETTINGS_CHANGED" == 1 ]]; then
        log_info "正在重启 ${XUI_SERVICE} 使订阅设置生效..."
        restart_panel || { fail "面板重启失败。"; return 1; }
    fi
    verify_subscription "$RUN_DIR/profiles.json" "$SUB_ID" || return 1
    mark_subscription_owner "$owner" || return 1
    verify_running_nodes || return 1
    load_ports_from_profiles
    write_node_files || return 1
    load_subscription_urls "$profiles_file"
    print_subscription_block
}

print_subscription_block() {
    case "${SUB_STATUS:-unavailable}" in
        ok)
            printf '\n%b[统一订阅]%b\n' "$CYAN" "$PLAIN"
            printf '通用订阅（v2rayN、v2rayNG、Shadowrocket 等）：\n%s\n' "$SUB_URL"
            if [[ -n "$SUB_CLASH_URL" ]]; then printf 'Clash / Mihomo 订阅：\n%s\n' "$SUB_CLASH_URL"; fi
            ;;
        disabled|inactive) printf '\n统一订阅：尚未开启，可运行 --enable-subscription 或在菜单选择 15。\n' ;;
    esac
}

# 17. Hy2 端口跳跃：把一段 UDP 端口重定向到 Hy2 端口。Xray 只监听原端口；
# 范围写入入站的 quicParams.udpHop，面板据此在分享链接和订阅中公布（mport / ports）。
HY2_HOP_CONF="/etc/3xuish/hy2-hop.conf"
HY2_HOP_SCRIPT="/usr/local/lib/3xuish/hy2-hop.sh"
HY2_HOP_SERVICE="3xuish-hy2-hop"

hy2_hop_has_systemd() { command -v systemctl >/dev/null 2>&1 && [[ -d /run/systemd/system ]]; }
hy2_hop_has_openrc() { command -v rc-service >/dev/null 2>&1 && command -v rc-update >/dev/null 2>&1; }

# 开机时由服务调用的 POSIX 脚本；只读取数字配置，不执行配置文件内容。
write_hy2_hop_script() {
    install -d -m 755 "${HY2_HOP_SCRIPT%/*}" || return 1
    cat >"$HY2_HOP_SCRIPT.tmp" <<'HOPSCRIPT' || return 1
#!/bin/sh
# 由 3xuish 生成：把 UDP 端口范围重定向到 Hy2 端口。配置：/etc/3xuish/hy2-hop.conf
set -u
CONF=/etc/3xuish/hy2-hop.conf
TABLE=hy2hop_3xuish
COMMENT=3xuish-hy2-hop
value() { sed -n "s/^$1=\([0-9a-z]\{1,8\}\)\$/\1/p" "$CONF" | head -n 1; }
read_conf() {
    [ -f "$CONF" ] || { echo "missing $CONF" >&2; exit 1; }
    START=$(value HOP_START); END=$(value HOP_END); TARGET=$(value HOP_TARGET); BACKEND=$(value HOP_BACKEND)
    case "$START$END$TARGET" in ''|*[!0-9]*) echo "invalid $CONF" >&2; exit 1 ;; esac
    case "$BACKEND" in nft|iptables) ;; *) echo "invalid backend in $CONF" >&2; exit 1 ;; esac
}
remove_rules() {
    if command -v nft >/dev/null 2>&1; then
        nft delete table ip "$TABLE" 2>/dev/null
        nft delete table ip6 "$TABLE" 2>/dev/null
    fi
    for cmd in iptables ip6tables; do
        command -v "$cmd" >/dev/null 2>&1 || continue
        "$cmd" -t nat -S PREROUTING 2>/dev/null | grep -F -- "$COMMENT" | sed -e 's/^-A /-D /' -e 's/"//g' |
            while read -r rule; do
                # shellcheck disable=SC2086
                "$cmd" -t nat $rule 2>/dev/null
            done
    done
    return 0
}
apply_rules() {
    read_conf
    remove_rules
    if [ "$BACKEND" = nft ]; then
        for fam in ip ip6; do
            if ! printf 'table %s %s {\n chain prerouting {\n  type nat hook prerouting priority -100; policy accept;\n  udp dport %s-%s counter redirect to :%s\n }\n}\n' \
                "$fam" "$TABLE" "$START" "$END" "$TARGET" | nft -f -; then
                [ "$fam" = ip6 ] || exit 1
                echo "IPv6 redirect not applied" >&2
            fi
        done
    else
        iptables -t nat -A PREROUTING -p udp --dport "$START:$END" -m comment --comment "$COMMENT" \
            -j REDIRECT --to-ports "$TARGET" || exit 1
        if command -v ip6tables >/dev/null 2>&1; then
            ip6tables -t nat -A PREROUTING -p udp --dport "$START:$END" -m comment --comment "$COMMENT" \
                -j REDIRECT --to-ports "$TARGET" 2>/dev/null || echo "IPv6 redirect not applied" >&2
        fi
    fi
}
active() {
    read_conf
    if [ "$BACKEND" = nft ]; then
        nft list table ip "$TABLE" 2>/dev/null | grep -q "dport $START-$END .*redirect to :$TARGET"
    else
        iptables -t nat -S PREROUTING 2>/dev/null | grep -F -- "$COMMENT" | grep -q -- "--dport $START:$END .*--to-ports $TARGET"
    fi
}
case "${1:-}" in
    apply) apply_rules ;;
    remove) remove_rules ;;
    active) active ;;
    *) echo "usage: $0 apply|remove|active" >&2; exit 2 ;;
esac
HOPSCRIPT
    chmod 755 "$HY2_HOP_SCRIPT.tmp" && mv -f -- "$HY2_HOP_SCRIPT.tmp" "$HY2_HOP_SCRIPT"
}

install_hy2_hop_service() {
    if hy2_hop_has_systemd; then
        cat >"/etc/systemd/system/$HY2_HOP_SERVICE.service" <<UNIT || return 1
[Unit]
Description=3xuish Hy2 UDP port hopping
Wants=network-pre.target
After=network-pre.target nftables.service firewalld.service ufw.service netfilter-persistent.service iptables.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh $HY2_HOP_SCRIPT apply
ExecStop=/bin/sh $HY2_HOP_SCRIPT remove

[Install]
WantedBy=multi-user.target
UNIT
        systemctl daemon-reload || return 1
        systemctl enable "$HY2_HOP_SERVICE" >/dev/null 2>&1 || return 1
        systemctl restart "$HY2_HOP_SERVICE" || return 1
    elif hy2_hop_has_openrc; then
        cat >"/etc/init.d/$HY2_HOP_SERVICE" <<RC || return 1
#!/sbin/openrc-run
description="3xuish Hy2 UDP port hopping"
depend() {
    need net
    after firewall iptables ip6tables nftables
}
start() {
    ebegin "Applying Hy2 port hopping"
    /bin/sh $HY2_HOP_SCRIPT apply
    eend \$?
}
stop() {
    ebegin "Removing Hy2 port hopping"
    /bin/sh $HY2_HOP_SCRIPT remove
    eend \$?
}
RC
        chmod 755 "/etc/init.d/$HY2_HOP_SERVICE" || return 1
        rc-update add "$HY2_HOP_SERVICE" default >/dev/null 2>&1 || return 1
        rc-service "$HY2_HOP_SERVICE" restart >/dev/null 2>&1 || return 1
    else
        log_warn "未检测到 systemd 或 OpenRC：规则已立即生效，但重启后不会自动恢复。"
        /bin/sh "$HY2_HOP_SCRIPT" apply || return 1
    fi
}

remove_hy2_hop_service() {
    if hy2_hop_has_systemd && [[ -f "/etc/systemd/system/$HY2_HOP_SERVICE.service" ]]; then
        systemctl disable --now "$HY2_HOP_SERVICE" >/dev/null 2>&1 || true
        rm -f -- "/etc/systemd/system/$HY2_HOP_SERVICE.service"
        systemctl daemon-reload >/dev/null 2>&1 || true
    elif [[ -f "/etc/init.d/$HY2_HOP_SERVICE" ]]; then
        rc-service "$HY2_HOP_SERVICE" stop >/dev/null 2>&1 || true
        rc-update del "$HY2_HOP_SERVICE" default >/dev/null 2>&1 || true
        rm -f -- "/etc/init.d/$HY2_HOP_SERVICE"
    fi
    if [[ -f "$HY2_HOP_SCRIPT" ]]; then /bin/sh "$HY2_HOP_SCRIPT" remove || true; fi
}

ensure_nat_backend() {
    if command -v nft >/dev/null 2>&1 && nft list tables >/dev/null 2>&1; then
        HY2_HOP_BACKEND=nft; return 0
    fi
    if command -v iptables >/dev/null 2>&1 && iptables -t nat -S PREROUTING >/dev/null 2>&1; then
        HY2_HOP_BACKEND=iptables; return 0
    fi
    log_info "正在安装 nftables，用于端口重定向..."
    case "${RELEASE:-}" in
        centos | rhel | almalinux | rocky | fedora | amzn | ol)
            if command -v dnf >/dev/null 2>&1; then dnf install -y -q nftables; else yum install -y -q nftables; fi ;;
        alpine) apk add --no-cache nftables ;;
        arch | manjaro) pacman -S --noconfirm --needed nftables ;;
        *) apt-get update -y -q && apt-get install -y -q nftables ;;
    esac || { fail "nftables 安装失败，未开启端口跳跃。"; return 1; }
    nft list tables >/dev/null 2>&1 || { fail "nftables 不可用（可能缺少内核模块或权限），未开启端口跳跃。"; return 1; }
    HY2_HOP_BACKEND=nft
}

hy2_hop_current_range() {
    local start end
    [[ -f "$HY2_HOP_CONF" ]] || return 1
    start=$(sed -n 's/^HOP_START=\([0-9]\{1,5\}\)$/\1/p' "$HY2_HOP_CONF")
    end=$(sed -n 's/^HOP_END=\([0-9]\{1,5\}\)$/\1/p' "$HY2_HOP_CONF")
    [[ -n "$start" && -n "$end" ]] || return 1
    printf '%s-%s\n' "$start" "$end"
}

# 范围内不能有其他 UDP 服务，否则会被重定向到 Hy2。
check_hop_range_free() {
    local start="$1" end="$2" hy2_port="$3" hy2_tag="$4" port count
    while read -r port; do
        [[ "$port" =~ ^[0-9]+$ ]] || continue
        if ((port >= start && port <= end && port != hy2_port)); then
            fail "UDP/${port} 已有程序监听，位于跳跃范围 ${start}-${end} 内；请换一个范围。"; return 1
        fi
    # 只查询 UDP 时 ss 不输出 Netid 列，本地地址在第 4 列。
    done < <(ss -H -lnu | awk '{n = split($4, a, ":"); print a[n]}' | sort -un)
    count=$(sqlite3 -readonly "$XUI_DB_PATH" "SELECT count(*) FROM inbounds WHERE tag<>'$hy2_tag' AND port BETWEEN $start AND $end AND (protocol IN ('hysteria','wireguard','tuic','amneziawg') OR json_extract(stream_settings,'$.network') IN ('kcp','quic'));") || return 1
    [[ "$count" == 0 ]] || { fail "面板中有 ${count} 个 UDP 入站端口位于 ${start}-${end} 内；请换一个范围。"; return 1; }
}

hy2_inbound_id() {
    local tag="$1" id
    [[ "$tag" =~ ^[A-Za-z0-9_-]+$ ]] || return 1
    id=$(sqlite3 -readonly "$XUI_DB_PATH" "SELECT id FROM inbounds WHERE tag='$tag' AND protocol='hysteria';") || return 1
    [[ "$id" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$id"
}

# ports 为空时删除 udpHop；返回新的 streamSettings 字符串写入 HY2_HOP_STREAM。
update_hy2_inbound_hop() {
    local id="$1" ports="$2" response inbound payload
    response=$(api_request GET "/panel/api/inbounds/get/$id") || { fail "无法读取 Hy2 入站。"; return 1; }
    inbound=$(jq -ce 'select(.success == true) | .obj | objects' <<<"$response") || { fail "面板未返回 Hy2 入站。"; return 1; }
    # 新版面板返回对象，旧版返回 JSON 字符串；提交时统一为字符串。
    payload=$(jq -c --arg ports "$ports" '
        def as_object: if type == "string" then fromjson else . end;
        def as_string: if type == "string" then . else tojson end;
        del(.clientStats) | .settings |= as_string | .sniffing |= as_string |
        .streamSettings |= (as_object |
            if $ports == "" then
                (if .finalmask.quicParams then .finalmask.quicParams |= del(.udpHop) else . end)
            else .finalmask.quicParams.udpHop = {ports: $ports, interval: "5-10"} end | tojson)' <<<"$inbound") || return 1
    response=$(api_request POST "/panel/api/inbounds/update/$id" "$payload") || { fail "更新 Hy2 入站时面板无响应。"; return 1; }
    jq -e '.success == true' >/dev/null <<<"$response" || {
        fail "面板拒绝更新 Hy2 入站：$(jq -r '.msg // "未知原因"' <<<"$response" 2>/dev/null)"; return 1;
    }
    HY2_HOP_STREAM=$(jq -r '.streamSettings' <<<"$payload")
}

wait_hy2_listening() {
    local port="$1"
    for _ in {1..20}; do
        [[ "$(ss -H -lnup "sport = :${port}")" == *xray* ]] && return 0
        sleep 1
    done
    fail "Hy2 UDP/${port} 未恢复监听。"
}

# 资料文件中的 Hy2 链接带上 mport；ports 为空表示关闭。
apply_hy2_hop_profile() {
    local ports="$1" profile link updated='[]'
    NODE_PROFILES=$(jq -c --arg ports "$ports" --arg stream "$HY2_HOP_STREAM" '
        map(if .key == "hy2" then
            (if $ports == "" then del(.hopPorts) else .hopPorts = $ports end) | .payload.streamSettings = $stream
        else . end)' <<<"$NODE_PROFILES") || return 1
    while IFS= read -r profile; do
        link=$(profile_share_link "$profile") || return 1
        updated=$(jq -c --argjson item "$profile" --arg link "$link" '. + [$item + {link: $link}]' <<<"$updated") || return 1
    done < <(jq -c '.[]' <<<"$NODE_PROFILES")
    NODE_PROFILES="$updated"
    printf '%s\n' "$NODE_PROFILES" >"$RUN_DIR/profiles.json"
}

# 18. Argo 隧道：用 Cloudflare 官方 cloudflared 把本机一个 XHTTP 入站经 Cloudflare 公开，
# 服务器 IP 被封时仍可连接。入站只监听 127.0.0.1，并启用 VLESS Encryption，Cloudflare 看不到代理明文。
ARGO_BIN="/usr/local/lib/3xuish/cloudflared"
ARGO_CONF="/etc/3xuish/argo.conf"
ARGO_ENV="/etc/3xuish/argo.env"
ARGO_SERVICE="3xuish-argo"
ARGO_LOG="/var/log/3xuish-argo.log"
ARGO_RELEASE_API="https://api.github.com/repos/cloudflare/cloudflared/releases/latest"
ARGO_INBOUND_ID=""

argo_profile_file() { printf '%s\n' "$XUI_INFO_DIR/3x-ui-argo.json"; }

argo_conf_value() {
    local key value
    [[ -f "$ARGO_CONF" ]] || return 1
    while IFS='=' read -r key value; do
        if [[ "$key" == "$1" && "$value" =~ ^[A-Za-z0-9.-]{1,253}$ ]]; then printf '%s\n' "$value"; return 0; fi
    done <"$ARGO_CONF"
    return 1
}

cloudflared_asset() {
    case "${ARCH:-$(get_arch)}" in
        amd64) printf 'cloudflared-linux-amd64\n' ;;
        386) printf 'cloudflared-linux-386\n' ;;
        arm64) printf 'cloudflared-linux-arm64\n' ;;
        armv7) printf 'cloudflared-linux-armhf\n' ;;
        *) fail "Cloudflare 未提供此架构的 cloudflared。"; return 1 ;;
    esac
}

install_cloudflared() {
    local asset release tag expected actual version tmp="${ARGO_BIN%/*}/.cloudflared-download"
    if [[ -x "$ARGO_BIN" ]] && "$ARGO_BIN" --version >/dev/null 2>&1; then return 0; fi
    asset=$(cloudflared_asset) || return 1
    release=$(curl -q -fsSL --connect-timeout 10 --max-time 30 --retry 2 \
        -H 'Accept: application/vnd.github+json' "$ARGO_RELEASE_API") || {
        fail "无法从 GitHub 获取 cloudflared 版本信息。$(ipv6_github_hint)"; return 1;
    }
    tag=$(jq -er 'select(.draft == false and .prerelease == false) | .tag_name' <<<"$release") || {
        fail "GitHub 未返回 cloudflared 稳定版本。"; return 1;
    }
    [[ "$tag" =~ ^[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,3}$ ]] || { fail "cloudflared 版本号格式异常：${tag}"; return 1; }
    # 官方在发布说明中列出 SHA256；缺失时使用 GitHub 记录的资产摘要。
    expected=$(jq -r '.body // ""' <<<"$release" | sed -nE "s/^[[:space:]]*${asset}:[[:space:]]*([0-9a-f]{64})[[:space:]]*\$/\1/p")
    expected="${expected%%$'\n'*}"
    if [[ ! "$expected" =~ ^[0-9a-f]{64}$ ]]; then
        expected=$(jq -r --arg a "$asset" '.assets[] | select(.name == $a) | (.digest // "") | sub("^sha256:"; "")' <<<"$release")
    fi
    [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || { fail "未找到 cloudflared 官方校验值，已停止。"; return 1; }
    install -d -m 755 "${ARGO_BIN%/*}" || return 1
    log_info "正在下载 cloudflared ${tag}（${asset}）..."
    curl -q -fsSL --connect-timeout 10 --max-time 300 --retry 2 --max-filesize 209715200 -o "$tmp" \
        "https://github.com/cloudflare/cloudflared/releases/download/$tag/$asset" || {
        rm -f -- "$tmp"; fail "cloudflared 下载失败。"; return 1;
    }
    actual=$(sha256sum "$tmp") || return 1
    [[ "${actual%% *}" == "$expected" ]] || { rm -f -- "$tmp"; fail "cloudflared SHA-256 与官方校验值不符，已停止。"; return 1; }
    chmod 755 "$tmp" || return 1
    version=$("$tmp" --version 2>/dev/null) || { rm -f -- "$tmp"; fail "cloudflared 无法在本机运行。"; return 1; }
    mv -f -- "$tmp" "$ARGO_BIN" || return 1
    log_info "cloudflared 已通过 SHA-256 校验：${version%%$'\n'*}"
}

pick_free_local_port() {
    local port count
    for _ in {1..50}; do
        port=$((20000 + RANDOM % 40000))
        [[ "$port" != "${1:-}" && -z "$(ss -H -lnt "sport = :${port}")" ]] || continue
        count=$(sqlite3 -readonly "$XUI_DB_PATH" "SELECT count(*) FROM inbounds WHERE port=$port;") || return 1
        [[ "$count" == 0 ]] || continue
        printf '%s\n' "$port"
        return 0
    done
    fail "未找到空闲的本地端口。"
}

argo_tunnel_args() {
    local mode="$1" port="$2" metrics="$3"
    # 默认停止前会等待 30 秒排空连接；缩短到 3 秒，重启和关闭不必长时间等待。
    printf 'tunnel --no-autoupdate --edge-ip-version auto --protocol http2 --grace-period 3s --metrics 127.0.0.1:%s' "$metrics"
    if [[ "$mode" == quick ]]; then printf ' --url http://127.0.0.1:%s\n' "$port"; else printf ' run\n'; fi
}

install_argo_service() {
    local mode="$1" port="$2" metrics="$3" args env_line
    args=$(argo_tunnel_args "$mode" "$port" "$metrics")
    env_line="XUI_INSTALL_DIR=$XUI_INSTALL_DIR XUI_DB_PATH=$XUI_DB_PATH XUI_INFO_DIR=$XUI_INFO_DIR XUI_SERVICE=$XUI_SERVICE"
    if hy2_hop_has_systemd; then
        {
            printf '[Unit]\nDescription=3xuish Argo tunnel (cloudflared)\nWants=network-online.target\n'
            printf 'After=network-online.target %s.service\n\n[Service]\nType=simple\n' "$XUI_SERVICE"
            printf 'EnvironmentFile=-%s\nExecStart=%s %s\n' "$ARGO_ENV" "$ARGO_BIN" "$args"
            if [[ "$mode" == quick ]]; then
                # 临时隧道每次启动域名都会变；启动后自动把新域名写回面板与订阅。
                printf 'Environment=%s\nExecStartPost=-/bin/bash %s --argo sync\nTimeoutStartSec=300\n' \
                    "$env_line" "$LOCAL_INSTALLER_COPY"
            fi
            printf 'Restart=on-failure\nRestartSec=5\n\n[Install]\nWantedBy=multi-user.target\n'
        } >"/etc/systemd/system/$ARGO_SERVICE.service" || return 1
        systemctl daemon-reload || return 1
        systemctl enable "$ARGO_SERVICE" >/dev/null 2>&1 || return 1
        systemctl restart "$ARGO_SERVICE" || return 1
    elif hy2_hop_has_openrc; then
        {
            printf '#!/sbin/openrc-run\ndescription="3xuish Argo tunnel (cloudflared)"\n'
            printf 'command="%s"\ncommand_args="%s"\ncommand_background=true\n' "$ARGO_BIN" "$args"
            printf 'pidfile="/run/%s.pid"\noutput_log="%s"\nerror_log="%s"\n' "$ARGO_SERVICE" "$ARGO_LOG" "$ARGO_LOG"
            printf 'depend() {\n    need net\n    after %s\n}\n' "$XUI_SERVICE"
            printf 'start_pre() {\n    if [ -r %s ]; then set -a; . %s; set +a; fi\n}\n' "$ARGO_ENV" "$ARGO_ENV"
            if [[ "$mode" == quick ]]; then
                printf 'start_post() {\n    ( sleep 2; env %s /bin/bash %s --argo sync >>%s 2>&1 ) &\n}\n' \
                    "$env_line" "$LOCAL_INSTALLER_COPY" "$ARGO_LOG"
            fi
        } >"/etc/init.d/$ARGO_SERVICE" || return 1
        chmod 755 "/etc/init.d/$ARGO_SERVICE" || return 1
        rc-update add "$ARGO_SERVICE" default >/dev/null 2>&1 || return 1
        rc-service "$ARGO_SERVICE" restart >/dev/null 2>&1 || return 1
    else
        log_warn "未检测到 systemd 或 OpenRC：隧道已在后台启动，但重启后不会自动恢复。"
        local token=""
        if [[ -r "$ARGO_ENV" ]]; then token=$(sed -n 's/^TUNNEL_TOKEN=//p' "$ARGO_ENV"); fi
        # shellcheck disable=SC2086
        TUNNEL_TOKEN="$token" nohup "$ARGO_BIN" $args </dev/null >>"$ARGO_LOG" 2>&1 &
        printf '%s\n' "$!" >"/run/$ARGO_SERVICE.pid" || return 1
    fi
}

remove_argo_service() {
    local pid
    if hy2_hop_has_systemd && [[ -f "/etc/systemd/system/$ARGO_SERVICE.service" ]]; then
        systemctl disable --now "$ARGO_SERVICE" >/dev/null 2>&1 || true
        rm -f -- "/etc/systemd/system/$ARGO_SERVICE.service"
        systemctl daemon-reload >/dev/null 2>&1 || true
    elif [[ -f "/etc/init.d/$ARGO_SERVICE" ]]; then
        rc-service "$ARGO_SERVICE" stop >/dev/null 2>&1 || true
        rc-update del "$ARGO_SERVICE" default >/dev/null 2>&1 || true
        rm -f -- "/etc/init.d/$ARGO_SERVICE"
    elif [[ -f "/run/$ARGO_SERVICE.pid" ]]; then
        pid=$(cat "/run/$ARGO_SERVICE.pid")
        if [[ "$pid" =~ ^[0-9]+$ && "$(readlink "/proc/$pid/exe" 2>/dev/null)" == "$ARGO_BIN" ]]; then
            kill "$pid" 2>/dev/null || true
            for _ in {1..20}; do [[ -d "/proc/$pid" ]] || break; sleep 0.5; done
            kill -9 "$pid" 2>/dev/null || true
        fi
        rm -f -- "/run/$ARGO_SERVICE.pid"
    fi
}

argo_quick_hostname() {
    local metrics="$1" body host
    for _ in {1..45}; do
        body=$(curl -q -fsS --noproxy '*' --max-time 3 "http://127.0.0.1:$metrics/quicktunnel" 2>/dev/null) || body=""
        host=$(jq -r '.hostname // ""' <<<"$body" 2>/dev/null) || host=""
        if [[ "$host" =~ ^[a-z0-9-]+\.trycloudflare\.com$ ]]; then printf '%s\n' "$host"; return 0; fi
        sleep 2
    done
    return 1
}

argo_wait_ready() {
    for _ in {1..45}; do
        [[ "$(curl -q -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$1/ready")" == 200 ]] && return 0
        sleep 2
    done
    return 1
}

# 经 Cloudflare 访问入站路径：Cloudflare 自身的错误页（502–504、520–530）表示还没连到本机。
# 新域名优先用 DoH 解析，避免本机 DNS 缓存“域名不存在”的结果而影响后续连接。
argo_edge_reachable() {
    local host="$1" path="$2" code="" doh=()
    if curl -q -s --doh-url https://1.1.1.1/dns-query -o /dev/null --max-time 5 https://1.1.1.1/ 2>/dev/null; then
        doh=(--doh-url https://1.1.1.1/dns-query)
    fi
    for _ in {1..30}; do
        code=$(curl -q -s "${doh[@]}" -o /dev/null -w '%{http_code}' --connect-timeout 5 --max-time 10 "https://$host$path" 2>/dev/null) || code=000
        if [[ "$code" =~ ^[1-4][0-9][0-9]$ ]]; then ARGO_EDGE_CODE="$code"; return 0; fi
        sleep 3
    done
    ARGO_EDGE_CODE="$code"
    return 1
}

argo_rollback() {
    remove_argo_service
    if [[ -n "$ARGO_INBOUND_ID" ]]; then
        api_request POST "/panel/api/inbounds/del/$ARGO_INBOUND_ID" '{}' >/dev/null ||
            log_warn "Argo 入站 ID=$ARGO_INBOUND_ID 删除失败，请在面板删除。"
        api_request POST '/panel/api/server/restartXrayService' '{}' >/dev/null || true
        ARGO_INBOUND_ID=""
    fi
    rm -f -- "$ARGO_CONF" "$ARGO_ENV" "$(argo_profile_file)"
    remove_local_installer_copy_if_unused
}

# 生成 Argo 资料：domain 为隧道域名，客户端连接 Cloudflare 的 443 端口。
argo_profile_json() {
    local domain="$1"
    jq -c --arg domain "$domain" '
        .domain = $domain |
        .clientOutbound.settings.address = $domain |
        .clientOutbound.streamSettings.tlsSettings.serverName = $domain |
        .clientOutbound.streamSettings.wsSettings.host = $domain' <<<"$2"
}

argo_write_profile() {
    local profile="$1" link
    link=$(profile_share_link "$profile") || return 1
    jq --arg link "$link" '.link = $link' <<<"$profile" >"$RUN_DIR/argo.json" || return 1
    install -m 600 "$RUN_DIR/argo.json" "$(argo_profile_file)"
}

argo_refresh_outputs() {
    NODE_PROFILES=$(jq -c '.' "$XUI_INFO_DIR/3x-ui-node-profiles.json") || return 1
    printf '%s\n' "$NODE_PROFILES" >"$RUN_DIR/profiles.json" || return 1
    load_ports_from_profiles
    write_node_files || return 1
    refresh_subscription_links || log_warn "统一订阅中的链接未能更新；可运行 --enable-subscription 重新同步。"
}

argo_setup() {
    local mode="$1" token="${ARGO_TOKEN:-}" domain="${ARGO_DOMAIN:-}" port metrics nonce uuid path sub email tag
    local payload response candidate profile host
    [[ -s "$XUI_INFO_DIR/3x-ui-node-profiles.json" ]] || { fail "未找到本脚本的节点资料，Argo 隧道需要先完成四节点部署。"; return 1; }
    if [[ -f "$ARGO_CONF" ]]; then
        fail "Argo 隧道已开启（$(argo_conf_value MODE)）；更换前请先关闭：--argo off。"; return 1
    fi
    if [[ "$mode" == fixed ]]; then
        printf '固定隧道：在 Cloudflare Zero Trust → 网络 → 隧道 创建隧道，复制令牌；公共主机名稍后按提示填写。\n'
        [[ -n "$token" ]] || prompt_read "隧道令牌（token）: " token
        [[ -n "$domain" ]] || prompt_read "公共主机名（例如 argo.example.com）: " domain
        token="${token//[[:space:]]/}"
        [[ "$token" =~ ^[A-Za-z0-9+/=_-]{50,2048}$ ]] || { fail "隧道令牌格式无效。"; return 1; }
        valid_domain "$domain" || { fail "公共主机名格式无效。"; return 1; }
    fi
    load_panel_context_for_api || return 1
    api_init || return 1
    install_cloudflared || return 1
    if [[ "$mode" == quick ]]; then install_local_installer_copy || return 1; fi
    VLESS_ENC_TYPE=1
    generate_vlessenc_keys || return 1
    port=$(pick_free_local_port) || return 1
    metrics=$(pick_free_local_port "$port") || return 1
    install -d -m 700 "${ARGO_CONF%/*}" || return 1
    printf 'MODE=%s\nPORT=%s\nMETRICS=%s\nDOMAIN=%s\n' "$mode" "$port" "$metrics" "${domain:-}" >"$ARGO_CONF" || return 1
    chmod 600 "$ARGO_CONF" || return 1
    if [[ "$mode" == fixed ]]; then
        (umask 077; printf 'TUNNEL_TOKEN=%s\n' "$token" >"$ARGO_ENV") || { argo_rollback; return 1; }
    fi
    run_without_lock_fd install_argo_service "$mode" "$port" "$metrics" || { argo_rollback; fail "Argo 服务启动失败，日志：$ARGO_LOG"; return 1; }
    if [[ "$mode" == quick ]]; then
        log_info "正在申请 Cloudflare 临时隧道域名..."
        host=$(argo_quick_hostname "$metrics") || { argo_rollback; fail "未能获取临时隧道域名，日志：$ARGO_LOG"; return 1; }
        argo_wait_ready "$metrics" || { argo_rollback; fail "临时隧道未能连接 Cloudflare，日志：$ARGO_LOG"; return 1; }
    else
        log_info "正在等待隧道连接 Cloudflare..."
        argo_wait_ready "$metrics" || { argo_rollback; fail "隧道未能连接 Cloudflare，请核对令牌；日志：$ARGO_LOG"; return 1; }
        host="$domain"
    fi
    log_info "隧道域名：$host"

    nonce=$(gen_random_str 16) uuid=$(gen_uuid) path="/$(gen_random_str 12)" sub=$(gen_random_str 20)
    email="3xuish_argo_$nonce" tag="3xuish-$nonce-Argo"
    payload=$(jq -cn --arg tag "$tag" --argjson port "$port" --arg uuid "$uuid" --arg email "$email" \
        --arg sub "$sub" --arg path "$path" --arg dec "$VLESS_DECRYPTION" --arg host "$host" '{
        up: 0, down: 0, total: 0, remark: "3xuish-Argo", enable: true, expiryTime: 0,
        listen: "127.0.0.1", port: $port, protocol: "vless", tag: $tag,
        settings: ({clients: [{id: $uuid, flow: "xtls-rprx-vision", email: $email, limitIp: 0, totalGB: 0,
            expiryTime: 0, enable: true, tgId: 0, subId: $sub, comment: "", reset: 0}],
            decryption: $dec, encryption: "none"} | tojson),
        # 实测 XHTTP 无法穿过 cloudflared 的本机转发（请求挂起至超时），WebSocket 可以。
        streamSettings: ({network: "ws", security: "none",
            wsSettings: {path: $path, host: "", headers: {}}, sockopt: {tcpWindowClamp: 0},
            externalProxy: [{forceTls: "tls", dest: $host, port: 443, remark: ""}]} | tojson),
        sniffing: ({enabled: false, destOverride: ["http", "tls", "quic"], metadataOnly: false, routeOnly: true} | tojson)
    }') || { argo_rollback; return 1; }
    response=$(api_request POST '/panel/api/inbounds/add' "$payload") || { argo_rollback; fail "创建 Argo 入站时面板无响应。"; return 1; }
    ARGO_INBOUND_ID=$(jq -er 'select(.success == true) | .obj.id' <<<"$response") || {
        argo_rollback; fail "面板拒绝创建 Argo 入站：$(jq -r '.msg // "未知原因"' <<<"$response" 2>/dev/null)"; return 1;
    }
    candidate="$RUN_DIR/xray-argo-candidate.json"
    if ! api_request GET '/panel/api/server/getConfigJson' | jq -er '.obj | objects' >"$candidate" ||
       ! (cd "$XUI_INSTALL_DIR" && "$XRAY_BIN" run -test -c "$candidate") >"$RUN_DIR/xray-argo-check.log" 2>&1; then
        argo_rollback; fail "加入 Argo 入站后的 Xray 配置未通过校验，已撤销。"; return 1
    fi
    response=$(api_request POST '/panel/api/server/restartXrayService' '{}') || { argo_rollback; fail "面板未能重启 Xray。"; return 1; }
    for _ in {1..20}; do
        [[ "$(ss -H -lntp "sport = :${port}")" == *xray* ]] && break
        sleep 1
    done
    [[ "$(ss -H -lntp "sport = :${port}")" == *xray* ]] || { argo_rollback; fail "Argo 入站未在 127.0.0.1:${port} 监听。"; return 1; }

    profile=$(jq -cn --arg mode "$mode" --argjson port "$port" --arg uuid "$uuid" --arg email "$email" \
        --arg tag "$tag" --arg path "$path" --arg enc "$VLESS_ENCRYPTION" '{
        key: "argo", label: "Argo（Cloudflare 隧道）", remark: "3xuish-Argo", mode: $mode, port: $port,
        credential: $uuid, email: $email, tag: $tag,
        clientOutbound: {protocol: "vless",
            settings: {address: "", port: 443, id: $uuid, encryption: $enc, flow: "xtls-rprx-vision"},
            # WebSocket 需要 HTTP/1.1 升级，ALPN 只声明 http/1.1。
            streamSettings: {network: "ws", security: "tls",
                wsSettings: {path: $path, host: ""},
                tlsSettings: {serverName: "", fingerprint: "chrome", alpn: ["http/1.1"]}}}
    }') || { argo_rollback; return 1; }
    profile=$(argo_profile_json "$host" "$profile") || { argo_rollback; return 1; }
    argo_write_profile "$profile" || { argo_rollback; return 1; }
    if [[ "$mode" == fixed ]]; then
        printf '\n请在 Cloudflare 隧道的“公共主机名”中添加：%s → 服务类型 HTTP，URL 127.0.0.1:%s\n' "$host" "$port"
        if [[ "${AUTO_YES:-0}" != 1 ]]; then prompt_read "添加完成后按回车继续检查连通性..." payload; fi
    fi
    log_info "正在经 Cloudflare 检查隧道连通性..."
    if argo_edge_reachable "$host" "$path"; then
        log_info "Cloudflare 已能连到本机 Argo 入站（HTTP ${ARGO_EDGE_CODE}）。"
    else
        log_warn "暂未经 Cloudflare 连到本机入站（HTTP ${ARGO_EDGE_CODE:-000}）；固定隧道请核对公共主机名，临时域名可能需要一两分钟生效。"
    fi
    ARGO_INBOUND_ID=""
    argo_refresh_outputs || return 1
    printf '\n%b[Argo 隧道]%b %s\n%s\n' "$CYAN" "$PLAIN" "$host" "$(jq -r '.link' "$(argo_profile_file)")"
    print_qr "$(jq -r '.link' "$(argo_profile_file)")" || true
    if [[ "$mode" == quick ]]; then
        log_warn "临时隧道仅供测试，域名在服务重启后会变化；脚本会自动更新统一订阅，单独导入的链接需重新导入。"
    fi
}

argo_inbound_id() {
    local tag
    tag=$(jq -r '.tag // ""' "$(argo_profile_file)" 2>/dev/null) || return 1
    inbound_id_by_tag "$tag"
}

inbound_id_by_tag() {
    local tag="$1" id
    [[ "$tag" =~ ^[A-Za-z0-9_-]+$ ]] || return 1
    id=$(sqlite3 -readonly "$XUI_DB_PATH" "SELECT id FROM inbounds WHERE tag='$tag';") || return 1
    [[ "$id" =~ ^[0-9]+$ ]] || return 1
    printf '%s\n' "$id"
}

# 临时隧道重启后由服务调用：读取新域名，写回面板入站、资料文件和统一订阅。
argo_sync() {
    local mode metrics host current id response inbound payload profile
    mode=$(argo_conf_value MODE) || { log_info "Argo 隧道未开启。"; return 0; }
    [[ "$mode" == quick && -s "$(argo_profile_file)" ]] || return 0
    metrics=$(argo_conf_value METRICS) || { fail "Argo 配置缺少 metrics 端口。"; return 1; }
    host=$(argo_quick_hostname "$metrics") || { fail "未能获取 Argo 临时隧道域名。"; return 1; }
    current=$(jq -r '.domain // ""' "$(argo_profile_file)")
    if [[ "$host" == "$current" ]]; then log_info "Argo 临时域名未变化：$host"; return 0; fi
    load_panel_context_for_api --no-deps || return 1
    for _ in {1..30}; do
        api_init >/dev/null 2>&1 && break
        sleep 2
    done
    [[ "$API_READY" == 1 ]] || { fail "面板 API 不可用，Argo 域名未更新。"; return 1; }
    id=$(argo_inbound_id) || { fail "面板中未找到 Argo 入站。"; return 1; }
    response=$(api_request GET "/panel/api/inbounds/get/$id") || return 1
    inbound=$(jq -ce 'select(.success == true) | .obj | objects' <<<"$response") || return 1
    payload=$(jq -c --arg host "$host" '
        def as_object: if type == "string" then fromjson else . end;
        def as_string: if type == "string" then . else tojson end;
        del(.clientStats) | .settings |= as_string | .sniffing |= as_string |
        .streamSettings |= (as_object | .externalProxy = [{forceTls: "tls", dest: $host, port: 443, remark: ""}] | tojson)' \
        <<<"$inbound") || return 1
    response=$(api_request POST "/panel/api/inbounds/update/$id" "$payload") || return 1
    jq -e '.success == true' >/dev/null <<<"$response" || { fail "面板拒绝更新 Argo 入站。"; return 1; }
    profile=$(argo_profile_json "$host" "$(jq -c '.' "$(argo_profile_file)")") || return 1
    argo_write_profile "$profile" || return 1
    argo_refresh_outputs || return 1
    log_info "Argo 临时域名已更新：${current:-无} → $host"
}

argo_off() {
    local id
    if [[ ! -f "$ARGO_CONF" && ! -s "$(argo_profile_file)" ]]; then log_info "Argo 隧道未开启。"; return 0; fi
    remove_argo_service
    if [[ -s "$(argo_profile_file)" ]] && id=$(argo_inbound_id); then
        load_panel_context_for_api || return 1
        api_init || return 1
        api_request POST "/panel/api/inbounds/del/$id" '{}' | jq -e '.success == true' >/dev/null || {
            fail "面板未能删除 Argo 入站 ID=${id}；隧道服务已停止，请在面板中删除该入站后重试。"; return 1;
        }
        api_request POST '/panel/api/server/restartXrayService' '{}' >/dev/null || true
    fi
    rm -f -- "$ARGO_CONF" "$ARGO_ENV" "$(argo_profile_file)" "$ARGO_BIN"
    rmdir -- "${ARGO_CONF%/*}" 2>/dev/null || true
    remove_local_installer_copy_if_unused
    if [[ -s "$XUI_INFO_DIR/3x-ui-node-profiles.json" ]]; then argo_refresh_outputs || return 1; fi
    log_info "已关闭 Argo 隧道，删除了隧道服务、cloudflared 和 Argo 入站。"
}

argo_status() {
    local mode metrics state="未运行"
    if ! mode=$(argo_conf_value MODE); then log_info "Argo 隧道未开启。"; return 0; fi
    metrics=$(argo_conf_value METRICS) || metrics=""
    if [[ -n "$metrics" && "$(curl -q -s --noproxy '*' -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:$metrics/ready")" == 200 ]]; then
        state="已连接 Cloudflare"
    fi
    log_info "Argo 隧道：$([[ "$mode" == quick ]] && printf '临时' || printf '固定')；状态：${state}；域名：$(jq -r '.domain // "未知"' "$(argo_profile_file)" 2>/dev/null)"
    if [[ -s "$(argo_profile_file)" ]]; then jq -r '.link' "$(argo_profile_file)"; fi
}

argo_command() {
    case "${1:-}" in
        quick|fixed) argo_setup "$1" ;;
        off) argo_off ;;
        sync) argo_sync ;;
        status) argo_status ;;
        *) fail "--argo 只接受 quick、fixed、off、status。"; return 1 ;;
    esac
}

# 重装/卸载不会处理这些独立服务；提前提示，避免留下指向旧端口的规则或隧道。
print_extra_services_notice() {
    local extras=()
    if [[ -f "$HY2_HOP_CONF" ]]; then extras+=("Hy2 端口跳跃（菜单 17 选 off 关闭）"); fi
    if [[ -f "$XUI_INFO_DIR/3x-ui-argo.json" ]]; then extras+=("Argo 隧道（菜单 18 关闭）"); fi
    if xray_auto_update_enabled; then extras+=("Xray 每日自动更新（菜单 14 关闭）"); fi
    ((${#extras[@]} > 0)) || return 0
    printf '注意：以下功能独立于面板，此操作不会移除，建议先关闭：\n'
    printf '  - %s\n' "${extras[@]}"
}

hy2_hop_command() {
    local arg="${1:-}" profiles_file="$XUI_INFO_DIR/3x-ui-node-profiles.json" start end hy2_port hy2_tag id ports old_conf=""
    case "$arg" in
        status)
            if ports=$(hy2_hop_current_range); then
                if [[ -f "$HY2_HOP_SCRIPT" ]] && /bin/sh "$HY2_HOP_SCRIPT" active; then
                    log_info "Hy2 端口跳跃已开启：UDP ${ports}，规则生效中。"
                else
                    log_warn "Hy2 端口跳跃已配置（UDP ${ports}），但当前未检测到重定向规则；重新运行 --hy2-hop ${ports} 可恢复。"
                fi
            else
                log_info "Hy2 端口跳跃未开启。"
            fi
            return 0 ;;
        off) ports="" ;;
        *)
            [[ "$arg" =~ ^([0-9]{4,5})[-:]([0-9]{4,5})$ ]] || {
                fail "端口范围格式应为 起始-结束，例如 20000-50000；关闭请用 off。"; return 1;
            }
            start=$((10#${BASH_REMATCH[1]})) end=$((10#${BASH_REMATCH[2]}))
            ((start >= 1024 && end <= 65535 && start < end)) || {
                fail "端口范围需在 1024–65535 之间，且起始小于结束。"; return 1;
            }
            ports="$start-$end" ;;
    esac
    [[ -s "$profiles_file" ]] || { fail "未找到本脚本的节点资料 ${profiles_file}。"; return 1; }
    NODE_PROFILES=$(jq -c '.' "$profiles_file") || return 1
    printf '%s\n' "$NODE_PROFILES" >"$RUN_DIR/profiles.json" || return 1
    hy2_port=$(jq -r '.[] | select(.key == "hy2") | .port' <<<"$NODE_PROFILES")
    hy2_tag=$(jq -r '.[] | select(.key == "hy2") | .payload.tag' <<<"$NODE_PROFILES")
    valid_port "$hy2_port" || { fail "资料文件中没有 Hy2 节点。"; return 1; }
    id=$(hy2_inbound_id "$hy2_tag") || { fail "面板中未找到 Hy2 入站 ${hy2_tag}。"; return 1; }
    if [[ -z "$ports" && ! -f "$HY2_HOP_CONF" ]] && ! jq -e '.[] | select(.key == "hy2") | .hopPorts' <<<"$NODE_PROFILES" >/dev/null; then
        log_info "Hy2 端口跳跃未开启，无需关闭。"
        return 0
    fi
    load_panel_context_for_api || return 1
    api_init || return 1
    if [[ -n "$ports" ]]; then
        check_hop_range_free "$start" "$end" "$hy2_port" "$hy2_tag" || return 1
        ensure_nat_backend || return 1
        if [[ -f "$HY2_HOP_CONF" ]]; then old_conf=$(cat "$HY2_HOP_CONF"); fi
        install -d -m 700 "${HY2_HOP_CONF%/*}" || return 1
        printf 'HOP_START=%s\nHOP_END=%s\nHOP_TARGET=%s\nHOP_BACKEND=%s\n' "$start" "$end" "$hy2_port" "$HY2_HOP_BACKEND" \
            >"$HY2_HOP_CONF" || return 1
        chmod 600 "$HY2_HOP_CONF" || return 1
        write_hy2_hop_script || return 1
        if ! run_without_lock_fd install_hy2_hop_service || ! /bin/sh "$HY2_HOP_SCRIPT" active; then
            remove_hy2_hop_service
            if [[ -n "$old_conf" ]]; then printf '%s\n' "$old_conf" >"$HY2_HOP_CONF"; run_without_lock_fd install_hy2_hop_service || true
            else rm -f -- "$HY2_HOP_CONF"; fi
            fail "端口重定向规则未能生效，已撤销本次修改。"; return 1
        fi
        log_info "已添加重定向：UDP ${ports} → ${hy2_port}（${HY2_HOP_BACKEND}）。"
    fi
    if ! update_hy2_inbound_hop "$id" "$ports"; then
        if [[ -n "$ports" && -n "$old_conf" ]]; then
            printf '%s\n' "$old_conf" >"$HY2_HOP_CONF"
            run_without_lock_fd install_hy2_hop_service || log_warn "恢复原端口跳跃规则失败，请运行 --hy2-hop status 检查。"
        elif [[ -n "$ports" ]]; then
            remove_hy2_hop_service
            rm -f -- "$HY2_HOP_CONF"
        fi
        return 1
    fi
    if [[ -z "$ports" ]]; then
        remove_hy2_hop_service
        rm -f -- "$HY2_HOP_CONF" "$HY2_HOP_SCRIPT"
        rmdir -- "${HY2_HOP_CONF%/*}" "${HY2_HOP_SCRIPT%/*}" 2>/dev/null || true
        log_info "已关闭 Hy2 端口跳跃并删除重定向规则。"
    fi
    wait_hy2_listening "$hy2_port" || return 1
    apply_hy2_hop_profile "$ports" || return 1
    load_ports_from_profiles
    write_node_files || return 1
    refresh_subscription_links || log_warn "统一订阅中的链接未能更新；可运行 --enable-subscription 重新同步。"
    if [[ -n "$ports" ]]; then
        log_info "Hy2 端口跳跃已开启：客户端会在 UDP ${ports} 内换端口连接。"
        log_warn "请在云安全组和系统防火墙放行 UDP ${ports}；旧客户端需重新导入 Hy2 链接或刷新订阅。"
        printf '%s\n' "$(jq -r '.[] | select(.key == "hy2") | .link' <<<"$NODE_PROFILES")"
    fi
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

# 15. Xray 内核更新：官方最新稳定版、SHA-256 校验、配置预检，启动失败自动恢复原内核。
xray_installed_version() {
    local output
    output=$("$1" version 2>/dev/null) || return 1
    output="${output%%$'\n'*}"
    [[ "$output" =~ ^Xray[[:space:]]+v?([0-9]+\.[0-9]+\.[0-9]+)([[:space:]]|$) ]] || return 1
    printf '%s\n' "${BASH_REMATCH[1]}"
}

# 第一个版本号严格大于第二个时返回 0；只比较 主.次.修订 三段数字。
xray_version_newer() {
    local left=() right=() index
    IFS=. read -r -a left <<<"$1"
    IFS=. read -r -a right <<<"$2"
    for index in 0 1 2; do
        if ((10#${left[index]:-0} > 10#${right[index]:-0})); then return 0; fi
        if ((10#${left[index]:-0} < 10#${right[index]:-0})); then return 1; fi
    done
    return 1
}

# 按面板内核文件名选择官方发布包，命名与 3x-ui 安装脚本的架构后缀一致。
xray_release_asset() {
    case "${1##*/xray-linux-}" in
        amd64) printf 'Xray-linux-64.zip\n' ;;
        386) printf 'Xray-linux-32.zip\n' ;;
        arm64) printf 'Xray-linux-arm64-v8a.zip\n' ;;
        armv7|arm) printf 'Xray-linux-arm32-v7a.zip\n' ;;
        armv6) printf 'Xray-linux-arm32-v6.zip\n' ;;
        armv5) printf 'Xray-linux-arm32-v5.zip\n' ;;
        s390x) printf 'Xray-linux-s390x.zip\n' ;;
        *) fail "无法识别 Xray 内核架构：${1##*/}"; return 1 ;;
    esac
}

xray_latest_release() {
    local response tag
    response=$(curl -q -fsSL --connect-timeout 10 --max-time 30 --retry 2 \
        -H 'Accept: application/vnd.github+json' "$XRAY_RELEASE_API") || {
        fail "无法从 GitHub 获取 Xray 最新版本，请检查服务器到 api.github.com 的连接；内核未改动。$(ipv6_github_hint)"; return 1;
    }
    tag=$(jq -er 'select(.draft == false and .prerelease == false) | .tag_name' <<<"$response") || {
        fail "GitHub 未返回可用的 Xray 稳定版本；内核未改动。"; return 1;
    }
    # 版本号会拼进下载地址，只接受官方 vX.Y.Z 格式。
    [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || { fail "Xray 版本号格式异常：${tag}；内核未改动。"; return 1; }
    printf '%s\n' "$tag"
}

ensure_unzip() {
    command -v unzip >/dev/null 2>&1 && return 0
    log_info "正在安装 unzip，用于解压 Xray 官方发布包..."
    case "${RELEASE:-}" in
        centos | rhel | almalinux | rocky | fedora | amzn | ol)
            if command -v dnf >/dev/null 2>&1; then dnf install -y -q unzip; else yum install -y -q unzip; fi ;;
        alpine) apk add --no-cache unzip ;;
        arch | manjaro) pacman -Sy --noconfirm unzip ;;
        *) apt-get update -y -q && apt-get install -y -q unzip ;;
    esac || { fail "unzip 安装失败，请手动安装后重试；内核未改动。"; return 1; }
    command -v unzip >/dev/null 2>&1 || { fail "未找到 unzip；内核未改动。"; return 1; }
}

xray_download_verified() {
    local tag="$1" asset="$2" archive="$3" url digest expected actual
    url="$XRAY_DOWNLOAD_BASE/$tag/$asset"
    log_info "正在下载 Xray ${tag}：${asset}"
    curl -q -fsSL --connect-timeout 10 --max-time 300 --retry 2 --max-filesize 209715200 \
        -o "$archive" "$url" || { fail "Xray 发布包下载失败；内核未改动。"; return 1; }
    digest=$(curl -q -fsSL --connect-timeout 10 --max-time 30 --retry 2 "$url.dgst") || {
        fail "Xray 官方校验文件下载失败；内核未改动。"; return 1;
    }
    expected=$(sed -nE 's/^SHA2-256=[[:space:]]*([0-9A-Fa-f]{64})[[:space:]]*$/\1/p' <<<"$digest")
    expected="${expected%%$'\n'*}"
    expected="${expected,,}"
    [[ "$expected" =~ ^[0-9a-f]{64}$ ]] || { fail "官方校验文件中没有 SHA2-256；内核未改动。"; return 1; }
    actual=$(sha256sum "$archive") || return 1
    [[ "${actual%% *}" == "$expected" ]] || {
        fail "Xray 发布包 SHA-256 与官方校验值不符，可能下载损坏或被篡改；内核未改动。"; return 1;
    }
    log_info "发布包 SHA-256 与官方校验值一致。"
}

xray_binary_pid() {
    local proc
    for proc in /proc/[0-9]*; do
        [[ "$(readlink "$proc/exe" 2>/dev/null)" == "$1" ]] || continue
        printf '%s\n' "${proc#/proc/}"
        return 0
    done
    return 1
}

# 只记录 Xray 在配置入站端口上的监听，忽略出站临时 UDP 套接字。
xray_inbound_listeners() {
    local runtime="$1" ports
    [[ -s "$runtime" ]] || return 0
    ports=$(jq -r '[.inbounds[]?.port
        | if type == "number" then tostring
          elif type == "string" and test("^[0-9]+$") then . else empty end]
        | unique | join(" ")' "$runtime") || return 1
    [[ -n "$ports" ]] || return 0
    ss -H -lntup | awk -v ports=" $ports " '
        /users:\(\("xray/ {
            count = split($5, parts, ":")
            if (index(ports, " " parts[count] " ")) print $1, $5
        }' | sort -u
}

# 新内核进程运行、原有入站监听全部恢复，并在 3 秒后仍是同一进程，才算启动成功。
xray_wait_healthy() {
    local target="$1" expected="$2" runtime="$3" pid current line missing
    for _ in {1..60}; do
        sleep 1
        pid=$(xray_binary_pid "$target") || continue
        if [[ -n "$expected" ]]; then
            current=$(xray_inbound_listeners "$runtime") || continue
            missing=0
            while IFS= read -r line; do
                [[ -z "$line" ]] || grep -Fxq -- "$line" <<<"$current" || { missing=1; break; }
            done <<<"$expected"
            [[ "$missing" == 0 ]] || continue
        fi
        sleep 3
        [[ "$(readlink "/proc/$pid/exe" 2>/dev/null)" == "$target" ]] && return 0
    done
    return 1
}

# 启动常驻进程时不把维护锁传下去，否则面板或隧道会一直持有锁，后续维护操作都会被拒绝。
run_without_lock_fd() {
    if [[ -n "${MAINTENANCE_LOCK_FD:-}" ]]; then
        (exec {MAINTENANCE_LOCK_FD}>&-; "$@")
    else
        "$@"
    fi
}

xray_restart_panel() { run_without_lock_fd restart_panel; }

xray_install_file() {
    local source="$1" target="$2"
    XRAY_UPDATE_CANDIDATE="${target%/*}/.3xuish-xray-staged"
    install -m 755 "$source" "$XRAY_UPDATE_CANDIDATE" || return 1
    mv -f -- "$XRAY_UPDATE_CANDIDATE" "$target" || return 1
    XRAY_UPDATE_CANDIDATE=""
}

print_mptcp_hint() {
    local enabled
    enabled=$(sysctl -n net.mptcp.enabled 2>/dev/null) || return 0
    [[ "$enabled" == 1 ]] || return 0
    log_warn "系统仍允许 MPTCP（net.mptcp.enabled=1）。Xray 自 v26.10.31 起默认不开启 MPTCP 监听，但面板等其他 Go 程序可能仍会开启；可用 ss -H -lMn 查看。"
    log_warn "Linux 内核漏洞需通过系统包管理器安装修复后的内核并重启才能根治；当前运行内核：$(uname -r)。"
}

xray_require_binary() {
    local tool
    for tool in curl jq sha256sum ss; do
        command -v "$tool" >/dev/null 2>&1 || { fail "缺少 ${tool}，请先安装后重试；内核未改动。"; return 1; }
    done
    resolve_xray_binary || return 1
    [[ "$XRAY_BIN" == "$XUI_INSTALL_DIR/bin/xray-linux-"* && -f "$XRAY_BIN" && ! -L "$XRAY_BIN" ]] || {
        fail "只支持更新 ${XUI_INSTALL_DIR}/bin 下由面板管理的 Xray 内核：${XRAY_BIN}"; return 1;
    }
}

xray_backup_file() { printf '%s\n' "$XUI_INFO_DIR/3xuish-xray-backup/${XRAY_BIN##*/}.previous"; }

# 下载官方发布包到内核同目录的候选文件：/tmp 可能禁止执行，且同目录 mv 是原子替换。
xray_prepare_release_candidate() {
    local tag="$1" asset archive
    asset=$(xray_release_asset "$XRAY_BIN") || return 1
    ensure_unzip || return 1
    archive="$RUN_DIR/$asset"
    xray_download_verified "$tag" "$asset" "$archive" || return 1
    XRAY_UPDATE_CANDIDATE="${XRAY_BIN%/*}/.3xuish-xray-candidate"
    unzip -p "$archive" xray >"$XRAY_UPDATE_CANDIDATE" || { fail "发布包中没有 xray 程序；内核未改动。"; return 1; }
    chmod 755 "$XRAY_UPDATE_CANDIDATE" || return 1
    [[ "$(xray_installed_version "$XRAY_UPDATE_CANDIDATE")" == "${tag#v}" ]] || {
        fail "新内核无法在本机运行或版本号不符；内核未改动。"; return 1;
    }
}

# 校验候选内核、备份当前内核、替换并重启；新内核启动失败时自动换回原内核。
xray_install_candidate() {
    local current="$1" version="$2" mode="$3" runtime target backup before="" was_running=0
    runtime="$XUI_INSTALL_DIR/bin/config.json"
    if [[ -s "$runtime" ]]; then
        if ! (cd "$XUI_INSTALL_DIR" && "$XRAY_UPDATE_CANDIDATE" run -test -c "$runtime") \
            >"$RUN_DIR/xray-update-test.log" 2>&1; then
            tail -n 20 "$RUN_DIR/xray-update-test.log" >&2 || true
            fail "Xray ${version} 不接受当前节点配置，未更换内核。请先升级 3x-ui 面板或调整上方报错的配置后重试。"
            return 1
        fi
        log_info "Xray ${version} 已通过当前节点配置校验。"
    fi
    target=$(readlink -f -- "$XRAY_BIN") || target="$XRAY_BIN"
    if xray_binary_pid "$target" >/dev/null; then was_running=1; fi
    before=$(xray_inbound_listeners "$runtime") || before=""
    install -d -m 700 "$XUI_INFO_DIR/3xuish-xray-backup" || return 1
    backup=$(xray_backup_file)
    install -m 700 "$XRAY_BIN" "$backup" || { fail "无法备份原内核；内核未改动。"; return 1; }
    mv -f -- "$XRAY_UPDATE_CANDIDATE" "$XRAY_BIN" || { fail "替换内核文件失败；原内核仍在使用。"; return 1; }
    XRAY_UPDATE_CANDIDATE=""
    log_info "已替换内核文件（旧内核备份：${backup}），正在重启 ${XUI_SERVICE}，节点连接会短暂中断..."

    if xray_restart_panel && xray_wait_healthy "$target" "$before" "$runtime"; then
        log_info "Xray 内核已切换：${current:-未知} → ${version}，原有入站监听均已恢复。"
        [[ "$mode" == --auto ]] || print_mptcp_hint
        return 0
    fi
    log_warn "新内核启动后未通过运行检查，正在恢复原内核 ${current:-（版本未知）}..."
    if xray_install_file "$backup" "$XRAY_BIN" && xray_restart_panel &&
       { [[ "$was_running" == 0 ]] || xray_wait_healthy "$target" "$before" "$runtime"; }; then
        fail "已恢复原内核 ${current:-（版本未知）}，本次未切换。请查看 journalctl -u ${XUI_SERVICE} -n 100 --no-pager 排查。"
    else
        fail "恢复原内核后仍未确认运行。请执行：install -m 755 '${backup}' '${XRAY_BIN}' 后重启 ${XUI_SERVICE}，并检查面板日志。"
    fi
    return 1
}

update_xray_core() {
    local mode="${1:-}" tag latest current
    if [[ "$mode" == --auto ]]; then
        RED='' GREEN='' YELLOW='' CYAN='' PLAIN=''
        printf '\n[%s] Xray 自动更新检查\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
        if [[ ! -d "$XUI_INSTALL_DIR/bin" ]]; then
            log_info "未检测到 3x-ui 程序目录 ${XUI_INSTALL_DIR}/bin，本次跳过。"
            return 0
        fi
    fi
    xray_require_binary || return 1
    current=$(xray_installed_version "$XRAY_BIN") || current=""
    tag=$(xray_latest_release) || return 1
    latest="${tag#v}"
    log_info "当前 Xray 内核：${current:-无法识别}；官方最新稳定版：${latest}"
    if [[ -n "$current" ]] && ! xray_version_newer "$latest" "$current"; then
        log_info "Xray 内核已是最新稳定版，无需更新。"
        [[ "$mode" == --auto ]] || print_mptcp_hint
        return 0
    fi
    xray_prepare_release_candidate "$tag" || return 1
    xray_install_candidate "$current" "$latest" "$mode"
}

# 指定版本或回滚后，每日自动更新仍会升级到最新稳定版；提醒用户按需关闭。
xray_pin_notice() {
    local version="$1" tag
    xray_auto_update_enabled || return 0
    tag=$(xray_latest_release 2>/dev/null) || tag=""
    if [[ -z "$tag" ]] || xray_version_newer "${tag#v}" "$version"; then
        log_warn "每日自动更新已开启，下次检查会升级到最新稳定版；要停留在 ${version}，请关闭自动更新（--disable-xray-auto-update 或菜单 14）。"
    fi
}

switch_xray_core() {
    local tag current response
    [[ "${1:-}" =~ ^v?([0-9]{1,4}\.[0-9]{1,4}\.[0-9]{1,4})$ ]] || {
        fail "版本号格式应为 26.10.21（可带 v 前缀）。"; return 1;
    }
    tag="v${BASH_REMATCH[1]}"
    xray_require_binary || return 1
    current=$(xray_installed_version "$XRAY_BIN") || current=""
    if [[ "$current" == "${tag#v}" ]]; then
        log_info "当前 Xray 内核已是 ${current}，无需切换。"
        return 0
    fi
    response=$(curl -q -fsSL --connect-timeout 10 --max-time 30 --retry 2 \
        -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/XTLS/Xray-core/releases/tags/$tag") || {
        fail "官方发布中没有 Xray ${tag}，或暂时无法访问 GitHub；内核未改动。"; return 1;
    }
    if jq -e '.prerelease == true' >/dev/null <<<"$response"; then
        log_warn "Xray ${tag} 是预发布版本，可能不稳定。"
    fi
    log_info "当前 Xray 内核：${current:-无法识别}；目标版本：${tag#v}"
    xray_prepare_release_candidate "$tag" || return 1
    xray_install_candidate "$current" "${tag#v}" manual || return 1
    xray_pin_notice "${tag#v}"
}

# 用上次更新前的备份替换当前内核；当前内核成为新的备份，再执行一次即可换回。
rollback_xray_core() {
    local backup previous current
    xray_require_binary || return 1
    backup=$(xray_backup_file)
    [[ -f "$backup" ]] || { fail "没有可回滚的旧内核：${backup}（只有通过本脚本更新过才会有备份）。"; return 1; }
    previous=$(xray_installed_version "$backup") || { fail "备份内核无法运行：${backup}"; return 1; }
    current=$(xray_installed_version "$XRAY_BIN") || current=""
    if [[ "$previous" == "$current" ]]; then
        log_info "备份内核与当前内核同为 ${current}，无需回滚。"
        return 0
    fi
    log_info "当前 Xray 内核：${current:-无法识别}；回滚到备份版本：${previous}"
    XRAY_UPDATE_CANDIDATE="${XRAY_BIN%/*}/.3xuish-xray-candidate"
    install -m 755 "$backup" "$XRAY_UPDATE_CANDIDATE" || { fail "无法读取备份内核。"; return 1; }
    xray_install_candidate "$current" "$previous" manual || return 1
    xray_pin_notice "$previous"
}

xray_auto_update_enabled() {
    local table
    table=$(crontab -l 2>/dev/null) || return 1
    [[ "$table" == *"$XRAY_AUTO_UPDATE_MARK"* ]]
}

# 定时任务和 Argo 服务运行本地固定副本，不在每次执行时下载远程脚本。
LOCAL_INSTALLER_COPY="$XRAY_AUTO_UPDATE_DIR/install.sh"
install_local_installer_copy() {
    local name value
    [[ -f "$INSTALLER_SOURCE_FILE" && -r "$INSTALLER_SOURCE_FILE" ]] || {
        fail "此功能需要先将脚本保存为文件：curl -fLso /tmp/3xuish-install.sh $REPO_RAW_URL && bash /tmp/3xuish-install.sh"
        return 1
    }
    # 这些值会原样写入 crontab 或服务文件，只接受路径常用字符（crontab 会把 % 当作换行）。
    for name in XUI_INSTALL_DIR XUI_DB_PATH XUI_INFO_DIR XUI_SERVICE; do
        value="${!name}"
        [[ "$value" =~ ^[A-Za-z0-9/._-]+$ ]] || { fail "${name} 含有不支持的字符：${value}"; return 1; }
    done
    bash -n "$INSTALLER_SOURCE_FILE" || { fail "脚本语法检查失败。"; return 1; }
    install -d -m 755 "$XRAY_AUTO_UPDATE_DIR" || return 1
    # 菜单运行时源文件就是这份副本本身，无需复制。
    if [[ "$(readlink -f -- "$INSTALLER_SOURCE_FILE")" != "$(readlink -f -- "$LOCAL_INSTALLER_COPY" 2>/dev/null)" ]]; then
        install -m 700 "$INSTALLER_SOURCE_FILE" "$LOCAL_INSTALLER_COPY.tmp" || return 1
        mv -f -- "$LOCAL_INSTALLER_COPY.tmp" "$LOCAL_INSTALLER_COPY" || return 1
    fi
}

# 只有自动更新和 Argo 临时隧道都不再使用时才删除副本。
remove_local_installer_copy_if_unused() {
    if xray_auto_update_enabled || [[ -f "${ARGO_CONF:-/etc/3xuish/argo.conf}" ]]; then return 0; fi
    rm -f -- "$LOCAL_INSTALLER_COPY"
    rmdir -- "$XRAY_AUTO_UPDATE_DIR" 2>/dev/null || true
}

enable_xray_auto_update() {
    local minute hour table line copy="$LOCAL_INSTALLER_COPY"
    command -v crontab >/dev/null 2>&1 || {
        fail "未找到 crontab，请先安装并启动 cron 服务后重试。"; return 1;
    }
    install_local_installer_copy || return 1
    minute=$((RANDOM % 60))
    hour=$((3 + RANDOM % 3))
    line="$minute $hour * * * /usr/bin/env PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    line+=" XUI_INSTALL_DIR=$XUI_INSTALL_DIR XUI_DB_PATH=$XUI_DB_PATH XUI_INFO_DIR=$XUI_INFO_DIR XUI_SERVICE=$XUI_SERVICE"
    line+=" bash $copy --update-xray --auto >>$XRAY_AUTO_UPDATE_LOG 2>&1 $XRAY_AUTO_UPDATE_MARK"
    table=$(crontab -l 2>/dev/null) || table=""
    {
        if [[ -n "$table" ]]; then grep -vF -- "$XRAY_AUTO_UPDATE_MARK" <<<"$table" || true; fi
        printf '%s\n' "$line"
    } | crontab - || { fail "写入 root 定时任务失败。"; return 1; }
    xray_auto_update_enabled || { fail "定时任务写入后未能确认，请执行 crontab -l 检查。"; return 1; }
    log_info "已开启 Xray 每日自动更新：每天 $(printf '%02d:%02d' "$hour" "$minute")（服务器时区）检查官方最新稳定版。"
    log_info "发现新版本时先校验 SHA-256 和当前配置，再替换内核并重启 ${XUI_SERVICE}；启动失败自动恢复原内核。"
    log_info "定时任务使用本地副本 ${copy}，日志：${XRAY_AUTO_UPDATE_LOG}。立即更新请运行 --update-xray 或在菜单选择 12。"
}

disable_xray_auto_update() {
    local table
    if command -v crontab >/dev/null 2>&1; then
        table=$(crontab -l 2>/dev/null) || table=""
        if [[ "$table" == *"$XRAY_AUTO_UPDATE_MARK"* ]]; then
            { grep -vF -- "$XRAY_AUTO_UPDATE_MARK" <<<"$table" || true; } | crontab - || {
                fail "修改 root 定时任务失败。"; return 1;
            }
        fi
    fi
    if xray_auto_update_enabled; then fail "仍检测到自动更新定时任务，请执行 crontab -l 检查。"; return 1; fi
    remove_local_installer_copy_if_unused
    log_info "已关闭 Xray 自动更新；当前内核保持不变，日志 ${XRAY_AUTO_UPDATE_LOG} 和旧内核备份保留。"
}

fresh_install() {
    if [[ -e "$XUI_DB_PATH" ]]; then
        fail "检测到已有 3x-ui 数据库，已停止新装。清空后全新部署请选择重装；也可选择测速、旧参数修复或添加节点。"
        return 1
    fi
    acquire_maintenance_lock
    install_deps
    detect_server_ip
    check_github_reachable
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
    # 订阅是附加功能：失败时保留已验证的节点，提示稍后重试。
    if ! setup_fresh_subscription; then
        log_warn "统一订阅未能开启；节点不受影响，可稍后运行 --enable-subscription 重试。"
    fi
    verify_running_nodes
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
    if [[ -e "$XUI_INFO_DIR/3xuish-panel-update/latest.json" ]]; then
        command -v python3 >/dev/null 2>&1 || { fail "需 python3 检查面板升级状态，已停止。"; return 1; }
        python3 - "$XUI_INFO_DIR/3xuish-panel-update" <<'PY' || { fail "上次面板升级尚未完成或需要恢复，请先选择菜单24查看结果。"; return 1; }
import json,re,sys
from pathlib import Path
root=Path(sys.argv[1])
pointer=root/'latest.json'
if any(p.is_symlink() for p in (pointer,root,*root.parents)): raise SystemExit(1)
value=json.loads(pointer.read_text())
name=value.get('job') if isinstance(value,dict) else None
if not isinstance(name,str) or not re.fullmatch(r'job-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}',name): raise SystemExit(1)
job=root/name
if job.is_symlink() or (job/'state.json').is_symlink(): raise SystemExit(1)
state=json.loads((job/'state.json').read_text())
status=state.get('status') if isinstance(state,dict) else None
raise SystemExit(not isinstance(status,str) or status not in {'complete','no_update','failed','rolled_back'})
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
        print_extra_services_notice
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
SOCKS_EXIT_SHA256="7ab4939ae63c18f5271dcf4ffc57150028e108b76ecd0f40e601fff6c159ba4b"
SOCKS_EXIT_PROBE_SHA256="cfd1d343de6f3044d3ceb5d2fc04e0c7d83689f0f94c53639a0c4092a96cb9e1"
SOCKS_EXIT_JOB_SHA256="0a31f62833dd82eb08a098c08f3fe85408e652bbb8839b57f2703f59da663a6e"
PROXY_INPUT_SHA256="94a4de635e84fb86b39af316b724c8665ffb6456c6e78954e2dd78a55bea5359"
NODE_OVERVIEW_SHA256="d0f26e1ffd96df69f642349ae38f9b05d385b98818187f293f507e8e540b5073"
PANEL_RELEASE_SHA256="526838fbcb6ade48f8cfba3e5e547422021b45b18b27548fd17bfff82f4cc9fa"
PANEL_UPGRADE_SHA256="959d14b36b6f5ddddc1ef483545d82c26dccf6047256606896d0f445b9066a37"
socks_exit_embedded_helper() {
    case "$1" in
        socks_exit.sh) cat <<'SOCKS_HELPER_PAYLOAD_0'
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
jU2yzlyWIvljCQVJQQlJ0Ffo+1/mejQKt1MAAA==
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
        node_overview.py) cat <<'SOCKS_HELPER_PAYLOAD_4'
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
OZdCwq4UmVSje/U9XrG+fj1aXd6+shqd/U6tA2MNG5T62qfbS0ug9ch6Km7HuiEI/6GUUPlbUrL1cLOz9kjF3jvzRXT+Xufa
s87yza2Hj6LT321tXk5TweuGyH7R65gQLm/mBz9Hx0UkNzwVMXeaqr4MkJwvVpXrOSvMH3OI4NFkPVRPoIUniWs8rcUGqDGV
kbEH1qmEjS40LsajtitVdZE9j2UOEKbk4UHFIP8643vtVhCHvX2jNWoMCeeVYwkZkykHEUEeO2EgGQ9PQpSTC+oZLouicTy/
qKLhs5UI1jBkQbmfg8zMp2wfZ2YetVvYK/FC9gGrFBgKwbiPMsk6KDrBFVGEHKPCn5zOaxsGK5TWdd7KvSCmLeYULuIqSAxs
Kr4JGrgfDz90yMTdgMuZ3moRbTbZd1oIblInCCzpgswP2ZoUGbAd2+alWTsHaEL1eRUFgeTIPlNiEbakJO3xxp/bmyl7rERj
sISeLggxhusLi6l8CaU3pbBOlmrVliLXDuc8qkghkftH82BIbueCkE9zYAxs7M702vXzm1KZ9sTapOELPI8/6z39MPbhEAvc
vdX5dDW5mfM1JPxWu4W4bMo+aUCsdWqrdvLFJvL7abZiUHWy5jsnlPJfRpwibS9n8j9wavEld0Z8a4wlPxVnaJCGKt1+WCJM
Z4nZcww+NrfHMa/N4QcJMF7iVOUflGy0frNz9kH3y42scHGjzJxUJyXOahhuzT5ZFDUJDEbAF9g+Ol7JjH3G2K741rc58vkX
1v3qK3CJSNuitn3j73Kdvb+917l8JT6QNnbXiMnrPjn7EK9j9NmK2GqxyoPeXfXmiWmssib8uSRV0K8nPGmSkvyNSnpYvgOm
5kh3nSAirvLE+6DRD75q5L5dV16CVO9b2DBMLy4mPC6rPLENOnV0rRJEVSsj1+IGu5qs/qAIpnKpQ7+kd76+HS2v9NbXsbeC
+TrQoO2lq91L93gou/E4OvMI85vz19QX5HobD6I1/voivccWnX1/69GXnct/jx6vJBoeZFtODu1yj/thpLM3zPCYd015k/Li
n7ev3I6NNo+OUNmW/zHE7BAd+cHD7eWtx1d7zy5BjB+t/zGDniMnNXh+xBC9s+6Yzto5MGJCqR16NSUO5AF7H7C3cUealMwI
CEIOY9RuHoVerrK8qEQBbn7piz1j5s7tYkJ0++N7FzQTo8J9RrqAosYrxcEZut72G8LmKNJXbgjj18GT1Z2ZNr7nyY7E+phj
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
SOCKS_HELPER_PAYLOAD_4
            ;;
        panel_release.py) cat <<'SOCKS_HELPER_PAYLOAD_5'
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
HVrka+9/AewQt98rRgAA
SOCKS_HELPER_PAYLOAD_5
            ;;
        panel_upgrade.py) cat <<'SOCKS_HELPER_PAYLOAD_6'
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
zu+VSKvAPP4/TvkBY7q0AAA=
SOCKS_HELPER_PAYLOAD_6
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
    for name in socks_exit.sh socks_node_probe.py socks_exit_job.py proxy_input.py node_overview.py panel_release.py panel_upgrade.py; do
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
            node_overview.py) expected="$NODE_OVERVIEW_SHA256" ;;
            panel_release.py) expected="$PANEL_RELEASE_SHA256" ;;
            panel_upgrade.py) expected="$PANEL_UPGRADE_SHA256" ;;
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
    # shellcheck disable=SC2034
    NODE_OVERVIEW_HELPER="$RUN_DIR/node_overview.py"
    PANEL_UPGRADE_HELPER="$RUN_DIR/panel_upgrade.py"
    # shellcheck source=lib/socks_exit.sh
    source "$RUN_DIR/socks_exit.sh"
}

socks_exit_command() {
    socks_exit_load_helpers || return 1
    create_socks_exit_node
}

socks_node_status_command() {
    socks_exit_load_helpers || return 1
    socks_exit_status_command "$@"
}

panel_upgrade_command() {
    local action="$1"
    shift
    socks_exit_load_helpers || return 1
    if [[ "$action" == launch ]]; then
        log_info "检查官方最新稳定版；需要升级时会备份面板和数据库，并短暂重启服务。"
        log_info "保留现有节点、账号、订阅和 Xray 内核；后台任务可在 SSH 断开后继续。"
    fi
    # The detached launcher acquires and inherits the shared locks itself.
    python3 "$PANEL_UPGRADE_HELPER" "$action" --database "$XUI_DB_PATH" \
        --install-dir "$XUI_INSTALL_DIR" --info-dir "$XUI_INFO_DIR" --service "$XUI_SERVICE" "$@"
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
  --proxy-node-status [--json] 查看全部面板节点、订阅地址和最近创建结果
  --add-socks-node           兼容旧命令，等同 --add-proxy-node
  --socks-node-status        兼容旧命令，等同 --proxy-node-status
  --update-panel            升级 3x-ui 面板到官方最新稳定版（保留节点和内核）
  --panel-update-status [--json] 查看最近一次面板升级结果
  --switch-domain 域名       迁移现有节点证书与连接域名
  --enable-subscription      本脚本的节点共用一条订阅链接（含 Clash/Mihomo 格式）
  --show-links               显示订阅、节点链接及二维码（只读）
  --hy2-hop 范围|off|status  Hy2 端口跳跃，例如 --hy2-hop 20000-50000
  --argo quick|fixed|off|status  Cloudflare Argo 隧道（临时或固定；固定可用 ARGO_TOKEN/ARGO_DOMAIN）
  --update-xray              更新 Xray 内核到官方最新稳定版（启动失败自动恢复）
  --switch-xray 版本号        切换 Xray 内核到指定官方版本，例如 26.10.21
  --rollback-xray            回滚到上一次更新前的 Xray 内核
  --enable-xray-auto-update  开启每日自动检查并更新 Xray 内核
  --disable-xray-auto-update 关闭 Xray 自动更新
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
    for name in performance_repair.py clean_reinstall.py socks_exit.sh socks_node_probe.py socks_exit_job.py proxy_input.py node_overview.py panel_release.py panel_upgrade.py; do
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
        log_info "已返回管理菜单。"
    else
        result=$?
        log_warn "操作未完成或部分测试失败（退出码：$result）；请查看上方结果。已返回管理菜单。"
    fi
}

menu_xray_status() {
    local binary version="未检测到" auto="未开启"
    for binary in "$XUI_INSTALL_DIR"/bin/xray-linux-*; do
        [[ -f "$binary" && -x "$binary" ]] || continue
        version=$(xray_installed_version "$binary") || version="无法识别"
        break
    done
    if xray_auto_update_enabled; then auto="已开启"; fi
    printf 'Xray 内核：%s；每日自动更新：%s\n' "$version" "$auto"
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
        menu_xray_status
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
            '  12) 一键更新 Xray 内核（官方最新稳定版，失败自动恢复）' \
            '  13) 开启 Xray 每日自动更新' \
            '  14) 关闭 Xray 自动更新' \
            '  15) 开启统一订阅（一条链接导入全部节点）' \
            '  16) 查看订阅、节点链接与二维码' \
            '  17) Hy2 端口跳跃（开启、修改或关闭）' \
            '  18) Argo 隧道（Cloudflare，IP 被封时备用）' \
            '  19) 切换 Xray 内核到指定版本' \
            '  20) 回滚到上一次更新前的 Xray 内核' \
            '  21) 添加代理出口节点（SOCKS5 / HTTP / HTTPS）' \
            '  22) 查看全部节点与订阅（含创建结果）' \
            '  23) 升级 3x-ui 面板（保留节点、订阅和内核）' \
            '  24) 查看面板升级结果' \
            '  0) 退出'
        choice=""
        if ! menu_read_choice choice '请选择 [0-24]：'; then
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
            12) menu_run_action --update-xray ;;
            13) menu_run_action --enable-xray-auto-update ;;
            14) menu_run_action --disable-xray-auto-update ;;
            15) menu_run_action --enable-subscription ;;
            16) menu_run_action --show-links ;;
            17)
                menu_run_action --hy2-hop status
                domain=""
                if ! menu_read_choice domain 'UDP 端口范围（如 20000-50000；off 关闭；留空取消）：'; then return 0; fi
                if [[ -n "$domain" ]]; then menu_run_action --hy2-hop "$domain"; fi
                ;;
            18)
                menu_run_action --argo status
                printf '%s\n' '  1) 临时隧道（trycloudflare.com，免账号，域名重启后会变）' \
                    '  2) 固定隧道（需要 Cloudflare 账号与托管域名）' '  3) 关闭 Argo 隧道'
                domain=""
                if ! menu_read_choice domain '请选择 [1-3，留空返回]：'; then return 0; fi
                case "$domain" in
                    1) menu_run_action --argo quick ;;
                    2) menu_run_action --argo fixed ;;
                    3) menu_run_action --argo off ;;
                    '') ;;
                    *) log_warn "请输入 1–3。" ;;
                esac
                ;;
            19)
                domain=""
                if ! menu_read_choice domain 'Xray 版本号（如 26.10.21；留空取消）：'; then return 0; fi
                if [[ -n "$domain" ]]; then menu_run_action --switch-xray "$domain"; fi
                ;;
            20) menu_run_action --rollback-xray ;;
            21) menu_run_action --add-proxy-node ;;
            22) menu_run_action --proxy-node-status ;;
            23) menu_run_action --update-panel ;;
            24) menu_run_action --panel-update-status ;;
            '') ;;
            *) log_warn "请输入 0–24。" ;;
        esac
    done
}

main() {
    local action="${1:-}"
    case "$action" in
        --help|-h) [[ $# -eq 1 ]] || { fail "帮助选项不能附带其他参数。"; return 1; }; print_usage; return 0 ;;
        --menu|--install|--add-nodes|--add-proxy-node|--add-socks-node|--update-panel|--reinstall|--reinstall-status|--uninstall|--uninstall-status)
            [[ $# -eq 1 ]] || { fail "${action} 不接受额外参数。"; return 1; } ;;
        --proxy-node-status|--socks-node-status|--panel-update-status)
            [[ $# -eq 1 || ( $# -eq 2 && "$2" == --json ) ]] || {
                fail "${action} 只接受可选的 --json。"; return 1;
            } ;;
        --switch-xray)
            [[ $# -eq 2 ]] || { fail "--switch-xray 需要一个版本号，例如 26.10.21。"; return 1; } ;;
        --enable-xray-auto-update|--disable-xray-auto-update|--enable-subscription|--show-links|--rollback-xray)
            [[ $# -eq 1 ]] || { fail "${action} 不接受额外参数。"; return 1; } ;;
        --switch-domain)
            [[ $# -le 2 ]] || { fail "--switch-domain 只接受一个域名。"; return 1; } ;;
        --hy2-hop)
            [[ $# -eq 2 ]] || { fail "--hy2-hop 需要一个参数：端口范围、off 或 status。"; return 1; } ;;
        --argo)
            [[ $# -eq 2 && "$2" =~ ^(quick|fixed|off|status|sync)$ ]] || {
                fail "--argo 需要一个参数：quick、fixed、off 或 status。"; return 1;
            } ;;
        --update-xray)
            [[ $# -eq 1 || ( $# -eq 2 && "$2" == --auto ) ]] || {
                fail "--update-xray 只接受可选的 --auto。"; return 1;
            } ;;
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
        --proxy-node-status|--socks-node-status) socks_node_status_command "${@:2}" ;;
        --update-panel) panel_upgrade_command launch ;;
        --panel-update-status) panel_upgrade_command status "${@:2}" ;;
        --switch-domain) acquire_maintenance_lock; switch_existing_to_domain "${2:-}" ;;
        --update-xray) acquire_maintenance_lock; update_xray_core "${2:-}" ;;
        --switch-xray) acquire_maintenance_lock; switch_xray_core "$2" ;;
        --rollback-xray) acquire_maintenance_lock; rollback_xray_core ;;
        --enable-subscription) acquire_maintenance_lock; enable_subscription_command ;;
        --show-links) show_links_command ;;
        --hy2-hop)
            if [[ "$2" == status ]]; then
                hy2_hop_command status
            else
                acquire_maintenance_lock; hy2_hop_command "$2"
            fi
            ;;
        --argo)
            if [[ "$2" == status ]]; then
                argo_command status
            else
                acquire_maintenance_lock; argo_command "$2"
            fi
            ;;
        --enable-xray-auto-update) enable_xray_auto_update ;;
        --disable-xray-auto-update) disable_xray_auto_update ;;
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
