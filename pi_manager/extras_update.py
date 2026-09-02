# -*- coding: utf-8 -*-
"""Pi Manager 自身更新检查（不执行原地安装）。

从 ``extras.py`` 下沉。``pi_manager.extras`` 继续 re-export，保持现有导入与
monkeypatch 点（``extras.xxx``）稳定。对会被测试 patch 的符号走 ``_extras().xxx``。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import core


def _extras():
    from . import extras

    return extras


# Optional remote version manifest (JSON: {"version":"x.y.z","notes":"...","url":"..."})
# 未配置时自动回退 GitHub Releases API
UPDATE_MANIFEST_URL = ""  # user can set in manager config
GITHUB_REPO = "suimi8/PiManager"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


def _http_get_json(url: str, *, timeout: int = 15) -> dict[str, Any]:
    import urllib.request

    from . import http_client

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"PiManager/{_extras().APP_VERSION}",
            "Accept": "application/vnd.github+json",
        },
    )
    opener = urllib.request.build_opener(http_client.DenyRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        body = http_client.read_limited(
            resp, http_client.MANIFEST_MAX_BYTES
        ).decode("utf-8", errors="replace")
    data = json.loads(body)
    return data if isinstance(data, dict) else {}


def _pick_release_asset(assets: list[dict[str, Any]]) -> dict[str, str]:
    """按当前平台挑选推荐下载资源。"""
    import sys

    names = [(str(a.get("name") or ""), str(a.get("browser_download_url") or "")) for a in assets]
    names = [(n, u) for n, u in names if n and u]
    prefer: list[str] = []
    if sys.platform == "win32":
        prefer = ["windows-x64-dir.zip", "windows-x64-onefile.zip", "windows"]
    elif sys.platform == "darwin":
        # Apple Silicon 优先 arm64，否则任意 macos
        prefer = ["macos-arm64.zip", "macos-x64.zip", "macos"]
    else:
        prefer = ["linux-x64.tar.gz", "linux"]
    for key in prefer:
        for n, u in names:
            if key in n.lower():
                return {"name": n, "url": u}
    if names:
        return {"name": names[0][0], "url": names[0][1]}
    return {"name": "", "url": ""}


def check_manager_update() -> dict[str, Any]:
    """Check the official release feed without trusting it for installation."""
    cfg = core.load_manager_config()
    settings = core.load_settings()
    url = ""
    manifest_url = str(cfg.get("update_manifest_url") or "").strip()
    if not manifest_url:
        manifest_url = str(settings.get("update_manifest_url") or "").strip()
    try:
        parsed = urlsplit(manifest_url)
        if parsed.scheme == "https" and parsed.hostname:
            url = manifest_url
    except ValueError:
        url = ""
    if not url:
        url = UPDATE_MANIFEST_URL
    local = _extras().APP_VERSION
    result: dict[str, Any] = {
        "ok": True,
        "local": local,
        "remote": None,
        "has_update": False,
        "notes": "",
        "url": url or GITHUB_RELEASES_PAGE,
        "download": "",
        "asset_name": "",
        "source": "",
        "message": f"当前版本 {local}",
    }
    cfg["last_manager_update_check"] = datetime.now().isoformat(timespec="seconds")
    core.save_manager_config(cfg)

    try:
        if url:
            data = _extras()._http_get_json(url)
            tag = str(
                data.get("version")
                or data.get("tag_name")
                or data.get("name")
                or ""
            ).strip()
            remote = tag.lstrip("vV")
            result["source"] = "manifest"
            result["remote"] = remote
            result["notes"] = str(data.get("notes") or data.get("body") or "")[:2000]
            result["url"] = str(data.get("url") or GITHUB_RELEASES_PAGE)
            result["download"] = ""
            result["asset_name"] = ""
        else:
            data = _extras()._http_get_json(GITHUB_RELEASES_API)
            tag = str(data.get("tag_name") or data.get("name") or "").strip()
            remote = tag.lstrip("vV")
            result["source"] = "github-notification-only"
            result["remote"] = remote
            result["notes"] = str(data.get("body") or "")[:2000]
            result["url"] = str(data.get("html_url") or GITHUB_RELEASES_PAGE)
            assets = data.get("assets") if isinstance(data.get("assets"), list) else []
            picked = _pick_release_asset([a for a in assets if isinstance(a, dict)])
            result["asset_name"] = picked.get("name") or ""
            result["download"] = ""

        remote = str(result.get("remote") or "")
        if remote and core.parse_semver(remote) > core.parse_semver(local):
            result["has_update"] = True
            asset = result.get("asset_name") or ""
            extra = f" · 推荐包 {asset}" if asset else ""
            result["message"] = f"发现新版本 v{remote}（当前 v{local}）{extra}"
        elif remote:
            result["message"] = f"已是最新（本地 v{local}，远程 v{remote}）"
        else:
            result["message"] = f"当前版本 v{local}（未能解析远程版本号）"
    except Exception as e:
        result["ok"] = False
        result["message"] = f"检查失败：{e}"
    cfg = core.load_manager_config()
    cfg["manager_update_status"] = {
        "state": "ok" if result.get("ok") else "failed",
        "local": result.get("local"),
        "remote": result.get("remote"),
        "has_update": bool(result.get("has_update")),
        "notes": str(result.get("notes") or "")[:2000],
        "url": str(result.get("url") or ""),
        "message": str(result.get("message") or ""),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    core.save_manager_config(cfg)
    return result


def _install_root() -> Path:
    """当前安装根目录（frozen）或源码根。"""
    import sys

    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        # macOS: .../PiManager.app/Contents/MacOS/PiManager
        if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
            return exe.parents[2]  # *.app
        return exe.parent
    return Path(__file__).resolve().parent.parent


def apply_manager_update_inplace(archive_path: str | Path) -> dict[str, Any]:
    """Reject in-place installation until signed package verification exists."""
    return {
        "ok": False,
        "need_exit": False,
        "message": "签名更新链尚未启用，已禁止原地安装。请从官方 Release 页面手动更新。",
    }
