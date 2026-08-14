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
