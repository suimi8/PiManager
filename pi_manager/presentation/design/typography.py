"""Typography helpers shared by modern widgets."""
from __future__ import annotations

import sys


def ui_font_family() -> str:
    if sys.platform == "darwin":
        return '"SF Pro Text", "PingFang SC", "Helvetica Neue", sans-serif'
    if sys.platform == "win32":
        return '"Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", sans-serif'
    return '"Inter", "Noto Sans CJK SC", "Noto Sans", sans-serif'


def mono_font_family() -> str:
    if sys.platform == "darwin":
        return '"SF Mono", Menlo, monospace'
    if sys.platform == "win32":
        return '"Cascadia Mono", Consolas, monospace'
    return '"JetBrains Mono", "DejaVu Sans Mono", monospace'
