# -*- coding: utf-8 -*-
"""Resolve bundled asset paths (dev + PyInstaller)."""
from __future__ import annotations

import sys
from pathlib import Path


def _candidates_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        roots.extend(
            [
                exe_dir,
                exe_dir / "assets",
                exe_dir / "_internal",
                exe_dir / "_internal" / "assets",
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
