"""Frozen onefile bookkeeping must not leak into children.

A packaged PiManager copies ``_PYI_*`` into ``os.environ``. If those names
reach ``pi`` / a terminal / ``PiManager.exe --print-provider-env``, PyInstaller
6.22+ treats the helper as a worker of the GUI instance and aborts when the
real parent is Cursor or node.
"""
from __future__ import annotations

from pi_manager import proc
from pi_manager.core_process import (
    is_pyinstaller_runtime_key,
    strip_pyinstaller_runtime_env,
)


def test_runtime_key_detection() -> None:
    assert is_pyinstaller_runtime_key("_PYI_ARCHIVE_FILE")
    assert is_pyinstaller_runtime_key("_PYI_APPLICATION_HOME_DIR")
    assert is_pyinstaller_runtime_key("PYINSTALLER_RESET_ENVIRONMENT")
    assert not is_pyinstaller_runtime_key("PATH")
    assert not is_pyinstaller_runtime_key("PI_MANAGER_PROVIDER_X_API_KEY")


def test_strip_drops_bootloader_vars_and_keeps_secrets() -> None:
    cleaned = strip_pyinstaller_runtime_env(
        {
            "PATH": "/bin",
            "KEEP": "1",
            "_PYI_ARCHIVE_FILE": r"E:\dist\PiManager.exe",
            "_PYI_APPLICATION_HOME_DIR": r"C:\Temp\_MEI123",
            "_PYI_PARENT_PROCESS_LEVEL": "1",
            "PYINSTALLER_RESET_ENVIRONMENT": "1",
            "PI_MANAGER_PROVIDER_X_API_KEY": "sk-test",
        }
    )
    assert cleaned == {
        "PATH": "/bin",
        "KEEP": "1",
        "PI_MANAGER_PROVIDER_X_API_KEY": "sk-test",
    }


def test_spawn_env_strips_inherited_pyi_vars(monkeypatch) -> None:
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", r"E:\dist\PiManager.exe")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")
    monkeypatch.setenv("KEEP_SPAWN", "ok")

    env = proc.spawn_env({"EXTRA": "1"})

    assert "KEEP_SPAWN" in env
    assert env["EXTRA"] == "1"
    assert "_PYI_ARCHIVE_FILE" not in env
    assert "_PYI_PARENT_PROCESS_LEVEL" not in env
    assert "PYINSTALLER_RESET_ENVIRONMENT" not in env


def test_spawn_env_strips_after_extra_merge(monkeypatch) -> None:
    monkeypatch.delenv("_PYI_ARCHIVE_FILE", raising=False)
    env = proc.spawn_env({"_PYI_ARCHIVE_FILE": r"E:\dist\PiManager.exe", "KEEP": "1"})
    assert env["KEEP"] == "1"
    assert "_PYI_ARCHIVE_FILE" not in env


def test_spawn_env_rpc_path_strips_ambient(monkeypatch) -> None:
    monkeypatch.setenv("_PYI_APPLICATION_HOME_DIR", r"C:\Temp\_MEI123")
    env = proc.spawn_env({"PROVIDER_KEY": "sk-test"}, sanitize_after_merge=False)
    assert env["PROVIDER_KEY"] == "sk-test"
    assert "_PYI_APPLICATION_HOME_DIR" not in env
