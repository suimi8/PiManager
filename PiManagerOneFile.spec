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
    "pi_manager.config_broker",
    "pi_manager.helper_registry",
    "pi_manager.rpc_session",
    "pi_manager.ui_features",
    "pi_manager.help_docs",
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
    excludes=[
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtQuickWidgets",
        "PySide6.QtQuick3D",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
        "PySide6.QtVirtualKeyboard",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtCharts",
        "PySide6.QtMultimedia",
        "PySide6.QtMultimediaWidgets",
    ],
    noarchive=False,
    optimize=0,
)

# Widgets-only app: mirror PiManager.spec's Qt trimming (Qml/Quick/Pdf/
# VirtualKeyboard runtimes and non zh/en translations).
_QT_TRIM_TAGS = (
    "Qt6Pdf",
    "Qt6Qml",
    "Qt6Quick",
    "Qt6VirtualKeyboard",
    "QtPdf",
    "QtQml",
    "QtQuick",
    "QtVirtualKeyboard",
    "qtvirtualkeyboard",
)
_QM_KEEP_SUFFIXES = ("_zh_CN.qm", "_zh_TW.qm", "_en.qm")


def _trim_qt(toc):
    kept = []
    for entry in toc:
        name = str(entry[0]).replace("\\", "/")
        base = name.rsplit("/", 1)[-1]
        if any(tag in base for tag in _QT_TRIM_TAGS):
            continue
        if "translations/" in name and base.endswith(".qm"):
            if not base.endswith(_QM_KEEP_SUFFIXES):
                continue
        kept.append(entry)
    return kept


a.binaries = _trim_qt(a.binaries)
a.datas = _trim_qt(a.datas)

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
