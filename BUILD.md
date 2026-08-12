# PiManager — 跨平台构建与独立运行说明

目标：Windows / macOS / Linux 的发布包在**对应系统上解压后即可独立运行**（无需本机 Python）。

> 完整 Pi 会话仍依赖官方 `pi` CLI（Node）。PiManager GUI 本身是独立二进制。

## 从源码运行

```bash
python -m pip install -r requirements.txt
python main.py
python main.py --self-check
```

依赖：
- Python 3.10+
- 可选：`npm install -g @earendil-works/pi-coding-agent`

## 本地打包（当前 OS）

发布前依次执行（与 AGENTS.md 维护约束一致）：

```bash
python -m pip install -r requirements.txt
python -m pytest tests -q
python main.py --self-check
# 本地打包（当前 OS）
python -m pip install -r requirements.txt pyinstaller
# macOS 额外：
# bash scripts/make_icns.sh
# Windows 产物为单文件版（dist/PiManager.exe）；macOS / Linux 用目录版 / .app：
#   Windows:    python -m PyInstaller --noconfirm --clean PiManagerOneFile.spec
#   macOS/Linux: python -m PyInstaller --noconfirm --clean PiManager.spec
python scripts/smoke_test_dist.py
python scripts/package_release.py --version 1.8.1
```

Cursor 扩展统一从项目根目录打包：

```bash
python scripts/package_extension.py
```

脚本会先运行扩展测试，再按 `package.json` 版本生成
`release-assets/pi-manager-pi-cursor-<版本>.vsix`。相对输出路径始终以项目根目录为基准。
若本机未全局安装 `vsce`，脚本会自动通过 `npx @vscode/vsce` 获取官方打包工具。

## 各平台独立运行要求

| 平台 | 推荐产物 | 用户操作 | 保持完整的部分 |
|------|----------|----------|----------------|
| Windows x64 | `...-windows-x64-onefile.zip` | 解压后直接运行 `PiManager.exe` | 单文件自包含 |
| macOS arm64 | `...-macos-arm64.zip` | 解压后打开 `PiManager.app` | 整个 `.app` bundle |
| Linux x64 | `...-linux-x64.tar.gz` | `./PiManager/PiManager` | 整个 `PiManager/` 目录 |

### Windows
- 产物为单文件版（`dist/PiManager.exe`），自包含，可单独拷贝分发；首次启动需解压到临时目录，稍慢
- 自检：`PiManager.exe --self-check`

### macOS
- 当前 CI 使用 `macos-latest`（通常 arm64 / Apple Silicon）
- 未使用 Apple Developer ID 签名时，首次需「右键打开」或在隐私设置中允许
- 打包脚本会对 `.app` 做 **ad-hoc** 签名（`codesign -s -`），便于同机校验；**不是**可分发的 Developer ID 签名
- 自检：`PiManager.app/Contents/MacOS/PiManager --self-check`

### Linux
- 基于 Ubuntu 22.04 构建；glibc 过旧的发行版可能无法运行
- 若缺 GUI 库，安装例如：
  ```bash
  sudo apt-get install -y libgl1 libegl1 libxkbcommon0 libxcb-cursor0 libdbus-1-3 libfontconfig1
  ```
- 也可用 `./PiManager/run-PiManager.sh`
- 自检：`./PiManager/PiManager --self-check`

## GitHub Actions（推荐）

[`.github/workflows/build.yml`](.github/workflows/build.yml) 会：

1. 在 Windows / macOS / Linux 各自构建
2. 运行 `scripts/smoke_test_dist.py`（`--self-check` + 资源/可执行位检查）
3. 打包 zip/tar.gz 与 `RUN-*.txt`
4. 可选上传到 GitHub Release

手动触发：Actions → **Build** → **Run workflow**  
- `version`：`1.8.1`
- `upload_to_release`：`v1.8.1`（可选）

打 tag 也会触发：

```bash
git tag v1.8.1
git push origin v1.8.1
```

## 平台能力表

| OS | 终端启动 | 密钥存储 |
|----|----------|----------|
| Windows | Windows Terminal / PowerShell / cmd | OS keyring + 文件库回退 |
| macOS | Terminal.app / iTerm2 | Keychain + 文件库 |
| Linux | gnome-terminal / konsole / xterm 等 | Secret Service + 文件库 |

## 打包实现要点

- `PiManagerOneFile.spec`：Windows 主产物（单文件版，`dist/PiManager.exe`）
- `PiManager.spec`：macOS / Linux 产物（目录版 / `.app`）；按平台收集 keyring 后端、certifi、assets；禁用 UPX
- `scripts/pyi_rth_pimanager.py`：冻结环境下设置 `QT_PLUGIN_PATH`
- `pi_manager/resources.py`：兼容 onedir / onefile / macOS `.app` 资源路径
- `main.py --self-check`：验证 PySide6 / cryptography / keyring / assets / 离屏 Qt

## 注意

- 不要把本机 `~/.pi/agent` 配置、密钥库打进安装包
- 二进制与 VSIX 的本地发布产物统一写入项目根目录 `release-assets/`，再由 CI 上传到 GitHub Releases
- Apple 正式签名/公证需额外 Developer ID 证书（可选增强，不是独立运行的硬性条件）
