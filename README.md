# 3x-ui 四节点一键部署

一次安装生成 Hy2、VLESS + RAW TCP + TLS、VLESS + REALITY 和 VLESS + XHTTP 四种节点，并让用户选择主用节点。支持 IP 证书、域名证书，以及后期切换域名。

默认主用 Hy2，保留三种备用方案。这个顺序来自一次实际链路对照，**不代表 Hy2 在所有网络里都最快**；应以客户端所在网络的实测为准。测试方法与结果见 [性能对照说明](docs/performance.md)。

## 开始安装

在服务器的 root 终端执行：

```bash
curl -fLso /tmp/3xuish-install.sh https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh
bash /tmp/3xuish-install.sh
```

如果提示没有 `curl`，Debian / Ubuntu 先执行 `apt update && apt install -y curl`；RHEL 系列使用 `dnf install -y curl` 或 `yum install -y curl`。

安装向导会询问证书方式、面板账号、端口以及主用节点。主用节点可以选：

| 选择 | 适合的用途 | 默认监听 |
| --- | --- | --- |
| Hy2，默认主用 | 作为 UDP 传输方案进行实测和日常使用 | UDP `24444` |
| RAW TCP + TLS | 使用普通 TCP 传输，保留 Vision | TCP `443` |
| VLESS + REALITY | 另一种 TCP 备用方案，使用 Vision | TCP `24443` |
| XHTTP | 保留兼容和对照用途，启用 VLESS Encryption + Vision | TCP `8443` |

**选择主用节点不会删掉其他节点。** 四种节点都会生成；主用选择决定结尾的推荐标记和分享链接排列顺序。脚本不会从服务器端自动判断客户端到服务器哪条线路最快。

旧入口 `install_3xui_xhttp_vlessenc.sh` 仍可使用，功能与 `install.sh` 保持一致。

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

如果在向导或环境变量中改过端口，以安装结尾输出为准。Hy2 需要放行的是 **UDP**；只开放同数字的 TCP 端口不够。

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

全新部署命令面向未安装 3x-ui 的服务器。已有面板请使用下面的 `--add-nodes`，域名迁移使用 `--switch-domain`。

## 给已有面板添加节点

此前的速度对照使用临时入口，结束后恢复原 XHTTP 并停止 REALITY、Hy2。需要长期使用时，在原服务器 root 终端执行：

```bash
curl -fsSL https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh -o /tmp/3xuish-install.sh
bash /tmp/3xuish-install.sh --add-nodes
```

此模式读取当前面板设置及现有 TLS 节点的证书，先备份数据库，再通过面板 API 新增四种节点。原节点、面板账号和证书继续使用，不重新安装面板或申请证书。

默认尝试 RAW TCP `24445`、REALITY TCP `24443`、Hy2 UDP `24444` 和 XHTTP TCP `8443`，自动避开已占用端口，以最终输出为准。原 XHTTP 在 TCP `443` 时会保留，因此面板会同时有原节点和新增节点。若用环境变量显式指定端口而发生冲突，脚本会停止。

新增项由面板长期管理，没有测速时的自动停止期限。重复运行会检查已保存节点并显示原链接，避免反复新增。需要开放云安全组和系统防火墙对应端口，尤其是 Hy2 的 UDP 端口。

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
