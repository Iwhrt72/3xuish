# SOCKS5 出口节点代码审查与修复记录

审查日期：2026-11-12（America/Los_Angeles）。审查基线：`efaf065d47615a062d63997a85f83269b65034b0`。

本文记录该基线的缺陷、证据、修复要求及后续验证结果。原始开发经过见 [开发记录](2026-11-12_开发记录-socks-exit-report.md)。审查和修复使用本地测试与隔离容器，未连接生产服务器。

## 审查结论

基线存在三项 P1、一项 P2，以及一项条件性 P2。现有单元测试通过不足以证明真实创建事务正确。新增节点仍是 VLESS TCP + TLS + Vision，原 REALITY/26560 节点不会因此自动修复；服务器回环验证也不代表手机公网验收。

| 编号 | 优先级 | 证据 Evidence | 问题 Finding | 修复路径 Path |
| --- | --- | --- | --- | --- |
| F1 | P1 | 官方 v3.11.0 容器中，启用后端口监听且 PID=83，磁盘无入站；原检查返回 false，重启后 PID=146 才通过 | 把磁盘启动配置当成热更新运行态，误触发全局重启 | 使用启用状态、监听与协议探测验证，删除基于旧文件的强制重启 |
| F2 | P1 | Linux 向测试进程组发送 HUP，原回滚报 `current.json: No such file or directory` | 父进程清理共享目录，破坏子事务回滚 | 独立会话任务持有文件、锁和持久状态，支持断连后查询 |
| F3 | P1 | 原合并函数保留全局 TCP/direct、UDP/53/direct、域名分流在节点规则之前 | 专属出口被旧规则抢先匹配，DNS 回包测试不能发现错误出口 | 明确节点路由优先级并保留安全阻断；检测不能安全处理的规则；验证真实 DNS 转发 |
| F4 | P2 | 历史 a6711d1 入口配合本地分支当前 helper，校验失败 | 固定入口提交仍下载可变分支组件，旧命令失效 | 将组件与入口一起固定，自动同步并检查哈希 |
| F5 | P2，条件性 | DNS/HTTPS 均成功，仅前后出口 IP 不同即触发失败 | 轮换 SOCKS 出口被当成创建失败而回滚 | 默认支持轮换，静态出口要求采用明确的严格模式 |

## 缺陷详情与复现证据

### F1：热启用成功后强制重启

基线 `lib/socks_exit.sh:106–114` 检查 `bin/config.json`，`259–260` 在检查失败后调用 `restartXrayService`。创建时先保存模板，但新入站仍禁用；随后 3x-ui 通过 gRPC 热启用普通 VLESS 入站，并不同时刷新启动文件。

在一次性官方 `ghcr.io/mhsanaei/3x-ui:v3.11.0` 容器中，创建禁用入站、添加实际 SOCKS/DNS 模板、启用入站并调用仓库原检查函数，得到：

```text
HOT_ENABLED tag=audit-inbound pid_before=83 pid_after=83
LISTEN 0 4096 *:26600 *:*
disk_contains_enabled_inbound=false
CHECKER=false
AFTER_FIVE_SECONDS pid=83，端口仍监听
AFTER_SCRIPT_FORCED_RESTART pid=146
CHECKER_AFTER_RESTART=true
```

这证明重启由错误的检查条件触发。生产记录中的六秒间隔与这条路径一致，但不能据此断言现场 SSH 的全部因果。

上游依据：[Local.AddInbound](https://github.com/MHSanaei/3x-ui/blob/v3.11.0/internal/web/runtime/local.go#L62)、[SetInboundEnable](https://github.com/MHSanaei/3x-ui/blob/v3.11.0/internal/web/service/inbound.go#L1762)。

### F2：断连破坏回滚

基线 `lib/socks_exit.sh:218–221` 设置子 shell 的退出和信号陷阱；父进程 `install.sh:947–948` 在退出时删除共同使用的 `$RUN_DIR`。保留原 `cleanup_install` 和 `proxy_exit_rollback`，仅模拟 API，发送进程组 HUP 后稳定复现：

```text
api /panel/api/inbounds/del/8 work_exists=yes
exit-node/current.json: No such file or directory
出口模板未能自动回滚
```

需要防止父进程删除事务所有的文件，并让断连后能够查询任务最终状态。SIGKILL 或整机掉电不能依赖 shell trap，应报告待人工恢复而不能假报成功。

### F3：规则先后顺序改变指定出口

基线合并函数仅将新规则插入少数形式的全局兜底之前，以下合法规则仍会优先截走流量：

```json
{"type":"field","network":"udp","port":"53","outboundTag":"direct"}
```

若此规则在新节点 DNS 规则之前，DNS 会走直连；HTTPS 可仍走 SOCKS，两项探测均可能通过。全局 TCP 直连或特定域名分流也会出现相应旁路。Xray [按第一条匹配规则转发](https://xtls.github.io/en/config/routing.html)。修复不能简单越过原安全阻断规则；应显式保留阻断策略并隔离出口选择。

### F4：入口与组件没有一起固定

基线 `install.sh:1525–1534` 从可变分支下载 helper，再与入口固定哈希比较。离线执行历史 `a6711d1` 原函数，用 `git show` 模拟当前分支返回内容，得到下载失败。当前基线组件哈希正确，缺陷在整个发布机制。

### F5：轮换出口被误判

基线 `lib/socks_node_probe.py:314–315` 强制前后两次独立连接出口 IP 相同。使用文档保留地址模拟两次正常 HTTPS：`203.0.113.10` 与 `203.0.113.11`，DNS 成功仍抛错。只有明确要求固定出口的模式才应把这种变化视为错误。

## 原有测试覆盖边界

审查时完整运行 152 项 Python 测试：135 通过、17 因 POSIX 会话/锁/PTY 等条件跳过、0 失败。两份安装入口相同，组件哈希匹配，shell 语法检查通过。

新增 9 项测试只覆盖人工构造 JSON 的合并，DNS fixture 仍用旧 `proxySettings`，没有执行生产配置生成、完整 API 事务、真实热启用或 HUP。`test_without_fallback_appends_after_existing_rules` 还把 TCP/direct 在前的错误出口顺序作为预期。

## 修复验收要求

1. 热启用后真实探测成功，Xray PID 保持不变，无脚本额外全局重启。
2. SSH/HUP 后后台事务仍持有锁和文件，最终状态可查询；失败补偿有明确状态。
3. 旧全局 DNS、TCP、域名出口规则不能抢走新节点流量；已有节点的规则行为保留。
4. 隔离测试中指定 SOCKS 服务确实收到 TCP DNS 请求。
5. 单文件入口携带或能够取得与其版本一致的组件，发布检查发现遗漏的同步。
6. 轮换出口默认可用，严格固定 IP 模式能拒绝错误出口。
7. 语法、单元测试和真实面板验证均通过，生产与手机验收边界明确记录。

## 已实施的修复

| 问题 | 实现 | 对应回归 |
| --- | --- | --- |
| F1 | `proxy_exit_running` 使用面板启用状态和监听，随后执行真实协议探测；移除启动文件过期触发的全局强制重启 | `tests/test_socks_runtime.py`；真实面板 smoke 检查核心 PID 与已有 TCP 会话 |
| F2 | 新增 `lib/socks_exit_job.py` 独立会话任务；快照源码与输入、继承维护锁、持久状态、菜单22与 `--socks-node-status` | `tests/test_socks_exit_job.py` 的 HUP、删除来源、锁、PTY 前台、状态与原函数补偿测试 |
| F3 | 保留面板核心 API 专用规则在首位；为新入站复制适用的全局 blackhole 阻断，再添加其 DNS/SOCKS 专属规则，随后保留旧业务规则原顺序；拒绝非空 routing.script | `tests/test_socks_exit_provision.py` 的真实计划生成与优先级回归；smoke 观察实际 TCP DNS CONNECT |
| F4 | 单文件入口内嵌全部 SOCKS 组件并逐个校验；生成器同步两入口与哈希；CI 检查过期 bundle | `tests/test_socks_bootstrap.py` 的离线解包、篡改、过期与入口分发测试 |
| F5 | 预检出口 IP 仅作观察值；默认支持轮换，`SOCKS_EXIT_EXPECTED_IP` 显式启用固定 IP 验收；返回实际节点出口 | `tests/test_socks_node_probe.py` 与 smoke 的两次不同出口 |

同时强化 DNS 响应解析，验证 A/CNAME 链、记录长度、压缩指针和问题匹配，避免伪造的空答案通过。回滚确认面板入站已删除、端口停止监听后，才清理专属规则及出口，再回读模板确认；失败则保留恢复依据并记录 `manual_recovery_required`。

完整 worker 的真实面板测试还发现 3x-ui 会将核心 API 专用规则移至最前。提交计划已遵循这项规范化，继续对保存后的业务规则进行完整、有序比较，避免再次产生模板回读误报。

维护保护还覆盖重装和卸载入口：`lib/clean_reinstall.py` 在取得共享锁后检查 SOCKS 遗留状态，防止工作进程中断后启动破坏性维护。仅 `complete`、`failed`、`rolled_back` 允许后续操作；异常或不确定状态停止。

任务存于 `$XUI_INFO_DIR/3xuish-socks-exit/job-*`，目录 0700、文件 0600。状态包含阶段、日志路径、备份与节点信息。正常结束或确认补偿后清理工作凭据，待恢复任务保留证据。前台最多等待60秒，之后可查询；SIGKILL/掉电后的活动状态不会被解释为成功。

## 复核命令

在完整仓库根目录执行以下离线检查；POSIX 会话和 flock 测试应在 Linux 下运行：

```bash
python3 scripts/sync_socks_helpers.py --check
bash -n install.sh
bash -n install_3xui_xhttp_vlessenc.sh
bash -n lib/socks_exit.sh
shellcheck -S warning install.sh install_3xui_xhttp_vlessenc.sh
cmp install.sh install_3xui_xhttp_vlessenc.sh
python3 -m unittest discover -s tests -v
```

真实面板检查只在一次性容器中运行。以下命令不会暴露宿主端口，仓库以只读方式挂载；容器中的模拟 SOCKS、DNS、HTTPS 均使用测试凭据和临时 CA：

```bash
docker run --rm --entrypoint /bin/sh \
  --mount "type=bind,source=$PWD,target=/repo,readonly" \
  ghcr.io/mhsanaei/3x-ui@sha256:1628adbd9bed680d25d6f714bef4a20f132105f4612ea294952f6665dad32e5b \
  -c 'apk add --no-cache bash jq sqlite openssl iproute2 curl python3 >/tmp/test-deps.log 2>&1 && cd /repo && XRAY_BIN=/app/bin/xray-linux-amd64 python3 tests/socks_exit_smoke.py'
```

## 最终回归结果

最终代码在 WSL Debian / Python 3.11 下运行完整套件：**199 项通过，0 失败，0 跳过**，耗时 167.835 秒。该次运行包含最后的核心 API 规则顺序修复及真实 PTY 前台输入测试。

| 检查 | 结果 |
| --- | --- |
| 完整 Python 套件 | `Ran 199 tests`，`OK` |
| Bash 语法 | 两份入口与 `lib/socks_exit.sh` 通过 |
| ShellCheck warning 级别 | 两份入口通过 |
| Python 语法 | 新任务、探针、发布生成器和真实面板测试通过 |
| 发布一致性 | 两份入口字节一致，三个 SOCKS helper 与维护 helper 的哈希匹配 |
| 自包含发布检查 | `python3 scripts/sync_socks_helpers.py --check` 通过 |
| Git 空白检查 | `git diff --check` 通过 |
| 官方面板与真实核心 | 下述完整端到端验收通过 |

CI 已接入 bundle 过期检查和 `socks_exit_smoke`，后续修改组件未同步入口或破坏真实创建流程时应直接失败。

## 真实面板验收结果

官方 v3.11.0 一次性容器已运行完整 `create_socks_exit_node_worker`、真实 API、Xray 及生产探针，输出：

```text
SOCKS_EXIT_SMOKE_OK: full worker; real TLS/VLESS; TCP DNS through SOCKS;
rotating exit; unchanged core PID and old TCP session;
old rules and deny policy preserved
```

已核对禁用创建、保存模板、热启用和最终 `complete` 结果。模拟 SOCKS 服务收到了 TCP/53 与 HTTPS CONNECT，覆盖预检和新节点两条路径；预检与最终出口从 `203.0.113.10` 轮换为 `203.0.113.11`，最终输出正确。原有 VLESS TCP 会话前后 echo 成功，核心 PID 保持不变，原入站身份及配置保持不变，过滤新节点规则后原规则数组完全相同。全局 blackhole 目标既没有进入 SOCKS，也没有通过 direct 到达目标。

临时 CA 的适配仅存在于测试：给临时 VLESS 客户端增加 `useSystemCA: true` 并使用 `SSL_CERT_FILE` 信任本次 CA，始终启用 TLS 证书校验。生产探针未增加 `allowInsecure`。容器未暴露宿主端口，测试完成后自动删除。

本记录中的修复和隔离验证没有部署生产服务器；GitHub 发布状态以仓库分支和合并记录为准。现有生产节点和手机公网可用性需要独立验收。

## 同日后续：主菜单与多协议输入扩展

发布前同步主分支的新功能后，代理选项顺延为21/22，保留主分支原12–20项编号。菜单21为“添加代理出口节点（SOCKS5 / HTTP / HTTPS）”，菜单22为“查看代理节点创建结果”。新增 `--add-proxy-node`、`--proxy-node-status`，原 SOCKS 命令和任务目录继续兼容。两份安装入口仍字节一致，新增输入解析器 `lib/proxy_input.py` 也包含在单文件发布中。

已支持用户截图中的四种格式：`hostname:port:username:password`、`socks5://username:password@hostname:port`、`username:password@hostname:port`、`hostname:port@username:password`。同时支持 HTTP/HTTPS URI、IP/域名/IDNA/IPv6、免认证以及分项输入。协议与排列格式分别处理；无前缀时选择协议，多个有效字段解释时拒绝自动猜测。URI 凭据按 UTF-8 解码一次，裸格式和分项凭据保留原文，错误不回显秘密资料。

HTTP/HTTPS 上游预检通过临时 Xray 使用将要发布的同一出站配置，验证真实 HTTPS 与 TCP DNS。HTTPS 指到代理服务器的 TLS 传输；HTTP CONNECT 本身也可以访问 HTTPS 网站。当前 DNS 方案仍要求允许 CONNECT 到 `1.1.1.1:53`，没有将受限服务静默改成直连 DNS。HTTP/HTTPS 的其他 UDP 不在支持范围内，认证支持 Basic 或免认证。

三个独立官方 v3.11.0 容器已分别通过 `SMOKE_PROXY_SCHEME=socks5`、`http`、`https` 的完整 worker 验证，输出 `PROXY_EXIT_SMOKE_OK scheme=...`。HTTP/HTTPS 额外覆盖拒绝 CONNECT53 的真实服务响应：创建前失败，原入站与模板未改变，已有会话仍可用。三种协议均核对核心 PID、原 TCP 会话、专属 DNS 路径、轮换出口与阻断规则。CI 已增加 HTTP/HTTPS 两项真实测试矩阵。完整输入规则与使用限制见 [README](../README.md#添加代理出口节点)。

扩展后的最终完整 Linux 回归为 **241 项通过，0 失败，0 跳过**（166.461 秒）。Bash 语法、ShellCheck、Python 语法、两份入口字节一致性、四个内嵌组件哈希及 `sync_socks_helpers.py --check` 均通过。上述199项是前一修复阶段的记录，241项为多协议扩展后的结果。

## 用户实测反馈：查看全部节点与订阅

用户在服务器选择菜单22后看到 `{"status":"no_previous_job","operation":"socks_exit"}`。该结果只说明没有脚本后台任务记录，却不能回答面板里是否有旧节点，原输出也不适合普通用户阅读。

菜单22已改为“查看全部节点与订阅（含创建结果）”。默认使用中文显示当前面板的全部节点、客户端、启停/到期状态、已有订阅地址与最近创建结果。没有任务记录时仍读取面板节点，并明确说明“这不代表面板中没有节点”。`--proxy-node-status --json` 保留自动化所需的结构化输出；新建任务的前台结果也改用中文。

新增 `lib/node_overview.py`，直接以只读 SQLite 事务读取数据，避免查看时创建管理员令牌或依赖可复用 TLS 入站。新版优先读取 `clients + client_inbounds`，旧版才读取 `inbounds.settings.clients`。订阅按现有 `subId` 分组，沿用面板配置的域名、端口、路径、反代 URI 和 Clash URI；不会自动合并或修改用户身份。TLS 地址不使用 REALITY 的伪装域名，未知地址、关闭订阅、缺少 ID、排除订阅及不支持的协议都有明确提示。

真实官方 v3.11.0 容器已验证4个手工/旧脚本/代理/停用节点，在没有任务历史、旧JSON过时或缺失时仍正确列出。实际请求概览给出的通用和Clash订阅，确认包含对应节点；查看前后 `inbounds`、`clients`、`client_inbounds`、`settings`、`users`、`api_tokens` 均未变化。关闭订阅服务后仍展示节点并解释原因，不提供假定可用的URL。测试入口为 `tests/node_overview_smoke.py`，已加入CI。

输出不包含客户端UUID、密码或私钥；订阅地址属于用户明确要求查看的凭据，仅在终端展示。已经删除的节点无法从当前数据库恢复，配置出的URL也不等于手机公网可达。
