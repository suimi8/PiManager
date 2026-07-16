"""Atomic, concurrency-safe JSON persistence helpers."""
from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Literal


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


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
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


@contextmanager
def locked(path: Path) -> Iterator[None]:
    """Hold a per-path thread lock and a best-effort inter-process lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _thread_lock(path)
    with thread_lock:
        lock_path = path.with_name(f".{path.name}.lock")
        try:
            lock_file = lock_path.open("a+b")
        except OSError:
            # Read-only or sandboxed directories cannot create a sidecar lock.
            # The in-process lock still protects threads; the actual read/write
            # will produce its own useful error when writes are not permitted.
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
        with path.open("r", encoding="utf-8") as handle:
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


def _rotate_backups(path: Path) -> None:
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
        os.replace(backup_temp, first)
    finally:
        backup_temp.unlink(missing_ok=True)


def _write_unlocked(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    current = _read_result_unlocked(path, None)
    if current.status in {"corrupt", "unsupported"}:
        raise CorruptJsonError(
            f"拒绝覆盖无法读取的配置文件：{path}: {current.error}"
        )
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _rotate_backups(path)
        os.replace(temp, path)
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def save_json(path: Path, data: Any) -> None:
    path = Path(path)
    with locked(path):
        _write_unlocked(path, data)


def update_json(path: Path, default: Any, updater: Callable[[Any], Any]) -> Any:
    """Atomically read, transform and write one JSON document."""
    path = Path(path)
    with locked(path):
        current = _read_unlocked(path, default)
        updated = updater(current)
        _write_unlocked(path, updated)
        return updated
