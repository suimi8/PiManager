"""Atomic, concurrency-safe JSON persistence helpers."""
from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, Iterator


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}


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


def _read_unlocked(path: Path, default: Any) -> Any:
    if not path.exists():
        return deepcopy(default)
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return deepcopy(default)


def load_json(path: Path, default: Any) -> Any:
    path = Path(path)
    with locked(path):
        return _read_unlocked(path, default)


def _write_unlocked(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
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
