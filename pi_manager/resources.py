# -*- coding: utf-8 -*-
"""Resolve bundled asset paths (dev + PyInstaller, all platforms)."""
from __future__ import annotations

import sys
from pathlib import Path


def _candidates_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        exe_dir = exe.parent
        roots.extend(
            [
                exe_dir,
                exe_dir / "assets",
                exe_dir / "_internal",
                exe_dir / "_internal" / "assets",
            ]
        )
        # macOS app bundle: Contents/MacOS/PiManager
        # assets may live under Contents/Resources or Contents/Frameworks
        if exe_dir.name == "MacOS":
            contents = exe_dir.parent
            roots.extend(
                [
                    contents / "Resources",
                    contents / "Resources" / "assets",
                    contents / "Frameworks",
                    contents / "Frameworks" / "assets",
                    contents / "MacOS" / "_internal" / "assets",
                ]
            )
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            mp = Path(meipass)
            roots.extend([mp, mp / "assets"])
    # source tree: pi_manager/resources.py -> ../assets
    here = Path(__file__).resolve().parent
    roots.extend([here.parent / "assets", here.parent, here / "assets"])
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        s = str(r)
        if s not in seen:
            seen.add(s)
            out.append(r)
    return out


def assets_dir() -> Path:
    for root in _candidates_roots():
        if root.name == "assets" and root.is_dir():
            return root
        if (root / "icon.png").exists() or (root / "pi-manager.ico").exists() or (root / "logo-256.png").exists():
            return root
        if (root / "assets").is_dir():
            return root / "assets"
    return Path(__file__).resolve().parent.parent / "assets"


def asset_path(*parts: str) -> Path | None:
    for root in _candidates_roots():
        p = root.joinpath(*parts)
        if p.exists():
            return p
        p2 = root / "assets"
        p = p2.joinpath(*parts)
        if p.exists():
            return p
    return None


def icon_candidates() -> list[Path]:
    names = [
        ("pi-manager.ico",),
        ("icon.png",),
        ("logo-256.png",),
        ("logo-512.png",),
        ("logo-1024.png",),
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for n in names:
        p = asset_path(*n)
        if p is not None and str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


def self_check() -> list[str]:
    """Return human-readable diagnostics. Empty list means OK for packaging smoke tests."""
    errors: list[str] = []
    # Critical imports for a frozen GUI build
    try:
        import PySide6  # noqa: F401
        from PySide6.QtCore import Qt  # noqa: F401
        from PySide6.QtWidgets import QApplication  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment specific
        errors.append(f"PySide6 import failed: {exc}")

    try:
        import certifi  # noqa: F401
    except Exception as exc:
        errors.append(f"certifi import failed: {exc}")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except Exception as exc:
        errors.append(f"cryptography import failed: {exc}")

    try:
        import keyring  # noqa: F401
    except Exception as exc:
        errors.append(f"keyring import failed: {exc}")

    try:
        from pi_manager import core, extras, secrets, platform_util, provider_env  # noqa: F401
        from pi_manager.presentation import ModernMainWindow  # noqa: F401
    except Exception as exc:
        errors.append(f"pi_manager package import failed: {exc}")

    icon = asset_path("icon.png") or asset_path("logo-256.png") or asset_path("pi-manager.ico")
    if icon is None:
        errors.append("bundled assets missing (icon.png / logo-256.png / pi-manager.ico)")
    required_ui_icons = ("home.svg", "models.svg", "providers.svg", "settings.svg")
    missing_ui_icons = [name for name in required_ui_icons if asset_path("icons", name) is None]
    if missing_ui_icons:
        errors.append(f"modern UI icons missing: {', '.join(missing_ui_icons)}")

    # Offscreen Qt app creation proves plugins are loadable without a display server
    # when QT_QPA_PLATFORM=offscreen (set by CI smoke tests).
    if not errors:
        try:
            from PySide6.QtWidgets import QApplication
            import os

            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            app = QApplication.instance() or QApplication(["PiManagerSelfCheck"])
            _ = app.applicationName()
        except Exception as exc:
            errors.append(f"Qt QApplication init failed: {exc}")
    return errors
