# PiManager

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-green.svg)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)](#)

跨平台 GUI，用于配置、切换、测试和启动官方 [Pi Coding Agent](https://github.com/badlogic/pi-mono)（`@earendil-works/pi-coding-agent`）。

> 完整 agent 能力始终由官方 `pi` 提供；PiManager 负责配置管理、密钥安全、批量测试与一键启动。

---

## 功能概览

- 全新现代化 UI：分组可折叠导航、全局昼夜主题、统一卡片/表格/对话框与本地 SVG 图标；Pi CLI 自动同步浅色/深色模式
- 自定义 Provider / 模型配置、从 API 拉取模型、批量可用性测试
- Provider 支持多 API Key：鉴权、限流或额度错误时先在同一接口内热切 Key，失败 Key 暂存失效池并可手动恢复
- 快速提问（桌面 Chat 页与 Cursor 扩展）默认走常驻 `pi --mode rpc` 会话：多轮上下文保留在会话内，failover 换模用 `set_model` 会话内热切不丢上下文；`pi --mode rpc` 不可用时自动回退一次性模式
- Provider API Key 安全存储：OS keyring 优先，当前用户 AES-GCM 文件库回退
- 启动官方 Pi 时仅向子进程注入密钥（`models.json` 只存环境变量引用）
- 全局代理、健康监控、测试历史、并发测试
- 配置 ZIP 导入导出（密钥可选 PBKDF2 + AES-256-GCM 加密）
- 会话过滤、重命名、删除与继续
- 系统托盘快速切换默认模型
- Cursor / VS Code 扩展：通过 helper 安全注入运行时环境
- Windows / macOS / Linux 终端启动支持
- Pi 更新器按 Node.js 版本自动选择 `latest` / `legacy-node20` 兼容通道，并在安装后验证实际 `pi -v` 运行结果

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
| Windows x64 | `...-windows-x64-onefile.zip`（便携版） | 解压后直接运行 `PiManager.exe`（单文件自包含，可单独拷贝） |
| macOS arm64 | `...-macos-arm64.zip` | 打开 `PiManager.app` |
| Linux x64 | `...-linux-x64.tar.gz` | `./PiManager/PiManager` |
| Cursor | `pi-manager-pi-cursor-*.vsix` | 在 Cursor 安装 VSIX |

Windows 为便携单文件版：解压得到单个 `PiManager.exe`，可放到任意目录 / U 盘使用；
macOS / Linux 为目录版，请保持解压目录完整（`_internal`、`.app` 不要拆散）。

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

- Python 3.11+（CI 测试矩阵 3.11 / 3.12；权威声明见 `pyproject.toml` 的 `requires-python`）
- Node.js + 官方 Pi CLI（`npm install -g @earendil-works/pi-coding-agent`）

```bash
git clone https://github.com/suimi8/PiManager.git
cd PiManager
python -m pip install -r requirements.txt
python main.py
```

### 运行测试

```bash
python -m pip install -r requirements-dev.txt
python -m pytest tests -q
```

提交前的完整门禁见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## Cursor 扩展

扩展源码位于 [`extensions/pi-cursor`](extensions/pi-cursor)。

`pi.askPrompt` 会先尝试同一 Provider 的其他可用 Key；只有 Key 池耗尽后，才按桌面端相同的失败阈值和候选顺序切换 Provider / 模型。扩展会独立检查 PiManager Release 中的新版 VSIX。

1. 打包：在项目根目录运行 `python scripts/package_extension.py`，产物固定写入 `release-assets/pi-manager-pi-cursor-<版本>.vsix`（或使用 Release 中的 `.vsix`）
2. 在 Cursor 中安装 VSIX
3. 命令面板搜索 `Pi:` 即可启动会话

正常情况下先启动一次 PiManager，扩展会自动发现安全 helper 与 Config Broker。若自动发现不可用，可在设置中手工配置：

```text
pi.providerEnvCommand = python /path/to/PiManager/main.py --print-provider-env
```

打包版可写为：`/path/to/PiManager.exe --print-provider-env`。

> **扩展与桌面端必须同版本升级**：`--print-provider-env` 现在要求调用方按值出示
> `~/.pi/agent/.broker-token`（`--token <值>` / `--token-file <文件>`），与
> `--config-mutate` 共用同一套授权模型。扩展 0.7.2+ 会自己读取并附加 token，
> 上面的 `pi.providerEnvCommand` 写法**不需要改**；但**旧版 VSIX 不带 token，
> 升级桌面端后会取不到密钥**，请同步安装 Release 中配套的 `.vsix`。

## 打包

详见 [BUILD.md](BUILD.md)。本地（当前操作系统）：

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
# Windows（单文件版）:
python -m PyInstaller --noconfirm --clean PiManagerOneFile.spec
# macOS / Linux（目录版 / .app）:
python -m PyInstaller --noconfirm --clean PiManager.spec
python scripts/smoke_test_dist.py
python scripts/package_release.py --version 1.8.7
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

- `settings.json` / `models.json` / `pi-manager.json` / `auth.json`
- `secrets.vault`（仅在 OS keyring 不可用时使用）；`secrets.index.json` 与 `.vault_master_key` 是它的索引与盐，**与密钥同等敏感，不要拷贝或提交**
- `pi-plugins.json`（插件注册表）/ `pimanager/plugins/<id>/<version>/`（已安装插件副本）/ `skills/`、`extensions/`（内置插件落盘）
- `mcp-servers.json`（MCP 桥）/ `themes/`（CLI 主题）
- `pi-manager-test-history.json` / `pi-manager-health.json` / `pi-manager-helper.json`
- `.broker-token` / `.config-revisions.json`（配置 Broker 凭据与修订记录，**凭据等价物**）

完整清单与「哪些该备份、哪些不该拷」见 [使用教程 → 路径速查](docs/使用教程.md#五路径速查)。迁移请用「工具 → 导出配置包」，不要手工拷贝目录。

真实 API Key **不会**明文写入 `models.json`。配置中仅保存官方 Pi 支持的引用，例如：

```text
${PI_MANAGER_PROVIDER_<SLUG>_<HASH>_API_KEY}
```

PiManager 启动官方 Pi 时从安全存储读取并注入子进程环境。

同一 Provider 可在 **Provider 管理 → API Keys** 中维护多把密钥。遇到 HTTP 401/403/429 或明确的鉴权、限流、额度错误时，当前 Key 会被暂时移入失效池并自动尝试下一把；网络错误和 HTTP 5xx 不会停用 Key。失效 Key 可在同一窗口恢复，整个过程不会把真实 Key 写入 `models.json`。

## 文档

- [使用教程与 FAQ](docs/使用教程.md)（由 `pi_manager/help_docs.py` 生成，与应用内帮助页同源）
- [发布说明 / 变更记录](docs/发布说明.md)
- [构建说明](BUILD.md)
- [开发规范（唯一权威正文）](docs/DEVELOPMENT_STANDARDS.md) · [贡献指南](CONTRIBUTING.md)
- [安全策略与威胁模型](SECURITY.md) · [插件格式](docs/PLUGIN_FORMAT.md)
- [Cursor 扩展说明](extensions/pi-cursor/README.md)

## 安全说明

- 请勿将含真实 API Key 的配置、`secrets.vault`、导出 ZIP 提交到 Git
- 回退文件库 `secrets.vault`（OS keyring 不可用时使用）的机密性依赖文件权限（仅当前用户可读/写）与随机盐（PBKDF2-HMAC-SHA256 派生 AES 密钥）；Windows 上另有 DPAPI 保护，密钥绑定当前 Windows 用户
- 导出含密钥的配置包时务必设置强密码
- 内置 MCP 桥扩展 spawn 第三方 MCP server 时仅透传白名单基础环境（`PATH`/`HOME`/`TEMP` 等），provider API Key 不会被继承；如需给某个 MCP server 传密钥，必须在 `~/.pi/agent/mcp-servers.json` 该 server 的 `env` 中显式写出
- 冻结产物**只信任内嵌资源**（`sys._MEIPASS`），便携单文件版不再读取 exe 同级目录的 `assets/`——否则在 exe 旁投放 `assets/builtin/manifest.json` 即可让应用把攻击者的内置扩展（拥有当前用户完整权限）落盘到 `~/.pi/agent/extensions/`
- Provider / Model / Thinking 名称有字符白名单（Provider：字母数字与 `. _ - : @ +`；Model 再加 `/`；Thinking：字母数字与 `_ -`）；含 `"` `&` `|` `%` 等字符的名称会拒绝启动 Pi（这些字符此前可经 Windows `cmd.exe` shim 注入任意命令），校验全平台生效
- Windows 上 `.broker-token` / `secrets.vault` / `.vault_master_key` / `secrets.index.json` / `pi-manager-helper.json` 会真正收紧 DACL 到仅当前用户
- 导入配置包默认**不覆盖 `AGENTS.md`**，且拒绝含可执行语义键（`hook` / `mcpServer` / `command` / `shell` / `exec` / `env` / `permissions` 等）的 `settings.json`——覆盖 `AGENTS.md` 等于让下一次运行的 Pi 遵循配置包作者的指令（Pi 有 shell 权限）
- 升级到本版本的行为变更（旧 VSIX 取不到密钥、旧格式 vault 需重填或一次性迁移）见 [发布说明 → 破坏性变更](docs/发布说明.md) 与 [使用教程 FAQ 第 G 组](docs/使用教程.md)
- 发现安全问题请走 GitHub Security Advisory 私密报告（仓库 **Security → Report a vulnerability**），流程见 [SECURITY.md](SECURITY.md)；避免在公开 Issue 中粘贴密钥

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
- [LinuxDo](https://linux.do/) — LinuxDo社区
