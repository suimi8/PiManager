# -*- coding: utf-8 -*-
"""进程管理：Pi 命令定位 / 终端选项 / 进程树终止 / 代理环境 / 一次性运行。

从 ``core.py`` 抽出的纯进程/网络工具，无配置状态依赖。
``core.py`` 在顶部重新导出这些符号以保持 ``core.xxx`` 调用兼容。
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading


def find_pi_command() -> str | None:
    """Return absolute path to pi launcher if possible (Windows / macOS / Linux)."""
    from . import platform_util as pu

    return pu.find_pi_command()


def pi_base_cmd() -> list[str]:
    raw = find_pi_command()
    if not raw:
        raise FileNotFoundError(
            "未找到 pi 命令。请先安装: npm install -g @earendil-works/pi-coding-agent"
        )
    if raw.startswith("NODECLI::"):
        parts = raw.split("::", 2)
        if len(parts) == 3:
            return [parts[1], parts[2]]
    if raw.startswith('"') and '" "' in raw:
        parts = re.findall(r'"([^"]+)"', raw)
        if len(parts) >= 2:
            return parts[:2]
    low = raw.lower()
    if low.endswith(".cmd") or low.endswith(".bat"):
        return ["cmd.exe", "/c", raw]
    if low.endswith(".ps1"):
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            raw,
        ]
    return [raw]


def escape_cmd_shim_args(
    args: list[str], base: list[str] | None = None
) -> list[str]:
    """Escape % to %% when the pi launcher is a cmd.exe batch shim.

    cmd.exe re-expands %VAR% in the /c command line before a .cmd/.bat script
    runs; escaping keeps literal percents so injected provider key env names
    are never substituted into the script's arguments.
    """
    if base is None:
        # 通过 core 动态查找，使测试 monkeypatch core.pi_base_cmd 生效。
        from . import core
        base = core.pi_base_cmd()
    if sys.platform == "win32" and base and base[0].lower() in {"cmd.exe", "cmd"}:
        return [arg.replace("%", "%%") for arg in args]
    return list(args)


def list_terminal_options() -> list[tuple[str, str]]:
    from . import platform_util as pu

    return pu.list_terminal_options()


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        else:
            os.killpg(process.pid, signal.SIGTERM)
    except Exception:
        process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            if sys.platform != "win32":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except Exception:
            process.kill()
        process.wait(timeout=2)


def proxy_reachable(proxy_url: str, timeout: float = 0.4) -> bool:
    """Quick TCP probe of a proxy endpoint (local proxies only, fast fail)."""
    import socket
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(proxy_url or ""))
        if parts.scheme not in {"http", "https"}:
            return False
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _is_private_host(hostname: str) -> bool:
    """True for loopback / link-local / private network hosts (RFC 1918 etc.).

    Local model servers (Ollama, LM Studio, ...) are legitimate plaintext
    targets; remote ones are not.
    """
    import ipaddress

    host = (hostname or "").strip().strip("[]").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
    )


def _check_request_scheme(url: str) -> str:
    """Return '' when the URL may be requested, else a Chinese error message.

    urllib's default handlers support file:// reads; only http(s) targets are
    ever allowed here. http to a non-local host is allowed but logs a warning
    that the key will travel in plaintext.
    """
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url or ""))
        scheme = (parts.scheme or "").lower()
    except (TypeError, ValueError):
        return "Base URL 不是合法的 http/https 地址。"
    if scheme not in {"http", "https"}:
        return f"Base URL 仅允许 http/https 协议，已拒绝 {scheme or '未知'}:// 请求。"
    if scheme == "http" and not _is_private_host(str(parts.hostname or "")):
        return (
            "Base URL 使用公网 HTTP 明文协议，API Key 将以明文传输，已被阻止。"
            "请改用 https://，或使用本地地址（如 127.0.0.1 / localhost）。"
        )
    return ""


def validate_proxy_url(proxy_url: str) -> str:
    """Return '' for a usable proxy URL, else a Chinese error message."""
    from urllib.parse import urlsplit

    value = (proxy_url or "").strip()
    if not value:
        return "代理地址不能为空。"
    try:
        parts = urlsplit(value)
    except (TypeError, ValueError):
        return "代理地址格式非法，应为 http://127.0.0.1:7890 形式。"
    scheme = (parts.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return f"代理地址仅支持 http/https 协议，当前为 {scheme or '空'}://。"
    if not parts.hostname:
        return "代理地址缺少主机名（host）。"
    return ""


def sanitize_proxy_env(env: dict[str, str]) -> dict[str, str]:
    """Drop proxy env vars that point at unreachable endpoints.

    A configured-but-stopped Clash/other proxy makes every child process fail
    with "Connection error"; when the proxy cannot be reached the child should
    simply connect directly instead.
    """
    result = dict(env)
    checked: set[str] = set()
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = (result.get(var) or "").strip()
        if not value or value in checked:
            continue
        checked.add(value)
        if not proxy_reachable(value):
            result.pop(var, None)
    return result


def run_pi(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = 60,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run Pi with real-time 8 MiB limits for stdout and stderr."""
    from . import proc
    from . import core

    base = core.pi_base_cmd()
    cmd = base + escape_cmd_shim_args(args, base)
    full_env = proc.spawn_env(env, sanitize_after_merge=True)
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    output_limit = 8 * 1024 * 1024
    creationflags = proc.create_no_window_flag()
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        cmd,
        cwd=cwd or os.getcwd(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=full_env,
        creationflags=creationflags,
        start_new_session=sys.platform != "win32",
    )
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    limit_exceeded = threading.Event()

    def read_stream(name: str, stream) -> None:
        try:
            while chunk := stream.read(64 * 1024):
                buffer = buffers[name]
                remaining = output_limit - len(buffer)
                if remaining > 0:
                    buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    limit_exceeded.set()
                    _terminate_process_tree(process)
                    break
        finally:
            stream.close()

    readers = [
        threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
        threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _terminate_process_tree(process)
        for reader in readers:
            reader.join(timeout=2)
        raise
    for reader in readers:
        reader.join(timeout=2)
    stdout = buffers["stdout"].decode("utf-8", errors="replace")
    stderr = buffers["stderr"].decode("utf-8", errors="replace")
    if limit_exceeded.is_set():
        return subprocess.CompletedProcess(
            cmd, -1, stdout, "process output limit exceeded\n" + stderr
        )
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
