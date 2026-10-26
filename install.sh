#!/usr/bin/env bash
# ==============================================================================
# 3x-ui (MHSanaei) 一键部署与后期域名平滑切换脚本
# 仓库: https://github.com/Iwhrt72/3xuish
# 架构: VLESS + XHTTP + VLESS Encryption (vlessenc) + XTLS-Vision
# 证书: 支持 一键 IP 证书 (默认) / 域名证书 / 后期从 IP 无缝切换至域名
# 适用系统: Debian / Ubuntu / CentOS / RHEL / Rocky / AlmaLinux / Alpine / Arch
# ==============================================================================

set -e

REPO_RAW_URL="https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
PLAIN='\033[0m'

log_info()  { echo -e "${GREEN}[INFO]${PLAIN} $*"; }
log_warn()  { echo -e "${YELLOW}[WARN]${PLAIN} $*"; }
log_error() { echo -e "${RED}[ERROR]${PLAIN} $*"; }

# 兼容管道执行 (curl ... | bash) 的交互式读取函数
prompt_read() {
    local prompt_text="$1"
    local var_name="$2"
    if [[ -t 0 ]]; then
        read -rp "$prompt_text" "$var_name"
    elif [[ -e /dev/tty ]]; then
        read -rp "$prompt_text" "$var_name" </dev/tty
    else
        printf -v "$var_name" '%s' ""
    fi
}

# 1. 检查 root 权限
if [[ $EUID -ne 0 ]]; then
    log_error "请使用 root 用户运行此脚本！(sudo -i)"
    exit 1
fi

# 2. 识别系统发行版与架构
if [[ -f /etc/os-release ]]; then
    # shellcheck disable=SC1091
    source /etc/os-release
    RELEASE=$ID
else
    log_error "无法识别当前操作系统类型！"
    exit 1
fi

get_arch() {
    case "$(uname -m)" in
        x86_64 | x64 | amd64) echo 'amd64' ;;
        i*86 | x86) echo '386' ;;
        armv8* | armv8 | arm64 | aarch64) echo 'arm64' ;;
        armv7* | armv7 | arm) echo 'armv7' ;;
        s390x) echo 's390x' ;;
        *) log_error "不支持的 CPU 架构: $(uname -m)" && exit 1 ;;
    esac
}
ARCH=$(get_arch)

gen_random_str() {
    local len="${1:-12}"
    openssl rand -base64 $((len * 2)) | tr -dc 'a-zA-Z0-9' | head -c "$len"
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
    log_info "正在检查并安装基础依赖组件 (curl, jq, socat, openssl, sqlite3, cron, tar)..."
    case "${RELEASE}" in
        ubuntu | debian | armbian)
            apt-get update -y -q
            apt-get install -y -q curl jq socat openssl sqlite3 cron tar ca-certificates tzdata
            systemctl enable --now cron >/dev/null 2>&1 || true
            ;;
        centos | rhel | almalinux | rocky | fedora | amzn | ol)
            if command -v dnf >/dev/null 2>&1; then
                dnf install -y -q curl jq socat openssl sqlite cronie tar ca-certificates tzdata
            else
                yum install -y -q curl jq socat openssl sqlite cronie tar ca-certificates tzdata
            fi
            systemctl enable --now crond >/dev/null 2>&1 || true
            ;;
        alpine)
            apk update
            apk add --no-cache curl jq socat openssl sqlite dcron tar ca-certificates tzdata bash
            rc-update add dcron default >/dev/null 2>&1 || true
            rc-service dcron start >/dev/null 2>&1 || true
            ;;
        arch | manjaro)
            pacman -Sy --noconfirm curl jq socat openssl sqlite cronie tar ca-certificates tzdata
            systemctl enable --now cronie >/dev/null 2>&1 || true
            ;;
        *)
            apt-get update -y -q && apt-get install -y -q curl jq socat openssl sqlite3 cron tar ca-certificates
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
            ip=$(curl -s4m 4 "$u" | tr -d '[:space:]')
            if [[ "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
                SERVER_IP="$ip"
                break
            fi
        done
    fi
    if [[ -z "$SERVER_IP" ]]; then
        prompt_read "未能自动检测到公网 IPv4，请手动输入服务器 IPv4 地址: " SERVER_IP
    fi
    log_info "检测到服务器公网 IPv4: ${CYAN}${SERVER_IP}${PLAIN}"
}

# 5. 释放 80 端口并确保 acme.sh 就绪
prepare_acme_port80() {
    log_info "正在检查 80 端口占用情况（申请证书需要通过 80 端口验证）..."
    if command -v ss >/dev/null 2>&1 && ss -ltn | awk '{print $4}' | grep -Eq ':80$'; then
        log_warn "检测到 80 端口被占用，尝试临时停止常见 Web/面板服务..."
        systemctl stop nginx caddy apache2 httpd x-ui >/dev/null 2>&1 || true
    fi

    if [[ ! -f ~/.acme.sh/acme.sh ]]; then
        log_info "正在安装 acme.sh..."
        curl -s https://get.acme.sh | sh >/dev/null 2>&1
    fi
    ~/.acme.sh/acme.sh --set-default-ca --server letsencrypt --force >/dev/null 2>&1
}

# 6A. 一键申请 Let's Encrypt IP 短期证书 (6天有效期 + acme.sh 自动续期)
issue_ip_cert() {
    prepare_acme_port80
    CERT_HOST="${SERVER_IP}"
    CERT_DIR="/root/cert/ip"
    CERT_FILE="${CERT_DIR}/fullchain.pem"
    KEY_FILE="${CERT_DIR}/privkey.pem"
    mkdir -p "$CERT_DIR"

    log_info "正在向 Let's Encrypt 申请 IP (${SERVER_IP}) SSL 证书 (shortlived 模式，自动续期)..."
    ~/.acme.sh/acme.sh --issue \
        -d "${SERVER_IP}" \
        --standalone \
        --server letsencrypt \
        --certificate-profile shortlived \
        --days 6 \
        --httpport 80 \
        --force

    local reload_cmd="systemctl restart x-ui 2>/dev/null || rc-service x-ui restart 2>/dev/null || true"
    ~/.acme.sh/acme.sh --installcert --force -d "${SERVER_IP}" \
        --key-file "${KEY_FILE}" \
        --fullchain-file "${CERT_FILE}" \
        --reloadcmd "${reload_cmd}" >/dev/null 2>&1 || true

    if [[ ! -s "$CERT_FILE" || ! -s "$KEY_FILE" ]]; then
        log_error "IP 证书文件生成失败！请确认服务器防火墙/安全组已放行 TCP 80 端口。"
        exit 1
    fi

    chmod 644 "$CERT_FILE"
    chmod 600 "$KEY_FILE"
    ~/.acme.sh/acme.sh --upgrade --auto-upgrade >/dev/null 2>&1 || true
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

    prepare_acme_port80
    CERT_HOST="${domain}"
    CERT_DIR="/root/cert/${domain}"
    CERT_FILE="${CERT_DIR}/fullchain.pem"
    KEY_FILE="${CERT_DIR}/privkey.pem"
    mkdir -p "$CERT_DIR"

    log_info "正在向 Let's Encrypt 申请域名 (${domain}) SSL 证书 (90天有效期，自动续期)..."
    ~/.acme.sh/acme.sh --issue \
        -d "${domain}" \
        --standalone \
        --server letsencrypt \
        --httpport 80 \
        --force

    local reload_cmd="systemctl restart x-ui 2>/dev/null || rc-service x-ui restart 2>/dev/null || true"
    ~/.acme.sh/acme.sh --installcert --force -d "${domain}" \
        --key-file "${KEY_FILE}" \
        --fullchain-file "${CERT_FILE}" \
        --reloadcmd "${reload_cmd}" >/dev/null 2>&1 || true

    if [[ ! -s "$CERT_FILE" || ! -s "$KEY_FILE" ]]; then
        log_error "域名证书生成失败！请确认域名已解析到 ${SERVER_IP}（若开启了 Cloudflare 小黄云请先切为仅 DNS 灰色云朵）且 80 端口已放行。"
        exit 1
    fi

    chmod 644 "$CERT_FILE"
    chmod 600 "$KEY_FILE"
    ~/.acme.sh/acme.sh --upgrade --auto-upgrade >/dev/null 2>&1 || true
    log_info "域名证书申请成功！存放路径: ${CERT_DIR}"
}

# 7. 后期平滑切换：从 IP 证书一键升级/切换为域名证书（保留原节点 UUID / 密钥 / 端口）
switch_existing_to_domain() {
    local target_domain="$1"
    if [[ ! -f /etc/x-ui/x-ui.db ]]; then
        log_error "未检测到已安装的 3x-ui 数据库 (/etc/x-ui/x-ui.db)，请先执行全新安装！"
        exit 1
    fi

    install_deps
    detect_server_ip
    issue_domain_cert "$target_domain"

    log_info "正在更新 3x-ui 面板 HTTPS 证书为域名证书..."
    /usr/local/x-ui/x-ui cert \
        -webCert "${CERT_FILE}" \
        -webCertKey "${KEY_FILE}" >/dev/null 2>&1

    log_info "正在同步更新现有 VLESS+XHTTP 节点的 TLS 证书路径与 SNI (${CERT_HOST})..."
    local inbound_ids
    mapfile -t inbound_ids < <(sqlite3 /etc/x-ui/x-ui.db "SELECT id FROM inbounds WHERE protocol='vless';")

    for id in "${inbound_ids[@]}"; do
        [[ -z "$id" ]] && continue
        local raw_stream
        raw_stream=$(sqlite3 /etc/x-ui/x-ui.db "SELECT stream_settings FROM inbounds WHERE id=${id};")
        if echo "$raw_stream" | jq -e '.security == "tls"' >/dev/null 2>&1; then
            local new_stream
            new_stream=$(echo "$raw_stream" | jq -c \
                --arg sni "$CERT_HOST" \
                --arg cert "$CERT_FILE" \
                --arg key "$KEY_FILE" \
                '.tlsSettings.serverName = $sni | .tlsSettings.certificates[0].certificateFile = $cert | .tlsSettings.certificates[0].keyFile = $key')
            local escaped_stream="${new_stream//\'/\'\'}"
            sqlite3 /etc/x-ui/x-ui.db "UPDATE inbounds SET stream_settings='${escaped_stream}' WHERE id=${id};"
            log_info "已更新入站节点 ID=${id} 的 TLS 证书与 SNI -> ${CERT_HOST}"
        fi
    done

    systemctl restart x-ui 2>/dev/null || rc-service x-ui restart 2>/dev/null
    sleep 2

    if [[ -f /root/3x-ui-xhttp-info.txt ]]; then
        sed -i "s#${SERVER_IP}#${CERT_HOST}#g" /root/3x-ui-xhttp-info.txt
        sed -i "s#/root/cert/ip/#${CERT_DIR}/#g" /root/3x-ui-xhttp-info.txt
    fi

    echo -e "\n${GREEN}============================================================${PLAIN}"
    echo -e "${GREEN}  🎉 已成功将面板与节点无缝切换至域名: ${CERT_HOST}${PLAIN}"
    echo -e "${GREEN}============================================================${PLAIN}"
    echo -e "  新证书路径: ${CERT_FILE}"
    echo -e "  新私钥路径: ${KEY_FILE}"
    if [[ -f /root/3x-ui-xhttp-info.txt ]]; then
        echo -e "\n${CYAN}最新连接信息 (已同步更新 /root/3x-ui-xhttp-info.txt):${PLAIN}"
        cat /root/3x-ui-xhttp-info.txt
    fi
    exit 0
}

# 8. 配置参数初始化（支持环境变量覆盖或交互式回车默认）
init_config() {
    echo -e "\n${CYAN}============================================================${PLAIN}"
    echo -e "${CYAN}  3x-ui (XHTTP + VLESS Encryption + Vision) 一键部署向导    ${PLAIN}"
    echo -e "${CYAN}============================================================${PLAIN}"

    if [[ "${AUTO_YES:-0}" != "1" && -f /etc/x-ui/x-ui.db ]]; then
        echo -e "${YELLOW}检测到当前服务器已安装 3x-ui，请选择操作模式:${PLAIN}"
        echo -e "  ${GREEN}1)${PLAIN} 全新部署 / 覆盖配置 (默认)"
        echo -e "  ${GREEN}2)${PLAIN} 后期配上域名：将现有 IP 证书无缝切换为域名证书 (保留原节点与数据)"
        local run_mode=""
        prompt_read "请选择 [默认: 1]: " run_mode
        if [[ "$run_mode" == "2" ]]; then
            switch_existing_to_domain ""
        fi
    fi

    local def_user="admin"
    local def_pass
    def_pass=$(gen_random_str 12)
    local def_panel_port
    def_panel_port=$(shuf -i 10000-55000 -n 1 2>/dev/null || echo "20530")
    local def_web_path
    def_web_path=$(gen_random_str 14)
    local def_inbound_port="443"
    local def_xhttp_path
    def_xhttp_path="/$(gen_random_str 8)"

    if [[ "${AUTO_YES:-0}" == "1" ]]; then
        CERT_MODE="${CERT_MODE:-1}"
        DOMAIN_NAME="${XUI_DOMAIN:-}"
        PANEL_USER="${XUI_USERNAME:-$def_user}"
        PANEL_PASS="${XUI_PASSWORD:-$def_pass}"
        PANEL_PORT="${XUI_PANEL_PORT:-$def_panel_port}"
        WEB_BASE_PATH="${XUI_WEB_BASE_PATH:-$def_web_path}"
        INBOUND_PORT="${INBOUND_PORT:-$def_inbound_port}"
        XHTTP_PATH="${XHTTP_PATH:-$def_xhttp_path}"
        VLESS_ENC_TYPE="${VLESS_ENC_TYPE:-1}"
        INBOUND_TLS_MODE="${INBOUND_TLS_MODE:-1}"
    else
        echo -e "\n请选择 SSL 证书申请方式:"
        echo -e "  ${GREEN}1)${PLAIN} 一键申请 IP 证书 (默认，无需域名，Let's Encrypt 6天自动续期)"
        echo -e "  ${GREEN}2)${PLAIN} 申请域名证书 (已有解析到本机的域名，Let's Encrypt 90天自动续期)"
        prompt_read "请选择 [默认: 1]: " CERT_MODE
        CERT_MODE="${CERT_MODE:-1}"
        if [[ "$CERT_MODE" == "2" ]]; then
            prompt_read "请输入已解析到本机 (${SERVER_IP}) 的域名: " DOMAIN_NAME
            DOMAIN_NAME="${DOMAIN_NAME// /}"
        fi

        prompt_read "1. 面板管理员账号 [默认: ${def_user}]: " PANEL_USER
        PANEL_USER="${PANEL_USER:-$def_user}"

        prompt_read "2. 面板管理员密码 [默认: ${def_pass}]: " PANEL_PASS
        PANEL_PASS="${PANEL_PASS:-$def_pass}"

        prompt_read "3. 面板访问端口 [默认: ${def_panel_port}]: " PANEL_PORT
        PANEL_PORT="${PANEL_PORT:-$def_panel_port}"

        prompt_read "4. 面板安全路径 WebBasePath [默认: ${def_web_path}]: " WEB_BASE_PATH
        WEB_BASE_PATH="${WEB_BASE_PATH:-$def_web_path}"
        WEB_BASE_PATH="${WEB_BASE_PATH#/}"
        WEB_BASE_PATH="${WEB_BASE_PATH%/}"

        prompt_read "5. VLESS+XHTTP 节点监听端口 [默认: ${def_inbound_port}]: " INBOUND_PORT
        INBOUND_PORT="${INBOUND_PORT:-$def_inbound_port}"

        prompt_read "6. XHTTP 路径 Path [默认: ${def_xhttp_path}]: " XHTTP_PATH
        XHTTP_PATH="${XHTTP_PATH:-$def_xhttp_path}"
        [[ "$XHTTP_PATH" != /* ]] && XHTTP_PATH="/${XHTTP_PATH}"

        echo -e "\n请选择 VLESS Encryption (vlessenc) 密钥算法:"
        echo -e "  ${GREEN}1)${PLAIN} X25519 (默认，兼容性最佳、二维码体积小)"
        echo -e "  ${GREEN}2)${PLAIN} ML-KEM-768 (抗量子加密 Post-Quantum)"
        prompt_read "请选择 [默认: 1]: " VLESS_ENC_TYPE
        VLESS_ENC_TYPE="${VLESS_ENC_TYPE:-1}"

        echo -e "\n请选择节点外层 TLS 模式:"
        echo -e "  ${GREEN}1)${PLAIN} 启用 TLS (使用本次申请的 SSL 证书 + VLESS Encryption 双重保护，默认)"
        echo -e "  ${GREEN}2)${PLAIN} 仅使用 VLESS Encryption 内置加密 (security=none，证书仅用于面板 HTTPS)"
        prompt_read "请选择 [默认: 1]: " INBOUND_TLS_MODE
        INBOUND_TLS_MODE="${INBOUND_TLS_MODE:-1}"
    fi
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

    bash <(curl -Ls https://raw.githubusercontent.com/MHSanaei/3x-ui/main/install.sh) </dev/null

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

    systemctl restart x-ui 2>/dev/null || rc-service x-ui restart 2>/dev/null
    sleep 3
}

# 10. 生成 VLESS Encryption (vlessenc) 密钥对
generate_vlessenc_keys() {
    local xray_bin="/usr/local/x-ui/bin/xray-linux-${ARCH}"
    if [[ ! -x "$xray_bin" ]]; then
        xray_bin=$(find /usr/local/x-ui/bin -maxdepth 1 -name "xray-linux-*" -type f | head -n 1)
    fi

    if [[ ! -x "$xray_bin" ]]; then
        log_error "未找到 Xray 核心二进制文件 (/usr/local/x-ui/bin/xray-linux-*)"
        exit 1
    fi

    log_info "正在调用 Xray 核心生成 VLESS Encryption (vlessenc) 密钥..."
    local vlessenc_out
    vlessenc_out=$("$xray_bin" vlessenc)

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

# 11. 通过 3x-ui 面板 API 自动创建 XHTTP + VLESS Encryption + Vision 入站节点
create_inbound_node() {
    CLIENT_UUID=$(gen_uuid)
    CLIENT_EMAIL="xhttp_$(gen_random_str 6)"
    CLIENT_SUBID=$(gen_random_str 16)
    INBOUND_REMARK="VLESS-XHTTP-Enc-Vision"

    local settings_json
    settings_json=$(jq -cn \
        --arg uuid "$CLIENT_UUID" \
        --arg email "$CLIENT_EMAIL" \
        --arg subId "$CLIENT_SUBID" \
        --arg dec "$VLESS_DECRYPTION" \
        --arg enc "$VLESS_ENCRYPTION" \
        --arg auth "$VLESS_AUTH_LABEL" \
        '{
            clients: [
                {
                    id: $uuid,
                    flow: "xtls-rprx-vision",
                    email: $email,
                    limitIp: 0,
                    totalGB: 0,
                    expiryTime: 0,
                    enable: true,
                    tgId: 0,
                    subId: $subId,
                    comment: "",
                    reset: 0
                }
            ],
            decryption: $dec,
            encryption: $enc,
            selectedAuth: $auth,
            fallbacks: []
        }')

    local stream_json
    if [[ "$INBOUND_TLS_MODE" == "1" ]]; then
        stream_json=$(jq -cn \
            --arg path "$XHTTP_PATH" \
            --arg sni "$CERT_HOST" \
            --arg cert "$CERT_FILE" \
            --arg key "$KEY_FILE" \
            '{
                network: "xhttp",
                security: "tls",
                externalProxy: [],
                tlsSettings: {
                    serverName: $sni,
                    minVersion: "1.2",
                    maxVersion: "1.3",
                    cipherSuites: "",
                    rejectUnknownSni: false,
                    disableSystemRoot: false,
                    enableSessionResumption: false,
                    alpn: ["h2", "http/1.1"],
                    settings: {
                        fingerprint: "chrome",
                        echConfigList: "",
                        pinnedPeerCertSha256: [],
                        verifyPeerCertByName: ""
                    },
                    certificates: [
                        {
                            certificateFile: $cert,
                            keyFile: $key,
                            ocspStapling: 0,
                            oneTimeLoading: false,
                            usage: "encipherment",
                            buildChain: false
                        }
                    ]
                },
                xhttpSettings: {
                    path: $path,
                    host: "",
                    headers: {},
                    scMaxBufferedPosts: 30,
                    scMaxEachPostBytes: "",
                    scStreamUpServerSecs: "20-80",
                    noSSEHeader: false,
                    xPaddingBytes: "100-1000",
                    mode: "auto"
                },
                sockopt: {
                    acceptProxyProtocol: false,
                    tcpFastOpen: false,
                    mark: 0,
                    tproxy: "off",
                    tcpMptcp: false,
                    penetrate: false,
                    domainStrategy: "UseIP",
                    tcpMaxSeg: 1440,
                    dialerProxy: "",
                    tcpKeepAliveInterval: 0,
                    tcpKeepAliveIdle: 300,
                    tcpUserTimeout: 10000,
                    tcpcongestion: "bbr",
                    V6Only: false,
                    tcpWindowClamp: 600,
                    interface: ""
                }
            }')
    else
        stream_json=$(jq -cn \
            --arg path "$XHTTP_PATH" \
            '{
                network: "xhttp",
                security: "none",
                externalProxy: [],
                xhttpSettings: {
                    path: $path,
                    host: "",
                    headers: {},
                    scMaxBufferedPosts: 30,
                    scMaxEachPostBytes: "",
                    scStreamUpServerSecs: "20-80",
                    noSSEHeader: false,
                    xPaddingBytes: "100-1000",
                    mode: "auto"
                }
            }')
    fi

    local sniffing_json
    sniffing_json=$(jq -cn '{
        enabled: true,
        destOverride: ["http", "tls", "quic", "fakedns"],
        metadataOnly: false,
        routeOnly: false
    }')

    local payload
    payload=$(jq -cn \
        --arg remark "$INBOUND_REMARK" \
        --argjson port "$INBOUND_PORT" \
        --arg settings "$settings_json" \
        --arg streamSettings "$stream_json" \
        --arg sniffing "$sniffing_json" \
        '{
            up: 0,
            down: 0,
            total: 0,
            remark: $remark,
            enable: true,
            expiryTime: 0,
            listen: "",
            port: $port,
            protocol: "vless",
            settings: $settings,
            streamSettings: $streamSettings,
            sniffing: $sniffing
        }')

    local base_url="https://127.0.0.1:${PANEL_PORT}/${WEB_BASE_PATH}"
    local api_token
    api_token=$(/usr/local/x-ui/x-ui setting -getApiToken 2>/dev/null | grep -Eo 'apiToken: .+' | awk '{print $2}' || true)

    log_info "正在通过 3x-ui API 创建入站节点 (端口: ${INBOUND_PORT})..."
    local add_resp=""

    if [[ -n "$api_token" ]]; then
        add_resp=$(curl -sk -X POST "${base_url}/panel/api/inbounds/add" \
            -H "Authorization: Bearer ${api_token}" \
            -H "Content-Type: application/json" \
            -d "$payload" || true)
    fi

    # 若无 API Token 或返回失败，回退使用 Cookie 登录方式调用
    if [[ -z "$add_resp" ]] || [[ "$(echo "$add_resp" | jq -r '.success // false' 2>/dev/null)" != "true" ]]; then
        local cookie_jar
        cookie_jar=$(mktemp)
        curl -sk -c "$cookie_jar" -X POST "${base_url}/login" \
            -H "Content-Type: application/x-www-form-urlencoded" \
            --data-urlencode "username=${PANEL_USER}" \
            --data-urlencode "password=${PANEL_PASS}" >/dev/null 2>&1

        add_resp=$(curl -sk -b "$cookie_jar" -X POST "${base_url}/panel/api/inbounds/add" \
            -H "X-Requested-With: XMLHttpRequest" \
            -H "Content-Type: application/json" \
            -d "$payload" || true)
        rm -f "$cookie_jar"
    fi

    if [[ "$(echo "$add_resp" | jq -r '.success // false' 2>/dev/null)" == "true" ]]; then
        log_info "入站节点 [${INBOUND_REMARK}] 创建成功！"
    else
        log_warn "API 返回信息: ${add_resp}"
        log_warn "如果端口 ${INBOUND_PORT} 已存在旧节点，请登录面板检查或手动删除冲突端口。"
    fi
}

# 12. 组装客户端 VLESS 分享链接并打印结果
print_summary() {
    local enc_urlencoded
    enc_urlencoded=$(jq -rn --arg v "$VLESS_ENCRYPTION" '$v|@uri')
    local path_urlencoded
    path_urlencoded=$(jq -rn --arg v "$XHTTP_PATH" '$v|@uri')
    local remark_urlencoded
    remark_urlencoded=$(jq -rn --arg v "${INBOUND_REMARK}-${CLIENT_EMAIL}" '$v|@uri')

    if [[ "$INBOUND_TLS_MODE" == "1" ]]; then
        VLESS_LINK="vless://${CLIENT_UUID}@${CERT_HOST}:${INBOUND_PORT}?type=xhttp&encryption=${enc_urlencoded}&path=${path_urlencoded}&mode=auto&security=tls&fp=chrome&alpn=h2%2Chttp%2F1.1&sni=${CERT_HOST}&flow=xtls-rprx-vision#${remark_urlencoded}"
    else
        VLESS_LINK="vless://${CLIENT_UUID}@${CERT_HOST}:${INBOUND_PORT}?type=xhttp&encryption=${enc_urlencoded}&path=${path_urlencoded}&mode=auto&security=none&flow=xtls-rprx-vision#${remark_urlencoded}"
    fi

    # 保存配置摘要到 /root/3x-ui-xhttp-info.txt
    local info_file="/root/3x-ui-xhttp-info.txt"
    cat > "$info_file" <<EOF
============================================================
  3x-ui + XHTTP + VLESS Encryption + Vision 部署信息
============================================================
[面板信息]
面板地址:     https://${CERT_HOST}:${PANEL_PORT}/${WEB_BASE_PATH}/
管理员账号:   ${PANEL_USER}
管理员密码:   ${PANEL_PASS}
SSL 证书路径: ${CERT_FILE}
SSL 私钥路径: ${KEY_FILE}

[节点参数]
协议 (Protocol):         VLESS
传输 (Network):          xhttp (SplitHTTP)
地址 (Address/SNI):      ${CERT_HOST}
端口 (Port):             ${INBOUND_PORT}
用户 UUID:               ${CLIENT_UUID}
流控 (Flow):             xtls-rprx-vision
XHTTP Path:              ${XHTTP_PATH}
XHTTP Mode:              auto
VLESS Encryption 类型:   ${VLESS_AUTH_LABEL}
服务端 Decryption:       ${VLESS_DECRYPTION}
客户端 Encryption:       ${VLESS_ENCRYPTION}
外层安全 (Security):     $( [[ "$INBOUND_TLS_MODE" == "1" ]] && echo "tls (${CERT_HOST} 证书)" || echo "none (纯 VLESS Encryption)" )

[客户端一键导入链接]
${VLESS_LINK}

[后期平滑切换至域名命令]
bash <(curl -Ls ${REPO_RAW_URL}) --switch-domain 你的域名.com
============================================================
EOF
    chmod 600 "$info_file"

    echo -e ""
    echo -e "${GREEN}============================================================${PLAIN}"
    echo -e "${GREEN}  🎉 部署完成！配置详情如下（已备份至 ${info_file}）${PLAIN}"
    echo -e "${GREEN}============================================================${PLAIN}"
    echo -e "${CYAN}[3x-ui 面板登录信息]${PLAIN}"
    echo -e "  面板访问地址: ${YELLOW}https://${CERT_HOST}:${PANEL_PORT}/${WEB_BASE_PATH}/${PLAIN}"
    echo -e "  管理员账号:   ${GREEN}${PANEL_USER}${PLAIN}"
    echo -e "  管理员密码:   ${GREEN}${PANEL_PASS}${PLAIN}"
    echo -e "  SSL 证书文件: ${CERT_FILE} (acme.sh 自动续期)"
    echo -e ""
    echo -e "${CYAN}[VLESS + XHTTP + VLESS Encryption + Vision 节点信息]${PLAIN}"
    echo -e "  连接地址/SNI: ${GREEN}${CERT_HOST}${PLAIN}"
    echo -e "  端口 (Port):  ${GREEN}${INBOUND_PORT}${PLAIN}"
    echo -e "  UUID:         ${GREEN}${CLIENT_UUID}${PLAIN}"
    echo -e "  流控 (Flow):  ${GREEN}xtls-rprx-vision${PLAIN}"
    echo -e "  传输协议:     ${GREEN}xhttp (path: ${XHTTP_PATH}, mode: auto)${PLAIN}"
    echo -e "  加密算法:     ${GREEN}${VLESS_AUTH_LABEL}${PLAIN}"
    echo -e ""
    echo -e "${CYAN}[客户端 VLESS 分享链接 (支持最新 v2rayN / v2rayNG / OneXray 等)]${PLAIN}"
    echo -e "${YELLOW}${VLESS_LINK}${PLAIN}"
    echo -e ""
    echo -e "${CYAN}[💡 后期如果买了域名，无需重装，运行以下命令即可一键无缝切换]:${PLAIN}"
    echo -e "  ${GREEN}bash <(curl -Ls ${REPO_RAW_URL}) --switch-domain 你的域名.com${PLAIN}"
    echo -e "${GREEN}============================================================${PLAIN}"
}

main() {
    if [[ "${1:-}" == "--switch-domain" ]]; then
        switch_existing_to_domain "${2:-}"
        exit 0
    fi

    install_deps
    detect_server_ip
    init_config
    if [[ "${CERT_MODE:-1}" == "2" ]]; then
        issue_domain_cert "$DOMAIN_NAME"
    else
        issue_ip_cert
    fi
    install_and_config_3xui
    generate_vlessenc_keys
    create_inbound_node
    print_summary
}

main "$@"
