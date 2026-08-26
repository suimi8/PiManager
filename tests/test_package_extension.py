from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from pi_manager import config_broker, core
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
    dist = repository / "dist"
    dist.mkdir(parents=True)
    external_cwd.mkdir()
    # Windows 只发布便携单文件版：PyInstaller 的产物就是 dist/PiManager.exe 本身。
    (dist / "PiManager.exe").write_bytes(b"desktop-package")

    monkeypatch.setattr(package_release, "REPO_ROOT", repository)
    monkeypatch.chdir(external_cwd)
    monkeypatch.setattr(
        sys,
        "argv",
        ["package_release.py", "--platform", "windows", "--version", "9.8.7"],
    )

    assert package_release.main() == 0
    archive = (
        repository / "release-assets" / "PiManager-v9.8.7-windows-x64-onefile.zip"
    )
    assert archive.is_file()
    # 解压即得单个自包含 exe（归档根目录不留 PiManager/ 子目录）。
    with zipfile.ZipFile(archive) as zf:
        assert zf.namelist() == ["PiManager.exe"]
        assert zf.read("PiManager.exe") == b"desktop-package"
    assert (repository / "release-assets" / "RUN-windows.txt").is_file()
    assert not (external_cwd / "release-assets").exists()


def test_desktop_release_windows_rejects_legacy_directory_layout(monkeypatch, tmp_path):
    repository = tmp_path / "repository"
    # 旧的目录版布局（dist/PiManager/PiManager.exe）已不是合法的 Windows 产物，
    # 必须干净失败而不是打出一个空归档。
    legacy = repository / "dist" / "PiManager"
    legacy.mkdir(parents=True)
    (legacy / "PiManager.exe").write_bytes(b"legacy-dir-package")

    monkeypatch.setattr(package_release, "REPO_ROOT", repository)
    monkeypatch.setattr(
        sys,
        "argv",
        ["package_release.py", "--platform", "windows", "--version", "9.8.7"],
    )

    assert package_release.main() == 1
    assert not list((repository / "release-assets").glob("*.zip"))


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


def test_config_mutate_bootstraps_when_token_file_missing(isolated_home):
    assert not config_broker.broker_token_path().exists()
    # First call creates the token but must still require it — an empty
    # token is no longer accepted (security fix: prevent race on first run).
    result_no_token = config_broker.mutate(
        {
            "schema_version": 1,
            "request_id": "bootstrap-1",
            "operation": "set_manager_fields",
            "arguments": {"fields": {"favorites": ["A/m"]}},
        }
    )
    assert result_no_token["ok"] is False
    token_path = config_broker.broker_token_path()
    assert token_path.is_file()
    if os.name != "nt":
        assert token_path.stat().st_mode & 0o077 == 0
    # Now use the created token to perform the mutation.
    token = token_path.read_text(encoding="utf-8").strip()
    result = config_broker.mutate(
        {
            "schema_version": 1,
            "request_id": "bootstrap-2",
            "token": token,
            "operation": "set_manager_fields",
            "arguments": {"fields": {"favorites": ["A/m"]}},
        }
    )
    assert result["ok"] is True
    assert core.load_manager_config()["favorites"] == ["A/m"]


def test_config_mutate_rejects_wrong_token_and_accepts_correct(isolated_home):
    token = config_broker._create_broker_token()
    denied = config_broker.mutate(
        {
            "schema_version": 1,
            "request_id": "denied-1",
            "token": "wrong-token-value",
            "operation": "set_manager_fields",
            "arguments": {"fields": {"favorites": ["B/m"]}},
        }
    )
    assert denied["ok"] is False
    assert "token" in denied["error"]
    assert core.load_manager_config()["favorites"] == []

    accepted = config_broker.mutate(
        {
            "schema_version": 1,
            "request_id": "accepted-1",
            "token": token,
            "operation": "set_manager_fields",
            "arguments": {"fields": {"favorites": ["B/m"]}},
        }
    )
    assert accepted["ok"] is True
    assert core.load_manager_config()["favorites"] == ["B/m"]
