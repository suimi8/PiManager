"""Whitelisted configuration mutations for desktop and Cursor clients."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import core, secrets, storage

_ALLOWED_MANAGER_FIELDS = frozenset(
    {
        "failover_fail_counts",
        "favorites",
        "failover_enabled",
        "failover_fail_threshold",
        "failover_silent",
    }
)

# broker token 最长有效期：180 天。校验通过后按此期限轮换。
_BROKER_TOKEN_MAX_AGE_SECONDS = 180 * 24 * 3600


def broker_token_path() -> Path:
    return secrets.broker_token_path()


def _create_broker_token() -> str:
    token = os.urandom(32).hex()
    path = broker_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(token.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # On Windows, chmod 0o600 is insufficient; restrict ACL to current user.
        if os.name == "nt":
            _restrict_windows_acl(path)
        return token
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _restrict_windows_acl(path: Path) -> None:
    """Restrict file ACL to the current user only on Windows.

    Builds an explicit DACL granting FILE_ALL_ACCESS to the file's owner
    (the current user, since we created the file in-process) and applies it
    together with PROTECTED_DACL_SECURITY_INFORMATION so inherited ACEs are
    removed. A NULL DACL must NEVER be passed here: Microsoft documents
    that a NULL DACL grants *every* local user full access, which would
    invert the intended protection and expose the broker token to all
    accounts on the machine.
    """
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32

        # --- function prototypes (define argtypes for safety/correctness) ---
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        advapi32.GetSecurityInfo.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(wintypes.PVOID), ctypes.POINTER(wintypes.PVOID),
            ctypes.POINTER(wintypes.PVOID), ctypes.POINTER(wintypes.PVOID),
            ctypes.POINTER(wintypes.PVOID),
        ]
        advapi32.GetSecurityInfo.restype = wintypes.DWORD

        advapi32.SetEntriesInAclW.argtypes = [
            wintypes.ULONG, ctypes.c_void_p, wintypes.PVOID,
            ctypes.POINTER(wintypes.PVOID),
        ]
        advapi32.SetEntriesInAclW.restype = wintypes.DWORD

        advapi32.SetSecurityInfo.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
            wintypes.PVOID, wintypes.PVOID, wintypes.PVOID, wintypes.PVOID,
        ]
        advapi32.SetSecurityInfo.restype = wintypes.DWORD

        advapi32.LocalFree.argtypes = [wintypes.HLOCAL]
        advapi32.LocalFree.restype = wintypes.HLOCAL

        # --- constants ---
        SE_FILE_OBJECT = 1
        OWNER_SECURITY_INFORMATION = 0x00000001
        DACL_SECURITY_INFORMATION = 0x00000004
        PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
        WRITE_DAC = 0x00040000
        READ_CONTROL = 0x00020000
        OPEN_EXISTING = 3
        FILE_ALL_ACCESS = 0x001F01FF
        GRANT_ACCESS = 1
        NO_INHERITANCE = 0
        TRUSTEE_IS_SID = 0
        TRUSTEE_IS_USER = 1

        class TRUSTEE_W(ctypes.Structure):
            _fields_ = [
                ("pMultipleTrustee", ctypes.c_void_p),
                ("MultipleTrusteeOperation", wintypes.DWORD),
                ("TrusteeForm", wintypes.DWORD),
                ("TrusteeType", wintypes.DWORD),
                ("ptstrName", wintypes.LPWSTR),
            ]

        class EXPLICIT_ACCESS_W(ctypes.Structure):
            _fields_ = [
                ("grfAccessPermissions", wintypes.DWORD),
                ("grfAccessMode", wintypes.DWORD),
                ("grfInheritance", wintypes.DWORD),
                ("Trustee", TRUSTEE_W),
            ]

        desired_access = WRITE_DAC | READ_CONTROL
        handle = kernel32.CreateFileW(
            str(path), desired_access, 0, None, OPEN_EXISTING, 0, None
        )
        invalid = ctypes.cast(-1, wintypes.HANDLE).value
        if not handle or handle == invalid:
            return
        sd = wintypes.PVOID()
        owner = wintypes.PVOID()
        try:
            # Fetch the file's owner SID (the current user, who created it).
            rc = advapi32.GetSecurityInfo(
                handle, SE_FILE_OBJECT, OWNER_SECURITY_INFORMATION,
                ctypes.byref(owner), None, None, None, ctypes.byref(sd),
            )
            if rc != 0 or not owner:
                return
            ea = EXPLICIT_ACCESS_W()
            ea.grfAccessPermissions = FILE_ALL_ACCESS
            ea.grfAccessMode = GRANT_ACCESS
            ea.grfInheritance = NO_INHERITANCE
            ea.Trustee.pMultipleTrustee = None
            ea.Trustee.MultipleTrusteeOperation = 0
            ea.Trustee.TrusteeForm = TRUSTEE_IS_SID
            ea.Trustee.TrusteeType = TRUSTEE_IS_USER
            # TrusteeForm=IS_SID: ptstrName is a PSID, not a string. The
            # LPWSTR field holds the pointer value as-is.
            ea.Trustee.ptstrName = ctypes.cast(owner, wintypes.LPWSTR)
            new_dacl = wintypes.PVOID()
            rc = advapi32.SetEntriesInAclW(
                1, ctypes.byref(ea), None, ctypes.byref(new_dacl)
            )
            if rc != 0 or not new_dacl:
                return
            try:
                info = (
                    OWNER_SECURITY_INFORMATION
                    | DACL_SECURITY_INFORMATION
                    | PROTECTED_DACL_SECURITY_INFORMATION
                )
                advapi32.SetSecurityInfo(
                    handle, SE_FILE_OBJECT, info,
                    owner, None, new_dacl, None,
                )
            finally:
                advapi32.LocalFree(new_dacl)
        finally:
            if sd:
                advapi32.LocalFree(sd)
            kernel32.CloseHandle(handle)
    except Exception:
        pass


def _rotate_broker_token_if_expired() -> None:
    """校验通过后按有效期轮换 broker token（默认 180 天，基于文件 mtime）。

    轮换发生在 token 校验成功之后，不影响当前请求；写入使用 os.replace
    保证原子性。扩展每次调用前重新读取 token 文件，因此下一次调用自然
    拿到新 token。轮换失败（目录只读等）时非致命：沿用旧 token，仅记日志。
    """
    path = broker_token_path()
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return
    if age < _BROKER_TOKEN_MAX_AGE_SECONDS:
        return
    new_token = os.urandom(32).hex()
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(new_token.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        if os.name == "nt":
            _restrict_windows_acl(temp)
        os.replace(temp, path)
        if os.name == "nt":
            _restrict_windows_acl(path)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        logging.getLogger(__name__).warning(
            "broker token 已过期但轮换失败，沿用旧 token：%s", path
        )
        return
    logging.getLogger(__name__).info("broker token 已按有效期轮换")


def _verify_broker_token(provided: str) -> bool:
    path = broker_token_path()
    if not path.exists():
        try:
            token = _create_broker_token()
            # Even on first creation, require the caller to present the token
            # we just created. An empty provided value is never accepted.
            return bool(provided) and hmac.compare_digest((provided or "").strip(), token)
        except FileExistsError:
            pass
    try:
        stored = path.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError):
        return False
    return bool(provided) and hmac.compare_digest((provided or "").strip(), stored)


def _revision_path() -> Path:
    return core.pi_agent_dir() / ".config-revisions.json"


def _broker_lock_path() -> Path:
    return core.pi_agent_dir() / ".config-broker.mutation"


def _sha256(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record_revision(path: Path) -> int:
    def update(current: Any) -> dict[str, Any]:
        state = current if isinstance(current, dict) else {}
        entry = state.get(path.name) if isinstance(state.get(path.name), dict) else {}
        revision = int(entry.get("revision") or 0) + 1
        state[path.name] = {"revision": revision, "sha256": _sha256(path)}
        return state

    state = storage.update_json(_revision_path(), {}, update)
    return int(state[path.name]["revision"])


def mutate(request: dict[str, Any]) -> dict[str, Any]:
    request_id = str(request.get("request_id") or "")
    if not _verify_broker_token(str(request.get("token") or "")):
        return {
            "ok": False,
            "request_id": request_id,
            "error": "broker token 校验失败，请求已被拒绝",
        }
    # 校验通过后按有效期轮换 token；轮换失败不影响当前请求。
    _rotate_broker_token_if_expired()
    if int(request.get("schema_version") or 0) != 1:
        return {"ok": False, "request_id": request_id, "error": "unsupported_schema"}
    operation = str(request.get("operation") or "")
    arguments = request.get("arguments")
    if not isinstance(arguments, dict):
        return {"ok": False, "request_id": request_id, "error": "invalid_arguments"}

    try:
        if operation == "set_default_model":
            provider = str(arguments.get("provider") or "").strip()
            model = str(arguments.get("model") or "").strip()
            if not provider or not model:
                raise ValueError("provider and model are required")
            thinking = str(arguments.get("thinking") or "").strip()
            sync_enabled = bool(arguments.get("sync_enabled", True))
            favorites = [str(item) for item in arguments.get("favorites", []) if isinstance(item, str)]

            def update_settings(current: Any) -> dict[str, Any]:
                if not isinstance(current, dict):
                    raise ValueError("settings.json 顶层必须是对象")
                result = dict(current)
                result["defaultProvider"] = provider
                result["defaultModel"] = model
                if thinking:
                    result["defaultThinkingLevel"] = thinking
                if sync_enabled:
                    enabled = [str(item) for item in result.get("enabledModels", []) if isinstance(item, str)]
                    result["enabledModels"] = list(dict.fromkeys([*enabled, *favorites, f"{provider}/{model}"]))
                return result

            with storage.locked(_broker_lock_path()):
                storage.update_json(core.settings_path(), {}, update_settings)
                core._invalidate_config_cache(core.settings_path())
                revision = _record_revision(core.settings_path())
            return {
                "ok": True,
                "request_id": request_id,
                "revision": revision,
                "result": {"provider": provider, "model": model},
            }

        if operation == "set_manager_fields":
            fields = arguments.get("fields")
            if not isinstance(fields, dict) or any(key not in _ALLOWED_MANAGER_FIELDS for key in fields):
                raise ValueError("manager mutation contains non-whitelisted fields")

            def update_manager(current: Any) -> dict[str, Any]:
                if not isinstance(current, dict):
                    raise ValueError("pi-manager.json 顶层必须是对象")
                return {**current, **fields}

            with storage.locked(_broker_lock_path()):
                storage.update_json(core.manager_config_path(), {}, update_manager)
                core._invalidate_config_cache(core.manager_config_path())
                revision = _record_revision(core.manager_config_path())
            return {"ok": True, "request_id": request_id, "revision": revision, "result": {}}

        return {"ok": False, "request_id": request_id, "error": "operation_not_allowed"}
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "config broker mutation failed: %s", exc, exc_info=True
        )
        return {"ok": False, "request_id": request_id, "error": "操作失败"}


def mutate_file(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists() or not source.is_file() or source.stat().st_size > 64 * 1024:
        return {"ok": False, "error": "invalid_request_file"}
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"invalid_request: {exc}"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "request_must_be_object"}
    return mutate(payload)
