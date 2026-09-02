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

from . import platform_util
from .storage import locked

SERVICE = "PiManager"
logger = logging.getLogger(__name__)
_KEYRING = None
_KEYRING_TRIED = False
_KEYRING_TRIED_AT = 0.0
_KEYRING_RETRY_COOLDOWN = 60.0
_KEYRING_PROBE_TIMEOUT = 5.0
_KEYRING_UNAVAILABLE_REASON = ""
_KEYRING_FALLBACK_LOGGED = False

# 进程内热路径缓存：盐不变则 PBKDF2 密钥不变；vault 按路径+mtime+size 命中。
_KDF_LOCK = threading.Lock()
_KDF_CACHED_SALT: bytes | None = None
_KDF_CACHED_KEY: bytes | None = None
_VAULT_CACHE_LOCK = threading.Lock()
_VAULT_CACHE: tuple[tuple[str, int, int], dict[str, str]] | None = None


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


def _log_keyring_fallback_once(
    reason: str, *, exc: BaseException | None = None
) -> None:
    """OS keyring 不可用时记一条 warning（只一次），绝不写入密钥值。"""
    global _KEYRING_UNAVAILABLE_REASON, _KEYRING_FALLBACK_LOGGED
    _KEYRING_UNAVAILABLE_REASON = reason
    if not _KEYRING_FALLBACK_LOGGED:
        _KEYRING_FALLBACK_LOGGED = True
        logger.warning(
            "OS keyring 不可用（%s），已回退到文件保险库",
            reason,
        )
    if exc is not None:
        logger.debug("OS keyring 不可用的细节", exc_info=exc)


def _get_keyring():
    global _KEYRING, _KEYRING_TRIED, _KEYRING_TRIED_AT
    global _KEYRING_UNAVAILABLE_REASON, _KEYRING_FALLBACK_LOGGED
    if _KEYRING_TRIED:
        cooldown_ok = time.monotonic() - _KEYRING_TRIED_AT >= _KEYRING_RETRY_COOLDOWN
        if _KEYRING is None and cooldown_ok:
            _KEYRING_TRIED = False
        else:
            return _KEYRING
    _KEYRING_TRIED = True
    _KEYRING_TRIED_AT = time.monotonic()
    try:
        import keyring  # type: ignore
    except Exception as exc:
        _KEYRING = None
        _log_keyring_fallback_once("无法导入 keyring 模块", exc=exc)
        return _KEYRING
    outcome: dict[str, Any] = {}

    def probe() -> None:
        try:
            backend = keyring.get_keyring()
            if backend is None:
                outcome["reason"] = "未找到可用后端"
                return
            if _keyring_backend_is_unsafe(backend):
                outcome["reason"] = "明文文件后端已被拒绝"
                return
            # 只读探测：历史实现会向 keyring 写入并删除一条
            # __pi_manager_probe__ 记录，在 macOS 上可能弹出授权框、并在系统
            # 钥匙串审计日志留痕（R2 审计 P3-7）。读取一个不存在的条目已足以
            # 判断后端能否正常应答；「写不报错但读回是空」这类骗人后端由
            # set_secret 的写后回读校验兜住，不需要在探测阶段留下副作用。
            keyring.get_password(SERVICE, "__pi_manager_probe__")
            outcome["backend"] = keyring
        except Exception as exc:
            outcome["error"] = exc
            outcome["reason"] = "探测失败"

    thread = threading.Thread(target=probe, daemon=True, name="keyring-probe")
    thread.start()
    thread.join(_KEYRING_PROBE_TIMEOUT)
    if thread.is_alive():
        _KEYRING = None
        _log_keyring_fallback_once("探测超时")
    elif "error" in outcome:
        _KEYRING = None
        probe_exc = outcome.get("error")
        _log_keyring_fallback_once(
            str(outcome.get("reason") or "探测失败"),
            exc=probe_exc if isinstance(probe_exc, BaseException) else None,
        )
    elif outcome.get("backend") is not None:
        _KEYRING = outcome["backend"]
        _KEYRING_UNAVAILABLE_REASON = ""
        _KEYRING_FALLBACK_LOGGED = False
    else:
        _KEYRING = None
        _log_keyring_fallback_once(str(outcome.get("reason") or "未找到可用后端"))
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


def _ensure_regular_file(path: Path, *, what: str) -> None:
    """校验路径是普通文件而非 reparse point / 符号链接，防止符号链接劫持。"""
    try:
        path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return  # 不存在无妨
    except OSError as exc:
        raise VaultCorruptError(f"{what} 状态检查失败: {exc}") from exc
    if platform_util.is_reparse_point(path):
        raise VaultCorruptError(f"{what} 是重解析点/符号链接，拒绝读取/写入")


def _load_or_create_master_key() -> bytes:
    """Load or atomically create a 32-byte per-user fallback key.

    The key is derived from a random salt file via PBKDF2-HMAC-SHA256 with a
    fixed application pepper, so a simple file copy is insufficient to decrypt
    the vault — the attacker would also need to brute-force the KDF.
    """
    _ensure_dir()
    path = _master_key_path()
    if path.exists():
        return _validate_master_key_salt(path)
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
            return _validate_master_key_salt(path)
        except OSError:
            if path.exists():
                return _validate_master_key_salt(path)
            os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o600)
    return _validate_master_key_salt(path)


_PEPPER = b"PiManager::vault::v3::pbkdf2"
# PBKDF2-HMAC-SHA256 迭代次数：vault 主密钥派生与配置包加密共用同一强度。
KDF_ITERATIONS = 600_000
# 向后兼容：本模块内部历史名称。
_KDF_ITERATIONS = KDF_ITERATIONS

# 旧的无认证加密格式（filekey: XOR 流）与明文 JSON vault 默认一律拒绝：
# 它们既无完整性保护、也无法与「本地攻击者注入的凭据」区分，而读取成功后还会
# 被立刻重写成 DPAPI/AES-GCM，等于替攻击者「洗白」（R2 审计 P0-3，Windows 已
# 实证）。确属本机旧数据时，用户可显式打开一次性迁移开关启动一次完成升级；
# 开关默认关闭，且每次读取都记 WARNING 审计日志。
# local:（固定硬编码 key）不提供任何开关，已永久移除。
_LEGACY_DECRYPT_ALLOWED = False
_LEGACY_MIGRATION_ENV = "PI_MANAGER_ALLOW_LEGACY_VAULT"


def _legacy_migration_enabled() -> bool:
    """True 时允许一次性读取无认证旧格式 / 明文 JSON vault。"""
    if _LEGACY_DECRYPT_ALLOWED:
        return True
    return os.environ.get(_LEGACY_MIGRATION_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _derive_key_from_salt(salt: bytes) -> bytes:
    """Derive a 32-byte AES key from a salt using PBKDF2 + fixed pepper.

    相同盐字节命中进程内最近一条缓存，避免 get_secret 热路径重复 600k PBKDF2。
    """
    global _KDF_CACHED_SALT, _KDF_CACHED_KEY
    salt_key = bytes(salt)
    with _KDF_LOCK:
        if _KDF_CACHED_SALT == salt_key and _KDF_CACHED_KEY is not None:
            return _KDF_CACHED_KEY
    key = hashlib.pbkdf2_hmac("sha256", _PEPPER, salt_key, _KDF_ITERATIONS, dklen=32)
    with _KDF_LOCK:
        _KDF_CACHED_SALT = salt_key
        _KDF_CACHED_KEY = key
    return key


def _clear_runtime_caches() -> None:
    """清掉进程内 KDF / vault 缓存与 keyring 回退日志标记。

    测试的 isolated_home 在切换 HOME 后必须调用，避免上一用例的盐或
    vault 路径命中缓存。
    """
    global _KDF_CACHED_SALT, _KDF_CACHED_KEY, _VAULT_CACHE
    global _KEYRING_FALLBACK_LOGGED, _KEYRING_UNAVAILABLE_REASON
    with _KDF_LOCK:
        _KDF_CACHED_SALT = None
        _KDF_CACHED_KEY = None
    with _VAULT_CACHE_LOCK:
        _VAULT_CACHE = None
    _KEYRING_FALLBACK_LOGGED = False
    _KEYRING_UNAVAILABLE_REASON = ""


def _validate_master_key_salt(path: Path) -> bytes:
    """Read and validate the salt file (32 bytes, regular, 0600).

    历史上本模块有两份语义完全相同的实现（``_validate_master_key`` 与本函数），
    且 ``_load_or_create_master_key`` 在不同返回路径上分别调用其中一个——命名
    （"master key" vs "salt"）与实际语义（都是盐）矛盾，后续重构极易只改一份
    而引入回退（R2 审计 P3-5）。现在只保留这一份实现。
    """
    if platform_util.is_reparse_point(path):
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
    """仅用于 ``filekey:`` 旧格式的一次性迁移解密。

    ``decrypt_blob`` 只在 ``_legacy_migration_enabled()`` 为真时调用本函数。
    ``local:`` 固定密钥格式无迁移路径、永久拒绝，不得用本函数解密。
    """
    if not key:
        raise ValueError("empty key")
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def encrypt_blob(data: bytes) -> bytes:
    """Encrypt bytes for on-disk vault."""
    if sys.platform == "win32":
        try:
            return b"dpapi:" + base64.b64encode(_dpapi_protect(data))
        except Exception:
            logger.debug("DPAPI 加密失败，回退 AES-GCM 文件保险库", exc_info=True)
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
        # 无认证 XOR（_xor_stream）：默认拒绝；仅迁移开关打开时可读，下次写入升级。
        # 与 local: 不同：local: 无迁移路径、永久拒绝（密钥硬编码在程序内）。
        if not _legacy_migration_enabled():
            raise VaultCorruptError(
                "旧的无认证加密格式（filekey:）默认已禁用（XOR 无完整性保护，"
                "无法与凭据注入区分）；确属本机旧数据请设置环境变量 "
                f"{_LEGACY_MIGRATION_ENV}=1 后启动一次完成迁移"
            )
        key = _get_master_key()
        plaintext = _xor_stream(base64.b64decode(raw[8:]), key)
        logger.warning(
            "读取了无认证的旧格式 vault 条目（filekey:），将在下次写入时升级为 AES-GCM"
        )
        return plaintext
    if raw.startswith(b"local:"):
        # 该格式的 XOR key 硬编码并随二进制分发，任何本地写入者都能零知识伪造
        # 出合法密文，注入后还会被重写成 DPAPI 格式「洗白」（R2 审计 P0-3，已
        # 实证）。因此无条件移除、不提供迁移开关——保留开关等于保留一条对所有
        # 平台都成立的凭据注入通道。不走 _xor_stream。
        raise VaultCorruptError(
            "旧的固定密钥加密格式（local:）已永久移除：其加密密钥硬编码在程序内，"
            "任何本地进程都能伪造，无法与凭据注入区分。请重新填写 API Key"
        )
    # raw dpapi blob (old whole-file format)
    if sys.platform == "win32":
        try:
            return _dpapi_unprotect(raw)
        except Exception:
            logger.debug("无法按旧版 DPAPI 整文件格式解密 vault", exc_info=True)
    raise VaultCorruptError("vault 数据无法识别加密格式")


def _vault_stat_token(path: Path) -> tuple[str, int, int]:
    """``(绝对路径, mtime_ns, size)``；文件不存在时 mtime/size 为 0。"""
    abs_path = os.path.normcase(os.path.abspath(str(path)))
    try:
        info = path.stat()
    except FileNotFoundError:
        return (abs_path, 0, 0)
    return (abs_path, info.st_mtime_ns, info.st_size)


def _invalidate_vault_cache() -> None:
    global _VAULT_CACHE
    with _VAULT_CACHE_LOCK:
        _VAULT_CACHE = None


def _vault_cache_get() -> dict[str, str] | None:
    token = _vault_stat_token(_vault_path())
    with _VAULT_CACHE_LOCK:
        cached = _VAULT_CACHE
        if cached is None or cached[0] != token:
            return None
        return dict(cached[1])


def _vault_cache_put(data: dict[str, str]) -> dict[str, str]:
    """写入缓存并返回独立拷贝（调用方不得改到缓存对象）。"""
    global _VAULT_CACHE
    snapshot = dict(data)
    token = _vault_stat_token(_vault_path())
    with _VAULT_CACHE_LOCK:
        _VAULT_CACHE = (token, snapshot)
    return dict(snapshot)


def load_vault() -> dict[str, str]:
    """Load the merged vault: primary ``secrets.vault`` first, legacy as fallback.

    The legacy file (``secrets.dpapi``) is only consulted when the primary vault
    is missing or unreadable. When the primary vault is valid and the legacy
    file is a strict subset of it, the leftover legacy file is removed; if the
    legacy file holds entries the primary vault does not have, it is kept and a
    warning is logged so no secret is silently dropped.
    """
    _ensure_dir()
    hit = _vault_cache_get()
    if hit is not None:
        return hit
    with locked(_vault_path()):
        hit = _vault_cache_get()
        if hit is not None:
            return hit
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
                    logger.debug("legacy vault 提升到主 vault 失败", exc_info=True)
                    # 提升失败：不缓存，下次仍尝试迁移。
                    return dict(legacy_data)
                return _vault_cache_put(legacy_data)
            if found:
                raise VaultCorruptError(
                    "Vault 无法解密或解析；原文件未被修改。" + " | ".join(errors)
                )
            return _vault_cache_put({})

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
                logger.warning(
                    "legacy vault %s 含有主 vault 没有的条目，保留文件待处理：%s",
                    legacy_path,
                    "、".join(sorted(legacy_data)),
                )
        return _vault_cache_put(primary)


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
        # 明文 JSON vault 只可能来自极早期版本。历史守卫用「.vault_master_key
        # 是否存在」判断 vault 是否已初始化为加密格式，但 Windows 上
        # encrypt_blob 优先走 DPAPI，_get_master_key() 从不被调用 → 该盐文件
        # 永不存在 → 守卫恒为假，任意可写 vault 的本地主体都能用明文 JSON 覆盖
        # 并完成凭据注入（R2 审计 P0-3，Windows 已实证）。现在不再依赖该文件：
        # 能走到这里说明 vault 文件已存在，默认一律拒绝明文，只有显式打开一次性
        # 迁移开关时才接受。
        if _master_key_path().exists():
            # 盐文件存在 = vault 确定已初始化为加密格式，明文只能是注入，
            # 连一次性迁移开关都不该放行。
            raise VaultCorruptError(
                "vault 已初始化为加密格式（主密钥盐文件已存在），"
                "拒绝接受明文 JSON（疑似凭据注入）"
            ) from exc
        if not _legacy_migration_enabled():
            raise VaultCorruptError(
                "vault 不是受认证保护的加密格式，拒绝作为明文 JSON 读取"
                "（疑似凭据注入）；确属旧版本遗留请设置环境变量 "
                f"{_LEGACY_MIGRATION_ENV}=1 后启动一次完成迁移"
            ) from exc
        try:
            text = raw.decode("utf-8", errors="strict")
        except Exception as text_exc:
            raise VaultCorruptError(f"vault 解密失败且无法作为文本解析: {exc}") from text_exc
        logging.getLogger(__name__).warning(
            "以一次性迁移模式读取了明文 JSON vault %s，将立即重写为认证加密格式",
            path,
        )
    data = json.loads(text or "{}")
    if not isinstance(data, dict):
        raise ValueError("Vault 顶层必须是 JSON 对象")
    result = {str(k): str(v) for k, v in data.items()}
    if rewrite_legacy_format and not raw.startswith((b"dpapi:", b"aesgcm:")):
        # filekey: XOR（仅迁移开关）、裸 DPAPI、明文 JSON 读到即升级。
        # local: 不会走到这里：decrypt_blob 已永久拒绝，不提供迁移。
        _save_vault_unlocked(result)
    return result


def _save_vault_unlocked(data: dict[str, str]) -> None:
    _invalidate_vault_cache()
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
        _vault_cache_put(data)
    finally:
        temp.unlink(missing_ok=True)


def save_vault(data: dict[str, str]) -> None:
    _ensure_dir()
    with locked(_vault_path()):
        _save_vault_unlocked(data)


def _load_index() -> set[str]:
    path = _index_path()
    _ensure_regular_file(path, what="secrets index")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(item) for item in data if isinstance(item, str)} if isinstance(data, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def _save_index(names: set[str]) -> None:
    _ensure_dir()
    path = _index_path()
    _ensure_regular_file(path, what="secrets index")
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
        # 空值删除路径：仅当 keyring 删除已确认成功或 keyring 完全不可用
        # 时才清理 vault 副本；keyring 删除抛异常时必须保留 vault 副本，
        # 避免密钥在 keyring 与 vault 两端同时丢失。
        keyring_delete_confirmed = False
        if kr is not None:
            try:
                if value:
                    kr.set_password(SERVICE, name, value)
                    # 写后立刻回读：keyring 探测改为只读后（P3-7），必须在这里
                    # 确认后端真的持久化了，才敢删掉 vault 里的副本——否则一个
                    # 「写不报错但读回是空」的后端会让密钥在两端同时丢失。
                    keyring_saved = kr.get_password(SERVICE, name) == value
                else:
                    try:
                        kr.delete_password(SERVICE, name)
                    except Exception:
                        logger.debug(
                            "从 OS keyring 删除失败，将保留文件保险库副本",
                            exc_info=True,
                        )
                        keyring_delete_confirmed = False
                    else:
                        keyring_delete_confirmed = True
            except Exception:
                logger.debug(
                    "写入 OS keyring 失败或写后回读未确认，改写入文件保险库",
                    exc_info=True,
                )
                kr = None
        vault = load_vault()
        if value:
            if keyring_saved:
                vault.pop(name, None)
            else:
                vault[name] = value
        elif keyring_delete_confirmed or kr is None:
            vault.pop(name, None)
        # else: keyring 删除失败，保留 vault 副本作为最后防线
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
            logger.debug("从 OS keyring 读取失败，回退到文件保险库", exc_info=True)
    return str(load_vault().get(name) or "")


def delete_secret(name: str) -> None:
    name = (name or "").strip()
    with locked(_mutation_lock_path()):
        kr = _get_keyring()
        if kr is not None:
            try:
                kr.delete_password(SERVICE, name)
            except Exception:
                logger.debug(
                    "从 OS keyring 删除失败，继续清理文件保险库",
                    exc_info=True,
                )
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
            logger.debug("secrets index 自愈写入失败", exc_info=True)
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


# 掩码固定用 8 个星号：既不暴露密钥长度，也把可见明文压到前 2 + 后 2。旧实现
# 暴露前 3 + 后 4，对 `sk-` 这类已知前缀的 provider 有效熵损失可观（R2 审计
# P3-2）。需要区分同一 provider 的多把 Key 时请用 key_id。
_MASK_BODY = "*" * 8


def _masked_provider_key(value: str) -> str:
    value = str(value or "")
    if len(value) <= 8:
        return _MASK_BODY
    return f"{value[:2]}{_MASK_BODY}{value[-2:]}"


def list_provider_keys(provider: str, *, reveal: bool = False) -> list[dict[str, Any]]:
    """列出 provider 的密钥池元数据。

    默认只返回掩码（``masked``），与既有调用方约定一致；``reveal=True``
    仅限 GUI 密钥管理对话框的「显示明文」显式请求，附带 ``value`` 字段。
    调用方不得把 ``value`` 写入 models.json / 日志 / 导出包。
    """
    pool = load_provider_key_pool(provider)
    active_id = str(pool.get("active_id") or "")
    rows: list[dict[str, Any]] = []
    for item in pool["keys"]:
        row = {
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
        if reveal:
            row["value"] = item["value"]
        rows.append(row)
    return rows


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


def _clean_control_chars(text: str) -> str:
    """去掉 NUL 与控制字符（保留换行），**不截断**。"""
    cleaned = []
    for ch in str(text or ""):
        code = ord(ch)
        if ch == "\n" or (code >= 0x20 and code != 0x7F and not 0x80 <= code <= 0x9F):
            cleaned.append(ch)
    return "".join(cleaned)


def _sanitize_reason(text: str) -> str:
    """清洗 + 截断，用于**落库/展示**。

    分类不能用截断后的串：`classify_provider_key_failure` 靠 401/429/quota 这类
    标志判断是鉴权、限流还是额度问题，而这些标志往往出现在上游错误串的**尾部**，
    在 400 字符处截断可能正好把它切掉，于是同一个错误在桌面端与扩展端会被分成
    不同类别（R2 扩展审计 D1）。所以分类走 `_clean_control_chars`（不截断），
    只有写进 vault / 展示给用户时才截断。
    """
    return _clean_control_chars(text)[:400]


def mark_provider_key_failed(provider: str, key_id: str, reason: str = "") -> bool:
    provider = (provider or "").strip()
    key_id = (key_id or "").strip()
    # 分类用完整串（标志常在尾部），落库时才截断——见 _sanitize_reason 的说明。
    reason = _clean_control_chars(reason)
    from .core import classify_provider_key_failure, redact_secret_values

    classification = classify_provider_key_failure(1, "", reason)
    status = classification.get("status") or "restricted"
    with locked(_provider_key_pool_lock_path()):
        pool, _migrated = _read_provider_key_pool(provider)
        # 脱敏：reason 可能混入密钥片段（如 "api key sk-xxx invalid"），
        # 用池内全部密钥值做替换，避免密钥片段被写入 vault 并在界面展示。
        # 与 classify_provider_key_failure 一样采用函数内延迟导入，避免顶层循环。
        secret_values = [str(item.get("value") or "") for item in pool["keys"]]
        safe_reason = _sanitize_reason(
            redact_secret_values(
                classification.get("reason") or reason.strip(), secret_values
            )
        )
        found = False
        for item in pool["keys"]:
            if item["id"] != key_id:
                continue
            item["status"] = status
            item["failed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            item["retry_at"] = classification.get("retry_at") or ""
            item["failure_kind"] = classification.get("failure_kind") or "unknown"
            item["failure_count"] = int(item.get("failure_count") or 0) + 1
            item["failure_reason"] = safe_reason
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


def store_provider_headers(
    provider: str, headers: dict[str, Any], *, trusted: bool = True
) -> dict[str, str]:
    """把敏感 Header 值迁入安全存储并换成引用。

    ``trusted=False``（外部输入）时不承认裸变量名，否则一个导入的配置包可以写
    `Authorization: SOME_ENV_NAME` 把用户环境里的凭据解析出来（同 P1-2）。
    """
    result: dict[str, str] = {}
    for name, value in headers.items():
        header = str(name)
        raw = str(value or "")
        if not is_sensitive_header_name(header):
            result[header] = raw
            continue
        env_name = _env_reference_name(raw, trusted=trusted)
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
    # 运行时读的是本机 models.json（受信任），沿用裸变量名兼容以免旧配置的
    # Header 引用突然被当成字面值发出去。
    env_name = _env_reference_name(str(value or ""))
    if not env_name:
        return str(value or "")
    if env_name == provider_header_env_name(provider, header):
        return get_secret(provider_header_secret_name(provider, header))
    return os.environ.get(env_name, "")


def provider_header_runtime_env(provider: str, headers: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for name, value in headers.items():
        env_name = _env_reference_name(str(value or ""))
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


# 旧版环境变量名必须形如 FOO_BAR（至少一个下划线分段）才有资格走兼容迁移；
# 上界 64 字符。真实凭据（`AKIAIOSFODNN7EXAMPLE`、大写十六进制 token）几乎
# 不会同时满足「含下划线分段」与「该名字确实存在于当前进程环境」两个条件。
_LEGACY_BARE_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")


def referenced_env_name(value: str) -> str:
    """解析显式环境变量引用名（`$NAME` / `${NAME}`），否则返回空串。

    历史实现还把「任意 ≥3 字符的全大写串」当成环境变量名，导致 AWS 风格的
    真实 Access Key ID（`AKIAIOSFODNN7EXAMPLE`）被判为引用：安全存储里的记录
    被删除，**密钥本身**被包成 `${AKIAIOSFODNN7EXAMPLE}` 明文写进 models.json
    并原样进入未加密导出 ZIP，同时 provider 因变量不存在而静默失效
    （R2 审计 P1-2，已实证）。这条启发式已删除——`help_docs.py` 早已声明外部
    环境变量必须显式写成 `$NAME` / `${NAME}`。裸变量名的向后兼容迁移见
    :func:`_legacy_bare_env_name`，只在受信任的本机配置迁移路径上生效。
    """
    val = (value or "").strip()
    match = re.fullmatch(r"\$(?:\{([A-Z][A-Z0-9_]*)\}|([A-Z][A-Z0-9_]*))", val)
    if match:
        return match.group(1) or match.group(2) or ""
    return ""


def _legacy_bare_env_name(value: str) -> str:
    """把旧配置里的裸变量名迁移成显式引用，无法确认时视为真实密钥。

    只有同时满足「形如 FOO_BAR 的常规变量名」「长度 ≤ 64」「该变量确实存在于
    当前进程环境且非空」时才承认，其余一律返回空串——交由调用方当成真实密钥
    存入安全存储。这个方向是安全的：最坏情况是用户真填了一个当前未设置的变量
    名，它会被当作字面密钥保管、provider 报鉴权失败（可恢复、无泄露），而不是
    像旧行为那样把真实密钥明文写进 models.json（不可恢复、直接泄露）。
    """
    val = (value or "").strip()
    if len(val) > 64 or not _LEGACY_BARE_ENV_NAME_RE.fullmatch(val):
        return ""
    if not os.environ.get(val, "").strip():
        return ""
    logging.getLogger(__name__).warning(
        "provider 凭据字段使用了旧式裸环境变量名 %s，已按 ${%s} 迁移；"
        "请在 Provider 编辑页改为显式 ${%s} 形式",
        val,
        val,
        val,
    )
    return val


def _env_reference_name(value: str, *, trusted: bool = True) -> str:
    """显式引用优先，其次（仅受信任输入）旧式裸变量名兼容迁移。

    ``trusted=False`` 用于外部输入（配置包导入）：此时不承认裸变量名，避免恶意
    配置包用 `OPENAI_API_KEY` 之类的名字把用户环境里的真实密钥解析出来、发往
    攻击者控制的 baseUrl。
    """
    name = referenced_env_name(value)
    if name or not trusted:
        return name
    return _legacy_bare_env_name(value)


_UNTRUSTED_DPAPI_MARKER_ERROR = (
    "拒绝处理 __DPAPI__: 历史标记：该标记只服务于本机旧配置的一次性迁移，"
    "不接受来自配置包等外部输入——它可以声明「我的 Key 存在另一个 Provider "
    "名下」，被用来把已有 Provider 的真实密钥复制给攻击者控制 baseUrl 的新 "
    "Provider。请在 Provider 编辑页重新填写 API Key"
)


def store_provider_api_key(provider: str, api_key: str, *, trusted: bool = True) -> str:
    """把 apiKey 字段落到安全存储，返回写回 models.json 的引用字符串。

    ``trusted=False`` 表示 ``api_key`` 直接来自外部输入（配置包导入）：此时
    ``__DPAPI__:`` 历史标记一律拒绝、裸变量名不做兼容迁移（R2 审计 P0-2、
    P1-2）。受信任路径（本机 models.json 迁移、Provider 编辑页保存）保持原有
    行为，避免升级用户丢失重命名 Provider 的密钥关联。
    """
    provider = (provider or "").strip()
    if not provider:
        return api_key
    if not api_key:
        delete_provider_api_keys(provider)
        return ""
    if api_key.startswith("__DPAPI__:"):
        if not trusted:
            raise ValueError(_UNTRUSTED_DPAPI_MARKER_ERROR)
        legacy_provider = api_key.split(":", 1)[1].strip() or provider
        credential = get_active_provider_credential(legacy_provider)
        if credential and legacy_provider != provider:
            # 留审计痕迹：跨 provider 复制是 P0-2 的核心动作，即使在受信任的
            # 本机迁移路径上也应该可追溯。
            logging.getLogger(__name__).warning(
                "按 __DPAPI__ 历史标记把 Provider %s 的密钥关联迁移到 %s"
                "（本机旧配置迁移）",
                legacy_provider,
                provider,
            )
            replace_provider_api_keys(provider, [credential["value"]])
        return provider_api_key_reference(provider)
    if api_key.startswith("!"):
        delete_provider_api_keys(provider)
        return api_key
    env_name = _env_reference_name(api_key, trusted=trusted)
    if env_name:
        if env_name != provider_env_name(provider):
            delete_provider_api_keys(provider)
        return f"${{{env_name}}}"
    replace_provider_api_keys(provider, [api_key])
    return provider_api_key_reference(provider)


def resolve_provider_api_key(
    api_key_field: str, provider: str = "", *, trusted: bool = True
) -> str:
    val = (api_key_field or "").strip()
    if val.startswith("__DPAPI__:"):
        if not trusted:
            raise ValueError(_UNTRUSTED_DPAPI_MARKER_ERROR)
        prov = val.split(":", 1)[1] or provider
        credential = get_active_provider_credential(prov)
        return credential["value"] if credential else ""
    env_name = _env_reference_name(val, trusted=trusted)
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


def migrate_plaintext_keys(
    providers: dict[str, Any], *, trusted: bool = True
) -> dict[str, Any]:
    """把 providers 里的明文凭据迁入安全存储并换成引用。

    ``trusted=False`` 用于配置包导入这类外部输入：拒绝 ``__DPAPI__:`` 标记、
    不做裸变量名兼容迁移（R2 审计 P0-2、P1-2）。
    """
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
            e["apiKey"] = store_provider_api_key(name, key, trusted=trusted)
        elif key and not key.startswith("!"):
            env_name = _env_reference_name(key, trusted=trusted)
            if env_name:
                e["apiKey"] = f"${{{env_name}}}"
            else:
                e["apiKey"] = store_provider_api_key(name, key, trusted=trusted)
        headers = e.get("headers")
        if isinstance(headers, dict):
            e["headers"] = store_provider_headers(name, headers, trusted=trusted)
        out[name] = e
    return out


def using_os_keyring() -> bool:
    """当前进程是否正在使用 OS keyring（而非仅文件保险库回退）。"""
    return _get_keyring() is not None


def backend_description() -> str:
    """当前密钥后端的用户可见说明（不含任何密钥值）。"""
    if using_os_keyring():
        if sys.platform == "win32":
            return "OS keyring + DPAPI vault"
        return "OS keyring + per-user file vault"
    reason = _KEYRING_UNAVAILABLE_REASON.strip() or "未启用或探测失败"
    if sys.platform == "win32":
        return (
            f"文件保险库回退（OS keyring 不可用：{reason}；当前使用 DPAPI vault）"
        )
    return f"文件保险库回退（OS keyring 不可用：{reason}）"
