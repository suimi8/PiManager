# -*- coding: utf-8 -*-
"""Secure secret storage for Pi Manager (cross-platform).

Priority:
1) OS keyring (Windows Credential Locker / macOS Keychain / Linux Secret Service)
2) Windows DPAPI vault file
3) Per-user file vault with randomly generated key (chmod 600), never a fixed XOR key
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import stat
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .storage import locked

SERVICE = "PiManager"
_KEYRING = None
_KEYRING_TRIED = False
_KEYRING_TRIED_AT = 0.0
_KEYRING_RETRY_COOLDOWN = 60.0
_KEYRING_PROBE_TIMEOUT = 5.0


class VaultCorruptError(ValueError):
    """Raised when an existing vault cannot be decrypted or parsed."""


def _vault_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / "secrets.vault"


def config_dir() -> Path:
    """Pi 配置目录（固定为 ~/.pi/agent/，Windows 为 %USERPROFILE%\\.pi\\agent）。"""
    return Path(os.path.expanduser("~")) / ".pi" / "agent"


def broker_token_path() -> Path:
    return config_dir() / ".broker-token"


def _legacy_vault_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / "secrets.dpapi"


def _master_key_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / ".vault_master_key"


def _index_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / "secrets.index.json"


def _mutation_lock_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / "secrets.mutation"


def _provider_key_pool_lock_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / "provider-keys.mutation"


def _ensure_dir() -> None:
    _vault_path().parent.mkdir(parents=True, exist_ok=True)


def _keyring_backend_is_unsafe(backend: Any) -> bool:
    """True when the selected keyring backend persists secrets in plain files."""
    name = f"{type(backend).__name__} {type(backend).__module__}".lower()
    return any(marker in name for marker in ("file", "plaintext", "keyrings.alt"))


def _get_keyring():
    global _KEYRING, _KEYRING_TRIED, _KEYRING_TRIED_AT
    if _KEYRING_TRIED:
        if _KEYRING is None and time.monotonic() - _KEYRING_TRIED_AT >= _KEYRING_RETRY_COOLDOWN:
            _KEYRING_TRIED = False
        else:
            return _KEYRING
    _KEYRING_TRIED = True
    _KEYRING_TRIED_AT = time.monotonic()
    try:
        import keyring  # type: ignore
    except Exception:
        _KEYRING = None
        return _KEYRING
    outcome: dict[str, Any] = {}

    def probe() -> None:
        try:
            backend = keyring.get_keyring()
            if backend is None or _keyring_backend_is_unsafe(backend):
                return
            probe_user = "__pi_manager_probe__"
            keyring.set_password(SERVICE, probe_user, "_")
            try:
                if keyring.get_password(SERVICE, probe_user) != "_":
                    return
            finally:
                try:
                    keyring.delete_password(SERVICE, probe_user)
                except Exception:
                    pass
            outcome["backend"] = keyring
        except Exception as exc:
            outcome["error"] = exc

    thread = threading.Thread(target=probe, daemon=True, name="keyring-probe")
    thread.start()
    thread.join(_KEYRING_PROBE_TIMEOUT)
    if thread.is_alive() or "error" in outcome:
        _KEYRING = None
    else:
        _KEYRING = outcome.get("backend")
    return _KEYRING


def _dpapi_protect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI only on Windows")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = DATA_BLOB(len(data), ctypes.create_string_buffer(data, len(data)))
    blob_out = DATA_BLOB()
    if not crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "PiManager",
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise OSError(f"CryptProtectData failed: {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    if sys.platform != "win32":
        raise OSError("DPAPI only on Windows")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    blob_in = DATA_BLOB(len(data), ctypes.create_string_buffer(data, len(data)))
    blob_out = DATA_BLOB()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(blob_out),
    ):
        raise OSError(f"CryptUnprotectData failed: {ctypes.GetLastError()}")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(blob_out.pbData)


_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _windows_file_attributes(path: Path) -> int | None:
    try:
        import ctypes

        func = ctypes.windll.kernel32.GetFileAttributesW
        func.argtypes = [ctypes.c_wchar_p]
        func.restype = ctypes.c_uint32
    except Exception:
        return None
    try:
        value = func(str(path))
    except Exception:
        return None
    if value == 0xFFFFFFFF:
        return None
    return int(value)


def _validate_master_key(path: Path) -> bytes:
    if os.name == "nt":
        attributes = _windows_file_attributes(path)
        if attributes is not None and attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise VaultCorruptError(f"主密钥不能是 reparse point: {path}")
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise VaultCorruptError(f"主密钥不是普通文件: {path}")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise VaultCorruptError(f"主密钥权限过宽，应为 0600: {path}")
    key = path.read_bytes()
    if len(key) != 32:
        raise VaultCorruptError(f"主密钥长度无效: {path}")
    return key


def _load_or_create_master_key() -> bytes:
    """Load or atomically create a 32-byte per-user fallback key.

    The key is derived from a random salt file via PBKDF2-HMAC-SHA256 with a
    fixed application pepper, so a simple file copy is insufficient to decrypt
    the vault — the attacker would also need to brute-force the KDF.
    """
    _ensure_dir()
    path = _master_key_path()
    if path.exists():
        return _validate_master_key(path)
    raw_salt = os.urandom(32)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(raw_salt)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            return _validate_master_key(path)
        except OSError:
            if path.exists():
                return _validate_master_key(path)
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return _validate_master_key_salt(path)


_PEPPER = b"PiManager::vault::v3::pbkdf2"
_KDF_ITERATIONS = 600_000

# 旧的无认证加密格式（filekey: XOR 流 / local: 固定硬编码 key）仅在迁移时
# 读取，保留向后兼容。默认开启解密能力，但每次读取都会记录 WARNING 审计
# 日志，提示旧条目将在下次写入时升级为 AES-GCM。
_LEGACY_DECRYPT_ALLOWED = True


def _derive_key_from_salt(salt: bytes) -> bytes:
    """Derive a 32-byte AES key from a salt using PBKDF2 + fixed pepper."""
    return hashlib.pbkdf2_hmac("sha256", _PEPPER, salt, _KDF_ITERATIONS, dklen=32)


def _validate_master_key_salt(path: Path) -> bytes:
    """Read and validate the salt file (32 bytes, regular, 0600)."""
    if os.name == "nt":
        attributes = _windows_file_attributes(path)
        if attributes is not None and attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            raise VaultCorruptError(f"主密钥盐文件不能是 reparse point: {path}")
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise VaultCorruptError(f"主密钥盐文件不是普通文件: {path}")
    if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
        raise VaultCorruptError(f"主密钥盐文件权限过宽，应为 0600: {path}")
    salt = path.read_bytes()
    if len(salt) != 32:
        raise VaultCorruptError(f"主密钥盐文件长度无效: {path}")
    return salt


def _get_master_key() -> bytes:
    """Get the derived AES key, loading or creating the salt as needed."""
    return _derive_key_from_salt(_load_or_create_master_key())


def _xor_stream(data: bytes, key: bytes) -> bytes:
    if not key:
        raise ValueError("empty key")
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def encrypt_blob(data: bytes) -> bytes:
    """Encrypt bytes for on-disk vault."""
    if sys.platform == "win32":
        try:
            return b"dpapi:" + base64.b64encode(_dpapi_protect(data))
        except Exception:
            pass
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = _get_master_key()
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, data, b"PiManagerVault:v2")
    return b"aesgcm:" + base64.b64encode(nonce + encrypted)


def decrypt_blob(raw: bytes) -> bytes:
    if raw.startswith(b"dpapi:"):
        return _dpapi_unprotect(base64.b64decode(raw[6:]))
    if raw.startswith(b"aesgcm:"):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        payload = base64.b64decode(raw[7:])
        if len(payload) < 13:
            raise ValueError("invalid AES-GCM vault")
        return AESGCM(_get_master_key()).decrypt(
            payload[:12], payload[12:], b"PiManagerVault:v2"
        )
    if raw.startswith(b"filekey:"):
        # Legacy unauthenticated fallback; successful reads are upgraded on next write.
        if not _LEGACY_DECRYPT_ALLOWED:
            raise VaultCorruptError("旧的无认证加密格式（filekey:）已被禁用")
        key = _get_master_key()
        plaintext = _xor_stream(base64.b64decode(raw[8:]), key)
        logging.getLogger(__name__).warning(
            "读取了无认证的旧格式 vault 条目（filekey:），将在下次写入时升级为 AES-GCM"
        )
        return plaintext
    if raw.startswith(b"local:"):
        # old fixed key — still decrypt for migration only
        if not _LEGACY_DECRYPT_ALLOWED:
            raise VaultCorruptError("旧的无认证加密格式（local:）已被禁用")
        legacy = b"PiManagerLocalFallbackKey!v1"
        plaintext = _xor_stream(base64.b64decode(raw[6:]), legacy)
        logging.getLogger(__name__).warning(
            "读取了无认证的旧格式 vault 条目（local:），将在下次写入时升级为 AES-GCM"
        )
        return plaintext
    # raw dpapi blob (old whole-file format)
    if sys.platform == "win32":
        try:
            return _dpapi_unprotect(raw)
        except Exception:
            pass
    raise VaultCorruptError("vault 数据无法识别加密格式")


def load_vault() -> dict[str, str]:
    """Load the merged vault: primary ``secrets.vault`` first, legacy as fallback.

    The legacy file (``secrets.dpapi``) is only consulted when the primary vault
    is missing or unreadable. When the primary vault is valid and the legacy
    file is a strict subset of it, the leftover legacy file is removed; if the
    legacy file holds entries the primary vault does not have, it is kept and a
    warning is logged so no secret is silently dropped.
    """
    _ensure_dir()
    with locked(_vault_path()):
        errors: list[str] = []
        found = False
        primary: dict[str, str] | None = None
        legacy_data: dict[str, str] | None = None

        primary_path = _vault_path()
        legacy_path = _legacy_vault_path()

        if primary_path.exists():
            found = True
            try:
                primary = _read_vault_file(primary_path, rewrite_legacy_format=True)
            except Exception as exc:
                errors.append(f"{primary_path}: {exc}")
        if legacy_path.exists():
            found = True
            try:
                legacy_data = _read_vault_file(legacy_path, rewrite_legacy_format=False)
            except Exception as exc:
                errors.append(f"{legacy_path}: {exc}")

        if primary is None:
            if legacy_data is not None:
                # Primary unreadable/missing: fall back to legacy and promote it
                # to the primary vault with authenticated encryption.
                try:
                    _save_vault_unlocked(legacy_data)
                    legacy_path.unlink(missing_ok=True)
                except Exception:
                    pass
                return legacy_data
            if found:
                raise VaultCorruptError(
                    "Vault 无法解密或解析；原文件未被修改。" + " | ".join(errors)
                )
            return {}

        if legacy_data is not None:
            subset = all(
                str(key) in primary and primary[str(key)] == str(value)
                for key, value in legacy_data.items()
            )
            if subset:
                # Pure leftover copy: safe to drop.
                try:
                    legacy_path.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                logging.getLogger(__name__).warning(
                    "legacy vault %s 含有主 vault 没有的条目，保留文件待处理：%s",
                    legacy_path,
                    "、".join(sorted(legacy_data)),
                )
        return primary


def _read_vault_file(path: Path, *, rewrite_legacy_format: bool = False) -> dict[str, str]:
    """Decrypt and parse one vault file into ``{name: value}``.

    ``rewrite_legacy_format`` rewrites unauthenticated legacy formats in place
    with authenticated encryption — only safe for the primary vault path.
    """
    raw = path.read_bytes()
    try:
        text = decrypt_blob(raw).decode("utf-8", errors="strict")
    except Exception as exc:
        if raw.startswith((b"dpapi:", b"aesgcm:", b"filekey:", b"local:")):
            raise VaultCorruptError(f"vault 解密失败: {exc}") from exc
        # Attempt legacy plaintext migration as a last resort, but never
        # silently accept untrusted plaintext — require it to parse as valid
        # JSON dict; otherwise the vault is corrupt. 为了防止本地攻击者用明文
        # JSON 替换已初始化的加密 vault 实现凭据注入，仅在尚未初始化主密钥
        # （首次迁移场景）时才接受明文 JSON；主密钥已存在时一律拒绝。
        if _master_key_path().exists():
            raise VaultCorruptError(
                "vault 已初始化为加密格式，拒绝接受明文 JSON（疑似凭据注入）"
            ) from exc
        try:
            text = raw.decode("utf-8", errors="strict")
        except Exception as text_exc:
            raise VaultCorruptError(f"vault 解密失败且无法作为文本解析: {exc}") from text_exc
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError("Vault 顶层必须是 JSON 对象")
    result = {str(k): str(v) for k, v in data.items()}
    if rewrite_legacy_format and not raw.startswith((b"dpapi:", b"aesgcm:")):
        # Legacy unauthenticated formats (filekey:/local: XOR, raw DPAPI
        # blobs, plaintext) are readable but must not stay on disk: rewrite
        # immediately with authenticated encryption.
        _save_vault_unlocked(result)
    return result


def _save_vault_unlocked(data: dict[str, str]) -> None:
    payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    blob = encrypt_blob(payload)
    vault = _vault_path()
    temp = vault.with_name(
        f".{vault.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, vault)
        try:
            os.chmod(vault, 0o600)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def save_vault(data: dict[str, str]) -> None:
    _ensure_dir()
    with locked(_vault_path()):
        _save_vault_unlocked(data)


def _load_index() -> set[str]:
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
        return {str(item) for item in data if isinstance(item, str)} if isinstance(data, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def _save_index(names: set[str]) -> None:
    _ensure_dir()
    path = _index_path()
    payload = json.dumps(sorted(names), ensure_ascii=False, indent=2).encode("utf-8")
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        os.replace(temp, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        temp.unlink(missing_ok=True)


def set_secret(name: str, value: str) -> None:
    name = (name or "").strip()
    if not name:
        return
    with locked(_mutation_lock_path()):
        kr = _get_keyring()
        keyring_saved = False
        if kr is not None:
            try:
                if value:
                    kr.set_password(SERVICE, name, value)
                else:
                    try:
                        kr.delete_password(SERVICE, name)
                    except Exception:
                        pass
                keyring_saved = True
            except Exception:
                kr = None
        vault = load_vault()
        if value and not keyring_saved:
            vault[name] = value
        elif name in vault:
            del vault[name]
        save_vault(vault)
        names = _load_index()
        if value:
            names.add(name)
        else:
            names.discard(name)
        _save_index(names)


def get_secret(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    kr = _get_keyring()
    if kr is not None:
        try:
            val = kr.get_password(SERVICE, name)
            if val:
                return str(val)
        except Exception:
            pass
    return str(load_vault().get(name) or "")


def delete_secret(name: str) -> None:
    name = (name or "").strip()
    with locked(_mutation_lock_path()):
        kr = _get_keyring()
        if kr is not None:
            try:
                kr.delete_password(SERVICE, name)
            except Exception:
                pass
        vault = load_vault()
        if name in vault:
            del vault[name]
            save_vault(vault)
        names = _load_index()
        names.discard(name)
        _save_index(names)


def list_secret_names() -> list[str]:
    vault_names = set(load_vault().keys())
    index = _load_index()
    names = vault_names | index
    if vault_names - index:
        # 自愈：vault 中存在但 index 缺失的名字补写回 index，避免后续
        # 导出含密钥包 / 导入回滚快照因 index 不同步而遗漏密钥。
        try:
            with locked(_index_path()):
                current = _load_index()
                _save_index(names | current)
        except Exception:
            pass
    return sorted(names)


def provider_pool_names() -> list[tuple[str, str, str]]:
    """Return (provider, pool_name, single_key_name) for every provider key pool.

    Used to detect key pools whose provider config has been removed from
    models.json (orphaned credentials).
    """
    providers: dict[str, None] = {}
    for name in list_secret_names():
        match = re.match(r"^provider:(.+):(apiKeys|apiKey)$", name)
        if match:
            providers.setdefault(match.group(1), None)
    return [
        (
            provider,
            f"provider:{provider}:apiKeys",
            f"provider:{provider}:apiKey",
        )
        for provider in sorted(providers)
    ]


def provider_key_name(provider: str) -> str:
    return f"provider:{provider}:apiKey"


def provider_key_pool_name(provider: str) -> str:
    return f"provider:{provider}:apiKeys"


def _new_provider_key(value: str) -> dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:16],
        "value": value,
        "status": "available",
        "failed_at": "",
        "retry_at": "",
        "failure_kind": "",
        "failure_count": 0,
        "failure_reason": "",
    }


def _available_key_fields() -> dict[str, Any]:
    return {
        "status": "available",
        "failed_at": "",
        "retry_at": "",
        "failure_kind": "",
        "failure_count": 0,
        "failure_reason": "",
    }


def _normalize_provider_key_pool(data: Any) -> dict[str, Any]:
    keys: list[dict[str, Any]] = []
    source = data.get("keys") if isinstance(data, dict) else []
    now = datetime.now(timezone.utc)
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        value = str(item.get("value") or "").strip()
        if not value:
            continue
        status = str(item.get("status") or "available")
        if status not in {"available", "cooldown", "restricted", "invalid"}:
            status = "available"
        retry_at = str(item.get("retry_at") or "")
        if status == "cooldown" and retry_at:
            try:
                if datetime.fromisoformat(retry_at.replace("Z", "+00:00")) <= now:
                    status = "available"
            except ValueError:
                status = "restricted"
        failed = status != "available"
        keys.append(
            {
                "id": str(item.get("id") or uuid.uuid4().hex[:16]),
                "value": value,
                "status": status,
                "failed_at": str(item.get("failed_at") or "") if failed else "",
                "retry_at": retry_at if status == "cooldown" else "",
                "failure_kind": str(item.get("failure_kind") or "") if failed else "",
                "failure_count": max(0, int(item.get("failure_count") or 0)) if failed else 0,
                "failure_reason": str(item.get("failure_reason") or "") if failed else "",
            }
        )
    active_id = str(data.get("active_id") or "") if isinstance(data, dict) else ""
    available_ids = {item["id"] for item in keys if item["status"] == "available"}
    if active_id not in available_ids:
        active_id = next((item["id"] for item in keys if item["status"] == "available"), "")
    return {"version": 1, "active_id": active_id, "keys": keys}


def _read_provider_key_pool(provider: str) -> tuple[dict[str, Any], bool]:
    raw = get_secret(provider_key_pool_name(provider))
    if raw:
        try:
            return _normalize_provider_key_pool(json.loads(raw)), False
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    legacy = get_secret(provider_key_name(provider)).strip()
    if legacy:
        item = _new_provider_key(legacy)
        return {"version": 1, "active_id": item["id"], "keys": [item]}, True
    return {"version": 1, "active_id": "", "keys": []}, False


def _write_provider_key_pool(provider: str, pool: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_provider_key_pool(pool)
    keys = normalized["keys"]
    if keys:
        set_secret(
            provider_key_pool_name(provider),
            json.dumps(normalized, ensure_ascii=False, separators=(",", ":")),
        )
    else:
        delete_secret(provider_key_pool_name(provider))

    active = next(
        (
            item
            for item in keys
            if item["id"] == normalized["active_id"] and item["status"] == "available"
        ),
        None,
    )
    if active:
        set_secret(provider_key_name(provider), active["value"])
    else:
        delete_secret(provider_key_name(provider))
    return normalized


def load_provider_key_pool(provider: str) -> dict[str, Any]:
    provider = (provider or "").strip()
    if not provider:
        return {"version": 1, "active_id": "", "keys": []}
    with locked(_provider_key_pool_lock_path()):
        pool, migrated = _read_provider_key_pool(provider)
        return _write_provider_key_pool(provider, pool) if migrated else pool


def replace_provider_api_keys(provider: str, values: list[str]) -> dict[str, Any]:
    provider = (provider or "").strip()
    clean: list[str] = []
    for value in values:
        key = str(value or "").strip()
        if key and key not in clean:
            clean.append(key)
    with locked(_provider_key_pool_lock_path()):
        keys = [_new_provider_key(value) for value in clean]
        pool = {
            "version": 1,
            "active_id": keys[0]["id"] if keys else "",
            "keys": keys,
        }
        return _write_provider_key_pool(provider, pool)


def _masked_provider_key(value: str) -> str:
    value = str(value or "")
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:3]}{'*' * min(8, max(4, len(value) - 7))}{value[-4:]}"


def list_provider_keys(provider: str) -> list[dict[str, Any]]:
    pool = load_provider_key_pool(provider)
    active_id = str(pool.get("active_id") or "")
    return [
        {
            "id": item["id"],
            "masked": _masked_provider_key(item["value"]),
            "status": item["status"],
            "active": item["id"] == active_id and item["status"] == "available",
            "failed_at": item.get("failed_at", ""),
            "retry_at": item.get("retry_at", ""),
            "failure_kind": item.get("failure_kind", ""),
            "failure_count": item.get("failure_count", 0),
            "failure_reason": item.get("failure_reason", ""),
        }
        for item in pool["keys"]
    ]


def add_provider_api_key(provider: str, value: str) -> dict[str, Any]:
    provider = (provider or "").strip()
    value = (value or "").strip()
    if not provider:
        raise ValueError("provider is required")
    if not value:
        raise ValueError("API key is required")
    with locked(_provider_key_pool_lock_path()):
        pool, _migrated = _read_provider_key_pool(provider)
        for item in pool["keys"]:
            if item["value"] == value:
                item.update(_available_key_fields())
                pool["active_id"] = item["id"]
                _write_provider_key_pool(provider, pool)
                return {
                    "id": item["id"],
                    "masked": _masked_provider_key(value),
                    "status": "available",
                    "active": True,
                    "failed_at": "",
                    "failure_reason": "",
                }
        item = _new_provider_key(value)
        pool["keys"].append(item)
        if not pool.get("active_id"):
            pool["active_id"] = item["id"]
        _write_provider_key_pool(provider, pool)
        return {
            "id": item["id"],
            "masked": _masked_provider_key(value),
            "status": "available",
            "active": pool["active_id"] == item["id"],
            "failed_at": "",
            "failure_reason": "",
        }


def remove_provider_api_key(provider: str, key_id: str) -> bool:
    provider = (provider or "").strip()
    key_id = (key_id or "").strip()
    with locked(_provider_key_pool_lock_path()):
        pool, _migrated = _read_provider_key_pool(provider)
        before = len(pool["keys"])
        pool["keys"] = [item for item in pool["keys"] if item["id"] != key_id]
        if len(pool["keys"]) == before:
            return False
        if pool.get("active_id") == key_id:
            pool["active_id"] = ""
        _write_provider_key_pool(provider, pool)
        return True


def _sanitize_reason(text: str) -> str:
    """Strip NUL and control characters (newlines kept) from external input."""
    cleaned = []
    for ch in str(text or ""):
        code = ord(ch)
        if ch == "\n" or (code >= 0x20 and code != 0x7F and not 0x80 <= code <= 0x9F):
            cleaned.append(ch)
    return "".join(cleaned)[:400]


def mark_provider_key_failed(provider: str, key_id: str, reason: str = "") -> bool:
    provider = (provider or "").strip()
    key_id = (key_id or "").strip()
    reason = _sanitize_reason(reason)
    from .core import classify_provider_key_failure

    classification = classify_provider_key_failure(1, "", reason)
    status = classification.get("status") or "restricted"
    with locked(_provider_key_pool_lock_path()):
        pool, _migrated = _read_provider_key_pool(provider)
        found = False
        for item in pool["keys"]:
            if item["id"] != key_id:
                continue
            item["status"] = status
            item["failed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            item["retry_at"] = classification.get("retry_at") or ""
            item["failure_kind"] = classification.get("failure_kind") or "unknown"
            item["failure_count"] = int(item.get("failure_count") or 0) + 1
            item["failure_reason"] = _sanitize_reason(
                classification.get("reason") or reason.strip()
            )
            found = True
            break
        if not found:
            return False
        if pool.get("active_id") == key_id:
            pool["active_id"] = ""
        _write_provider_key_pool(provider, pool)
        return True


def restore_provider_key(provider: str, key_id: str) -> bool:
    provider = (provider or "").strip()
    key_id = (key_id or "").strip()
    with locked(_provider_key_pool_lock_path()):
        pool, _migrated = _read_provider_key_pool(provider)
        found = False
        for item in pool["keys"]:
            if item["id"] != key_id:
                continue
            item.update(_available_key_fields())
            found = True
            break
        if not found:
            return False
        if not pool.get("active_id"):
            pool["active_id"] = key_id
        _write_provider_key_pool(provider, pool)
        return True


def restore_all_provider_keys(provider: str) -> int:
    provider = (provider or "").strip()
    with locked(_provider_key_pool_lock_path()):
        pool, _migrated = _read_provider_key_pool(provider)
        restored = 0
        for item in pool["keys"]:
            if item["status"] != "available":
                item.update(_available_key_fields())
                restored += 1
        if not pool.get("active_id"):
            pool["active_id"] = next((item["id"] for item in pool["keys"]), "")
        _write_provider_key_pool(provider, pool)
        return restored


def get_active_provider_credential(provider: str) -> dict[str, str] | None:
    pool = load_provider_key_pool(provider)
    active_id = str(pool.get("active_id") or "")
    for item in pool["keys"]:
        if item["id"] == active_id and item["status"] == "available":
            return {"key_id": item["id"], "value": item["value"]}
    return None


def delete_provider_api_keys(provider: str) -> None:
    with locked(_provider_key_pool_lock_path()):
        delete_secret(provider_key_pool_name(provider))
        delete_secret(provider_key_name(provider))


def provider_env_name(provider: str) -> str:
    """Return a stable, provider-scoped environment variable name."""
    provider = (provider or "").strip()
    slug = re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_")[:24] or "CUSTOM"
    digest = hashlib.sha256(provider.encode("utf-8")).hexdigest()[:12].upper()
    return f"PI_MANAGER_PROVIDER_{slug}_{digest}_API_KEY"


def provider_api_key_reference(provider: str) -> str:
    return f"${{{provider_env_name(provider)}}}"


def is_sensitive_header_name(header: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", str(header or "").lower())
    return any(
        marker in normalized
        for marker in ("authorization", "apikey", "token", "secret", "cookie")
    )


def provider_header_secret_name(provider: str, header: str) -> str:
    digest = hashlib.sha256(header.lower().encode("utf-8")).hexdigest()[:16]
    return f"provider:{provider}:header:{digest}"


def provider_header_env_name(provider: str, header: str) -> str:
    provider_part = provider_env_name(provider).removesuffix("_API_KEY")
    digest = hashlib.sha256(header.lower().encode("utf-8")).hexdigest()[:12].upper()
    return f"{provider_part}_HEADER_{digest}"


def store_provider_headers(provider: str, headers: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        header = str(name)
        raw = str(value or "")
        if not is_sensitive_header_name(header):
            result[header] = raw
            continue
        env_name = referenced_env_name(raw)
        managed_env = provider_header_env_name(provider, header)
        if env_name and env_name != managed_env:
            result[header] = f"${{{env_name}}}"
            continue
        if env_name == managed_env:
            result[header] = f"${{{managed_env}}}"
            continue
        if raw:
            set_secret(provider_header_secret_name(provider, header), raw)
            result[header] = f"${{{managed_env}}}"
        else:
            delete_secret(provider_header_secret_name(provider, header))
            result[header] = ""
    return result


def resolve_provider_header_value(provider: str, header: str, value: str) -> str:
    env_name = referenced_env_name(str(value or ""))
    if not env_name:
        return str(value or "")
    if env_name == provider_header_env_name(provider, header):
        return get_secret(provider_header_secret_name(provider, header))
    return os.environ.get(env_name, "")


def provider_header_runtime_env(provider: str, headers: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        env_name = referenced_env_name(str(value or ""))
        if not env_name:
            continue
        secret = resolve_provider_header_value(
            provider, str(name), str(value or "")
        )
        if secret:
            result[env_name] = secret
    return result


def delete_provider_header_secrets(provider: str, headers: dict[str, Any]) -> None:
    for name in headers:
        if is_sensitive_header_name(str(name)):
            delete_secret(provider_header_secret_name(provider, str(name)))


def referenced_env_name(value: str) -> str:
    val = (value or "").strip()
    match = re.fullmatch(r"\$(?:\{([A-Z][A-Z0-9_]*)\}|([A-Z][A-Z0-9_]*))", val)
    if match:
        return match.group(1) or match.group(2) or ""
    if re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", val):
        return val
    return ""


def store_provider_api_key(provider: str, api_key: str) -> str:
    provider = (provider or "").strip()
    if not provider:
        return api_key
    if not api_key:
        delete_provider_api_keys(provider)
        return ""
    if api_key.startswith("__DPAPI__:"):
        legacy_provider = api_key.split(":", 1)[1].strip() or provider
        credential = get_active_provider_credential(legacy_provider)
        if credential and legacy_provider != provider:
            replace_provider_api_keys(provider, [credential["value"]])
        return provider_api_key_reference(provider)
    if api_key.startswith("!"):
        delete_provider_api_keys(provider)
        return api_key
    env_name = referenced_env_name(api_key)
    if env_name:
        if env_name != provider_env_name(provider):
            delete_provider_api_keys(provider)
        return f"${{{env_name}}}"
    replace_provider_api_keys(provider, [api_key])
    return provider_api_key_reference(provider)


def resolve_provider_api_key(api_key_field: str, provider: str = "") -> str:
    val = (api_key_field or "").strip()
    if val.startswith("__DPAPI__:"):
        prov = val.split(":", 1)[1] or provider
        credential = get_active_provider_credential(prov)
        return credential["value"] if credential else ""
    env_name = referenced_env_name(val)
    if env_name:
        if provider and env_name == provider_env_name(provider):
            credential = get_active_provider_credential(provider)
            return (credential["value"] if credential else "") or os.environ.get(env_name, "")
        return os.environ.get(env_name, "")
    if provider:
        credential = get_active_provider_credential(provider)
        if credential and not val:
            return credential["value"]
    return val


def migrate_plaintext_keys(providers: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for name, entry in (providers or {}).items():
        if not isinstance(entry, dict):
            out[name] = entry
            continue
        e = dict(entry)
        key = str(e.get("apiKey") or "")
        if key.startswith("__DPAPI__:"):
            # Older configurations encoded the vault lookup name in the
            # marker. Preserve that association when a provider was renamed.
            e["apiKey"] = store_provider_api_key(name, key)
        elif key and not key.startswith("!"):
            env_name = referenced_env_name(key)
            if env_name:
                e["apiKey"] = f"${{{env_name}}}"
            else:
                e["apiKey"] = store_provider_api_key(name, key)
        headers = e.get("headers")
        if isinstance(headers, dict):
            e["headers"] = store_provider_headers(name, headers)
        out[name] = e
    return out


def backend_description() -> str:
    parts = []
    if _get_keyring() is not None:
        parts.append("OS keyring")
    if sys.platform == "win32":
        parts.append("DPAPI vault")
    else:
        parts.append("per-user file vault")
    return " + ".join(parts) if parts else "file vault"
