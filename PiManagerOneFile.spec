# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

project_root = Path(SPECPATH)
datas = [(str(project_root / "assets"), "assets")]
datas += collect_data_files("certifi")


a = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "keyring.backends",
        "keyring.backends.Windows",
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
    ],
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
    a.binaries,
    a.datas,
    [],
    name="PiManager",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project_root / "assets" / "pi-manager.ico"),
)
