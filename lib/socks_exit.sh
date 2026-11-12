# Sourced by the 3xuish installer after verifying this component's checksum.
# shellcheck shell=bash

merge_socks_exit_config() {
    jq -e --slurpfile plan "$2" '
        $plan[0] as $p | $p.inboundTag as $inbound |
        ($p.outbounds | map(.tag)) as $tags |
        def fallback:
            (del(.type, .outboundTag, .balancerTag, .ruleTag) | keys) as $match |
            ($match == []) or ($match == ["network"] and
                ((.network | split(",") | sort) == ["tcp", "udp"]));
        if (($tags | length) != 2 or ($tags | unique | length) != 2
            or any($tags[]; type != "string" or length == 0)) then
            error("invalid exit tags")
        elif any(.outbounds[]; .tag as $tag | $tags | index($tag) != null) then
            error("exit already configured")
        elif any(.routing.rules[]?; (.inboundTag // []) | index($inbound) != null) then
            error("inbound already has an explicit route")
        else
            .outbounds += $p.outbounds |
            (.routing.rules // []) as $rules |
            ([range(0; $rules | length) | select($rules[.] | fallback)][0] // ($rules | length)) as $index |
            .routing.rules = ($rules[:$index] + [
                {type:"field", inboundTag:[$inbound], network:"udp", port:"53", outboundTag:$tags[1]},
                {type:"field", inboundTag:[$inbound], outboundTag:$tags[0]}
            ] + $rules[$index:])
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

proxy_exit_rollback() {
    local rc="$1" recovered inbound_id response restored=0 safe_to_remove=1
    trap - EXIT HUP INT TERM
    [[ "$rc" != 0 && "$attempted_add" == 1 ]] || return "$rc"
    log_warn "创建未完成，正在移除本次新建的节点和路由。"
    inbound_id="$exit_id"
    if [[ -z "$inbound_id" ]]; then
        if recovered=$(proxy_exit_find_inbound); then
            inbound_id=$(jq -r '.id // empty' <<<"$recovered")
            [[ -n "$inbound_id" ]] || safe_to_remove=0
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
    if [[ "$attempted_template" == 1 ]]; then
        if [[ "$safe_to_remove" != 1 ]]; then
            log_warn "未确认入站已移除，保留对应出口规则，防止新入站使用默认出口。备份：$exit_backup"
            return "$rc"
        fi
        if proxy_exit_get_template >"$work/current.json" &&
            jq -e --slurpfile plan "$work/plan.json" '
                $plan[0] as $p | ($p.outbounds | map(.tag)) as $tags |
                .xraySetting | .outbounds |= map(select((.tag // "") as $tag | $tags | index($tag) == null)) |
                .routing.rules |= map(select(
                    ((.inboundTag // []) == [$p.inboundTag] and
                     (.outboundTag as $tag | $tags | index($tag) != null)) | not))
            ' "$work/current.json" >"$work/rollback.json" &&
            jq -jr '.outboundTestUrl // ""' "$work/current.json" >"$work/test-url" &&
            proxy_exit_write_template "$work/rollback.json" "$work/test-url"; then
            restored=1
        fi
        [[ "$restored" == 1 ]] || log_warn "出口模板未能自动回滚；备份：$exit_backup"
    fi
    return "$rc"
}

proxy_exit_validate() {
    if ! (cd "$XUI_INSTALL_DIR" && "$XRAY_BIN" run -test -c "$1") >"$work/xray-check.log" 2>&1; then
        fail "当前 Xray 拒绝此出口或 DNS 配置；未发布未经校验的配置。"
    fi
}

proxy_exit_running() {
    local config="$XUI_INSTALL_DIR/bin/config.json" attempt
    for attempt in {1..5}; do
        if jq -e --slurpfile p "$work/plan.json" --arg tag "$exit_tag" --argjson port "$EXIT_NODE_PORT" '
            any(.inbounds[]?; .tag == $tag and .port == $port) and
            ([$p[0].outbounds[].tag] - [.outbounds[].tag] | length == 0) and
            any(.routing.rules[]?; .inboundTag == [$tag] and .outboundTag == $p[0].outbounds[1].tag) and
            any(.routing.rules[]?; .inboundTag == [$tag] and .outboundTag == $p[0].outbounds[0].tag)
        ' "$config" >/dev/null 2>&1 && [[ -n "$(ss -H -lnt "sport = :$EXIT_NODE_PORT")" ]]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

create_socks_exit_node() (
    local upstream_ip upstream_port upstream_user upstream_password upstream_id exit_ip node_uuid nonce
    local exit_tag exit_id='' exit_remark exit_backup='' attempted_add=0 attempted_template=0
    local work="$RUN_DIR/exit-node" response runtime candidate
    [[ -f "$XUI_DB_PATH" ]] || { fail "请先安装 3x-ui。"; return 1; }
    local tool
    for tool in python3 jq curl sqlite3 ss openssl sha256sum; do
        command -v "$tool" >/dev/null 2>&1 || { fail "缺少依赖：$tool"; return 1; }
    done
    mkdir -m 700 "$work" || return 1
    prompt_read 'SOCKS5 出口 IP：' upstream_ip
    prompt_read 'SOCKS5 端口：' upstream_port
    prompt_read 'SOCKS5 账号：' upstream_user
    if [[ -t 0 ]]; then
        IFS= read -rs -p 'SOCKS5 密码（不回显）：' upstream_password || return 1
    elif [[ -r /dev/tty ]] && { true </dev/tty; } 2>/dev/null; then
        IFS= read -rs -p 'SOCKS5 密码（不回显）：' upstream_password </dev/tty || return 1
    else
        fail "需要交互终端以安全读取 SOCKS5 密码。"; return 1
    fi
    printf '\n'
    upstream_ip=$(python3 -c 'import ipaddress,sys; a=ipaddress.ip_address(sys.argv[1]); assert not (a.is_unspecified or a.is_multicast or "%" in str(a)); print(a)' "$upstream_ip" 2>/dev/null) || {
        fail "请填写有效的 IPv4 或 IPv6 地址。"; return 1;
    }
    valid_port "$upstream_port" || { fail "出口端口必须是 1–65535。"; return 1; }
    upstream_port=$((10#$upstream_port))
    [[ -n "$upstream_user" && -n "$upstream_password" ]] || { fail "SOCKS5 账号和密码不能为空。"; return 1; }

    upstream_id=$(printf '%s\0' "$upstream_ip" "$upstream_port" "$upstream_user" "$upstream_password" | sha256sum) || return 1
    upstream_id="${upstream_id:0:16}"
    exit_tag="exitnode-$upstream_id"
    exit_remark="SOCKS5-$upstream_ip-$upstream_id"
    printf '%s\n' "$upstream_user" "$upstream_password" | jq -Rn \
        --arg host "$upstream_ip" --argjson port "$upstream_port" --arg tag "$exit_tag-socks" '
        input as $user | input as $password |
        {tag:$tag, protocol:"socks", settings:{servers:[{address:$host, port:$port, users:[{user:$user, pass:$password}]}]}}
    ' >"$work/upstream.json" || return 1
    unset upstream_password upstream_user
    log_info "正在验证出口 HTTPS 与 TCP DNS。"
    exit_ip=$(python3 "$SOCKS_EXIT_PROBE" upstream "$work/upstream.json") || return 1

    detect_server_ip || return 1
    resolve_xray_binary || return 1
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
    jq -n --arg tag "$exit_tag" --slurpfile upstream "$work/upstream.json" '
        {inboundTag:$tag, outbounds:[$upstream[0], {tag:($tag+"-dns"), protocol:"dns",
            settings:{rewriteNetwork:"tcp", rewriteAddress:"1.1.1.1", rewritePort:53, rules:[{action:"direct"}]},
            streamSettings:{sockopt:{dialerProxy:$upstream[0].tag}}}]}
    ' >"$work/plan.json" || return 1
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
    trap 'proxy_exit_rollback "$?"' EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
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
    jq -cS --arg tag "$exit_tag" '
        .xraySetting.inbounds = ((.xraySetting.inbounds // []) |
            map(select((.tag // "") != $tag)) | sort_by(.tag // ""))
    ' "$work/original.json" >"$work/original-normalized.json" || return 1
    jq -cS --arg tag "$exit_tag" '
        .xraySetting.inbounds = ((.xraySetting.inbounds // []) |
            map(select((.tag // "") != $tag)) | sort_by(.tag // ""))
    ' "$work/current.json" >"$work/current-normalized.json" || return 1
    if [[ "$(<"$work/original-normalized.json")" != "$(<"$work/current-normalized.json")" ]]; then
        fail "面板模板在创建期间被其他操作修改，请重试。"; return 1
    fi
    jq '.xraySetting' "$work/current.json" >"$work/template.json" &&
        jq -jr '.outboundTestUrl // ""' "$work/current.json" >"$work/test-url" &&
        merge_socks_exit_config "$work/template.json" "$work/plan.json" >"$work/updated.json" || return 1
    attempted_template=1
    proxy_exit_write_template "$work/updated.json" "$work/test-url" || { fail "面板未能应用出口模板。"; return 1; }
    proxy_exit_get_template >"$work/applied.json" || return 1
    jq -e --slurpfile p "$work/plan.json" '
        .xraySetting as $t |
        all($p[0].outbounds[]; . as $outbound | $t.outbounds | index($outbound) != null) and
        any($t.routing.rules[]; .inboundTag == [$p[0].inboundTag] and .outboundTag == $p[0].outbounds[1].tag and .network == "udp" and (.port | tostring) == "53") and
        any($t.routing.rules[]; .inboundTag == [$p[0].inboundTag] and .outboundTag == $p[0].outbounds[0].tag)
    ' "$work/applied.json" >/dev/null || { fail "面板未保存预期的出口和路由；节点保持禁用。"; return 1; }
    api_request POST "/panel/api/inbounds/setEnable/$exit_id" '{"enable":true}' |
        jq -e '.success == true' >/dev/null || { fail "面板未能启用节点。"; return 1; }
    if ! proxy_exit_running; then
        api_request POST '/panel/api/server/restartXrayService' '{}' | jq -e '.success == true' >/dev/null || return 1
        proxy_exit_running || { fail "新入站或路由未在 Xray 生效。"; return 1; }
    fi

    printf '%s\n' "$node_uuid" | jq -Rn --arg host "$CERT_HOST" --argjson port "$EXIT_NODE_PORT" '
        {protocol:"vless",settings:{address:"127.0.0.1",port:$port,id:input,encryption:"none",flow:"xtls-rprx-vision"},
            streamSettings:{network:"tcp",security:"tls",tlsSettings:{serverName:$host,fingerprint:"chrome"}}}
    ' >"$work/client.json" || return 1
    python3 "$SOCKS_EXIT_PROBE" node "$XRAY_BIN" "$XUI_INSTALL_DIR" "$work/client.json" "$exit_ip" || return 1
    response=$(proxy_exit_find_inbound) || return 1
    jq -e --argjson id "$exit_id" '.id == $id and .enable == true' <<<"$response" >/dev/null || return 1
    attempted_add=0
    log_info "节点已创建并通过 DNS 和 HTTPS 验证。"
    printf '面板入站：%s（ID %s，端口 %s）\n实际出口 IP：%s\n' "$exit_remark" "$exit_id" "$EXIT_NODE_PORT" "$exit_ip"
)
