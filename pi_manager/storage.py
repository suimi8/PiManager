"""Atomic, concurrency-safe JSON persistence helpers."""
from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal

from . import platform_util

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}

# 同一线程已持有的跨进程锁深度（规范化路径 -> 计数）。
#
# 为什么需要它：``msvcrt.locking`` / ``fcntl.flock`` 的锁归属于「打开的文件
# 句柄」，同一进程再开一个句柄去锁同一文件**依然互斥** —— Windows 上
# ``LK_LOCK`` 重试 10 次（约 9 秒）后抛 ``OSError: Resource deadlock
# avoided``，POSIX 上 ``flock(LOCK_EX)`` 没有重试上限、会永久阻塞。而外层的
# ``threading.RLock`` 是可重入的，会**放行**嵌套调用：两层保护的可重入语义
# 不一致，正是陷阱所在。结果是上层「读-改-写」无法整体持锁，只能退化成
# ``threading.Lock``（只防线程不防进程），丢失更新变成结构性问题。
#
# 于是在进程内自己记数：最外层才真正申请/释放 OS 锁，内层只累加计数。
# 必须用 ``threading.local``：跨进程锁的互斥单位是「进程」，但重入的**合法性**
# 单位是「线程」—— 同一进程的另一个线程必须真正等待，这由 ``_LOCKS`` 里的
# ``RLock`` 保证（最外层持有者在整个嵌套期间都不放开它）。
_HELD = threading.local()

# 复用 platform_util 的重解析点常量（CreateFile 路径仍需直接位运算）。
_FILE_ATTRIBUTE_REPARSE_POINT = platform_util.FILE_ATTRIBUTE_REPARSE_POINT


class CorruptJsonError(ValueError):
    """Raised when an existing JSON document cannot be safely loaded."""


class _Unchanged:
    """``update_json`` 的 updater 返回 ``UNCHANGED`` 表示「无需改动」。"""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - 仅便于调试输出
        return "storage.UNCHANGED"


UNCHANGED = _Unchanged()
"""updater 的「无需写入」信号。

判定在锁内做，所以结论是可信的（不像锁外预检那样可能被并发写入推翻）。
跳过写入不只是省一次 fsync：``_rotate_backups`` 会把当前内容推进
``.bak.1``、把 ``.bak.1`` 推进 ``.bak.2``，空写会用相同内容把有用的历史挤掉。
"""


@dataclass(frozen=True)
class LoadResult:
    status: Literal["ok", "missing", "corrupt", "unsupported"]
    data: Any
    error: str = ""
    source_path: Path | None = None
    backup_path: Path | None = None


def _lock_key(path: Path) -> str:
    # 规范化 key：realpath 解析符号链接并返回绝对路径，normcase 在 Windows 上
    # 统一大小写和分隔符，确保不同写法的同一文件得到同一锁 key，维持互斥语义。
    # 重入计数（_HELD）必须与线程锁（_LOCKS）共用这一套 key，否则同一文件的
    # 两种写法会一边被判定为「已持有」、一边去申请新的 OS 锁。
    return os.path.normcase(os.path.realpath(str(path)))


def _thread_lock(path: Path) -> threading.RLock:
    return _thread_lock_for_key(_lock_key(path))


def _thread_lock_for_key(key: str) -> threading.RLock:
    # _LOCKS 不做激进清理：实际使用中路径数量有限（固定几个配置文件），
    # 规范化 key 后重复路径不再产生新条目，泄漏风险已大幅缓解。
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def _held_depths() -> dict[str, int]:
    depths: dict[str, int] | None = getattr(_HELD, "depths", None)
    if depths is None:
        depths = {}
        _HELD.depths = depths
    return depths


@contextmanager
def _count_reentry(key: str) -> Iterator[None]:
    """在 ``_HELD`` 里标记「本线程持有 *key* 的锁」，退出时精确回退。

    计数归零时把 key 从字典里删掉，避免长生命周期线程（GUI 主线程）里
    ``_HELD.depths`` 无界增长。
    """
    depths = _held_depths()
    depths[key] = depths.get(key, 0) + 1
    try:
        yield
    finally:
        remaining = depths.get(key, 1) - 1
        if remaining > 0:
            depths[key] = remaining
        else:
            depths.pop(key, None)


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
    """Hold a per-path thread lock and a best-effort inter-process lock.

    **进程内可重入**：同一线程对同一路径重复加锁只累加计数，不会再去申请一次
    OS 锁（原因见 ``_HELD`` 的注释）。这让上层可以把「读 → 改 → 写」整体包进
    一把锁里 —— 内部的 ``load_json`` / ``save_json`` 各自再 ``locked()`` 也安全。

    **跨进程语义不变**：重入计数是 per-process 的，其它进程仍然会被
    ``msvcrt.locking`` / ``fcntl.flock`` 挡在外面，直到最外层退出才释放。
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # mkdir 之后再算 key：目录存在后 realpath 才能稳定解析符号链接，
    # 否则同一路径在「目录创建前/后」可能得到两个不同的 key。
    key = _lock_key(path)
    if _held_depths().get(key, 0) > 0:
        # 本线程已持有最外层锁（包括降级为仅线程锁的情形）→ 纯计数重入。
        with _count_reentry(key):
            yield
        return
    thread_lock = _thread_lock_for_key(key)
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
            # 降级路径也要记数：否则嵌套调用会一遍遍重试注定失败的建锁操作。
            with _count_reentry(key):
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
            # 计数只在 OS 锁真正拿到之后才登记：加锁本身失败时必须让后续调用
            # 重新去争锁，而不是误以为「已持有」而跳过。
            with _count_reentry(key):
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


def _harden_private(path: Path) -> None:
    """把 *path* 的 Windows DACL 收紧为「仅当前用户」；POSIX 上退化为 chmod 0600。

    为什么 chmod 不够：NTFS 走 ACL、不看 mode 位，``os.chmod(p, 0o600)`` 在
    Windows 上对访问控制**毫无效果**，``private=True`` 的文件一直继承父目录 ACL
    （SYSTEM / Administrators 也可读）。主文件已经在 ``_write_payload_unlocked``
    里补了这一步，但**它的副本没补**：``_rotate_backups`` 的 ``.bak.*`` 与
    ``_quarantine_corrupt`` 的 ``.corrupt.*`` 内容和主文件完全一致（pi-manager.json
    可能带含凭据的代理 URL），实测主文件只有 1 条显式 ACE、而副本有 3 条继承 ACE，
    正好违反 ``_rotate_backups`` 自己注释里「备份绝不能比它复制的文件更可读」。

    尽力而为：加固失败绝不能让写入失败（文件已经落盘），
    ``platform_util.harden_private_path`` 内部已记 WARNING。
    """
    try:
        # 走模块属性而非 from-import：保持 monkeypatch 可见（测试会替换它）。
        platform_util.harden_private_path(path)
    except Exception:
        pass


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
        if private:
            # 在替换**之前**加固：同卷 rename 会带着文件自己的安全描述符走，
            # 所以 .bak.1 落地时 DACL 已经是收紧的，不存在「先宽后紧」的窗口。
            # （随后 .bak.1 → .bak.2 的 os.replace 同理自动继承这条显式 ACE。）
            _harden_private(backup_temp)
        os.replace(backup_temp, first)
    finally:
        backup_temp.unlink(missing_ok=True)


def _quarantine_corrupt(path: Path, *, private: bool = False) -> Path | None:
    """把损坏的配置另存为 ``<name>.corrupt.<时间戳>``，保留取证线索。

    恢复流程会用备份内容整份覆盖损坏文件。直接丢弃损坏内容等于销毁「到底坏成
    什么样」的唯一证据；而让它走 ``_rotate_backups`` 进 ``.bak.1`` 又会污染备份
    链（把仅存的可用备份挤到 ``.bak.2`` 乃至挤掉）。所以单独隔离到一个既不参与
    轮转、也不出现在恢复列表（那里只匹配 ``*.bak.*``）的文件名。

    尽力而为：隔离失败不应该阻断恢复本身 —— 恢复不了配置的代价远大于少一份取证。
    """
    try:
        if not path.is_file():
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = path.with_name(f"{path.name}.corrupt.{stamp}")
        suffix = 1
        while target.exists():
            target = path.with_name(f"{path.name}.corrupt.{stamp}-{suffix}")
            suffix += 1
        # 用「复制」而不是「重命名」：后续原子替换若失败，损坏文件仍在原位，
        # 不会出现「原文件没了、新文件也没写成」的空洞。
        with path.open("rb") as source, target.open("xb") as sink:
            while chunk := source.read(64 * 1024):
                sink.write(chunk)
            sink.flush()
            os.fsync(sink.fileno())
        try:
            os.chmod(target, 0o600 if private else stat.S_IMODE(path.stat().st_mode))
        except OSError:
            pass
        if private:
            # 隔离副本和主文件内容一字不差，权限也必须一样紧（见 _harden_private）。
            _harden_private(target)
        return target
    except OSError as exc:
        logging.getLogger(__name__).warning("隔离损坏配置 %s 失败: %s", path, exc)
        return None


def _write_payload_unlocked(
    path: Path,
    payload: bytes,
    *,
    private: bool = False,
    rotate_backup: bool = True,
) -> None:
    """原子替换 *path* 的内容（``O_EXCL`` 临时文件 + fsync + ``os.replace``）。

    JSON 与纯文本共用同一套原子写/权限保持/备份轮转策略，差异只在序列化。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
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
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            try:
                os.chmod(temp, previous_mode)
            except OSError:
                pass
        if rotate_backup:
            _rotate_backups(path, private=private)
        os.replace(temp, path)
        if private:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            # chmod 0o600 在 Windows 上是空操作（NTFS 走 ACL，不看 mode 位），
            # 所以 private=True 的文件在 Windows 上其实一直继承着父目录 ACL
            # （R2 安全审计 P1-1 的连带项）。这里补一次真正的 DACL 收紧。
            _harden_private(path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def _write_unlocked(
    path: Path,
    data: Any,
    *,
    private: bool = False,
    allow_corrupt_overwrite: bool = False,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_result_unlocked(path, None)
    corrupt = current.status in {"corrupt", "unsupported"}
    if corrupt and not allow_corrupt_overwrite:
        raise CorruptJsonError(
            f"拒绝覆盖无法读取的配置文件：{path}: {current.error}"
        )
    if corrupt:
        _quarantine_corrupt(path, private=private)
    # 序列化保持与旧实现完全一致（indent=2 + ensure_ascii=False + 末尾换行），
    # 只是改为先成串再按字节写，避免文本模式的换行转换。
    payload = (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    _write_payload_unlocked(
        path,
        payload,
        private=private,
        # 恢复路径（allow_corrupt_overwrite）跳过备份轮转：轮转会把「当前内容」
        # 推进 .bak.1、把 .bak.1 推进 .bak.2，恰好覆盖掉用户正在恢复的那份备份，
        # 连续两次恢复就会毁掉整条备份链。损坏内容已由 _quarantine_corrupt 留证。
        rotate_backup=not allow_corrupt_overwrite,
    )


def save_json(
    path: Path,
    data: Any,
    *,
    private: bool = False,
    allow_corrupt_overwrite: bool = False,
) -> None:
    """Atomically write one JSON document.

    ``allow_corrupt_overwrite`` 是**唯一**绕过「拒绝覆盖无法读取的配置文件」守卫
    的出口，只给「从备份恢复」这一条修复路径使用（``core.restore_config_backup``）。
    守卫本身必须留着：普通写入路径若默默覆盖损坏文件，会毁掉用户仅存的原始内容
    与取证线索。绕过时损坏内容被隔离到 ``<name>.corrupt.<ts>``，且不做备份轮转。

    不要把这个参数暴露给通用写入封装（``core.save_json`` 就没有），否则「防误
    覆盖」会退化成一个随处可用的后门。
    """
    path = Path(path)
    with locked(path):
        _write_unlocked(
            path,
            data,
            private=private,
            allow_corrupt_overwrite=allow_corrupt_overwrite,
        )


def _write_text_unlocked(path: Path, content: str, *, private: bool = False) -> None:
    # 统一用 \n：AGENTS.md 之类的文本文件在 Windows 上也不做 CRLF 转换，
    # 保证同一份内容在不同平台产出相同字节（备份比对/校验和才有意义）。
    payload = content.replace("\r\n", "\n").encode("utf-8")
    _write_payload_unlocked(path, payload, private=private)


def save_text(path: Path, content: str, *, private: bool = False) -> None:
    """Atomically write one text document (AGENTS.md、主题文件等)。

    与 ``save_json`` 共享原子替换 + 权限保持 + 备份轮转：``Path.write_text`` 是
    「先截断再写」，中途崩溃会把用户手写的全局指令文件截成半截且无任何退路。
    """
    path = Path(path)
    with locked(path):
        _write_text_unlocked(path, content, private=private)


def update_json(
    path: Path,
    default: Any,
    updater: Callable[[Any], Any],
    *,
    private: bool = False,
) -> Any:
    """Atomically read, transform and write one JSON document.

    读与写在同一把锁内完成 —— 这是唯一没有「丢失更新」窗口的写入方式。
    """
    path = Path(path)
    with locked(path):
        current = _read_unlocked(path, default)
        updated = updater(current)
        if updated is UNCHANGED:
            return current
        _write_unlocked(path, updated, private=private)
        return updated
