<div align="center">

  # 📊 Netcup Monitor Pro

  **专为 Netcup RS/VPS 打造的智能化流量监控与自动化流控面板**

  <p>
    <img src="https://img.shields.io/badge/Docker-ghcr.io%2Fagonie0v0%2Fnetcup--monitor-blue?logo=docker" alt="Docker Image">
    <img src="https://img.shields.io/badge/Python-3.9+-yellow?logo=python" alt="Python">
    <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
  </p>

  <p>
    <a href="#features">功能特性</a> • 
    <a href="#logic">工作原理</a> • 
    <a href="#deploy">部署指南</a> • 
    <a href="#config">配置详解</a> • 
    <a href="#disclaimer">免责声明</a>
  </p>
</div>

---

## 📖 项目简介

**Netcup Monitor Pro** 解决了 Netcup 用户最大的痛点：**限速（Throttling）后的自动化处理**。

不同于普通的监控脚本，本项目通过对接 Netcup 官方 SOAP API 精准识别服务器状态。当检测到限速时，它能自动指挥 **qBittorrent** 和 **Vertex** 进行精细化的流量规避，并在限速解除后自动恢复生产，实现真正的“无人值守”。

<h2 id="features">✨ 功能特性</h2>

### 🛡️ 智能流控策略
* **HR 智能保护**：限速期间，对 **下载中** 的 HR 种子自动暂停，对 **做种中** 的 HR 种子限制上传速度（保种模式），防止因删种导致 HR 考核失败。
* **分类管理**：
    * **保留分类 (Keep)**：限速时仅暂停任务，保留文件。
    * **自动清理**：非保留、非 HR 分类的种子，在限速时自动删除以释放空间。
* **自动恢复**：检测到限速解除（恢复高速）后，自动恢复所有暂停任务并解除速度限制。

### 🔗 生态联动
* **Vertex 深度集成**：
    * **智能选机**：根据服务器限速状态，动态更新 Vertex 的 RSS 下载规则，确保任务只分发给未限速的机器。
    * **容器自愈**：规则更新后支持自动重启 Vertex 容器（需挂载 Docker Socket）。
* **qBittorrent 全管**：支持多实例管理，直接通过 API 控制种子状态。

### 📊 可视化面板
* **流量趋势图**：内置 7 日流量消耗折线图与健康度分析。
* **实时监控**：查看当前上传/下载速度、今日流量、本月流量。
* **健康报告**：记录每日限速时长、日均限速分析。
* **Web配置**：所有参数（账号、策略、通知）均可在网页端热修改，无需重启容器。

### 🔔 多渠道通知
* **Telegram Bot**：支持富文本状态简报。
* **企业微信**：支持 Webhook 机器人及企业微信应用（App）推送。

<h2 id="logic">🧠 自动化策略逻辑</h2>

系统每 5 分钟（默认）检测一次状态，根据 Netcup API 返回的 `trafficThrottled` 状态执行以下逻辑：

| 种子类型/分类 | 🟢 正常状态 (高速) | 🔴 限速状态 (低速) |
| :--- | :--- | :--- |
| **HR (做种中)** | 🚀 全速上传 | 🛡️ **限速上传** (默认 10KB/s) |
| **HR (下载中)** | 🚀 全速下载 | ⏸️ **暂停下载** |
| **保留分类 (Keep)** | ▶️ 恢复运行 | ⏸️ **暂停任务** |
| **其他普通种子** | ▶️ 恢复运行 | 🗑️ **直接删除** (释放空间) |
| **全局限速** | 🔓 无限制 | 🔒 全局限速生效 |

<h2 id="deploy">🚀 部署指南</h2>

推荐使用 Docker Compose 进行一键部署。

### 1. 准备工作
确保服务器已安装 Docker 和 Docker Compose。

### 2. 配置文件
创建目录并编写 `docker-compose.yml`：

~~~bash
mkdir -p /root/netcup-monitor/data
cd /root/netcup-monitor
nano docker-compose.yml
~~~

填入以下内容：

~~~yaml
version: '3.8'

services:
  netcup-monitor:
    image: ghcr.io/agonie0v0/netcup-monitor:latest
    container_name: netcup-monitor
    restart: unless-stopped
    # 端口映射 (宿主机端口:容器端口)
    ports:
      - "5000:5000"
    volumes:
      # 数据与配置文件持久化
      - ./data:/app/data
      # 宿主机时间同步
      - /etc/localtime:/etc/localtime:ro
      # (可选) 若需自动重启 Vertex 容器，必须挂载此 Socket
      - /var/run/docker.sock:/var/run/docker.sock
    environment:
      - TZ=Asia/Shanghai
~~~

### 3. 启动服务

~~~bash
docker compose up -d
~~~
访问面板：`http://你的IP:5000`

### 4. 容器更新

~~~bash
# 步骤 1: 强制拉取最新的镜像版本

cd /root/netcup-monitor

docker compose pull

# 如果拉取失败: 先注销 ghcr.io

docker logout ghcr.io

# 重新拉取

docker compose pull

# 步骤 2: 停止旧容器，使用最新镜像重新创建并启动
docker compose up -d --force-recreate
~~~

<h2 id="config">⚙️ 配置详解</h2>

首次访问点击右上角 **“登录”**（默认admin），进入设置页面。


### 1. 基础连接
* **qBittorrent 设置**：填写 QB 的地址、账号、密码。
* **Vertex 设置**：
    * **API 地址**：Vertex 的访问地址。
    * **容器名**：如果 Vertex 和本服务在同一台机器，填写 Vertex 的容器名（如 `vertex`）可实现自动重启。
    * **监控 RSS ID**：在 Vertex 规则列表中找到需要动态管理的规则 ID（如 `4c145005`），多个 ID 用逗号分隔。

### 2. Netcup SCP 账号 (核心)
在“SCP 账号”标签页添加账号。
* **Customer ID**：Netcup 客户号（数字）。
* **Password**：登录 SCP 面板的密码。

> 🔒 **安全提示**：密码仅保存在本地 `data/config.json` 中，通过 Docker 隔离，不会上传第三方。

### 3. 策略管理
* **保留分类 (Keep Categories)**：填写分类名，如 `Keep, FRD`。这些分类下的种子在限速时只会暂停，不会被删除。
* **HR 保护分类**：填写如 `HR, VIP`。
* **限速上传限制**：设置限速期间 HR 种子的最大上传速度（KB/s），防止长时间无流量被站点判定为不再做种。

### 4. 实例配置 (Servers)
添加你需要监控的 VPS 实例：
* **名称**：自定义别名。
* **IP 地址**：必须与 Netcup 后台显示的 IP 一致（用于匹配 API 数据）。
* **Client ID**：对应 Vertex 中下载器的ID（用于更新 RSS 规则）。
* **不托管 (Unmanaged)**：开启后，脚本只监控流量和状态，**不执行**任何删除、暂停操作。

## 📸 界面预览
### 仪表盘
<img width="1545" height="1271" alt="PixPin_2025-12-26_20-46-59" src="https://github.com/user-attachments/assets/bf7658c7-9805-4866-b962-9d177f6e50e4" />

### 流量统计
<img width="1534" height="1253" alt="PixPin_2025-12-26_20-46-34" src="https://github.com/user-attachments/assets/ebb135bf-4af8-4932-ae21-6101ecf23bb0" />


<h2 id="disclaimer">⚠️ 免责声明</h2>

1.  **数据安全**：本项目涉及对 qBittorrent 种子的 **删除** 操作。请务必正确配置 “保留分类” 及 “不托管” 选项。
2.  **账号安全**：Netcup 账号密码存储在你的服务器本地，请确保服务器安全。
3.  **责任界定**：作者不对因使用该项目导致的数据丢失或账号问题负责。
4.  **项目更新**：懒得更新，如果需要可以自己fork仓库修改。

---

<div align="center">
    Project maintained by <a href="https://github.com/agonie0v0">懒羊羊
</div>
