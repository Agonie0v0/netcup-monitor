[v1.2.0] - 2026-08-14

Fixed (重要修复)

- **Netcup 接口迁移**：彻底重构 Netcup 监控接口，全面废弃已下线的 legacy SOAP API (`WSEndUser?wsdl`)，升级为 Netcup 官方全新 **SCP REST API** (`scp-core/api/v1`)。
- **Keycloak OAuth2/OIDC 认证**：实现自动 Token 获取、自动刷新及内存缓存机制，同时支持 `Customer ID + 密码` (Password Grant) 与 `Refresh Token` 认证方式。
- **实例状态与限速检测**：适配全新 `Server` 与 `ServerInterface` 数据结构，精准提取各网卡 `trafficThrottled` 限速标记，并自动关联 IPv4 / IPv6 地址与实例别名。
- **向下兼容**：自动兼容现有的 `soap_config` 历史配置文件，用户无需重新迁移配置即可无缝升级。
- **依赖瘦身**：移除 `zeep` 和 `lxml` 依赖，显著降低 Docker 镜像体积并加快构建速度。
- **面板增强**：在 SCP 账号管理页面新增“测试连接”功能，方便实时校验账号有效性及实例识别情况。

[v1.1.0] - 2025-12-29

Fixed (修复)

通知系统: 修复企业微信应用 (WeChat App) 推送 Markdown 消息在普通微信客户端被折叠的问题。

新增纯文本消息生成逻辑。

企业微信应用推送类型从 markdown 更改为 text，确保消息内容直接在聊天窗口完整显示。

流控逻辑: 修复了“保留分类 (Keep Categories)”种子在解除限速后无法自动恢复运行的 Bug。

修复了当仅存在保留分类种子（无 HR 种子）时，未生成恢复标记文件的问题。现在只要处于限速状态，无论是否有 HR 种子，都会强制生成标记文件。

恢复高速状态时，现在会执行全局 resume 操作，确保被暂停的保留种子能正确启动。

兼容性: 增强 qBittorrent API 兼容性 (适配 v4.x 及 v5.x)。

将启动命令标准化为 resume (符合 API v2 标准)。

增加 API 命令失败时的自动回退 (Fallback) 机制：如果 resume 失败尝试 start，如果 stop 失败尝试 pause。

Changed (变更)

优化了 app.py 中的日志记录格式，增加了更详细的时间戳。

[v1.0.0] - 2025-05-01

Initial Release (初始发布)

基础功能发布：Netcup RS/VPS 流量监控与自动化流控。

支持功能：

对接 Netcup SOAP API 获取实时流控状态。

qBittorrent 深度集成（HR 保护、自动删种、保留分类）。

Vertex 联动（根据限速状态动态更新 RSS 规则）。

多渠道通知（Telegram Bot、企业微信 Webhook/应用）。

Web 可视化仪表盘与配置管理。
