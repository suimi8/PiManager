#!/usr/bin/env python3
"""Package PyInstaller dist/ output into standalone release archives.

Usage (after pyinstaller):
  python scripts/package_release.py --platform windows|macos|linux --version 1.6.2
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path


def detect_platform() -> str:
    system = platform.system().lower()
    if system == "windows":
        return "windows"
    if system == "darwin":
        return "macos"
    if system == "linux":
        return "linux"
    return system


def _ensure_executable(path: Path) -> None:
    if not path.exists() or path.is_dir():
        return
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def zip_dir(src: Path, dst: Path, arc_root: str | None = None) -> None:
    if dst.exists():
        dst.unlink()
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(src)
            arc = f"{arc_root}/{rel.as_posix()}" if arc_root else rel.as_posix()
            info = zipfile.ZipInfo(arc)
            st = path.stat()
            info.date_time = time.localtime(st.st_mtime)[:6]
            info.compress_type = zipfile.ZIP_DEFLATED
            # Preserve executable bit for Unix unzip tools
            executable = os.access(path, os.X_OK) or path.name in {"PiManager", "run-PiManager.sh"}
            info.external_attr = (0o755 if executable else 0o644) << 16
            with path.open("rb") as fh:
                zf.writestr(info, fh.read())


def tar_gz_dir(src: Path, dst: Path, arc_root: str | None = None) -> None:
    if dst.exists():
        dst.unlink()

    def _filter(ti: tarfile.TarInfo) -> tarfile.TarInfo:
        name = ti.name.replace("\\", "/")
        base = name.rsplit("/", 1)[-1]
        if ti.isfile() and (
            base == "PiManager" or name.endswith("/PiManager") or base.endswith(".sh")
        ):
            ti.mode = 0o755
        return ti

    with tarfile.open(dst, "w:gz") as tf:
        tf.add(src, arcname=arc_root or src.name, filter=_filter)


def write_run_notes(out_dir: Path, plat: str, version: str) -> Path:
    if plat == "windows":
        text = f"""PiManager v{version} (Windows x64)

独立运行说明：
1. 解压本 zip 到任意目录（不要只运行压缩包内路径）
2. 双击 PiManager\\PiManager.exe
3. 目录版请保持 PiManager.exe 与 _internal 文件夹同级，勿拆散

可选：安装官方 Pi CLI 以启动完整会话
  npm install -g @earendil-works/pi-coding-agent

自检：
  PiManager\\PiManager.exe --self-check
"""
    elif plat == "macos":
        text = f"""PiManager v{version} (macOS)

独立运行说明：
1. 解压 zip
2. 将 PiManager.app 拖到「应用程序」或任意文件夹
3. 首次打开：右键 → 打开（未签名时需在「隐私与安全性」中允许）

注意：
- 请使用与本机架构匹配的包（Apple Silicon 用 arm64 包）
- 完整 Pi 会话仍需本机安装官方 pi CLI

自检：
  PiManager.app/Contents/MacOS/PiManager --self-check
"""
    else:
        text = f"""PiManager v{version} (Linux x64)

独立运行说明：
1. tar -xzf PiManager-v{version}-linux-x64.tar.gz
2. ./PiManager/PiManager
   或 ./PiManager/run-PiManager.sh
3. 保持目录完整（可执行文件与 _internal 等同级依赖不要拆开）

若启动报缺库，按发行版安装常见 GUI 依赖，例如 Debian/Ubuntu：
  sudo apt-get install -y libgl1 libegl1 libxkbcommon0 libxcb-cursor0 libdbus-1-3 libfontconfig1

完整 Pi 会话仍需本机安装官方 pi CLI：
  npm install -g @earendil-works/pi-coding-agent

自检：
  ./PiManager/PiManager --self-check
"""
    path = out_dir / f"RUN-{plat}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", default=detect_platform())
    parser.add_argument("--version", default="1.6.2")
    parser.add_argument("--dist", default="dist")
    parser.add_argument("--out", default="release-assets")
    args = parser.parse_args()

    dist = Path(args.dist)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    version = args.version
    plat = args.platform.lower()
    written: list[Path] = []

    if plat == "windows":
        dir_src = dist / "PiManager"
        one_src = dist / "PiManager.exe"
        if dir_src.is_dir():
            target = out / f"PiManager-v{version}-windows-x64-dir.zip"
            zip_dir(dir_src, target, arc_root="PiManager")
            written.append(target)
        if one_src.is_file():
            target = out / f"PiManager-v{version}-windows-x64-onefile.zip"
            if target.exists():
                target.unlink()
            with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
                zf.write(one_src, arcname="PiManager.exe")
            written.append(target)
    elif plat == "macos":
        app = dist / "PiManager.app"
        if not app.is_dir():
            print("PiManager.app not found", file=sys.stderr)
            return 1
        binary = app / "Contents" / "MacOS" / "PiManager"
        _ensure_executable(binary)
        # Ad-hoc sign for local consistency (not a Developer ID signature).
        if shutil.which("codesign"):
            subprocess.run(
                ["codesign", "--force", "--deep", "--sign", "-", str(app)],
                check=False,
                capture_output=True,
                text=True,
            )
        machine = platform.machine().lower()
        if machine in {"arm64", "aarch64"}:
            target = out / f"PiManager-v{version}-macos-arm64.zip"
        elif machine in {"x86_64", "amd64"}:
            target = out / f"PiManager-v{version}-macos-x64.zip"
        else:
            target = out / f"PiManager-v{version}-macos.zip"
        zip_dir(app, target, arc_root="PiManager.app")
        written.append(target)
    elif plat == "linux":
        dir_src = dist / "PiManager"
        if not dir_src.is_dir():
            print("dist/PiManager not found", file=sys.stderr)
            return 1
        binary = dir_src / "PiManager"
        _ensure_executable(binary)
        launcher = dir_src / "run-PiManager.sh"
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            "set -euo pipefail\n"
            'HERE="$(cd "$(dirname "$0")" && pwd)"\n'
            'cd "$HERE"\n'
            'exec "$HERE/PiManager" "$@"\n',
            encoding="utf-8",
        )
        _ensure_executable(launcher)
        target = out / f"PiManager-v{version}-linux-x64.tar.gz"
        tar_gz_dir(dir_src, target, arc_root="PiManager")
        written.append(target)
    else:
        print(f"unsupported platform: {plat}", file=sys.stderr)
        return 2

    notes = write_run_notes(out, plat, version)
    written.append(notes)

    for path in written:
        size = path.stat().st_size if path.is_file() else 0
        print(f"wrote {path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
