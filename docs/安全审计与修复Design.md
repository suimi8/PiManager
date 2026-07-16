# Pi Manager 安全审计与修复 Design

> 状态：Proposed  
> 审计快照：2026-07-15 当前工作树  
> 适用版本：Pi Manager 1.6.4、Pi Cursor 扩展 0.5.0  
> 文档目标：把已确认的安全、可靠性、内存和性能问题转换为可实施、可迁移、可验收的修复方案。

---

## 1. 背景与结论

本次审计覆盖桌面端 Python 核心、PySide6 UI、配置与密钥持久化、模型测试、会话管理、自动更新器、Cursor 扩展、发布脚本和 CI。审计采用静态代码检查、现有测试复核和本地无害动态探针，不向真实第三方 Provider 发送攻击流量。

当前实现的常规功能测试全部通过，但仍存在五条需要优先关闭的信任边界：

1. Cursor 扩展可执行工作区提供的命令配置。
2. 导入包可通过带引号的 `!command` 绕过确认。
3. 桌面更新器缺少签名和哈希校验，且 TAR 可逃逸解压目录。
4. Provider 请求跨 Origin 重定向时会转发凭据。
5. Python 与 Node 不共享配置事务协议，能够稳定丢失更新。

内存方面未发现已证实的持续型泄漏，但存在响应体、子进程输出、任务队列和聊天历史导致的无界内存峰值。这些路径可引起 OOM、UI 卡顿或进程被操作系统终止，应按资源耗尽缺陷处理。

## 2. 范围与非目标

### 2.1 审计范围

- `pi_manager/core.py`：Provider、HTTP、Pi 子进程、会话和配置业务逻辑。
- `pi_manager/extras.py`：导入导出、更新、并发测试、健康历史和故障切换。
- `pi_manager/storage.py`、`pi_manager/secrets.py`：原子写入、锁和秘密存储。
- `pi_manager/ui.py`、`pi_manager/ui_features.py`：线程、取消、增量刷新和聊天状态。
- `extensions/pi-cursor/`：命令执行、凭据 helper、配置写入和 VSIX 更新。
- `.github/workflows/build.yml`、`scripts/`、`requirements.txt`：构建与供应链。
- `tests/` 和扩展测试：现有覆盖及缺失边界。

### 2.2 非目标

- 不评估第三方 Pi CLI、Provider 服务端或 Cursor/VS Code 本体的内部实现。
- 不把拥有当前用户权限的本机恶意软件纳入防御范围。
- 不在本设计中重写完整 GUI 或替换现有 Provider 配置格式。
- 不把显式勾选的“忽略 TLS 校验”诊断功能视为默认路径漏洞，但要求限制其生命周期和可见性。

## 3. 证据与优先级

### 3.1 证据等级

| 等级 | 含义 |
|---|---|
| 已复现 | 使用本地受控数据或服务观察到越界行为、数据丢失或秘密传播。 |
| 静态确认 | 执行路径明确成立，但未扩大资源消耗或破坏性影响。 |
| 加固项 | 当前没有直接利用证据，属于降低供应链、运维或未来回归风险。 |

### 3.2 优先级

| 优先级 | 处理原则 |
|---|---|
| P0 | 发布阻断。可导致本地代码执行、安装任意更新或凭据泄露。 |
| P1 | 下一修复版本必须完成。可导致配置损坏、OOM、秘密落盘或持续不可用。 |
| P2 | 后续迭代完成。主要影响规模化性能、权限加固和构建可复现性。 |

## 4. 问题清单

| ID | 优先级 | 证据 | 问题 | 主要位置 |
|---|---:|---|---|---|
| SEC-001 | P0 | 静态确认 | 不可信工作区可控制扩展执行命令 | `extension.js:216-264,322-395,539-585`；`package.json:145-182` |
| SEC-002 | P0 | 已复现 | 带引号的 `!command` 可绕过导入确认 | `extras.py:476-498`；`core.py:1157-1181` |
| SEC-003 | P0 | 已复现 | 更新无来源完整性校验，TAR 可路径/链接逃逸 | `extras.py:695-872,969-1047` |
| SEC-004 | P0 | 已复现 | Provider 凭据随重定向传播到新 Origin | `core.py:1562-1569,1827-1837` |
| REL-001 | P1 | 已复现 | Python/Node 配置写入竞态导致丢更新 | `storage.py:24-120`；`extension.js:43-96`；`failover.js:58-70` |
| PERF-001 | P1 | 静态确认 | HTTP、子进程、扩展 JSON 和聊天存在无界内存峰值 | `core.py:135-158,1567-1569,1833-1847`；`extension.js:608-692` |
| REL-002 | P1 | 静态确认 | QThread 取消无效并使用强制终止 | `ui.py:60-136`；`ui_features.py:169-187` |
| REL-003 | P1 | 静态确认 | 损坏配置和 Vault 被静默当成空数据 | `storage.py:73-80`；`secrets.py:225-245` |
| SEC-005 | P1 | 部分已复现 | 敏感 Header 明文持久化，URL/代理脱敏不完整 | `core.py:75-90,406-440,1342-1366` |
| REL-004 | P1 | 已由测试行为确认 | 429、Quota、Billing 被标记为永久 Key 失效 | `core.py:1095-1132`；`provider-keys.js:3-24` |
| SEC-006 | P2 | 静态确认 | 回退主密钥创建存在权限窗口且缺少启动校验 | `secrets.py:132-156` |
| PERF-002 | P2 | 静态确认 | 会话扫描、批量 Future 和 UI 刷新随规模退化 | `core.py:798-851`；`extras.py:142-214`；`ui.py:2021-2047` |
| SC-001 | P2 | 加固项 | Action、Python 依赖和 VSCE 未固定不可变版本 | `.github/workflows/build.yml:36-42,73-77,146-156` |

## 5. 威胁模型

### 5.1 需要保护的资产

- Provider API Key、自定义 Authorization Header 和带认证信息的代理 URL。
- `settings.json`、`models.json`、`pi-manager.json`、Vault 和历史记录的完整性。
- 当前安装目录、更新包和 Cursor 扩展安装产物。
- 当前用户会话、工作区文件以及 Pi 子进程权限。
- 应用进程的内存、线程、文件描述符和网络并发预算。

### 5.2 受控输入

- Cursor 工作区及其 `.vscode/settings.json`。
- 用户导入的配置 ZIP。
- Provider URL、Header、HTTP 响应和重定向地址。
- 更新 manifest、Release API 响应和压缩包成员。
- Pi 子进程 stdout/stderr。
- 另一个 Pi Manager/Cursor/Pi 进程对同一配置文件的并发修改。

### 5.3 核心安全不变量

1. 工作区和导入包中的字符串不能直接变成本地可执行程序。
2. Provider 凭据只能发送到用户配置且经过确认的目标 Origin。
3. 未通过来源、签名、哈希、平台和包结构校验的更新不得进入安装目录。
4. 一个成功返回的配置 mutation 不得被另一个成功 mutation 静默覆盖。
5. 损坏配置不得自动转换为默认值后覆盖原文件。
6. 所有外部输入都必须有字节、成员数、并发数和保留时间上限。
7. 日志、UI、历史和导出包不得包含可直接使用的秘密。

## 6. 目标架构

```text
Cursor Commands / Desktop UI
            |
            v
   Command Policy Gate ---------> Workspace Trust + machine-scope config
            |
            +-------------------> Config Broker -----> Atomic Store + Backups
            |                            |
            |                            +-----------> Secret Store
            |
            +-------------------> Safe HTTP Client --> Redirect/size/redaction policy
            |
            +-------------------> Managed Process ---> cancellation + bounded output

Update UI -> Signed Manifest -> Verified Downloader -> Safe Extractor
                                                -> Staged Self-check
                                                -> Versioned Install + Rollback
```

设计原则：

- 只有一个模块负责解释可执行配置。
- 只有一个模块负责写共享配置。
- 只有一个 HTTP Transport 负责重定向、资源限制和脱敏。
- 更新验证和安装分离，验证失败时安装目录保持完全不变。
- 取消必须从 UI 传递到网络、线程池和子进程，而不是强杀 Python 线程。

## 7. Design A：命令执行与配置导入

### 7.1 Cursor 工作区信任

在 `package.json` 增加受限工作区声明，并把所有影响进程执行的配置设为 machine scope：

```json
{
  "capabilities": {
    "untrustedWorkspaces": {
      "supported": "limited",
      "description": "未信任工作区中禁用 Pi 进程启动和凭据 helper。",
      "restrictedConfigurations": [
        "pi.command",
        "pi.extraArgs",
        "pi.providerEnvCommand"
      ]
    }
  }
}
```

每个会启动进程的命令在入口统一调用：

```js
function requireTrustedExecution() {
  if (!vscode.workspace.isTrusted) {
    throw new Error("当前工作区未受信任，已禁止启动本地进程");
  }
}
```

不能只依赖 manifest。读取配置时使用 `configuration.inspect()`，如果 `workspaceValue` 或 `workspaceFolderValue` 非空，则忽略并记录安全事件。旧配置只允许从 `globalValue` 迁移。

### 7.2 结构化进程调用

废弃字符串形式的命令和 `extraArgs`，内部统一为：

```ts
type ProcessSpec = {
  executable: string;
  args: string[];
  cwd: string;
  env: Record<string, string>;
};
```

- 快速提问使用 `execFile(executable, args, options)`。
- 交互会话优先使用 `createTerminal({ shellPath, shellArgs, cwd, env })`。
- 不使用 `sendText()` 执行拼接后的 Shell 字符串。
- `.cmd/.bat` 如必须通过 `cmd.exe` 启动，参数由专用 Windows 参数编码器生成，不接受用户提供的命令片段。
- `pi.providerEnvCommand` 替换为受信任的 helper 路径加固定参数；不再解析通用字符串命令 DSL。

### 7.3 移除 `!command` 凭据

`apiKey` 和 Header 的命令型值默认永久禁用。允许的凭据引用仅包括：

```text
${ENV_NAME}
${PI_MANAGER_PROVIDER_<ID>_API_KEY}
${PI_MANAGER_PROVIDER_<ID>_HEADER_<ID>}
```

导入流程使用严格 Schema 校验已知字段。所有字符串先执行统一的 Unicode、空白和成对引号规范化，再拒绝以下内容：

- 规范化后以 `!` 开头的值。
- 旧 `__DPAPI__:` 之外的未知内部标记。
- Header 中未声明类型的对象、数组或嵌套可执行字段。

为了兼容确需外部凭据工具的高级用户，可在后续版本引入本机 Credential Provider Registry：

```json
{
  "provider_id": "local-keychain-helper",
  "executable": "C:/Program Files/PiManager/helper.exe",
  "args": ["get", "provider-id"],
  "sha256": "...",
  "created_by_user": true
}
```

Registry 位于用户级安全目录，不包含在配置导出包中，也不能由工作区或 Provider 配置创建。

### 7.4 迁移行为

- 启动时发现现有 `!command`：标记 Provider 为“需要迁移”，不执行命令。
- UI 提供“改为环境变量”或“写入安全密钥库”操作。
- 导入含命令值的包直接失败，不再提供“仍然导入”选项。
- 迁移完成前不自动覆盖原配置，并生成脱敏备份。

## 8. Design B：统一安全 HTTP Transport

### 8.1 接口

Python 新增单一 Transport，例如 `pi_manager/http_client.py`：

```python
@dataclass(frozen=True)
class RequestPolicy:
    timeout_seconds: float
    max_response_bytes: int
    max_error_bytes: int = 64 * 1024
    redirect_mode: str = "deny"
    max_redirects: int = 0
    require_https: bool = True


@dataclass
class HttpResult:
    status: int
    body: bytes
    latency_ms: float
    public_url: str
    public_proxy: str
```

所有模型拉取、模型测试、更新检查和下载都必须经过该模块。Cursor 扩展实现相同策略，或者通过桌面 helper 复用 Python Transport。

### 8.2 重定向策略

Provider 敏感请求默认 `redirect_mode="deny"`。如果某个已知 Provider 必须重定向，只允许满足以下全部条件：

1. 新旧 URL 的规范化 `(scheme, hostname, effective_port)` 完全相同。
2. 不允许 HTTPS 降级到 HTTP。
3. 重定向次数不超过显式上限。
4. 重新构造请求，不自动复制 `Authorization`、`Cookie`、`Proxy-Authorization` 和自定义敏感 Header。
5. 跨 Origin 一律返回 `redirect_blocked`，UI 只显示脱敏后的目标 Host。

更新下载可以允许固定 Origin 集合内的跨域 CDN 跳转，但每个 Origin 必须存在于签名 manifest 的 allowlist 中，且下载后仍要校验长度和 SHA-256。

### 8.3 响应上限

建议初始预算：

| 请求类型 | 最大响应/文件 | 超限行为 |
|---|---:|---|
| 模型列表 JSON | 4 MiB | 关闭连接并返回 `response_too_large` |
| 模型测试正文 | 2 MiB | 保留受限预览，停止读取或终止请求 |
| 错误正文 | 64 KiB | 截断并脱敏 |
| 更新 manifest | 1 MiB | 拒绝 |
| VSIX | 50 MiB | 删除 `.part` 文件并拒绝 |
| 桌面更新包 | 512 MiB | 删除 `.part` 文件并拒绝 |

读取规则：

- 先检查可信的 `Content-Length`，超过上限立即拒绝。
- 无长度或长度不可信时，按 64 KiB 分块读取并累计。
- 不使用无参数 `resp.read()`。
- 若未来启用内容压缩，同时限制解压后字节数和压缩比。
- JSON 解析前先完成字节上限和 UTF-8 策略检查。

### 8.4 集中式脱敏

新增 `RedactionContext`，记录本次操作实际解析出的秘密值、敏感 Header 名和代理信息。任何数据进入 UI、日志或历史前统一清洗：

- URL 删除 `username:password@`，敏感查询参数替换为 `***`。
- `Authorization`、`Cookie`、`Set-Cookie`、`x-api-key` 等 Header 不写入结果对象。
- 自定义 Header 根据显式元数据和名称规则判断；实际 Secret 值做精确替换作为第二道保护。
- 代理只显示 `scheme://host:port` 和“包含认证信息”标志。
- Provider 错误体先尝试按 JSON 字段脱敏，再做 Secret 值替换，最后截断。
- 历史持久化前再执行一次脱敏，避免调用方遗漏。

## 9. Design C：可信更新链与安全安装

### 9.1 临时策略

在签名更新链完成前：

- 禁用自定义 `update_manifest_url` 的自动安装能力。
- 桌面端只允许打开官方 Release 页面，不执行原地覆盖。
- VSIX 可以提示更新，但自动安装必须等待哈希/签名验证完成。

### 9.2 签名 manifest

Release 流程生成规范化 JSON，并使用离线 Ed25519 发布密钥签名：

```json
{
  "schema_version": 1,
  "product": "pi-manager",
  "version": "1.6.5",
  "published_at": "2026-07-15T00:00:00Z",
  "expires_at": "2026-08-15T00:00:00Z",
  "minimum_version": "1.6.4",
  "assets": [
    {
      "platform": "windows",
      "arch": "x86_64",
      "format": "zip",
      "url": "https://github.com/.../PiManager-v1.6.5-windows-x64-dir.zip",
      "size": 12345678,
      "sha256": "..."
    }
  ],
  "signature": "base64..."
}
```

要求：

- 客户端内置一个或多个发布公钥，私钥不进入仓库和普通 CI Secret。
- 签名覆盖移除 `signature` 后的 RFC 8785/JCS 规范化 JSON。
- 校验产品名、过期时间、版本单调性、平台、架构、格式、URL Origin、长度和哈希。
- 无平台/架构精确匹配时失败，不再回退选择第一个资产。
- 支持双签名密钥轮换；旧客户端只信任已经内置的根密钥。
- 中长期可迁移到 TUF，以覆盖回滚、冻结和密钥撤销场景。

### 9.3 下载与验证

1. 下载到用户专属更新目录中的随机 `.part` 文件。
2. 按块计算 SHA-256 并执行总字节限制。
3. 文件长度和哈希必须与已签名 manifest 完全一致。
4. `fsync` 后将 `.part` 原子改名为只读缓存文件。
5. 验证失败删除临时文件并记录不含 URL 凭据的事件。
6. 同一版本和哈希可复用缓存，不复用仅版本相同但哈希不同的文件。

### 9.4 安全解压

不得直接调用 `extractall()`。ZIP/TAR 每个成员在写入前执行：

- 将反斜杠转为 `/`，拒绝绝对路径、盘符、UNC、空路径、`.` 和 `..`。
- 通过 `target.resolve()` 验证目标仍在 staging 根目录内。
- 拒绝符号链接、硬链接、设备文件、FIFO 和 Socket。
- 拒绝重复路径以及 Windows/macOS 上大小写折叠后的冲突。
- 限制成员数、单成员大小、解压后总大小和压缩比。
- 先创建普通目录，再以独占方式创建普通文件。
- 文件权限只允许普通文件 `0644` 和明确入口文件 `0755`，清除 setuid/setgid 位。

初始限制建议：成员数 20,000、单文件 256 MiB、总大小 1 GiB、最大压缩比 100:1。

### 9.5 包结构校验

解压后必须存在签名覆盖的 `update-package.json`，其中声明入口文件和包内文件摘要。校验内容包括：

- 只存在当前平台允许的应用布局。
- 主入口存在、是普通文件且摘要匹配。
- 不能从任意 `rglob()` 结果推测第一个可执行程序为应用根。
- 冻结包执行 `--self-check`，返回码为 0 后才允许安装。
- macOS 校验签名和 Bundle ID；Windows 后续发布应加入 Authenticode 校验。

### 9.6 版本化安装与回滚

目标布局采用 side-by-side 版本目录：

```text
PiManager/
  launcher/
  versions/
    1.6.4/
    1.6.5.staging/
  current.json
  update-journal.json
```

安装事务：

1. 写入 `1.6.5.staging` 并完成包校验、自检和 fsync。
2. 将 staging 原子改名为 `1.6.5`。
3. 写入 journal，记录旧版本、新版本和事务状态。
4. 原子替换 `current.json` 指针。
5. 启动新版本并等待启动成功标记。
6. 启动失败或超时则恢复旧指针。
7. 至少保留一个上一版本，成功运行一段时间后再清理更旧版本。

Windows 不再直接覆盖正在运行的 EXE，也不使用“先删除目标再 rename”。如果短期无法引入稳定 launcher，必须先做同目录备份、安装日志和失败回滚，再退出旧进程。

## 10. Design D：统一配置事务

### 10.1 决策

采用单写入者 Config Broker。Cursor 扩展不再直接写 `settings.json` 或 `pi-manager.json`，而是通过 Pi Manager helper 执行白名单 mutation。这样可复用 Python 已有锁、备份、Schema 和损坏检测，避免在 Node 中实现另一套不兼容的 Windows 锁。

读取可以保持直接只读，但解析失败必须显示错误，不能返回 `{}` 后继续写。

### 10.2 Broker 协议

请求通过 stdin/stdout JSON 或权限受限的临时文件传递：

```json
{
  "schema_version": 1,
  "request_id": "uuid",
  "operation": "record_model_failure",
  "arguments": {
    "provider": "demo",
    "model": "model-a"
  },
  "expected_revision": 42
}
```

响应：

```json
{
  "ok": true,
  "request_id": "uuid",
  "revision": 43,
  "result": {"failure_count": 2}
}
```

首批白名单操作：

- `set_default_model`
- `sync_enabled_models`
- `record_model_failure`
- `record_model_success`
- `set_manager_fields`
- `import_config_transaction`

不提供任意文件路径、任意 JSON Pointer 或任意命令执行操作。

### 10.3 存储格式与 revision

不能改变 Pi CLI 直接消费的 `settings.json`/`models.json` 顶层格式，因此 revision 存入独立 sidecar：

```json
{
  "settings.json": {"revision": 43, "sha256": "..."},
  "models.json": {"revision": 18, "sha256": "..."},
  "pi-manager.json": {"revision": 91, "sha256": "..."}
}
```

mutation 在锁内完成：读取原文件和 revision、验证 Schema、应用字段级变更、写临时文件、fsync、平台原子替换、更新 revision。`expected_revision` 不匹配时返回 `conflict`，调用方重新读取后重试有限次数。

外部 Pi CLI 不遵守本锁时，Broker 通过 sidecar SHA-256 识别外部变化，将其作为新 revision 基线后再应用字段级 mutation。此机制不能让不配合的进程获得严格串行化，但可以避免使用旧快照覆盖整份文档，并记录冲突。

### 10.4 Windows 原子替换

- 优先调用 `ReplaceFileW`，同时生成 `.bak`。
- 目标不存在时使用同卷临时文件加原子 rename。
- 禁止先 `rmSync(target)` 再 rename。
- POSIX 写入完成后对文件和父目录执行 `fsync`。

### 10.5 多文件导入事务

导入包使用 write-ahead journal：

```json
{
  "transaction_id": "uuid",
  "state": "prepared",
  "files": [
    {"target": "settings.json", "old_sha256": "...", "new_sha256": "..."}
  ]
}
```

先完成全部 Schema、命令值、秘密和容量校验，再写 staging 与 journal。提交中断时，下次启动根据 journal 恢复全部旧文件或完成提交，不能出现部分文件为新版本、部分文件为旧版本。

## 11. Design E：损坏配置与密钥存储

### 11.1 显式加载状态

`load_json()` 不再只返回数据，内部改为：

```python
@dataclass
class LoadResult:
    status: Literal["ok", "missing", "corrupt", "unsupported"]
    data: Any
    error: str = ""
    source_path: Path | None = None
    backup_path: Path | None = None
```

- `missing` 才能使用默认值。
- `corrupt` 和 `unsupported` 进入 fail-closed，只读状态。
- UI 展示恢复、导出原文件、选择备份和重置操作。
- 未经用户明确确认，不移动、不删除、不覆盖损坏文件。
- 写入前再次确认当前加载状态不是 `corrupt`。

### 11.2 备份策略

- 每次成功写入保留最近 2 份校验通过的备份。
- 备份和正式文件都记录 SHA-256、Schema 版本和写入时间。
- 备份轮换在成功提交后进行，失败不能删除最后可用版本。
- Vault 解密失败时禁止调用 `save_vault({})`。

### 11.3 主密钥

POSIX：

- 使用 `os.open(..., O_CREAT | O_EXCL, 0o600)` 创建权限正确的临时文件。
- 写入 32 字节后 `fsync`，再以不覆盖已有密钥的方式发布。
- 启动时校验所有者、普通文件类型、长度和 mode；异常时拒绝解密并提示修复。

Windows：

- 正常路径继续使用 DPAPI。
- 文件回退路径设置仅当前用户可读写的明确 ACL。
- DPAPI 异常不得静默降级后继续长期运行，应产生可见告警。

### 11.4 自定义 Header 密钥化

Provider Header 增加 `sensitive` 元数据。常见敏感 Header 默认开启，值写入 OS keyring/Vault，`models.json` 只保留环境引用。迁移时扫描 `authorization`、`api-key`、`token`、`secret`、`cookie` 等名称，并允许用户补充自定义敏感字段。

## 12. Design F：资源预算、取消和性能

### 12.1 受控子进程

新增 `ManagedProcess`：

- 使用 `Popen`，stdout/stderr 由独立 reader 增量读取。
- 每个流最多保留 8 MiB；UI 只保留尾部和必要前缀。
- 达到硬上限时终止进程，返回 `output_limit_exceeded`。
- 超时或取消时先协作终止，再在宽限期后强制终止进程树。
- Windows 使用 Job Object 或独立进程组；POSIX 使用新 session 和 `killpg`。
- 任何返回结果在进入日志和历史前脱敏。

### 12.2 协作取消

所有后台任务接收统一 `CancellationToken`：

```python
class CancellationToken:
    def is_cancelled(self) -> bool: ...
    def raise_if_cancelled(self) -> None: ...
```

- HTTP 每次读取分块前检查 token，并在取消时关闭响应。
- ThreadPool 停止继续提交，取消未开始 Future。
- 子进程收到取消后终止进程树。
- QThread 只负责等待业务任务完成并发信号。
- 删除 `QThread.terminate()`；应用退出时显示有限等待状态，完成清理后退出。

### 12.3 有限任务队列

批量测试最多保持 `max_workers * 2` 个在途 Future。完成一个再提交一个，结果可以按输入索引增量落盘，不预先创建全部 Future。取消后未提交项目直接标记为 cancelled。

### 12.4 UI 增量刷新

- Worker 结果先进入线程安全队列。
- UI 使用 100 至 250 ms 的 QTimer 合并更新。
- 只更新对应模型行和计数，不在每个结果后重建全部表。
- 历史表仅在批次结束或节流窗口内刷新一次。
- 大表启用 Model/View 和虚拟化，避免大量 `QTableWidgetItem` 重建。

### 12.5 聊天与历史上限

- Prompt 上下文：最多 6 轮且最多 128 KiB。
- 内存中的 `chat_history`：最多 20 轮或 512 KiB，以先达到者为准。
- `QPlainTextEdit`：最多 10,000 block 或 1 MiB 可见文本。
- 测试历史保持现有 500 条限制，但写入前统一脱敏。
- 健康记录删除配置中已不存在且超过 30 天的模型。

### 12.6 会话索引

短期将全量排序改为 `heapq.nlargest(limit, ...)`，内存从 O(N) 降到 O(limit)，并缓存一次 `stat()` 结果。长期增加 SQLite 会话索引：

- 后台增量扫描文件变更。
- 按 `mtime` 建索引，查询直接 `ORDER BY mtime DESC LIMIT ?`。
- 文件删除或解析失败时更新索引状态。
- 索引可随时重建，不作为唯一数据源。

## 13. Design G：API Key 状态机

### 13.1 状态定义

```text
available  -> 可立即使用
cooldown   -> 短期限流，retry_at 后自动恢复
restricted -> Quota/Billing/权限受限，需要定时探测或用户操作
invalid    -> 明确无效、撤销或认证失败，需要用户恢复/替换
```

每个 Key 记录：

```json
{
  "status": "cooldown",
  "failure_kind": "rate_limit",
  "failed_at": "2026-07-15T00:00:00Z",
  "retry_at": "2026-07-15T00:01:00Z",
  "failure_count": 1,
  "last_reason": "HTTP 429"
}
```

### 13.2 分类规则

| 信号 | Key 状态 | 行为 |
|---|---|---|
| HTTP 401 或明确 `invalid_api_key` | `invalid` | 切换下一 Key，等待用户修复 |
| HTTP 429 | `cooldown` | 解析 `Retry-After`，无值时指数退避加抖动 |
| Quota/Credit/Billing | `restricted` | 不永久删除，降低探测频率并提示账户状态 |
| HTTP 403 | 默认 `restricted` | 只有明确 Key 撤销语义才设为 `invalid` |
| HTTP 5xx、DNS、连接失败、超时 | 状态不变 | 记录 Provider/模型瞬时失败 |
| 请求成功 | `available` | 清理 cooldown 和连续失败计数 |

Python 是状态分类的唯一实现。Cursor 扩展通过 helper 提交结构化的 HTTP 状态和 Provider 错误码，由 helper 返回状态转换，避免 Python/Node 两套正则再次漂移。

## 14. Design H：构建与供应链

### 14.1 依赖固定

- 使用 `pip-tools` 或等价工具生成带 SHA-256 的平台 lock 文件。
- `PyInstaller`、`PySide6`、`cryptography`、`keyring` 和 `certifi` 固定完整版本。
- 扩展增加 `package-lock.json`，把 `@vscode/vsce` 设为精确版本的 devDependency。
- `package_extension.py` 禁止无版本的 `npx @vscode/vsce` 回退。

### 14.2 CI 固定与发布证明

- GitHub Actions 使用完整 Commit SHA，不只使用 `@v4`、`@v5`。
- 构建产物生成 SHA-256、CycloneDX/SPDX SBOM 和 provenance。
- Release manifest 签名步骤与普通构建权限隔离。
- macOS 使用 Developer ID 签名和 notarization；Windows 使用 Authenticode。
- CI 验证 lock 文件未漂移、源码版本与包版本一致、产物可重复构建的关键摘要。

## 15. 分阶段实施

### Phase 0：发布阻断修复

必须全部完成后才能恢复自动更新或发布安全修复版：

1. 扩展执行入口增加 Workspace Trust 守卫并忽略 workspace-scope 命令配置。
2. 禁用 `!command`，修复所有规范化差异和导入绕过。
3. Provider 请求禁用自动重定向。
4. 禁用未签名的原地更新和任意 manifest 安装。
5. 为上述四项增加负向安全测试。

### Phase 1：可靠性与资源边界

1. 引入 Config Broker，删除扩展直接写共享配置的逻辑。
2. 引入有上限的 HTTP 和子进程读取。
3. 用协作取消替换 `QThread.terminate()`。
4. 区分 missing/corrupt，增加备份恢复。
5. 完成统一脱敏和敏感 Header 密钥化。
6. 上线新的 Key 状态机。

### Phase 2：可信更新和规模化性能

1. 上线签名 manifest、哈希下载、安全解压和回滚。
2. VSIX 使用同一可信发布元数据。
3. 批量测试使用有限队列，UI 合并增量刷新。
4. 引入会话索引、健康记录清理和聊天容量限制。
5. 固定依赖、Action SHA，生成 SBOM 和 provenance。

### Phase 3：清理兼容代码

- 删除 `!command` 解析器和旧确认 UI。
- 删除 Node 侧直接配置写入和重复 Key 分类逻辑。
- 删除旧原地覆盖更新脚本。
- 在至少两个稳定版本后移除旧未认证 Vault 格式的写入兼容，仅保留受控导入迁移工具。

## 16. 回归测试矩阵

| 测试 ID | 场景 | 预期结果 |
|---|---|---|
| TRUST-001 | 未信任工作区配置 `pi.command` | 命令不执行，显示受限提示 |
| TRUST-002 | 工作区覆盖全局 `providerEnvCommand` | 忽略工作区值，只使用 machine/global 安全值 |
| TRUST-003 | 可信工作区正常启动 Pi | 参数以数组传递，功能不回归 |
| IMPORT-001 | `!cmd`、`"!cmd"`、`'!cmd'` 和前导空白 | 全部导入失败且不修改文件 |
| IMPORT-002 | Header 中命令值 | 导入失败且不执行探针 |
| IMPORT-003 | 非法 JSON 或超大 ZIP | 失败，原配置和秘密保持不变 |
| IMPORT-004 | 提交中途模拟进程终止 | 下次启动完整回滚或完成提交 |
| HTTP-001 | 同 Origin 302 | Provider 默认仍拒绝；显式策略下按规则处理 |
| HTTP-002 | 跨 Host、端口或协议重定向 | 拒绝，第二服务收不到任何敏感 Header |
| HTTP-003 | HTTPS 跳转 HTTP | 拒绝 |
| HTTP-004 | 100 MiB 无 Content-Length 响应 | 在配置上限处停止，RSS 增量受控 |
| HTTP-005 | 错误体回显 API Key 和 URL userinfo | UI、日志、历史中均不可见 |
| UPDATE-001 | manifest 签名错误或已过期 | 拒绝下载/安装 |
| UPDATE-002 | 资产长度或 SHA-256 不匹配 | 删除临时文件，安装目录不变 |
| UPDATE-003 | 无当前平台/架构资产 | 明确失败，不选择第一个资产 |
| UPDATE-004 | TAR/ZIP 包含 `..`、绝对路径、符号链接、硬链接 | 解压前拒绝，无 staging 外文件 |
| UPDATE-005 | ZIP Bomb、超成员数、大小写冲突 | 资源上限内拒绝 |
| UPDATE-006 | 新版本自检失败或首次启动失败 | 自动回滚上一版本 |
| CONFIG-001 | Python 与 Node 并发 1,000 次字段 mutation | 无静默丢更新，revision 单调递增 |
| CONFIG-002 | 写临时文件后、替换前模拟崩溃 | 正式文件可读，临时文件可安全清理 |
| CONFIG-003 | Windows 替换目标被占用 | 返回可恢复错误，不删除正式文件 |
| CORRUPT-001 | 截断 JSON | 显示 corrupt，禁止默认值覆盖 |
| CORRUPT-002 | Vault 密文被篡改 | fail-closed，可选择备份恢复 |
| KEY-001 | HTTP 401 invalid key | 仅当前 Key 进入 invalid 并轮换 |
| KEY-002 | HTTP 429 带/不带 Retry-After | 进入 cooldown，到期自动恢复候选资格 |
| KEY-003 | Quota/Billing | 进入 restricted，不永久删除 |
| KEY-004 | 5xx、DNS、超时 | Key 状态不变 |
| CANCEL-001 | HTTP、子进程、批量测试中途退出 | 全部协作结束，无 QThread 强杀和遗留进程 |
| PERF-001 | 100,000 个会话文件查询前 100 条 | 内存 O(limit)，UI 不被长时间阻塞 |
| PERF-002 | 10,000 个模型批量测试并取消 | 在途任务有界，取消后不继续提交 |
| PERF-003 | 长时间聊天和 8 小时健康检查 soak | 历史、Widget、线程和句柄保持在预算内 |

安全测试必须使用本地受控 HTTP 服务、临时 HOME 和无害命令探针。不得在测试日志中打印真实 Key。

## 17. 性能与资源验收预算

以下预算需在 CI 或固定基准机上记录基线，超过预算视为回归：

- 4 MiB 模型响应的读取峰值额外 RSS 不超过 32 MiB。
- 100 MiB 响应在达到策略上限后立即拒绝，不把完整正文保存在内存。
- 批量任务初始化内存与总模型数近似无关，只与有限在途队列相关。
- 聊天达到上限后继续 1,000 轮，内存中的历史条数和可见文本不再增长。
- 后台任务取消后 5 秒内无业务 Worker 和子进程存活；特别慢的系统操作应返回明确超时状态。
- 会话列表只解析最终 Top-K 文件的元数据；长期方案从索引返回首屏结果。

## 18. 可观测性与日志

增加结构化安全/可靠性事件：

```text
workspace.execution_blocked
import.executable_value_rejected
http.redirect_blocked
http.response_limit_exceeded
update.signature_failed
update.asset_hash_failed
update.rollback_completed
config.conflict
config.corrupt
process.output_limit_exceeded
task.cancel_timeout
```

日志要求：

- 不记录完整 Header、响应体、环境变量和代理凭据。
- URL 只记录脱敏后的 scheme/host/port/path。
- Request ID 可用于关联，不使用 API Key 摘要作为标识。
- 本地日志默认轮转，例如单文件 10 MiB、保留 3 份。
- 用户导出的诊断包必须再次执行集中式脱敏。

## 19. 发布与回滚门槛

发布候选版本必须满足：

1. 现有 Python 和 Cursor 扩展功能测试全部通过。
2. Phase 对应的安全回归测试全部通过。
3. 无跨 Origin 凭据传播、无恶意归档逃逸、无导入命令执行。
4. 跨语言并发测试无静默丢更新。
5. 损坏配置和 Vault 不会被默认值覆盖。
6. 资源上限和取消 soak 测试通过。
7. 更新产物具备签名 manifest、哈希、SBOM 和回滚验证。

如安全修复引发兼容性问题，回滚只能恢复应用版本，不能恢复已禁用的 `!command`、未签名更新或跨 Origin 凭据转发。

## 20. 完成定义

本 Design 视为完成实施需同时满足以下条件：

- 任意工作区或导入包都不能提供直接执行的本地命令字符串。
- Provider 凭据只发送到明确允许的 Origin，重定向测试的接收端看不到秘密。
- 篡改、错误平台、恶意归档或自检失败的更新不会改变安装目录。
- Python、Cursor 和外部配置变化不会被旧快照静默覆盖。
- HTTP、子进程、任务队列、聊天和历史均存在代码级硬上限。
- 应用退出不再调用 `QThread.terminate()`。
- 损坏配置进入可恢复状态，不能自动清空后保存。
- 429/Quota/Billing 不再永久标记 Key 无效。
- 所有负向安全测试进入 CI，并且发布流程使用不可变依赖和签名产物。

---

## 附录 A：当前验证基线

在 2026-07-15 当前工作树上：

- Python：`51 passed`
- Cursor 扩展：`12 passed`
- Python `compileall`：通过
- Node `--check`：通过

这些结果是后续修复不得破坏的功能基线，不代表上述攻击边界已经受到保护。

## 附录 B：已完成的无害动态证据

- 带引号的 `!command` 通过导入校验后被运行时解析为命令。
- TAR 的 `../` 成员写出目标解压目录。
- TAR 符号链接成员在解压后保留并可指向 staging 外部。
- Provider 302 到不同端口/主机名时，目标收到 Authorization 和自定义秘密 Header。
- Python 持锁旧快照、Node 写入、Python 再提交时，两个进程都成功但 Node 字段消失。
- URL 查询参数能脱敏，但 URL userinfo 和部分代理字段仍会显示。

动态证据只证明最小影响，未进行批量提取、真实凭据泄露或破坏性更新覆盖。
