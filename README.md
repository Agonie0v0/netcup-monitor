<div align="center">

<img src="https://socialify.git.ci/agonie0v0/netcup-monitor/image?description=1&font=Inter&language=1&name=1&owner=1&pattern=Circuit%20Board&theme=Auto" alt="Netcup Monitor Pro" width="640" height="320" />

# 📊 Netcup Monitor Pro

**专为 Netcup RS/VPS 打造的智能化流量监控与自动化流控面板**

[![Docker Image](https://img.shields.io/badge/Docker-ghcr.io%2Fagonie0v0%2Fnetcup--monitor-blue?logo=docker)](https://github.com/agonie0v0/netcup-monitor/pkgs/container/netcup-monitor)
[![Python](https://img.shields.io/badge/Python-3.9+-yellow?logo=python)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green)](LICENSE)

[功能特性](#-功能特性) • [工作原理](#-自动化策略逻辑) • [部署指南](#-部署指南) • [配置详解](#-配置详解) • [常见问题](#-常见问题)

</div>

---

## 📖 项目简介

**Netcup Monitor Pro** 解决了 Netcup 用户最大的痛点：**限速（Throttling）后的自动化处理**。

不同于普通的监控脚本，本项目通过对接 Netcup 官方 SOAP API 精准识别服务器状态。当检测到限速时，它能自动指挥 **qBittorrent** 和 **Vertex** 进行精细化的流量规避，并在限速解除后自动恢复生产，实现真正的“无人值守”。

## ✨ 功能特性

### 🛡️ 智能流控策略
- **HR 智能保护**：限速期间，对 **下载中** 的 HR 种子自动暂停，对 **做种中** 的 HR 种子限制上传速度（保种模式），防止因删种导致 HR 考核失败。
- **分类管理**：
  - **保留分类 (Keep)**：限速时仅暂停任务，保留文件。
  - **自动清理**：非保留、非 HR 分类的种子，在限速时自动删除以释放空间。
- **自动恢复**：检测到限速解除（恢复高速）后，自动恢复所有暂停任务并解除速度限制。

### 🔗 生态联动
- **Vertex 深度集成**：
  - **智能选机**：根据服务器限速状态，动态更新 Vertex 的 RSS 下载规则，确保任务只分发给未限速的机器。
  - **容器自愈**：规则更新后支持自动重启 Vertex 容器（需挂载 Docker Socket）。
- **qBittorrent 全管**：支持多实例管理，直接通过 API 控制种子状态。

### 📊 可视化面板
- **流量趋势图**：内置 7 日流量消耗折线图与健康度分析。
- **实时监控**：查看当前上传/下载速度、今日流量、本月流量。
- **健康报告**：记录每日限速时长、日均限速分析。
- **Web配置**：所有参数（账号、策略、通知）均可在网页端热修改，无需重启容器。

### 🔔 多渠道通知
- **Telegram Bot**：支持富文本状态简报。
- **企业微信**：支持 Webhook 机器人及企业微信应用（App）推送。

---

## 🧠 自动化策略逻辑

系统每 5 分钟（默认）检测一次状态，根据 Netcup API 返回的 `trafficThrottled` 状态执行以下逻辑：

| 种子类型/分类 | 🟢 正常状态 (高速) | 🔴 限速状态 (低速) |
| :--- | :--- | :--- |
| **HR (做种中)** | 🚀 全速上传 | 🛡️ **限速上传** (默认 10KB/s) |
| **HR (下载中)** | 🚀 全速下载 | ⏸️ **暂停下载** |
| **保留分类 (Keep)** | ▶️ 恢复运行 | ⏸️ **暂停任务** |
| **其他普通种子** | ▶️ 恢复运行 | 🗑️ **直接删除** (释放空间) |
| **全局限速** | 🔓 无限制 | 🔒 全局限速生效 |

---

## 🚀 部署指南

推荐使用 Docker Compose 进行一键部署。

### 1. 准备工作
确保服务器已安装 Docker 和 Docker Compose。

### 2. 配置文件
创建目录并编写 `docker-compose.yml`：

```bash
mkdir -p /root/netcup-monitor/data
cd /root/netcup-monitor
nano docker-compose.yml
