# HTTPS 传输加密（gatewayTls）

> 适用版本：V0.9.1（HTTPS 自 V0.9.0 起；V0.9.1 起 LAN IP 漂移会重载在线 Caddy 的 TLS 站点绑定）。
> 目标：保护 LAN 管理凭据与应用登录的传输安全——明文只存在于回环。

## 1. 支持矩阵

| 网关 | gatewayTls=internal | 说明 |
| --- | --- | --- |
| caddy | ✅ | Caddy 内嵌 CA（internal CA）自动签发，证书 SAN 覆盖 `127.0.0.1` 与当前 LAN IP |
| builtin | ❌ | `http.server` 无 TLS 能力；配置校验直接拒绝 `builtin + gatewayTls=internal` |
| nginx | ❌ | 未实现（白名单占位） |

TLS 失败（证书损坏、端口被占、验证不通过）时 `lwa gateway on` **显式报错，绝不静默回退明文**。

## 2. 开启步骤

`local-web.yml`：

```yaml
staticGateway: caddy      # 前置条件
gatewayTls: internal      # off（现状，明文）| internal
gatewayTlsPort: 8443      # HTTPS 别名入口
managerTlsPort: 9443      # 管理面独立 HTTPS origin（不是同源 /manager/ 路径）
# gatewayPlainPort: 8080  # 可选保留明文入口——非安全边界，默认不设（关闭）
instanceBindHost: 0.0.0.0 # 建议收敛为 127.0.0.1（见 §5）
```

然后：

```bash
lwa gateway on            # 首次启动会初始化 internal CA 并签发证书
lwa manager on            # manager 自动收敛为仅回环监听（LAN 经 :9443 反代）
lwa ca export             # 导出根证书 + SHA-256 指纹 + 各平台安装指引
lwa doctor                # gateway_tls 检查项：根证书/https 验证/明文收敛
```

访问地址变为：

- 实例别名：`https://<LAN-IP>:8443/<alias>/`
- 管理页：`https://<LAN-IP>:9443/`（独立端口 = 独立 origin；本机仍可 `http://127.0.0.1:17800/` 直连）

## 3. 客户端信任（必做）

Caddy internal CA 的根证书**不会**被客户端自动信任（Caddy 官方 Automatic HTTPS 文档明确：服务端自动安装只覆盖本机系统，不保证其他客户端）。两步：

1. `lwa ca export --out lwa-root-ca.crt` 拷贝到客户端（注意核对打印的 SHA-256 指纹，防导出/传输环节被替换）；
2. 按命令输出的指引安装并**完全信任**（macOS 钥匙串 / Windows 证书存储 / Linux update-ca-certificates / Firefox 单独信任库 / iOS 证书信任设置 / Android CA 证书）。

**禁止**「点穿浏览器证书告警」继续访问——那等于把明文中间人攻击升级为换证书中间人攻击。未安装根证书时看到的告警就是应有的行为。

curl / Agent 客户端：`curl --cacert lwa-root-ca.crt https://…`；Python：`ssl.create_default_context()` 后 `load_verify_locations(cafile=…)`。LWA 自身探活（`lwa access review`、别名活验证、gateway 启动验证）已内置根证书验证——错误证书按失败呈现。

## 4. 明文入口收敛

| 通道 | TLS 开启后 |
| --- | --- |
| 别名入口 :8080 | 默认关闭（除非显式设 `gatewayPlainPort`；保留时**不是安全边界**——IP 访问无 HSTS，重定向可被在径剥离） |
| manager :17800 | 绑定强制收敛 `127.0.0.1`（LAN 经 `https://<ip>:9443/` 反代；显式 `--host` 为调试放行——仅 WARNING 提示且不收敛绑定，明文管理面会直接暴露，生产勿用） |
| 实例直连 :18000+ | 由 `instanceBindHost` 决定（见 §5） |

回环内的明文（别名反代上游 `127.0.0.1:<hostPort>`、探活、M1 Agent stdio 桥）**保持明文**——收敛目标是"明文只存在于回环"，回环无在径攻击者。

## 5. 实例直连收敛（instanceBindHost）

设 `127.0.0.1` 后三处同步绑定：builtin 网关 `--bind`、Caddy 站点块 `bind`、Docker 端口发布 `127.0.0.1:<host>:<container>`（IPv6 需 `::1`）。收敛后：

- LAN 只剩网关入口（8443/9443）——"单入口 + 加密"；
- `lanUrl` 的直连语义退化（直连口仅本机可达），路径别名成为唯一 LAN 入口；无别名的实例在 LAN 不可直连，`lwa access review` / `lwa doctor` 会给出迁移提示（设别名或保持宽松绑定）。

**Docker/防火墙注意**（官方 Docker 文档 Packet filtering and firewalls）：Docker 发布端口的流量在 iptables FORWARD 链处理，可能绕过普通 ufw 规则——收敛与否都以**实际端口扫描**为准做验收；显式 host-ip 发布（`instanceBindHost=127.0.0.1`）只绑该地址，是比防火墙规则更可靠的收敛方式。IPv6 同理：Caddy 端口通配监听双栈，Docker 显式 `127.0.0.1` 发布不会开放 v6。

## 6. 反代与鉴权语义

- manager 经 `https://<ip>:9443/` 反代访问时，**远程管理强制凭据**：回环免鉴权判定会解析 X-Forwarded-For 链——LAN 客户端伪造 `Host: 127.0.0.1` 不能获得本机免 token 待遇；仅当 XFF 链全回环（本机客户端经反代）或无 XFF（直连本机）才按本机对待。
- M1 Agent stdio 桥继续走严格回环明文 HTTP（`127.0.0.1:17800`），不受影响。
- XFF 口径：TLS 模式下**无** X-Forwarded-For 头的回环请求视为直连本机；**带** XFF 的请求要求整链全回环才算本机——本机脚本若被中间层注入了垃圾 XFF，会失去回环 GET 免 token 豁免（按远程对待，属有意的保守方向）。
- Cookie 语义：管理页凭据是 Bearer token（无 Cookie 会话），端口构成的 origin 区分用于避免与实例入口同源；如未来引入 Cookie 需重新评审（Cookie 不按端口隔离）。

## 7. LAN IP 变化与运维

证书 SAN 覆盖签发时的 LAN IP。`lanIpStrategy: auto` 时，地址刷新（管理页列表节流、daemon 周期检查或 `lwa access refresh`）若发现主 Caddyfile 的 8443/9443 仍绑旧 IP 或只剩回环，且 Caddy 已在线，会按当前 LAN IP 重写并 reload；两个端口分别判断，不会因为别名入口已更新就放过管理面。网关处于关闭状态时不会被这次检查重新拉起，下次 `lwa gateway on` 会按当前 IP 重写。reload 失败时 manifest 已更新，可再执行 `lwa gateway on`。固定 IP 环境建议 `lanIpStrategy: manual` + `manualLanIp`。

验收清单（至少两台设备）：

1. 安装根证书后 `https://<ip>:8443/<alias>/` 与 `https://<ip>:9443/` 无告警；
2. 未装证书的设备得到硬告警；
3. 明文拒绝：`http://<ip>:8080`（默认关）、`http://<ip>:17800`（回环化）不可达；
4. 无旁路：`instanceBindHost=127.0.0.1` 时 LAN 扫描仅剩 SSH 与 8443/9443（以实际扫描为准）；
5. `lwa services restart` 后 TLS 入口恢复；证书目录损坏时 gateway 启动显式报错不降级。

## 8. 远程 Agent 状态

HTTPS 启用**不自动开放远程 Agent**——仍需 M2 独立身份、scope/实例 ACL 与远程协议验收（`agent-info.json` 的发现端点仍仅声明本机 stdio 通道）。
