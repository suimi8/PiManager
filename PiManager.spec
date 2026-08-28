# -*- mode: python ; coding: utf-8 -*-
"""macOS / Linux 构建：目录版 / .app（Windows 改用 PiManagerOneFile.spec 单文件版）。

Linux -> dist/PiManager/（目录版）
macOS -> dist/PiManager.app

Windows 只发布 onefile 单文件版（dist/PiManager.exe），见 PiManagerOneFile.spec
与 build.yml 的平台分支。

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

# Keep binaries portable: never UPX-compress Qt/cryptography natives.
a = Analysis(
    ["main.py"],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    # 无自定义 runtime hook：PyInstaller 把自定义 rthook 排在最前执行，随后
    # 官方 hooks/rthooks/pyi_rth_pyside6.py 会**无条件**赋值 QT_PLUGIN_PATH /
    # QML2_IMPORT_PATH，任何自定义设置都会被覆盖。旧的
    # scripts/pyi_rth_pimanager.py 因此是死代码，已删除
    # （docs/review/r2-build.md B-3 / B-14）。
    runtime_hooks=[],
    excludes=pyi_common.EXCLUDES,
    noarchive=False,
    optimize=0,
)

# This is a Widgets-only app: drop the QtQml/Quick chain, Pdf and
# VirtualKeyboard runtimes the PySide6 hook still bundles, and keep only
# Chinese/English Qt translations. opengl32sw.dll stays (software-GL
# fallback for VMs/remote desktops).
a.binaries = pyi_common.trim_qt(a.binaries)
a.datas = pyi_common.trim_qt(a.datas)

pyz = PYZ(a.pure)

exe_kwargs = dict(
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
if sys.platform == "win32":
    # Windows EXE version resource (FileVersion / ProductVersion ...) from
    # the single source of truth pi_manager/extras.py -> APP_VERSION.
    exe_kwargs["version"] = pyi_version_info.write_version_file(
        APP_VERSION, project_root
    )

exe = EXE(
    pyz,
    a.scripts,
    [],
    **exe_kwargs,
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
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
            "CFBundlePackageType": "APPL",
            "CFBundleExecutable": "PiManager",
            "LSMinimumSystemVersion": "12.0",
            "NSHighResolutionCapable": True,
            "NSRequiresAquaSystemAppearance": False,
            # Allow launching Terminal/iTerm helper tools from the GUI.
            "NSAppleEventsUsageDescription": "PiManager needs automation permission to open a terminal for Pi sessions.",
        },
    )
