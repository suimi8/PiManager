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
- Python 3.11+（CI 测试矩阵为 3.11 / 3.12；`pyproject.toml` 的 `requires-python` 与 ruff `target-version` 同为 3.11）
- 可选：`npm install -g @earendil-works/pi-coding-agent`

## 本地打包（当前 OS）

发布前依次执行（与 AGENTS.md 维护约束一致）：

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
ruff check .
python -m pytest tests -q
python scripts/check_secrets.py --scan-tests
python scripts/check_versions.py
python main.py --self-check
# 本地打包（当前 OS；PyInstaller 由 requirements-dev.txt 提供）
# macOS 额外：
# bash scripts/make_icns.sh
# Windows 产物为单文件版（dist/PiManager.exe）；macOS / Linux 用目录版 / .app：
#   Windows:    python -m PyInstaller --noconfirm --clean PiManagerOneFile.spec
#   macOS/Linux: python -m PyInstaller --noconfirm --clean PiManager.spec
python scripts/smoke_test_dist.py
python scripts/package_release.py --version 1.8.11
```

### 冒烟测试与打包脚本的门禁

两个脚本各自带**默认开启**的版本闸门，正常发布流程不需要手工传版本号：

`python scripts/smoke_test_dist.py`
- **版本闸门默认开启**：不传 `--expected-version` 时，期望值直接从
  `pi_manager/extras.py` 的 `APP_VERSION`（单一来源）读取，并与产物自己
  `--self-check` 输出的 `version=` 比对。过期的 `dist/` 再也无法悄悄通过。
  `--no-version-check` 可显式关闭（仅调试用）。
- 默认还会校验 `--print-provider-env` / `--config-mutate` 的 JSON 契约；
  `--skip-cli-contract` 可跳过。
- `--deep`：在 `--self-check` 内额外实例化主窗口（置
  `PIMANAGER_SELFCHECK_DEEP=1`）。它**会写用户配置目录**，所以只适合一次性的
  CI runner，不要在开发机上跑。当前 workflow 尚未启用，是留给加深冒烟覆盖的开关。
- `--dist` 相对路径以**项目根目录**为基准，不是当前工作目录。

`python scripts/package_release.py`
- `--version` 缺省即 `APP_VERSION`，一般不需要传。
- **打包前的二进制版本闸门**：实际执行产物的 `--self-check` 并比对版本，
  确保过期的 `dist/` 不会被套上新版本的文件名发出去。`--skip-version-check`
  可绕过（仅调试；发布路径绝不使用），`--self-check-timeout` 调整超时。
- **为每个产物落 `<产物名>.sha256`**（`<hash>  <name>` 格式），并把主产物摘要
  写进 `RUN-*.txt`，附带各平台的校验命令。`--no-checksums` 可关闭。
- macOS `--strict-sign`：把 `codesign` 与 `codesign --verify` 失败视为致命错误
  （**CI 已启用**）。归档保留符号链接，否则 `.app` 的
  Frameworks↔Resources 交叉链接被压平会让 ad-hoc 签名失效，用户看到「已损坏，
  无法打开」——比未签名更糟，因为「右键打开」也绕不过去。
- `--out` 中其他版本的 PiManager 归档默认会被清理，`--no-prune-stale` 可保留。

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
2. 运行 `scripts/smoke_test_dist.py`（`--self-check` + 版本闸门 + CLI 契约 + 资源/可执行位检查；CI 显式传 `--expected-version`，与缺省值同源）
3. 打包 zip/tar.gz、每个产物的 `.sha256` 与含摘要的 `RUN-*.txt`（macOS 带 `--strict-sign`）
4. 可选上传到 GitHub Release

手动触发：Actions → **Build** → **Run workflow**  
- `version`：`1.8.11`
- `upload_to_release`：`v1.8.11`（可选）

打 tag 也会触发：

```bash
git tag v1.8.11
git push origin v1.8.11
```

## 平台能力表

| OS | 终端启动 | 密钥存储 |
|----|----------|----------|
| Windows | Windows Terminal / PowerShell / cmd | OS keyring + 文件库回退 |
| macOS | Terminal.app / iTerm2 | Keychain + 文件库 |
| Linux | gnome-terminal / konsole / xterm 等 | Secret Service + 文件库回退（见下方注意） |

> **Linux headless / 无 D-Bus 环境注意**：无可用 Secret Service 后端（服务器、
> 容器、纯 SSH 会话）时，密钥会**静默回退**到当前用户 AES-GCM 文件库
> `~/.pi/agent/secrets.vault`。该回退路径的机密性弱于 OS keyring——它依赖文件
> 0600 权限与随机盐，`SECURITY.md`「回退 vault 的威胁模型」有完整说明。在这类
> 环境部署前请先读该节。

## 打包实现要点

- `PiManagerOneFile.spec`：Windows 主产物（单文件版，`dist/PiManager.exe`）
- `PiManager.spec`：macOS / Linux 产物（目录版 / `.app`）；按平台收集 keyring 后端、certifi、assets；禁用 UPX
- Qt 插件路径由 PySide6 官方 runtime hook（`pyi_rth_pyside6.py`）处理，本项目不再自带
  runtime hook：自定义 rthook 排在官方 hook **之前**执行，而官方 hook 无条件覆写
  `QT_PLUGIN_PATH`，故原 `scripts/pyi_rth_pimanager.py` 在任何场景都不可能生效，已删除
- `pi_manager/resources.py`：兼容 onedir / onefile / macOS `.app` 资源路径；冻结态
  **只信任 `sys._MEIPASS`**，不再回退 exe 同级目录（否则便携版旁放 `assets/` 可劫持内置资源）
- `main.py --self-check`：验证 PySide6 / cryptography / keyring / assets / 离屏 Qt

## 注意

- 不要把本机 `~/.pi/agent` 配置、密钥库打进安装包
- 二进制与 VSIX 的本地发布产物统一写入项目根目录 `release-assets/`，再由 CI 上传到 GitHub Releases
- Apple 正式签名/公证需额外 Developer ID 证书（可选增强，不是独立运行的硬性条件）

## 代码签名（可选）

产物签名是公开发布的前提（Windows SmartScreen 拦截「未知发布者」，macOS
Gatekeeper 拦截 ad-hoc 签名包），但需要真实证书，未配置时不阻塞本地/内部发布。
CI（`build.yml`）已预留条件签名步骤，配置以下 secrets 后自动生效：

| 平台 | Secret | 说明 |
|---|---|---|
| Windows | `WINDOWS_SIGN_CERT_BASE64` | 代码签名证书（.pfx）的 base64 |
| Windows | `WINDOWS_SIGN_CERT_PASSWORD` | 证书私钥密码 |

Windows 步骤在 `PyInstaller` 与 `smoke_test_dist.py` 之后、归档打包之前执行
`signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256`，确保
zip 内即为已签名 exe。

macOS 正式签名需 Developer ID 证书 + 公证（notarize），涉及 Apple 开发者账号与
`xcrun notarytool`，本仓库未内置该流程；需要时在 `build.yml` 的 macOS job 中于
`package_release.py` 之前对 `dist/PiManager.app` 执行：

```bash
codesign --force --options runtime --sign "Developer ID Application: <Your Name>" dist/PiManager.app
xcrun notarytool submit dist/PiManager.app --keychain-profile <profile> --wait
xcrun stapler staple dist/PiManager.app
```

注意：`package_release.py` 对 macOS 的 ad-hoc 签名是**硬闸门**（`--strict-sign`），
外部先做 Developer ID 签名后该步骤会以 `--force` 覆盖为 ad-hoc——如需保留正式
签名，请在调用 `package_release.py` 时去掉 macOS 的 `--strict-sign` 并设置
环境变量 `PM_MACOS_SIGN_IDENTITY`（设为 Developer ID 时脚本改用该身份签名，
而不是 ad-hoc；未设置时行为与现在完全一致）。
