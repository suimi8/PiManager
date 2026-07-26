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


def apply_app_font(app) -> None:
    """Set application-wide QFont with platform-friendly defaults."""
    try:
        from PySide6.QtGui import QFont

        if sys.platform == "darwin":
            family = ".AppleSystemUIFont"
            size = 13
        elif sys.platform == "win32":
            family = "Segoe UI"
            size = 10  # Windows point size ~ visual 13px
        else:
            family = "Noto Sans"
            size = 10
        font = QFont(family, size)
        font.setStyleHint(QFont.SansSerif)
        font.setHintingPreference(QFont.PreferDefaultHinting)
        app.setFont(font)
    except Exception:
        pass
