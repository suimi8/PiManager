# -*- coding: utf-8 -*-
"""凭据与 Provider 密钥：失效分类 / 环境变量解析 / 运行时凭据。

从 ``core.py`` 抽出。对 core 配置函数（get_provider_config / models_path /
_invalidate_config_cache / load_models_config）用函数内延迟 import。
core.py 顶部重新导出这些符号以保持 ``core.xxx`` 调用兼容。
"""
from __future__ import annotations

import os
import re
import unicodedata
from typing import Any

from . import storage


class ProviderKeyError(RuntimeError):
    """Raised when a selected custom provider has no usable credential."""


_PROVIDER_SERVER_ERROR_PATTERN = r"\b(?:http\s*)?5\d\d\b"
_PROVIDER_KEY_HTTP_STATUS_PATTERN = r"\b(?:http\s*)?(?:401|403|429)\b"
_PROVIDER_REQUEST_BLOCK_PATTERNS: tuple[str, ...] = (
    r"\byour request (?:was|is|has been) blocked\b",
    r"\brequest (?:was |is |has been )?blocked\b",
    r"\bblocked by\b[^\n]{0,80}\b(?:waf|firewall|security|policy|cloudflare)\b",
    r"\b(?:waf|firewall|cloudflare)\b[^\n]{0,80}\b(?:blocked?|denied)\b",
)


def _is_provider_request_block_error(text: str) -> bool:
    return any(
        re.search(pattern, text or "", re.IGNORECASE)
        for pattern in _PROVIDER_REQUEST_BLOCK_PATTERNS
    )


def classify_provider_key_failure(
    returncode: int, stdout: str, stderr: str
) -> dict[str, str]:
    """Classify a provider failure into the shared key state machine."""
    from datetime import datetime, timedelta, timezone

    combined = f"{stdout or ''}\n{stderr or ''}"
    low = combined.lower()
    if int(returncode or 0) == 0 or _is_provider_request_block_error(combined):
        return {"status": "", "failure_kind": "", "reason": "", "retry_at": ""}
    if re.search(_PROVIDER_SERVER_ERROR_PATTERN, combined, re.IGNORECASE) and not re.search(
        _PROVIDER_KEY_HTTP_STATUS_PATTERN, combined, re.IGNORECASE
    ):
        return {"status": "", "failure_kind": "", "reason": "", "retry_at": ""}
    if re.search(r"\b(?:http\s*)?429\b|rate(?:\s+|_|-)limit|too(?:\s+|_|-)many(?:\s+|_|-)requests", low):
        retry_at = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(
            timespec="seconds"
        )
        return {
            "status": "cooldown",
            "failure_kind": "rate_limit",
            "reason": "HTTP 429",
            "retry_at": retry_at,
        }
    if re.search(r"(?:quota|credit|billing)", low):
        return {
            "status": "restricted",
            "failure_kind": "account_restricted",
            "reason": "quota or billing restriction",
            "retry_at": "",
        }
    if re.search(r"\b(?:http\s*)?401\b", low):
        return {
            "status": "invalid",
            "failure_kind": "invalid_credential",
            "reason": "HTTP 401",
            "retry_at": "",
        }
    if re.search(r"\b(?:http\s*)?403\b", low):
        revoked = bool(
            re.search(
                r"invalid(?:\s+|_|-)api(?:\s+|_|-)key|(?:revoked|disabled|invalid)(?:\s+|_|-)(?:api(?:\s+|_|-)?key|credential)",
                low,
            )
        )
        return {
            "status": "invalid" if revoked else "restricted",
            "failure_kind": "invalid_credential" if revoked else "permission_restricted",
            "reason": "HTTP 403",
            "retry_at": "",
        }
    if re.search(r"invalid(?:\s+|_|-)api(?:\s+|_|-)key|unauthori[sz]ed", low):
        return {
            "status": "invalid",
            "failure_kind": "invalid_credential",
            "reason": "invalid API key",
            "retry_at": "",
        }
    if re.search(r"authentication(?:\s+failed|\s+error|\s+required)?", low):
        return {
            "status": "invalid",
            "failure_kind": "authentication_failed",
            "reason": "authentication failed",
            "retry_at": "",
        }
    return {"status": "", "failure_kind": "", "reason": "", "retry_at": ""}


def provider_key_failure_reason(returncode: int, stdout: str, stderr: str) -> str:
    return classify_provider_key_failure(returncode, stdout, stderr)["reason"]


def is_provider_key_error(returncode: int, stdout: str, stderr: str) -> bool:
    return bool(classify_provider_key_failure(returncode, stdout, stderr)["status"])


def normalize_config_string(value: Any) -> str:
    """Normalize security-sensitive strings before applying prefix policies."""
    text = unicodedata.normalize("NFKC", str(value or ""))
    for _ in range(4):
        text = text.strip()
        if len(text) < 2 or text[0] not in {"'", '"'} or text[-1] != text[0]:
            break
        text = text[1:-1]
    return text.strip()


def is_executable_config_value(value: Any) -> bool:
    return normalize_config_string(value).startswith("!")


def resolve_api_key_value(
    api_key: str,
    provider: str = "",
    *,
    allow_command: bool = False,
) -> str:
    """Resolve a secure reference, environment reference, or literal credential.

    Command credentials are permanently disabled. ``allow_command`` remains only
    as a source-compatible argument for callers from older integrations.
    """
    if not api_key:
        return ""
    key = normalize_config_string(api_key)
    if not key:
        return ""
    if key.startswith("__DPAPI__:") or provider:
        try:
            from . import secrets as secretstore

            secured = secretstore.resolve_provider_api_key(key, provider)
            if secured and secured != key:
                key = secured
            elif key.startswith("__DPAPI__:"):
                key = secretstore.resolve_provider_api_key(key, provider)
        except Exception:
            pass
    if is_executable_config_value(key):
        return ""
    try:
        from . import secrets as secretstore

        env_name = secretstore.referenced_env_name(key)
    except Exception:
        env_name = ""
    if env_name:
        # A syntactically valid environment reference is never a literal key.
        # Returning empty lets callers surface an actionable missing-key error.
        return os.environ.get(env_name, "").strip()
    return key


def provider_runtime_credential(provider: str | None) -> dict[str, Any]:
    """Resolve one custom provider credential for a Pi child process.

    The real key stays out of models.json and command-line arguments. Built-in
    providers are left to Pi's normal auth and environment resolution.
    """
    from . import core

    provider = (provider or "").strip()
    if not provider:
        return {"env": {}, "key_id": ""}
    entry = core.get_provider_config(provider)
    if not entry:
        return {"env": {}, "key_id": ""}

    from . import secrets as secretstore

    raw = normalize_config_string(entry.get("apiKey"))
    header_env = secretstore.provider_header_runtime_env(
        provider,
        entry.get("headers") if isinstance(entry.get("headers"), dict) else {},
    )
    if not raw:
        return {"env": header_env, "key_id": ""}
    if is_executable_config_value(raw):
        raise ProviderKeyError(
            f"Provider「{provider}」使用了已禁用的 !command 凭据。"
            "请改为环境变量引用或在 Provider 编辑页写入安全密钥库。"
        )

    env_name = secretstore.referenced_env_name(raw)
    if not env_name:
        # A legacy/plaintext configuration may not have reached the migration
        # path yet. Pi must see the new reference in models.json; injecting an
        # environment variable alone would leave Pi sending the old marker.
        reference = secretstore.store_provider_api_key(provider, raw)
        env_name = secretstore.referenced_env_name(reference)
        if env_name:
            changed_concurrently = False

            def persist_reference(config: Any) -> dict[str, Any]:
                nonlocal changed_concurrently
                if not isinstance(config, dict):
                    raise ValueError("models.json 顶层必须是对象")
                providers = config.get("providers")
                if not isinstance(providers, dict):
                    raise ValueError("models.json.providers 必须是对象")
                current = providers.get(provider)
                if not isinstance(current, dict):
                    raise ValueError(f"Provider「{provider}」已不存在")
                current_raw = str(current.get("apiKey") or "").strip()
                if current_raw == raw:
                    updated = dict(current)
                    updated["apiKey"] = reference
                    providers = dict(providers)
                    providers[provider] = updated
                    config = dict(config)
                    config["providers"] = providers
                elif current_raw != reference:
                    changed_concurrently = True
                return config

            try:
                storage.update_json(
                    core.models_path(), {"providers": {}}, persist_reference
                )
                core._invalidate_config_cache(core.models_path())
            except Exception as exc:
                raise ProviderKeyError(
                    f"Provider「{provider}」的旧 API Key 配置无法迁移到安全引用：{exc}。"
                    "请确认 models.json 可写，然后在 Provider 编辑页重新保存。"
                ) from exc
            if changed_concurrently:
                raise ProviderKeyError(
                    f"Provider「{provider}」在启动时被其他进程修改。请重试启动。"
                )

    if not env_name:
        return {"env": header_env, "key_id": ""}
    if env_name == secretstore.provider_env_name(provider):
        credential = secretstore.get_active_provider_credential(provider)
        if credential:
            return {
                # API-key entry first so callers that take the first env value
                # (historically next(iter(env.values()))) resolve the API key,
                # not a sensitive header secret. header_env may be empty.
                "env": {env_name: credential["value"], **header_env},
                "key": credential["value"],
                "key_id": credential["key_id"],
            }
        if secretstore.list_provider_keys(provider):
            raise ProviderKeyError(
                f"Provider「{provider}」的 API Key 已全部暂时失效。"
                "请在 Provider 的“管理 API Keys”中恢复或添加可用 Key。"
            )
        value = os.environ.get(env_name, "")
    else:
        value = os.environ.get(env_name, "")
    if not value:
        raise ProviderKeyError(
            f"Provider「{provider}」引用的环境变量 {env_name} 未设置或安全密钥已丢失。"
            "请在 Provider 编辑页重新填写 API Key 后保存。"
        )
    return {"env": {env_name: value, **header_env}, "key": value, "key_id": ""}


def provider_runtime_env(provider: str | None) -> dict[str, str]:
    """Compatibility wrapper returning only child-process environment values."""
    return dict(provider_runtime_credential(provider)["env"])


def all_provider_runtime_env(*, strict: bool = False) -> dict[str, str]:
    """Resolve credentials needed while Pi enumerates all custom models."""
    from . import core
    from . import secrets as secretstore

    cfg = core.load_models_config()
    providers = cfg.get("providers") or {}
    result: dict[str, str] = {}
    # 预热进程内 vault 缓存，避免每个 provider 的 get_secret 重复解密。
    secretstore.load_vault()
    for provider in providers if isinstance(providers, dict) else {}:
        try:
            result.update(provider_runtime_env(str(provider)))
        except ProviderKeyError:
            if strict:
                raise
    return result
