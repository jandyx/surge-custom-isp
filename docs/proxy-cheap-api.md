# Proxy-Cheap API 接入可行性

核查日期：2026-10-20。现已提供 `make isp-auto` 交互入口；已获用户授权完成一次真实账户只读列表验证（结果见下文）。

## 官方文档确认

- `GET https://api.proxy-cheap.com/proxies` 列出代理；响应包含状态、类型、连接地址/端口与认证字段。
- 请求使用 `X-Api-Key`、`X-Api-Secret`，从 [API Keys 控制台](https://app.proxy-cheap.com/api-keys) 获取。
- SOCKS5 映射：`connection.connectIp` → server，`connection.socks5Port` → port，`authentication.username/password` → 代理认证。`connection.publicIp` 与连接地址不同。
- `GET /proxies/:id/change-protocol` 可查询当前及可用协议。仅出现 SOCKS5 端口字段不等于已启用该协议。
- 未找到公开 OAuth 授权接入规范；不能将网站社交登录等同于第三方 API OAuth。

来源：[Proxy-Cheap 官方 API 文档](https://docs.proxy-cheap.com/)。

## 本地交互流程

1. 本地终端隐藏输入 API Key 与 Secret，仅在内存保存；不得要求用户发到聊天。
2. 只读拉取列表，不购买、续费、改白名单或切换远端协议。
3. 显示编号、供应商 ID、国家、类型、状态和到期时间，不展示密码。允许输入多个编号，再单独选择默认节点。
4. 默认只接受 ACTIVE 的静态 ISP，核实 SOCKS5 可用且已有非空用户名/密码。状态、协议、端口或认证不满足时标明原因并跳过，不擅自修改供应商账户。
5. 生成稳定且不命中中转筛选的节点名；同 ID 更新需预览，不删除用户其他节点。同名冲突拒绝静默覆盖。
6. 合并后复用本项目校验与 0600 原子写入；取消或 API 失败不写文件。若已有后台绑定，保存本地设置也可能触发同步，必须先明确告知。
7. 重用现有 dry-run / apply / check 流程。API 导入成功不等于代理可连接。

实现需处理超时、401/403、429、非 JSON、异常字段、空列表及响应体上限；禁止把原始响应/HTTP 异常正文打到日志。禁止携带认证头跟随未知重定向。
公开示例不足以确认所有产品的分页、协议同时可用性和区域字段；不可猜参数或凭 IP 推断州。真实账户验证前保留这些限制。

## 真实只读验证（2026-10-20）

- 使用 curl 请求 `GET /proxies`，两项认证头配对使用，返回 HTTP 200。
- 本次实际 JSON 外层为对象，列表位于 `proxies` 字段，不是文档示例中的顶层数组。实现时要显式兼容这两种结构，未知结构报错，不当成空列表。
- 本次返回 2 个 ACTIVE、RESIDENTIAL_STATIC、SOCKS5 节点，均具备连接主机、SOCKS5 端口、用户名、密码和公网 IP 字段。这里只保留数量和结构，不保存账户响应或真实凭据。
- 先前 Python urllib 请求返回 403，curl 成功；尚未隔离根因，不能断言一定是 User-Agent，也不能将所有 403 都判为密钥错误。
- 仅证明 API 认证及代理信息获取成功；未通过这些代理连接外网，未修改本机配置或供应商账户。

## 用户确认的协议约束

Proxy-Cheap 接入仅使用 SOCKS5：控制台「连接协议」选 SOCKS5，API 中核实 `proxyType == "SOCKS5"`，映射 `connection.socks5Port`，本地 `protocol=socks5`。HTTP/HTTPS 不导入，不自动降级或擅自修改远端协议。此为自动导入功能的协议约束。

## 已确认的协议切换写入接口

官方页面预加载的 Postman 集合包含 `Change the protocol`：

```text
POST https://api.proxy-cheap.com/proxies/:id/change-protocol
Content-Type: application/json
请求体：{"newType":"SOCKS5"}
```

官方成功示例为 HTTP 201、空对象响应。先 GET 同一路径检查 `currentType` / `availableTypes`，用户授权修改选中节点后才 POST，再 GET 与刷新列表核实结果。已为 SOCKS5 时不写入。字段名是 `newType`，不是 `type` 或 `proxyType`。

来源：[官方文档](https://docs.proxy-cheap.com/)；[该页面加载的官方 Postman 集合](https://docs.proxy-cheap.com/api/collections/36507349/2sB2ixmEUJ?segregateAuth=true&versionTag=latest)。核查日期 2026-10-20。

该接口仅完成文档确认，未对真实账户执行写操作；导入和授权切换已由 `make isp-auto` 串联，真实写操作仍未实测。控制台切换是手动替代方案，不是唯一方式。

## 一条龙入口与验证范围

运行 `make isp-auto`，按菜单输入两项 API 凭据并选择代理、默认节点、Profile、目标组与中转节点。程序在明确确认后执行协议切换、本地接入、Profile 激活、Panel/后台任务安装和当前出口验证。`DRY_RUN=1` 只读预览。
实现见 `scripts/proxy-cheap-setup.py`，离线回归见 `tests/surge_isp_api_test.py`。密钥通过 stdin 传给 curl，关闭 curl 用户配置，不跟随重定向，不记录原始响应。失败不自动重试写请求；未知分页拒绝导入，防止静默只取部分列表。
自动流程保留其他本地节点；同 ID 节点更新需要确认。共享设置变更可能影响其他绑定 Profile。真实账户仅完成历史只读列表验证，自动协议切换及完整 Surge 环境尚未实机验收。
