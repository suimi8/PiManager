<!--
本模板承载 docs/DEVELOPMENT_STANDARDS.md §4.3 的 PR 三要素要求。
规范唯一权威正文：docs/DEVELOPMENT_STANDARDS.md（冲突时以它为准）。
-->

## 动机

<!-- 为什么要改？关联的 Issue / 审查结论 / 用户反馈。 -->

## 改动点

<!-- 改了哪些文件、哪些行为。API / 配置格式 / 命令行参数有变化时请显式列出。 -->

## 验证方式

<!-- 实际跑过的命令与结果（贴关键输出）。 -->

- [ ] `ruff check .`
- [ ] `python -m pytest tests -q`（integration 用例默认排除）
- [ ] `python scripts/check_secrets.py --scan-tests`
- [ ] `python scripts/check_versions.py`
- [ ] `python main.py --self-check`

## 影响面自查

- [ ] 未提交任何真实 API Key、`secrets.vault`、`.vault_master_key`、本机配置或构建产物
- [ ] 新增/变更行为附带了测试（`tests/`）
- [ ] 测试没有写开发者真实 `~/.pi/agent/`（写用户配置的用例已声明 `isolated_home`）
- [ ] 用户可见文案为中文；代码标识符 / 命令 / 路径未翻译
- [ ] 改了使用教程内容的话，改的是 `pi_manager/help_docs.py` 并已运行
      `python scripts/check_versions.py --write` 同步 `docs/使用教程.md`

<!-- 涉及密钥 / 导入导出 / 启动 Pi 路径的 PR，请在「验证方式」中附回归结果。 -->
