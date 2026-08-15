# PiManager 开发规范（唯一权威正文）

> 本文档是仓库**唯一权威**的开发规范正文。`CONTRIBUTING.md` 是导航壳，`AGENTS.md`
> 只保留机器可读的不可破坏边界，`BUILD.md`/`SECURITY.md`/`docs/PLUGIN_FORMAT.md`
> 是专题手册。**冲突时以本文档为准**，并在本文档中登记每条红线由谁强制。
>
> 版本：1.0（随 1.8.3 发布）

---

## 0. 规范层级与入口

```text
CONTRIBUTING.md（新贡献者第一入口，导航壳）
  └── docs/DEVELOPMENT_STANDARDS.md（本文档，唯一权威正文）← 冲突时以这里为准
        ├── AGENTS.md（机器可读不可破坏边界 + 检测不变量）
        ├── BUILD.md（构建/发布操作手册）
        ├── SECURITY.md（威胁模型/漏洞报告）
        └── docs/PLUGIN_FORMAT.md（插件开发者规范）
```

- 新贡献者：先读 `CONTRIBUTING.md`，再按需读本文档对应章节。
- AI 编码代理（pi 等）：`AGENTS.md` 会被自动注入，原则性红线在其内；可自动化的
  检测项在本规范登记"由 X 强制"。
- 插件开发者：`docs/PLUGIN_FORMAT.md` + 本规范第 7 节。

---

## 1. 红线清单（强制，多数可脚本化）

| # | 红线 | 检测方式 | 强制者 |
|---|------|----------|--------|
| R1 | 仓库任何位置不得出现真实 API Key / 导出密码 / `secrets.vault` / `auth.json` / `*.pem` / `*.key`；`models.json` 只允许 `${PI_MANAGER_PROVIDER_<SLUG>_<HASH>_API_KEY}` 引用 | `scripts/check_secrets.py`（文件名黑名单 + 内容模式扫描） | CI `secret-scan` job；pre-commit 建议 |
| R2 | 用户配置目录固定 `~/.pi/agent/`（Windows `%USERPROFILE%\.pi\agent\`），不得改写 | 源码 grep + 现有测试 | `tests/test_plugin_standards.py` |
| R3 | keyring 优先、vault 回退；导出含密钥必须 PBKDF2 + AES-256-GCM；密钥绝不落日志/未加密导出 | 现有安全测试 | `tests/test_keyring_priority.py` 等（AGENTS.md 已列） |
| R4 | 轻量 CLI（`--print-provider-env` / `--vision-describe` / `--config-mutate`）不得 import PySide6 | AST 静态断言 | `tests/test_cli_dispatch.py` + `tests/test_plugin_standards.py` |
| R5 | 桌面版本单一来源 = `pi_manager/extras.py:APP_VERSION`；Cursor 扩展版本 = `extensions/pi-cursor/package.json`；`docs/发布说明.md` / `docs/使用教程.md` 顶部版本同步 | `scripts/check_versions.py` | CI `consistency` job + `tests/test_plugin_standards.py` |
| R6 | `pytest tests -q` 全绿；`main.py --self-check` 输出 OK；打包产物过 `smoke_test_dist.py`；Linux 用 xvfb | CI 现有 job | `ci.yml` test / self-check / nightly-packaging；`build.yml` |
| R7 | 发布产物（`release-assets/`、`dist/`、构建目录）不得入库；二进制走 GitHub Releases | `git ls-files` 白名单 + review | 人工 + `tests/test_plugin_standards.py` 抽查 |
| R8 | 用户可见文案中文；代码标识符/命令/路径不翻译 | 人工 review | PR 审查 |
| R9 | 插件包必须通过 `plugin_manager.inspect_plugin` 级校验（SemVer、ID、资源路径、frontmatter） | `tests/test_plugin_security_matrix.py` 等 | CI test job |

---

## 2. 代码风格

### 2.1 Python（ruff 强制）

配置见 `pyproject.toml`。CI `lint` job 执行 `ruff check .`（当前强制规则集：
`E4/E7/E9/F`——硬语法错误与未使用检测）。

- 行宽 ≤ 100（存量超长行属存量债务，见第 9 节，新代码必须遵守）。
- 类型注解：公共函数/方法必须标注；`from __future__ import annotations` 统一使用。
- docstring：**模块与公共 API 用中文**；类/函数第一行概括用途，复杂逻辑补充说明。
- 不再写入 `# -*- coding: utf-8 -*-`（Python 3 默认 UTF-8）。
- import 顺序：标准库 → 第三方 → 本地（`pi_manager`/`main`），组间空行（isort 风格）。
- `pi_manager/core.py` 是公共 API 汇聚模块，**禁止删除其中的 re-export**（曾被
  ruff 误删 `ProviderKeyError` 导致下游 import 失败；该文件 F401 已豁免，清理前
  必须先确认无下游引用）。

### 2.2 TypeScript（扩展侧）

`extensions/pi-cursor/` 遵循其自身 `package.json` 脚本；`npm test` 在 CI
`extension-test` job 强制。新增 extension 必须通过代码审查（extension 拥有
当前用户完整权限，等于 PiManager 替它背书）。

### 2.3 通用

- 不引入未论证的第三方依赖；新增依赖需在 PR 说明理由。
- 错误消息中文，且可被测试断言（关键词匹配）。
- 不做无谓的 `except Exception: pass`；必须给出注释或日志。

---

## 3. 提交规范

提交信息采用统一前缀 + 中文标题 + 要点列表：

```text
@<type>: <中文标题>

- 要点 1
- 要点 2
```

- `type ∈ {fix, feat, refactor, docs, chore}`。
- 标题 ≤ 50 字，概括改动意图；要点说明改了什么、为什么、影响面。
- 分支命名：`fix/xxx`、`feat/xxx`、`refactor/xxx`、`docs/xxx`、`chore/xxx`。
- 提交不得包含：密钥/凭据、本机配置、`secrets.vault`、构建产物、`.coverage` 等
  （R1/R7）。

> 与用户级 skill `commit-message` 的规则一致；仓库内以本规范为准。

---

## 4. 分支与 PR 流程

1. 从 `main` 创建分支，按第 3 节命名。
2. 开发完成后本地验证：`ruff check .` + `python -m pytest tests -q`
   （integration 用例默认排除，需要时显式 `-m integration`）。
3. PR 描述必须包含：动机、改动点、验证方式；涉及密钥/导入导出/启动 Pi 路径的
   PR 必须附回归结果。
4. 合并前由 reviewer（或本人自查）对照本规范红线清单逐项勾选。
5. 涉及多端行为一致（Cloudflare 版 / 桌面版 / 服务器版）时，显式移植并分别测试。

---

## 5. 测试与质量门禁

- 常规测试：`python -m pytest tests -q`（`pyproject.toml` 已配置默认排除
  `integration` 标记用例，避免本地/CI 行为不一致）。
- 集成测试：真实 Pi CLI / 外网用例必须标 `@pytest.mark.integration`，仅在有
  pi CLI 的环境显式运行：`pytest -m integration`。
- 覆盖率门槛：CI 当前 `--cov-fail-under=55`（包整体）；插件安全核心模块
  （`plugin_manager.py` / `builtin_plugins.py`）目标 ≥ 80%，见第 9 节存量债务。
- 新增功能必须附带测试；安全校验分支（ZIP 矩阵、frontmatter、self_check 错误
  分支）参照 `tests/test_plugin_security_matrix.py` 的模式补测。
- 文档-实现一致性：版本类断言在 `tests/test_plugin_standards.py`；插件格式类
  断言在 `tests/test_plugin_security_matrix.py`。

---

## 6. 版本与发布

- 桌面版本：只改 `pi_manager/extras.py:APP_VERSION`；发布时同步更新
  `docs/发布说明.md`、`docs/使用教程.md` 顶部版本（`scripts/check_versions.py`
  会在 CI 强制）。
- 扩展版本：只改 `extensions/pi-cursor/package.json:version`。
- 发布流程以 `BUILD.md` 与 `AGENTS.md` 为准：pytest → self-check → PyInstaller
  → `smoke_test_dist.py` → `package_release.py`。
- 发布产物（`release-assets/`、`dist/`）不入库，走 GitHub Releases。

---

## 7. 插件开发

- 规范正文：`docs/PLUGIN_FORMAT.md`。
- 最小 `package.json` 模板、资源入口、权限语义、生命周期、导入/回滚、自测命令
  见该文档；`assets/builtin/manifest.json` 是内置插件清单示例。
- 内置插件红线：新增内置 extension 必须经过代码审查并在 manifest 显式声明
  （extension 拥有完整系统权限）；`target_dir` 必须落在 `~/.pi/agent/` 内
  （`builtin_plugins._assert_safe_target_dir` 强制）。
- npm 依赖：内置扩展必须提交 `package-lock.json`；安装一律 `--ignore-scripts`
  （有 lockfile 时用 `npm ci`），不执行依赖包生命周期脚本。
- 用户插件安全边界由 `plugin_manager` 静态校验（不执行插件代码），测试见
  `tests/test_plugin_security_matrix.py`。

---

## 8. 自动化审查（CI 强制项）

CI（`.github/workflows/ci.yml`）在 push/PR 上强制以下 job：

| job | 内容 | 失败即阻断 |
|-----|------|:---:|
| `test` | 3 OS × 2 Python：pytest + 覆盖率 + self-check | 是 |
| `lint` | `ruff check .` | 是 |
| `secret-scan` | `python scripts/check_secrets.py --scan-tests` | 是 |
| `consistency` | `python scripts/check_versions.py` + 规范一致性测试 | 是 |
| `extension-test` | `extensions/pi-cursor` npm test | 是 |
| `nightly-packaging` | 每日 PyInstaller + smoke | 是（发布回归） |

本地建议：pre-commit 挂 `ruff check`、`check_secrets.py`、`check_versions.py`。

---

## 9. 存量债务与豁免（承认并登记，逐步收敛）

以下为历史遗留，**当前豁免不代表可以新增**；清理时在对应 PR 中更新本清单：

1. `E501`（行宽 >100 列）：存量多处（`core.py`、`core_sessions.py` 等），新代码
   必须 ≤100 列。
2. `pi_manager/core.py` 的 `F401`（re-export 无法被 ruff 识别）：豁免，清理需
   先确认无下游引用。
3. 格式类规则（`W`/`I`/`UP`/`B` 等）暂不在 CI 强制范围：存量存在大量尾随空白、
   import 未排序、`# -*- coding: utf-8 -*-` 冗余；建议后续一次性 `ruff format`
   收敛。
4. 插件安全核心模块覆盖率（`plugin_manager.py` 实测 75% / `builtin_plugins.py`
   83%，2026-08 基线）：目标 ≥80%，随 `test_plugin_security_matrix.py` 扩展提升。
5. `docs/review/` 系列审查报告是过程产物，可归档不入库或定期清理。

---

## 10. 审查与改进闭环

- 本规范每季度或重大变更时复审；子代理审查（`scripts/run_subagents.py`）产出的
  `docs/review/` 报告结论应回流到本规范或修复清单，避免"审查结论与规范脱节"。
- 新增不可自动化的红线时，必须同步登记到第 1 节表格并注明强制者。
