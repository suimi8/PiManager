# -*- coding: utf-8 -*-
"""进程管理：Pi 命令定位 / 终端选项 / 进程树终止 / 代理环境 / 一次性运行。

从 ``core.py`` 抽出的纯进程/网络工具，无配置状态依赖。
``core.py`` 在顶部重新导出这些符号以保持 ``core.xxx`` 调用兼容。
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import sys
import threading

logger = logging.getLogger(__name__)


def find_pi_command() -> str | None:
    """Return absolute path to pi launcher if possible (Windows / macOS / Linux)."""
    from . import platform_util as pu

    return pu.find_pi_command()


_SHIM_MAX_BYTES = 64 * 1024
# npm shim 里引用 dist/cli.js 时使用的「脚本自身目录」占位符。
_SHIM_BASEDIR_RE = re.compile(
    r"(%~?dp0%?|\$\{basedir\}|\$basedir|\$PSScriptRoot)", re.IGNORECASE
)


def _parse_shim_cli_js(raw: str) -> str | None:
    """从 npm 的 pi.cmd / pi.ps1 / pi shim 文本里解析出 dist/cli.js 的真实路径。"""
    from pathlib import Path

    try:
        shim = Path(raw)
        if shim.stat().st_size > _SHIM_MAX_BYTES:
            return None
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    shim_dir = shim.parent
    for match in re.finditer(r"""[^\s"'`]*\.js""", text):
        token = _SHIM_BASEDIR_RE.sub("", match.group(0)).strip("\"'`")
        token = token.lstrip("\\/")
        if not token:
            continue
        candidate = Path(token)
        if not candidate.is_absolute():
            candidate = shim_dir / token
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None


def _shim_direct_node_cmd(raw: str) -> list[str] | None:
    """把 Windows 上的 npm shim 解析成 ``[node, cli.js]`` 直启命令。

    走 ``cmd.exe /c <shim>`` 会让每个参数二次经过 cmd.exe 的命令行解析
    （引号状态机 / ``& | < > ( ) ^`` / ``%VAR%`` 展开），批处理体内的 ``%*``
    转发还会让文本被第三次解析。``subprocess.list2cmdline`` 产出的 ``\\"``
    不被 cmd.exe 识别为转义，引号会提前闭合——这就是 P0-1 的注入面
    （Windows ``CreateProcess`` 对 ``.bat`` / ``.cmd`` 同样隐式调用 cmd.exe，
    所以「命令里不写 cmd.exe」并不能规避）。唯一彻底的修复是根本不执行
    shim：直接用 node 运行包内的 ``dist/cli.js``。
    """
    import shutil
    from pathlib import Path

    cli: str | None = None
    try:
        from . import platform_util as pu

        found = pu.find_pi_cli_js()
        if found is not None:
            cli = str(found)
    except Exception:
        cli = None
    if cli is None:
        cli = _parse_shim_cli_js(raw)
    if not cli:
        return None
    shim_dir = Path(raw).parent
    for name in ("node.exe", "node"):
        try:
            local = shim_dir / name
            if local.is_file():
                return [str(local), cli]
        except OSError:
            pass
    node = shutil.which("node")
    if node:
        return [node, cli]
    return None


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
    if low.endswith(".cmd") or low.endswith(".bat") or low.endswith(".ps1"):
        # 优先绕开 shell（见 _shim_direct_node_cmd 说明）；只有解析失败时才退回
        # shim，此时 escape_cmd_shim_args 会做保守转义或直接拒绝执行。
        direct = _shim_direct_node_cmd(raw)
        if direct:
            return direct
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


# provider / model / thinking 是唯一「由外部配置（models.json / settings.json，
# 可被导入的配置包完全控制）决定、又会进入 pi 命令行」的字段，因此在这里施加
# 字符白名单，从源头收窄注入面。字符集覆盖真实模型 ID 形态：
# ``deepseek/deepseek-chat``、``google/gemini-2.0-flash-exp:free``、
# ``gemini-1.5-pro@002``、``qwen2.5-72b-instruct``。
_SAFE_PROVIDER_RE = re.compile(r"^[A-Za-z0-9._@:+-]{1,64}$")
_SAFE_MODEL_RE = re.compile(r"^[A-Za-z0-9._@:+/-]{1,128}$")
_SAFE_THINKING_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
_LAUNCH_TOKEN_RULES: dict[str, tuple[str, "re.Pattern[str]"]] = {
    "--provider": ("Provider 名称", _SAFE_PROVIDER_RE),
    "--model": ("Model 名称", _SAFE_MODEL_RE),
    "--thinking": ("Thinking 级别", _SAFE_THINKING_RE),
}


def validate_launch_tokens(args: list[str]) -> None:
    """校验 pi 启动参数里 provider / model / thinking 的字符集（全平台生效）。

    非法字符一律抛 ``ValueError``：这三个字段没有任何合法理由包含引号、shell
    元字符或 ``%``，而它们恰好是恶意配置包唯一能控制的命令行内容。
    """
    for index, item in enumerate(args):
        rule = _LAUNCH_TOKEN_RULES.get(str(item))
        if rule is None or index + 1 >= len(args):
            continue
        label, pattern = rule
        value = str(args[index + 1])
        if not pattern.match(value):
            raise ValueError(
                f"{label}含非法字符，已拒绝启动 Pi：{value!r}。"
                "仅允许字母、数字与 . _ - : / @ + 组合。"
            )


# 裸参数（未被 list2cmdline 加引号）下会被 cmd.exe 解释的字符。
_CMD_BARE_UNSAFE = "&|<>()^%!"


def _escape_cmd_shim_arg(arg: str) -> str:
    """把单个参数转换成可安全穿过 ``cmd.exe /c <shim>`` 的形式。

    实测结论（Windows 11 + npm 风格 shim，见 tests/test_cmd_shim_escape.py）：

    - ``"`` 无法保真穿过：``list2cmdline`` 会输出 cmd.exe 不认的 ``\\"``，
      引号状态机提前闭合，后续 ``&`` 立刻成为命令分隔符。改写成 ``'``。
    - 参数含空格 / 制表符时 ``list2cmdline`` 会整体加引号；引号内 cmd.exe 不
      解释 ``& | < > ( ) ^``，且批处理 ``%*`` 转发时引号被保留 → 安全。
    - 参数不含空格时会被裸输出，而 ``^`` 转义在 ``%*`` 二次解析前就已被消耗
      （实测 ``a^b`` 传到子进程是 ``ab``），无法提供保护 → 只能拒绝执行。
    - ``%VAR%`` 在 ``cmd /c`` 命令行上无法转义（实测 ``%%`` 并非转义，仍会
      展开成 ``%VALUE%``），因此不再做 ``%`` → ``%%`` 的伪转义。
    """
    text = str(arg)
    if "\x00" in text or "\r" in text or "\n" in text:
        raise ValueError(
            "参数含换行 / NUL 字符，无法安全通过 cmd.exe 批处理 shim 传递，"
            "已拒绝启动 Pi。"
        )
    text = text.replace('"', "'")
    if " " in text or "\t" in text:
        return text
    bad = sorted({ch for ch in text if ch in _CMD_BARE_UNSAFE})
    if bad:
        raise ValueError(
            "参数含 cmd.exe 元字符 "
            + "".join(bad)
            + "，无法安全通过批处理 shim 传递，已拒绝启动 Pi。"
            "请安装 Node.js 或修复 npm 全局安装（使 pi 可解析到 dist/cli.js）。"
        )
    return text


def escape_cmd_shim_args(
    args: list[str], base: list[str] | None = None
) -> list[str]:
    """校验 provider/model/thinking 字符集，并按需做 cmd.exe shim 转义。

    两条腿（P0-1）：

    1. ``validate_launch_tokens`` 全平台无条件执行——恶意配置包提供的
       provider / model 名一律在这里被拒；
    2. 只有当 pi 启动器确实解析成了 ``cmd.exe /c <shim>``（``pi_base_cmd``
       已尽力避免）时才对参数做保守转义：无法安全表达的参数直接抛
       ``ValueError``，绝不放行成可注入的命令行。
    """
    validate_launch_tokens(list(args))
    if base is None:
        # 通过 core 动态查找，使测试 monkeypatch core.pi_base_cmd 生效。
        from . import core
        base = core.pi_base_cmd()
    if sys.platform == "win32" and base and base[0].lower() in {"cmd.exe", "cmd"}:
        return [_escape_cmd_shim_arg(arg) for arg in args]
    return list(args)


def list_terminal_options() -> list[tuple[str, str]]:
    from . import platform_util as pu

    return pu.list_terminal_options()


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
    """终止子进程及其整个进程树（Windows taskkill /T，POSIX 进程组信号）。

    只杀单个 pid 是不够的：pi 是 node 启动器，真正干活的是它的子孙进程，
    它们还持有 stdout/stderr 管道句柄——不连带回收会让读取线程永远阻塞在
    ``stream.read()``，超时/超限分支形同虚设。

    本函数可能被两个读取线程与主线程并发调用，也可能在进程树卡死时无法在
    2 秒内回收，因此所有 ``wait`` 都做了保护：绝不把异常抛回调用者线程
    （之前最后一行 ``process.wait(timeout=2)`` 未保护，会让读取线程带着
    未捕获的 ``TimeoutExpired`` 结束）。
    """
    if process.poll() is not None:
        return
    from . import proc

    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=proc.create_no_window_flag(),
            )
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    except Exception:
        try:
            process.terminate()
        except OSError:
            return
    try:
        process.wait(timeout=2)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        if sys.platform != "win32":
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        else:
            process.kill()
    except Exception:
        try:
            process.kill()
        except OSError:
            return
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        # 进程树仍未回收（僵死驱动/不可中断 IO）。调用方只关心「已尽力终止」，
        # 把异常抛回读取线程只会产生噪声日志并掩盖真正的超限/超时原因。
        logger.warning("进程树未能在超时内回收: pid=%s", process.pid)


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
    # ::ffff:127.0.0.1 形式的 IPv4-mapped 地址在 IPv6 对象上 is_loopback 为
    # False，必须先还原成 IPv4 才能正确判定（与 extras 的更严实现对齐）。
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
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


def redact_proxy_url(proxy_url: str) -> str:
    """把代理 URL 里的 userinfo 脱敏，供日志使用（P2-6）。

    ``pi-manager.json`` 里的代理地址可能形如 ``http://user:pass@host:7890``，
    直接写进日志会把凭据落盘。任何调用点在把代理地址交给 logger 之前都应先
    过这个函数。
    """
    from urllib.parse import urlsplit, urlunsplit

    value = str(proxy_url or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        if not (parts.username or parts.password):
            return value
        host = parts.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = f"***:***@{host}" if parts.password else f"***@{host}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except (TypeError, ValueError):
        return "<代理地址已脱敏>"


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


def is_pyinstaller_runtime_key(key: str) -> bool:
    """Return True for PyInstaller bootloader bookkeeping variables."""
    return key.startswith("_PYI_") or key == "PYINSTALLER_RESET_ENVIRONMENT"


def strip_pyinstaller_runtime_env(env: dict[str, str]) -> dict[str, str]:
    """Drop bootloader bookkeeping so a child is not treated as our worker.

    Frozen onefile PiManager puts ``_PYI_*`` into ``os.environ``. Copying that
    into ``pi`` / a terminal / a later ``PiManager.exe`` helper makes the
    bootloader inherit the GUI instance. If the real parent is Cursor, node,
    or cmd, PyInstaller 6.22+ aborts with
    ``Security validation failure: parent process has different executable``.
    """
    return {key: value for key, value in env.items() if not is_pyinstaller_runtime_key(key)}


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
                    # 读取线程里绝不放行异常：终止失败只是「没杀干净」，
                    # 而未捕获的异常会让线程带着 traceback 结束并掩盖超限原因。
                    try:
                        _terminate_process_tree(process)
                    except Exception as exc:
                        logger.warning("输出超限后终止进程树失败: %s", exc)
                    break
        except OSError as exc:
            # 管道在进程树被杀时可能直接失效，这不是错误。
            logger.debug("读取 %s 管道结束: %s", name, exc)
        finally:
            try:
                stream.close()
            except OSError:
                pass

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
    # 读取线程可能仍卡在被孙进程持有的管道上；先快照再解码，避免解码期间
    # bytearray 被并发追加。
    stdout = bytes(buffers["stdout"]).decode("utf-8", errors="replace")
    stderr = bytes(buffers["stderr"]).decode("utf-8", errors="replace")
    if limit_exceeded.is_set():
        return subprocess.CompletedProcess(
            cmd, -1, stdout, "process output limit exceeded\n" + stderr
        )
    return subprocess.CompletedProcess(cmd, returncode, stdout, stderr)
