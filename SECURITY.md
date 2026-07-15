# 安全策略

## 支持的版本

当前主线（源码 `main` 与最新 GitHub Release）为优先修复目标。

## 报告漏洞

请勿在公开 Issue 中粘贴真实 API Key、导出包密码或个人配置。

建议通过 GitHub Security Advisory（若已启用）或仓库 Issues 中仅描述问题类型与复现步骤（脱敏）联系维护者：

- 仓库：https://github.com/suimi8/PiManager

## 密钥相关说明

- 真实 Provider Key 应存放在 OS keyring 或本地加密库，而非 Git 仓库
- `models.json` 中的 `${PI_MANAGER_PROVIDER_..._API_KEY}` 仅为引用
- 配置 ZIP 若包含密钥，使用 PBKDF2-HMAC-SHA256 + AES-256-GCM；请使用强密码并妥善保管
