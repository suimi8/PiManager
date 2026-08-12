# -*- coding: utf-8 -*-
"""Cross-platform helpers for Pi Manager (Windows / macOS / Linux)."""
from __future__ import annotations

import logging
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)


def is_windows() -> bool:
    return sys.platform == "win32"


def is_macos() -> bool:
    return sys.platform == "darwin"


def is_linux() -> bool:
    return sys.platform.startswith("linux")


def platform_name() -> str:
    if is_windows():
        return "windows"
    if is_macos():
        return "macos"
    if is_linux():
        return "linux"
    return sys.platform


def subprocess_no_window_kwargs() -> dict:
    if is_windows():
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def open_path(path: str | Path, *, select_if_file: bool = False) -> bool:
    p = Path(path).expanduser()
    if not p.exists():
        try:
            if p.suffix:
                p.parent.mkdir(parents=True, exist_ok=True)
            else:
                p.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("创建路径失败: %s: %s", p, exc)
            return False

    if is_windows():
        try:
            if select_if_file and p.is_file():
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                os.startfile(str(p if p.exists() else p.parent))  # type: ignore[attr-defined]
        except OSError as exc:
            logger.warning("打开路径失败: %s: %s", p, exc)
            return False
        return True

    if is_macos():
        try:
            if select_if_file and p.is_file():
                subprocess.Popen(["open", "-R", str(p)])
            else:
                target = p if p.exists() else p.parent
                subprocess.Popen(["open", str(target)])
        except OSError as exc:
            logger.warning("打开路径失败: %s: %s", p, exc)
            return False
        return True

    target = p if p.is_dir() or not p.exists() else (p.parent if select_if_file and p.is_file() else p)
    target_s = str(target if target.exists() else p.parent)
    for args in (["xdg-open", target_s], ["gio", "open", target_s]):
        try:
            subprocess.Popen(args)
            return True
        except FileNotFoundError:
            continue
    logger.warning("未找到 xdg-open 或 gio，无法打开路径: %s", target_s)
    return False


def which_many(names: Iterable[str]) -> str | None:
    for name in names:
        hit = shutil.which(name)
        if hit:
            return hit
    return None


def npm_global_roots() -> list[Path]:
    roots: list[Path] = []
    home = Path.home()

    if is_windows():
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            roots.append(Path(appdata) / "npm")
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            roots.append(Path(local) / "npm")
    else:
        roots.extend(
            [
                home / ".npm-global",
                home / ".local",
                Path("/usr/local"),
                Path("/opt/homebrew"),
            ]
        )
        nvm = Path(os.environ.get("NVM_DIR", str(home / ".nvm"))) / "versions" / "node"
        if nvm.exists():
            try:
                for v in sorted(nvm.iterdir(), reverse=True)[:6]:
                    roots.append(v)
            except OSError:
                pass
        roots.append(home / ".volta")

    for key in ("npm_config_prefix", "NPM_CONFIG_PREFIX", "PREFIX"):
        val = os.environ.get(key)
        if val:
            roots.append(Path(val))

    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        s = str(r)
        if s and s not in seen:
            seen.add(s)
            out.append(r)
    return out


def find_pi_cli_js() -> Path | None:
    packages = (
        ("@earendil-works", "pi-coding-agent"),
        ("@mariozechner", "pi-coding-agent"),
    )
    for root in npm_global_roots():
        for scope, name in packages:
            candidates = [
                root / "node_modules" / scope / name / "dist" / "cli.js",
                root / "lib" / "node_modules" / scope / name / "dist" / "cli.js",
                root / "lib" / "node_modules" / scope / name / "dist" / "cli.js",
            ]
            for c in candidates:
                if c.is_file():
                    return c
    return None


def _is_safe_executable(path: str) -> bool:
    """POSIX 下校验可执行文件所有权与权限是否安全。

    - other-write 位被设置则视为不安全（他人可改写该可执行文件）。
    - 不属于当前用户、也不属于 root 时视为不安全。
    - 任何异常都返回 True，避免在校验不可用时阻塞主流程。
    """
    if is_windows():
        return True
    try:
        st = os.stat(path, follow_symlinks=False)
        if st.st_mode & stat.S_IWOTH:
            return False
        uid = os.getuid()
        if st.st_uid not in (uid, 0):
            return False
    except Exception:
        return True
    return True


def find_pi_command() -> str | None:
    which = shutil.which("pi")
    if which:
        if is_windows() and Path(which).suffix.lower() in {".cmd", ".bat", ".ps1"}:
            # npm's Windows shims require a command shell, which re-parses
            # prompts and system messages containing newlines or metacharacters.
            # Execute the package CLI with Node directly whenever available.
            cli = find_pi_cli_js()
            node = shutil.which("node")
            if cli is not None and node:
                return f"NODECLI::{node}::{cli}"
        if not _is_safe_executable(which):
            logger.warning("跳过不安全的 pi 可执行文件（其他用户可写或非本用户拥有）: %s", which)
        else:
            return which

    for root in npm_global_roots():
        candidates = []
        if is_windows():
            candidates = [root / "pi.cmd", root / "pi.ps1", root / "pi"]
        else:
            candidates = [root / "bin" / "pi", root / "pi"]
        for p in candidates:
            if p.is_file():
                ps = str(p)
                if not _is_safe_executable(ps):
                    logger.warning("跳过不安全的 pi 可执行文件（其他用户可写或非本用户拥有）: %s", ps)
                    continue
                return ps

    cli = find_pi_cli_js()
    if cli is not None:
        node = shutil.which("node") or "node"
        return f"NODECLI::{node}::{cli}"
    return None


def list_terminal_options() -> list[tuple[str, str]]:
    if is_windows():
        return [
            ("auto", "自动"),
            ("wt", "Windows Terminal"),
            ("powershell", "PowerShell"),
            ("cmd", "命令提示符 cmd"),
        ]
    if is_macos():
        return [
            ("auto", "自动"),
            ("terminal", "终端.app"),
            ("iterm", "iTerm2（若已安装）"),
        ]
    return [
        ("auto", "自动"),
        ("xdg", "系统默认终端"),
        ("gnome", "GNOME Terminal"),
        ("konsole", "Konsole"),
        ("xterm", "xterm"),
    ]


def _linux_terminal_prefix(mode: str = "auto") -> tuple[str, list[str]] | None:
    ordered: list[tuple[str, list[str]]] = []
    if mode == "gnome":
        ordered = [("gnome-terminal", ["--"])]
    elif mode == "konsole":
        ordered = [("konsole", ["-e"])]
    elif mode == "xterm":
        ordered = [("xterm", ["-e"])]
    else:
        ordered = [
            ("x-terminal-emulator", ["-e"]),
            ("gnome-terminal", ["--"]),
            ("kgx", ["-e"]),
            ("konsole", ["-e"]),
            ("xfce4-terminal", ["-e"]),
            ("mate-terminal", ["-e"]),
            ("tilix", ["-e"]),
            ("alacritty", ["-e"]),
            ("kitty", ["-e"]),
            ("xterm", ["-e"]),
        ]
    for name, extra in ordered:
        path = shutil.which(name)
        if path:
            return name, [path, *extra]
    return None


def launch_in_terminal(
    argv: list[str],
    workdir: str,
    terminal: str = "auto",
    env: dict[str, str] | None = None,
) -> str:
    from .core import proxy_reachable

    workdir = str(Path(workdir).expanduser())
    Path(workdir).mkdir(parents=True, exist_ok=True)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
        # Empty-string values explicitly remove a variable.
        for key, value in env.items():
            if value == "":
                full_env.pop(key, None)
    # A configured-but-stopped proxy makes every spawned session fail with
    # "Connection error"; drop unreachable proxy vars so the child connects
    # directly instead.
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = full_env.get(var)
        if value and not proxy_reachable(value):
            full_env.pop(var, None)
    mode = (terminal or "auto").lower()
    if is_windows():
        return _launch_windows(argv, workdir, mode, full_env)
    if is_macos():
        return _launch_macos(argv, workdir, mode, full_env)
    return _launch_linux(argv, workdir, mode, full_env)


def _launch_windows(argv: list[str], workdir: str, mode: str, env: dict[str, str]) -> str:
    def cmd_quote(a: str) -> str:
        if not a:
            return '""'
        if any(ch in a for ch in ' \t"&<>|^') or "@" in a:
            return '"' + a.replace('"', '""') + '"'
        return a

    def ps_quote(a: str) -> str:
        return "'" + a.replace("'", "''") + "'"

    cmdline_cmd = " ".join(cmd_quote(x) for x in argv)
    cmdline_ps = "& " + " ".join(ps_quote(x) for x in argv)

    if mode == "auto":
        wt = shutil.which("wt")
        wt_path = wt or str(Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "wt.exe")
        mode = "wt" if wt_path and Path(wt_path).exists() else "cmd"

    if mode in {"wt", "windows-terminal"}:
        wt = shutil.which("wt") or str(
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / "wt.exe"
        )
        if wt and Path(wt).exists():
            # Pass each Pi argument directly to Windows Terminal. Going through
            # cmd /k corrupts quoted paths and multiline system prompts.
            subprocess.Popen(
                [wt, "-d", workdir, *argv],
                cwd=workdir,
                env=env,
            )
            return f"Windows Terminal: {cmdline_cmd}"
        mode = "cmd"

    if mode == "cmd":
        # CREATE_NEW_CONSOLE provides the requested terminal without another
        # shell parsing pass. This also works when Windows delegates consoles
        # to Windows Terminal.
        subprocess.Popen(
            argv,
            cwd=workdir,
            env=env,
            creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        )
        return f"cmd: {cmdline_cmd}"

    ps = (
        f"Set-Location -LiteralPath {ps_quote(workdir)}; "
        f"Write-Host 'Starting Pi...' -ForegroundColor Cyan; "
        f"{cmdline_ps}"
    )
    subprocess.Popen(
        [
            "powershell.exe",
            "-NoExit",
            "-NoProfile",
            "-ExecutionPolicy",
            "RemoteSigned",
            "-Command",
            ps,
        ],
        cwd=workdir,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )
    return f"PowerShell: {cmdline_ps}"


def _applescript_string(text: str) -> str:
    """Render a strict AppleScript string literal (repr is not a safe stand-in)."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _launch_macos(argv: list[str], workdir: str, mode: str, env: dict[str, str]) -> str:
    changed_env = {
        key: value for key, value in env.items() if os.environ.get(key) != value
    }
    if changed_env:
        # Terminal.app may be an already-running app and therefore not inherit
        # the caller's environment. Keep the secret out of AppleScript and the
        # visible command line: a 0600 wrapper (invoked via sh, no exec bit
        # needed) self-deletes on start.
        # Use a private directory under the user config tree (~/.pi/agent/.tmp)
        # instead of the shared /tmp, where directory names could be enumerated
        # by other local users even with 0700 permissions.
        tmp_base = Path(os.path.expanduser("~")) / ".pi" / "agent" / ".tmp"
        try:
            tmp_base.mkdir(parents=True, exist_ok=True)
            private_dir = Path(tempfile.mkdtemp(prefix="pi-manager-", dir=str(tmp_base)))
        except Exception:
            # 创建私有基础目录失败（权限、跨平台差异等）时，回退到系统默认临时目录，
            # 不阻塞终端启动；后续 wrapper 仍受 O_EXCL / 0600 保护。
            private_dir = Path(tempfile.mkdtemp(prefix="pi-manager-"))
        wrapper = private_dir / "wrapper.sh"
        fd = os.open(str(wrapper), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            # mkstemp already creates 0600; clamp explicitly before writing so
            # no wider umask window exists, and never follow symlinks.
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
            lines = ["#!/bin/sh", "set -eu"]
            for key, value in changed_env.items():
                if not key or not key.replace("_", "").isalnum():
                    continue
                lines.append(f"export {key}={shlex.quote(value)}")
            lines.extend(
                [
                    'rm -f -- "$0" 2>/dev/null || true',
                    f'rmdir -- {shlex.quote(str(private_dir))} 2>/dev/null || true',
                    f"cd {shlex.quote(workdir)}",
                    "exec " + " ".join(shlex.quote(a) for a in argv),
                    "",
                ]
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(wrapper, 0o600)
            except OSError:
                pass
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            wrapper.unlink(missing_ok=True)
            private_dir.rmdir(missing_ok=True)
            raise
        # The wrapper self-deletes when it runs; if the terminal launch fails
        # it never runs, so a detached janitor removes the secret-bearing file
        # shortly afterwards either way.
        try:
            subprocess.Popen(
                ["/bin/sh", "-c", f"sleep 10; rm -f -- {shlex.quote(str(wrapper))}; rmdir -- {shlex.quote(str(private_dir))} 2>/dev/null || true"],
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass
        cmd = "sh " + shlex.quote(str(wrapper))
    else:
        cmd = "cd " + shlex.quote(workdir) + " && " + " ".join(shlex.quote(a) for a in argv)
    cmd_keep = cmd + "; echo; echo '[Pi Manager] session ended — press enter to close'; read _"

    if mode == "iterm":
        script = (
            'tell application "iTerm"\n'
            "  if (count of windows) = 0 then create window with default profile\n"
            "  tell current session of current window\n"
            f"    write text {_applescript_string(cmd_keep)}\n"
            "  end tell\n"
            "  activate\n"
            "end tell"
        )
        try:
            r = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                env=env,
            )
            if r.returncode == 0:
                return f"iTerm2: {cmd}"
        except Exception:
            pass
        mode = "terminal"

    script = f'tell application "Terminal" to do script {_applescript_string(cmd_keep)}'
    subprocess.Popen(["osascript", "-e", script], env=env)
    try:
        subprocess.Popen(
            ["osascript", "-e", 'tell application "Terminal" to activate'],
            env=env,
        )
    except Exception:
        pass
    return f"Terminal.app: {cmd}"


def _launch_linux(argv: list[str], workdir: str, mode: str, env: dict[str, str]) -> str:
    cmd = "cd " + shlex.quote(workdir) + " && " + " ".join(shlex.quote(a) for a in argv)
    inner = cmd + "; echo; echo '[Pi Manager] session ended — press enter to close'; read _"
    bash_cmd = ["bash", "-lc", inner]

    found = _linux_terminal_prefix(mode if mode not in {"auto", "xdg", "system"} else "auto")
    if found:
        name, prefix = found
        subprocess.Popen(prefix + bash_cmd, cwd=workdir, env=env)
        return f"{name}: {cmd}"

    subprocess.Popen(argv, cwd=workdir, env=env, start_new_session=True)
    return f"detached: {cmd}"
