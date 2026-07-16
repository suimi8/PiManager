# Pi Coding Agent（Cursor / VS Code 扩展）

在 Cursor 中启动官方 Pi，并**热切换** `~/.pi/agent/settings.json` 中的默认 Provider / 模型。

## 功能

- 侧栏 **Pi Manager** Webview：Provider / Model 下拉、收藏一键设默认、启动 Pi
- 命令面板 / 快捷键切换默认模型（写 settings，下次启动生效）
- 状态栏显示当前默认模型，点击即可切换
- 终端启动完整 Pi 会话；快速提问先轮换同 Provider 的可用 Key，再复用 Pi Manager 的失败计数与自动换模
- 启动后每天检查 PiManager Release 中的 VSIX 更新，也可从命令面板手动检查
- 支持 Pi Manager 安全密钥 helper（`pi.providerEnvCommand`）

## 快捷键

| 快捷键 | 作用 |
|--------|------|
| `Ctrl+Alt+P` / `Cmd+Alt+P` | 用默认模型启动 Pi |
| `Ctrl+Alt+M` / `Cmd+Alt+M` | 切换默认 Provider/模型 |
| `Ctrl+Alt+Shift+P` / `Cmd+Alt+Shift+P` | 打开侧栏管理面板 |

## 热切换说明

- **能做的**：立即改写 `settings.json` 的 `defaultProvider` / `defaultModel`（可选同步 `enabledModels`）
- **不能做的**：无法注入已在运行的 Pi 交互会话内部状态；已开会话需在 Pi 内 `Ctrl+P` 循环，或重新启动会话
- 模型列表来源：`pi-manager.json` 收藏、`settings.enabledModels`、`models.json` providers
- 快速提问：单模型失败次数会写入 `pi-manager.json`，达到桌面端相同阈值后按当前模型 → 收藏 → 启用模型 → 默认模型重试；成功后同步新的默认模型

## 多 API Key

- Key 池由桌面端 **Provider 管理 → API Keys** 维护，真实 Key 仍保存在 PiManager 安全存储中
- `pi.askPrompt` 遇到 HTTP 401/403/429 或明确的鉴权、限流、额度错误时，会通过安全 helper 标记当前 Key 并立即尝试同 Provider 的下一把 Key
- 只有同 Provider 的可用 Key 全部失败后，错误才进入模型失败计数与换模流程
- HTTP 5xx、超时、DNS 和网络中断不会停用 Key；失效 Key 需在桌面端手动恢复

## 安装

从 [PiManager Releases](https://github.com/suimi8/PiManager/releases) 下载 `pi-manager-pi-cursor-*.vsix`，在 Cursor 中 “Install from VSIX”。扩展也会在 Release 有新版 VSIX 时提示更新。

源码打包请在 PiManager 项目根目录运行：

```bash
python scripts/package_extension.py
```

产物固定写入项目根目录的 `release-assets/`。
