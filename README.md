# PiManager

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.10%2B-green.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#)

跨平台 GUI，用于配置、切换、测试和启动官方 [Pi Coding Agent](https://github.com/badlogic/pi-mono)（`@earendil-works/pi-coding-agent`）。

> 完整 agent 能力始终由官方 `pi` 提供；PiManager 负责配置管理、密钥安全、批量测试与一键启动。

---

## 功能概览

- 自定义 Provider / 模型配置、从 API 拉取模型、批量可用性测试
- Provider API Key 安全存储：OS keyring 优先，当前用户 AES-GCM 文件库回退
- 启动官方 Pi 时仅向子进程注入密钥（`models.json` 只存环境变量引用）
- 全局代理、健康监控、测试历史、并发测试
- 配置 ZIP 导入导出（密钥可选 PBKDF2 + AES-256-GCM 加密）
- 会话过滤、重命名、删除与继续
- 系统托盘快速切换默认模型
- Cursor / VS Code 扩展：通过 helper 安全注入运行时环境
- Windows / macOS / Linux 终端启动支持

## 截图 / 品牌

| 资源 | 说明 |
|------|------|
| `assets/logo.svg` | 矢量 Logo |
| `assets/icon.png` | 应用图标 |
| `assets/logo-wordmark-dark.png` | 深色字标 |

## 快速开始

### 方式一：下载发布包（对应系统独立运行）

从 [Releases](https://github.com/suimi8/PiManager/releases) 下载**与本机系统匹配**的包，解压后即可运行（无需安装 Python）：

| 平台 | 附件示例 | 如何运行 |
|------|----------|----------|
| Windows x64 | `...-windows-x64-dir.zip`（推荐） | 解压后运行 `PiManager\PiManager.exe` |
| macOS arm64 | `...-macos-arm64.zip` | 打开 `PiManager.app` |
| Linux x64 | `...-linux-x64.tar.gz` | `./PiManager/PiManager` |
| Cursor | `pi-manager-pi-cursor-*.vsix` | 在 Cursor 安装 VSIX |

请保持解压目录完整（Windows/Linux 的 `_internal`、macOS 的 `.app` 不要拆散）。

完整 Pi 会话仍需官方 CLI：

```bash
npm install -g @earendil-works/pi-coding-agent
```

可选自检：

```bash
# Windows
PiManager\PiManager.exe --self-check
# macOS
PiManager.app/Contents/MacOS/PiManager --self-check
# Linux
./PiManager/PiManager --self-check
```

macOS 若提示无法打开未签名应用：右键打开，或到系统设置 → 隐私与安全性 → 仍要打开。

### 方式二：从源码运行

**依赖**

- Python 3.10+
- Node.js + 官方 Pi CLI（`npm install -g @earendil-works/pi-coding-agent`）

```bash
git clone https://github.com/suimi8/PiManager.git
cd PiManager
python -m pip install -r requirements.txt
python main.py
```

### 运行测试

```bash
python -m pip install pytest
python -m pytest tests -q
```

## Cursor 扩展

扩展源码位于 [`extensions/pi-cursor`](extensions/pi-cursor)。

1. 打包：在扩展目录用 `vsce package`（或使用 Release 中的 `.vsix`）
2. 在 Cursor 中安装 VSIX
3. 命令面板搜索 `Pi:` 即可启动会话

若扩展与 PiManager 不在相邻目录，请在设置中配置：

```text
pi.providerEnvCommand = python /path/to/PiManager/main.py --print-provider-env
```

打包版可写为：`/path/to/PiManager.exe --print-provider-env`。

## 打包

详见 [BUILD.md](BUILD.md)。本地（当前操作系统）：

```bash
python -m pip install -r requirements.txt pyinstaller
python -m PyInstaller --noconfirm --clean PiManager.spec
python scripts/smoke_test_dist.py
python scripts/package_release.py --version 1.6.4
```

跨平台（Windows / macOS / Linux）推荐用 GitHub Actions：
Actions → **Build** → **Run workflow**。CI 会在三端分别构建并做 `--self-check` 冒烟测试，再上传 Release。

二进制产物请通过 GitHub Releases 分发，不纳入本仓库源码树。

## 配置目录

| 平台 | 路径 |
|------|------|
| Windows | `%USERPROFILE%\.pi\agent\` |
| macOS / Linux | `~/.pi/agent/` |

主要文件：

- `settings.json` / `models.json` / `pi-manager.json`
- `secrets.vault`（仅在 OS keyring 不可用时使用）
- `pi-manager-test-history.json` / `pi-manager-health.json`

真实 API Key **不会**明文写入 `models.json`。配置中仅保存官方 Pi 支持的引用，例如：

```text
${PI_MANAGER_PROVIDER_<SLUG>_<HASH>_API_KEY}
```

PiManager 启动官方 Pi 时从安全存储读取并注入子进程环境。

## 文档

- [使用教程与 FAQ](docs/使用教程.md)
- [构建说明](BUILD.md)
- [Cursor 扩展说明](extensions/pi-cursor/README.md)

## 安全说明

- 请勿将含真实 API Key 的配置、`secrets.vault`、导出 ZIP 提交到 Git
- 导出含密钥的配置包时务必设置强密码
- 发现安全问题请优先私下联系维护者，避免在公开 Issue 中粘贴密钥

## 许可证

本项目采用 [Apache License 2.0](LICENSE)。

```
Copyright 2026 suimi8
```

PiManager 是独立的第三方管理工具，与官方 Pi Coding Agent 无隶属关系，除非另有说明。

## 致谢

- [Pi Coding Agent](https://www.npmjs.com/package/@earendil-works/pi-coding-agent) — 官方 agent 运行时
- [PySide6 / Qt](https://doc.qt.io/qtforpython/) — GUI
- [keyring](https://github.com/jaraco/keyring) / [cryptography](https://github.com/pyca/cryptography) — 密钥存储与加密
