# -*- coding: utf-8 -*-
"""Cross-platform helpers for Pi Manager (Windows / macOS / Linux)."""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Iterable


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


def open_path(path: str | Path, *, select_if_file: bool = False) -> None:
    p = Path(path).expanduser()
    if not p.exists():
        try:
            if p.suffix:
                p.parent.mkdir(parents=True, exist_ok=True)
            else:
                p.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    if is_windows():
        if select_if_file and p.is_file():
            subprocess.Popen(["explorer", "/select,", str(p)])
        else:
            os.startfile(str(p if p.exists() else p.parent))  # type: ignore[attr-defined]
        return

    if is_macos():
        if select_if_file and p.is_file():
            subprocess.Popen(["open", "-R", str(p)])
        else:
            target = p if p.exists() else p.parent
            subprocess.Popen(["open", str(target)])
        return

    target = p if p.is_dir() or not p.exists() else (p.parent if select_if_file and p.is_file() else p)
    target_s = str(target if target.exists() else p.parent)
    for args in (["xdg-open", target_s], ["gio", "open", target_s]):
        try:
            subprocess.Popen(args)
            return
        except FileNotFoundError:
            continue
    raise FileNotFoundError("未找到 xdg-open，无法打开路径")


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
        return which

    for root in npm_global_roots():
        candidates = []
        if is_windows():
            candidates = [root / "pi.cmd", root / "pi.ps1", root / "pi"]
        else:
            candidates = [root / "bin" / "pi", root / "pi"]
        for p in candidates:
            if p.is_file():
                return str(p)

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
    workdir = str(Path(workdir).expanduser())
    Path(workdir).mkdir(parents=True, exist_ok=True)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
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
            subprocess.Popen(
                [wt, "-d", workdir, "cmd", "/k", cmdline_cmd],
                cwd=workdir,
                env=env,
            )
            return f"Windows Terminal: {cmdline_cmd}"
        mode = "cmd"

    if mode == "cmd":
        subprocess.Popen(
            [
                "cmd.exe",
                "/c",
                "start",
                "Pi Coding Agent",
                "cmd.exe",
                "/k",
                f"cd /d {cmd_quote(workdir)} && {cmdline_cmd}",
            ],
            cwd=workdir,
            env=env,
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
            "Bypass",
            "-Command",
            ps,
        ],
        cwd=workdir,
        env=env,
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
    )
    return f"PowerShell: {cmdline_ps}"


def _launch_macos(argv: list[str], workdir: str, mode: str, env: dict[str, str]) -> str:
    changed_env = {
        key: value for key, value in env.items() if os.environ.get(key) != value
    }
    if changed_env:
        # Terminal.app may be an already-running app and therefore not inherit
        # the caller's environment. Keep the secret out of AppleScript and the
        # visible command line: a mode-0700 wrapper self-deletes on start.
        fd, wrapper_name = tempfile.mkstemp(prefix="pi-manager-", suffix=".sh")
        wrapper = Path(wrapper_name)
        try:
            lines = ["#!/bin/sh", "set -eu"]
            for key, value in changed_env.items():
                if not key or not key.replace("_", "").isalnum():
                    continue
                lines.append(f"export {key}={shlex.quote(value)}")
            lines.extend(
                [
                    'rm -f -- "$0" 2>/dev/null || true',
                    f"cd {shlex.quote(workdir)}",
                    "exec " + " ".join(shlex.quote(a) for a in argv),
                    "",
                ]
            )
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write("\n".join(lines))
            os.chmod(wrapper, 0o700)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            wrapper.unlink(missing_ok=True)
            raise
        cmd = "sh " + shlex.quote(str(wrapper))
    else:
        cmd = "cd " + shlex.quote(workdir) + " && " + " ".join(shlex.quote(a) for a in argv)
    cmd_keep = cmd + "; echo; echo '[Pi Manager] session ended — press enter to close'; read _"

    if mode == "iterm":
        script = (
            'tell application "iTerm"\n'
            "  if (count of windows) = 0 then create window with default profile\n"
            "  tell current session of current window\n"
            f"    write text {cmd_keep!r}\n"
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

    script = f'tell application "Terminal" to do script {cmd_keep!r}'
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
