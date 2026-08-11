from __future__ import annotations

import os
import subprocess

from pi_manager import core
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


def test_launch_drops_unreachable_proxy_vars(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(platform_util, "is_windows", lambda: True)
    monkeypatch.setattr(platform_util, "is_macos", lambda: False)
    monkeypatch.setattr(platform_util.shutil, "which", lambda name: None)
    monkeypatch.setattr(
        core,
        "proxy_reachable",
        lambda url, timeout=0.4: False,
    )
    monkeypatch.setattr(
        platform_util.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    platform_util.launch_in_terminal(
        ["pi"],
        str(tmp_path),
        terminal="auto",
        env={"HTTPS_PROXY": "http://127.0.0.1:1", "KEEP": "1"},
    )

    assert calls
    assert "HTTPS_PROXY" not in calls[0][1]["env"]
    assert calls[0][1]["env"]["KEEP"] == "1"


def test_launch_keeps_reachable_proxy_vars(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(platform_util, "is_windows", lambda: True)
    monkeypatch.setattr(platform_util, "is_macos", lambda: False)
    monkeypatch.setattr(platform_util.shutil, "which", lambda name: None)
    monkeypatch.setattr(core, "proxy_reachable", lambda url, timeout=0.4: True)
    monkeypatch.setattr(
        platform_util.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    platform_util.launch_in_terminal(
        ["pi"],
        str(tmp_path),
        terminal="auto",
        env={"HTTPS_PROXY": "http://127.0.0.1:7890"},
    )

    assert calls
    assert calls[0][1]["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7890"


def test_launch_macos_wrapper_omits_unreachable_proxy(monkeypatch, tmp_path):
    calls = []
    wrapper = tmp_path / "pi-manager-launch-test.sh"
    monkeypatch.setattr(platform_util, "is_windows", lambda: False)
    monkeypatch.setattr(platform_util, "is_macos", lambda: True)
    monkeypatch.setattr(core, "proxy_reachable", lambda url, timeout=0.4: False)

    def fake_mkstemp(prefix, suffix):
        fd = os.open(str(wrapper), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        return fd, str(wrapper)

    monkeypatch.setattr(os, "fchmod", lambda fd, mode: None, raising=False)
    monkeypatch.setattr(platform_util.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(
        platform_util.subprocess,
        "Popen",
        lambda args, **kwargs: calls.append((args, kwargs)),
    )

    platform_util.launch_in_terminal(
        ["pi"],
        str(tmp_path),
        terminal="auto",
        env={"HTTPS_PROXY": "http://127.0.0.1:1", "KEEP": "1"},
    )

    content = wrapper.read_text(encoding="utf-8")
    assert "HTTPS_PROXY" not in content
    assert "export KEEP=1" in content
    assert calls


def test_open_path_returns_false_without_opener(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_util, "is_windows", lambda: False)
    monkeypatch.setattr(platform_util, "is_macos", lambda: False)
    monkeypatch.setattr(
        platform_util.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()),
    )

    assert platform_util.open_path(str(tmp_path)) is False


def test_open_path_windows_opens_existing_dir(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(platform_util, "is_windows", lambda: True)
    monkeypatch.setattr(platform_util, "is_macos", lambda: False)
    monkeypatch.setattr(
        platform_util.os, "startfile", lambda p: calls.append(str(p)), raising=False
    )

    assert platform_util.open_path(str(tmp_path)) is True
    assert calls == [str(tmp_path)]


def test_decode_session_folder_slug_windows_drive_styles(monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "win32")
    # 旧版 Pi：盘符冒号编码为 --，如 --C--Users-suimi-Desktop-app--
    assert (
        core._decode_session_folder_slug("--C--Users-suimi-Desktop-app--")
        == r"C:\Users\suimi\Desktop\app"
    )
    # 新版 Pi（0.84.1）：冒号同样编码为单横线，如 --C-Users-suimi-Desktop-app--
    assert (
        core._decode_session_folder_slug("--C-Users-suimi-Desktop-app--")
        == r"C:\Users\suimi\Desktop\app"
    )
    # 目录名本身含 --（连字符）不额外损坏
    assert (
        core._decode_session_folder_slug("--D--Users-my--app--")
        == r"D:\Users\my--app"
    )


def test_decode_session_folder_slug_posix_does_not_use_drive_rule(monkeypatch):
    monkeypatch.setattr(core.sys, "platform", "linux")
    assert core._decode_session_folder_slug("--home-suimi-my-app--") == "/home/suimi/my/app"
    assert core._decode_session_folder_slug("--C-Users-x--") == "/C/Users/x"


def test_decode_session_folder_slug_passthrough():
    assert core._decode_session_folder_slug("plain") == "plain"
    assert core._decode_session_folder_slug("") == ""
    assert core._decode_session_folder_slug("--") == "--"
