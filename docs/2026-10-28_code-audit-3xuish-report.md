# 3xuish 部署脚本审计与修复

日期：2026-10-28（America/Los_Angeles）。范围为已发布提交 `623d10e948564988ae61f6b837b866926d67c2f5`、本地已有面板追加节点功能，以及相应测试和 CI。审计覆盖 Bash 配置生成、证书生命周期、面板管理 API、失败恢复、客户端分享链接和实际 Xray 传输；未把隔离环境验证当作测速服务器已部署的证明。

审计确认已发布版本的四节点生成和主用选择存在，TLS 范围仍为用户指定的 1.0–1.3，窗口限制与空 `fallbacks` 修复保留。同时发现并修复了证书、结果保存和管理 API 的异常路径。没有发现“所有新装节点必然不可用”的证据。

## 核对此前的发布和验证声明

| 声明 | 核对结果 | 证据 |
| --- | --- | --- |
| 已推送 `623d10e` | 已确认，审计开始时远端 main 指向此提交 | `git ls-remote origin refs/heads/main` |
| 12 项测试、GitHub CI 通过 | 已确认，原 CI 日志为 `Ran 12 tests in 4.651s`、`OK` | [原 CI 运行](https://github.com/Iwhrt72/3xuish/actions/runs/40739038961) |
| CI 包含真实面板和内核验证 | 原 CI 未包含，原配置只运行 ShellCheck、语法和模拟测试 | 基线 `.github/workflows/test.yml` |
| 四种节点配置正确 | 已重新验证真实面板创建、监听及生成的客户端/服务端配置 | `tests/panel_smoke.sh` |
| 四种协议均有实际传输验证 | 此前 REALITY 只做配置验证；本轮补齐本地 TLS 1.3 目标，四种协议均完成 1 MiB 下载和 SHA256 校验 | `tests/runtime_smoke.py` |

新 CI 增加三个独立容器任务：新建四节点、已有面板追加节点、四协议真实内核传输。镜像固定为 `ghcr.io/mhsanaei/3x-ui@sha256:1628adbd9bed680d25d6f714bef4a20f132105f4612ea294952f6665dad32e5b`，对应 3x-ui v3.11.0 / Xray 26.10.21。

## Evidence：可复现证据

以下命令在仓库根目录执行，需要 Bash、Python3、jq、OpenSSL 和 SQLite3；测试使用临时目录和模拟认证，不接触生产面板。

| 编号 | 观察与复现 | 证据文件 |
| --- | --- | --- |
| E-01 | 原 GitHub CI 确认 12 项测试通过，但没有执行真实面板/内核脚本 | 上述 CI 链接及基线 workflow |
| E-02 | 原签发流程在条件调用中收到 ACME 失败后仍可采用旧文件；修复前新增证书用例 6 项失败，修复后通过 | `python3 -m unittest discover -s tests -p test_certificates.py -v` |
| E-03 | 向真实结果写入函数注入文件写入失败，原代码仍可返回成功，或清除回滚标记后才失败；修复后四个目标文件分别失败均恢复旧节点与旧资料 | `python3 -m unittest discover -s tests -p test_output_recovery.py -v` |
| E-04 | 设置环境代理并清空 NO_PROXY 时，旧管理 API 参数会将本机请求交给代理；修复后本机假代理收到零请求，假面板收到正确 GET/POST | `python3 -m unittest discover -s tests -p test_api_transport.py -v` |
| E-05 | 修改运行配置中的 XHTTP 路径后，旧重复执行检查仍接受旧链接；修复后 10 种独立配置变更均被拒绝，且不新建节点 | `python3 -m unittest discover -s tests -p test_runtime_drift.py -v` |
| E-06 | Git Bash 把 POSIX 盘符路径转换为 Windows 原生路径，造成测试误判；统一测试数据中的实际路径后，证书与私钥仍逐字段精确核对 | `tests/test_existing_installation.py` |
| E-07 | 官方隔离面板保留原 XHTTP、账号和证书，新增四个长期节点；再次运行不增加节点 | `tests/existing_panel_smoke.sh` |
| E-08 | Hy2、RAW、REALITY、XHTTP 均通过真实 Xray 的 1 MiB 传输与 SHA256 校验 | `tests/runtime_smoke.py` |

## Findings：确认问题及修复

### F-01：证书失败被旧文件掩盖

- 严重度：medium；类别：可靠性；状态：validated、已修复；置信度：high；证据：E-02。
- 位置：`prepare_acme_port80`、`issue_ip_cert`、`issue_domain_cert`、`load_existing_panel_context`。
- 调用条件：域名切换用 `issue_domain_cert ... || return` 调用函数，Bash 在该上下文中禁用函数内部的 `errexit`；原 `--installcert` 还明确忽略了错误。
- 影响：签发、安装或续期设置失败后，非空旧文件可能被当作成功；仅判断文件存在无法排除错误域名、过期或私钥不匹配。
- 修复：下载、ACME 配置、签发和安装逐步检查退出状态；统一校验证书时间、域名/IP 和公钥匹配。使用 `openssl verify`，避免仅依赖 `x509 -checkhost` 的退出码。
- 范围说明：本地名称/时间验证将选定证书作为验证锚，只用于核对本地证书身份与配对，不声称证明公开 CA 信任链。真实客户端继续验证远端证书。

### F-02：保存失败仍报告完成

- 严重度：medium；类别：可靠性；状态：validated、已修复；置信度：high；证据：E-03。
- 位置：`write_node_files`，由追加节点和域名切换路径调用。
- 影响：正式 profiles、链接或说明文件缺失时仍可能报告成功；部分路径会先清除回滚标记，留下新增节点及不完整资料。
- 修复：每一步生成和写入显式返回错误，由调用方执行已有回滚；回归对四个实际输出文件分别注入失败，不再只模拟整个写入函数失败。

### F-03：本机管理请求受环境代理影响

- 严重度：medium；类别：认证信息暴露条件；状态：validated、已修复；置信度：high；证据：E-04。
- 位置：`api_init` 的 curl 公共参数。
- 影响：存在 HTTP/HTTPS/ALL_PROXY 且未正确排除本机地址时，管理请求可能经过环境代理，造成连接失败或将认证请求交给代理。未观察到真实主机发生凭据泄露。
- 修复：本机管理 API 统一使用 `--noproxy '*'`。证据通过本机假面板和假代理验证，无第三方接收测试认证。

### F-04：重复执行可能返回失效的旧链接

- 严重度：medium；类别：配置一致性；状态：validated、已修复；置信度：high；证据：E-05。
- 位置：`verify_running_nodes`。
- 影响：面板手工修改 XHTTP 路径、VLESS 加密参数、REALITY 密钥或 TLS 信息后，仅凭端口和部分用户字段仍可能认定保存链接可用。
- 修复：检查保存 profiles 与实际配置的 TLS SNI/证书、REALITY 密钥/目标、XHTTP 参数和 VLESS decryption；兼容 REALITY `target`/`dest` 等价写法。发现不一致时停止，不覆盖面板编辑，也不重复创建。

### F-05：追加流程的顶层错误传播不足

- 严重度：medium；类别：可靠性；状态：validated、已修复；置信度：high；证据：E-07 及 `test_existing_installation.py`。
- 范围：原先尚未发布的 `--add-nodes` 修改，不属于 `623d10e` 的新装入口。
- 影响：准备、创建或重复验证失败时，条件调用可能继续进入成功输出。
- 修复：关键步骤显式传播错误，创建失败回滚只删除本次新增项，原 XHTTP、面板账号、证书与其他入站保留。

## Path：失败与恢复调用链

P-01（callflow）：`--add-nodes` → 备份原数据库和资料 → API 创建节点 → 校验实际配置和监听 → `write_node_files`。E-03 的写入失败触发 F-02 的显式错误返回 → EXIT 清理 → 删除本次新增节点并恢复旧资料。原入站保持不变由 E-07 和结果写入回归共同验证。

P-02（callflow）：`--switch-domain` → `issue_domain_cert` → ACME 签发/安装 → 证书校验。E-02 的失败现在在更新面板及入站证书前返回；后续数据库、重启或结果保存失败仍走域名迁移恢复测试覆盖的回滚路径。

## 验证范围与残余限制

安装器以 root 运行，并信任官方上游下载的 acme.sh、3x-ui 安装器及其更新机制。当前真实运行测试绑定到上述固定镜像；安装脚本仍获取上游最新版，因此不能保证未来上游变更自动兼容。

隔离测试不申请公网证书，不证明真实服务器的 ACME、DNS、云安全组、UDP 可达性或用户线路性能。它们必须在正式部署阶段另行检查。TLS 最低 1.0 是既定兼容配置，本轮保留；Hy2 的实际 QUIC 握手使用 TLS 1.3。

未确认可达的 SQL/命令注入；域名、端口、标签经过校验，数据库 JSON 更新使用 SQLite 引号处理。节点认证资料默认由 root 目录和 0600 文件权限保护。审计结论限于已检查路径，不构成对所有运行环境的无条件保证。

## 复验

本轮最终本地验证：Windows 32/32（301.009 秒）、WSL Debian 32/32（155.638 秒），Bash 语法、ShellCheck、两个入口内容一致性全部通过；固定镜像的新建面板、已有面板追加和四协议实核任务全部通过。对应 `install.sh` 的 SHA256 为 `e33d8789393a170147530c543e9a371cc9721936b8d5192e9fb447621acb16e9`。这些是脚本验证结果，不是生产部署或公网测速结果。

```bash
bash -n install.sh
shellcheck -S warning install.sh install_3xui_xhttp_vlessenc.sh
cmp install.sh install_3xui_xhttp_vlessenc.sh
python3 -m unittest discover -s tests -v
```

真实面板与内核验证的可执行容器命令保存在 `.github/workflows/test.yml`；三个任务都使用临时环境和只读仓库挂载。正式部署必须使用与这些验证对应的提交，不应把“本地修改完成”“推送分支”“合入 main”和“测速服务器部署完成”混称为已发布或已部署。
