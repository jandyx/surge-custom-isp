# Surge Mac：可复用 ISP 出口

## 菜单和链路

只新增一个可见的 `Custom-ISP` 组；`Custom-ISP-Transit` 和 `Custom-ISP-Entry` 隐藏。
原订阅的 `Claude` 组改成 `Custom-ISP` 在前，其余成员及其顺序完整保留，包括 HK、SG、JP、US 和 DIRECT。
其他订阅策略组不改。不把策略组永久冻结成手工副本。

```text
Claude → Custom-ISP → 选中的 ISP 节点 → Claude 服务
        或原订阅节点（手动选择时不经过 ISP）

连接 ISP 的路径：
  用户配置的 direct_ssids → DIRECT 接 ISP 节点
  其他网络 → 自动测速筛选的中转节点 → ISP
```

独立版不内置办公室 SSID、网关或整机 DIRECT fallback。
`direct_ssids` 只控制 ISP 节点入口，不改变其他流量的订阅规则。

`Custom-ISP` 为手动选择组，不自动在不同 ISP 出口之间 failover。
切到普通订阅节点意味着不再固定使用 ISP IP，这是用户明确的菜单选择。

## 首次使用

要求 Python 3.9+、Surge Mac 6.8+，无第三方 Python 依赖。不适用于 Clash YAML 或 Surge iOS 的后台自动维护。

```sh
make isp                        # 交互录入，默认保存在仓库外
make isp-apply PROFILE=Provider DRY_RUN=1
make isp-apply PROFILE=Provider      # 备份、校验、接入、安装 launchd
make isp-status
make isp-check
```

活动 Profile 的 setup 成功后还会安装 Panel：

ISP 行通过 `Custom-ISP` 查询 IPPure，正常只显示「地区 | IP 类型 | 风险分数」和出口 IP；不展示策略名、ASN 或 HTTP 调试信息。类型/风险属于第三方判断，缺失时显示未知，查询失败则提示异常，不伪造低风险。预期 IP 不符或 Anthropic 连通性异常时单独告警。

- 若本机已安装 `#!name=Unlock & IP Risk Panel`（包括 Surge 生成的 hash 文件名），原地追加/替换受管的 `isp_check` 两行，不复制第二份模块，也不删除用户自定义的其他 Panel 行。
- 若未安装，则使用仓库内 `assets/surge-isp-panel/` 的 vendored 模块和脚本。脚本会被部署到本机 Surge Profiles 目录，模块里的 `script-path` 渲染为带引号的本机绝对路径，以兼容 `Application Support` 这类带空格路径。
- `isp_check` 的参数来自本机私有 ISP 配置：`title=ISP&policy=Custom-ISP`，如果默认节点填了 `expected_ip`，再附加 `expected-ip=...`。仓库内资产不包含真实出口 IP 或凭据。
- `DRY_RUN=1` 不写模块、不部署脚本、不启用模块；非活动 Profile 暂缓安装 Panel，避免在还没加载 `Custom-ISP` 的 Profile 上启用一个会找不到策略的面板。选用该 Profile 后再跑一次 `make isp-apply` 即可安装。

活动 Profile 的 setup 成功后会自动检测并显示出口，无需再手动运行一次 check：

```text
[isp] Setup applied; verifying the active Claude exit
[isp] Outbound mode: rule
[isp] Testing route: Claude -> Custom-ISP -> ISP-Example
[isp] Claude policy exit IP: 192.0.2.50
[isp] Exit IP and API connectivity passed ...
```

复用此前实测的两个接口：`https://ipv4.icanhazip.com` 查询出口，
`https://api.anthropic.com/v1/models` 做无密钥连通性检查（401 是预期响应）。
前者不是 Claude 官方的 IP 查询接口；它证明的是相同策略路径的公网出口。
setup 后网络检测失败会非零退出并明确说明“配置已经应用，检测失败”，不因此回滚有效配置或换出口。
非活动 Profile 会提示暂缓验证；选用后运行 `make isp-check`。dry-run 和定时 sync 不做这些外部探测。

支持两个输入方式：隐藏粘贴 `socks5://用户名:密码@服务器:端口`（也支持 HTTP/HTTPS），
或逐项输入协议、服务器、端口、用户名、密码。URL 中的 `@ : # /` 等凭据字符需百分号编码；
不确定时使用逐项输入。密码和整条连接串不放到 make 参数或 shell 命令历史。
输入的是代理服务器地址，不是把公网出口 IP 当网关。预期出口 IP 单独填写，可留空。
节点名/州由用户指定，不使用地理数据库替用户决定。输入 `curl ...` 不会执行，而是报格式错误。

交互菜单也支持改默认节点、删除节点、编辑额外 SSID/中转正则/域名。
字段示例见 [surge-isp.example.json](surge-isp.example.json)，全部使用文档保留地址和虚构凭据。
自动化可通过 `ISP_CONFIG=/绝对路径/私有设置.json` 指定文件，不提供明文密码命令行参数。

### 从本机此前的手工配置迁移

```sh
make isp-import PROFILE=Provider                # 仅读本机旧文件并生成私有设置；不覆盖已有设置
make isp-apply PROFILE=Provider LEGACY=1 DRY_RUN=1
make isp-apply PROFILE=Provider LEGACY=1
```

只支持本会话旧版文件格式 `Claude-ISP.dconf`，有歧义（如旧凭据含逗号）则要求交互录入，
不会猜字符或覆盖其他本地路由自定义。旧文件不删除，便于恢复。

### 另一家机场

先在 Surge 中导入并更新标准 Surge 托管订阅，例如 `AnotherProvider.conf`：

```sh
make isp-apply PROFILE=AnotherProvider TARGET_GROUP='🤖 Claude'
```

原始托管文件保持不变，生成 `AnotherProvider-ISP.conf`；首次在 Surge 选用这个新入口。
若目标本来就是单一机场的本地 linked profile，则在原入口接入，保留其 General/Host 等非路由设置。
首版不接受多来源混合订阅、嵌套 include 或带未管理本地路由层的入口；目标组必须为 `select`。
找不到目标组、没有中转节点、名称冲突或可能的代理循环都会报错，不静默变成 DIRECT。
如果原订阅的其他组使用无过滤的 `include-all-proxies`，新增节点会偷偷出现在那些组里；
首版对此报错，而不是破坏“其他组选项不变”的约定。已有过滤器能排除本地 ISP 的动态组可以使用。

默认中转正则 `^🇺🇸` 适用于美国国旗开头的机场节点；其他命名方式通过 `make isp` 修改。
自建 ISP 名称不能命中这个正则，否则可能将自己纳入自己的中转池。
所有 Profile 共用本机 ISP 设置，但各自从其原始订阅合并策略组和中转候选。

## 日常更新：不需要再 setup

1. 继续在 Surge 使用原有的订阅更新功能。
2. 用户级 `local.surge-isp.sync` 每 30 秒检查本机源文件和设置文件内容。
3. 内容有变化，重新合并最新订阅。节点、策略组和规则一起生成到单个原子替换的文件，避免半新半旧。
4. `surge-cli --check` 验证候选；仅当前活动 Profile 需要热重载，非活动 Profile 只刷新文件。
5. 日常更新不会强制重置手动选择或全局/规则模式；首次接入和修改默认 ISP 时才应用默认值。

如果机场删除了正在选用的节点，Surge 自身可能回到组默认值，无法保留一个不存在的选项。
全局代理若要跟随 ISP 组选项，应选择 `Custom-ISP` 组，而不是固定某个具体州节点；工具不会擅自改变原有全局选择。

原始托管文件方案直接跟踪那个文件。URL linked profile 方案保留 `[General]` 的远程 include，
让 Surge 继续注册/更新原始订阅，并跟踪该 URL 对应的 `.managed/<MD5(URL)>.conf` 缓存。
这个缓存映射已在 Surge Mac 6.8 实机验证；升级后如果格式改变，工具明确报错并保留最近有效配置，
不能宣称支持任何未来内部格式。更新间隔仍由 Surge 控制；维护器不自行请求机场服务器。

**Reload 不等于强制拉取订阅。** 本地入口本身不是托管订阅，不能对它执行 CLI 的
`managed-profile update`（该命令只更新当前活动的托管原件）。应在 Surge 的订阅管理中
更新原始托管来源，或等待其原生自动更新；维护器跟随实际源文件内容变化，不把一次 Reload
误报为订阅刷新。若希望有明确可见的原件，优先从导入的托管 `Provider.conf` 接入，而不是 URL linked profile。

维护器运行需要 Mac 登录且唤醒；repo 移动或 Python 路径改变后运行 `make isp-watch`。
源码路径直接指向本 checkout，拉取后下次任务会使用新代码。不是 iOS 后台服务。
手动诊断同步可运行 `make isp-sync`；观察 `~/.local/state/surge-isp/isp/sync.log`。

## 安全、验证和恢复

- 设置：`~/.config/surge-isp/isp.json`，权限 0600。它是明文私有文件，不是加密保险库。
- 状态和备份：`~/.local/state/surge-isp/isp/`，目录 0700、文件 0600。
- 生成文件：Surge Profiles 目录中的 `surge-isp-*.dconf`，包含 ISP 与订阅凭据，权限 0600；不要公开分享。
- 不记录输入密码、完整订阅 URL 或包含凭据的 Surge 校验错误。预览不显示密文或明文密码。
- `DRY_RUN=1` 不写配置或安装服务；正式应用先验证候选再写，应用/运行验证失败恢复旧文件和选择。
- 发现入口/生成文件被外部修改时不覆盖。先检查自定义改动，必要时恢复备份后重新接入。
- 不触碰 DHCP、IPv6 RA、iPad 网关配置，不卸载软件，不强制重启 Surge。
- 运行核验会拒绝更高优先级的 SUBNET/DIRECT 冲突，不偷偷关闭用户模块。
- `make isp-check` 在规则模式下通过 Surge 脚本使用当前目标组（默认 `Claude`），检查该策略的出口及无密钥 Claude API 响应；
  如果菜单选了普通机场节点，会显示那个节点的出口，不误报成 ISP IP。
  全局/直连模式会明确提示绕过 Claude 组，改为测当前出站路径；检测期间不改变模式和选择。
  只想单独测试 ISP 组时，可运行 `python3 scripts/surge-isp.py check --isp-only`。
  不注入临时分流规则、不替换正在使用的 ISP。401 不等于账号/模型调用成功。
  外部探测失败会报告，不会自动切换另一个州。没有填写 expected_ip 时只能报告观察到的 IP，不能验证固定出口匹配。

```sh
make isp-restore BACKUP=/绝对路径/备份目录
```

恢复后暂停该 Profile 的后台写入，避免下一轮又覆盖恢复结果。确认后再接入。
其他 Profile 的维护不受影响。恢复和初次安装可能短暂重建连接，不保证正在传输的请求完全无感。
策略切换主要影响新连接；已有长连接可能继续使用原路径，验收时发起新请求，不把旧连接当成切换失败或新出口证据。

## 开发验证

CLI 固定日志、错误、交互提示及帮助统一使用英文，采用英文输出约定。
操作日志带 `[isp]` 前缀，错误写 stderr；探测 JSON 保持机器可读。
用户/订阅提供的节点名、策略组名和 SSID 保持原样，不翻译。说明文档保留中文。
已有历史日志不重写，后续运行采用新文案。

```text
[isp] Merged Provider.conf; default ISP=ISP-Example; backup=...
[isp] Configured default ISP: ISP-Example; nodes: ...
[isp] ERROR: The target group must be select; refusing to change its semantics implicitly
```

```sh
make test-isp
python3 -m unittest discover -s tests -p '*_test.py'
```

测试使用虚构地址/凭据和假的 Surge 控制器，覆盖组顺序、订阅增删、默认值、隐藏组、SSID 去重、
输入转义、循环/冲突拒绝、幂等、更新自动合并、选择保留、回滚、托管源不被覆盖及非活动 Profile。
这些离线测试不等于在其他人的网络、机场或 iOS 上完成实测。

参考：[分离配置](https://manual.nssurge.com/profile/format.html)、
[策略组导入](https://manual.nssurge.com/policy-groups/policy-including.html)、
[脚本 HTTP policy 参数](https://manual.nssurge.com/scripting/api.html)。
