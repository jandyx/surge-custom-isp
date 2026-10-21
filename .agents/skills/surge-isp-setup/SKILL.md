---
name: surge-isp-setup
description: 用 Proxy-Cheap API Key 和 Secret 引导多选静态 ISP、切换 SOCKS5、配置 Surge Mac 并输出出口检测；也支持手动录入和恢复，不用于购买代理、Clash 或 Surge iOS。
---

# Surge ISP 一条龙配置

## 定位项目与前提

定位含 `scripts/proxy-cheap-setup.py`、`scripts/surge-isp.py`、`Makefile` 的项目根目录；不得硬编码用户名或工作区路径。先读 `docs/surge-isp.md`，API 细节见 `docs/proxy-cheap-api.md`。
需要 macOS、Python 3.9+、curl、运行中的 Surge Mac 6.8+，以及已在 Surge 导入的标准托管订阅。用户还需要已购买且 ACTIVE 的静态 ISP；本工具不购买或续费代理。
若没有合适订阅或 select 类型策略组，解释具体缺项，不创建虚构节点，不修改 DHCP/系统网关，也不要求用户提供订阅密码到聊天。

## 1. 获取两项 API 凭据

引导用户在自己的浏览器打开 https://app.proxy-cheap.com/api-keys ，登录后进入「API 密钥」，点击「生成新的 API 密钥」。必须获取同一对的两项：

- 「API 密钥」→ `X-Api-Key`。
- 「API 密钥（私钥）」→ `X-Api-Secret`。

创建时间无需输入。这不是账户登录密码，也不是 SOCKS5 用户名/密码。官方未提供已确认的第三方 OAuth 接入流程，不虚构 OAuth 登录。
密钥只在本地终端隐藏输入，不让用户发到聊天、截图、命令参数或 Git；不读取浏览器会话。已暴露的密钥应撤销并重新生成。API 密钥不落盘；选中代理的连接凭据会作为必要配置以 0600 明文保存。

## 2. 启动唯一入口

在项目根目录运行：

```sh
make isp-auto
```

仅预览可用 `make isp-auto DRY_RUN=1`；预览不会切协议、写设置或激活 Profile，若节点尚不是 SOCKS5，会指出需要切换并停止。
自定义设置路径用 `ISP_CONFIG=/绝对路径/私有设置.json`，之后手动维护命令始终使用同一路径。

此入口要求真实 TTY。秘密必须由用户直接输入本地终端，不能将其经 agent 工具参数代输。若当前客户端不能让用户直接操作该终端，提供上述命令让用户本地运行；不要让无人输入的交互进程长期挂起。

## 3. 菜单操作及必需输入

引导用户按实际菜单顺序操作：

1. 隐藏输入 API Key 和 Secret。
2. 多选代理，输入编号如 `1,3`。只列出 ACTIVE 静态 ISP，显示 ID、国家和协议，不显示密码。输入 `q` 取消。
3. 从所选代理中选择默认出口。
4. 从本机支持的订阅/Profile 中选择接入入口。
5. 从原订阅 select 策略组中选择目标组，通常是 Claude。已绑定入口只能沿用原目标组。
6. 多选原订阅中用于中转的节点。工具生成精确匹配正则，让 Surge 自动测速择优，不猜节点命名。
7. 同 ID 的 `PC-ISP-<id>` 已存在时，询问是否更新；保留其他节点。让用户检查所选节点、默认出口、Profile 与目标组的预览。
8. 若选中代理不是 SOCKS5，先检查可用协议，再逐节点询问是否切换。说明切换可能影响其他使用同一代理的客户端。用户不授权则停止，不强制切换、不降级 HTTP。
9. 最后确认是否保存、接入、激活 Profile、安装 Panel/后台同步并检测。明确提示连接可能中断，且其他已绑定 Profile 共用本机 ISP 设置。

不需要用户手填代理地址、端口、用户名、密码或出口 IP，均从 API 获取。只提供密钥不能替用户决定使用哪些代理/订阅/策略组；这些通过菜单选择，不能擅自默认全部接入。

## 4. 自动处理与结果报告

工具会核实 SOCKS5、刷新连接信息、检查配置，保存私有设置，调用现有备份/校验/合并逻辑，保留托管订阅原件，激活本地 ISP 入口，再安装 Panel 和 `local.surge-isp.sync` 后台服务。
API 只允许读取代理与查询/切换 SOCKS5，不调用购买、续费、白名单或其他管理接口。

终端输出当前出站模式、实际策略链、出口 IP、预期 IP 比对及 Anthropic 连通结果。报告边界：

- 只检测当前所选路径，不代表每个导入节点都已经实测，也不是带宽测速。
- HTTP 401 是无密钥 API 探测的预期响应，不证明账号或对话可用。
- 全局/直连模式可能绕过目标组，按实际输出解释，不强制改模式。
- API 成功、配置应用、Profile 激活、Panel 安装、后台服务、出口检测是不同阶段；未完成的阶段不得声称成功。
- “Settings applied, but … did not finish” 表示设置已应用，不能说未改动；先看状态再修复，不盲目反复重跑。
- 远端协议切换成功后，后续取消或本地失败不会自动切回 HTTP，需如实报告；不要为了测试来回切换。

默认文件：`~/.config/surge-isp/isp.json`；状态/备份：`~/.local/state/surge-isp/isp/`。生成文件含代理及订阅凭据，不可公开分享。不要让原项目与独立版同时管理同一个 Profile。

## 5. 验证、维护与恢复

```sh
make isp-status
make isp-check
make isp-sync
make isp-watch
make isp-restore BACKUP=/绝对路径/备份目录
```

恢复必须指定正确备份，会暂停该 Profile 自动维护；它不是供应商协议切换的回滚。订阅更新由 Surge 完成，sync 不强制拉取远程订阅。项目/Python 路径移动后用 watch 更新后台路径。
遇到错误只分享脱敏状态，不让用户粘贴完整 JSON、生成配置或含凭据日志。403 不等于密钥错误，先区分网络/访问防护与认证；不绕过授权限制。

## 手动替代路径

不用 Proxy-Cheap 时运行 `make isp`。填写唯一节点名、协议、供应商连接地址、端口、隐藏用户名/密码，选填预期出口 IP；必须区分连接地址和出口 IP。可隐藏粘贴代理 URL，特殊字符需百分号编码，不接受 curl 命令。
该通用手动入口保留 HTTP/HTTPS；Proxy-Cheap 自动流程则仅 SOCKS5。
`make isp` 每次一个操作，可重复新增节点或修改网络设置（中转正则、直连 SSID、目标域名）。直连 SSID 默认空，仅控制 ISP 入口，不生成全局直连。
然后使用 `make isp-apply PROFILE=用户入口 TARGET_GROUP=目标组 DRY_RUN=1` 预览，经授权后移除 DRY_RUN 应用。详细字段示例见 `docs/surge-isp.example.json`。
