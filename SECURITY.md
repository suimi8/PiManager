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

## 回退 vault 的威胁模型（如实说明）

当 OS keyring 不可用时，Pi Manager 回退到当前用户 AES-GCM 文件库
`~/.pi/agent/secrets.vault`。该回退路径的机密性受以下限制：

- 文件库的“主密钥”并非独立的随机密钥：它由随二进制分发的固定 pepper 与
  每次安装生成的随机盐经 PBKDF2-HMAC-SHA256（600,000 次迭代）派生。
  知道 pepper（任何取得二进制的人都知道）后，剩余保护依赖盐的随机性与
  文件权限。
- 因此该回退 vault 的机密性实际依赖：1) `secrets.vault` 与盐文件强制 0600
  权限；2) 盐文件与 vault 不可被同一本地攻击者同时读取；3) 离线暴力破解
  PBKDF2 的算力成本。仅复制 vault 文件不足以解密（缺少盐），但在同一
  用户上下文（可读盐文件）下的攻击者可以离线穷举。
- Windows 平台不受此限制：Windows 上优先使用 DPAPI（绑定当前 Windows
  用户账户）加密 vault 内容。
- 中期改进方向：由 OS keyring 托管派生密钥，彻底移除二进制内置 pepper
  的依赖。在此之前，回退 vault 提供的是“同用户权限边界 + 适度 KDF 成本”
  的防护，机密性弱于 OS keyring / DPAPI，不应作为对抗同用户恶意进程的
  强隔离手段。
