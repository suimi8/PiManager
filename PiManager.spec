# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform directory build (recommended).

Windows / Linux  -> dist/PiManager/
macOS            -> dist/PiManager.app  (via BUNDLE)
"""
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

project_root = Path(SPECPATH)
datas = [(str(project_root / "assets"), "assets")]
datas += collect_data_files("certifi")

hiddenimports = [
    "keyring.backends",
    "cryptography",
    "pi_manager.platform_util",
    "pi_manager.resources",
    "pi_manager.extras",
    "pi_manager.secrets",
    "pi_manager.storage",
    "pi_manager.provider_env",
    "pi_manager.ui_features",
    "pi_manager.help_docs",
    "pi_manager.ui_theme",
    "pi_manager.builtin_themes",
]

if sys.platform == "win32":
    hiddenimports += ["keyring.backends.Windows"]
    icon = str(project_root / "assets" / "pi-manager.ico")
elif sys.platform == "darwin":
    hiddenimports += ["keyring.backends.macOS", "keyring.backends.chainer"]
    # Prefer .icns if present; PNG is acceptable fallback for recent PyInstaller.
    icns = project_root / "assets" / "pi-manager.icns"
    icon = str(icns if icns.exists() else project_root / "assets" / "icon.png")
else:
    hiddenimports += ["keyring.backends.SecretService", "keyring.backends.chainer"]
    icon = None

a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PiManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=sys.platform == "darwin",
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PiManager",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="PiManager.app",
        icon=icon,
        bundle_identifier="com.suimi8.pimanager",
        info_plist={
            "CFBundleName": "PiManager",
            "CFBundleDisplayName": "PiManager",
            "CFBundleShortVersionString": "1.6.0",
            "CFBundleVersion": "1.6.0",
            "NSHighResolutionCapable": True,
        },
    )
