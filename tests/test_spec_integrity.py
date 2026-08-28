# -*- coding: utf-8 -*-
"""Static integrity checks between the PyInstaller specs and the source tree.

Read-only by design: never runs PyInstaller packaging. Verifies that both
spec files share the common configuration module (scripts/pyi_common.py),
that the collected hiddenimports cover every module currently exported by
pi_manager/presentation/pages, and that the generated Windows version
resource parses (when PyInstaller is available).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SPEC_NAMES = ("PiManager.spec", "PiManagerOneFile.spec")

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import pyi_common  # noqa: E402
import pyi_version_info  # noqa: E402


def _page_modules() -> list[str]:
    """Modules currently exported by pi_manager/presentation/pages (read-only)."""
    init = (
        REPO_ROOT
        / "pi_manager"
        / "presentation"
        / "pages"
        / "__init__.py"
    ).read_text(encoding="utf-8")
    modules = sorted(set(re.findall(r"^from \.(\w+) import", init, flags=re.M)))
    assert modules, "no page modules parsed from pages/__init__.py"
    return modules


def test_pyi_common_is_importable_without_pyinstaller() -> None:
    # Module-level imports in pyi_common are stdlib only, so importing it
    # must never require PyInstaller (it is a build-time dependency).
    assert pyi_common.PROJECT_ROOT == REPO_ROOT
    version = pyi_common.read_app_version(REPO_ROOT)
    assert re.fullmatch(r"\d+\.\d+(\.\d+)?", version), version


def test_specs_parse_as_python() -> None:
    for name in SPEC_NAMES:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        compile(text, name, "exec")  # in-memory parse only; no disk writes


def test_specs_share_common_config() -> None:
    for name in SPEC_NAMES:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "pyi_common" in text, f"{name} does not use the shared config module"
        assert "build_hiddenimports" in text, f"{name} does not share hiddenimports"


def _runtime_hook_literals(text: str) -> list[str]:
    """String literals appearing inside the spec's ``runtime_hooks=[...]``."""
    match = re.search(r"runtime_hooks\s*=\s*\[(.*?)\]", text, flags=re.S)
    assert match, "spec has no runtime_hooks= argument"
    return re.findall(r'"([^"]+)"', match.group(1))


def test_specs_reference_no_missing_runtime_hook() -> None:
    """Every runtime hook a spec names must exist under scripts/.

    B-3: ``scripts/pyi_rth_pimanager.py`` was dead code — PyInstaller runs
    custom rthooks first and the bundled ``pyi_rth_pyside6.py`` then assigns
    ``QT_PLUGIN_PATH`` unconditionally, wiping it out. It was removed; this
    guard keeps a spec from pointing at a hook file that is not there.
    """
    for name in SPEC_NAMES:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        literals = _runtime_hook_literals(text)
        assert "pyi_rth_pimanager.py" not in literals, (
            f"{name} re-added the dead PySide6 QT_PLUGIN_PATH hook"
        )
        for literal in literals:
            if not literal.endswith(".py"):
                continue
            assert (REPO_ROOT / "scripts" / literal).is_file(), (
                f"{name} references a missing runtime hook: {literal}"
            )
    assert not (REPO_ROOT / "scripts" / "pyi_rth_pimanager.py").exists()


def test_qt_trim_drops_the_dangling_pdf_image_plugin() -> None:
    """B-11: plugins/imageformats/qpdf.* has no Qt6Pdf tag in its basename."""
    assert "qpdf" in pyi_common.QT_TRIM_TAGS
    toc = [
        ("PySide6/plugins/imageformats/qpdf.dll", "/abs/qpdf.dll", "BINARY"),
        ("PySide6/plugins/imageformats/libqpdf.so", "/abs/libqpdf.so", "BINARY"),
        ("PySide6/Qt6Pdf.dll", "/abs/Qt6Pdf.dll", "BINARY"),
        ("PySide6/plugins/imageformats/qpng.dll", "/abs/qpng.dll", "BINARY"),
        ("PySide6/plugins/imageformats/qsvg.dll", "/abs/qsvg.dll", "BINARY"),
        ("PySide6/Qt6Svg.dll", "/abs/Qt6Svg.dll", "BINARY"),
        ("PySide6/translations/qtbase_zh_CN.qm", "/abs/qtbase_zh_CN.qm", "DATA"),
        ("PySide6/translations/qtbase_de.qm", "/abs/qtbase_de.qm", "DATA"),
    ]
    kept = {entry[0] for entry in pyi_common.trim_qt(toc)}
    assert "PySide6/plugins/imageformats/qpdf.dll" not in kept
    assert "PySide6/plugins/imageformats/libqpdf.so" not in kept
    assert "PySide6/Qt6Pdf.dll" not in kept
    assert "PySide6/translations/qtbase_de.qm" not in kept
    # Icon rendering must survive the trim (self_check now renders an SVG).
    assert "PySide6/plugins/imageformats/qpng.dll" in kept
    assert "PySide6/plugins/imageformats/qsvg.dll" in kept
    assert "PySide6/Qt6Svg.dll" in kept
    assert "PySide6/translations/qtbase_zh_CN.qm" in kept


def test_asset_datas_skip_dev_files_but_keep_plugin_payloads() -> None:
    """B-12: assets/README.md and assets/_gen_logo.py must not ship."""
    entries = pyi_common.collect_asset_datas(REPO_ROOT)
    assert entries, "no asset datas collected"
    sources = {Path(src).name for src, _dest in entries}
    assert "README.md" not in sources
    assert "_gen_logo.py" not in sources
    # Real payloads and their destination mapping stay intact.
    by_rel = {
        (Path(src).relative_to(REPO_ROOT / "assets").as_posix(), dest)
        for src, dest in entries
    }
    assert ("icon.png", "assets") in by_rel
    assert ("icons/home.svg", "assets/icons") in by_rel
    assert ("builtin/manifest.json", "assets/builtin") in by_rel
    assert any(
        rel.startswith("builtin/skills/document-processing/scripts/") and rel.endswith(".py")
        for rel, _dest in by_rel
    ), "builtin skill scripts were filtered out"
    assert not any("__pycache__" in rel for rel, _dest in by_rel)


def test_hiddenimports_cover_current_presentation_pages() -> None:
    hidden = set(pyi_common.build_hiddenimports())
    for mod in _page_modules():
        full = f"pi_manager.presentation.pages.{mod}"
        assert full in hidden, f"hiddenimports missing current page module: {full}"


def test_hiddenimports_keep_explicit_core_entries() -> None:
    hidden = set(pyi_common.build_hiddenimports())
    for entry in (
        "pi_manager.core",
        "pi_manager.ui",
        "pi_manager.extras",
        "pi_manager.secrets",
        "pi_manager.storage",
        "pi_manager.help_docs",
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
    ):
        assert entry in hidden, f"hiddenimports missing explicit entry: {entry}"


def test_specs_resolve_version_from_extras() -> None:
    for name in SPEC_NAMES:
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert "read_app_version" in text, f"{name} does not use read_app_version()"
    version = pyi_common.read_app_version(REPO_ROOT)
    extras = (REPO_ROOT / "pi_manager" / "extras.py").read_text(encoding="utf-8")
    assert f'APP_VERSION = "{version}"' in extras


def test_windows_version_resource_parses(tmp_path) -> None:
    version = pyi_common.read_app_version(REPO_ROOT)
    path = Path(pyi_version_info.write_version_file(version, tmp_path))
    text = path.read_text(encoding="utf-8")
    assert "VSVersionInfo(" in text and "FixedFileInfo(" in text
    assert f"FileVersion', '{version}'" in text
    pytest.importorskip("PyInstaller")
    # PyInstaller.utils.win32 requires pefile, which is only installed on
    # Windows; skip elsewhere so cross-platform CI stays green.
    pytest.importorskip("pefile")
    from PyInstaller.utils.win32 import versioninfo

    info = versioninfo.load_version_info_from_text_file(str(path))
    major, minor = (int(p) for p in version.split(".")[:2])
    fv = info.ffi.fileVersionMS >> 16, info.ffi.fileVersionMS & 0xFFFF
    assert fv == (major, minor)
