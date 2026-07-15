# 贡献指南

感谢关注 PiManager！欢迎 Issue、讨论与 Pull Request。

## 开发环境

```bash
git clone https://github.com/suimi8/PiManager.git
cd PiManager
python -m pip install -r requirements.txt
python -m pip install pytest
python main.py
python -m pytest tests -q
```

需要本机已安装 Node.js，以及：

```bash
npm install -g @earendil-works/pi-coding-agent
```

## 提交前检查

1. 不要提交密钥、`secrets.vault`、本机配置或构建产物
2. 变更尽量附带或更新对应测试（`tests/`）
3. 保持中文用户可见文案清晰；代码标识符、命令、路径保持原样
4. 大文件（EXE、VSIX、完整 dist）走 GitHub Releases，不要塞进源码树

## Pull Request

1. Fork 本仓库并创建分支（如 `fix/xxx`、`feat/xxx`）
2. 说明动机、改动点与验证方式
3. 若影响密钥/导入导出/启动 Pi 路径，请说明兼容性与回归结果

## 行为准则

请保持友善、就事论事。恶意提交、包含密钥的 PR、明显无关的垃圾内容会被关闭。
