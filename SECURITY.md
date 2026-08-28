# 安全策略

## 支持的版本

当前主线（源码 `main` 与最新 GitHub Release）为优先修复目标。

## 报告漏洞

请勿在公开 Issue 中粘贴真实 API Key、导出包密码或个人配置。

**私密报告渠道（首选）**：GitHub Security Advisory。打开仓库
https://github.com/suimi8/PiManager → **Security** 标签 → **Report a vulnerability**。
该渠道只有维护者可见，可安全附上复现步骤与影响分析。

若该渠道对你不可用，请在公开 Issue 中**只**描述问题类型与脱敏后的复现步骤
（不要附带真实密钥、真实 Base URL 或你的配置文件），并说明希望私下沟通细节。

请在报告中包含：受影响版本（`PiManager --self-check` 输出的 `version=`）、操作系统、
以及是否涉及密钥存储 / 导入导出 / 插件安装 / Cursor 扩展这几条敏感路径。

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
- **Linux headless / 无 D-Bus 环境**（服务器、容器、纯 SSH 会话）通常没有可用的
  Secret Service 后端，此时会**静默回退**到本节描述的文件 vault。也就是说这类
  部署默认就运行在上述较弱的防护下，而不是 OS keyring 上（`BUILD.md` 的平台能力
  表已标注）。
- 盐文件 `~/.pi/agent/.vault_master_key` 与 `secrets.vault` **同等敏感**：不要
  拷贝、不要提交、不要放进未加密的备份。`.gitignore` 与
  `scripts/check_secrets.py` 已同时把它、`secrets.index.json` 与配置 Broker 的
  `.broker-token` 纳入拦截范围。迁移配置请用「工具 → 导出配置包」（PBKDF2 +
  AES-256-GCM），而不是手工拷贝目录。
- 中期改进方向：由 OS keyring 托管派生密钥，彻底移除二进制内置 pepper
  的依赖。在此之前，回退 vault 提供的是“同用户权限边界 + 适度 KDF 成本”
  的防护，机密性弱于 OS keyring / DPAPI，不应作为对抗同用户恶意进程的
  强隔离手段。

### 已被移除支持的旧 vault 格式（升级即生效）

- **`local:`（固定硬编码密钥）已永久移除，无迁移开关**。该格式的加密密钥硬编码在
  程序内，任何本地写入者都能构造合法密文注入凭据，且注入内容会被自动重写成 DPAPI
  格式「洗白」——即攻击者写入的 Key 会获得与合法 Key 相同的可信外观。仍是该格式的
  用户需在「Provider 管理 → API Keys」重新填入密钥。
- **明文 JSON 格式默认拒绝读取**，需临时设 `PI_MANAGER_ALLOW_LEGACY_VAULT=1` 启动
  一次完成迁移（迁移后立刻重写为认证加密格式），之后应取消该变量。迁移完成后会
  自动擦除仍含明文的 `models.json.bak.*` 与残留 `.tmp`。
- 旧式「裸环境变量名」形式的 apiKey 仅在该变量当前确实存在时才自动迁移。此前的
  `^[A-Z][A-Z0-9_]{2,}$` 启发式会把 `AKIAIOSFODNN7EXAMPLE` 这类**真实密钥**当成变量
  名，明文写进 `models.json` 且界面不打码。现在只承认显式的 `$NAME` / `${NAME}`。

## 本地 CLI 入口的授权模型

轻量 CLI 入口（`--print-provider-env` / `--provider-env`、`--config-mutate`、
`--vision-describe`）以当前用户身份运行，因此**同用户的任意进程都能调用它们**。

- **`--print-provider-env` 要求出示 broker token**：调用方必须按值提交
  `~/.pi/agent/.broker-token` 的内容（`--token <值>` 或 `--token-file <文件>`），
  以此证明它读得到该文件。此前它零认证即可吐出明文 API Key，而只写白名单字段的
  `--config-mutate` 反而要 token——授权模型是倒置的，现在两者共用同一套校验。
  影响：**旧版 Cursor 扩展（VSIX）不带 token，会取不到密钥**，需同步升级扩展。
- `--token-file` **拒绝直接指向 `.broker-token` 本身**：helper 以用户身份运行，
  接受一个「请你自己去读那个文件」的路径等于把它变成 confused deputy。
- token 由 `config_broker` 轮换（默认 180 天）；调用方应在收到 token 相关错误时
  重读磁盘上的 token 再试一次。

## 配置包导入的信任边界

导入的配置 ZIP 由不可信作者产出，且下一次运行的 Pi 拥有 shell 权限，因此：

- **默认不覆盖 `AGENTS.md`**：覆盖它等于让下一次运行的 Pi 遵循配置包作者的指令，
  是一条间接提示注入（indirect prompt injection）通路。导入结果会明确列出被跳过项。
- **拒绝含可执行语义键的 `settings.json`**：`hook` / `mcpServer` / `command` /
  `shell` / `exec` / `helper` / `interpreter` / `env` / `permissions`。
- 用户插件只做静态校验、不执行插件代码；`trust` 不提供沙箱，未信任插件不会被启用；
  内置插件 `target_dir` 必须落在 `~/.pi/agent/` 内。

## 启动 Pi 子进程的命令行边界

Provider / Model / Thinking 是恶意配置包唯一能控制的命令行内容，现由
`core_process.validate_launch_tokens()` 施加逐字段字符白名单：

| 字段 | 允许字符 | 长度上限 |
|---|---|---|
| `--provider` | 字母、数字与 `. _ - : @ +` | 64 |
| `--model` | 同上，再加 `/` | 128 |
| `--thinking` | 字母、数字与 `_ -` | 32 |

含 `"` `&` `|` `%` `!` 等字符的名称会**拒绝启动 Pi**——这些字符此前可经 Windows
`cmd.exe` 的 npm 风格 shim 注入任意命令。校验全平台生效，不只 Windows。

## 冻结产物的资源信任边界

冻结态**只信任 `sys._MEIPASS`** 内的内嵌资源，不再回退 exe 同级目录。此前
`exe_dir/assets` 的优先级高于内嵌资源，在便携单文件版旁投放一个
`assets/builtin/manifest.json` 即可让应用把攻击者的内置扩展（extension 拥有当前用户
完整权限，等于 PiManager 替它背书）落盘到 `~/.pi/agent/extensions/`。

## 平台文件权限加固

- POSIX：敏感文件强制 0600、敏感目录 0700。
- **Windows：`.broker-token`、`secrets.vault`、`.vault_master_key`、
  `secrets.index.json`、`pi-manager-helper.json` 会真正收紧 DACL 到仅当前用户。**
  此前的加固代码因三处 ctypes 声明错误加 `except Exception: pass`，在所有 Windows
  机器上都是空操作——即这些文件实际继承目录的默认 ACL。升级后才真正生效。
