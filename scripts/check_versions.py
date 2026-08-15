#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本单一来源一致性检查（零第三方依赖）。

依据 ``AGENTS.md`` / ``docs/DEVELOPMENT_STANDARDS.md`` 的「版本单一来源」红线：

- 桌面应用版本：``pi_manager/extras.py`` 的 ``APP_VERSION``（唯一权威）。
- Cursor 扩展版本：``extensions/pi-cursor/package.json`` 的 ``version``（独立权威）。
- 文档引用：``docs/发布说明.md``、``docs/使用教程.md`` 顶部版本必须与
  ``APP_VERSION`` 一致；``README.md`` 中的 ``vX.Y.Z`` 引用不应落后于权威版本。

本脚本校验：

1. ``APP_VERSION`` 存在且为合法 SemVer。
2. ``发布说明.md`` / ``使用教程.md`` 顶部版本与 ``APP_VERSION`` 一致。
3. 扩展 ``package.json`` 版本存在且为合法 SemVer。

用法::

    python scripts/check_versions.py        # 退出码 0 = 一致；1 = 不一致

与 ``tests/test_plugin_standards.py`` 中的版本断言配套；CI 的
``consistency`` job 强制执行本脚本。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# CI Windows runner 的 stdout 默认 cp1252，中文输出会触发 UnicodeEncodeError；
# 统一强制 UTF-8（errors=replace 兜底），保证跨平台一致。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]

_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)(\.(0|[1-9]\d*)){2}"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _app_version() -> str:
    extras = (REPO_ROOT / "pi_manager" / "extras.py").read_text(encoding="utf-8")
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', extras)
    if not match:
        raise SystemExit("ERROR: pi_manager/extras.py 缺少 APP_VERSION")
    return match.group(1)


def _extension_version() -> str:
    package = json.loads(
        (REPO_ROOT / "extensions" / "pi-cursor" / "package.json").read_text(encoding="utf-8")
    )
    version = str(package.get("version") or "")
    if not version:
        raise SystemExit("ERROR: extensions/pi-cursor/package.json 缺少 version")
    return version


def _doc_top_version(path: Path) -> str | None:
    text = path.read_text(encoding="utf-8")
    head = "\n".join(text.splitlines()[:12])
    match = re.search(r"v?(\d+\.\d+\.\d+)", head)
    return match.group(1) if match else None


def main() -> int:
    errors: list[str] = []
    app = _app_version()
    if not _SEMVER_RE.match(app):
        errors.append(f"APP_VERSION 不是合法 SemVer: {app!r}")

    ext = _extension_version()
    if not _SEMVER_RE.match(ext):
        errors.append(f"扩展版本不是合法 SemVer: {ext!r}")

    for rel in ("docs/发布说明.md", "docs/使用教程.md"):
        path = REPO_ROOT / rel
        if not path.exists():
            errors.append(f"缺少文档: {rel}")
            continue
        doc_version = _doc_top_version(path)
        if doc_version is None:
            errors.append(f"{rel} 前 12 行未找到版本号")
        elif doc_version != app:
            errors.append(
                f"{rel} 版本 {doc_version} 与 APP_VERSION {app} 不一致"
            )

    if errors:
        print("版本一致性检查失败：")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"OK：桌面 {app} · 扩展 {ext} · 文档引用一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
