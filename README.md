# 3x-ui 一键部署脚本 (XHTTP + VLESS Encryption + Vision + IP/域名证书)

本仓库提供基于 [MHSanaei/3x-ui](https://github.com/MHSanaei/3x-ui) 的全自动一键部署与后期域名平滑切换脚本，集成了以下特性：

1. **一键申请 Let's Encrypt IP 证书（默认）**：无需购买域名，直接使用服务器公网 IPv4 申请 Let's Encrypt 官方短期证书（`shortlived` 模式，有效期约 6 天，通过 `acme.sh` 定时任务全自动续期并重载 `x-ui`）。
2. **支持后期一键无缝切换为域名证书**：前期用裸 IP 部署后，后期如果购买并解析了域名，无需重装面板或重建节点，一条命令即可自动申请 90 天域名证书并同步更新面板与节点的 TLS/SNI 配置。
3. **自动配置 `VLESS + XHTTP + VLESS Encryption + Vision` 节点**：
   - 自动调用 Xray 内核 (`xray vlessenc`) 生成原生 `VLESS Encryption` 密钥对（支持 `X25519` 或抗量子 `ML-KEM-768`）。
   - 自动开启 `xtls-rprx-vision` 流控（Xray 支持在 `VLESS Encryption` 隧道内运行 Vision 流控）。
   - 自动通过 `3x-ui` API 创建入站节点并输出 `vless://` 一键导入链接。

---

## 一键拉取部署命令

> 如果刚开通的纯净系统提示 `curl: command not found`，先运行 `apt update && apt install -y curl`（Ubuntu/Debian）或 `yum install -y curl`（CentOS/Rocky）。

### 1. 交互式一键部署（推荐，直接按回车即用安全默认值）

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh)
```

### 2. 全自动零交互一键部署（无人值守，全部使用默认随机安全参数）

```bash
AUTO_YES=1 bash <(curl -Ls https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh)
```

### 3. 后期配上域名后：一键无损切换为域名证书

将域名 `A 记录` 解析到服务器公网 IP 后（若使用 Cloudflare 需先关闭小黄云设为“仅 DNS”），直接运行：

```bash
bash <(curl -Ls https://raw.githubusercontent.com/Iwhrt72/3xuish/main/install.sh) --switch-domain 你的域名.com
```

---

## 部署后查看面板与节点信息

所有生成的面板地址、账号密码、证书路径以及 `vless://` 分享链接均自动备份在服务器上，随时可通过以下命令查看：

```bash
cat /root/3x-ui-xhttp-info.txt
```
