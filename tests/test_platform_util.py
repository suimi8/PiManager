from __future__ import annotations

import subprocess

from pi_manager import platform_util


def test_windows_terminal_launch_passes_pi_arguments_directly(monkeypatch, tmp_path):
    wt = tmp_path / "wt.exe"
    wt.touch()
    calls = []
    argv = [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Users\user\AppData\Roaming\npm\node_modules\@earendil-works\pi-coding-agent\dist\cli.js",
        "--append-system-prompt",
        "first line\nsecond line",
    ]
    workdir = str(tmp_path / "project with spaces")

    monkeypatch.setattr(
        platform_util.shutil,
        "which",
        lambda name: str(wt) if name == "wt" else None,
    )
    monkeypatch.setattr(
        platform_util.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    result = platform_util._launch_windows(argv, workdir, "wt", {"TOKEN": "secret"})

    assert calls == [
        (
            [str(wt), "-d", workdir, *argv],
            {"cwd": workdir, "env": {"TOKEN": "secret"}},
        )
    ]
    assert result.startswith("Windows Terminal:")


def test_cmd_launch_creates_console_without_nested_shell(monkeypatch, tmp_path):
    calls = []
    argv = [
        r"C:\Program Files\nodejs\node.exe",
        r"C:\Users\user\AppData\Roaming\npm\node_modules\@earendil-works\pi-coding-agent\dist\cli.js",
        "--provider",
        "provider with spaces",
    ]
    workdir = str(tmp_path / "project with spaces")

    monkeypatch.setattr(
        platform_util.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    result = platform_util._launch_windows(argv, workdir, "cmd", {"TOKEN": "secret"})

    assert calls == [
        (
            argv,
            {
                "cwd": workdir,
                "env": {"TOKEN": "secret"},
                "creationflags": getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            },
        )
    ]
    assert result.startswith("cmd:")
