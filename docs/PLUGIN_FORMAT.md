# PiManager 用户插件规范

本文定义 PiManager 用户插件的目录、`package.json` 和生命周期约定。

> 状态说明：当前源码中的 `pi_manager.builtin_plugins` 只管理随 PiManager 分发的内置插件；`pi_manager.plugin_manager` 已实现本地目录/ZIP 用户插件导入、静态校验、启用/禁用、信任、卸载和回滚。本版本不包含远程 npm/Git 发现、下载或更新。

## 1. 插件包结构

插件包根目录必须直接包含 `package.json`。推荐结构如下：

```text
my-plugin/
├── package.json
├── README.md
├── skills/
│   └── code-review/
│       └── SKILL.md
├── extensions/
│   └── index.ts
└── package-lock.json       # 有运行时依赖时推荐提交
```

`pi` 描述 Pi 要加载的资源，路径都相对于插件根目录；`piManager` 描述 PiManager 用于校验、兼容性检查和风险确认的元数据。两者不要重复维护同一资源路径。

用户插件的安装副本位于（已实现并强制，见 `plugin_manager._relative_install_root()` /
`_validate_install_root_record()`；注册表记录被篡改成其他布局时会被拒绝）：

```text
~/.pi/agent/pimanager/plugins/<id>/<version>/
```

注册表和活动版本由 `plugin_manager` 管理。当前内置插件仍按 `assets/builtin/manifest.json` 的规则直接落到 `~/.pi/agent/`，不要把两种格式混用。

### `package.json` 最小要求

- `name`、`version`、`description` 必须是字符串；`version` 使用 SemVer 2.0。
- `pi` 至少声明一个实际资源入口：`skills`、`extensions`、`prompts` 或 `themes`。
- `piManager.schemaVersion` 必须存在且等于当前规范版本 `1`。
- `piManager.id` 是安装、启停和回滚使用的稳定 ID，不随显示名称变化。
- 包内所有入口必须解析到插件根目录之内的普通文件或目录。

## 2. 字段和校验约束

### ID、版本和元数据

- `id` 必须全局唯一，长度必须为 1–64；只允许小写 ASCII 字母、数字、`.`、`_` 和 `-`，首尾必须是字母或数字，禁止路径分隔符、盘符和控制字符。可用正则：`^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$`。
- `package.json.version`、注册表版本和活动版本必须一致。同一 `id/version` 的内容哈希变化时不得静默覆盖，应要求重新导入或明确替换。
- `displayName` 用于界面展示；`description` 应说明用途、限制和需要的权限，不得包含 API Key、Cookie 或其他秘密。
- `compatibility.pi`、`compatibility.node` 使用版本范围；`platforms` 只能填写 `win32`、`darwin`、`linux`。

### `pi` 资源入口

```json
{
  "skills": ["./skills"],
  "extensions": ["./extensions/index.ts"],
  "prompts": ["./prompts"],
  "themes": ["./themes"]
}
```

约束如下：

- 每个路径必须是插件根目录内的相对路径；拒绝绝对路径、`..`、符号链接、硬链接和 Windows 重解析点。
- 路径按大小写不敏感规则检查冲突，避免 Windows 与其他平台表现不同。
- `extensions` 使用明确的 `.ts` 或 `.js` 入口；推荐写具体文件而不是依赖目录扫描。
- 包内不得包含 `node_modules`；依赖只写在 `dependencies` / `devDependencies`，当前导入流程不自动执行依赖安装。
- 校验和导入阶段只读取文件，不加载或执行 Extension、脚本和安装钩子。

### `SKILL.md`

每个 Skill 目录必须有 UTF-8 编码的 `SKILL.md`，并包含至少以下 frontmatter：

```markdown
---
name: code-review
description: 审查代码变更并给出可执行的修复建议。
---

# Code review

这里写给模型的使用说明、输入约束和输出格式。
```

`name` 应与 Skill 目录名一致或能明确对应；`description` 不得为空。文档正文可以指导模型调用工具，因此 Skill 也必须经过信任审查，不能因为它不是 Extension 就视为安全。

### 依赖和安装脚本

- Node 运行时依赖放在标准 `dependencies`；开发依赖放在 `devDependencies`，不要把依赖写进自定义字段。
- 有依赖时应提交锁文件并固定可复现版本；不要把 `node_modules` 作为普通插件内容导入。
- 禁止 `preinstall`、`install`、`postinstall`、`prepare` 等生命周期脚本。校验阶段不执行 `package.json.scripts`。
- 当前用户插件范围不包含远程依赖解析或 npm/Git 下载。`dependencies` 只是包声明；依赖安装由后续受控流程或开发者手工完成，不能在插件中嵌入自动安装命令。

### 权限声明

`piManager.permissions` 只写最小必要需求，值为可审查的名称，不写秘密内容：

- `network`：主机名，例如 `api.github.com`，不得包含 Token 或 URL 中的凭据。
- `filesystem`：例如 `workspace-read`、`workspace-write`。
- `process`：需要调用的可执行程序，例如 `git`。
- `secrets`：需要的环境变量名，例如 `GITHUB_TOKEN`，不填写真实值。

权限声明用于界面展示、用户确认和审计记录，**不是沙箱**，也不会限制 Node API、文件系统、环境变量、网络或子进程。真正隔离需要独立进程、操作系统权限控制或容器；本格式本身不提供沙箱。

## 3. 公共 API 和生命周期

`pi_manager.plugin_manager` 提供面向资源路径和稳定 ID 的高层 API；调用者不应直接编辑注册表、`settings.json` 或安装目录：

```python
from pathlib import Path
from pi_manager import plugin_manager

# 所有函数返回字典；失败时返回 {"ok": False, "error": ..., "errors": [...]}
plugin_manager.inspect_plugin("my-plugin")
plugin_manager.import_plugin("my-plugin", enable=False, trust=False)
plugin_manager.list_plugins()
plugin_manager.set_plugin_trust("acme-code-review", True, enable=True)
plugin_manager.set_plugin_enabled("acme-code-review", False)
plugin_manager.remove_plugin("acme-code-review")
plugin_manager.rollback_plugin("acme-code-review", version=None)
plugin_manager.plugin_registry_path()
plugin_manager.self_check()
```

为兼容常见管理器命名，也提供 `validate`、`install`、`status`、`enable`、`disable`、`uninstall` 和 `rollback` 别名；它们不会绕过同一套校验和事务逻辑。

`inspect_plugin` 只读校验本地目录或 ZIP，不写盘、不执行插件代码；`import_plugin` 会把版本安装到 `~/.pi/agent/pimanager/plugins/<id>/<version>/`，默认禁用；`set_plugin_trust` 记录用户明确的信任决定，`set_plugin_enabled` 切换活动版本的加载状态；`remove_plugin` 删除管理器拥有的插件版本；`rollback_plugin` 在已保留版本间切换。返回记录至少包含 `id`、`version`、`source`、`status`、`enabled`、内容哈希和错误信息。注册表及 Pi 配置使用项目现有 `storage.locked` 和原子写入机制；真实密钥不得进入注册表、日志或未加密导出包。

Pi 的 `settings.json.packages` 投影只使用标准的 `source`、`skills`、`extensions`、`prompts`、`themes` 字段：未信任或禁用时四类资源都写为空数组；启用且信任时省略过滤键，让 Pi 按插件清单加载资源。

建议状态流转为：

```text
staged → validated → pending-trust → installed-disabled → enabled
                                      ↘ broken / rollback
                                      ↘ removed
```

### 本地目录导入

1. 将目录复制到 PiManager 管理的暂存区，不修改用户原目录。
2. 校验 `package.json`、SemVer、ID、入口路径、`SKILL.md`、依赖、权限和兼容性。
3. 计算包内容 SHA-256，检查同 ID、版本和资源冲突。
4. 通过用户确认后，将整个版本原子安装到 `<id>/<version>`；默认状态为 `disabled`，请求启用但尚未信任时为 `pending-trust`。
5. 只有用户明确启用且通过信任确认后，才更新活动版本；通常在下次 Pi 启动时生效。

### ZIP 导入

ZIP 解压前必须拒绝 Zip Slip、绝对路径、盘符路径、符号链接/硬链接、Windows 重解析点、大小写冲突、超大成员、超大总大小和压缩炸弹。解压后的插件根目录必须能直接找到 `package.json`；不得执行其中任何代码或安装钩子。

### 启用、禁用、卸载和回滚

- **启用**：只切换已校验且已信任的版本；任何资源都不能因导入而自动启用。
- **禁用**：保留已安装版本和注册信息，取消活动映射；运行中的 Pi 通常在下次启动后生效。
- **卸载**：先禁用，再只删除插件管理器拥有的版本目录和注册信息；不得删除插件目录之外的文件。运行中的 Extension 不强行热卸载，可标记为下次启动清理。
- **回滚**：保留至少一个可用旧版本，先校验并原子切换活动版本，切换失败时继续使用旧版本。升级失败不得先删除旧版本，也不能把“强制重装”当作回滚。

当前不支持通过 URL、npm 包名或 Git 仓库导入、发现、更新插件；这些属于未来功能，届时必须另行定义固定版本、哈希和信任策略。

## 4. 完整示例

下面是一个同时包含 Skill 和 Extension 的合法 `package.json` 示例；对应的 `skills/`、`extensions/`、`prompts/` 和 `themes/` 路径必须真实存在。

```json
{
  "name": "@acme/pi-code-review",
  "version": "1.0.0",
  "description": "代码审查辅助插件：提供审查 Skill 和 Git 变更摘要 Extension。",
  "license": "MIT",
  "dependencies": {},
  "pi": {
    "skills": ["./skills"],
    "extensions": ["./extensions/index.ts"],
    "prompts": ["./prompts"],
    "themes": ["./themes"]
  },
  "piManager": {
    "schemaVersion": 1,
    "id": "acme-code-review",
    "displayName": "代码审查",
    "compatibility": {
      "pi": ">=1.8.2",
      "node": ">=20.0.0",
      "platforms": ["win32", "darwin", "linux"]
    },
    "permissions": {
      "network": ["api.github.com"],
      "filesystem": ["workspace-read"],
      "process": ["git"],
      "secrets": []
    }
  }
}
```

## 5. 开发者自测

在插件根目录先执行：

```bash
python -m json.tool package.json
```

实现或修改 `plugin_manager` 后，至少验证以下场景：

1. 用 `plugin_manager.inspect_plugin(".")` 检查合法包，以及非法 JSON、重复 ID、非法 SemVer、缺失 frontmatter、越界路径和未知入口。
2. 分别导入本地目录和 ZIP；测试 Zip Slip、符号链接/硬链接、大小限制和大小写冲突。
3. 确认导入默认为禁用，启用/禁用/卸载/回滚状态正确，升级失败仍保留旧版本。
4. 确认校验阶段不会执行 `.ts`、`.js`、Shell 命令或 npm 生命周期脚本；依赖失败不会破坏已启用版本。
5. 执行项目基线检查：

   ```bash
   python -m pytest tests -q
   python main.py --self-check
   ```

测试中还应确认注册表和日志不包含真实 API Key，配置写入仍经过 `storage.locked` 与原子替换。
