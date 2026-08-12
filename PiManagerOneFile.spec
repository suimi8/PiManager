# -*- mode: python ; coding: utf-8 -*-
"""Windows 主产物：onefile 单文件版（双击即用，无需随附依赖目录）。

Windows 只发布这一种产物形态（dist/PiManager.exe）；macOS / Linux 使用
PiManager.spec（目录版 / .app）。CI 在 build.yml 中按平台选择 spec。

共享构建配置（hiddenimports、datas、Qt 裁剪、图标与 APP_VERSION 提取）位于
scripts/pyi_common.py；Windows EXE 版本资源由 scripts/pyi_version_info.py 生成。
"""
from __future__ import annotations

import sys
from pathlib import Path

project_root = Path(SPECPATH)
sys.path.insert(0, str(project_root / "scripts"))

import pyi_common
import pyi_version_info

APP_VERSION = pyi_common.read_app_version(project_root)
datas = pyi_common.build_datas(project_root)
hiddenimports = pyi_common.build_hiddenimports()
icon = pyi_common.resolve_icon(project_root)

a = Analysis(
    ["main.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(project_root / "scripts" / "pyi_rth_pimanager.py")],
    excludes=pyi_common.EXCLUDES,
    noarchive=False,
    optimize=0,
)

# Widgets-only app: mirror PiManager.spec's Qt trimming (Qml/Quick/Pdf/
# VirtualKeyboard runtimes and non zh/en translations).
a.binaries = pyi_common.trim_qt(a.binaries)
a.datas = pyi_common.trim_qt(a.datas)

pyz = PYZ(a.pure)

exe_kwargs = dict(
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
if sys.platform == "win32":
    # Windows EXE version resource (FileVersion / ProductVersion ...) from
    # the single source of truth pi_manager/extras.py -> APP_VERSION.
    exe_kwargs["version"] = pyi_version_info.write_version_file(
        APP_VERSION, project_root
    )

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    **exe_kwargs,
)
