# -*- coding: utf-8 -*-
"""Built-in usage tutorial and FAQ (Markdown).

本模块的 ``_HELP_MARKDOWN`` 是使用教程的**唯一来源**：``docs/使用教程.md`` 由
``python scripts/check_versions.py --write`` 从这里生成，CI 的 ``consistency`` job
与 ``tests/test_help_docs.py`` 会断言两者逐字一致。

为什么方向是「代码 → 文档」而不是反过来：应用内帮助页只能读代码常量
（``presentation/pages/help.py`` 取 ``help_sections()``），而 ``docs/*.md``
**没有被打包进产物**（两个 spec 都不收 docs/）。让文档当来源就必须改打包，
所以由代码生成文档。改教程内容请改本文件，不要手改 ``docs/使用教程.md``。
"""
from __future__ import annotations

import html
import re

from .extras import APP_VERSION

_RE_CODE = re.compile(r"`([^`]+)`")
_RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_RE_ITALIC = re.compile(r"\*([^*]+)\*")
_RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_RE_SEP = re.compile(r":?-{3,}:?")
_RE_UL = re.compile(r"^[-*] ")
_RE_OL = re.compile(r"^\d+\. ")

_HELP_MARKDOWN = r'''# Pi Manager 使用教程与常见问题

> 版本 __APP_VERSION__ · 跨平台 GUI 管理官方 Pi Coding Agent（Windows / macOS / Linux）
> 完整 agent 能力始终由官方 `pi` 提供，本工具负责配置、切换、测试与启动。

---

## 一、快速上手（3 分钟）

### 界面布局（侧边栏）

左侧边栏（参考 CC Switch 风格）：

1. **概览**（首页）：当前默认模型、连接状态、已配置 Provider、最近测试；快速接入与工作目录启动  
2. **Provider / 模型列表 / 工具 / 插件**：配置密钥、模型和扩展  
3. **快速提问 / 会话 / 健康监控 / 测试历史**：日常运行与诊断  
4. **设置 / 使用教程**：系统偏好与帮助  

日常先看「概览」确认现在能不能用。窗口可缩到 800×600；约 1024 宽以下会自动收起侧栏（只显示图标，不覆盖你手动保存的折叠偏好）。导入配置、小批量测试等较长结果显示在页头下方的结果面板，不再弹出大段对话框。



### 1. 安装 / 确认 Pi
1. 打开侧边栏 **工具** → **运行自检**，确认「Pi CLI」通过。  
2. 若未安装：设置页点 **安装/升级 Pi**，或工具栏按提示操作。  
3. 版本显示在右上角绿色标签（如 `pi: 0.80.6`）。

### 2. 添加自定义 Provider（推荐流程）
**方式 A：一键模板（最快）**
1. 进入 **Provider** → **新建 Provider**。  
2. 顶部 **常用模板** 下拉选择目标大模型（如 DeepSeek / 智谱 GLM / Kimi / 通义千问 / OpenAI / Claude / Gemini / Grokified …国内外主流 27 家）。  
3. Base URL、API 类型、常用模型列表、兼容选项自动填充，**只需粘贴自己的 API Key** → 保存。  
4. 回到 **模型列表** → **刷新模型列表**。

**方式 B：手动填写**
1. 在首页 **概览** 填写 Base URL + API Key 点「拉取并保存」，或进入 **Provider** → **从 API 拉取模型**。  
2. 填写：
   - Provider 名称（如 `免费grok`）
   - Base URL（如 `https://xxx/v1`）
   - API 类型（多为 `openai-completions`）
   - API Key（真实 `sk-...` 或环境变量名）
3. 点 **拉取上游模型** → 用搜索框过滤（如 `qwen`、`:free`）→ **勾选要接入的模型** → 用能力条确认 **1M 上下文 / 只开思考**（需要图片再勾选）→ 保存。不会默认写入全部上游模型。拉取返回 HTTP 503 / `upstream_overloaded` 时，只要已用模板预填模型即可直接保存，不必等拉取成功。  
4. 回到 **模型列表** → **刷新模型列表**。

### 3. 设为默认并启动
1. 模型列表已按 **Provider 分组为树状**：点击分组前的箭头可展开 / 收起，双击分组同样可切换。  
2. 选中某个模型 → **设为默认**（或双击模型行）。  
3. 在 **概览** 确认默认模型正确。  
4. 拖入工作文件夹，或点 **启动完整 Pi 会话** / **启动 Pi**。

### 4. 一键切换
- **收藏**：模型页选中 → **加入收藏**（可批量多选后加入）。  
- **托盘**：关闭窗口进托盘 → 右键 **切换默认模型**。  
- **enabledModels**：把收藏写入循环列表，Pi 内 `Ctrl+P` 切换。

---

## 二、功能分类说明

> 小节标题与侧边栏菜单项一一对应（共 11 页），照标题就能找到入口。

### 概览（首页）
- 顶部摘要：默认模型、连接状态、已配置 Provider、最近测试。
- 拖拽文件夹：设工作目录并可用默认模型启动 Pi。
- 收藏列表：双击设默认；支持批量测试收藏。
- 快速接入：拉取并保存时默认写入 1M 上下文、只开思考、不含图片。

### 模型列表
| 操作 | 说明 |
|------|------|
| 树状分组 | 模型按 Provider 分组；点击箭头或双击分组展开/收起，状态自动记忆 |
| 过滤 | 按关键字、Provider、思考/图片能力筛选（匹配 Provider 时保留整组） |
| 多选 | Ctrl/Shift 多选模型子项 |
| 设为默认 | 当前选中第一项（双击模型行） |
| 批量加入收藏 | 所有选中子项 |
| 测试选中 | 批量测可用性与延迟 |
| 测试当前过滤结果 | 对筛选后全部模型批量测 |
| 批量测试收藏 / 全部模型 | 放在「更多」菜单；会确认费用与覆盖范围 |
| 配置能力 | 选中自定义 Provider 模型后，一键写入上下文与思考/图片；默认 1M 上下文、只开思考、不含图片 |
| 启动 Pi | 用选中模型开官方会话 |

**测试方式**
- **自动(HTTP优先)**：自定义 Provider 先 HTTP，失败再 Pi  
- **HTTP 直连**：只打 BaseURL（适合中转）  
- **Pi 实测**：官方 `pi -p`（适合 OAuth / 内置）

### Provider
- 增删改自定义 Provider。
- **常用模板**：新建 Provider 时从国内外主流大模型模板选择（OpenAI / Claude / Gemini / DeepSeek / GLM / Kimi / Qwen / ERNIE / 混元 / 星火 / 豆包 / SiliconFlow 等），自动填充 Base URL、API 类型与模型列表，仅需粘贴 API Key。
- **从 API 拉取模型**：BaseURL + API Key 获取上游目录；可搜索、勾选后再保存，不会默认把全部模型写入。保存默认写入 **1M 上下文、只开思考、不含图片**；可在能力条改上下文或补图片后再「一键应用到已选」。
- **API Keys**：同一 Provider 可添加多把 Key，查看可用/失效状态，删除 Key，或恢复选中/全部失效 Key。
- 请求遇到 HTTP 401/403/429 或明确的鉴权、限流、额度错误时，会先在同一 Provider 内切换下一把可用 Key；HTTP 5xx、超时、DNS 和网络中断不会停用 Key。
- API Key 默认保存在 OS keyring；不可用时回退到当前用户专属的加密文件库。
- `models.json` 仅保存官方 Pi 可识别的 `${PI_MANAGER_PROVIDER_..._API_KEY}` 引用，启动 Pi 时才把真实密钥注入子进程环境。

### 快速提问
- 默认使用常驻 `pi --mode rpc` 会话：多轮上下文保留在会话内，故障切换模型时通过 `set_model` 在同一会话内热切、不丢上下文。
- 当前 Provider 的 Key 失败时先热切同接口其他 Key；所有 Key 均不可用后，才累计该模型失败次数并执行模型故障切换。
- 空闲一段时间后会自动回收 pi 进程（固定会话 ID 保证下次提问自动恢复上下文）；可在「设置 → 可靠性」关闭常驻会话回退到一次性模式。
- 输入框 `Ctrl+Enter` 直接发送。复杂改代码请用「启动完整 Pi」。
- **识图（默认启用）**：粘贴/拖入图片时，Pi skill 自动调用内置免费识图模型（智谱 GLM-4.6V-Flash 优先，限流自动切 GLM-4.1V-Thinking-Flash）把图片转为文字，再交给当前默认对话模型回答。
  - 识图模型在「设置 → 识图模型」中配置（Key + 模型选择），**只用于识图管道，不会自动出现在模型列表中**；如需在列表中使用智谱模型，请在 Provider 管理中手动添加。
  - 未配置智谱 Key 时，附加的图片会被跳过并给出提示。

### 会话
- 按路径/名称过滤。  
- 批量删除选中会话；重命名、继续、资源管理器打开。

### 健康监控
- 范围可选：**收藏 / 默认 / 自定义 Provider / 全部已加载模型 / 模型页选中**。  
- 显示状态、延迟、方式、错误摘要。  
- 可设定时巡检（分钟，0=关闭）。

### 测试历史
- 自动记录每次测试；可过滤、清空。

### 工具
- 本页用于环境自检、配置导入导出与版本检查，不会在这里直接执行模型工具。
- 自检清单、配置导入导出、密钥加密、版本检查；自检与更新检查进行中时可取消。
- 导入配置包后，恢复 / 跳过 / 风险说明显示在页头下方的结果面板，可关闭。

### 插件（内置 skills / extensions 一键安装）
随包分发，安装后落盘到 `~/.pi/agent/`（skills / extensions 子目录）；插件页可查看、启用/禁用、导入本地插件与回滚版本。用户插件只做静态校验、不执行插件代码；未信任的插件不会被启用。

- **识图 skill**（默认）：图片自动转文字再交对话模型回答，详见「快速提问 → 识图」。
- **提交规范 skill**（默认）：按项目规范（@fix:/@feat:/@refactor: 前缀 + 中文要点）生成 commit message。
- **文档处理 skill**（默认）：提取 PDF / Word / Excel / PowerPoint 文字与表格（PDF 需一次性 `pip install pdfplumber`，部分文件回退到 `pypdf`）。
- **敏感凭据防泄漏 extension**（默认）：拦截对 ~/.pi/agent 密钥/配置文件与项目 .env/*.pem 的读取与修改，输出侧抹除 sk-/ghp_ 等密钥模式。
- **Git 自动检查点 extension**（默认）：每轮自动 `git stash create`，`/git-checkpoints` 查看、`/git-checkpoint-restore N` 回滚。
- **状态注入 extension**（默认）：每轮把默认模型、收藏模型、健康巡检结果注入 pi 系统提示。
- **MCP 桥 extension**（默认关闭）：连接 ~/.pi/agent/mcp-servers.json 声明的 MCP server（需 npm install），提供 /mcp-status、/mcp-reload。MCP server 子进程只继承白名单基础环境（PATH/HOME 等）、不自动继承 provider API Key；如需给某个 server 传密钥，必须在 mcp-servers.json 该 server 的 env 中显式写出。

### 设置
- 默认模型、语言（中文优先）、CLI/界面主题。  
- 全局代理、批量测试并发、托盘行为、密钥加密。

---

## 三、推荐日常流程

```text
添加/更新 Provider → 刷新模型列表 → 批量测试关键模型
→ 收藏常用 → 设默认 → 拖入项目目录启动 Pi
→ （可选）健康监控定时巡检
```

### 批量操作一览
- 模型：多选 → 批量收藏 / 批量测试 / 测过滤结果 / 测全部  
- 健康：按范围批量巡检  
- 会话：多选批量删除  
- 历史：一键清空  
- 配置：导出/导入整包 ZIP  
- 密钥：一键加密现有明文 Key  

---

## 四、常见问题（FAQ）

### A. 安装与启动

**Q1：单文件 EXE 报 `QSpinBox is not defined`？**
A：旧包缺陷，请下载最新版本的 Windows 便携单文件包（`PiManager.exe`）。Windows 已不再提供目录版。

**Q2：PowerShell 启动 pi 报「意外的标记」？**  
A：路径含 `@scope` 时需 `&` 调用。请用 Pi Manager「启动 Pi」，或 Windows Terminal / cmd。

**Q3：关闭窗口后程序还在？**  
A：默认最小化到托盘。托盘右键 → 退出。可在设置关闭「关闭窗口时最小化到托盘」。

### B. Provider / API Key / 拉取模型

**Q4：切换 Provider 后为什么仍报 401 Invalid API key / Missing bearer？**  
A：旧版会把 `__DPAPI__:名称` 写进 `models.json`，官方 Pi 不认识该私有占位符，会把它当成真实 API Key 发送。当前版本会自动迁移为 `${PI_MANAGER_PROVIDER_..._API_KEY}` 并在启动 Pi 时注入真实密钥。升级后请打开一次 Provider 页面并重新保存；若提示安全密钥丢失，请重新填写真实 Key。外部环境变量必须显式写为 `$OPENAI_API_KEY` 或 `${OPENAI_API_KEY}`。

**Q5：SSL UNEXPECTED_EOF？**  
A：网络/防火墙/直连不稳定。可：
1. 设置全局代理（如 `http://127.0.0.1:7890`）  
2. 改用可访问的中转 Base URL  
3. 拉取对话框勾选忽略 SSL（仅排查）

**Q6：Key 存在 models.json 安全吗？**
A：真实 Key 不写入 `models.json`。它优先存入系统 keyring，回退时写入 `secrets.vault` 加密库；`models.json` 中只有 `${PI_MANAGER_PROVIDER_..._API_KEY}` 引用。导出密钥时必须设置至少 10 位密码，密钥包使用 PBKDF2-HMAC-SHA256 + AES-256-GCM。

**Q7：一把 Key 限流或额度用完后怎么处理？**
A：为同一 Provider 添加多把 Key 后，快速提问、HTTP 模型测试、模型列表拉取和 Cursor `pi.askPrompt` 会自动尝试下一把可用 Key。失败 Key 会保留在失效池；在 **Provider → API Keys** 中可恢复单个或全部 Key。只有明确的 Key 错误会进入失效池，服务端 5xx 或网络故障不会误停用。遇到 HTTP 503 / `upstream_overloaded`（Grokified 把 `/v1/models` 转给 xAI 时常见）会自动重试几次；仍失败说明上游暂时过载，不是 Key 填错——可先保存已手填或模板中的模型再对话。

### C. 测试与健康检查

**Q8：手动测试 免费grok/grok-4.5 可用，健康检查 0/3 全挂？**
A：健康检查默认测的是 **收藏列表**。若收藏是 `openai-codex/...`（需登录/OAuth），会不可用。
解决：
1. 把可用的 `免费grok/grok-4.5` 加入收藏
2. 健康范围选「默认模型」或「自定义 Provider」
3. 查看健康表错误列 / 测试历史

**Q9：延迟多少算正常？**
A：中转常见 1–5 秒；>10 秒检查网络/代理/服务商。

**Q10：测试会花钱吗？**
A：会发极短 prompt，通常费用极低；免费额度以服务商为准。

### D. 启动 Pi 与工作目录

**Q11：如何用默认模型在某项目打开 Pi？**
A：把文件夹拖到「概览」首页；或填工作目录后点启动。

**Q12：Pi 里如何尽量用中文？**
A：设置「默认语言」= 简体中文。会写入 `AGENTS.md` 并附加 system prompt。

### E. 配置迁移

**Q13：换电脑怎么迁移？**
A：工具页 **导出配置包**（需要 Key 再勾选含密钥）→ 新机器 **导入配置包**。

**Q14：配置文件在哪？**
A：`%USERPROFILE%\.pi\agent\`
含 `settings.json`、`models.json`、`pi-manager.json`、`secrets.vault` 等，完整清单见下方「路径速查」。系统 keyring 可用时，真实密钥由操作系统管理；旧 `secrets.dpapi` 只用于兼容迁移。

### F. Cursor / 其它

**Q15：Cursor 里怎么用 Pi？**
A：若已装本地扩展 `pi-manager.pi-cursor`，命令面板搜 Pi；否则用本 GUI 启动终端 Pi。

**Q16：昼夜模式会同步到 Pi CLI 吗？**
A：会。全局白天模式会让管理器、弹窗、帮助页与 Pi CLI 同步使用浅色；全局夜间模式会同步使用深色。Pi CLI 不再单独配置主题。

**Q17：Provider 里的「兼容选项」是什么？**
A：写进 `models.json` 的 `compat` 字段，告诉官方 Pi 这个接口支持哪些能力：  
- **支持 Developer 角色**：能否用 `developer` 角色消息（部分 OpenAI 兼容中转支持；不确定就关掉）。  
- **支持推理强度（Reasoning Effort）**：能否调节 thinking/reasoning 强度（支持就勾选，不支持关掉以免请求报错）。


### G. 升级到本版本（破坏性变更）

> 本版本为安全专项修复，以下四条**改变了既有行为**。只有升级后遇到对应现象时才需要看。

**Q18：升级后 Cursor 扩展突然取不到密钥，报「缺少 broker token」？**
A：`--print-provider-env` 以前零认证就能吐出明文 API Key，而只写白名单字段的
`--config-mutate` 反而要 token——授权模型是倒置的。现在两者共用同一套 broker token
校验：调用方必须按值出示 `~/.pi/agent/.broker-token` 的内容（`--token <值>` 或
`--token-file <文件>`）。**旧版 VSIX 不会带 token，因此会取不到密钥**。
解决：升级 Cursor 扩展到与本版本配套的 VSIX（Release 中的 `pi-manager-pi-cursor-*.vsix`，当前为 **0.7.5**）。
扩展会自己读取并出示 token，`pi.providerEnvCommand` 的写法不用改。
手工调用 helper 时也必须自己带上 token。

**Q19：升级后提示密钥库无法读取 / 需要重新填写 API Key？**
A：取决于你的 `secrets.vault` 是哪种旧格式：
- **`local:` 格式已永久移除，无迁移开关**。它用硬编码在程序内的固定密钥，任何本地
  写入者都能构造合法密文注入凭据，且注入内容会被自动重写成 DPAPI 格式「洗白」。
  请在 **Provider 管理 → API Keys** 重新填入密钥。
- **明文 JSON 格式默认拒绝读取**。临时设环境变量 `PI_MANAGER_ALLOW_LEGACY_VAULT=1`
  启动一次即完成迁移（迁移后立刻重写为认证加密格式），随后请取消该变量。
- 旧式「裸环境变量名」形式的 apiKey，现在只在该变量当前确实存在时才自动迁移；
  外部环境变量请显式写成 `$NAME` 或 `${NAME}`。无法确认是变量名的一律当真实密钥
  存入安全存储（此前的启发式会把 `AKIAIOSFODNN7EXAMPLE` 这类真实密钥当成变量名，
  明文写进 `models.json` 且界面上不打码）。

**Q20：启动 Pi 报「Provider 名称含非法字符，已拒绝启动 Pi」？**
A：Provider / Model / Thinking 三个字段现在有字符白名单：Provider 只允许字母、数字与
`. _ - : @ +`（≤64 字符）；Model 在此基础上再允许 `/`（≤128 字符）；Thinking 只允许
字母、数字与 `_ -`。这些字段是恶意配置包唯一能控制的命令行内容，含 `"` `&` `|` `%`
等字符时此前可经 Windows `cmd.exe` shim 注入任意命令（校验全平台生效，**Cursor 扩展启动 Pi 使用同一套白名单**）。请把
Provider / 模型改名为只含上述字符，再重新保存。

**Q21：便携版旁边放的 `assets/` 目录不再生效了？**
A：这是有意移除的。冻结态现在**只信任打包进产物的内嵌资源**（`sys._MEIPASS`），
不再回退 exe 同级目录——此前 exe 旁放一个 `assets/builtin/manifest.json` 就能让应用把
攻击者的内置扩展（拥有当前用户完整权限）落盘到 `~/.pi/agent/extensions/`。
自定义内置资源请从源码重新打包。

**Q22：Cursor 里调用模型弹出 `Security validation failure: parent process has different executable`？**
A：这是 Windows 单文件版 `PiManager.exe` 的 PyInstaller 启动器在拦 helper，不是模型或 Key 坏了。从 Pi Manager 弹出的终端再开 Cursor 时，`_PYI_*` 会进编辑器环境；扩展再拉起同一份 exe 取密钥，启动器以为这是 GUI 的子进程，核对父进程却是 Cursor，于是直接弹英文 Error。请安装配套扩展 **0.7.5**（启动 helper 时会重置冻结环境），并从开始菜单单独打开 Cursor；不要从 Pi Manager 进程树里启动编辑器。


---

## 五、路径速查

全部位于配置根 `%USERPROFILE%\.pi\agent\`（macOS / Linux 为 `~/.pi/agent/`）。

| 项目 | 路径 | 迁移备份 |
|------|------|------|
| 默认模型 / enabledModels | `settings.json` | 建议 |
| 自定义 Provider 与模型 | `models.json` | 建议 |
| GUI 偏好 | `pi-manager.json` | 建议 |
| 官方 Pi 登录凭据 | `auth.json` | 建议 |
| 加密密钥回退库 | `secrets.vault`（旧 `secrets.dpapi` 仅用于迁移） | 用导出配置包，勿直接拷 |
| 密钥索引 / vault 盐 | `secrets.index.json`、`.vault_master_key` | **勿拷贝**（与 vault 同等敏感） |
| 插件注册表 | `pi-plugins.json` | 建议 |
| 已安装插件副本 | `pimanager/plugins/<id>/<version>/` | 可重装，无需备份 |
| 内置 skills / extensions | `skills/`、`extensions/` | 可重装，无需备份 |
| MCP server 声明 | `mcp-servers.json` | 建议 |
| CLI 主题 | `themes/` | 可选 |
| 测试历史 | `pi-manager-test-history.json` | 可选 |
| 健康状态 | `pi-manager-health.json` | 可选 |
| helper 注册信息（Cursor 扩展自动发现） | `pi-manager-helper.json` | 自动重建 |
| 配置 Broker token / 修订记录 | `.broker-token`、`.config-revisions.json` | **勿拷贝**（凭据等价物） |

推荐用「工具 → 导出配置包」迁移，而不是手工拷贝目录：导出包会正确加密密钥，手工拷贝会连带把盐文件与 token 一起带走。

---

## 六、故障排查清单

1. 自检是否全绿？
2. 默认模型是否为已验证可用模型？
3. 自定义 Provider 的 BaseURL / Key 是否正确？
4. 需要代理时是否启用全局代理？
5. 健康检查范围是否包含「会失败的 OAuth 模型」？
6. 测试历史里的错误摘要是什么？
7. 是否用最新 EXE？先退出托盘再启动。
8. Cursor 弹 `parent process has different executable`？用配套扩展，并单独启动 Cursor（见 Q22）。

---

*完整编码/agent 能力请始终通过「启动 Pi」使用官方交互会话。*
'''

# 版本号单一来源：pi_manager/extras.py 的 APP_VERSION。
HELP_MARKDOWN = _HELP_MARKDOWN.replace("__APP_VERSION__", APP_VERSION)


def _help_theme_colors(mode: str = "night") -> dict[str, str]:
    """HTML 内联色：随昼夜模式切换，保证 QTextBrowser 可读。"""
    m = (mode or "night").lower().strip()
    if m in {"day", "light", "白天"}:
        return {
            "text": "#1f2937",
            "muted": "#4b5563",
            "title": "#111827",
            "heading": "#1d4ed8",
            "border": "#d1d5db",
            "code_bg": "#f3f4f6",
            "code_fg": "#1d4ed8",
            "pre_bg": "#f8fafc",
            "pre_fg": "#1f2937",
            "pre_border": "#e5e7eb",
            "quote_bg": "#eff6ff",
            "quote_fg": "#1e3a5f",
            "quote_border": "#3b82f6",
            "th_bg": "#eef2ff",
            "th_fg": "#1e40af",
            "td_fg": "#1f2937",
            "link": "#2563eb",
            "hr": "#d1d5db",
        }
    return {
        "text": "#e8eef7",
        "muted": "#c5d0e0",
        "title": "#f4f7fb",
        "heading": "#93c5fd",
        "border": "#2a3545",
        "code_bg": "#1a222d",
        "code_fg": "#93c5fd",
        "pre_bg": "#0f141b",
        "pre_fg": "#d4d4d4",
        "pre_border": "#243041",
        "quote_bg": "#132033",
        "quote_fg": "#c5d0e0",
        "quote_border": "#3b82f6",
        "th_bg": "#161d27",
        "th_fg": "#93c5fd",
        "td_fg": "#e8eef7",
        "link": "#60a5fa",
        "hr": "#243041",
    }


def markdown_to_html(md: str, mode: str = "night") -> str:
    """Lightweight Markdown -> HTML for QTextBrowser (no external deps)."""
    c = _help_theme_colors(mode)
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_code = False
    in_ul = False
    in_table = False
    table_rows: list[list[str]] = []

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def flush_table():
        nonlocal in_table, table_rows
        if not table_rows:
            return
        out.append(
            '<table border="0" cellspacing="0" cellpadding="6" '
            'style="border-collapse:collapse;margin:10px 0;width:100%;">'
        )
        for i, row in enumerate(table_rows):
            # skip separator row
            if all(_RE_SEP.fullmatch(c_cell.strip() or "") for c_cell in row):
                continue
            tag = "th" if i == 0 else "td"
            style = f"border:1px solid {c['border']};padding:8px 12px;color:{c['td_fg']};"
            if i == 0:
                style += f"background:{c['th_bg']};font-weight:700;color:{c['th_fg']};"
            else:
                style += "background:transparent;"
            cells = "".join(f'<{tag} style="{style}">{_inline(cell)}</{tag}>' for cell in row)
            out.append(f"<tr>{cells}</tr>")
        out.append("</table>")
        table_rows = []
        in_table = False

    def _inline(text: str) -> str:
        t = html.escape(text)
        code_open = (
            f"<code style='background:{c['code_bg']};color:{c['code_fg']};padding:2px 6px;"
            f"border-radius:6px;font-family:Consolas,monospace;'>"
        )
        t = _RE_CODE.sub(lambda m: f"{code_open}{m.group(1)}</code>", t)
        t = _RE_BOLD.sub(r"<b>\1</b>", t)
        t = _RE_ITALIC.sub(r"<i>\1</i>", t)
        link_style = f'color:{c["link"]};text-decoration:none;'
        t = _RE_LINK.sub(
            lambda m: f'<a href="{m.group(2)}" style="{link_style}">{m.group(1)}</a>',
            t,
        )
        return t

    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            close_ul()
            flush_table()
            if not in_code:
                in_code = True
                out.append(
                    f"<pre style=\"background:{c['pre_bg']};color:{c['pre_fg']};padding:12px 14px;"
                    f"border-radius:10px;overflow:auto;border:1px solid {c['pre_border']};"
                    f"font-family:Consolas,monospace;font-size:12.5px;line-height:1.45;\">"
                )
            else:
                in_code = False
                out.append("</pre>")
            continue
        if in_code:
            out.append(html.escape(raw))
            continue

        if "|" in line and line.strip().startswith("|"):
            close_ul()
            parts = [p.strip() for p in line.strip().strip("|").split("|")]
            table_rows.append(parts)
            in_table = True
            continue
        else:
            if in_table:
                flush_table()

        if not line.strip():
            close_ul()
            out.append("<br/>")
            continue
        if line.startswith("### "):
            close_ul()
            out.append(
                f"<h3 style='margin:16px 0 8px;color:{c['heading']};font-size:14px;'>{_inline(line[4:])}</h3>"
            )
        elif line.startswith("## "):
            close_ul()
            out.append(
                f"<h2 style='margin:20px 0 10px;border-bottom:1px solid {c['border']};"
                f"padding-bottom:6px;color:{c['title']};font-size:16px;'>{_inline(line[3:])}</h2>"
            )
        elif line.startswith("# "):
            close_ul()
            out.append(
                f"<h1 style='margin:8px 0 14px;color:{c['title']};font-size:20px;'>{_inline(line[2:])}</h1>"
            )
        elif line.startswith("> "):
            close_ul()
            out.append(
                f"<blockquote style='margin:10px 0;padding:10px 14px;border-left:4px solid {c['quote_border']};"
                f"background:{c['quote_bg']};color:{c['quote_fg']};border-radius:0 8px 8px 0;'>{_inline(line[2:])}</blockquote>"
            )
        elif line.startswith("---"):
            close_ul()
            out.append(f"<hr style='border:none;border-top:1px solid {c['hr']};margin:18px 0;'/>")
        elif _RE_UL.match(line):
            if not in_ul:
                out.append(f"<ul style='margin:8px 0 8px 1.2em;color:{c['text']};'>")
                in_ul = True
            out.append(f"<li style='margin:4px 0;line-height:1.5;'>{_inline(line[2:])}</li>")
        elif _RE_OL.match(line):
            close_ul()
            out.append(f"<p style='margin:4px 0 4px 0.5em;color:{c['text']};'>{_inline(line)}</p>")
        else:
            close_ul()
            out.append(f"<p style='margin:8px 0;line-height:1.6;color:{c['text']};'>{_inline(line)}</p>")

    close_ul()
    flush_table()
    if in_code:
        out.append("</pre>")

    body = "\n".join(out)
    return (
        "<html><head><meta charset='utf-8'></head>"
        "<body style=\"font-family:'Segoe UI','Microsoft YaHei UI','PingFang SC',sans-serif;"
        f"font-size:13px;color:{c['text']};background:transparent;padding:10px 14px;line-height:1.55;\">"
        f"{body}</body></html>"
    )


def help_sections() -> list[tuple[str, str]]:
    """Split HELP_MARKDOWN into tab-friendly sections by top-level ## headers."""
    lines = HELP_MARKDOWN.strip().splitlines()
    intro: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current: list[str] = []

    def short_title(raw: str) -> str:
        mapping = [
            ("快速上手", "快速上手"),
            ("界面布局", "界面布局"),
            ("功能分类", "功能说明"),
            ("推荐日常", "日常流程"),
            ("常见问题", "常见问题"),
            ("路径速查", "路径速查"),
            ("故障排查", "故障排查"),
        ]
        for key, short in mapping:
            if key in raw:
                return short
        return raw

    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            # skip document H1
            continue
        if line.startswith("## ") and not line.startswith("### "):
            if current_title is None:
                # flush intro as 概览 if has content
                intro_text = "\n".join(intro).strip()
                if intro_text:
                    sections.append(("概览", intro[:]))
                intro = []
            else:
                sections.append((current_title, current))
            current_title = short_title(line[3:].strip())
            current = [line]
        else:
            if current_title is None:
                intro.append(line)
            else:
                current.append(line)
    if current_title is not None:
        sections.append((current_title, current))
    elif intro:
        sections.append(("概览", intro))

    out: list[tuple[str, str]] = []
    for title, body_lines in sections:
        text = "\n".join(body_lines).strip()
        if text:
            out.append((title, text + "\n"))
    if not out:
        out = [("全部", HELP_MARKDOWN)]
    return out


def help_section_html(section_md: str, mode: str = "night") -> str:
    return markdown_to_html(section_md, mode=mode)


def help_html(mode: str = "night") -> str:
    return markdown_to_html(HELP_MARKDOWN, mode=mode)
