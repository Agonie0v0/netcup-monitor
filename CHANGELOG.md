[v1.3.0] - 2026-08-14

### Added (新增功能与深度优化)

- **双轨流量监控 (Dual-Track Traffic)**：集成 Netcup 官方网卡月度计费流量 (`rxMonthlyInMiB` / `txMonthlyInMiB`) 与 qBittorrent 应用层做种流量，并在 Web 仪表盘清晰并轨展示，便于分析偷跑流量与计费限额。
- **Delta 异常穿透保护 (Traffic Anti-Reset Protection)**：重构流量增量计算引擎，智能过滤 qB 重启或计数器重置导致的虚假流量暴增 Bug。
- **即时事件告警 (Instant Event Alerts)**：当检测到服务器发生限速或限速恢复时，触发秒级突发事件推送，详细汇报流控执行动作；与日常定时简报解耦。
- **并发多实例巡检引擎 (Concurrent Polling)**：基于 `ThreadPoolExecutor` 并发巡检各 VPS 实例及 qB 状态，大幅缩短巡检耗时（从数十秒降至秒级）。
- **停机看门狗 (Heartbeat Watchdog)**：智能识别监控服务停机与断点时间，避免在宕机期间虚假累计限速时长。
- **数据库高效索引与自动归档**：为 SQLite 数据表建立复合索引，并新增每日凌晨定时数据修剪与 VACUUM 维护机制，防止数据库体积膨胀。
- **安全加固与防爆破**：登录认证升级为加盐安全哈希 (PBKDF2)，并对登录接口施加 IP 速率限制与防暴力破解策略。
- **一键诊断工具**：面板新增一键测试所有/单台 qB 连通性、一键测试通知通道、一键测试 SCP REST 连接。

[v1.2.0] - 2026-08-14

### Fixed (重要修复)

- **Netcup 接口迁移**：彻底重构 Netcup 监控接口，全面废弃已下线的 legacy SOAP API (`WSEndUser?wsdl`)，升级为 Netcup 官方全新 **SCP REST API** (`scp-core/api/v1`)。
- **Keycloak OAuth2/OIDC 认证**：实现自动 Token 获取、自动刷新及内存缓存机制，同时支持 `Customer ID + 密码` (Password Grant) 与 `Refresh Token` 认证方式。
- **实例状态与限速检测**：适配全新 `Server` 与 `ServerInterface` 数据结构，精准提取各网卡 `trafficThrottled` 限速标记，并自动关联 IPv4 / IPv6 地址与实例别名。
- **向下兼容**：自动兼容现有的 `soap_config` 历史配置文件，用户无需重新迁移配置即可无缝升级。
- **依赖瘦身**：移除 `zeep` 和 `lxml` 依赖，显著降低 Docker 镜像体积并加快构建速度。
- **面板增强**：在 SCP 账号管理页面新增“测试连接”功能，方便实时校验账号有效性及实例识别情况。

[v1.1.0] - 2025-12-29

### Fixed (修复)

- **通知系统**：修复企业微信应用 (WeChat App) 推送 Markdown 消息在普通微信客户端被折叠的问题。
- **流控逻辑**：修复了“保留分类 (Keep Categories)”种子在解除限速后无法自动恢复运行的 Bug。
- **兼容性**：增强 qBittorrent API 兼容性 (适配 v4.x 及 v5.x)，增加自动回退 (Fallback) 机制。

[v1.0.0] - 2025-05-01

### Initial Release (初始发布)

- 基础功能发布：Netcup RS/VPS 流量监控与自动化流控。
- 对接 Netcup SOAP API 获取实时流控状态。
- qBittorrent 深度集成（HR 保护、自动删种、保留分类）。
- Vertex 联动（根据限速状态动态更新 RSS 规则）。
- 多渠道通知（Telegram Bot、企业微信 Webhook/应用）。
- Web 可视化仪表盘与配置管理。
