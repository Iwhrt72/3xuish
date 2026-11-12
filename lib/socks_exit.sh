# Sourced by the 3xuish installer after verifying this component's checksum.
# shellcheck shell=bash

build_socks_exit_plan() {
    jq -n --arg tag "$2" --slurpfile upstream "$1" '
        {inboundTag:$tag, outbounds:[$upstream[0], {tag:($tag+"-dns"), protocol:"dns",
            settings:{rewriteNetwork:"tcp", rewriteAddress:"1.1.1.1", rewritePort:53, rules:[{action:"direct"}]},
            streamSettings:{sockopt:{dialerProxy:$upstream[0].tag}}}]}
    '
}

merge_socks_exit_config() {
    jq -e --slurpfile plan "$2" '
        $plan[0] as $p | $p.inboundTag as $inbound |
        ($p.outbounds | map(.tag)) as $tags |
        if (($tags | length) != 2 or ($tags | unique | length) != 2
            or any($tags[]; type != "string" or length == 0)) then
            error("invalid exit tags")
        elif any(.outbounds[]; .tag as $tag | $tags | index($tag) != null) then
            error("exit already configured")
        elif any(.routing.rules[]?; (.inboundTag // []) | index($inbound) != null) then
            error("inbound already has an explicit route")
        elif ((.routing.script // "") != "") then
            error("routing scripts must be reviewed before assigning a dedicated exit")
        else
            (.routing.rules // []) as $rules |
            (.api.tag // "") as $api |
            # 3x-ui normalizes its core API rule to the beginning on save.
            # Keep that inbound-only control route first in the submitted plan.
            def api_rule: $api != "" and .inboundTag == [$api] and .outboundTag == $api;
            [$rules[] | select(api_rule)] as $apiRules |
            [$rules[] | select(api_rule | not)] as $trafficRules |
            # Preserve global deny policy for this node while keeping every
            # original rule, in its original order, for all existing inbounds.
            [.outbounds[] | select(.protocol == "blackhole") | .tag] as $blocked |
            [$rules | to_entries[] |
                select(((.value.inboundTag // []) | length) == 0) |
                select(.value.outboundTag as $tag | $blocked | index($tag) != null) |
                .value + {inboundTag:[$inbound],
                          ruleTag:($inbound + "-deny-" + (.key | tostring))}] as $guards |
            .outbounds += $p.outbounds |
            .routing.rules = ($apiRules + $guards + [
                {type:"field", inboundTag:[$inbound], network:"udp", port:"53", outboundTag:$tags[1]},
                {type:"field", inboundTag:[$inbound], outboundTag:$tags[0]}
            ] + $trafficRules)
        end
    ' "$1"
}

proxy_exit_get_template() {
    api_request POST '/panel/api/xray/' '{}' | jq -e '
        select(.success == true) | .obj | if type == "string" then fromjson else . end |
        .xraySetting |= (if type == "string" then fromjson else . end) |
        select((.xraySetting.outbounds | type) == "array")
    '
}

proxy_exit_write_template() {
    curl "${API_COMMON[@]}" "${API_AUTH[@]}" -X POST \
        --data-urlencode "xraySetting@$1" --data-urlencode "outboundTestUrl@$2" \
        "$API_BASE_URL/panel/api/xray/update" |
        jq -e '.success == true' >/dev/null
}

proxy_exit_find_inbound() {
    api_request GET '/panel/api/inbounds/list' | jq -ce \
        --arg remark "$exit_remark" --argjson port "$EXIT_NODE_PORT" '
        select(.success == true) | [.obj[] | select(.remark == $remark and .port == $port)] |
        if length == 1 then .[0] elif length == 0 then {} else error("ambiguous inbound") end
    '
}

proxy_exit_job_result() {
    [[ -n "${SOCKS_EXIT_JOB_DIR:-}" ]] || return 0
    jq -n --arg status "$1" --arg backup "${exit_backup:-}" --arg id "${exit_id:-}" \
        --arg port "${EXIT_NODE_PORT:-}" --arg name "${exit_remark:-}" --arg ip "${exit_ip:-}" \
        '{status:$status,backup:$backup,inbound_id:$id,port:$port,name:$name,exit_ip:$ip}' \
        >"$SOCKS_EXIT_JOB_DIR/outcome.json.next" &&
        mv -- "$SOCKS_EXIT_JOB_DIR/outcome.json.next" "$SOCKS_EXIT_JOB_DIR/outcome.json"
}

proxy_exit_job_phase() {
    [[ -n "${SOCKS_EXIT_JOB_DIR:-}" ]] || return 0
    python3 "$SOCKS_EXIT_JOB_HELPER" phase "$SOCKS_EXIT_JOB_DIR" "$1"
}

proxy_exit_rollback() {
    local rc="$1" recovered inbound_id response restored=1 safe_to_remove=1
    trap - EXIT
    trap '' HUP INT TERM
    if [[ "$rc" == 0 ]]; then proxy_exit_job_result complete; return 0; fi
    if [[ "$attempted_add" != 1 ]]; then proxy_exit_job_result failed; return "$rc"; fi
    proxy_exit_job_phase rolling_back || true
    log_warn "创建未完成，正在移除本次新建的节点和路由。"
    inbound_id="$exit_id"
    if [[ -z "$inbound_id" ]]; then
        if recovered=$(proxy_exit_find_inbound); then
            inbound_id=$(jq -r '.id // empty' <<<"$recovered")
            [[ -n "$inbound_id" ]] || safe_to_remove=1
        else
            log_warn "无法核对面板节点；请检查名称 ${exit_remark}。"
            safe_to_remove=0
        fi
    fi
    if [[ -n "$inbound_id" ]]; then
        response=$(api_request POST "/panel/api/inbounds/del/$inbound_id" '{}') || response='{}'
        if ! jq -e '.success == true' >/dev/null <<<"$response"; then
            log_warn "面板没有确认删除新节点 ID=${inbound_id}，请人工核对。"
        fi
        if ! recovered=$(proxy_exit_find_inbound) || ! jq -e 'length == 0' >/dev/null <<<"$recovered"; then
            safe_to_remove=0
        fi
    fi
    # A successful delete response/empty database row is not proof that the
    # core has removed the listening handler. Wait for it before compensation.
    if [[ "$safe_to_remove" == 1 ]]; then
        local attempt listener stopped=0
        for attempt in {1..5}; do
            if listener=$(ss -H -lnt "sport = :$EXIT_NODE_PORT") && [[ -z "$listener" ]]; then
                stopped=1
                break
            fi
            sleep 1
        done
        [[ "$stopped" == 1 ]] || safe_to_remove=0
    fi
    if [[ "$attempted_template" == 1 ]]; then
        restored=0
        if [[ "$safe_to_remove" != 1 ]]; then
            log_warn "未确认入站已移除，保留对应出口规则，防止新入站使用默认出口。备份：$exit_backup"
            proxy_exit_job_result manual_recovery_required
            return "$rc"
        fi
        if proxy_exit_get_template >"$work/current.json" &&
            jq -e --slurpfile plan "$work/plan.json" '
                $plan[0] as $p | ($p.outbounds | map(.tag)) as $tags |
                .xraySetting | .outbounds |= map(select((.tag // "") as $tag | $tags | index($tag) == null)) |
                .routing.rules |= map(select((.inboundTag // []) != [$p.inboundTag]))
            ' "$work/current.json" >"$work/rollback.json" &&
            jq -jr '.outboundTestUrl // ""' "$work/current.json" >"$work/test-url" &&
            proxy_exit_write_template "$work/rollback.json" "$work/test-url" &&
            proxy_exit_get_template >"$work/rolled-back.json" &&
            jq -e --slurpfile plan "$work/plan.json" '
                $plan[0] as $p | ($p.outbounds | map(.tag)) as $tags |
                .xraySetting |
                all(.outbounds[]; .tag as $tag | $tags | index($tag) == null) and
                all(.routing.rules[]; (.inboundTag // []) | index($p.inboundTag) == null)
            ' "$work/rolled-back.json" >/dev/null; then
            restored=1
        fi
        [[ "$restored" == 1 ]] || log_warn "出口模板未能自动回滚；备份：$exit_backup"
    fi
    if [[ "$safe_to_remove" == 1 && "$restored" == 1 ]]; then
        proxy_exit_job_result rolled_back
    else
        proxy_exit_job_result manual_recovery_required
    fi
    return "$rc"
}

proxy_exit_validate() {
    if ! (cd "$XUI_INSTALL_DIR" && "$XRAY_BIN" run -test -c "$1") >"$work/xray-check.log" 2>&1; then
        fail "当前 Xray 拒绝此出口或 DNS 配置；未发布未经校验的配置。"
    fi
}

proxy_exit_running() {
    local attempt inbound
    for attempt in {1..5}; do
        # Hot-added inbounds need not be present in the core startup file.
        # This is only a readiness check; the real client probe below proves use.
        if inbound=$(proxy_exit_find_inbound) &&
            jq -e --argjson id "$exit_id" --arg tag "$exit_tag" --argjson port "$EXIT_NODE_PORT" '
                .id == $id and .enable == true and .tag == $tag and .port == $port
            ' <<<"$inbound" >/dev/null && [[ -n "$(ss -H -lnt "sport = :$EXIT_NODE_PORT")" ]]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

proxy_exit_read_private() {
    if [[ -t 0 ]]; then
        IFS= read -rs -p "$1" "${2?}" || return 1
    elif [[ -r /dev/tty ]] && { true </dev/tty; } 2>/dev/null; then
        IFS= read -rs -p "$1" "${2?}" </dev/tty || return 1
    else
        fail "需要交互终端以安全读取代理资料。"; return 1
    fi
    printf '\n'
}

proxy_exit_choose_protocol() {
    local protocol_choice
    prompt_read '代理协议 [1 SOCKS5 / 2 HTTP CONNECT / 3 HTTPS CONNECT，默认1]：' protocol_choice
    case "${protocol_choice:-1}" in
        1) printf -v "$1" '%s' socks5 ;;
        2) printf -v "$1" '%s' http ;;
        3) printf -v "$1" '%s' https ;;
        *) fail "请选择 1、2 或 3。"; return 1 ;;
    esac
}

proxy_exit_collect_upstream() {
    local mode layout_choice layout upstream_protocol raw upstream_host upstream_port upstream_user upstream_password
    prompt_read '输入方式 [1 分项填写 / 2 整行粘贴，默认1]：' mode
    case "${mode:-1}" in
        1)
            proxy_exit_choose_protocol upstream_protocol || return 1
            prompt_read '代理地址（IP 或域名）：' upstream_host
            prompt_read '代理端口：' upstream_port
            prompt_read '代理账号（无认证留空）：' upstream_user
            proxy_exit_read_private '代理密码（无认证留空，不回显）：' upstream_password || return 1
            printf '%s\n' "$upstream_host" "$upstream_port" "$upstream_user" "$upstream_password" |
                jq -Rn '{host:input,port:input,username:input,password:input}' |
                python3 "$PROXY_INPUT_HELPER" fields --protocol "$upstream_protocol" >"$1" || return 1
            ;;
        2)
            printf '%s\n' '格式：0 自动识别；1 host:port:user:pass；2 协议://user:pass@host:port；3 user:pass@host:port；4 host:port@user:pass'
            prompt_read '排列格式 [默认0]：' layout_choice
            case "${layout_choice:-0}" in
                0) layout=auto ;; 1) layout=colon ;; 2) layout=url ;;
                3) layout=userinfo ;; 4) layout=endpoint ;;
                *) fail "请选择 0–4。"; return 1 ;;
            esac
            proxy_exit_read_private '粘贴代理资料（包含密码，不回显）：' raw || return 1
            upstream_protocol=auto
            if [[ "$raw" != *'://'* ]]; then
                proxy_exit_choose_protocol upstream_protocol || return 1
            fi
            printf '%s' "$raw" | python3 "$PROXY_INPUT_HELPER" parse \
                --protocol "$upstream_protocol" --layout "$layout" >"$1" || return 1
            ;;
        *) fail "请选择 1 或 2。"; return 1 ;;
    esac
}

create_socks_exit_node() (
    local job response work="$RUN_DIR/exit-node" tool
    [[ -f "$XUI_DB_PATH" ]] || { fail "请先安装 3x-ui。"; return 1; }
    for tool in python3 jq curl sqlite3 ss openssl sha256sum; do
        command -v "$tool" >/dev/null 2>&1 || { fail "缺少依赖：$tool"; return 1; }
    done
    mkdir -m 700 "$work" || return 1
    proxy_exit_collect_upstream "$work/upstream.json" || return 1
    response=$(python3 "$SOCKS_EXIT_JOB_HELPER" launch \
        --installer "$INSTALLER_SOURCE_FILE" --helper "${BASH_SOURCE[0]}" \
        --probe "$SOCKS_EXIT_PROBE" --request "$work/upstream.json" \
        --info-dir "$XUI_INFO_DIR" --database "$XUI_DB_PATH" \
        --install-dir "$XUI_INSTALL_DIR" --service "$XUI_SERVICE" \
        --lock-fd "$MAINTENANCE_LOCK_FD") || return 1
    job=$(jq -er '.job_directory' <<<"$response") || return 1
    log_info "创建任务已启动，SSH 断开后仍会继续。任务目录：$job"
    log_info "可用 --proxy-node-status 查询结果。"
    response=$(python3 "$SOCKS_EXIT_JOB_HELPER" wait "$job" --timeout 60) || return 1
    printf '%s\n' "$response"
    case "$(jq -r '.status' <<<"$response")" in
        complete|starting|running) return 0 ;;
        *) return 1 ;;
    esac
)

socks_exit_status_command() {
    python3 "$SOCKS_EXIT_JOB_HELPER" status --info-dir "$XUI_INFO_DIR"
}

create_socks_exit_node_worker() (
    local exit_ip node_uuid nonce upstream_ip upstream_id upstream_label
    local exit_tag exit_id='' exit_remark exit_backup='' attempted_add=0 attempted_template=0
    local work="$RUN_DIR/exit-node" response runtime candidate
    mkdir -m 700 "$work" || return 1
    cp -- "$1" "$work/upstream.json" || return 1
    exit_tag=$(jq -er '.tag | strings | rtrimstr("-socks")' "$work/upstream.json") || return 1
    upstream_ip=$(jq -er '.settings.servers[0].address' "$work/upstream.json") || return 1
    upstream_id="${exit_tag#exitnode-}"
    upstream_label=$(jq -er 'if .protocol == "socks" then "SOCKS5"
        elif .protocol == "http" and .streamSettings.security == "tls" then "HTTPS"
        elif .protocol == "http" then "HTTP" else error("unsupported proxy protocol") end' "$work/upstream.json") || return 1
    exit_remark="$upstream_label-$upstream_ip-$upstream_id"
    trap 'proxy_exit_rollback "$?"' EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    proxy_exit_job_phase checking_upstream || return 1
    log_info "正在验证出口 HTTPS 与 TCP DNS。"
    resolve_xray_binary || return 1
    exit_ip=$(python3 "$SOCKS_EXIT_PROBE" upstream "$work/upstream.json" "$XRAY_BIN" "$XUI_INSTALL_DIR") || return 1

    proxy_exit_job_phase preparing_panel || return 1
    detect_server_ip || return 1
    load_existing_panel_context || return 1
    api_init || return 1
    proxy_exit_get_template >"$work/original.json" || { fail "面板没有可用的 Xray 模板接口。"; return 1; }
    jq -e --arg tag "$exit_tag-socks" 'any(.xraySetting.outbounds[]; .tag == $tag) | not' "$work/original.json" >/dev/null || {
        fail "此出口已创建，请在面板核对 $exit_remark。"; return 1;
    }
    response=$(api_request GET '/panel/api/inbounds/list') || return 1
    jq -e --arg name "$exit_remark" '.success == true and (any(.obj[]; .remark == $name) | not)' <<<"$response" >/dev/null || {
        fail "检测到同名入站，请在面板核对后再继续。"; return 1;
    }
    EXISTING_SELECTED_TCP='|'
    existing_select_node_port EXIT_NODE_PORT 26600 tcp || return 1
    node_uuid=$(gen_uuid) || return 1
    nonce=$(openssl rand -hex 10) || return 1
    build_socks_exit_plan "$work/upstream.json" "$exit_tag" >"$work/plan.json" || return 1
    printf '%s\n' "$node_uuid" | jq -Rn --arg tag "$exit_tag" --arg name "$exit_remark" \
        --argjson port "$EXIT_NODE_PORT" --arg nonce "$nonce" --arg host "$CERT_HOST" \
        --arg cert "$CERT_FILE" --arg key "$KEY_FILE" '
        input as $uuid | {up:0, down:0, total:0, expiryTime:0, listen:"", protocol:"vless", enable:false,
            port:$port, tag:$tag, remark:$name,
            settings:({decryption:"none", encryption:"none", clients:[{id:$uuid, email:("exitnode_"+$nonce),
                flow:"xtls-rprx-vision", subId:$nonce, enable:true, limitIp:0, totalGB:0, expiryTime:0, reset:0}]} | tojson),
            streamSettings:({network:"tcp", security:"tls", tcpSettings:{header:{type:"none"}},
                tlsSettings:{serverName:$host, minVersion:"1.2", alpn:["h2","http/1.1"],
                    certificates:[{certificateFile:$cert,keyFile:$key,usage:"encipherment"}],
                    settings:{fingerprint:"chrome"}}} | tojson),
            sniffing:({enabled:false,routeOnly:true} | tojson)}
    ' >"$work/inbound.json" || return 1

    runtime="$work/runtime.json"
    candidate="$work/candidate.json"
    api_request GET '/panel/api/server/getConfigJson' | jq -e 'select(.success == true) | .obj | objects' >"$runtime" || return 1
    merge_socks_exit_config "$runtime" "$work/plan.json" | jq -e --slurpfile new "$work/inbound.json" '
        .inbounds += [$new[0] | {tag,port,listen,protocol,settings,streamSettings,sniffing} |
            .settings |= fromjson | .streamSettings |= fromjson | .sniffing |= fromjson]
    ' >"$candidate" || return 1
    proxy_exit_validate "$candidate" || return 1

    install -d -m 700 "$XUI_INFO_DIR" || return 1
    exit_backup="$XUI_INFO_DIR/3x-ui-before-socks-exit-$(date -u +%Y%m%dT%H%M%SZ)-$$.db"
    python3 - "$XUI_DB_PATH" "$exit_backup" <<'PY' || return 1
import sqlite3
import sys

with sqlite3.connect(sys.argv[1], timeout=10) as source, sqlite3.connect(sys.argv[2], timeout=10) as backup:
    source.backup(backup)
PY
    chmod 600 "$exit_backup" || return 1
    log_info "已备份面板数据库：$exit_backup"
    if [[ -n "${SOCKS_EXIT_JOB_DIR:-}" ]]; then : >"$SOCKS_EXIT_JOB_DIR/mutating"; fi
    proxy_exit_job_phase creating_disabled_inbound || return 1
    attempted_add=1
    response=$(api_request POST '/panel/api/inbounds/add' "$(<"$work/inbound.json")") || return 1
    exit_id=$(jq -er 'select(.success == true) | .obj.id | numbers' <<<"$response") || {
        fail "面板未确认创建入站。"; return 1;
    }
    response=$(proxy_exit_find_inbound) || return 1
    exit_tag=$(jq -er --argjson id "$exit_id" 'select(.id == $id and .enable == false) | .tag | strings | select(length > 0)' <<<"$response") || {
        fail "面板未能回读为禁用的新入站。"; return 1;
    }
    jq --arg tag "$exit_tag" '.inboundTag = $tag' "$work/plan.json" >"$work/actual-plan.json" &&
        mv -- "$work/actual-plan.json" "$work/plan.json" || return 1
    proxy_exit_get_template >"$work/current.json" || return 1
    jq -cS '
        {xraySetting: (.xraySetting | del(.inbounds)),
         outboundTestUrl: (.outboundTestUrl // "")}
    ' "$work/original.json" >"$work/original-normalized.json" || return 1
    jq -cS '
        {xraySetting: (.xraySetting | del(.inbounds)),
         outboundTestUrl: (.outboundTestUrl // "")}
    ' "$work/current.json" >"$work/current-normalized.json" || return 1
    if [[ "$(<"$work/original-normalized.json")" != "$(<"$work/current-normalized.json")" ]]; then
        fail "面板模板在创建期间被其他操作修改，请重试。"; return 1
    fi
    jq '.xraySetting' "$work/current.json" >"$work/template.json" &&
        jq -jr '.outboundTestUrl // ""' "$work/current.json" >"$work/test-url" &&
        merge_socks_exit_config "$work/template.json" "$work/plan.json" >"$work/updated.json" || return 1
    attempted_template=1
    proxy_exit_job_phase applying_template || return 1
    proxy_exit_write_template "$work/updated.json" "$work/test-url" || { fail "面板未能应用出口模板。"; return 1; }
    proxy_exit_get_template >"$work/applied.json" || return 1
    jq -e --slurpfile p "$work/plan.json" --slurpfile desired "$work/updated.json" '
        .xraySetting as $t |
        all($p[0].outbounds[]; . as $outbound | $t.outbounds | index($outbound) != null) and
        $t.routing.rules == $desired[0].routing.rules and
        ($t.routing.script // "") == ""
    ' "$work/applied.json" >/dev/null || { fail "面板未保存预期的出口和路由；节点保持禁用。"; return 1; }
    proxy_exit_job_phase enabling_inbound || return 1
    api_request POST "/panel/api/inbounds/setEnable/$exit_id" '{"enable":true}' |
        jq -e '.success == true' >/dev/null || { fail "面板未能启用节点。"; return 1; }
    proxy_exit_running || { fail "新入站未启用或未监听；正在回滚本次创建。"; return 1; }

    printf '%s\n' "$node_uuid" | jq -Rn --arg host "$CERT_HOST" --argjson port "$EXIT_NODE_PORT" '
        {protocol:"vless",settings:{address:"127.0.0.1",port:$port,id:input,encryption:"none",flow:"xtls-rprx-vision"},
            streamSettings:{network:"tcp",security:"tls",tlsSettings:{serverName:$host,fingerprint:"chrome"}}}
    ' >"$work/client.json" || return 1
    proxy_exit_job_phase verifying_node || return 1
    exit_ip=$(python3 "$SOCKS_EXIT_PROBE" node "$XRAY_BIN" "$XUI_INSTALL_DIR" "$work/client.json" "$exit_ip") || return 1
    response=$(proxy_exit_find_inbound) || return 1
    jq -e --argjson id "$exit_id" '.id == $id and .enable == true' <<<"$response" >/dev/null || return 1
    attempted_add=0
    log_info "节点已创建并通过 DNS 和 HTTPS 验证。"
    printf '面板入站：%s（ID %s，端口 %s）\n实际出口 IP：%s\n' "$exit_remark" "$exit_id" "$EXIT_NODE_PORT" "$exit_ip"
)
