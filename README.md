# surge-custom-isp

面向个人用户的 Surge Mac ISP 出口配置工具，从原项目独立提取。
仅需 **macOS、Python 3.9+、Surge Mac 6.8+**，Python 无第三方依赖。

## Proxy-Cheap 一条龙（推荐入口）

先安装并打开 Surge Mac，导入自己的标准 Surge 托管订阅。
在 [Proxy-Cheap API 密钥页](https://app.proxy-cheap.com/api-keys) 点击「生成新的 API 密钥」，准备同一对 **API 密钥 + API 密钥（私钥）**，不要发到聊天。

```sh
make isp-auto
# 仅预览，不切协议/写配置/激活 Profile：
make isp-auto DRY_RUN=1
```

按菜单隐藏输入两项密钥 → 多选已有静态 ISP → 选择默认出口 → 选择 Surge Profile/目标组/中转节点 → 按需确认切 SOCKS5 → 确认应用。
自动获取代理地址和认证信息，激活生成的 Profile，安装 Panel 和后台同步，最后在终端输出当前线路及出口连通性检测。
只检测当前线路，不是逐节点测速；API 密钥不保存，选中代理凭据保存在本机私有配置。
协议切换可能影响其他客户端；之后取消不会自动撤销已完成的远端切换。无购买、续费或白名单操作。

## 其他供应商手动配置

```sh
make isp
make isp-apply PROFILE=YourProfile TARGET_GROUP=Claude DRY_RUN=1
make isp-apply PROFILE=YourProfile TARGET_GROUP=Claude
make isp-status
make isp-check
```

将 Profile 和目标组替换为自己的值。手动路径默认中转正则 `^🇺🇸`，可通过 `make isp` 修改。
详见 [完整说明](docs/surge-isp.md)。

## 独立版边界

- 不含原组织的办公室、网关、内网工具或私有配置；不内置办公室直连规则。
- `direct_ssids` 只让 ISP 入口直连，不把整机流量切为 DIRECT。
- 私有设置：`~/.config/surge-isp/isp.json`；状态/备份：`~/.local/state/surge-isp/isp/`。
- 后台服务：`local.surge-isp.sync`。不要让原版与独立版同时管理同一个 Surge Profile。
- 不包含真实代理凭据；用交互式输入录入，不把密码放进命令行或提交到 Git。
- Panel 查询第三方 IPPure 元数据，并请求 Anthropic API 检查连通性；401 不代表账号可用。
- 不支持 Clash YAML 或 Surge iOS 后台维护。

详见 [使用、迁移、恢复说明](docs/surge-isp.md) 和 [配置示例](docs/surge-isp.example.json)。

## 配置 skill

已附带项目级 skill：[surge-isp-setup](.agents/skills/surge-isp-setup/SKILL.md)。
在支持项目 `.agents/skills` 的客户端打开本项目，调用 `$surge-isp-setup`。
包含必填/可选字段、隐藏录入、预览应用、验证与恢复。若客户端只支持全局 skills，
可将整个 `surge-isp-setup` 文件夹复制到对应 skills 目录；仍需保留本项目代码和文档。

Proxy-Cheap API 调研见 [接入说明](docs/proxy-cheap-api.md)。

## 开发验证

```sh
make test-isp
python3 -m compileall -q scripts tests
node --check assets/surge-isp-panel/isp_check.js
```

测试不访问真实 Surge 或代理，不代表外部用户网络已实测。
仓库未自行为原有代码或第三方资产授予新许可证；公开发布前需确认相应授权。
