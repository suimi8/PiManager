#!/usr/bin/env python3
"""Smoke-test a packaged PiManager distribution on the current OS.

Usage:
  python scripts/smoke_test_dist.py --platform windows|macos|linux --dist dist
"""
from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
import time
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


def resolve_binary(dist: Path, plat: str) -> Path:
    if plat == "windows":
        candidates = [
            dist / "PiManager" / "PiManager.exe",
            dist / "PiManager.exe",
        ]
    elif plat == "macos":
        candidates = [
            dist / "PiManager.app" / "Contents" / "MacOS" / "PiManager",
            dist / "PiManager",
        ]
    else:
        candidates = [
            dist / "PiManager" / "PiManager",
            dist / "PiManager",
        ]
    for path in candidates:
        if path.exists():
            return path
    raise FileNotFoundError(f"no packaged binary under {dist} for platform={plat}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", default=detect_platform())
    parser.add_argument("--dist", default="dist")
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args()

    plat = args.platform.lower()
    dist = Path(args.dist)
    binary = resolve_binary(dist, plat)
    print(f"smoke binary: {binary}")

    env = os.environ.copy()
    # Headless-friendly Qt backend for CI / servers.
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    # Avoid picking up a developer venv accidentally.
    env.pop("PYTHONPATH", None)

    cmd = [str(binary), "--self-check"]
    if plat == "linux":
        # Prefer xvfb when available for extra realism, still keep offscreen fallback.
        if subprocess.call(["bash", "-lc", "command -v xvfb-run >/dev/null"], stdout=subprocess.DEVNULL) == 0:
            cmd = ["xvfb-run", "-a", str(binary), "--self-check"]

    started = time.time()
    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=args.timeout,
        check=False,
    )
    elapsed = time.time() - started
    print(f"exit={proc.returncode} elapsed={elapsed:.1f}s")
    if proc.stdout:
        print("--- stdout ---")
        print(proc.stdout)
    if proc.stderr:
        print("--- stderr ---")
        print(proc.stderr)
    if proc.returncode != 0:
        return proc.returncode

    # Basic layout checks
    if plat == "windows":
        assets = binary.parent / "_internal" / "assets"
        if not assets.is_dir():
            assets = binary.parent / "assets"
        if not assets.is_dir():
            print("FAIL: assets directory missing next to Windows binary", file=sys.stderr)
            return 2
    elif plat == "macos":
        app = dist / "PiManager.app"
        if not app.is_dir():
            print("FAIL: PiManager.app missing", file=sys.stderr)
            return 2
        # Ensure executable bit
        if not os.access(binary, os.X_OK):
            print("FAIL: macOS binary not executable", file=sys.stderr)
            return 2
    else:
        if not os.access(binary, os.X_OK):
            print("FAIL: Linux binary not executable", file=sys.stderr)
            return 2
        # Shared libs commonly expected beside binary in onedir builds
        internal = binary.parent / "_internal"
        if not internal.is_dir() and not (binary.parent / "libpython3.12.so.1.0").exists():
            # Older layout may keep libs next to binary; warn only if completely empty tree
            if len(list(binary.parent.iterdir())) < 3:
                print("FAIL: Linux onedir looks incomplete", file=sys.stderr)
                return 2

    print("smoke-test: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
