# PiManager — 跨平台构建说明

## 从源码运行

```bash
python -m pip install -r requirements.txt
python main.py
```

依赖：
- Python 3.10+
- Node.js + `npm install -g @earendil-works/pi-coding-agent`

## 平台差异

| OS | 终端启动 | 密钥存储 |
|----|----------|----------|
| Windows | Windows Terminal / PowerShell / cmd | OS keyring + DPAPI/AES-GCM 文件库回退 |
| macOS | Terminal.app / iTerm2 | Keychain（keyring）+ 文件库 |
| Linux | gnome-terminal / konsole / xterm / x-terminal-emulator | Secret Service（keyring）+ 文件库 |

## 本地 PyInstaller

先安装：

```bash
python -m pip install -r requirements.txt pyinstaller
```

### Windows

```bat
python -m PyInstaller --noconfirm --clean PiManager.spec
python -m PyInstaller --noconfirm --clean PiManagerOneFile.spec
python scripts/package_release.py --platform windows --version 1.6.0
```

产物：
- `dist/PiManager/` 目录版
- `dist/PiManager.exe` 单文件版
- `release-assets/PiManager-v1.6.0-windows-*.zip`

### macOS

```bash
bash scripts/make_icns.sh   # 生成 assets/pi-manager.icns
python -m PyInstaller --noconfirm --clean PiManager.spec
python scripts/package_release.py --platform macos --version 1.6.0
```

产物：
- `dist/PiManager.app`
- `release-assets/PiManager-v1.6.0-macos-arm64.zip`（或 `macos-x64`）

说明：
- 未签名未公证的 `.app`，首次打开可能需在「系统设置 → 隐私与安全性」中允许
- Apple Silicon 与 Intel 需在对应架构机器上分别打包（CI 的 `macos-latest` 当前为 arm64）

### Linux

```bash
# Debian/Ubuntu 示例依赖
sudo apt-get install -y libgl1 libxkbcommon0 libxcb-cursor0 libdbus-1-3
python -m PyInstaller --noconfirm --clean PiManager.spec
python scripts/package_release.py --platform linux --version 1.6.0
```

产物：
- `dist/PiManager/`
- `release-assets/PiManager-v1.6.0-linux-x64.tar.gz`

运行目录版：

```bash
./PiManager/PiManager
```

若缺少系统库，按报错安装对应 `libxcb-*` / OpenGL 包。

## GitHub Actions 跨平台打包（推荐）

仓库已包含 [`.github/workflows/build.yml`](.github/workflows/build.yml)，会在 **Windows / macOS / Linux** 上分别构建：

1. 打开 Actions → **Build** → **Run workflow**
2. `version` 填 `1.6.0`
3. 若要直接挂到已有 Release，`upload_to_release` 填 `v1.6.0`
4. 构建完成后可在 Artifacts 下载，或到 Release 页查看附件

也可打 tag 触发：

```bash
git tag v1.6.1
git push origin v1.6.1
```

## 注意

- GUI 可在三平台打包；完整 Pi 会话仍需本机 PATH 上有官方 `pi` CLI
- 二进制与 VSIX 走 GitHub Releases，不进入源码树
- 不要把本机 `~/.pi/agent` 配置、密钥库打进安装包
