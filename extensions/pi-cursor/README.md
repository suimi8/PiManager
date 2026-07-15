# Pi Coding Agent（Cursor / VS Code 扩展）

在 Cursor 中启动官方 Pi，并**热切换** `~/.pi/agent/settings.json` 中的默认 Provider / 模型。

## 功能

- 侧栏 **Pi Manager** Webview：Provider / Model 下拉、收藏一键设默认、启动 Pi
- 命令面板 / 快捷键切换默认模型（写 settings，下次启动生效）
- 状态栏显示当前默认模型，点击即可切换
- 终端启动完整 Pi 会话；快速提问 `pi -p`
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

## 安装

从 [PiManager Releases](https://github.com/suimi8/PiManager/releases) 下载 `pi-manager-pi-cursor-*.vsix`，在 Cursor 中 “Install from VSIX”。
