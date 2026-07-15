#!/usr/bin/env python3
"""Package PyInstaller dist/ output into release archives.

Usage (after pyinstaller):
  python scripts/package_release.py --platform windows|macos|linux --version 1.6.0
"""
from __future__ import annotations

import argparse
import platform
import shutil
import sys
import tarfile
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


def zip_dir(src: Path, dst: Path, arc_root: str | None = None) -> None:
    if dst.exists():
        dst.unlink()
    with zipfile.ZipFile(dst, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for path in src.rglob("*"):
            if path.is_file():
                rel = path.relative_to(src)
                arc = f"{arc_root}/{rel.as_posix()}" if arc_root else rel.as_posix()
                zf.write(path, arcname=arc)


def tar_gz_dir(src: Path, dst: Path, arc_root: str | None = None) -> None:
    if dst.exists():
        dst.unlink()
    with tarfile.open(dst, "w:gz") as tf:
        tf.add(src, arcname=arc_root or src.name)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", default=detect_platform())
    parser.add_argument("--version", default="1.6.0")
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
        one = dist / "PiManager"
        if app.is_dir():
            target = out / f"PiManager-v{version}-macos-arm64-or-x64.zip"
            # Prefer machine arch in filename when available.
            machine = platform.machine().lower()
            if machine in {"arm64", "aarch64"}:
                target = out / f"PiManager-v{version}-macos-arm64.zip"
            elif machine in {"x86_64", "amd64"}:
                target = out / f"PiManager-v{version}-macos-x64.zip"
            zip_dir(app, target, arc_root="PiManager.app")
            written.append(target)
        elif one.is_file():
            target = out / f"PiManager-v{version}-macos-onefile"
            shutil.copy2(one, target)
            written.append(target)
    elif plat == "linux":
        dir_src = dist / "PiManager"
        one_src = dist / "PiManager"
        if dir_src.is_dir():
            target = out / f"PiManager-v{version}-linux-x64.tar.gz"
            tar_gz_dir(dir_src, target, arc_root="PiManager")
            written.append(target)
        elif one_src.is_file():
            target = out / f"PiManager-v{version}-linux-x64-onefile.tar.gz"
            if target.exists():
                target.unlink()
            with tarfile.open(target, "w:gz") as tf:
                tf.add(one_src, arcname="PiManager")
            written.append(target)
    else:
        print(f"unsupported platform: {plat}", file=sys.stderr)
        return 2

    if not written:
        print(f"no dist artifacts found under {dist}", file=sys.stderr)
        return 1

    for path in written:
        print(f"wrote {path} ({path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
