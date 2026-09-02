from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

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

    # 无分号时转义是恒等变换：既有行为（含多行 system prompt）完全不变。
    assert calls == [
        (
            [str(wt), "-d", workdir, *argv],
            {"cwd": workdir, "env": {"TOKEN": "secret"}},
        )
    ]
    assert result.startswith("Windows Terminal:")


def test_windows_terminal_escapes_semicolons_in_arguments(monkeypatch, tmp_path):
    """wt.exe 把未转义的 `;` 当成子命令分隔符，参数会被就地截断。

    本机实测：`wt -d <dir> python probe.py "a;b" "c d"` 的子进程只收到 ["a"]，
    `"c d"` 整个丢失；`--` 分隔符无效，只有 `\\;` 转义能让参数原样送达。
    """
    wt = tmp_path / "wt.exe"
    wt.touch()
    calls = []
    argv = [r"C:\Program Files\nodejs\node.exe", "--append-system-prompt", "say a;b then c"]
    workdir = str(tmp_path / "proj;dir")

    monkeypatch.setattr(
        platform_util.shutil, "which", lambda name: str(wt) if name == "wt" else None
    )
    monkeypatch.setattr(
        platform_util.subprocess, "Popen", lambda args, **kwargs: calls.append((args, kwargs))
    )

    platform_util._launch_windows(argv, workdir, "wt", {})

    passed = calls[0][0]
    assert passed[2] == workdir.replace(";", "\\;")
    assert passed[-1] == "say a\\;b then c"
    # cwd 仍是未转义的真实路径（转义只针对 wt 的命令行解析）。
    assert calls[0][1]["cwd"] == workdir
    # 不含分号的参数不得被改动。
    assert passed[3] == argv[0]
    assert passed[4] == "--append-system-prompt"


def test_wt_escape_is_identity_without_semicolons():
    assert platform_util.wt_escape(r"C:\Users\x\pi.cmd") == r"C:\Users\x\pi.cmd"
    assert platform_util.wt_escape("line1\nline2") == "line1\nline2"
    assert platform_util.wt_escape("a;b;c") == "a\\;b\\;c"


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


def test_launch_strips_pyinstaller_runtime_vars(monkeypatch, tmp_path):
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
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", r"E:\dist\PiManager.exe")
    monkeypatch.setenv("_PYI_PARENT_PROCESS_LEVEL", "1")

    platform_util.launch_in_terminal(
        ["pi"],
        str(tmp_path),
        terminal="auto",
        env={"KEEP": "1"},
    )

    assert calls
    child_env = calls[0][1]["env"]
    assert child_env["KEEP"] == "1"
    assert "_PYI_ARCHIVE_FILE" not in child_env
    assert "_PYI_PARENT_PROCESS_LEVEL" not in child_env


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


def test_launch_macos_wrapper_omits_unreachable_proxy(monkeypatch, tmp_path, isolated_home):
    calls = []
    wrapper_dir = tmp_path / "pi-manager-wrapper-dir"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "wrapper.sh"
    monkeypatch.setattr(platform_util, "is_windows", lambda: False)
    monkeypatch.setattr(platform_util, "is_macos", lambda: True)
    monkeypatch.setattr(core, "proxy_reachable", lambda url, timeout=0.4: False)

    def fake_mkdtemp(prefix):
        return str(wrapper_dir)

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))
        class _FakeProc:
            returncode = 0
            pid = 12345
        return _FakeProc()

    monkeypatch.setattr(os, "fchmod", lambda fd, mode: None, raising=False)
    monkeypatch.setattr(platform_util.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(
        platform_util.subprocess,
        "Popen",
        fake_popen,
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


def test_launch_macos_wrapper_unsets_pyinstaller_runtime_vars(
    monkeypatch, tmp_path, isolated_home
):
    calls = []
    wrapper_dir = tmp_path / "pi-manager-wrapper-dir"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "wrapper.sh"
    monkeypatch.setattr(platform_util, "is_windows", lambda: False)
    monkeypatch.setattr(platform_util, "is_macos", lambda: True)
    monkeypatch.setattr(core, "proxy_reachable", lambda url, timeout=0.4: True)
    monkeypatch.setenv("_PYI_ARCHIVE_FILE", "/tmp/PiManager")
    monkeypatch.setenv("KEEP", "1")

    def fake_mkdtemp(prefix):
        return str(wrapper_dir)

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))

        class _FakeProc:
            returncode = 0
            pid = 12345

        return _FakeProc()

    monkeypatch.setattr(os, "fchmod", lambda fd, mode: None, raising=False)
    monkeypatch.setattr(platform_util.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(platform_util.subprocess, "Popen", fake_popen)

    platform_util.launch_in_terminal(
        ["pi"],
        str(tmp_path),
        terminal="auto",
        env={"KEEP": "1", "NEW_SECRET": "x"},
    )

    content = wrapper.read_text(encoding="utf-8")
    assert "unset _PYI_ARCHIVE_FILE" in content
    assert "export _PYI_ARCHIVE_FILE" not in content
    assert "export NEW_SECRET=x" in content
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
    monkeypatch.setattr(sys, "platform", "win32")
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
    monkeypatch.setattr(sys, "platform", "linux")
    assert core._decode_session_folder_slug("--home-suimi-my-app--") == "/home/suimi/my/app"
    assert core._decode_session_folder_slug("--C-Users-x--") == "/C/Users/x"


def test_decode_session_folder_slug_passthrough():
    assert core._decode_session_folder_slug("plain") == "plain"
    assert core._decode_session_folder_slug("") == ""
    assert core._decode_session_folder_slug("--") == "--"


def test_pi_cli_js_candidates_are_unique_and_complete():
    """候选恰好两条且无重复：node_modules 与 lib/node_modules。"""
    root = Path("prefix")
    cands = platform_util._pi_cli_js_candidates(root, "@scope", "name")
    assert len(cands) == len(set(cands))
    assert cands == [
        root / "node_modules" / "@scope" / "name" / "dist" / "cli.js",
        root / "lib" / "node_modules" / "@scope" / "name" / "dist" / "cli.js",
    ]


def test_find_pi_cli_js_finds_lib_node_modules_layout(monkeypatch, tmp_path):
    """仅存在 lib/node_modules 布局时也能定位 cli.js，且不扫系统 npm。"""
    cli = (
        tmp_path
        / "lib"
        / "node_modules"
        / "@earendil-works"
        / "pi-coding-agent"
        / "dist"
        / "cli.js"
    )
    cli.parent.mkdir(parents=True)
    cli.write_text("// cli\n", encoding="utf-8")
    monkeypatch.setattr(platform_util, "npm_global_roots", lambda: [tmp_path])
    assert platform_util.find_pi_cli_js() == cli


# ---------------------------------------------------------------------------
# _is_safe_executable：POSIX 可执行文件属主/权限校验
#
# 为什么要单独测：这段安全校验只在「pi CLI 已安装」时才会被 find_pi_command 调用，
# CI 上从来跑不到，等于长期没有任何验证。这里用 monkeypatch 直接驱动 POSIX 分支，
# 使它在任何平台上都能被独立验证（Windows 开发机同样跑得到）。
# ---------------------------------------------------------------------------
def _force_posix(monkeypatch, *, uid: int = 1000) -> None:
    monkeypatch.setattr(platform_util, "is_windows", lambda: False)
    # Windows 上 os.getuid 不存在，缺了它整段会走 except -> return True，
    # 于是所有断言都会「意外通过」，反而掩盖回归。
    monkeypatch.setattr(platform_util.os, "getuid", lambda: uid, raising=False)


def test_is_safe_executable_true_on_windows(monkeypatch, tmp_path):
    """Windows 上没有 POSIX 权限语义，一律视为安全（保持既有行为）。"""
    monkeypatch.setattr(platform_util, "is_windows", lambda: True)
    assert platform_util._is_safe_executable(str(tmp_path / "whatever")) is True


def test_is_safe_executable_rejects_other_writable(monkeypatch, tmp_path):
    """other-write 位被设置 = 同机任何用户都能改写这个可执行文件。"""
    target = tmp_path / "pi"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    _force_posix(monkeypatch)

    real_stat = platform_util.os.stat

    class FakeStat:
        st_mode = 0o100777  # 普通文件 + rwxrwxrwx（含 S_IWOTH）
        st_uid = 1000

    monkeypatch.setattr(
        platform_util.os,
        "stat",
        lambda path, **kw: FakeStat() if str(path) == str(target) else real_stat(path, **kw),
    )
    assert platform_util._is_safe_executable(str(target)) is False


def test_is_safe_executable_rejects_foreign_owner(monkeypatch, tmp_path):
    """既不属于当前用户也不属于 root 的可执行文件不可信。"""
    target = tmp_path / "pi"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    _force_posix(monkeypatch, uid=1000)

    real_stat = platform_util.os.stat

    class FakeStat:
        st_mode = 0o100755
        st_uid = 4242  # 别的账户

    monkeypatch.setattr(
        platform_util.os,
        "stat",
        lambda path, **kw: FakeStat() if str(path) == str(target) else real_stat(path, **kw),
    )
    assert platform_util._is_safe_executable(str(target)) is False


def test_is_safe_executable_accepts_current_user_and_root(monkeypatch, tmp_path):
    target = tmp_path / "pi"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    _force_posix(monkeypatch, uid=1000)
    real_stat = platform_util.os.stat

    for owner in (1000, 0):  # 当前用户 / root 都应通过

        class FakeStat:
            st_mode = 0o100755
            st_uid = owner

        monkeypatch.setattr(
            platform_util.os,
            "stat",
            lambda path, **kw: FakeStat() if str(path) == str(target) else real_stat(path, **kw),
        )
        assert platform_util._is_safe_executable(str(target)) is True


def test_is_safe_executable_fails_open_when_stat_unavailable(monkeypatch, tmp_path):
    """校验不可用时按文档 fail-open（返回 True），不阻塞主流程。

    这条断言把「fail-open」这个安全权衡固定成显式契约：将来若要改成 fail-closed，
    必须先改掉这个测试，从而迫使改动被评审。
    """
    _force_posix(monkeypatch)
    target = tmp_path / "pi"
    real_stat = platform_util.os.stat

    def fake_stat(path, **kwargs):
        # 只对目标路径失败：全局打桩 os.stat 会打坏 pytest 自身的 tmp_path 清理。
        if str(path) == str(target):
            raise PermissionError("denied")
        return real_stat(path, **kwargs)

    monkeypatch.setattr(platform_util.os, "stat", fake_stat)
    assert platform_util._is_safe_executable(str(target)) is True


def test_find_pi_command_skips_unsafe_executable(monkeypatch, tmp_path, caplog):
    """不安全的 pi 可执行文件必须被跳过（fail-closed）并留下 WARNING。

    这条用例把 _is_safe_executable 的返回值真正接到决策上：它是这段校验唯一的
    调用点，而该调用点只在「pi CLI 已安装」时才命中，CI 上从不执行。
    """
    target = tmp_path / "pi"
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        platform_util.shutil, "which", lambda name: str(target) if name == "pi" else None
    )
    monkeypatch.setattr(platform_util, "is_windows", lambda: False)
    monkeypatch.setattr(platform_util, "npm_global_roots", lambda: [])
    monkeypatch.setattr(platform_util, "find_pi_cli_js", lambda: None)
    monkeypatch.setattr(platform_util, "_is_safe_executable", lambda path: False)
    with caplog.at_level("WARNING", logger="pi_manager.platform_util"):
        assert platform_util.find_pi_command() is None
    assert any("不安全" in record.getMessage() for record in caplog.records)

    # 反面断言：安全时必须照常返回，确保上面的 None 不是别的原因导致的。
    monkeypatch.setattr(platform_util, "_is_safe_executable", lambda path: True)
    assert platform_util.find_pi_command() == str(target)


# ---------------------------------------------------------------------------
# _launch_macos 的失败处理器：清理动作绝不能替换原始异常
# ---------------------------------------------------------------------------
def test_launch_macos_failure_preserves_original_error(monkeypatch, isolated_home):
    """原本写成 private_dir.rmdir(missing_ok=True)（该参数不存在）。

    结果是：wrapper 写入失败（磁盘满/无权限/沙箱）时，处理器自身抛 TypeError，
    把真正的失败原因整个替换掉，`raise` 永不执行，而且 private_dir 目录还残留。
    这个 handler 只在 macOS 失败分支执行，CI 跑不到，所以必须显式驱动它。
    """
    created_dirs = []
    real_mkdtemp = platform_util.tempfile.mkdtemp

    def tracking_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created_dirs.append(path)
        return path

    monkeypatch.setattr(platform_util.tempfile, "mkdtemp", tracking_mkdtemp)
    # Windows 上 os.fchmod 不存在，不补桩会在到达目标分支前先抛 AttributeError。
    monkeypatch.setattr(platform_util.os, "fchmod", lambda fd, mode: None, raising=False)
    # 模拟「写 wrapper 时磁盘满」：fsync 在 with 块内，异常会走到 handler。
    monkeypatch.setattr(
        platform_util.os,
        "fsync",
        lambda fd: (_ for _ in ()).throw(OSError("no space left on device")),
    )

    with pytest.raises(OSError) as excinfo:
        platform_util._launch_macos(
            ["/usr/bin/true"],
            str(isolated_home),
            "terminal",
            {"PI_MANAGER_PROVIDER_X_API_KEY": "sk-secret"},
        )
    # 关键断言：抛出的必须是原始 OSError，而不是清理代码引入的 TypeError。
    assert not isinstance(excinfo.value, TypeError)
    assert "no space left on device" in str(excinfo.value)
    # 处理器必须跑完：wrapper 与私有目录都不残留（密钥不落盘）。
    assert created_dirs, "未创建私有临时目录，测试未覆盖目标分支"
    for path in created_dirs:
        assert not os.path.exists(os.path.join(path, "wrapper.sh"))
        assert not os.path.exists(path), "private_dir 残留（handler 中途抛异常的症状）"


def test_launch_macos_cleanup_survives_undeletable_directory(monkeypatch, isolated_home):
    """清理失败（目录非空/无权限）也不能覆盖原始异常。"""
    monkeypatch.setattr(platform_util.os, "fchmod", lambda fd, mode: None, raising=False)
    monkeypatch.setattr(
        platform_util.os,
        "fsync",
        lambda fd: (_ for _ in ()).throw(OSError("disk quota exceeded")),
    )
    monkeypatch.setattr(
        platform_util.Path,
        "rmdir",
        lambda self: (_ for _ in ()).throw(OSError("directory not empty")),
    )
    with pytest.raises(OSError, match="disk quota exceeded"):
        platform_util._launch_macos(
            ["/usr/bin/true"], str(isolated_home), "terminal", {"K": "v"}
        )
