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
import os
import re
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from .storage import locked

SERVICE = "PiManager"
_KEYRING = None
_KEYRING_TRIED = False


def _vault_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / "secrets.vault"


def _legacy_vault_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / "secrets.dpapi"


def _master_key_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / ".vault_master_key"


def _index_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / "secrets.index.json"


def _mutation_lock_path() -> Path:
    return Path(os.path.expanduser("~")) / ".pi" / "agent" / "secrets.mutation"


def _ensure_dir() -> None:
    _vault_path().parent.mkdir(parents=True, exist_ok=True)


def _get_keyring():
    global _KEYRING, _KEYRING_TRIED
    if _KEYRING_TRIED:
        return _KEYRING
    _KEYRING_TRIED = True
    try:
        import keyring  # type: ignore

        # probe
        keyring.get_password(SERVICE, "__pi_manager_probe__")
        _KEYRING = keyring
    except Exception:
        _KEYRING = None
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


def _load_or_create_master_key() -> bytes:
    """Random 32-byte key stored with restrictive permissions (portable fallback)."""
    _ensure_dir()
    path = _master_key_path()
    if path.exists():
        return path.read_bytes()
    key = os.urandom(32)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_bytes(key)
        try:
            os.link(temp, path)
        except FileExistsError:
            return path.read_bytes()
        except OSError:
            if path.exists():
                return path.read_bytes()
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


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

    key = _load_or_create_master_key()
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
        return AESGCM(_load_or_create_master_key()).decrypt(
            payload[:12], payload[12:], b"PiManagerVault:v2"
        )
    if raw.startswith(b"filekey:"):
        # Legacy unauthenticated fallback; successful reads are upgraded on next write.
        key = _load_or_create_master_key()
        return _xor_stream(base64.b64decode(raw[8:]), key)
    # legacy local: fixed-key tokens (migrate away)
    if raw.startswith(b"local:"):
        # old fixed key — still decrypt for migration only
        legacy = b"PiManagerLocalFallbackKey!v1"
        return _xor_stream(base64.b64decode(raw[6:]), legacy)
    # raw dpapi blob (old whole-file format)
    if sys.platform == "win32":
        try:
            return _dpapi_unprotect(raw)
        except Exception:
            pass
    return raw


def encrypt_text(text: str) -> str:
    blob = encrypt_blob((text or "").encode("utf-8"))
    return blob.decode("ascii", errors="ignore")


def decrypt_text(token: str) -> str:
    token = (token or "").strip()
    if not token:
        return ""
    try:
        return decrypt_blob(token.encode("ascii")).decode("utf-8", errors="replace")
    except Exception:
        return token


def load_vault() -> dict[str, str]:
    _ensure_dir()
    with locked(_vault_path()):
        for path in (_vault_path(), _legacy_vault_path()):
            if not path.exists():
                continue
            try:
                raw = path.read_bytes()
                if raw.startswith((b"dpapi:", b"aesgcm:", b"filekey:", b"local:")):
                    text = decrypt_blob(raw).decode("utf-8", errors="strict")
                else:
                    try:
                        text = decrypt_blob(raw).decode("utf-8", errors="strict")
                    except Exception:
                        text = raw.decode("utf-8", errors="strict")
                data = json.loads(text or "{}")
                if isinstance(data, dict):
                    return {str(k): str(v) for k, v in data.items()}
            except Exception:
                continue
    return {}


def save_vault(data: dict[str, str]) -> None:
    _ensure_dir()
    with locked(_vault_path()):
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        blob = encrypt_blob(payload)
        temp = _vault_path().with_name(
            f".{_vault_path().name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            temp.write_bytes(blob)
            os.replace(temp, _vault_path())
            try:
                os.chmod(_vault_path(), 0o600)
            except OSError:
                pass
        finally:
            temp.unlink(missing_ok=True)


def _load_index() -> set[str]:
    try:
        data = json.loads(_index_path().read_text(encoding="utf-8"))
        return {str(item) for item in data if isinstance(item, str)} if isinstance(data, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def _save_index(names: set[str]) -> None:
    _ensure_dir()
    path = _index_path()
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temp.write_text(json.dumps(sorted(names), ensure_ascii=False, indent=2), encoding="utf-8")
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
    names = set(load_vault().keys()) | _load_index()
    return sorted(names)


def provider_key_name(provider: str) -> str:
    return f"provider:{provider}:apiKey"


def provider_env_name(provider: str) -> str:
    """Return a stable, provider-scoped environment variable name."""
    provider = (provider or "").strip()
    slug = re.sub(r"[^A-Z0-9]+", "_", provider.upper()).strip("_")[:24] or "CUSTOM"
    digest = hashlib.sha256(provider.encode("utf-8")).hexdigest()[:12].upper()
    return f"PI_MANAGER_PROVIDER_{slug}_{digest}_API_KEY"


def provider_api_key_reference(provider: str) -> str:
    return f"${{{provider_env_name(provider)}}}"


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
        delete_secret(provider_key_name(provider))
        return ""
    if api_key.startswith("__DPAPI__:"):
        legacy_provider = api_key.split(":", 1)[1].strip() or provider
        legacy_value = get_secret(provider_key_name(legacy_provider))
        if legacy_value and legacy_provider != provider:
            set_secret(provider_key_name(provider), legacy_value)
        return provider_api_key_reference(provider)
    if api_key.startswith("!"):
        delete_secret(provider_key_name(provider))
        return api_key
    env_name = referenced_env_name(api_key)
    if env_name:
        if env_name != provider_env_name(provider):
            delete_secret(provider_key_name(provider))
        return f"${{{env_name}}}"
    set_secret(provider_key_name(provider), api_key)
    return provider_api_key_reference(provider)


def resolve_provider_api_key(api_key_field: str, provider: str = "") -> str:
    val = (api_key_field or "").strip()
    if val.startswith("__DPAPI__:"):
        prov = val.split(":", 1)[1] or provider
        return get_secret(provider_key_name(prov))
    env_name = referenced_env_name(val)
    if env_name:
        if provider and env_name == provider_env_name(provider):
            return get_secret(provider_key_name(provider)) or os.environ.get(env_name, "")
        return os.environ.get(env_name, "")
    if provider:
        secured = get_secret(provider_key_name(provider))
        if secured and not val:
            return secured
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
