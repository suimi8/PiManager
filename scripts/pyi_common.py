# -*- coding: utf-8 -*-
"""Shared PyInstaller spec configuration for PiManager.

Used by ``PiManager.spec`` and ``PiManagerOneFile.spec`` so that
hiddenimports, datas, Qt trimming and version/icon resolution live in a
single place instead of being copy-pasted between the two spec files.

Design rules:
- Module level imports are stdlib only; PyInstaller hooks are imported
  lazily inside functions so this module can be imported (and unit-tested)
  in environments without PyInstaller.
- The submodule enumeration falls back to stdlib ``pkgutil`` when
  PyInstaller is unavailable; results are identical for pure-Python
  packages such as ``pi_manager.presentation``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Project root: this file lives in <root>/scripts/.
PROJECT_ROOT = Path(__file__).resolve().parents[1]

_APP_VERSION_RE = re.compile(r'APP_VERSION\s*=\s*"([^"]+)"')

# Common excludes: this is a Widgets-only app; drop the QtQml/Quick chain,
# Pdf and VirtualKeyboard runtimes the PySide6 hook still bundles.
EXCLUDES = [
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
]

# Keep only Chinese/English Qt translations; opengl32sw.dll stays
# (software-GL fallback for VMs/remote desktops).
QT_TRIM_TAGS = (
    "Qt6Pdf",
    "Qt6Qml",
    "Qt6Quick",
    "Qt6VirtualKeyboard",
    "QtPdf",
    "QtQml",
    "QtQuick",
    "QtVirtualKeyboard",
    "qtvirtualkeyboard",
    # plugins/imageformats/qpdf.dll (libqpdf.so / .dylib) is the Qt6Pdf image
    # format plugin; its basename carries no Qt6Pdf/QtPdf tag, so it used to
    # survive as a dangling plugin whose load always failed
    # (docs/review/r2-build.md B-11).
    "qpdf",
)
QM_KEEP_SUFFIXES = ("_zh_CN.qm", "_zh_TW.qm", "_en.qm")

# Development-only files that live in assets/ but must not ship in a product
# (docs/review/r2-build.md B-12). Matched against the top-level entries of
# assets/ only, so plugin payloads such as
# assets/builtin/skills/*/scripts/extract_*.py are never touched.
ASSET_EXCLUDE_TOP_LEVEL = ("README.md",)
ASSET_EXCLUDE_TOP_LEVEL_PREFIXES = ("_",)


def read_app_version(project_root: Path = PROJECT_ROOT) -> str:
    """Extract APP_VERSION from pi_manager/extras.py (single source of truth)."""
    src = (project_root / "pi_manager" / "extras.py").read_text(encoding="utf-8")
    match = _APP_VERSION_RE.search(src)
    if not match:
        raise SystemExit("cannot extract APP_VERSION from pi_manager/extras.py")
    return match.group(1)


def _walk_package_submodules(package: str) -> list[str]:
    """Enumerate submodules of a pure-Python package using only stdlib."""
    import importlib
    import pkgutil

    pkg = importlib.import_module(package)
    return [
        info.name
        for info in pkgutil.walk_packages(pkg.__path__, prefix=package + ".")
    ]


def _collect_submodules(package: str) -> list[str]:
    """Prefer PyInstaller's collect_submodules, fall back to pkgutil."""
    try:
        from PyInstaller.utils.hooks import collect_submodules

        return list(collect_submodules(package))
    except Exception:
        return _walk_package_submodules(package)


def collect_asset_datas(project_root: Path = PROJECT_ROOT) -> list[tuple[str, str]]:
    """Enumerate assets/ file by file so dev-only files can be skipped.

    The plain ``(assets_dir, "assets")`` tuple form shipped ``assets/README.md``
    and ``assets/_gen_logo.py`` into every product (B-12). Filtering happens at
    the top level only; everything under assets/builtin/** is kept verbatim
    because builtin_plugins copies those payloads to ~/.pi/agent/.
    """
    assets = project_root / "assets"
    out: list[tuple[str, str]] = []
    for path in sorted(assets.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(assets)
        top = rel.parts[0]
        if len(rel.parts) == 1:
            if top in ASSET_EXCLUDE_TOP_LEVEL or top.startswith(ASSET_EXCLUDE_TOP_LEVEL_PREFIXES):
                continue
        if "__pycache__" in rel.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        dest = "assets" if len(rel.parts) == 1 else "assets/" + rel.parent.as_posix()
        out.append((str(path), dest))
    return out


def build_datas(project_root: Path = PROJECT_ROOT) -> list:
    from PyInstaller.utils.hooks import collect_data_files

    datas = list(collect_asset_datas(project_root))
    datas += collect_data_files("certifi")
    # Bundle package data that may be imported dynamically.
    try:
        datas += collect_data_files("keyring")
    except Exception:
        pass
    return datas


def build_hiddenimports() -> list:
    hiddenimports = [
        "keyring.backends",
        "cryptography",
        "yaml",
        "yaml._yaml",
        "pi_manager.platform_util",
        "pi_manager.resources",
        "pi_manager.extras",
        "pi_manager.extras_history",
        "pi_manager.extras_proxy",
        "pi_manager.extras_keys",
        "pi_manager.extras_bundle",
        "pi_manager.extras_selfcheck",
        "pi_manager.extras_update",
        "pi_manager.extras_sessions",
        "pi_manager.extras_chat",
        "pi_manager.secrets",
        "pi_manager.storage",
        "pi_manager.plugin_manager",
        "pi_manager.provider_env",
        "pi_manager.config_broker",
        "pi_manager.helper_registry",
        "pi_manager.rpc_session",
        "pi_manager.ui_features",
        "pi_manager.help_docs",
        "pi_manager.builtin_themes",
        "pi_manager.core",
        "pi_manager.core_paths",
        "pi_manager.core_store",
        "pi_manager.core_catalog",
        "pi_manager.core_launch",
        "pi_manager.core_prefs",
        "pi_manager.core_pi_runtime",
        "pi_manager.core_admin",
        "pi_manager.core_http",
        "pi_manager.core_process",
        "pi_manager.core_remote",
        "pi_manager.core_sessions",
        "pi_manager.core_credentials",
        "pi_manager.core_vision",
        "pi_manager.ui",
        "pi_manager.presentation",
        "pi_manager.presentation.app",
        "pi_manager.presentation.lifecycle",
        "pi_manager.presentation.shell",
        "pi_manager.presentation.main_window",
        "pi_manager.presentation.window_chrome",
        "pi_manager.presentation.window_updates",
        "pi_manager.presentation.window_overlays",
        "pi_manager.presentation.workers",
        "pi_manager.presentation.dialogs",
        "pi_manager.presentation.dialogs.providers",
        "pi_manager.presentation.dialogs.setup",
        "pi_manager.presentation.design.stylesheet",
        "pi_manager.presentation.components.navigation",
        "pi_manager.presentation.pages.dashboard",
        "pi_manager.presentation.pages.models",
        "pi_manager.presentation.pages.providers",
        "pi_manager.presentation.pages.chat",
        "pi_manager.presentation.pages.sessions",
        "pi_manager.presentation.pages.diagnostics",
        "pi_manager.presentation.pages.health",
        "pi_manager.presentation.pages.history",
        "pi_manager.presentation.pages.tools",
        "pi_manager.presentation.pages.settings",
        "pi_manager.presentation.pages.help",
        "pi_manager.presentation.pages.plugins",
        "pi_manager.presentation.pages.plugins.format",
        "pi_manager.presentation.pages.plugins.cards",
        "pi_manager.presentation.pages.plugins.ops",
        "pi_manager.presentation.pages.plugins.page",
    ]

    # Include every modular presentation page in frozen builds.
    hiddenimports += _collect_submodules("pi_manager.presentation")

    # Pull all keyring backends so frozen apps do not miss platform providers.
    try:
        hiddenimports += _collect_submodules("keyring.backends")
    except Exception:
        pass

    if sys.platform == "win32":
        hiddenimports += [
            "keyring.backends.Windows",
            "win32timezone",
            "pythoncom",
            "pywintypes",
        ]
    elif sys.platform == "darwin":
        hiddenimports += [
            "keyring.backends.macOS",
            "keyring.backends.chainer",
            "keyring.backends.fail",
        ]
    else:
        hiddenimports += [
            "keyring.backends.SecretService",
            "keyring.backends.chainer",
            "keyring.backends.fail",
            "keyring.backends.libsecret",
            "jeepney",
            "secretstorage",
        ]
    return hiddenimports


def resolve_icon(project_root: Path = PROJECT_ROOT):
    if sys.platform == "win32":
        return str(project_root / "assets" / "pi-manager.ico")
    if sys.platform == "darwin":
        icns = project_root / "assets" / "pi-manager.icns"
        return str(icns if icns.exists() else project_root / "assets" / "icon.png")
    return None


def trim_qt(toc):
    """Drop QtQml/Quick/Pdf/VirtualKeyboard runtimes and non zh/en .qm files."""
    kept = []
    for entry in toc:
        name = str(entry[0]).replace("\\", "/")
        base = name.rsplit("/", 1)[-1]
        if any(tag in base for tag in QT_TRIM_TAGS):
            continue
        if "translations/" in name and base.endswith(".qm"):
            if not base.endswith(QM_KEEP_SUFFIXES):
                continue
        kept.append(entry)
    return kept
