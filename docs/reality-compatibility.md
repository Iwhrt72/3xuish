# REALITY 目标站点与手机客户端兼容性

将微软目标换成苹果后恢复连接，说明这个部署的故障与目标站点或握手组合有关。不能仅凭面板的零流量、普通 TLS 握手成功，或其他用户的报错，认定本次一定是证书记录超过 8 KiB。

## 8273 字节与旧 8192 字节限制

[Xray 官方问题 #6356](https://github.com/XTLS/Xray-core/issues/6356) 记录了微软某些 TLS 响应的 Certificate 记录总长度达到 8273 字节，旧 REALITY 的 8192 字节记录缓冲区无法处理。这里说的是 TLS 记录，并不是微软所有证书都固定为 8273 字节，也不是 RSA 密钥的 8192 位限制。

[REALITY 修复 PR #33](https://github.com/XTLS/REALITY/pull/33) 于 2026-09-08 合入，最终缓冲区为 `17 * 1024` 字节。当前可查的 Xray `v26.9.9`、`v26.9.30`、`v26.10.21` 和 `v26.10.29` 使用含该修复的 REALITY；`v26.7.28` 仍使用旧的 8192 字节实现。依据：[v26.9.9 依赖](https://github.com/XTLS/Xray-core/blob/v26.9.9/go.mod)、[v26.10.21 依赖](https://github.com/XTLS/Xray-core/blob/v26.10.21/go.mod)、[对应 REALITY 源码](https://github.com/XTLS/REALITY/blob/83f621788ca8/tls.go)。

仓库固定测试镜像中的 Xray 为 26.10.21。新增的隔离测试使用同一证书与私钥，分别构造小、大证书链：Certificate 记录为 838 和 10534 字节，两者均完成 TLS 1.3/h2 握手，并通过 REALITY 完整传输 1 MiB，SHA256 校验一致。这证明该测试内核能处理超过旧上限的记录，不能据此证明客户的手机已经兼容。

## 新部署的默认行为

新装、清空重装及新增四节点，REALITY 默认使用：

```text
服务端 target：      www.apple.com:443
服务端 serverNames： ["www.apple.com"]
客户端 SNI：         www.apple.com
```

仍可通过 `REALITY_SERVER_NAME` 和 `REALITY_TARGET` 显式指定其他目标。苹果并非所有地区、网络和客户端的通用保证，脚本不会因为站点名称属于微软就一律拒绝。

目标预检要求握手成功、TLS 1.3、X25519、证书名称验证以及 ALPN `h2`。预检只验证服务器到目标站点的基本 TLS 能力，不等同于真实 REALITY 握手，更不等同于小火箭、手机网络或公网节点可达性测试。

## 已有节点怎样处理

下载或重新打开新版脚本，不会自动迁移现有 REALITY 节点。`--repair-performance` 只修复旧 TCP 参数，不能用来切换 REALITY 目标。无需为了改目标而清空重装。

先记录服务器实际 Xray 版本、小火箭版本和最小客户端版本设置。服务端版本可在面板查看；默认 amd64 安装也可运行：

```bash
/usr/local/x-ui/bin/xray-linux-amd64 version
```

若确认需要采用已实测可用的新目标，应先备份，然后在面板同步修改服务端 `target`（部分界面称 `dest`）、`serverNames`，并在手机刷新订阅或更新手动导入节点的 SNI。保持原 UUID、密钥、公钥、shortId、端口和 flow；目标切换本身不需要重新生成这些认证。不要只改节点备注，也不要关闭证书或 REALITY 认证来绕过故障。

面板动态订阅与安装时保存的静态分享链接文件不同。手动改面板后，`/root/3x-ui-node-links.txt`、profiles 等历史文件不会自动同步；请从面板重新导出当前链接，不要继续分发旧 SNI 的静态链接，也不要用旧资料覆盖已修改的配置。脚本再次发现保存资料与当前配置不一致时会停止校验，不能据此认为新目标失效。

[官方问题 #6482](https://github.com/XTLS/Xray-core/issues/6482) 还存在客户端版本限制和小火箭兼容性的相关讨论。这些报告不能与旧证书记录上限混为一个根因，也不足以证明客户的具体故障。若新内核下仍失败，需要结合手机端日志和服务端时间对应的错误再判断；不建议仅凭这一现象降级到旧内核。
