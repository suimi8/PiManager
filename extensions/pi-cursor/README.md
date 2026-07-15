# Pi for Cursor

在 Cursor 中启动官方 Pi Coding Agent 终端会话。

## 命令

- `Pi: 用默认模型启动`（快捷键 Ctrl+Alt+P）
- `Pi: 在终端启动完整会话`
- `Pi: 快速提问 (pi -p)`
- `Pi: 打开配置目录`
- `Pi: 检查版本`

## 依赖

需已安装：`npm install -g @earendil-works/pi-coding-agent`

## Pi Manager 安全密钥

扩展会在相邻的 `pi-manager` 源码目录中自动发现环境 helper，并把所选 Provider 的密钥仅注入新终端环境。若扩展与 Pi Manager 不在相邻目录，请设置：

```text
pi.providerEnvCommand = python C:\path\to\pi-manager\main.py --print-provider-env
```

打包版可配置为：`C:\path\to\PiManager.exe --print-provider-env`。

使用 Pi Manager 安全密钥但 helper 不可用时，扩展会停止启动并显示错误，避免以无效占位符请求接口而返回 401。
