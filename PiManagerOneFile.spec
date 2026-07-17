# -*- mode: python ; coding: utf-8 -*-
"""Cross-platform onefile build (slower first launch). Windows release secondary option."""
from __future__ import annotations

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

project_root = Path(SPECPATH)
datas = [(str(project_root / "assets"), "assets")]
datas += collect_data_files("certifi")
try:
    datas += collect_data_files("keyring")
except Exception:
    pass

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
    "pi_manager.core",
    "pi_manager.ui",
    "pi_manager.presentation",
    "pi_manager.presentation.main_window",
    "pi_manager.presentation.design.stylesheet",
    "pi_manager.presentation.components.navigation",
    "pi_manager.presentation.pages.dashboard",
    "pi_manager.presentation.pages.models",
    "pi_manager.presentation.pages.providers",
    "pi_manager.presentation.pages.chat",
    "pi_manager.presentation.pages.sessions",
    "pi_manager.presentation.pages.diagnostics",
    "pi_manager.presentation.pages.settings",
    "pi_manager.presentation.pages.help",
]

# Include every modular presentation page in frozen builds.
hiddenimports += collect_submodules("pi_manager.presentation")
try:
    hiddenimports += collect_submodules("keyring.backends")
except Exception:
    pass

if sys.platform == "win32":
    hiddenimports += [
        "keyring.backends.Windows",
        "win32timezone",
        "pythoncom",
        "pywintypes",
    ]
    icon = str(project_root / "assets" / "pi-manager.ico")
elif sys.platform == "darwin":
    hiddenimports += ["keyring.backends.macOS", "keyring.backends.chainer", "keyring.backends.fail"]
    icns = project_root / "assets" / "pi-manager.icns"
    icon = str(icns if icns.exists() else project_root / "assets" / "icon.png")
else:
    hiddenimports += [
        "keyring.backends.SecretService",
        "keyring.backends.chainer",
        "keyring.backends.fail",
        "jeepney",
        "secretstorage",
    ]
    icon = None

a = Analysis(
    ["main.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(project_root / "scripts" / "pyi_rth_pimanager.py")],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
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
