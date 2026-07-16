from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from scripts import package_extension, package_release


def _prepare_package_run(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    extension_dir = tmp_path / "extension"
    extension_dir.mkdir()
    (extension_dir / "package.json").write_text(
        '{"version": "9.8.7"}', encoding="utf-8"
    )
    output_dir = tmp_path / "output"

    monkeypatch.setattr(package_extension, "EXTENSION_DIR", extension_dir)
    monkeypatch.setattr(
        package_extension, "vsce_command", lambda _name: (["fake-vsce"], None)
    )
    monkeypatch.setattr(package_extension, "find_command", lambda *_args: "fake-npm")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "package_extension.py",
            "--out",
            str(output_dir),
            "--skip-tests",
        ],
    )
    target = output_dir / "pi-manager-pi-cursor-9.8.7.vsix"
    staged = output_dir / ".pi-manager-pi-cursor-9.8.7.staging.vsix"
    return target, staged


def test_relative_output_is_resolved_from_repository_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)

    assert package_extension.resolve_repo_path("release-assets") == (
        package_extension.REPO_ROOT / "release-assets"
    )


def test_desktop_release_defaults_are_resolved_from_repository_root(
    monkeypatch, tmp_path
):
    repository = tmp_path / "repository"
    external_cwd = tmp_path / "external"
    source = repository / "dist" / "PiManager"
    source.mkdir(parents=True)
    external_cwd.mkdir()
    (source / "PiManager.exe").write_bytes(b"desktop-package")

    monkeypatch.setattr(package_release, "REPO_ROOT", repository)
    monkeypatch.chdir(external_cwd)
    monkeypatch.setattr(
        sys,
        "argv",
        ["package_release.py", "--platform", "windows", "--version", "9.8.7"],
    )

    assert package_release.main() == 0
    assert (
        repository
        / "release-assets"
        / "PiManager-v9.8.7-windows-x64-dir.zip"
    ).is_file()
    assert (repository / "release-assets" / "RUN-windows.txt").is_file()
    assert not (external_cwd / "release-assets").exists()


def test_successful_package_atomically_replaces_existing_target(monkeypatch, tmp_path):
    target, staged = _prepare_package_run(monkeypatch, tmp_path)
    target.parent.mkdir()
    target.write_bytes(b"old-package")

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"new-package")

    monkeypatch.setattr(package_extension.subprocess, "run", fake_run)

    assert package_extension.main() == 0
    assert target.read_bytes() == b"new-package"
    assert not staged.exists()


def test_failed_package_preserves_existing_target_and_removes_staging(
    monkeypatch, tmp_path
):
    target, staged = _prepare_package_run(monkeypatch, tmp_path)
    target.parent.mkdir()
    target.write_bytes(b"known-good-package")

    def fake_run(command, **_kwargs):
        Path(command[-1]).write_bytes(b"partial-package")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(package_extension.subprocess, "run", fake_run)

    with pytest.raises(subprocess.CalledProcessError):
        package_extension.main()

    assert target.read_bytes() == b"known-good-package"
    assert not staged.exists()


def test_missing_staged_package_preserves_existing_target(monkeypatch, tmp_path):
    target, staged = _prepare_package_run(monkeypatch, tmp_path)
    target.parent.mkdir()
    target.write_bytes(b"known-good-package")
    monkeypatch.setattr(package_extension.subprocess, "run", lambda *_args, **_kwargs: None)

    assert package_extension.main() == 1
    assert target.read_bytes() == b"known-good-package"
    assert not staged.exists()
