#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""版本单一来源 + 文档单一来源一致性检查（零第三方依赖）。

依据 ``AGENTS.md`` / ``docs/DEVELOPMENT_STANDARDS.md`` 的「单一来源」红线：

- 桌面应用版本：``pi_manager/extras.py`` 的 ``APP_VERSION``（唯一权威）。
- Cursor 扩展版本：``extensions/pi-cursor/package.json`` 的 ``version``（独立权威）。
- 使用教程正文：``pi_manager/help_docs.py`` 的 ``_HELP_MARKDOWN``（唯一权威），
  ``docs/使用教程.md`` 是由它生成的产物。

本脚本校验：

1. ``APP_VERSION`` 存在且为合法 SemVer。
2. ``docs/发布说明.md`` / ``docs/使用教程.md`` 顶部版本与 ``APP_VERSION`` 一致。
3. 扩展 ``package.json`` 版本存在且为合法 SemVer。
4. ``README.md`` / ``BUILD.md`` 里的**操作性版本引用**（``--version X.Y.Z``、
   ``vX.Y.Z``、``version：X.Y.Z``）与 ``APP_VERSION`` 一致。
   —— 这一项曾经只写在 docstring 里而 ``main()`` 从未实现，直接导致 BUILD.md 的
   版本示例停在 1.8.1、漂移 5 个版本无人发现（审查 D7/D8）。``docs/发布说明.md``
   刻意不做此项：它是变更日志，历史版本号必须保留。
5. ``docs/使用教程.md`` 与 ``pi_manager/help_docs.py`` 逐字一致。
   —— 二者曾是同一份教程的两个手工副本、已双向漂移（审查 G2）：应用内帮助页有
   插件/识图/Provider 模板整节而文档没有，FAQ 编号还错位。现在文档由代码生成，
   漂移在 CI 上即刻可见。

用法::

    python scripts/check_versions.py           # 退出码 0 = 一致；1 = 不一致
    python scripts/check_versions.py --write    # 按 help_docs.py 重新生成使用教程

与 ``tests/test_plugin_standards.py`` / ``tests/test_help_docs.py`` 中的断言配套；
CI 的 ``consistency`` job 与 ``build.yml`` 的 ``gate`` job 强制执行本脚本。
"""
from __future__ import annotations

import argparse
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

# 顶部版本必须与 APP_VERSION 一致的文档。
_TOP_VERSION_DOCS = ("docs/发布说明.md", "docs/使用教程.md")

# 全文操作性版本引用必须与 APP_VERSION 一致的文档（不含变更日志类文档）。
_OPERATIONAL_DOCS = ("README.md", "BUILD.md")

# 只匹配「这是本应用的版本」的上下文，避免误伤 Python 3.12 / Ubuntu 22.04 之类。
_OPERATIONAL_VERSION_RES = (
    re.compile(r"--version\s+`?v?(\d+\.\d+\.\d+)`?"),
    re.compile(r"\bv(\d+\.\d+\.\d+)\b"),
    re.compile(r"version`?\s*[：:]\s*`?v?(\d+\.\d+\.\d+)`?"),
)

# 使用教程的单一来源与生成产物。
_TUTORIAL_SOURCE = "pi_manager/help_docs.py"
_TUTORIAL_DOC = "docs/使用教程.md"
_GENERATED_HEADER = (
    "<!-- 本文件由 pi_manager/help_docs.py 的 HELP_MARKDOWN 自动生成，请勿手工编辑。 -->\n"
    "<!-- 改内容请改 pi_manager/help_docs.py，再运行：python scripts/check_versions.py --write -->\n"
    "\n"
)
_HELP_MARKDOWN_RE = re.compile(r"_HELP_MARKDOWN\s*=\s*r'''(.*?)'''", re.DOTALL)


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


def _operational_version_mentions(text: str) -> dict[str, list[int]]:
    """返回 {版本号: [行号, ...]}，只收「操作性引用」。"""
    found: dict[str, list[int]] = {}
    for lineno, line in enumerate(text.splitlines(), start=1):
        for pattern in _OPERATIONAL_VERSION_RES:
            for match in pattern.finditer(line):
                found.setdefault(match.group(1), []).append(lineno)
    return found


def _expected_tutorial(app: str) -> str:
    """由 help_docs.py 的 HELP_MARKDOWN 生成 docs/使用教程.md 的期望内容。

    刻意用正则从源码文本里抠出 raw 字符串，而不是 ``import pi_manager.help_docs``：
    本脚本必须保持零第三方依赖（help_docs 会连带 import cryptography 等运行时依赖），
    这样它在只有标准库的环境里也能作为门禁运行。
    """
    source = (REPO_ROOT / _TUTORIAL_SOURCE).read_text(encoding="utf-8")
    match = _HELP_MARKDOWN_RE.search(source)
    if not match:
        raise SystemExit(f"ERROR: {_TUTORIAL_SOURCE} 未找到 _HELP_MARKDOWN 定义")
    body = match.group(1).replace("__APP_VERSION__", app)
    return _GENERATED_HEADER + body.lstrip("\n")


def _normalize(text: str) -> str:
    return text.replace("\r\n", "\n")


def _write_tutorial(app: str) -> bool:
    """重新生成使用教程；返回是否发生了改动。"""
    path = REPO_ROOT / _TUTORIAL_DOC
    expected = _expected_tutorial(app)
    current = _normalize(path.read_text(encoding="utf-8")) if path.exists() else None
    if current == expected:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(expected)
    return True


def _check(app: str, errors: list[str]) -> None:
    ext = _extension_version()
    if not _SEMVER_RE.match(app):
        errors.append(f"APP_VERSION 不是合法 SemVer: {app!r}")
    if not _SEMVER_RE.match(ext):
        errors.append(f"扩展版本不是合法 SemVer: {ext!r}")

    for rel in _TOP_VERSION_DOCS:
        path = REPO_ROOT / rel
        if not path.exists():
            errors.append(f"缺少文档: {rel}")
            continue
        doc_version = _doc_top_version(path)
        if doc_version is None:
            errors.append(f"{rel} 前 12 行未找到版本号")
        elif doc_version != app:
            errors.append(f"{rel} 版本 {doc_version} 与 APP_VERSION {app} 不一致")

    for rel in _OPERATIONAL_DOCS:
        path = REPO_ROOT / rel
        if not path.exists():
            errors.append(f"缺少文档: {rel}")
            continue
        for version, linenos in sorted(_operational_version_mentions(path.read_text("utf-8")).items()):
            if version != app:
                lines = "、".join(f"L{n}" for n in sorted(set(linenos)))
                errors.append(
                    f"{rel} 的版本引用 {version}（{lines}）与 APP_VERSION {app} 不一致"
                )

    tutorial = REPO_ROOT / _TUTORIAL_DOC
    if tutorial.exists():
        expected = _expected_tutorial(app)
        if _normalize(tutorial.read_text(encoding="utf-8")) != expected:
            errors.append(
                f"{_TUTORIAL_DOC} 与单一来源 {_TUTORIAL_SOURCE} 不一致"
                "（请运行 python scripts/check_versions.py --write 重新生成）"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description="版本与文档单一来源一致性检查")
    parser.add_argument(
        "--write",
        action="store_true",
        help=f"按 {_TUTORIAL_SOURCE} 重新生成 {_TUTORIAL_DOC}（其余项仍只检查）",
    )
    args = parser.parse_args()

    app = _app_version()
    if args.write:
        changed = _write_tutorial(app)
        print(f"{'已重新生成' if changed else '无需改动'}：{_TUTORIAL_DOC}")

    errors: list[str] = []
    _check(app, errors)
    if errors:
        print("版本 / 文档一致性检查失败：")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(
        f"OK：桌面 {app} · 扩展 {_extension_version()} · "
        "文档版本引用一致 · 使用教程与 help_docs.py 同步。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
