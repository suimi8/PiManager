---
name: geonode-ip-rotator
description: "GeoNode 住宅代理出口 IP 自动切换。当 API 调用返回 HTTP 402/429 额度耗尽/限流时，自动切换代理端口（9000-9010）更换出口 IP，仅影响 pi 的 API 调用，不修改系统代理。"
---

# GeoNode 出口 IP 自动切换技能

## 核心原则（必须遵守）

> **本技能只修改 Pi Manager 的 `proxy_url` 配置，进而影响 pi 子进程的 API 出站流量，绝不修改 Windows 系统代理设置。**

Pi Manager 的代理机制：
1. `proxy_url` 存储在 `~/.pi/agent/pi-manager.json`
2. 启动 pi 子进程时，Pi Manager 将 `proxy_url` 注入 `HTTP_PROXY`/`HTTPS_PROXY` 环境变量
3. 仅 pi 进程的 API 请求走代理，系统其他流量不受影响

## 概述

当 pi 调用 LLM API 返回 HTTP 402/429 等额度耗尽/限流错误时，自动执行：
1. 旋转 GeoNode 代理端口（9000→9001→...→9010→9000）
2. 更新 Pi Manager 配置中的 `proxy_url`
3. 下次 pi 请求自动使用新出口 IP

## 前置条件

1. 已购买 GeoNode 住宅代理服务（https://geonode.com）
2. 已获取代理凭据（hostname:port:username:password）
3. 首次使用前设置代理密码（见下方配置说明）

## 首次配置（必读）

> **⚠️ 注意**：脚本内置密码为占位符，必须设置真实凭据才能使用。

### 方式一：环境变量（推荐，仅当前会话有效）

```bash
# 在启动 OpenCode / Pi Manager 前设置
set GEONODE_PROXY_PASSWORD=你的真实密码
set GEONODE_PROXY_HOST=你的GeoNode代理主机
set GEONODE_PORT_START=9000
set GEONODE_PORT_END=9010
set GEONODE_PROXY_USERNAME=geonode_你的账号ID-type-residential
```

### 方式二：修改脚本默认值

编辑 `scripts/geonode_ip_rotator.py`，找到 `DEFAULT_PROXY_PASSWORD` 替换为真实密码。

### 方式三：通过 Pi Manager 设置

1. 打开 Pi Manager → 设置 → 全局代理
2. 填入 GeoNode 代理 URL：`http://geonode_你的账号ID-type-residential:密码@你的GeoNode代理主机:9000`
3. 保存后，pi 的 API 请求将自动走代理

## 核心规则

### 1. 额度耗尽检测

遇到以下情况**必须**执行 IP 切换：
- HTTP **402**（Payment Required / 额度耗尽）
- HTTP **429**（Too Many Requests / 限流）
- 响应体含配额相关关键词：`quota exhausted`、`insufficient quota`、`rate limit`、`额度不足`、`余额不足` 等

### 2. 自动切换流程

```bash
# 检测到额度耗尽，执行：
python geonode_ip_rotator.py rotate
# 输出示例：
# ✅ 已旋转到端口 9001，新出口 IP: xxx.xxx.xxx.xxx（France）
#    已更新 Pi Manager 配置，下次 pi 请求将使用新 IP

# 验证状态：
python geonode_ip_rotator.py status
```

### 3. 配置 GeoNode 代理

```bash
python geonode_ip_rotator.py configure
# 将设置 Pi Manager 的 proxy_url 为 GeoNode 代理
# 仅影响 pi 的 API 调用，不修改系统代理
```

### 4. 恢复直连

```bash
python geonode_ip_rotator.py remove
# 移除代理配置，pi 恢复直连
```

## 端口切换原理

GeoNode 旋转代理使用端口范围 9000-9010，每个端口对应不同出口 IP：

| 端口 | 出口 IP |
|------|---------|
| 9000 | 当前 IP（法国住宅） |
| 9001-9010 | 备用 IP |

每次 `rotate` 操作将端口递增，循环使用。

## 架构原理

```
┌─────────────────────────────────────────────────┐
│                   系统全局                        │
│  系统代理设置（未修改） ← 本技能不动这里          │
└─────────────────────────────────────────────────┘
                        │
┌─────────────────────────────────────────────────┐
│               OpenCode IDE                       │
│  ┌───────────────────────────────────────────┐   │
│  │  Pi Agent (pi 进程)                       │   │
│  │  HTTP_PROXY=http://user:pass@host:port    │   │
│  │  API 调用 → GeoNode 代理 → LLM Provider  │   │
│  └───────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
                        │
              Pi Manager 配置 (proxy_url)
              ~/.pi/agent/pi-manager.json
```

## MCP 模式（最推荐 — AI 可直接调用工具）

通过 MCP（Model Context Protocol），AI 可以直接调用工具来切换出口 IP，无需手动执行脚本。

### 前提条件

1. Pi Manager 的 **MCP 桥扩展** 已启用：
   ```bash
   cd ~/.pi/agent/extensions/pi-manager-mcp-bridge
   npm install --omit=dev
   ```
   （Pi Manager 中启用：设置 → 扩展 → pi-manager-mcp-bridge）

2. 在 `~/.pi/agent/mcp-servers.json` 中注册 GeoNode MCP 服务器：
   ```json
   {
     "servers": {
       "geonode": {
         "command": "python",
         "args": ["<代理密码绝对路径>/geonode_mcp_server.py"],
         "env": {
           "GEONODE_PROXY_PASSWORD": "你的真实密码"
         }
       }
     }
   }
   ```
   将 `<代理密码绝对路径>` 替换为实际路径（`~/.pi/agent/skills/geonode-ip-rotator/scripts/geonode_mcp_server.py`），密码替换为真实值。

3. 重启 Pi 会话使配置生效（或执行 `/mcp-reload`）。

### 暴露的工具

注册后，AI 可以直接调用以下 MCP 工具：

| 工具名 | 功能 | 触发场景 |
|--------|------|----------|
| `geonode_configure` | 配置 GeoNode 代理到 Pi Manager | 首次设置 |
| `geonode_rotate` | 切换到下一个端口，获得新出口 IP | HTTP 402/429 额度耗尽 |
| `geonode_status` | 查看当前代理状态和出口 IP | 巡检/诊断 |
| `geonode_auto_fix` | 自动检测额度耗尽并切换+重试 | 一键修复 |
| `geonode_remove` | 移除代理，恢复直连 | 不再需要代理 |

### AI 自动调用示例

当 AI 遇到 API 调用返回 HTTP 429 时，自动执行：
```
→ 调用 geonode_rotate()
  ← 已切换到端口 9001，新出口 IP: xxx.xxx.xxx.xxx
→ 重试原 API 请求
  ← 成功！
```

### 三种模式对比

| 特性 | Skill（被动描述） | 热切换代理（自动守护） | **MCP（AI 主动调用）** |
|------|------------------|---------------------|----------------------|
| 切换方式 | AI 读文档后手动 | **全自动守护进程** | **AI 直接调用工具** |
| 检测错误 | AI 判断 | 代理自动检测 | **AI 判断 + 调用工具** |
| 重试请求 | 需手动重试 | **自动重试** | AI 可自动重试 |
| 安装复杂度 | 最低 | 中 | 中（需启用 MCP 桥） |
| 推荐场景 | 快速上手 | 长期稳定运行 | **最灵活，AI 自主决策** |

## 自动热切换模式（推荐）

### 一键启动热切换代理服务器

```bash
# 设置密码（必须）
set GEONODE_PROXY_PASSWORD=你的真实密码

# 启动热切换代理服务器（前台运行）
python geonode_proxy_server.py start

# 或后台运行
python geonode_proxy_server.py start --daemon

# 查看状态
python geonode_proxy_server.py status

# 停止
python geonode_proxy_server.py stop
```

启动后，代理服务器会自动完成以下操作：

| 步骤 | 说明 |
|------|------|
| 1. 更新 Pi Manager 配置 | 自动将 `proxy_url` 设为 `http://127.0.0.1:9876` |
| 2. 透明转发 | 所有 pi 的 API 请求经本地代理 → GeoNode → LLM API |
| 3. 自动检测 402/429 | 监控响应状态码，发现额度耗尽立即触发旋转 |
| 4. 自动旋转端口 | 9000→9001→...→9010→9000，获得新出口 IP |
| 5. 自动重试 | 旋转后自动重试失败的请求，pi 无感知 |

### 架构

```
Pi Agent ──→ 本地代理 (:9876) ──→ GeoNode (:9000-9010) ──→ LLM API
                │                       ↑
                │  自动检测 402/429 ─────┘
                │  自动旋转端口 + 重试
                │
                ↓ 更新 Pi Manager 配置
           ~/.pi/agent/pi-manager.json
```

### 与普通模式对比

| 特性 | 普通模式 (rotate) | 热切换模式 (proxy_server) |
|------|-------------------|--------------------------|
| 切换方式 | 手动/脚本 | **自动** |
| 检测错误 | AI 识别 | **代理自动检测** |
| 重试请求 | 需手动重试 | **自动重试** |
| Pi 进程 | 需重启感知新代理 | **无感，不丢上下文** |
| 后台运行 | 否 | **支持 `--daemon`** |
| 使用场景 | 一次性切换 | **长期运行** |

## 故障处理

| 症状 | 处理方式 |
|------|----------|
| `代理密码未设置` | 设置 `GEONODE_PROXY_PASSWORD` 环境变量 |
| HTTP 407 | 代理凭据错误，检查用户名/密码 |
| 所有端口不可用 | 检查网络连接，或到 GeoNode 仪表盘验证 |
| 代理超时 | 等待 5 秒后重试，仍失败则 `rotate` |
| `pi-manager.json` 写入失败 | 检查文件权限 |

## 安全说明

- 代理凭据仅存储在环境变量或 `pi-manager.json` 中
- 不写入 Git、日志或系统代理设置
- 本技能不收集任何数据，所有操作仅修改本地配置