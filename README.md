# 3x-ui 四节点一键部署

一次安装生成 Hy2、VLESS + RAW TCP + TLS、VLESS + REALITY 和 VLESS + XHTTP 四种节点，并让用户选择主用节点。支持 IP 证书、域名证书，以及后期切换域名。

默认主用 Hy2，保留三种备用方案。这个顺序来自一次实际链路对照，**不代表 Hy2 在所有网络里都最快**；应以客户端所在网络的实测为准。测试方法与结果见 [性能对照说明](docs/performance.md)。

## 打开管理菜单

在服务器的 root 终端执行：

```bash
curl -fLso /tmp/3xuish-install.sh https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh &&
bash /tmp/3xuish-install.sh
```

如果提示没有 `curl`，Debian / Ubuntu 先执行 `apt update && apt install -y curl`；RHEL 系列使用 `dnf install -y curl` 或 `yum install -y curl`。

脚本会显示是否检测到已有面板，并打开菜单：

```text
1) 全新安装四节点
2) 服务器与现有节点测速
3) 检查旧节点参数
4) 原位修复旧节点参数
5) 给已有面板添加四节点
6) 切换域名
7) 查看修复状态
8) 清空后全新部署（旧链接失效）
9) 查看清空重装结果
10) 卸载面板与节点（保留私有备份和证书）
11) 查看卸载结果
12) 一键更新 Xray 内核
13) 开启 Xray 每日自动更新
14) 关闭 Xray 自动更新
15) 开启统一订阅（一条链接导入全部节点）
16) 查看订阅、节点链接与二维码
17) Hy2 端口跳跃（开启、修改或关闭）
18) Argo 隧道（Cloudflare，IP 被封时备用）
19) 切换 Xray 内核到指定版本
20) 回滚到上一次更新前的 Xray 内核
0) 退出
```

已有面板时可以直接选择 `2` 测速，结束后返回菜单。测速不修改配置或重启面板，会消耗测试流量；完整行为和用量见下方“服务器测速”。选择 `1` 新装时会先检查已有数据库；已有面板要清空后重新部署，请选择 `8`。添加四节点、原位修复和清空重装是独立选项。

请先下载到文件再执行，以便菜单为每个操作启动独立进程；不要使用 `curl ... | bash` 打开菜单。关闭终端前输入 `0` 退出即可。

选择 `1` 后，安装向导会询问证书方式、面板账号、端口以及主用节点。主用节点可以选：

| 选择 | 适合的用途 | 默认监听 |
| --- | --- | --- |
| Hy2，默认主用 | 作为 UDP 传输方案进行实测和日常使用 | UDP `24444` |
| RAW TCP + TLS | 使用普通 TCP 传输，保留 Vision | TCP `443` |
| VLESS + REALITY | 另一种 TCP 备用方案，使用 Vision | TCP `24443` |
| XHTTP | 保留兼容和对照用途，启用 VLESS Encryption + Vision | TCP `8443` |

**选择主用节点不会删掉其他节点。** 四种节点都会生成；主用选择决定结尾的推荐标记和分享链接排列顺序。脚本不会从服务器端自动判断客户端到服务器哪条线路最快。

旧入口 `install_3xui_xhttp_vlessenc.sh` 仍可使用，功能与 `install.sh` 保持一致。

REALITY 新部署默认目标为 `www.apple.com:443`，客户端 SNI 同步使用 `www.apple.com`；可用 `REALITY_SERVER_NAME`、`REALITY_TARGET` 自定义。目标预检覆盖 TLS 1.3、X25519、证书名称和 h2，但不代表手机端兼容测试通过。新版不会自动改动已有节点；微软目标的历史记录长度问题、版本边界及已有节点处理方法见 [REALITY 兼容说明](docs/reality-compatibility.md)。

已有自动化参数继续可用。`AUTO_YES=1` 无参数运行仍走无人值守新装；也可显式指定操作：

```bash
bash /tmp/3xuish-install.sh --menu
bash /tmp/3xuish-install.sh --install
bash /tmp/3xuish-install.sh --speed-test --seconds 5
bash /tmp/3xuish-install.sh --speed-test --inbound-id 1 --inbound-id 4
bash /tmp/3xuish-install.sh --enable-subscription
bash /tmp/3xuish-install.sh --show-links
bash /tmp/3xuish-install.sh --hy2-hop 20000-50000
bash /tmp/3xuish-install.sh --argo quick
bash /tmp/3xuish-install.sh --switch-xray 26.10.21
bash /tmp/3xuish-install.sh --rollback-xray
```

`bash /tmp/3xuish-install.sh --help` 列出全部选项。

## 已有服务器清空重装

在管理菜单选择 `8`，或执行：

```bash
bash /tmp/3xuish-install.sh --reinstall
```

此选项只提供**清空旧面板数据后全新部署**。它会重新生成面板账号、节点 UUID、认证及订阅标识；旧节点和旧订阅链接将失效，需要重新导入新链接。脚本先显示影响范围，并要求手动输入 `REINSTALL`；其他输入或结束输入均取消。`AUTO_YES=1` 不会跳过这一确认。

确认后，先收集新安装参数并预下载上游安装脚本，再通过 root 私有后台任务停止服务，归档旧数据库、程序和节点资料，执行四节点新装并核对实际配置与监听。旧数据库的 WAL/SHM 和运行程序会一起处理，避免残留旧数据。默认 SQLite 安装路径才支持自动重装；自定义路径、服务、不支持的数据库配置或 OpenRC 自定义运行级别会在清空前停止。

任务脱离 SSH 会话运行，断线后可重新连接，在菜单选择 `9` 或执行：

```bash
bash /tmp/3xuish-install.sh --reinstall-status
```

以 `complete` 为重装完成依据，随后读取服务器 `/root/3x-ui-nodes-info.txt` 和 `/root/3x-ui-node-links.txt` 获取新资料。任务记录和旧数据备份位于 `/root/3xuish-reinstall/`，仅 root 可读；普通失败会尝试恢复旧程序、数据库、资料和相关证书文件，恢复异常显示 `manual_recovery_required`，请保留整个任务目录核验。重装期间不要同时在面板或其他终端编辑节点。

此操作清空的是面板部署数据，不重装操作系统，不删除整个证书目录、acme 账户或其他应用。证书签发及续期元数据可能更新，旧备份中保留的节点资料包含认证，请按私密文件保管。

## 卸载面板与节点

在管理菜单选择 `10`，或执行：

```bash
bash /tmp/3xuish-install.sh --uninstall
```

输入 `UNINSTALL` 后，任务会在后台停止服务、取消开机启动，把默认位置的面板程序、数据库及节点资料归档到 `/root/3xuish-reinstall/` 的私有任务目录，再验证原位置已移除。现有节点和订阅将不可用，操作不会自动重装。取消输入或结束输入不会卸载；`AUTO_YES=1` 不会跳过确认。

卸载支持默认 SQLite 布局，也可处理该布局下残留的部分安装。它保留证书、acme 账户、测速报告和已有备份，不清理系统依赖或其他应用。备份仍含节点认证，此功能移除运行中的部署，不等于擦除所有私密数据。卸载出错时尝试恢复原部署；`manual_recovery_required` 表示需要保留任务目录核验。

SSH 断开后在菜单选择 `11`，或查询：

```bash
bash /tmp/3xuish-install.sh --uninstall-status
```

以 `complete` 为卸载完成依据。安装、重装、卸载、参数修复、添加节点、域名迁移、Xray 内核更新/切换/回滚、统一订阅、端口跳跃和 Argo 隧道共用维护锁；已有维护任务未完成时，其他修改操作会停止，避免同时移动或写入部署数据。操作期间也请勿直接在面板修改配置。

## 更新 Xray 内核

在管理菜单选择 `12`，或执行：

```bash
bash /tmp/3xuish-install.sh --update-xray
```

脚本从 [XTLS/Xray-core](https://github.com/XTLS/Xray-core/releases) 获取最新稳定版，不使用预发布版。当前已是最新时不修改文件，也不重启。需要更新时按以下顺序执行，任一步失败都不替换内核：

1. 下载与面板内核架构对应的官方发布包，核对同一发布中 `.dgst` 文件的 SHA2-256。
2. 用新内核对当前 `bin/config.json` 执行 `run -test`，确认新版本接受现有节点配置。
3. 把原内核备份到 `/root/3xuish-xray-backup/`，替换后重启 `x-ui`，节点会短暂断开。
4. 最多等待约 60 秒，确认新内核进程在运行，且原有入站端口全部恢复监听；否则自动换回原内核并重启。

此功能只更新 Xray，不更新 3x-ui 面板、geo 文件或操作系统内核。以后升级 3x-ui 面板时，面板会装回自带的 Xray 版本，可再次运行本命令或开启自动更新。通过本机节点代理连接 SSH 时，重启会中断管理连接，建议使用直连 SSH。

### 指定版本与回滚

菜单 `19` 切换到指定版本，`20` 回滚，或执行：

```bash
bash /tmp/3xuish-install.sh --switch-xray 26.10.21
bash /tmp/3xuish-install.sh --rollback-xray
```

`--switch-xray` 可以安装任意官方发布版本，包括降级和预发布版（会提示不稳定）；官方没有的版本号会直接拒绝。`--rollback-xray` 换回上一次更新、切换或回滚之前的内核，当前内核同时成为新的备份，所以再执行一次就能换回来。两者都走上面的同一套校验、替换、启动检查和自动恢复流程。

开启了每日自动更新时，指定的旧版本会在下次检查时被升级到最新稳定版，脚本会提示；要停留在某个版本，请先关闭自动更新。

### 每日自动更新

菜单 `13` 开启，`14` 关闭，或执行：

```bash
bash /tmp/3xuish-install.sh --enable-xray-auto-update
bash /tmp/3xuish-install.sh --disable-xray-auto-update
```

开启后，脚本会复制到 `/usr/local/lib/3xuish/install.sh`（Argo 临时隧道也使用这份副本），并在 root 的 crontab 写入一条任务：每天 03:00–05:59 之间的随机时刻（服务器时区）执行与上面相同的检查、校验和回滚，日志写入 `/var/log/3xuish-xray-update.log`。定时任务只运行这份本地副本，不会每天下载远程脚本；要更新副本，用新版脚本重新开启一次即可。发现新版本时会重启一次 `x-ui`。系统需要已安装并运行 cron（安装四节点时已自动安装）。

关闭只删除这条定时任务；本地副本在 Argo 临时隧道也不再使用时才删除。当前内核、日志和旧内核备份保持不变。

CVE-2026-98432 是 Linux 内核 IPv4 TCP 处理中的漏洞。Xray v26.10.31 默认关闭 MPTCP，可以减小攻击面，但不能修复内核本身；根本修复需要通过系统包管理器安装发行版修复后的内核并重启。更新完成后，若系统仍为 `net.mptcp.enabled=1`，脚本会提示当前运行的内核版本。

## 统一订阅

新装会直接开启；已有部署在菜单选择 `15`，或执行：

```bash
bash /tmp/3xuish-install.sh --enable-subscription
```

订阅使用 3x-ui 自带的订阅服务，安装结尾和 `/root/3x-ui-nodes-info.txt` 会给出两条地址：

- 通用订阅：v2rayN、v2rayNG、Shadowrocket 等导入后得到全部节点。
- Clash / Mihomo 订阅：Mihomo 内核的客户端使用。

3x-ui 要求每个客户端的订阅 ID 唯一，因此由主用节点的客户端承载统一订阅，其他节点的分享链接作为它的“外部链接”（面板中该客户端的“链接”页）。各节点仍保留独立的 UUID 和认证凭据，订阅里的链接与资料文件中的一致。请勿在面板中删除这些外部链接；域名切换、端口跳跃和 Argo 隧道的变化会由脚本自动同步。

- **新装**：订阅端口 TCP `2096`（可用 `XUI_SUB_PORT` 指定），使用节点证书提供 HTTPS，并开启 Clash/Mihomo 格式。订阅路径由 3x-ui 随机生成。
- **已有部署**：只改承载客户端的订阅 ID（旧 ID 由节点标签派生，改为随机值），其他客户端、流量额度和到期时间不变。订阅服务已开启时保留原端口；原来是 HTTP 时会询问是否改为 HTTPS（已分发的 HTTP 订阅地址会失效）。重复执行不会再修改。

需要在安全组放行订阅端口。部分客户端不支持某些协议的 Clash 写法时，脚本会提示这些节点改用通用订阅。

## 节点二维码

安装结尾显示统一订阅的二维码。查看全部节点的链接和二维码，在菜单选择 `16`，或执行：

```bash
bash /tmp/3xuish-install.sh --show-links
```

此操作只读。缺少 `qrencode` 时会尝试安装（RHEL 系列可能需要 EPEL，装不上时只显示链接）。超过约 550 个字符的链接（例如 ML-KEM-768 认证的 XHTTP 链接）在终端里过密无法扫描，会提示改用复制链接或统一订阅。

## Hy2 端口跳跃

运营商对单个 UDP 端口限速时，可以让客户端在一段端口范围内轮换连接。在菜单选择 `17`，或执行：

```bash
bash /tmp/3xuish-install.sh --hy2-hop 20000-50000   # 开启或修改范围
bash /tmp/3xuish-install.sh --hy2-hop status
bash /tmp/3xuish-install.sh --hy2-hop off
```

脚本用 nftables（没有时用 iptables）把这段 UDP 端口重定向到 Hy2 端口，系统支持时同时添加 IPv6 规则，并安装 `3xuish-hy2-hop` 开机服务（systemd 或 OpenRC）恢复规则。Xray 仍只监听原 Hy2 端口；范围同时写入入站的 UDP Hop 设置，分享链接带 `mport`，订阅和 Clash 格式也会带上端口范围。

- 范围内已有其他程序或面板入站使用的 UDP 端口时会拒绝，避免把它们的流量导向 Hy2。
- 需要在云安全组和系统防火墙放行整段 UDP 范围；旧客户端需重新导入 Hy2 链接或刷新订阅。
- 客户端需支持端口跳跃（如 v2rayN、Mihomo）；不支持的客户端仍连接原端口。
- 重新加载 nftables 或 firewalld 可能清掉规则，`--hy2-hop status` 会提示，重新运行开启命令即可恢复。

## Argo 隧道

服务器 IP 被封时，可以经 Cloudflare 的隧道连接。在菜单选择 `18`，或执行：

```bash
bash /tmp/3xuish-install.sh --argo quick    # 临时隧道，免账号
bash /tmp/3xuish-install.sh --argo fixed    # 固定隧道，需要 Cloudflare 账号和托管域名
bash /tmp/3xuish-install.sh --argo status
bash /tmp/3xuish-install.sh --argo off
```

脚本下载 Cloudflare 官方 cloudflared（核对发布说明中的 SHA-256），新增一个只监听 `127.0.0.1` 的 VLESS 入站，安装 `3xuish-argo` 开机服务，并把 Argo 节点加入统一订阅。

- 入站使用 WebSocket + VLESS Encryption + Vision：Cloudflare 终止外层 TLS，VLESS Encryption 让它看不到代理内容。实测 XHTTP 无论哪种模式都无法穿过 cloudflared 的本机转发，因此 Argo 节点使用 WebSocket；Xray 会提示 WebSocket 不再推荐，可忽略。
- **临时隧道**使用 `trycloudflare.com` 域名，仅供测试，无可用性保证，服务每次重启域名都会变。服务启动后会自动把新域名写回面板、资料文件和统一订阅；单独导入的链接需要重新导入。
- **固定隧道**需要先在 Cloudflare Zero Trust → 网络 → 隧道 创建隧道并复制令牌（也可用 `ARGO_TOKEN`、`ARGO_DOMAIN` 传入）。脚本会提示在“公共主机名”中把域名指向 `HTTP 127.0.0.1:端口`。令牌只保存在 `/etc/3xuish/argo.env`（权限 600），不出现在进程命令行。
- 关闭会删除 Argo 入站、服务、cloudflared 和相关文件，并从统一订阅中移除。

清空重装和卸载不会处理端口跳跃、Argo 隧道和自动更新；执行前脚本会列出仍在运行的这几项，建议先关闭。

## IPv6 服务器

- **纯 IPv6 服务器**：检测不到公网 IPv4 时自动使用 IPv6 地址部署，IP 证书和域名证书都会让 acme.sh 监听 IPv6（`--listen-v6`）。
- **双栈服务器**：默认仍用 IPv4；要用 IPv6 部署，设置 `XUI_SERVER_IP` 为 IPv6 地址，例如 `XUI_SERVER_IP=2001:db8::10`。
- 分享链接、面板地址和订阅地址中的 IPv6 地址会加方括号。

GitHub 不支持 IPv6，而安装 3x-ui、更新 Xray 和下载 cloudflared 都需要访问 GitHub。纯 IPv6 服务器请先配置 NAT64/DNS64，或安装 WARP 获得 IPv4 出口；新装时访问不到 GitHub 会在申请证书前停止并给出提示。

## 放行需要的端口

在云服务器安全组和系统防火墙中放行实际使用的端口。脚本不会替你修改防火墙规则。

| 端口 | 协议 | 用途 |
| --- | --- | --- |
| `80` | TCP | 首次申请证书和后续自动续期 |
| 面板端口 | TCP | 访问 3x-ui；默认在 `10000–55000` 中选择 |
| `24444` | UDP | Hy2 |
| `443` | TCP | RAW TCP + TLS |
| `24443` | TCP | VLESS + REALITY |
| `8443` | TCP | XHTTP |
| `2096` | TCP | 统一订阅（`XUI_SUB_PORT` 可改） |
| 跳跃范围 | UDP | Hy2 端口跳跃，仅在开启后需要 |

如果在向导或环境变量中改过端口，以安装结尾输出为准。Hy2 需要放行的是 **UDP**；只开放同数字的 TCP 端口不够。Argo 隧道由服务器主动连接 Cloudflare，不需要放行入站端口。

## 选择证书

- **IP 证书，默认**：无需域名，使用 Let's Encrypt 短期 IP 证书，通过 acme.sh 自动续期。
- **域名证书**：先将域名的 A 记录解析到这台服务器，再在向导里填写域名。使用 Cloudflare DNS 时，将该记录设为“仅 DNS”，不要开启代理。

证书续期仍需服务器的 TCP `80` 可达。REALITY 使用自己的目标站点与密钥设置，不需要把目标站点改成自己的证书域名。

## 无人值守安装

安装全部四种节点，并使用默认主用 Hy2：

```bash
curl -fLso /tmp/3xuish-install.sh https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh
AUTO_YES=1 PRIMARY_NODE=hy2 bash /tmp/3xuish-install.sh
```

例如，指定 RAW TCP 为主用，并修改面板与 Hy2 端口：

```bash
AUTO_YES=1 PRIMARY_NODE=raw XUI_PANEL_PORT=20530 HY2_PORT=24445 bash /tmp/3xuish-install.sh
```

使用自己的域名时，将下面的 `example.com` 换成已经解析到服务器的域名：

```bash
AUTO_YES=1 CERT_MODE=2 XUI_DOMAIN=example.com PRIMARY_NODE=hy2 bash /tmp/3xuish-install.sh
```

常用环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AUTO_YES` | `0` | 设为 `1` 使用无人值守模式 |
| `PRIMARY_NODE` | `hy2` | `hy2`、`raw`、`reality` 或 `xhttp`；只影响推荐标记和链接顺序 |
| `CERT_MODE` | `1` | `1` 为 IP 证书，`2` 为域名证书 |
| `XUI_DOMAIN` | 无 | 域名证书使用的域名 |
| `XUI_SERVER_IP` | 自动检测 | 手动指定服务器公网 IPv4 |
| `XUI_USERNAME` | `admin` | 面板登录用户名 |
| `XUI_PASSWORD` | 随机生成 | 面板登录密码 |
| `XUI_PANEL_PORT` | 随机选择 | 面板 HTTPS 端口 |
| `XUI_WEB_BASE_PATH` | 随机生成 | 面板访问路径 |
| `RAW_PORT` | `443` | RAW TCP + TLS 端口 |
| `REALITY_PORT` | `24443` | REALITY 的 TCP 端口 |
| `HY2_PORT` | `24444` | Hy2 的 UDP 端口 |
| `XHTTP_PORT` | `8443` | XHTTP 的 TCP 端口 |
| `XHTTP_PATH` | 随机生成 | XHTTP 路径 |

节点端口和面板端口不能冲突。脚本遇到与本次部署端口冲突的已有入站或监听服务会停止并提示处理，不会盲目覆盖现有入站。

全新部署命令面向未安装 3x-ui 的服务器。旧节点原位修复使用 `--repair-performance`，已有面板新增四节点使用 `--add-nodes`，域名迁移使用 `--switch-domain`。

## 修复旧节点，保留原链接

老用户保留原节点、端口和订阅链接，一条命令检查并原位修复。在服务器 root 终端执行：

```bash
curl -fLso /tmp/3xuish-install.sh https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh &&
bash /tmp/3xuish-install.sh --repair-performance
```

脚本自动检查入站并显示选中原因、旧参数及检查数量，不输出 UUID、密码或私钥。只有一个入站且为支持的 VLESS TCP/RAW/XHTTP 传输时，自动选中它，不要求保留原备注，也不要求先安装四节点。多个入站时，默认识别旧脚本的 `VLESS-XHTTP-Enc-Vision` TLS/XHTTP 入站，以及本脚本已保存资料明确登记的 VLESS 节点。此模式不会安装或升级面板。

服务器的 `python3` 命令需要 Python **3.8 或更新版本**，可用 `python3 --version` 确认。Debian/Ubuntu 可执行 `apt-get install -y python3`，Alpine 执行 `apk add python3`；旧版系统安装后仍须核对版本。版本不足时，脚本会在下载修复组件或修改节点之前停止。

只想预览、不修改配置时：

```bash
bash /tmp/3xuish-install.sh --check-performance
```

| 旧脚本保存的值 | 修复后的行为 |
| --- | --- |
| `tcpWindowClamp: 600` | 改为 `0`，取消此接收窗口限制 |
| `tcpMaxSeg: 1440` | 删除固定 MSS，让系统协商 |
| `tcpUserTimeout: 10000` | 删除固定的 10 秒超时 |
| `tcpcongestion: "bbr"` | 删除每连接覆盖，采用系统当前拥塞控制设置 |

只修改表中精确匹配的旧值，其他自定义值保留。系统全局 BBR、缓冲区和防火墙不会被修改。域名、端口、UUID、VLESS Encryption/REALITY 密钥、XHTTP 路径和订阅链接保持原值；修复生效后客户端断开重连即可。

任务会备份数据库和相关文件，先验证候选 Xray 配置，再更新数据库、重启面板并核对实际生成配置及原监听。命令会自动等待最多 60 秒并显示最终结果：`complete` 表示完成，`no_changes` 会同时说明哪些入站已经检查、为何没有修改。未选中的入站仍有旧参数或无法完整检查时，输出 `review_required: true`，不会把它当作整台服务器都已检查干净。修复命令检查后发现无需修改时，也会保存本次检查记录；`--check-performance` 预览仍然只读。

任务独立于 SSH 会话运行，重启导致管理连接断开后仍会继续。等待超过 60 秒或连接中断时，重新连接后查询：

```bash
bash /tmp/3xuish-install.sh --repair-performance-status
```

`started`、`starting` 或 `running` 表示尚未确认完成，必须以最终 `complete` 为成功依据。

任务记录和备份位于服务器 `/root/3xuish-performance-repair/` 下的独立目录，仅 root 可读。普通失败会尝试精确回滚本次修改；`rolled_back` 表示修复失败并恢复，`manual_recovery_required` 表示检测到并发修改、后台任务异常中断或恢复异常，应保留该目录检查，不能盲目覆盖整个数据库。异常中断不会一直显示“运行中”，再次修复也会停止并保留上次记录，待核验恢复后处理。

多个入站中存在无法自动确定归属的旧参数时，检查结果会列出未选中的入站 ID。核对目标后可以显式指定，例如 ID 为 1：

```bash
bash /tmp/3xuish-install.sh --check-performance --inbound-id 1
bash /tmp/3xuish-install.sh --repair-performance --inbound-id 1
```

已经没有旧值的节点会跳过，不重复重启。`--add-nodes` 仍只用于新增方案，不代替原位修复。此功能修正旧配置，实际吞吐量仍取决于当前网络和传输方案；`tcpWindowClamp` 主要影响服务端接收方向，不能把全部下载慢都归因于它。

## 服务器测速，不重装

在上方管理菜单选择 `2`，或通过安装脚本直接测速：

```bash
bash /tmp/3xuish-install.sh --speed-test
```

独立诊断入口也继续可用。在服务器 root 终端执行：

```bash
curl -q -fLso /tmp/3xuish-speed-test.sh https://raw.githubusercontent.com/Iwhrt72/3xuish/main/scripts/server-speed-test.sh &&
bash /tmp/3xuish-speed-test.sh
```

需要 Bash、Python 3.8+、curl、sha256sum，以及面板现有的 Xray 内核；缺少 SNI 时，使用 OpenSSL 从现有公开证书的 SAN 中读取具体名称。脚本不安装软件、不修改节点或订阅、不重启面板。它会读取数据库与运行配置，使用有效的现有认证在回环地址启动临时客户端，结束或按 Ctrl+C 后关闭自己的客户端并清理临时认证文件。

每条路径依次测 OVH 单连接、OVH 四连接和 Cloudflare 单连接，每组默认最多 10 秒。首先测试服务器直接下载，再测试最多四个已启用的 VLESS TCP/RAW/XHTTP/REALITY 或 Hy2 入站。现有 XHTTP/443 和 Hy2/42710 会分别显示为对应入站 ID 的测试路径；不识别的节点或缺失参数会显示跳过原因。TLS 继续验证证书，不能验证的测试不会自动改成跳过证书检查。

默认测两个节点时，采样约 90 秒以内，加上初始化可能需要 1–2 分钟，下载有效载荷最多约 1.8 GiB。测试流量会计入服务器流量和所测节点用量；没有下载大文件到磁盘。HTTP 错误页、认证失败、传输失败不计为有效速度；有效的限时样本会保留已下载字节和退出码。四连接速度按整个组的实际用时计算。

结果表使用 Mbps，`direct` 是服务器直接下载，`inbound-1-xhttp` 等是通过现有入站的本机回环下载。**回环测试排除了用户电脑到服务器的公网链路，不能证明客户端实际速度或公网节点可达性；本脚本也不测上传速度。** 相同下载源的直接下载和回环结果可以帮助判断节点自身是否有明显处理瓶颈；不同源、不同测试时间的差异还可能来自线路变化。

完整脱敏报告保存在 `/root/3xuish-speed-test/<时间与随机编号>/report.json`，不含 UUID、认证、私钥、节点域名或原始日志，目录仅 root 可读。`complete` 表示本次选定测试都有有效结果，`partial` 表示部分源失败、部分节点跳过或超过四个节点未全部测完，`failed` 表示没有有效速度；失败项不能当作 `0 Mbps` 判断节点慢。

需要缩短采样、只测基础下载或指定入站时：

```bash
bash /tmp/3xuish-speed-test.sh --seconds 5
bash /tmp/3xuish-speed-test.sh --direct-only
bash /tmp/3xuish-speed-test.sh --inbound-id 1 --inbound-id 4
```

下载源依据 [OVH 官方测速说明](https://proof.ovh.net/) 和 [Cloudflare 官方 speedtest API](https://github.com/cloudflare/speedtest)；单次结果不代表服务器套餐或所有目的地的带宽上限。

## 给已有面板添加节点

此前的速度对照使用临时入口，结束后恢复原 XHTTP 并停止 REALITY、Hy2。需要长期使用时，在原服务器 root 终端执行：

```bash
curl -fsSL https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh -o /tmp/3xuish-install.sh
bash /tmp/3xuish-install.sh --add-nodes
```

此模式读取当前面板设置及现有 TLS 节点的证书，先备份数据库，再通过面板 API 新增四种节点。原节点、面板账号和证书继续使用，不重新安装面板或申请证书。

默认尝试 RAW TCP `24445`、REALITY TCP `24443`、Hy2 UDP `24444` 和 XHTTP TCP `8443`，自动避开已占用端口，以最终输出为准。原 XHTTP 在 TCP `443` 时会保留，因此面板会同时有原节点和新增节点。若用环境变量显式指定端口而发生冲突，脚本会停止。

新增项由面板长期管理，没有测速时的自动停止期限。重复运行会检查已保存节点并显示原链接，避免反复新增。需要开放云安全组和系统防火墙对应端口，尤其是 Hy2 的 UDP 端口。

如果 SSH 在重启 Xray 时断开，不要把断线直接视为成功或失败。先核对服务器状态；脚本检测到自己创建的入站但正式资料缺失时会停止，避免再次生成一组节点。保留数据库备份及 `/tmp/3xuish.*/profiles.json` 供核验恢复。通过正在修改的代理连接 SSH 时，重启该代理会中断管理链路，宜使用独立管理连接或持久终端会话执行。

复用证书前会核对有效期、域名/IP 以及证书和私钥是否匹配。若原节点未保存正确 SNI，可用 `XUI_DOMAIN=证书覆盖的域名` 指定。重复运行也会检查证书路径、REALITY 密钥、XHTTP 路径和加密参数；面板配置与保存的链接不一致时会停止，请先核对面板配置。

安装结尾显示长期节点的导入链接，请在 v2rayN 中重新导入这些链接。临时测速凭据与新建正式节点不同，不应继续使用临时链接。

## 安装后导入客户端

安装结尾会显示：

1. 面板 HTTPS 地址、用户名、密码和证书路径。
2. 选定的主用节点，以及另外三条备用节点。
3. 每条节点的协议、服务器地址、端口、传输方式和分享链接。
4. 需要放行的端口、客户端导入方法和常见排查命令。

先导入标有“主用”的链接，在客户端连接后实测。也可以复制全部链接，以多行文本导入 v2rayN 等支持的客户端；Hy2 使用 `hysteria2://` 分享链接，其他节点使用 `vless://` 分享链接。

XHTTP 使用 VLESS Encryption，客户端内核需要支持相应功能。使用旧客户端时，先升级内核，或使用 RAW TCP / REALITY 备用节点。

安装结果保存在服务器，仅 root 可读：

```bash
# 完整中文说明：面板信息、四种节点、分享链接和排查方法
cat /root/3x-ui-nodes-info.txt

# 只有分享链接，便于整组复制导入
cat /root/3x-ui-node-links.txt
```

`/root/3x-ui-node-profiles.json` 保存机器可读的节点资料。旧路径 `/root/3x-ui-xhttp-info.txt` 仍保留，方便原有使用方式。

## TLS 和网络参数

本脚本的 TLS 入站安全设置显式采用：

```json
{
  "minVersion": "1.0",
  "maxVersion": "1.3"
}
```

最低版本按兼容需求设为 `1.0`，最高版本使用当前支持的 `1.3`。这不是“强制使用 TLS 1.0”，实际版本由两端协商。Hy2 的 QUIC 传输实际使用 TLS 1.3，不会因此降级到 TLS 1.0。REALITY 采用自己的安全设置。

本次对照中 RAW 与 XHTTP 使用同一份证书，速度仍有明显差异；没有证据表明仅把 TLS 最低版本调低，就能解决所有无法联网或慢速问题。

同时修复了原脚本的几项默认行为：

- `tcpWindowClamp` 设为 `0`，不再把 TCP 接收窗口限制在 `600` 字节对应的小窗口范围。
- 不强制固定 MSS，也不强制设置 10 秒 TCP 超时。
- 不向不支持 BBR 的内核盲目写入 TCP 拥塞算法；Hy2 使用自身的自适应 BBR，不把套餐标称 `1Gbps` 当成强制发包速率。
- 启用 VLESS Encryption 的入站不再写入空的 `fallbacks` 数组，避免当前 Xray 拒绝启动。
- 创建入站或核心配置验证失败时停止，不再把失败输出为“部署完成”。

## 后期从 IP 切换到域名

先将域名 A 记录解析到本机，并保持“仅 DNS”。然后执行；将 `example.com` 换成你的域名：

```bash
curl -fLso /tmp/3xuish-install.sh https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh
bash /tmp/3xuish-install.sh --switch-domain example.com
```

脚本更新面板和使用证书的节点，保留现有节点的用户标识、密钥和端口。切换后以最新输出的链接重新导入或更新客户端。REALITY 的伪装目标和密钥不跟随证书域名改动。

## 无法连接或速度不理想

| 现象 | 先检查什么 |
| --- | --- |
| Hy2 连不上，TCP 节点能连 | 安全组和防火墙是否开放 Hy2 的 UDP 端口；当前接入网络是否允许 UDP |
| 所有节点都无法连接 | 服务器 IP、端口、服务状态，以及客户端是否选中了正确节点 |
| 只有 XHTTP 无法连接 | 客户端内核是否支持当前 VLESS Encryption，地址、SNI 和 XHTTP Path 是否匹配 |
| 证书申请或续期失败 | DNS 是否指向本机，TCP `80` 是否可达，域名是否开启了 CDN 代理 |
| 面板能开，节点很慢 | 在同客户端、同下载源和相近时间比较不同节点，再看重传、CPU 和路由 |

在服务器查看监听和日志：

```bash
ss -lntup
systemctl status x-ui --no-pager
journalctl -u x-ui -n 100 --no-pager
```

使用 OpenRC 的系统通过 `rc-service x-ui status` 查看状态。面板进程存在不等于 Xray 已正常监听，应同时核对节点端口。

带宽单位也要一致：`1000Mbps ÷ 8 = 125MB/s` 是理论换算，`1TB` 是流量额度。套餐标称端口带宽不等于任意跨网单连接都能达到该速度。限时测速若返回 `curl: (28)`，但已得到 HTTP `200` 和下载数据，可能只是到达测试设置的时间上限；应结合测试命令和实际字节数判断。

## 本地验证

```bash
bash -n install.sh
shellcheck -S warning install.sh install_3xui_xhttp_vlessenc.sh
cmp install.sh install_3xui_xhttp_vlessenc.sh
python3 -m unittest discover -s tests -v
```

测试需要 Bash、jq、SQLite3、OpenSSL 和 Python3。单元集成测试覆盖四节点生成、主用选择、面板 API 失败回滚、域名迁移恢复、真实证书匹配、结果文件写入失败、本机 API 代理绕行以及运行配置变更。可单独运行真实内核测试：

```bash
XRAY_BIN=/path/to/xray python3 tests/runtime_smoke.py
```

`tests/panel_smoke.sh` 和 `tests/existing_panel_smoke.sh` 分别验证新建四节点、已有面板追加节点与重复执行。使用一次性官方 3x-ui 容器和临时证书，不申请公网证书。`runtime_smoke.py` 对 Hy2、RAW、REALITY、XHTTP 分别执行回环下载并核对 SHA256；REALITY 使用临时本地 TLS 1.3 目标。

GitHub CI 同时运行模拟测试、真实面板与真实内核测试，容器按镜像摘要固定到 3x-ui v3.11.0 / Xray 26.10.21。安装脚本仍从官方获取上游最新版；未来上游升级、实际公网 ACME 申请、云安全组和用户线路不在隔离测试的证明范围内。审计发现和复现方法见 [2026-10-28 审计报告](docs/2026-10-28_code-audit-3xuish-report.md)。

## 参考资料

- [3x-ui](https://github.com/MHSanaei/3x-ui)
- [Xray TLS 配置](https://xtls.github.io/config/transports/tls.html)
- [Xray Hysteria2 传输](https://xtls.github.io/config/transports/hysteria.html)
- [Xray VLESS 配置](https://xtls.github.io/config/inbounds/vless.html)
