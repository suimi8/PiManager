# 贡献指南

感谢关注 PiManager！欢迎 Issue、讨论与 Pull Request。

**开发规范唯一权威正文：`docs/DEVELOPMENT_STANDARDS.md`。** 本文件只提供入口导航；
规范冲突时以 `docs/DEVELOPMENT_STANDARDS.md` 为准。

## 先读什么

| 场景 | 文档 |
|------|------|
| 开发规范（红线、代码风格、提交规范、PR 流程、质量门禁） | `docs/DEVELOPMENT_STANDARDS.md` |
| 机器可读的不可破坏边界与检测不变量 | `AGENTS.md`（AI 代理会自动注入） |
| 构建 / 打包 / 发布 | `BUILD.md` |
| 安全策略与漏洞报告 | `SECURITY.md` |
| 插件开发者规范 | `docs/PLUGIN_FORMAT.md` |
| 用户操作说明 | `docs/使用教程.md` |

## 开发环境

```bash
git clone https://github.com/suimi8/PiManager.git
cd PiManager
python -m pip install -r requirements.txt
python -m pip install pytest pytest-cov ruff
python main.py
python -m pytest tests -q
```

需要本机已安装 Node.js，以及：

```bash
npm install -g @earendil-works/pi-coding-agent
```

## 提交前检查

1. `ruff check .` 通过（无硬语法错误与未使用导入）
2. `python -m pytest tests -q` 通过（integration 用例默认排除，需要时 `-m integration`）
3. `python scripts/check_secrets.py --scan-tests` 无密钥/敏感文件泄漏
4. `python scripts/check_versions.py` 版本一致（改了版本号时）
5. 变更尽量附带或更新对应测试（`tests/`）
6. 提交信息遵循 `@<type>: <中文标题>` 前缀约定（见开发规范第 3 节）
7. 不要提交密钥、`secrets.vault`、本机配置或构建产物（`release-assets/`、`dist/`）

## Pull Request

1. Fork 本仓库并创建分支（如 `fix/xxx`、`feat/xxx`、`refactor/xxx`、`docs/xxx`、`chore/xxx`）
2. 描述包含：动机、改动点、验证方式
3. 若影响密钥/导入导出/启动 Pi 路径，请说明兼容性与回归结果
4. CI 全部 job 通过（test / lint / secret-scan / consistency / extension-test）

## 行为准则

请保持友善、就事论事。恶意提交、包含密钥的 PR、明显无关的垃圾内容会被关闭。
