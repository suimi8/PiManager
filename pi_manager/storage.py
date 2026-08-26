"""Atomic, concurrency-safe JSON persistence helpers."""
from __future__ import annotations

import json
import logging
import os
import stat
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from . import platform_util

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}

# 复用 platform_util 的重解析点常量（CreateFile 路径仍需直接位运算）。
_FILE_ATTRIBUTE_REPARSE_POINT = platform_util.FILE_ATTRIBUTE_REPARSE_POINT


class CorruptJsonError(ValueError):
    """Raised when an existing JSON document cannot be safely loaded."""


@dataclass(frozen=True)
class LoadResult:
    status: Literal["ok", "missing", "corrupt", "unsupported"]
    data: Any
    error: str = ""
    source_path: Path | None = None
    backup_path: Path | None = None


def _thread_lock(path: Path) -> threading.RLock:
    # 规范化 key：realpath 解析符号链接并返回绝对路径，normcase 在 Windows 上
    # 统一大小写和分隔符，确保不同写法的同一文件得到同一锁 key，维持互斥语义。
    # _LOCKS 不做激进清理：实际使用中路径数量有限（固定几个配置文件），
    # 规范化 key 后重复路径不再产生新条目，泄漏风险已大幅缓解。
    key = os.path.normcase(os.path.realpath(str(path)))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _open_lock_file_win32(path: Path):
    """Open a lock file without following reparse points (no check/open race)."""
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

        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        FILE_SHARE_READ = 0x00000001
        FILE_SHARE_WRITE = 0x00000002
        FILE_SHARE_DELETE = 0x00000004
        OPEN_ALWAYS = 4
        FILE_ATTRIBUTE_NORMAL = 0x80
        FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000

        handle = create_file(
            str(path),
            GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
            None,
            OPEN_ALWAYS,
            FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
            None,
        )
        if not handle or handle == wintypes.HANDLE(-1).value:
            raise OSError(f"CreateFileW failed for lock file: {path}")
        try:
            info = BY_HANDLE_FILE_INFORMATION()
            if not get_file_info(handle, ctypes.byref(info)):
                raise OSError(f"GetFileInformationByHandle failed for lock file: {path}")
            if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                raise OSError("lock file must not be a reparse point")
            return os.fdopen(
                msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_APPEND | os.O_BINARY), "a+b"
            )
        except Exception:
            kernel32.CloseHandle(handle)
            raise
    except (AttributeError, ImportError):
        attributes = platform_util.windows_file_attributes(path)
        if attributes is not None and attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise OSError("lock file must not be a reparse point")
        return path.open("a+b")


def lock_sidecar_path(path: Path) -> Path:
    """返回 ``locked(path)`` 实际创建的边车锁文件路径（命名约定的单一来源）。

    边车锁**有意**长期留在磁盘上：它会被后续加锁复用，数量按「被保护路径」收敛
    （不随操作次数增长）。切勿在释放后顺手删除——POSIX 上删掉仍被其它进程
    ``flock`` 等待/持有的 inode，会让该进程与随后新建锁文件的进程同时「持锁」，
    直接破坏互斥；Windows 上则会在有竞争时删除失败。只有确定某路径永不再被
    加锁（例如已下架插件的安装锁）时，才可以安全回收。
    """
    path = Path(path)
    return path.with_name(f".{path.name}.lock")


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold a per-path thread lock and a best-effort inter-process lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock(path)
    with thread_lock:
        lock_path = lock_sidecar_path(path)
        try:
            if os.name == "nt":
                lock_file = _open_lock_file_win32(lock_path)
            else:
                flags = os.O_RDWR | os.O_CREAT
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                lock_file = os.fdopen(os.open(lock_path, flags, 0o600), "a+b")
        except OSError:
            # Read-only or sandboxed directories cannot create a sidecar lock.
            # The in-process lock still protects threads; the actual read/write
            # will produce its own useful error when writes are not permitted.
            logging.getLogger(__name__).warning(
                "无法创建锁文件 %s（目录只读或受限），已降级为仅线程锁",
                lock_path,
            )
            yield
            return
        acquired = False
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            acquired = True
            yield
        finally:
            try:
                if acquired:
                    lock_file.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            finally:
                lock_file.close()


def _read_result_unlocked(path: Path, default: Any) -> LoadResult:
    if not path.exists():
        return LoadResult("missing", deepcopy(default), source_path=path)
    if not path.is_file():
        return LoadResult(
            "unsupported", deepcopy(default), "配置路径不是普通文件", source_path=path
        )
    try:
        # utf-8-sig：用户手工编辑（记事本 / PowerShell）可能带 BOM，而 U+FEFF 不是
        # 合法 JSON 起始，否则整份配置会被误判为 corrupt 并拒绝写入。
        with path.open("r", encoding="utf-8-sig") as handle:
            return LoadResult("ok", json.load(handle), source_path=path)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return LoadResult("corrupt", deepcopy(default), str(exc), source_path=path)
    except OSError as exc:
        return LoadResult("unsupported", deepcopy(default), str(exc), source_path=path)


def _read_unlocked(path: Path, default: Any) -> Any:
    result = _read_result_unlocked(path, default)
    if result.status in {"corrupt", "unsupported"}:
        raise CorruptJsonError(f"配置文件无法读取：{path}: {result.error}")
    return result.data


def load_json_result(path: Path, default: Any) -> LoadResult:
    path = Path(path)
    with locked(path):
        return _read_result_unlocked(path, default)


def load_json(path: Path, default: Any) -> Any:
    result = load_json_result(path, default)
    if result.status in {"corrupt", "unsupported"}:
        raise CorruptJsonError(f"配置文件无法读取：{path}: {result.error}")
    return result.data


def _rotate_backups(path: Path, *, private: bool = False) -> None:
    if not path.exists() or not path.is_file():
        return
    first = path.with_name(f"{path.name}.bak.1")
    second = path.with_name(f"{path.name}.bak.2")
    if first.exists():
        os.replace(first, second)
    backup_temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.bak.tmp")
    try:
        with path.open("rb") as source, backup_temp.open("xb") as target:
            while chunk := source.read(64 * 1024):
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        # Backups must never be more readable than the file they copy — and a
        # private write must clamp the backup even when the previous on-disk
        # file was still world-readable.
        try:
            os.chmod(
                backup_temp,
                0o600 if private else stat.S_IMODE(path.stat().st_mode),
            )
        except OSError:
            pass
        os.replace(backup_temp, first)
    finally:
        backup_temp.unlink(missing_ok=True)


def _write_unlocked(path: Path, data: Any, *, private: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_result_unlocked(path, None)
    if current.status in {"corrupt", "unsupported"}:
        raise CorruptJsonError(
            f"拒绝覆盖无法读取的配置文件：{path}: {current.error}"
        )
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    # A rewrite must not silently widen permissions a previous writer set:
    # remember the current mode and restore it after the atomic replace.
    previous_mode: int | None = None
    if not private:
        try:
            previous_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            previous_mode = None
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        with handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            try:
                os.chmod(temp, previous_mode)
            except OSError:
                pass
        _rotate_backups(path, private=private)
        os.replace(temp, path)
        if private:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def save_json(path: Path, data: Any, *, private: bool = False) -> None:
    path = Path(path)
    with locked(path):
        _write_unlocked(path, data, private=private)


def update_json(path: Path, default: Any, updater: Callable[[Any], Any]) -> Any:
    """Atomically read, transform and write one JSON document."""
    path = Path(path)
    with locked(path):
        current = _read_unlocked(path, default)
        updated = updater(current)
        _write_unlocked(path, updated)
        return updated
