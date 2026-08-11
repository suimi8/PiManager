# PiManager 桌面版本维护约束

本目录是完整、独立的跨平台桌面应用（Windows / macOS / Linux）源码单元。维护、构建或发布本版本时，只允许依赖本目录中的文件。

## 不可破坏的边界

- Python 源码（`main.py`、`pi_manager/`）、`tests/`、`extensions/pi-cursor`、`scripts/`、`PiManager.spec`、`PiManagerOneFile.spec`、`BUILD.md` 必须全部保留在本目录。
- 用户配置目录固定为 `~/.pi/agent/`（Windows 为 `%USERPROFILE%\.pi\agent\`），不得改写为其他位置；`models.json`、`settings.json`、`auth.json`、`pi-manager.json`、`secrets.vault` 均位于其中。
- 真实 API Key 只存 OS keyring（Windows Credential Locker / macOS Keychain / Linux Secret Service），keyring 不可用时回退当前用户 AES-GCM 文件库 `secrets.vault`；绝不写入 `models.json`、`settings.json`、日志或未加密导出包。
- `models.json` 只保存官方 Pi 支持的环境变量引用（`${PI_MANAGER_PROVIDER_<SLUG>_<HASH>_API_KEY}`）；启动 Pi 子进程时由应用从安全存储读取并注入环境，密钥不落盘。
- 导出配置 ZIP 含密钥时必须使用 PBKDF2 + AES-256-GCM 加密；未加密导出不得包含任何密钥。
- 轻量 CLI 入口 `--print-provider-env`、`--vision-describe`、`--config-mutate` 不得导入 GUI（PySide6）；它们是 Cursor 扩展的热路径，必须保持无 GUI 依赖。
- Cursor / VS Code 扩展（`extensions/pi-cursor`）通过 helper 安全注入运行时环境，不得内嵌或读取明文密钥。
- JSON 存储写入必须走 `pi_manager/storage.py` 的原子写与并发锁（`locked`），防止多进程 / 多线程写坏配置。
- 应用版本以 `pi_manager/extras.py` 的 `APP_VERSION` 为单一来源；Cursor 扩展版本以 `extensions/pi-cursor/package.json` 为准，两者独立维护、发布时同步更新。
- 发布产物（`release-assets/`、dist 目录等）不得提交到仓库；二进制通过 GitHub Releases 分发。

## 检测不变量

- `python -m pytest tests -q` 必须全部通过。
- `python main.py --self-check` 必须输出 `self-check: OK`；发布包以对应平台产物（`PiManager.exe --self-check` / `PiManager.app/Contents/MacOS/PiManager --self-check` / `./PiManager/PiManager --self-check`）运行等价自检。
- Linux 无头环境（CI）必须用 `xvfb` 运行 GUI 冒烟测试。
- 打包后运行 `python scripts/smoke_test_dist.py` 验证产物可独立启动。
- 密钥相关行为（keyring 优先、vault 回退、`models.json` 无明文）必须有测试覆盖，禁止出现安全回退退化。

## 修改和部署

修改本目录不会自动改变已发布的二进制或 CI 产物。需要多平台行为一致时，必须在 Windows / macOS / Linux 分别构建并测试（推荐 GitHub Actions → **Build**）。

发布前执行：

```bash
python -m pip install -r requirements.txt
python -m pytest tests -q
python main.py --self-check
# 本地打包（当前 OS）
python -m PyInstaller --noconfirm --clean PiManager.spec
python scripts/smoke_test_dist.py
python scripts/package_release.py --version <新版本号>
```

打包脚本与发布脚本互不调用，也不共享状态；不得把 `release-assets/`、任何含真实密钥的配置、`secrets.vault` 或未加密导出 ZIP 提交到仓库。
