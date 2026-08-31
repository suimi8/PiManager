#!/usr/bin/env python3
"""Smoke-test a packaged PiManager distribution on the current OS.

Usage:
  python scripts/smoke_test_dist.py --platform windows|macos|linux --dist dist

The version gate is ON by default: with no --expected-version the expected
value is read from pi_manager/extras.py (APP_VERSION, the single source of
truth), so a stale dist/ can no longer sail through unnoticed. Pass
--no-version-check to disable it explicitly (docs/review/r2-build.md B-5).
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]

_APP_VERSION_RE = re.compile(r'APP_VERSION\s*=\s*"([^"]+)"')
_SELF_CHECK_VERSION_RE = re.compile(r"^version=(.+)$", re.MULTILINE)


def read_app_version(project_root: Path = REPO_ROOT) -> str:
    """Extract APP_VERSION from pi_manager/extras.py (single source of truth)."""
    src = (project_root / "pi_manager" / "extras.py").read_text(encoding="utf-8")
    match = _APP_VERSION_RE.search(src)
    if not match:
        raise SystemExit("cannot extract APP_VERSION from pi_manager/extras.py")
    return match.group(1)


def resolve_repo_path(value: str) -> Path:
    """Resolve --dist relative to the repo root, not the CWD (B-15).

    package_release.py already does this; running the smoke test from a
    subdirectory used to fail with a confusing FileNotFoundError.
    """
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


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


def resolve_onefile(dist: Path, plat: str) -> Path | None:
    if plat == "windows":
        candidate = dist / "PiManager.exe"
    elif plat == "macos":
        candidate = dist / "PiManager.app" / "Contents" / "MacOS" / "PiManager"
    else:
        candidate = dist / "PiManager"
    # Onedir builds keep a directory named like the binary; only a real
    # standalone file at this location is a onefile product.
    return candidate if candidate.is_file() else None


def parse_self_check_version(stdout: str) -> str | None:
    """Exact ``version=<X>`` line match.

    The previous substring check (``f"version={expected}" not in stdout``)
    accepted 1.8.60 for an expected 1.8.6 (B-13).
    """
    match = _SELF_CHECK_VERSION_RE.search(stdout or "")
    return match.group(1).strip() if match else None


def check_self_check_output(stdout: str, expected_version: str | None) -> list[str]:
    problems: list[str] = []
    if "self-check: OK" not in (stdout or ""):
        problems.append("self-check did not report OK")
    if expected_version is None:
        return problems
    found = parse_self_check_version(stdout)
    if found is None:
        problems.append("self-check printed no 'version=' line")
    elif found != expected_version:
        problems.append(
            f"packaged version mismatch: binary reports {found!r}, expected {expected_version!r}"
        )
    return problems


def run_self_check(binary: Path, plat: str, env: dict[str, str], timeout: int) -> tuple[int, str]:
    cmd = [str(binary), "--self-check"]
    if plat == "linux":
        # Prefer xvfb when available for extra realism, still keep offscreen fallback.
        if subprocess.call(["bash", "-lc", "command -v xvfb-run >/dev/null"], stdout=subprocess.DEVNULL) == 0:
            cmd = ["xvfb-run", "-a", "-s", "-screen 0 1024x768x24", str(binary), "--self-check"]
    started = time.time()
    proc = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
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
    return proc.returncode, proc.stdout or ""


def run_cli_contract(binary: Path, env: dict[str, str], timeout: int) -> list[str]:
    """Exercise the extension-facing CLI subcommands, not just --self-check.

    --self-check never touches provider_env / config_broker, which are the
    extension's hot path; a frozen build that lost those modules used to print
    "self-check: OK" all the same (B-8).

    Both entry points are invoked without arguments on purpose: they must answer
    with the documented JSON-only error contract (no argparse usage noise on
    stdout, no traceback). That is enough to prove the modules survived
    freezing, and it needs neither a broker token nor any user state.
    """
    import json

    problems: list[str] = []
    checks: list[list[str]] = [
        ["--print-provider-env"],
        ["--config-mutate"],
    ]
    for extra in checks:
        label = " ".join(extra)
        try:
            proc = subprocess.run(
                [str(binary), *extra],
                env=env,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            problems.append(f"{label} timed out after {timeout}s")
            continue
        raw = (proc.stdout or "").strip()
        if not raw:
            problems.append(f"{label} printed nothing (module missing from the bundle?)")
            continue
        try:
            payload = json.loads(raw.splitlines()[-1])
        except (ValueError, IndexError):
            problems.append(f"{label} did not print JSON: {raw[:120]!r}")
            continue
        if not isinstance(payload, dict) or "ok" not in payload:
            problems.append(f"{label} JSON has no 'ok' field: {raw[:120]!r}")
            continue
        print(f"cli contract: {label} -> ok={payload.get('ok')}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", default=detect_platform())
    parser.add_argument("--dist", default="dist")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument(
        "--expected-version",
        default="",
        help="expected APP_VERSION; defaults to pi_manager/extras.py",
    )
    parser.add_argument(
        "--no-version-check",
        action="store_true",
        help="disable the version gate (debug only)",
    )
    parser.add_argument(
        "--deep",
        action="store_true",
        help="also instantiate the main window inside --self-check "
        "(PIMANAGER_SELFCHECK_DEEP=1; writes to the user config dir, so CI only)",
    )
    parser.add_argument(
        "--skip-cli-contract",
        action="store_true",
        help="skip the provider-env / config-mutate JSON contract checks",
    )
    args = parser.parse_args()

    plat = args.platform.lower()
    dist = resolve_repo_path(args.dist)
    binary = resolve_binary(dist, plat)
    print(f"smoke binary: {binary}")

    expected_version: str | None
    if args.no_version_check:
        expected_version = None
        print("warning: version gate disabled by --no-version-check", file=sys.stderr)
    else:
        expected_version = args.expected_version or read_app_version()
        print(f"expected version: {expected_version}")

    env = os.environ.copy()
    # A developer shell started from packaged PiManager still carries _PYI_*;
    # the onefile bootloader would then treat this smoke launch as a worker.
    for key in [name for name in env if name.startswith("_PYI_")]:
        env.pop(key, None)
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    # Headless-friendly Qt backend for CI / servers.
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    env.setdefault("QT_OPENGL", "software")
    # Avoid picking up a developer venv accidentally.
    env.pop("PYTHONPATH", None)
    if args.deep:
        env["PIMANAGER_SELFCHECK_DEEP"] = "1"
    else:
        env.pop("PIMANAGER_SELFCHECK_DEEP", None)

    # Ensure onedir shared libraries are found first on Linux.
    if plat == "linux":
        lib_dirs = [str(binary.parent), str(binary.parent / "_internal")]
        existing = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = ":".join([d for d in lib_dirs if Path(d).is_dir()] + ([existing] if existing else []))

    returncode, stdout = run_self_check(binary, plat, env, args.timeout)
    if returncode != 0:
        return returncode
    problems = check_self_check_output(stdout, expected_version)
    if problems:
        for line in problems:
            print(f"FAIL: {line}", file=sys.stderr)
        return 2

    # Basic layout checks
    if plat == "windows":
        # Onefile embeds assets inside the exe (validated by --self-check);
        # only the onedir layout keeps _internal/ (or assets/) next to the binary.
        onedir = (binary.parent / "_internal").is_dir() or (binary.parent / "assets").is_dir()
        if onedir:
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
        if not internal.is_dir() and not list(binary.parent.glob("libpython*.so*")):
            # Older layout may keep libs next to binary; warn only if completely empty tree
            if len(list(binary.parent.iterdir())) < 3:
                print("FAIL: Linux onedir looks incomplete", file=sys.stderr)
                return 2

    if not args.skip_cli_contract:
        problems = run_cli_contract(binary, env, args.timeout)
        if problems:
            for line in problems:
                print(f"FAIL: {line}", file=sys.stderr)
            return 2

    # onefile products land directly in dist/ root; validate them too when present.
    onefile = resolve_onefile(dist, plat)
    if onefile is not None and onefile != binary:
        print(f"smoke onefile: {onefile}")
        returncode, stdout = run_self_check(onefile, plat, env, args.timeout)
        if returncode != 0:
            return returncode
        problems = check_self_check_output(stdout, expected_version)
        if problems:
            for line in problems:
                print(f"FAIL: onefile {line}", file=sys.stderr)
            return 2

    print("smoke-test: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
