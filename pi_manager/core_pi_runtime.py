"""Pi CLI 安装通道、版本检查与升级。"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import logging
import re
from typing import Any

from . import storage

logger = logging.getLogger(__name__)


def _core():
    from . import core

    return core



# ==== 模型列表与 Pi 版本 ====


def get_pi_version() -> str:
    """Return Pi's version only when the CLI exits successfully.

    Runtime failures can mention a Node.js version in stderr. Those failures
    must never be parsed as Pi's installed version.
    """
    try:
        process = _core().run_pi(["-v"], timeout=20)
        output = (process.stdout or process.stderr or "").strip()
        first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
        if process.returncode != 0:
            detail = first_line or "\u672a\u8fd4\u56de\u9519\u8bef\u8be6\u60c5"
            return f"error: Pi \u542f\u52a8\u5931\u8d25\uff08\u9000\u51fa\u7801 {process.returncode}\uff09\uff1a{detail}"
        return first_line or "unknown"
    except Exception as exc:
        return f"error: {exc}"



PI_NPM_PACKAGE = "@earendil-works/pi-coding-agent"

PI_LATEST_TAG = "latest"

PI_LEGACY_NODE20_TAG = "legacy-node20"

PI_LATEST_MIN_NODE = (22, 19, 0)

PI_LEGACY_MIN_NODE = (20, 6, 0)



def _npm_command(*args: str) -> list[str]:
    """Resolve npm's Windows command shim without invoking a shell."""
    names = ("npm.cmd", "npm") if sys.platform == "win32" else ("npm",)
    executable = next((path for name in names if (path := shutil.which(name))), names[0])
    return [executable, *args]



def _node_command(*args: str) -> list[str]:
    names = ("node.exe", "node") if sys.platform == "win32" else ("node",)
    executable = next((path for name in names if (path := shutil.which(name))), names[0])
    return [executable, *args]



def _run_version_command(command: list[str], timeout: float = 20) -> str | None:
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env=_core().strip_pyinstaller_runtime_env(os.environ),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        return None
    if process.returncode != 0:
        return None
    output = (process.stdout or process.stderr or "").strip()
    match = re.search(r"(?:^|\D)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", output)
    return match.group(1) if match else None



def get_node_version(timeout: float = 20) -> str | None:
    """Return the active Node.js semantic version without a leading v."""
    return _run_version_command(_core()._node_command("--version"), timeout=timeout)



def get_npm_version(timeout: float = 20) -> str | None:
    """Return the active npm semantic version."""
    return _run_version_command(_core()._npm_command("--version"), timeout=timeout)



def select_pi_install_channel(node_version: str | None = None) -> str | None:
    """Select the npm dist-tag compatible with the active Node.js runtime."""
    version = node_version if node_version is not None else _core().get_node_version()
    if not version:
        return None
    parsed = parse_semver(version)
    if parsed >= PI_LATEST_MIN_NODE:
        return PI_LATEST_TAG
    if parsed >= PI_LEGACY_MIN_NODE:
        return PI_LEGACY_NODE20_TAG
    return None



def pi_package_spec(channel: str | None) -> str | None:
    return f"{PI_NPM_PACKAGE}@{channel}" if channel else None



def get_latest_pi_version(timeout: float = 20, tag: str | None = None) -> str | None:
    """Return the newest Pi version for an npm compatibility channel."""
    channel = tag or _core().select_pi_install_channel() or PI_LATEST_TAG
    try:
        process = subprocess.run(
            _core()._npm_command("view", f"{PI_NPM_PACKAGE}@{channel}", "version"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env=_core().strip_pyinstaller_runtime_env(os.environ),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        return None
    if process.returncode != 0:
        return None
    output = (process.stdout or "").strip()
    match = re.search(r"(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", output)
    return match.group(1) if match else None



def get_installed_pi_version() -> str | None:
    value = get_pi_version()
    if not value or value.startswith("error:") or value == "unknown":
        return None
    match = re.search(r"(?:^|\D)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", value)
    return match.group(1) if match else None



def parse_semver(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(v or ""))
    values = tuple(int(x) for x in parts[:3]) if parts else (0,)
    return values + (0,) * (3 - len(values))



def get_pi_runtime_status() -> dict[str, Any]:
    """Inspect whether the Pi command exists and can actually start."""
    command = _core().find_pi_command()
    if not command:
        return {
            "command": None,
            "installed": None,
            "raw_version": None,
            "missing": True,
            "runtime_broken": False,
            "ok": False,
            "error": "\u672a\u627e\u5230 Pi \u547d\u4ee4\u3002",
        }
    raw_version = get_pi_version()
    if raw_version.startswith("error:") or raw_version == "unknown":
        error = raw_version.removeprefix("error:").strip() or "Pi \u65e0\u6cd5\u542f\u52a8\u3002"
        return {
            "command": command,
            "installed": None,
            "raw_version": raw_version,
            "missing": False,
            "runtime_broken": True,
            "ok": False,
            "error": error,
        }
    match = re.search(r"(?:^|\D)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", raw_version)
    installed = match.group(1) if match else None
    if not installed:
        return {
            "command": command,
            "installed": None,
            "raw_version": raw_version,
            "missing": False,
            "runtime_broken": True,
            "ok": False,
            "error": f"\u65e0\u6cd5\u89e3\u6790 Pi \u7248\u672c\u8f93\u51fa\uff1a{raw_version}",
        }
    return {
        "command": command,
        "installed": installed,
        "raw_version": raw_version,
        "missing": False,
        "runtime_broken": False,
        "ok": True,
        "error": "",
    }



def needs_pi_install_or_update() -> dict[str, Any]:
    """Return actionable Pi runtime, registry, and compatibility status."""
    node_version = _core().get_node_version()
    npm_version = _core().get_npm_version()
    channel = _core().select_pi_install_channel(node_version)
    package_spec = _core().pi_package_spec(channel)
    runtime = _core().get_pi_runtime_status()

    blocked_reason = ""
    if not node_version:
        blocked_reason = "\u672a\u68c0\u6d4b\u5230 Node.js\u3002\u8bf7\u5148\u5b89\u88c5 Node.js 20.6 \u6216\u66f4\u9ad8\u7248\u672c\u3002"
    elif parse_semver(node_version) < PI_LEGACY_MIN_NODE:
        blocked_reason = (
            f"\u5f53\u524d Node.js {node_version} \u8fc7\u4f4e\uff1bPi \u81f3\u5c11\u9700\u8981 Node.js 20.6\uff0c"
            "\u63a8\u8350\u5347\u7ea7\u5230 22.19 \u6216\u66f4\u9ad8\u7248\u672c\u3002"
        )
    elif not npm_version:
        blocked_reason = "\u672a\u68c0\u6d4b\u5230\u53ef\u7528\u7684 npm\u3002\u8bf7\u4fee\u590d Node.js/npm \u5b89\u88c5\u540e\u91cd\u8bd5\u3002"

    installable = bool(channel and npm_version and not blocked_reason)
    latest = _core().get_latest_pi_version(tag=channel) if installable else None
    registry_ok = bool(latest)
    check_failed = bool(installable and not registry_ok)
    installed = runtime.get("installed")
    missing = bool(runtime.get("missing"))
    runtime_broken = bool(runtime.get("runtime_broken"))
    repair_required = runtime_broken
    outdated = bool(
        installed and latest and parse_semver(str(installed)) < parse_semver(str(latest))
    )

    result: dict[str, Any] = {
        "installed": installed,
        "latest": latest,
        "missing": missing,
        "outdated": outdated,
        "ok": False,
        "message": "",
        "registry_ok": registry_ok,
        "check_failed": check_failed,
        "runtime_broken": runtime_broken,
        "repair_required": repair_required,
        "installable": installable,
        "blocked": bool(blocked_reason),
        "node_version": node_version,
        "npm_version": npm_version,
        "channel": channel,
        "package_spec": package_spec,
        "error": "",
        "command": runtime.get("command"),
    }

    channel_label = "\u6700\u65b0\u7248\u901a\u9053" if channel == PI_LATEST_TAG else "Node 20 \u517c\u5bb9\u901a\u9053"
    channel_detail = f"{channel_label}\uff08{channel}\uff09" if channel else "\u65e0\u517c\u5bb9\u901a\u9053"
    if blocked_reason:
        runtime_detail = f" \u5f53\u524d Pi\uff1a{installed}\u3002" if installed else ""
        result["message"] = blocked_reason + runtime_detail
        result["error"] = blocked_reason
        return result
    if runtime_broken:
        detail = str(runtime.get("error") or "Pi \u65e0\u6cd5\u542f\u52a8")
        result["message"] = (
            f"\u68c0\u6d4b\u5230 Pi \u547d\u4ee4\uff0c\u4f46\u8fd0\u884c\u5931\u8d25\uff1a{detail}\n"
            f"\u53ef\u901a\u8fc7 {package_spec} \u6267\u884c\u4fee\u590d\u5b89\u88c5\u3002"
        )
        result["error"] = detail
        return result
    if check_failed:
        installed_detail = f"\u5f53\u524d\u5df2\u5b89\u88c5 {installed}\uff0c" if installed else ""
        result["message"] = (
            f"{installed_detail}\u4f46\u65e0\u6cd5\u4ece npm registry \u83b7\u53d6 {channel_detail} \u7684\u7248\u672c\u4fe1\u606f\u3002"
            "\u8bf7\u68c0\u67e5\u7f51\u7edc\u3001\u4ee3\u7406\u6216 npm registry \u914d\u7f6e\u540e\u91cd\u8bd5\u3002"
        )
        result["error"] = "npm registry \u7248\u672c\u67e5\u8be2\u5931\u8d25"
        return result
    if missing:
        result["message"] = (
            f"\u672a\u68c0\u6d4b\u5230 Pi\u3002\u5f53\u524d Node.js {node_version}\uff0c\u5c06\u5b89\u88c5 {channel_detail}"
            f"\uff08\u76ee\u6807 {latest}\uff09\u3002"
        )
        return result
    if outdated:
        result["message"] = (
            f"\u5df2\u5b89\u88c5 Pi {installed}\uff0c{channel_detail} \u6700\u65b0\u4e3a {latest}\uff0c\u5efa\u8bae\u5347\u7ea7\u3002"
        )
        return result

    result["ok"] = True
    result["message"] = (
        f"Pi \u5df2\u5c31\u7eea\uff08{installed}\uff0c{channel_detail} \u6700\u65b0 {latest}\uff1b"
        f"Node.js {node_version}\uff0cnpm {npm_version}\uff09"
    )
    return result



def pi_update_state(status: dict[str, Any]) -> str:
    """Classify a needs_pi_install_or_update() result into a UI state."""
    if status.get("check_failed"):
        return "check_failed"
    if status.get("blocked"):
        return "blocked"
    if status.get("missing"):
        return "missing"
    if status.get("runtime_broken") or status.get("repair_required"):
        return "repair_required"
    if status.get("outdated"):
        return "outdated"
    if status.get("ok"):
        return "ok"
    return "unknown"



def check_pi_status() -> dict[str, Any]:
    """Run the Pi update check and persist a compact snapshot for the UI."""
    status = _core().needs_pi_install_or_update()
    from datetime import datetime

    snapshot = {
        "state": _core().pi_update_state(status),
        "installed": status.get("installed"),
        "latest": status.get("latest"),
        "channel": status.get("channel"),
        "message": str(status.get("message") or ""),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    def _apply(cfg: dict[str, Any]) -> Any:
        cfg["pi_update_status"] = snapshot
        cfg["last_update_check"] = snapshot["checked_at"]
        return cfg

    _core().update_manager_config(_apply)
    return status



def load_pi_update_status() -> dict[str, Any]:
    return dict(_core().load_manager_config().get("pi_update_status") or {})



def is_update_dismissed(kind: str, version: str) -> bool:
    """True when the user dismissed this update before (same version)."""
    if not version:
        return False
    key = f"{kind}@{str(version).strip()}"
    return key in {str(x) for x in (_core().load_manager_config().get("dismissed_updates") or [])}



def dismiss_update(kind: str, version: str) -> None:
    """Remember the user dismissed this kind@version so it stops nagging."""
    if not version:
        return
    key = f"{kind}@{str(version).strip()}"

    def _apply(cfg: dict[str, Any]) -> Any:
        entries = [str(x) for x in (cfg.get("dismissed_updates") or [])]
        if key in entries:
            return storage.UNCHANGED
        entries.append(key)
        cfg["dismissed_updates"] = entries
        return cfg

    _core().update_manager_config(_apply)



def install_or_update_pi(timeout: float = 300) -> tuple[int, str, str]:
    """Install the Node-compatible Pi channel and verify the resulting CLI."""
    node_version = _core().get_node_version()
    npm_version = _core().get_npm_version()
    channel = _core().select_pi_install_channel(node_version)
    if not node_version:
        return 2, "", "\u672a\u68c0\u6d4b\u5230 Node.js\uff1b\u8bf7\u5148\u5b89\u88c5 Node.js 20.6 \u6216\u66f4\u9ad8\u7248\u672c\u3002"
    if parse_semver(node_version) < PI_LEGACY_MIN_NODE or not channel:
        return (
            2,
            "",
            f"\u5f53\u524d Node.js {node_version} \u8fc7\u4f4e\uff1b\u8bf7\u5347\u7ea7\u5230 20.6 \u6216\u66f4\u9ad8\u7248\u672c\uff08\u63a8\u8350 22.19+\uff09\u3002",
        )
    if not npm_version:
        return 2, "", "\u672a\u68c0\u6d4b\u5230\u53ef\u7528\u7684 npm\uff1b\u8bf7\u4fee\u590d Node.js/npm \u5b89\u88c5\u3002"

    package_spec = _core().pi_package_spec(channel)
    target_version = _core().get_latest_pi_version(timeout=min(timeout, 30), tag=channel)
    if not target_version:
        return (
            3,
            "",
            f"\u65e0\u6cd5\u4ece npm registry \u83b7\u53d6 {package_spec} \u7684\u7248\u672c\u4fe1\u606f\uff1b\u672a\u6267\u884c\u5b89\u88c5\u3002",
        )

    command = _core()._npm_command("install", "-g", str(package_spec))
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            env=_core().strip_pyinstaller_runtime_env(os.environ),
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as exc:
        return 1, "", str(exc)
    stdout = process.stdout or ""
    stderr = process.stderr or ""
    if process.returncode != 0:
        return process.returncode, stdout, stderr

    runtime = _core().get_pi_runtime_status()
    installed = runtime.get("installed")
    if not runtime.get("ok") or not installed:
        detail = str(runtime.get("error") or "npm \u5b89\u88c5\u5b8c\u6210\uff0c\u4f46 Pi \u4ecd\u65e0\u6cd5\u542f\u52a8\u3002")
        return 4, stdout, (stderr + "\n" + detail).strip()
    if parse_semver(str(installed)) < parse_semver(target_version):
        detail = (
            f"npm \u5df2\u5b89\u88c5 {package_spec} {target_version}\uff0c\u4f46 PATH \u4e2d\u5b9e\u9645\u8fd0\u884c\u7684 Pi \u4ecd\u4e3a "
            f"{installed}\u3002\u8bf7\u68c0\u67e5\u65e7\u7684 pi \u547d\u4ee4\u6216 npm \u5168\u5c40 bin \u8def\u5f84\u3002"
        )
        return 5, stdout, (stderr + "\n" + detail).strip()

    verified = (
        f"\u5df2\u9a8c\u8bc1 Pi {installed}\uff08{channel} \u901a\u9053\uff0c\u76ee\u6807 {target_version}\uff1b"
        f"Node.js {node_version}\uff0cnpm {npm_version}\uff09"
    )
    return 0, (stdout.rstrip() + ("\n" if stdout.strip() else "") + verified + "\n"), stderr
