"""Small non-GUI helper used by the Cursor extension to obtain provider env."""
from __future__ import annotations

import argparse
import json
import os
import stat
from pathlib import Path

from . import platform_util
from . import secrets as secretstore
from .core import (
    ProviderKeyError,
    classify_provider_key_failure,
    provider_runtime_credential,
)

# token 文件上限：token 恒为 64 个 hex 字符，留出换行/BOM 余量。
_TOKEN_FILE_MAX_BYTES = 256
# reason 文件上限：扩展侧已把原因文本按 UTF-8 字节裁到 64 KiB
# （provider-keys.js:REASON_MAX_BYTES），这里留 4 倍余量吸收 JSON 转义膨胀。
_REASON_FILE_MAX_BYTES = 256 * 1024


def _emit_error(message: str, output: str | None) -> None:
    """输出一条不含密钥的错误 JSON；--output 不可写时回退 stdout。

    错误 payload 里没有任何机密，因此回退到 stdout 是安全的，而「调用方拿不到
    任何响应」才是真正的故障模式（扩展只会报「返回了无效响应」而看不到原因）。

    stdout 分支刻意用 ensure_ascii=True：main.py 在父进程已重定向管道时不会把
    stdout 转成 UTF-8，中文错误信息会按控制台代码页（简中机器上是 GBK）编码，
    调用方按 UTF-8 解析就得到乱码。纯 ASCII 的 \\uXXXX 转义是合法 JSON，任何
    解析器都能还原出原文，与控制台编码无关。写文件的分支由 _emit 以 UTF-8
    打开，保留 ensure_ascii=False 的可读输出。
    """
    payload = {"ok": False, "error": message}
    if not output:
        print(json.dumps(payload))
        return
    try:
        _emit(payload, output)
    except (ValueError, OSError):
        print(json.dumps(payload))


def _read_token_file(path_text: str) -> str:
    """从调用方写入的文件里读取 token 值。

    必须拒绝直接指向 ~/.pi/agent/.broker-token 本身：helper 以用户身份运行，
    读得到那个文件，攻击者只要把路径交上来就能「白拿」凭据 —— 典型的混淆代理
    （confused deputy）。凭据必须由调用方**按值出示**，以此证明它读得到 token。
    """
    path = Path(path_text)
    try:
        broker = secretstore.broker_token_path().resolve()
        if path.resolve() == broker:
            raise ValueError(
                "--token-file 不能指向 broker token 本身，请按值出示 token"
            )
    except OSError:
        pass
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"无法读取 --token-file: {exc}") from None
    if not stat.S_ISREG(info.st_mode) or platform_util.is_reparse_point(path):
        raise ValueError("--token-file 必须是普通文件")
    if info.st_size > _TOKEN_FILE_MAX_BYTES:
        raise ValueError("--token-file 内容过大")
    try:
        return path.read_text(encoding="utf-8-sig", errors="strict").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(f"无法读取 --token-file: {exc}") from None


def _shred_file(path_text: str) -> None:
    """零覆盖并删除一次性输入文件（best-effort）。

    与 ``main.py:_shred_request_file`` 同一标准：``--config-mutate`` 的请求文件
    带着 broker token，reason 文件带着可能夹了密钥片段的上游错误串，两者都是
    「一次性凭据/敏感载体」，不能调用完还躺在临时目录里等兜底清理。

    只处理「普通文件且非重解析点」：否则同机攻击者可以用 junction/symlink 把
    reason 路径指向别处，让 helper 去零覆盖一个本不该动的文件。
    """
    path = Path(path_text)
    try:
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or platform_util.is_reparse_point(path):
            return
        size = info.st_size
    except OSError:
        return
    try:
        with path.open("r+b") as handle:
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass
    try:
        path.unlink()
    except OSError:
        pass


def _read_reason_file(path_text: str) -> str:
    """从调用方写入的一次性文件里读取「Key 失败原因」文本。

    G1：原因文本此前经 ``--reason`` 进入 helper 的**进程命令行**，而命令行在所有
    主流系统上都是非特权可读的（Linux ``/proc/<pid>/cmdline``、Windows
    ``Win32_Process.CommandLine``、macOS ``ps -ww``）。上游错误串里可能夹着密钥
    片段（"Invalid api key: sk-..."），于是同机任意进程都能在 helper 存活的窗口
    内把它捞走。扩展侧的本地脱敏只是缓解，argv 通道本身必须废弃。

    形态校验与 ``config_broker.mutate_file`` 一致：普通文件 / 非重解析点 / 限长 /
    POSIX 上属主必须是当前用户。

    内容既接受 ``{"reason": "..."}``（扩展用的形态，与 ``.json`` 后缀自洽），也
    接受纯文本（手工调用 / 脚本调用更顺手）。
    """
    path = Path(path_text)
    try:
        info = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError(f"无法读取 --reason-file: {exc}") from None
    if not stat.S_ISREG(info.st_mode) or platform_util.is_reparse_point(path):
        raise ValueError("--reason-file 必须是普通文件")
    if info.st_size > _REASON_FILE_MAX_BYTES:
        raise ValueError("--reason-file 内容过大")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise ValueError("--reason-file 必须属于当前用户")
    try:
        # errors="replace"：原因文本是上游 provider 回显的任意字节，一个坏字节
        # 不该让整次分类失败（分类失败 = 该轮换的 Key 不轮换）。
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        raise ValueError(f"无法读取 --reason-file: {exc}") from None
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(payload, dict):
        return str(payload.get("reason") or "")
    return text


def _authorize(args: argparse.Namespace) -> str:
    """校验 broker token；通过返回空串，否则返回给调用方的错误信息。

    P1-5：本入口直接吐出明文 API Key，却曾经**不要求任何凭据**，而只能改两个
    白名单字段的 --config-mutate 反而要 token —— 授权模型是倒置的。现在两者
    共用同一套 broker token 校验（config_broker._verify_broker_token）。
    """
    try:
        if args.token:
            token = args.token.strip()
        elif args.token_file:
            token = _read_token_file(args.token_file)
        else:
            token = ""
    except ValueError as exc:
        return str(exc)
    if not token:
        return (
            "缺少 broker token：请用 --token <值> 或 --token-file <文件> 出示 "
            "~/.pi/agent/.broker-token 的内容（与 --config-mutate 相同的授权模型）"
        )
    # 延迟导入：保持轻量 CLI 的启动开销，且 config_broker 不依赖 PySide6。
    from . import config_broker

    if not config_broker._verify_broker_token(token):
        return "broker token 校验失败，请求已被拒绝"
    return ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a Pi Manager provider environment as JSON")
    parser.add_argument("--json", action="store_true", help="emit JSON (kept for explicit callers)")
    parser.add_argument("--output", help="write JSON to an existing private file")
    parser.add_argument("--mark-failed", action="store_true", help="mark one managed key invalid")
    parser.add_argument("--key-id", default="", help="non-sensitive managed key identifier")
    parser.add_argument(
        "--reason",
        default="",
        help="failure reason (DEPRECATED: lands in the process command line; use --reason-file)",
    )
    parser.add_argument(
        "--reason-file",
        default="",
        help="file holding the failure reason ({\"reason\": ...} or plain text); shredded after use",
    )
    parser.add_argument("--token", default="", help="broker token value")
    parser.add_argument("--token-file", default="", help="file holding the broker token value")
    parser.add_argument("provider")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # P3-3：argparse 默认把 usage 打到 stderr 并抛 SystemExit，破坏本入口对
        # Cursor 扩展承诺的 JSON-only 契约（扩展会拿到非 JSON 而无法解析）。
        if not exc.code:
            return 0  # --help 已正常输出
        print(json.dumps({"ok": False, "error": "invalid_arguments"}))
        return 2
    denied = _authorize(args)
    if denied:
        _emit_error(denied, args.output)
        return 2
    # 原因文本优先走文件通道；``--reason`` 只为向后兼容（旧扩展仍会传它）保留。
    # 读取放在鉴权**之后**：未授权的调用方不该让 helper 去碰它指定的任何路径。
    # 读完立即零覆盖 + 删除，与 --config-mutate 的请求文件同一待遇。
    reason = args.reason
    if args.reason_file:
        try:
            reason = _read_reason_file(args.reason_file)
        except ValueError as exc:
            _emit_error(str(exc), args.output)
            return 2
        finally:
            _shred_file(args.reason_file)
    if args.mark_failed:
        if not args.key_id:
            _emit({"ok": False, "error": "--key-id is required"}, args.output)
            return 2
        # 分类用**完整**原因串：401/429/quota 这类标志常出现在上游错误串的尾部，
        # 任何提前截断都可能把它切掉，让该轮换的 Key 被当成模型故障而不轮换
        # （R2 扩展审计 D1）。落库截断由 secrets._sanitize_reason 负责。
        classification = classify_provider_key_failure(1, "", reason)
        if not classification.get("status"):
            _emit(
                {
                    "ok": True,
                    "marked": False,
                    "status": "",
                    "has_available": True,
                },
                args.output,
            )
            return 0
        changed = secretstore.mark_provider_key_failed(
            args.provider, args.key_id, reason
        )
        if not changed:
            _emit({"ok": False, "error": "API Key 不存在或已被删除"}, args.output)
            return 2
        next_credential = secretstore.get_active_provider_credential(args.provider)
        _emit(
            {
                "ok": True,
                "marked": True,
                "status": classification["status"],
                "failure_kind": classification["failure_kind"],
                "retry_at": classification["retry_at"],
                "has_available": bool(next_credential),
            },
            args.output,
        )
        return 0
    # 明文 API Key 绝不写 stdout：stdout 会进父进程管道、终端回滚缓冲与各类日志
    # 采集。要求调用方预创建 0600 响应文件（_emit 已对其做重解析点/属主加固）。
    if not args.output:
        _emit_error(
            "--output 是必需的：含明文 API Key 的响应只写入调用方预创建的私有文件",
            None,
        )
        return 2
    try:
        credential = provider_runtime_credential(args.provider)
    except ProviderKeyError as exc:
        _emit({"ok": False, "error": str(exc)}, args.output)
        return 2
    _emit(
        {
            "ok": True,
            "env": credential["env"],
            "key_id": credential.get("key_id", ""),
        },
        args.output,
    )
    return 0


# 复用 platform_util 的重解析点常量与属性查询。
_FILE_ATTRIBUTE_REPARSE_POINT = platform_util.FILE_ATTRIBUTE_REPARSE_POINT


def _open_output_file_win32(path: str) -> int:
    """Open an existing output file without following reparse points (no race)."""
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        get_file_info = kernel32.GetFileInformationByHandle
        get_file_info.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        get_file_info.restype = wintypes.BOOL

        class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME),
                ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_EXISTING = 3
        FILE_ATTRIBUTE_NORMAL = 0x80
        FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

        handle = create_file(
            path,
            GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if not handle or handle == wintypes.HANDLE(-1).value:
            if kernel32.GetLastError() == 2:
                raise FileNotFoundError(path)
            raise OSError(f"CreateFileW failed: {path}")
        try:
            info = BY_HANDLE_FILE_INFORMATION()
            if not get_file_info(handle, ctypes.byref(info)):
                raise OSError(f"GetFileInformationByHandle failed: {path}")
            if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("helper output file must not be a reparse point")
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
            os.ftruncate(fd, 0)
            return fd
        except Exception:
            kernel32.CloseHandle(handle)
            raise
    except (AttributeError, ImportError):
        attributes = platform_util.windows_file_attributes(path)
        if attributes is not None and attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise ValueError("helper output file must not be a reparse point")
        return os.open(path, os.O_WRONLY | os.O_TRUNC)


def _emit(payload: dict[str, object], output: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    if not output:
        print(text)
        return
    # Open the pre-created response file directly (no exists()/open() gap) and
    # refuse symlinks so a tmp-dir race cannot redirect the secret elsewhere.
    try:
        if os.name == "nt":
            fd = _open_output_file_win32(output)
        else:
            flags = os.O_WRONLY | os.O_TRUNC
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            fd = os.open(output, flags)
    except FileNotFoundError:
        raise ValueError("helper output file must already exist") from None
    except OSError as exc:
        raise ValueError(f"helper output file is not writable: {exc}") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("helper output file must be a regular file")
        if hasattr(os, "getuid"):
            try:
                if os.fstat(fd).st_uid != os.getuid():
                    raise ValueError("helper output file must be owned by the current user")
            except (AttributeError, OSError):
                pass
        if hasattr(os, "fchmod"):
            try:
                os.fchmod(fd, 0o600)
            except OSError:
                pass
        handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
    except Exception:
        os.close(fd)
        raise
    with handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(output, 0o600)
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
