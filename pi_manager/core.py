"""
Pi Manager - Cross-platform GUI for managing and launching Pi Coding Agent.
All agent capability comes from the official `pi` CLI; this app manages
providers/models/settings and launches full Pi sessions.
"""
from __future__ import annotations

import copy
import base64
import heapq
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import secrets as secretstore
from . import storage
# HTTP 工具函数已抽到 core_http，此处重新导出以保持 core.xxx 调用兼容。
from .core_http import (
    _friendly_fetch_error,
    _ssl_context,
    normalize_openai_base_url,
    redact_endpoint_url,
    redact_secret_values,
)
# 视觉识图管道已抽到 core_vision，此处重新导出以保持 core.xxx 调用兼容。
from .core_vision import (
    ZHIPU_API_KEY_SECRET,
    ZHIPU_BASE_URL,
    ZHIPU_VISION_MODELS,
    build_vision_prompt,
    describe_image,
    ensure_zhipu_provider,
    install_vision_skill,
    load_image_for_describe,
    set_vision_model_choice,
    set_zhipu_api_key,
    test_vision,
    vision_model_choice,
    zhipu_api_key,
)
# 进程管理工具已抽到 core_process，此处重新导出以保持 core.xxx 调用兼容。
from .core_process import (
    _check_request_scheme,
    _is_private_host,
    _terminate_process_tree,
    escape_cmd_shim_args,
    find_pi_command,
    list_terminal_options,
    pi_base_cmd,
    proxy_reachable,
    run_pi,
    sanitize_proxy_env,
    validate_proxy_url,
)
# 远程模型获取与 HTTP 连通性测试已抽到 core_remote，此处重新导出。
from .core_remote import (
    _extract_reply_preview,
    _http_json_request,
    _resolve_provider_runtime_key,
    fetch_remote_models,
    format_test_summary,
    test_model,
    test_model_http,
    test_model_via_pi,
)
# 会话管理已抽到 core_sessions，此处重新导出。
from .core_sessions import (
    _decode_session_folder_slug,
    _parse_session_meta,
    list_sessions,
    open_in_explorer,
    open_path,
    project_name_from_path,
)

logger = logging.getLogger(__name__)


# ==== 基础工具：路径定位 / JSON 读写 / 敏感数据脱敏 ====


def user_home() -> Path:
    return Path(os.path.expanduser("~"))


def pi_agent_dir() -> Path:
    return secretstore.config_dir()


def models_path() -> Path:
    return pi_agent_dir() / "models.json"


def settings_path() -> Path:
    return pi_agent_dir() / "settings.json"


def auth_path() -> Path:
    return pi_agent_dir() / "auth.json"


def manager_config_path() -> Path:
    return pi_agent_dir() / "pi-manager.json"


def sessions_dir() -> Path:
    return pi_agent_dir() / "sessions"


def ensure_agent_dir() -> None:
    pi_agent_dir().mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    return storage.load_json(path, default)


def save_json(path: Path, data: Any, *, private: bool = False) -> None:
    ensure_agent_dir()
    storage.save_json(path, data, private=private)
    _invalidate_config_cache(path)


_ENV_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,}$")


def mask_secret(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    s = str(value)
    if s.startswith(("!", "$")) or (
        _ENV_NAME_RE.match(s) and not s.startswith(("sk", "tp-"))
    ):
        # env var name or shell command
        return s
    if len(s) <= keep * 2:
        return "*" * len(s)
    return s[:keep] + "*" * max(4, len(s) - keep * 2) + s[-keep:]


def redact_sensitive_config(value: Any, field_name: str = "") -> Any:
    """Return a display-safe deep copy of provider configuration."""
    sensitive = any(
        marker in field_name.lower().replace("_", "-")
        for marker in ("apikey", "api-key", "authorization", "token", "secret", "cookie")
    )
    if sensitive and isinstance(value, (str, int, float)):
        return mask_secret(str(value))
    if isinstance(value, dict):
        return {
            str(key): redact_sensitive_config(item, str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_config(item, field_name) for item in value]
    return value


# ==== 模型列表与 Pi 版本 ====


def get_pi_version() -> str:
    """Return Pi's version only when the CLI exits successfully.

    Runtime failures can mention a Node.js version in stderr. Those failures
    must never be parsed as Pi's installed version.
    """
    try:
        process = run_pi(["-v"], timeout=20)
        output = (process.stdout or process.stderr or "").strip()
        first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
        if process.returncode != 0:
            detail = first_line or "\u672a\u8fd4\u56de\u9519\u8bef\u8be6\u60c5"
            return f"error: Pi \u542f\u52a8\u5931\u8d25\uff08\u9000\u51fa\u7801 {process.returncode}\uff09\uff1a{detail}"
        return first_line or "unknown"
    except Exception as exc:
        return f"error: {exc}"


@dataclass
class ModelInfo:
    provider: str
    model: str
    context: str = ""
    max_out: str = ""
    thinking: str = ""
    images: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    def display(self) -> str:
        extra = []
        if self.context:
            extra.append(f"ctx {self.context}")
        if self.thinking and self.thinking.lower() in {"yes", "true", "y"}:
            extra.append("thinking")
        if self.images and self.images.lower() in {"yes", "true", "y"}:
            extra.append("images")
        suffix = f"  ({', '.join(extra)})" if extra else ""
        return f"{self.key}{suffix}"


def list_models(search: str | None = None) -> list[ModelInfo]:
    args = ["--list-models"]
    if search:
        args.append(search)
    try:
        p = run_pi(args, timeout=45, env=all_provider_runtime_env(strict=False))
    except Exception:
        return []
    text = (p.stdout or "") + "\n" + (p.stderr or "")
    models: list[ModelInfo] = []
    # lines like: provider  model  context  max-out  thinking  images
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("provider"):
            continue
        # collapse multiple spaces
        parts = re.split(r"\s{2,}|\t+", line)
        if len(parts) < 2:
            parts = line.split()
        if len(parts) < 2:
            continue
        provider, model = parts[0], parts[1]
        if provider in {"provider", "─", "-", "="}:
            continue
        # Provider names may contain spaces or non-ASCII characters (e.g.
        # "opencode go", "中转站"), so only reject obvious junk: a trailing
        # colon identifies warning/error lines ("Warning: ...").
        if not provider or not model or provider.endswith(":"):
            continue
        # Real table rows carry a numeric-ish capability column right after
        # the model id (e.g. "128K", "1M", "32.8K"); free-text lines such as
        # "No models matching ..." never do. When the row is too short to
        # check, keep it.
        if len(parts) >= 3 and not re.match(r"^[0-9.,]+[KM]?$", parts[2]):
            continue
        models.append(
            ModelInfo(
                provider=provider,
                model=model,
                context=parts[2] if len(parts) > 2 else "",
                max_out=parts[3] if len(parts) > 3 else "",
                thinking=parts[4] if len(parts) > 4 else "",
                images=parts[5] if len(parts) > 5 else "",
            )
        )
    # de-dupe
    seen: set[str] = set()
    uniq: list[ModelInfo] = []
    for m in models:
        if m.key in seen:
            continue
        seen.add(m.key)
        uniq.append(m)
    return uniq


_CONFIG_CACHE: dict[str, tuple[int, int, Any, float]] = {}
_CONFIG_CACHE_LOCK = threading.Lock()
# ==== 配置读写：settings / models / auth / manager（带进程内缓存） ====

_CONFIG_CACHE_TTL = 5.0  # seconds


def _invalidate_config_cache(path: Path | None = None) -> None:
    """Drop cached config entries after an in-process write."""
    with _CONFIG_CACHE_LOCK:
        if path is None:
            _CONFIG_CACHE.clear()
        else:
            _CONFIG_CACHE.pop(str(path), None)


def _load_json_cached(path: Path, default: Any) -> Any:
    """load_json with an (mtime_ns, size)-keyed cache for hot-path configs.

    A quick-ask reads pi-manager.json many times per prompt; one os.stat is
    far cheaper than the full file-lock + parse round trip. Writers (this
    process, the pi CLI, the extension's broker) all replace the file, so a
    changed signature naturally invalidates the entry.

    A monotonic TTL guards against file systems whose mtime granularity is
    too coarse to detect a rapid in-place rewrite by another process.
    """
    key = str(path)
    try:
        stat_before = os.stat(path)
        signature = (stat_before.st_mtime_ns, stat_before.st_size)
    except OSError:
        signature = None
    if signature is not None:
        with _CONFIG_CACHE_LOCK:
            cached = _CONFIG_CACHE.get(key)
        if (
            cached is not None
            and (cached[0], cached[1]) == signature
            and (time.monotonic() - cached[3]) < _CONFIG_CACHE_TTL
        ):
            return copy.deepcopy(cached[2])
    data = load_json(path, default)
    # Only cache when the file did not change while we were reading it.
    try:
        stat_after = os.stat(path)
        after = (stat_after.st_mtime_ns, stat_after.st_size)
    except OSError:
        after = None
    if after is not None and after == signature:
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE[key] = (after[0], after[1], copy.deepcopy(data), time.monotonic())
    return data


def load_settings() -> dict[str, Any]:
    return _load_json_cached(settings_path(), {})


def save_settings(data: dict[str, Any]) -> None:
    save_json(settings_path(), data)


DEFAULT_OPENAI_COMPAT_USER_AGENT = "PiManager/1.0 (+PiCLI)"
_OPENAI_COMPAT_APIS = frozenset(
    {"openai", "openai-completions", "openai-responses"}
)


def _openai_compat_headers(
    api: str, headers: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Add a WAF-friendly UA without overriding a non-empty custom value."""
    result = dict(headers or {})
    if str(api or "").strip().lower() not in _OPENAI_COMPAT_APIS:
        return result
    user_agent_key = next(
        (key for key in result if str(key).strip().lower() == "user-agent"),
        None,
    )
    if user_agent_key is None:
        result["User-Agent"] = DEFAULT_OPENAI_COMPAT_USER_AGENT
    elif not str(result.get(user_agent_key) or "").strip():
        result[user_agent_key] = DEFAULT_OPENAI_COMPAT_USER_AGENT
    return result


def _restore_latest_config_backup(target_path: Path) -> dict[str, Any] | None:
    """Return the newest parseable ``<name>.bak.*`` backup for *target_path*.

    Used as a last resort when the live config file is corrupt. Backups are
    tried newest-first; the first one that parses to a dict wins. Returns
    ``None`` if no usable backup exists.
    """
    name = target_path.name
    root = target_path.parent
    try:
        candidates = [
            p
            for p in root.glob(f"{name}.bak.*")
            if p.is_file()
        ]
    except OSError:
        return None
    # Newest by mtime first.
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for bak in candidates:
        try:
            data = load_json(bak, None)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None


def load_models_config() -> dict[str, Any]:
    try:
        cfg = _load_json_cached(models_path(), {"providers": {}})
    except storage.CorruptJsonError as exc:
        # models.json is corrupt/unreadable. Try to restore the most recent
        # backup before giving up; otherwise fall back to an empty config so
        # the UI keeps working instead of bubbling the exception up.
        logger.warning("models.json 损坏无法读取，尝试恢复备份: %s", exc)
        restored = _restore_latest_config_backup(models_path())
        if restored is not None:
            cfg = restored
        else:
            logger.warning("无可用备份，使用空配置兜底")
            cfg = {"providers": {}, "models": []}
    if not isinstance(cfg, dict):
        cfg = {"providers": {}}
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        cfg["providers"] = {}
        return cfg

    # Pi understands environment references but not Pi Manager's legacy
    # __DPAPI__ marker. Migrate legacy markers and plaintext keys on first read.
    try:
        from . import secrets as secretstore

        needs_migration = any(
            isinstance(entry, dict)
            and bool(str(entry.get("apiKey") or ""))
            and not str(entry.get("apiKey") or "").startswith("!")
            and (
                str(entry.get("apiKey") or "").startswith("__DPAPI__:")
                or not secretstore.referenced_env_name(str(entry.get("apiKey") or ""))
            )
            for entry in providers.values()
        )
        if needs_migration:
            migrated = secretstore.migrate_plaintext_keys(providers)
            if migrated != providers:
                migrated_cfg = dict(cfg)
                migrated_cfg["providers"] = migrated
                save_models_config(migrated_cfg)
                cfg = migrated_cfg
    except Exception as exc:
        # Keep configuration readable even if the platform keyring is broken,
        # but leave a trace: a failed migration means plaintext keys may still
        # sit in models.json and must not disappear silently.
        logging.getLogger(__name__).warning(
            "models.json 密钥迁移失败，明文引用可能仍保留在配置中: %s", exc
        )

    # OpenAI's Node SDK UA may be blocked by some compatible-provider WAFs.
    # Persist the safe default so upgraded, existing providers behave like new ones.
    providers = cfg.get("providers", {})
    if isinstance(providers, dict):
        updated_providers = dict(providers)
        headers_changed = False
        for name, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            current_headers = entry.get("headers")
            if current_headers is not None and not isinstance(current_headers, dict):
                continue
            effective_headers = _openai_compat_headers(
                str(entry.get("api") or "openai-completions"),
                current_headers,
            )
            if effective_headers == (current_headers or {}):
                continue
            updated_entry = dict(entry)
            updated_entry["headers"] = effective_headers
            updated_providers[name] = updated_entry
            headers_changed = True
        if headers_changed:
            cfg = dict(cfg)
            cfg["providers"] = updated_providers
            try:
                save_models_config(cfg)
            except Exception as exc:
                logger.warning(
                    "保存默认 User-Agent 头失败，models.json 与内存配置可能不一致: %s", exc
                )

    # Migrate reasoning models missing a thinkingLevelMap: without it, Pi
    # silently clamps "max" down to "high" (and drops xhigh/max from the
    # supported levels list). Fill in the default map for existing models
    # that were saved before this migration existed.
    providers = cfg.get("providers", {})
    if isinstance(providers, dict):
        thinking_changed = False
        updated_providers = dict(providers)
        for name, entry in providers.items():
            if not isinstance(entry, dict):
                continue
            models = entry.get("models")
            if not isinstance(models, list):
                continue
            new_models = []
            any_changed = False
            for m in models:
                if not isinstance(m, dict):
                    new_models.append(m)
                    continue
                migrated = ensure_thinking_level_map(m)
                if migrated is not m:
                    any_changed = True
                new_models.append(migrated)
            if any_changed:
                updated_entry = dict(entry)
                updated_entry["models"] = new_models
                updated_providers[name] = updated_entry
                thinking_changed = True
        if thinking_changed:
            cfg = dict(cfg)
            cfg["providers"] = updated_providers
            try:
                save_models_config(cfg)
            except Exception as exc:
                logger.warning(
                    "保存 thinkingLevelMap 迁移失败: %s", exc
                )

    return cfg


def save_models_config(data: dict[str, Any]) -> None:
    save_json(models_path(), data)


def load_auth() -> dict[str, Any]:
    return load_json(auth_path(), {})


def auth_summary() -> list[dict[str, str]]:
    auth = load_auth()
    rows = []
    for name, val in auth.items():
        if not isinstance(val, dict):
            continue
        t = val.get("type", "unknown")
        if t == "oauth" or "access" in val or "refresh" in val:
            status = "OAuth 已登录"
        elif t == "api_key" or "key" in val:
            key = val.get("key", "")
            status = f"API Key ({mask_secret(str(key))})"
        else:
            status = str(t)
        rows.append({"provider": name, "status": status})
    return rows


def delete_provider_auth(provider: str) -> dict[str, Any] | None:
    """Remove one provider's Pi credentials from auth.json (Pi-only logout).

    Other local tools (OpenAI CLI, Claude Code, Gemini CLI, …) keep their own
    credential stores and are never touched by this operation.
    """
    provider = (provider or "").strip()
    if not provider:
        return None
    removed: dict[str, Any] | None = None

    def remove(current: Any) -> dict[str, Any]:
        nonlocal removed
        if not isinstance(current, dict):
            raise ValueError("auth.json 顶层必须是对象")
        entry = current.get(provider)
        if not isinstance(entry, dict):
            raise ValueError(f"Provider「{provider}」没有已保存的认证")
        removed = entry
        result = dict(current)
        del result[provider]
        return result

    try:
        storage.update_json(auth_path(), {}, remove)
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    return removed


def load_manager_config() -> dict[str, Any]:
    data = _load_json_cached(
        manager_config_path(),
        {
            "favorites": [],
            "last_workdir": str(user_home()),
            "terminal": "auto",
            "quick_models": [],
            "drop_auto_launch": True,
            "language": "zh-CN",
            "setup_done": False,
            "auto_check_update": True,
            "last_update_check": "",
            "ui_mode": "night",
            "ui_accent": "blue",
            "proxy_enabled": False,
            "proxy_url": "",
            "test_concurrency": 3,
            "secure_keys": True,
            "minimize_to_tray": True,
            "start_minimized": False,
            "health_interval_min": 0,
            "update_manifest_url": "",
            "last_manager_update_check": "",
            # 快速提问：模型连续失败后自动切换下一个收藏模型
            "failover_enabled": True,
            "failover_fail_threshold": 3,
            "failover_fail_counts": {},
            "failover_silent": True,
            # 快速提问：常驻 pi --mode rpc 会话（多轮上下文 + set_model 会话内热切）
            "chat_persistent_session": True,
            "chat_session_idle_min": 10,
        },
    )
    # merge missing keys for upgrades
    defaults = {
        "proxy_enabled": False,
        "proxy_url": "",
        "test_concurrency": 3,
        "secure_keys": True,
        "minimize_to_tray": True,
        "start_minimized": False,
        "health_interval_min": 0,
        "update_manifest_url": "",
        "last_manager_update_check": "",
        "drop_auto_launch": True,
        "language": "zh-CN",
        "ui_mode": "night",
        "ui_accent": "blue",
        "auto_check_update": True,
        "failover_enabled": True,
        "failover_fail_threshold": 3,
        "failover_fail_counts": {},
        "failover_silent": True,
        "chat_persistent_session": True,
        "chat_session_idle_min": 10,
    }
    if not isinstance(data, dict):
        data = {}
    for k, v in defaults.items():
        data.setdefault(k, v)
    return data


def save_manager_config(data: dict[str, Any]) -> None:
    # pi-manager.json may hold a proxy URL with embedded credentials.
    save_json(manager_config_path(), data, private=True)


# ==== 默认模型 / 收藏 / 自定义 provider / 模型管理 ====


def normalize_model_pair(
    provider: str | None,
    model: str | None,
    *,
    allow_empty: bool = True,
) -> tuple[str, str] | None:
    """Normalize an atomic Provider/Model pair without mixing partial defaults."""
    p = str(provider or "").strip()
    m = str(model or "").strip()
    if not p and not m and allow_empty:
        return None
    if not p or not m:
        raise ValueError("Provider 和 Model 必须成对指定，不能跨模型混用")
    return p, m


DEFAULT_THINKING_LEVEL = "medium"


def set_default_model(provider: str, model: str, thinking: str | None = None) -> dict[str, Any]:
    pair = normalize_model_pair(provider, model, allow_empty=False)
    assert pair is not None
    provider, model = pair
    settings = load_settings()
    settings["defaultProvider"] = provider
    settings["defaultModel"] = model
    if thinking:
        settings["defaultThinkingLevel"] = thinking
    save_settings(settings)
    return settings


def get_default_model() -> tuple[str, str, str]:
    s = load_settings()
    return (
        str(s.get("defaultProvider") or ""),
        str(s.get("defaultModel") or ""),
        str(s.get("defaultThinkingLevel") or DEFAULT_THINKING_LEVEL),
    )


def set_enabled_models(patterns: list[str]) -> dict[str, Any]:
    settings = load_settings()
    if patterns:
        settings["enabledModels"] = patterns
    elif "enabledModels" in settings:
        del settings["enabledModels"]
    save_settings(settings)
    return settings


def upsert_custom_provider(
    name: str,
    *,
    base_url: str,
    api: str = "openai-completions",
    api_key: str | None = None,
    models: list[dict[str, Any]] | None = None,
    compat: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    cfg = load_models_config()
    providers = cfg.setdefault("providers", {})
    existing = providers.get(name, {}) if isinstance(providers.get(name), dict) else {}
    from . import secrets as secretstore

    if api_key is None:
        raw_key = str(existing.get("apiKey") or "")
    else:
        raw_key = secretstore.store_provider_api_key(name, str(api_key).strip())
    saved_models = [
        ensure_thinking_level_map(m)
        for m in (models if models is not None else existing.get("models", []))
    ]
    entry: dict[str, Any] = {
        "baseUrl": base_url,
        "api": api,
        "apiKey": raw_key,
        "models": saved_models,
    }
    if compat is not None:
        entry["compat"] = compat
    elif "compat" in existing:
        entry["compat"] = existing["compat"]
    header_source = headers if headers is not None else existing.get("headers")
    if isinstance(header_source, dict) or header_source is None:
        effective_headers = _openai_compat_headers(api, header_source)
        if effective_headers or headers is not None or "headers" in existing:
            entry["headers"] = secretstore.store_provider_headers(
                name, effective_headers
            )
    elif "headers" in existing:
        entry["headers"] = existing["headers"]
    providers[name] = entry
    save_models_config(cfg)
    # 保存 provider 后用户即将使用 Pi：确保所有内置插件（含 vision skill）
    # 已落盘，让图片处理等开箱即用。委托给 builtin_plugins 统一机制。
    try:
        from . import builtin_plugins
        builtin_plugins.install_all_builtins()
    except Exception as exc:
        logger.warning("安装内置插件失败: %s", exc)
    return cfg


def parse_favorite_key(key: str) -> tuple[str, str] | None:
    key = (key or "").strip()
    if "/" not in key:
        return None
    provider, model = key.split("/", 1)
    provider, model = provider.strip(), model.strip()
    if not provider or not model:
        return None
    return provider, model


def purge_favorites(
    *,
    provider: str | None = None,
    model: str | None = None,
    redefault: bool = True,
) -> dict[str, Any]:
    """从收藏中移除匹配项；若默认模型被移除，则自动切换到下一个收藏。

    - 仅 provider：删除该 Provider 下全部收藏
    - provider + model：只删除该模型收藏
    - redefault=True：默认模型落在被删集合时，切到剩余收藏第一项；无剩余则清空默认
    """
    provider = (provider or "").strip()
    model = (model or "").strip()
    mgr = load_manager_config()
    favs = list(mgr.get("favorites") or [])
    kept: list[str] = []
    removed: list[str] = []
    for key in favs:
        parsed = parse_favorite_key(str(key))
        if not parsed:
            kept.append(str(key))
            continue
        p, m = parsed
        drop = False
        if provider and model:
            drop = p == provider and m == model
        elif provider:
            drop = p == provider
        if drop:
            removed.append(str(key))
        else:
            kept.append(str(key))

    changed = removed or favs != kept
    if changed:
        mgr["favorites"] = kept
        save_manager_config(mgr)

    result: dict[str, Any] = {
        "removed_favorites": removed,
        "favorites": kept,
        "default_changed": False,
        "default_provider": "",
        "default_model": "",
    }

    if not redefault:
        return result

    cur_p, cur_m, thinking = get_default_model()
    need_redefault = False
    if provider and model:
        need_redefault = cur_p == provider and cur_m == model
    elif provider:
        need_redefault = cur_p == provider
    # 默认模型对应收藏已被删，或默认本身指向已删 provider
    if not need_redefault and removed:
        cur_key = f"{cur_p}/{cur_m}" if cur_p and cur_m else ""
        if cur_key and cur_key in removed:
            need_redefault = True

    if need_redefault:
        next_p, next_m = "", ""
        for key in kept:
            parsed = parse_favorite_key(str(key))
            if parsed:
                next_p, next_m = parsed
                break
        if next_p and next_m:
            set_default_model(next_p, next_m, thinking or None)
            result["default_changed"] = True
            result["default_provider"] = next_p
            result["default_model"] = next_m
        else:
            # 无可用收藏：清空默认，避免指向已删除 provider
            settings = load_settings()
            settings["defaultProvider"] = ""
            settings["defaultModel"] = ""
            save_settings(settings)
            result["default_changed"] = True
            result["default_provider"] = ""
            result["default_model"] = ""
    else:
        result["default_provider"] = cur_p
        result["default_model"] = cur_m

    return result


def purge_enabled_models(
    *,
    provider: str | None = None,
    model: str | None = None,
) -> list[str]:
    """从 settings.enabledModels 中移除指向已删除 Provider/模型 的残留模式。

    - 仅 provider：移除该 Provider 下全部模式（如 ``name/model``）
    - provider + model：只移除精确匹配 ``provider/model`` 的模式
    - 纯模型名（不含 ``/``）不参与匹配，原样保留
    """
    provider = (provider or "").strip()
    model = (model or "").strip()
    settings = load_settings()
    patterns = settings.get("enabledModels")
    if not isinstance(patterns, list):
        return []
    kept: list[str] = []
    removed: list[str] = []
    for pattern in patterns:
        key = str(pattern)
        parsed = parse_favorite_key(key)
        if not parsed:
            kept.append(key)
            continue
        p, m = parsed
        drop = False
        if provider and model:
            drop = p == provider and m == model
        elif provider:
            drop = p == provider
        if drop:
            removed.append(key)
        else:
            kept.append(key)
    if removed:
        if kept:
            settings["enabledModels"] = kept
        else:
            settings.pop("enabledModels", None)
        save_settings(settings)
    return removed


def list_stale_enabled_models(builtin_providers: set[str] | None = None) -> list[str]:
    """返回 settings.enabledModels 中引用已不存在 Provider 的残留模式。

    ``builtin_providers`` 传入 Pi 内置 Provider 名集合时可避免误报；
    不传时仅与 models.json 中的自定义 Provider 比对。
    """
    settings = load_settings()
    patterns = settings.get("enabledModels")
    if not isinstance(patterns, list):
        return []
    cfg = load_models_config()
    custom = set(cfg.get("providers") or {})
    stale: list[str] = []
    for pattern in patterns:
        parsed = parse_favorite_key(str(pattern))
        if not parsed:
            continue
        p, _m = parsed
        if p in custom:
            continue
        if builtin_providers and p in builtin_providers:
            continue
        stale.append(str(pattern))
    return stale


def delete_custom_provider(name: str) -> dict[str, Any]:
    cfg = load_models_config()
    providers = cfg.get("providers", {})
    deleted_entry = providers.get(name) if isinstance(providers, dict) else None
    if name in providers:
        del providers[name]
        cfg["providers"] = providers
        save_models_config(cfg)
    try:
        from . import secrets as secretstore

        if isinstance(deleted_entry, dict) and isinstance(deleted_entry.get("headers"), dict):
            secretstore.delete_provider_header_secrets(name, deleted_entry["headers"])
        secretstore.delete_provider_api_keys(name)
    except Exception as exc:
        logger.warning("删除 provider「%s」的密钥/头清理失败: %s", name, exc)
    # 同步清理收藏，并在默认属于该 Provider 时切换到下一个收藏
    try:
        cfg["_purge"] = purge_favorites(provider=name, redefault=True)
    except Exception as exc:
        logger.warning("删除 provider「%s」后清理收藏失败: %s", name, exc)
        cfg["_purge"] = {"removed_favorites": [], "favorites": [], "default_changed": False}
    # 同步清理 settings.enabledModels 中的残留模式，避免 Pi 每次启动
    # 输出 "No models match pattern" 警告并污染测试结果
    try:
        cfg["_purged_enabled"] = purge_enabled_models(provider=name)
    except Exception as exc:
        logger.warning("删除 provider「%s」后清理 enabledModels 失败: %s", name, exc)
        cfg["_purged_enabled"] = []
    return cfg


# Default mapping from Pi thinking levels to OpenAI-style reasoning_effort
# values. Pi treats xhigh/max as unsupported when a reasoning model has no
# thinkingLevelMap, silently clamping max down to high — so fill it in.
DEFAULT_THINKING_LEVEL_MAP: dict[str, str] = {
    "off": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def ensure_thinking_level_map(model: dict[str, Any]) -> dict[str, Any]:
    """Fill a default thinkingLevelMap for reasoning models missing one.

    Without a thinkingLevelMap, Pi's getSupportedThinkingLevels() drops
    xhigh/max, and clampThinkingLevel() demotes max to high. Only touch
    models that support reasoning and have no explicit map, so user-provided
    custom mappings are preserved.
    """
    if not isinstance(model, dict):
        return model
    if not model.get("reasoning") or model.get("thinkingLevelMap"):
        return model
    result = dict(model)
    result["thinkingLevelMap"] = dict(DEFAULT_THINKING_LEVEL_MAP)
    return result


def add_model_to_provider(provider: str, model_id: str, **kwargs: Any) -> dict[str, Any]:
    cfg = load_models_config()
    providers = cfg.setdefault("providers", {})
    if provider not in providers:
        raise KeyError(f"provider not found: {provider}")
    models = providers[provider].setdefault("models", [])
    # replace if exists
    models = [m for m in models if m.get("id") != model_id]
    item = ensure_thinking_level_map({"id": model_id, **kwargs})
    if "cost" not in item:
        item["cost"] = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    models.append(item)
    providers[provider]["models"] = models
    save_models_config(cfg)
    return cfg


def remove_model_from_provider(provider: str, model_id: str) -> dict[str, Any]:
    cfg = load_models_config()
    providers = cfg.get("providers", {})
    if provider in providers:
        models = providers[provider].get("models", [])
        providers[provider]["models"] = [m for m in models if m.get("id") != model_id]
        save_models_config(cfg)
    try:
        cfg["_purge"] = purge_favorites(provider=provider, model=model_id, redefault=True)
    except Exception as exc:
        logger.warning("移除模型后清理收藏失败: %s", exc)
        cfg["_purge"] = {"removed_favorites": [], "favorites": [], "default_changed": False}
    try:
        cfg["_purged_enabled"] = purge_enabled_models(provider=provider, model=model_id)
    except Exception as exc:
        logger.warning("移除模型后清理 enabledModels 失败: %s", exc)
        cfg["_purged_enabled"] = []
    return cfg



def build_pi_launch_args(
    *,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    args: list[str] = []
    pair = normalize_model_pair(provider, model)
    if pair is not None:
        pair_provider, pair_model = pair
        args += ["--provider", pair_provider, "--model", pair_model]
    if thinking:
        args += ["--thinking", thinking]
    if extra:
        args += extra
    return args


def launch_pi_interactive(
    workdir: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    terminal: str = "auto",
    extra: list[str] | None = None,
) -> str:
    """Launch full interactive Pi in an external terminal (cross-platform)."""
    from . import platform_util as pu

    pi_args = build_pi_launch_args(
        provider=provider, model=model, thinking=thinking, extra=extra
    )
    pi_args = append_language_args(pi_args)
    pi_args = append_vision_args(pi_args)
    base = pi_base_cmd()
    # Mirror run_pi: when the pi launcher is a cmd.exe batch shim, cmd.exe
    # re-expands %VAR% in the command line (e.g. %TEMP% in the vision rule)
    # before the script runs. Escape percents so args stay literal.
    pi_args = escape_cmd_shim_args(pi_args, base)
    full_cmd_list = base + pi_args
    workdir = workdir or str(user_home())
    if provider:
        child_env = provider_runtime_env(provider)
    else:
        child_env = {}
    return pu.launch_in_terminal(
        full_cmd_list,
        workdir,
        terminal=terminal,
        env=child_env,
    )


def run_pi_print(
    prompt: str,
    *,
    workdir: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    timeout: float = 300,
) -> tuple[int, str, str]:
    args = build_pi_launch_args(provider=provider, model=model, thinking=thinking)
    args = append_language_args(args)
    args += ["-p", "--no-session", prompt]
    # project trust for non-interactive
    args += ["--approve"]
    attempted_key_ids: set[str] = set()
    while True:
        credential = provider_runtime_credential(provider)
        p = run_pi(
            args,
            cwd=workdir,
            timeout=timeout,
            env=credential["env"],
        )
        stdout = p.stdout or ""
        stderr = p.stderr or ""
        key_id = str(credential.get("key_id") or "")
        if p.returncode == 0 or not key_id or not is_provider_key_error(
            p.returncode, stdout, stderr
        ):
            return p.returncode, stdout, stderr
        if key_id in attempted_key_ids:
            return p.returncode, stdout, stderr
        attempted_key_ids.add(key_id)

        from . import secrets as secretstore

        reason = provider_key_failure_reason(p.returncode, stdout, stderr)
        changed = secretstore.mark_provider_key_failed(
            str(provider or ""), key_id, reason
        )
        next_credential = secretstore.get_active_provider_credential(str(provider or ""))
        if (
            not changed
            or not next_credential
            or next_credential["key_id"] in attempted_key_ids
        ):
            return p.returncode, stdout, stderr



def default_model_template(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 128000,
        "maxTokens": 32768,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }



# Language / install / theme helpers
# ---------------------------------------------------------------------------

LANG_ZH_PROMPT = """## 语言偏好（必须遵守）
- 请尽可能使用简体中文与用户交流、解释、写说明与文档。
- 仅当中文无法准确表达时才保留英文（如 API 名、协议字段、库名、错误码、固定术语），并尽量附简短中文说明。
- 代码标识符、命令、路径、配置键名保持原样，不要翻译。
- 回答优先中文，结构清晰，避免无必要的英文整段输出。
"""

LANG_EN_PROMPT = """## Language preference
- Prefer clear English for explanations and documentation.
- Keep code identifiers, commands, paths, and config keys unchanged.
"""

LANG_PROMPTS = {
    "zh-CN": LANG_ZH_PROMPT,
    "en": LANG_EN_PROMPT,
}


def get_language() -> str:
    cfg = load_manager_config()
    lang = str(cfg.get("language") or "zh-CN")
    return lang if lang in LANG_PROMPTS or lang == "auto" else "zh-CN"


def set_language(lang: str) -> None:
    cfg = load_manager_config()
    cfg["language"] = lang
    save_manager_config(cfg)
    apply_language_preference(lang)


def language_prompt_text(lang: str | None = None) -> str:
    lang = lang or get_language()
    if lang == "auto":
        return ""
    return LANG_PROMPTS.get(lang, LANG_ZH_PROMPT)


def agents_md_path() -> Path:
    return pi_agent_dir() / "AGENTS.md"


_LANG_BLOCK_RE = re.compile(
    r"<!-- PI-MANAGER-LANG-START -->.*?<!-- PI-MANAGER-LANG-END -->\n?",
    re.DOTALL,
)


def apply_language_preference(lang: str | None = None) -> Path:
    """Write global AGENTS.md language block so Pi sessions use the preference."""
    lang = lang or get_language()
    ensure_agent_dir()
    path = agents_md_path()
    # Tolerate non-UTF-8 bytes (e.g. a user edited AGENTS.md as GBK/ANSI on
    # Windows): replacing bad bytes lets the save proceed and rewrites the
    # file as clean UTF-8, instead of crashing the language preference action.
    existing = (
        path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    )
    existing = _LANG_BLOCK_RE.sub("", existing).rstrip() + "\n"
    body = language_prompt_text(lang).strip()
    if body:
        block = (
            "\n<!-- PI-MANAGER-LANG-START -->\n"
            f"{body}\n"
            "<!-- PI-MANAGER-LANG-END -->\n"
        )
        existing = existing.rstrip() + "\n" + block
    path.write_text(existing.lstrip() + ("\n" if not existing.endswith("\n") else ""), encoding="utf-8")
    return path


def append_language_args(args: list[str], lang: str | None = None) -> list[str]:
    """Add --append-system-prompt for launch-time language enforcement."""
    text = language_prompt_text(lang)
    if text.strip():
        args = list(args) + ["--append-system-prompt", text.strip()]
    return args


def _helper_command_text() -> str:
    """Shell-quoted helper command for prompts/skills (single source of truth)."""
    import shlex

    try:
        from .helper_registry import current_helper_command

        return " ".join(shlex.quote(str(part)) for part in current_helper_command())
    except Exception:
        if getattr(sys, "frozen", False):
            return shlex.quote(sys.executable)
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(main_path))}"


def _vision_rule_prompt() -> str:
    """System-prompt rule appended when launching Pi: images are always
    routed through the free Zhipu vision model first — never sent straight to
    a (possibly text-only) provider model."""
    command = _helper_command_text()
    return (
        "## 图片处理规则（必须遵守）\n"
        "- 用户发送图片、粘贴截图或要求查看图片时：绝不要尝试把图片直接发送给当前对话模型"
        "（纯文本模型会报 image_url 错误）。\n"
        "- 先获取图片文件路径：用户提供，或从系统临时目录（%TEMP%）查找最新的 "
        "pi-clipboard-*.png 文件。\n"
        f"- 运行识图命令：{command} --vision-describe \"<图片路径>\" \"<用户问题，可空>\"\n"
        "- 将命令输出的文字描述视为图片内容，结合用户问题回答。\n"
        "- 若提示未配置智谱 API Key，请告知用户在 Pi Manager「设置 → 识图模型」中配置。"
    )


def append_vision_args(args: list[str]) -> list[str]:
    """Add the image-routing system-prompt rule at launch time."""
    try:
        text = _vision_rule_prompt()
    except Exception:
        return list(args)
    return list(args) + ["--append-system-prompt", text]


def apply_theme(theme_name: str) -> dict[str, Any]:
    from .builtin_themes import ensure_builtin_themes

    ensure_builtin_themes()
    settings = load_settings()
    settings["theme"] = theme_name
    save_settings(settings)
    return settings


def get_theme() -> str:
    return str(load_settings().get("theme") or "dark")


def list_themes() -> list[tuple[str, str]]:
    from .builtin_themes import list_theme_choices

    return list_theme_choices()


PI_NPM_PACKAGE = "@earendil-works/pi-coding-agent"
PI_LATEST_TAG = "latest"
PI_LEGACY_NODE20_TAG = "legacy-node20"
PI_LATEST_MIN_NODE = (22, 19, 0)
PI_LEGACY_MIN_NODE = (20, 6, 0)


def _npm_command(*args: str) -> list[str]:
    """Resolve npm's Windows command shim without invoking a shell."""
    names = ("npm.cmd", "npm") if sys.platform == "win32" else ("npm",)
    executable = next((path for name in names if (path := shutil.which(name))), names[0])
    return [executable, *args]


def _node_command(*args: str) -> list[str]:
    names = ("node.exe", "node") if sys.platform == "win32" else ("node",)
    executable = next((path for name in names if (path := shutil.which(name))), names[0])
    return [executable, *args]


def _run_version_command(command: list[str], timeout: float = 20) -> str | None:
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        return None
    if process.returncode != 0:
        return None
    output = (process.stdout or process.stderr or "").strip()
    match = re.search(r"(?:^|\D)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", output)
    return match.group(1) if match else None


def get_node_version(timeout: float = 20) -> str | None:
    """Return the active Node.js semantic version without a leading v."""
    return _run_version_command(_node_command("--version"), timeout=timeout)


def get_npm_version(timeout: float = 20) -> str | None:
    """Return the active npm semantic version."""
    return _run_version_command(_npm_command("--version"), timeout=timeout)


def select_pi_install_channel(node_version: str | None = None) -> str | None:
    """Select the npm dist-tag compatible with the active Node.js runtime."""
    version = node_version if node_version is not None else get_node_version()
    if not version:
        return None
    parsed = parse_semver(version)
    if parsed >= PI_LATEST_MIN_NODE:
        return PI_LATEST_TAG
    if parsed >= PI_LEGACY_MIN_NODE:
        return PI_LEGACY_NODE20_TAG
    return None


def pi_package_spec(channel: str | None) -> str | None:
    return f"{PI_NPM_PACKAGE}@{channel}" if channel else None


def get_latest_pi_version(timeout: float = 20, tag: str | None = None) -> str | None:
    """Return the newest Pi version for an npm compatibility channel."""
    channel = tag or select_pi_install_channel() or PI_LATEST_TAG
    try:
        process = subprocess.run(
            _npm_command("view", f"{PI_NPM_PACKAGE}@{channel}", "version"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        return None
    if process.returncode != 0:
        return None
    output = (process.stdout or "").strip()
    match = re.search(r"(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", output)
    return match.group(1) if match else None


def get_installed_pi_version() -> str | None:
    value = get_pi_version()
    if not value or value.startswith("error:") or value == "unknown":
        return None
    match = re.search(r"(?:^|\D)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", value)
    return match.group(1) if match else None


def parse_semver(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(v or ""))
    values = tuple(int(x) for x in parts[:3]) if parts else (0,)
    return values + (0,) * (3 - len(values))


def get_pi_runtime_status() -> dict[str, Any]:
    """Inspect whether the Pi command exists and can actually start."""
    command = find_pi_command()
    if not command:
        return {
            "command": None,
            "installed": None,
            "raw_version": None,
            "missing": True,
            "runtime_broken": False,
            "ok": False,
            "error": "\u672a\u627e\u5230 Pi \u547d\u4ee4\u3002",
        }
    raw_version = get_pi_version()
    if raw_version.startswith("error:") or raw_version == "unknown":
        error = raw_version.removeprefix("error:").strip() or "Pi \u65e0\u6cd5\u542f\u52a8\u3002"
        return {
            "command": command,
            "installed": None,
            "raw_version": raw_version,
            "missing": False,
            "runtime_broken": True,
            "ok": False,
            "error": error,
        }
    match = re.search(r"(?:^|\D)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", raw_version)
    installed = match.group(1) if match else None
    if not installed:
        return {
            "command": command,
            "installed": None,
            "raw_version": raw_version,
            "missing": False,
            "runtime_broken": True,
            "ok": False,
            "error": f"\u65e0\u6cd5\u89e3\u6790 Pi \u7248\u672c\u8f93\u51fa\uff1a{raw_version}",
        }
    return {
        "command": command,
        "installed": installed,
        "raw_version": raw_version,
        "missing": False,
        "runtime_broken": False,
        "ok": True,
        "error": "",
    }


def needs_pi_install_or_update() -> dict[str, Any]:
    """Return actionable Pi runtime, registry, and compatibility status."""
    node_version = get_node_version()
    npm_version = get_npm_version()
    channel = select_pi_install_channel(node_version)
    package_spec = pi_package_spec(channel)
    runtime = get_pi_runtime_status()

    blocked_reason = ""
    if not node_version:
        blocked_reason = "\u672a\u68c0\u6d4b\u5230 Node.js\u3002\u8bf7\u5148\u5b89\u88c5 Node.js 20.6 \u6216\u66f4\u9ad8\u7248\u672c\u3002"
    elif parse_semver(node_version) < PI_LEGACY_MIN_NODE:
        blocked_reason = (
            f"\u5f53\u524d Node.js {node_version} \u8fc7\u4f4e\uff1bPi \u81f3\u5c11\u9700\u8981 Node.js 20.6\uff0c"
            "\u63a8\u8350\u5347\u7ea7\u5230 22.19 \u6216\u66f4\u9ad8\u7248\u672c\u3002"
        )
    elif not npm_version:
        blocked_reason = "\u672a\u68c0\u6d4b\u5230\u53ef\u7528\u7684 npm\u3002\u8bf7\u4fee\u590d Node.js/npm \u5b89\u88c5\u540e\u91cd\u8bd5\u3002"

    installable = bool(channel and npm_version and not blocked_reason)
    latest = get_latest_pi_version(tag=channel) if installable else None
    registry_ok = bool(latest)
    check_failed = bool(installable and not registry_ok)
    installed = runtime.get("installed")
    missing = bool(runtime.get("missing"))
    runtime_broken = bool(runtime.get("runtime_broken"))
    repair_required = runtime_broken
    outdated = bool(
        installed and latest and parse_semver(str(installed)) < parse_semver(str(latest))
    )

    result: dict[str, Any] = {
        "installed": installed,
        "latest": latest,
        "missing": missing,
        "outdated": outdated,
        "ok": False,
        "message": "",
        "registry_ok": registry_ok,
        "check_failed": check_failed,
        "runtime_broken": runtime_broken,
        "repair_required": repair_required,
        "installable": installable,
        "blocked": bool(blocked_reason),
        "node_version": node_version,
        "npm_version": npm_version,
        "channel": channel,
        "package_spec": package_spec,
        "error": "",
        "command": runtime.get("command"),
    }

    channel_label = "\u6700\u65b0\u7248\u901a\u9053" if channel == PI_LATEST_TAG else "Node 20 \u517c\u5bb9\u901a\u9053"
    channel_detail = f"{channel_label}\uff08{channel}\uff09" if channel else "\u65e0\u517c\u5bb9\u901a\u9053"
    if blocked_reason:
        runtime_detail = f" \u5f53\u524d Pi\uff1a{installed}\u3002" if installed else ""
        result["message"] = blocked_reason + runtime_detail
        result["error"] = blocked_reason
        return result
    if runtime_broken:
        detail = str(runtime.get("error") or "Pi \u65e0\u6cd5\u542f\u52a8")
        result["message"] = (
            f"\u68c0\u6d4b\u5230 Pi \u547d\u4ee4\uff0c\u4f46\u8fd0\u884c\u5931\u8d25\uff1a{detail}\n"
            f"\u53ef\u901a\u8fc7 {package_spec} \u6267\u884c\u4fee\u590d\u5b89\u88c5\u3002"
        )
        result["error"] = detail
        return result
    if check_failed:
        installed_detail = f"\u5f53\u524d\u5df2\u5b89\u88c5 {installed}\uff0c" if installed else ""
        result["message"] = (
            f"{installed_detail}\u4f46\u65e0\u6cd5\u4ece npm registry \u83b7\u53d6 {channel_detail} \u7684\u7248\u672c\u4fe1\u606f\u3002"
            "\u8bf7\u68c0\u67e5\u7f51\u7edc\u3001\u4ee3\u7406\u6216 npm registry \u914d\u7f6e\u540e\u91cd\u8bd5\u3002"
        )
        result["error"] = "npm registry \u7248\u672c\u67e5\u8be2\u5931\u8d25"
        return result
    if missing:
        result["message"] = (
            f"\u672a\u68c0\u6d4b\u5230 Pi\u3002\u5f53\u524d Node.js {node_version}\uff0c\u5c06\u5b89\u88c5 {channel_detail}"
            f"\uff08\u76ee\u6807 {latest}\uff09\u3002"
        )
        return result
    if outdated:
        result["message"] = (
            f"\u5df2\u5b89\u88c5 Pi {installed}\uff0c{channel_detail} \u6700\u65b0\u4e3a {latest}\uff0c\u5efa\u8bae\u5347\u7ea7\u3002"
        )
        return result

    result["ok"] = True
    result["message"] = (
        f"Pi \u5df2\u5c31\u7eea\uff08{installed}\uff0c{channel_detail} \u6700\u65b0 {latest}\uff1b"
        f"Node.js {node_version}\uff0cnpm {npm_version}\uff09"
    )
    return result


def install_or_update_pi(timeout: float = 300) -> tuple[int, str, str]:
    """Install the Node-compatible Pi channel and verify the resulting CLI."""
    node_version = get_node_version()
    npm_version = get_npm_version()
    channel = select_pi_install_channel(node_version)
    if not node_version:
        return 2, "", "\u672a\u68c0\u6d4b\u5230 Node.js\uff1b\u8bf7\u5148\u5b89\u88c5 Node.js 20.6 \u6216\u66f4\u9ad8\u7248\u672c\u3002"
    if parse_semver(node_version) < PI_LEGACY_MIN_NODE or not channel:
        return (
            2,
            "",
            f"\u5f53\u524d Node.js {node_version} \u8fc7\u4f4e\uff1b\u8bf7\u5347\u7ea7\u5230 20.6 \u6216\u66f4\u9ad8\u7248\u672c\uff08\u63a8\u8350 22.19+\uff09\u3002",
        )
    if not npm_version:
        return 2, "", "\u672a\u68c0\u6d4b\u5230\u53ef\u7528\u7684 npm\uff1b\u8bf7\u4fee\u590d Node.js/npm \u5b89\u88c5\u3002"

    package_spec = pi_package_spec(channel)
    target_version = get_latest_pi_version(timeout=min(timeout, 30), tag=channel)
    if not target_version:
        return (
            3,
            "",
            f"\u65e0\u6cd5\u4ece npm registry \u83b7\u53d6 {package_spec} \u7684\u7248\u672c\u4fe1\u606f\uff1b\u672a\u6267\u884c\u5b89\u88c5\u3002",
        )

    command = _npm_command("install", "-g", str(package_spec))
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as exc:
        return 1, "", str(exc)
    stdout = process.stdout or ""
    stderr = process.stderr or ""
    if process.returncode != 0:
        return process.returncode, stdout, stderr

    runtime = get_pi_runtime_status()
    installed = runtime.get("installed")
    if not runtime.get("ok") or not installed:
        detail = str(runtime.get("error") or "npm \u5b89\u88c5\u5b8c\u6210\uff0c\u4f46 Pi \u4ecd\u65e0\u6cd5\u542f\u52a8\u3002")
        return 4, stdout, (stderr + "\n" + detail).strip()
    if parse_semver(str(installed)) < parse_semver(target_version):
        detail = (
            f"npm \u5df2\u5b89\u88c5 {package_spec} {target_version}\uff0c\u4f46 PATH \u4e2d\u5b9e\u9645\u8fd0\u884c\u7684 Pi \u4ecd\u4e3a "
            f"{installed}\u3002\u8bf7\u68c0\u67e5\u65e7\u7684 pi \u547d\u4ee4\u6216 npm \u5168\u5c40 bin \u8def\u5f84\u3002"
        )
        return 5, stdout, (stderr + "\n" + detail).strip()

    verified = (
        f"\u5df2\u9a8c\u8bc1 Pi {installed}\uff08{channel} \u901a\u9053\uff0c\u76ee\u6807 {target_version}\uff1b"
        f"Node.js {node_version}\uff0cnpm {npm_version}\uff09"
    )
    return 0, (stdout.rstrip() + ("\n" if stdout.strip() else "") + verified + "\n"), stderr

def is_setup_done() -> bool:
    return bool(load_manager_config().get("setup_done"))


def mark_setup_done(done: bool = True) -> None:
    cfg = load_manager_config()
    cfg["setup_done"] = bool(done)
    save_manager_config(cfg)


def run_first_time_bootstrap() -> None:
    """Ensure language block + themes exist."""
    from .builtin_themes import ensure_builtin_themes

    ensure_builtin_themes()
    apply_language_preference(get_language())

def normalize_ui_mode(mode: str | None) -> str:
    value = str(mode or "night").strip().lower()
    return "day" if value in {"day", "light", "\u767d\u5929"} else "night"


def cli_theme_for_ui_mode(mode: str | None) -> str:
    """Map the global UI mode to Pi CLI's matching built-in theme."""
    return "light" if normalize_ui_mode(mode) == "day" else "dark"


def sync_cli_theme_with_ui(mode: str | None = None) -> str:
    """Persist Pi CLI's theme so it always follows the manager's global mode."""
    normalized = normalize_ui_mode(mode or get_ui_theme().get("mode"))
    theme = cli_theme_for_ui_mode(normalized)
    settings = load_settings()
    if settings.get("theme") != theme:
        settings["theme"] = theme
        save_settings(settings)
    return theme


def get_ui_theme() -> dict[str, str]:
    cfg = load_manager_config()
    mode = normalize_ui_mode(str(cfg.get("ui_mode") or "night"))
    accent = str(cfg.get("ui_accent") or "blue").strip().lower()
    if accent not in {"blue", "green", "purple", "orange", "cyan"}:
        accent = "blue"
    return {"mode": mode, "accent": accent}


def set_ui_theme(mode: str | None = None, accent: str | None = None) -> dict[str, str]:
    cfg = load_manager_config()
    current = get_ui_theme()
    mode_name = normalize_ui_mode(mode if mode is not None else current.get("mode"))
    accent_name = str(accent if accent is not None else current.get("accent") or "blue").strip().lower()
    if accent_name not in {"blue", "green", "purple", "orange", "cyan"}:
        accent_name = "blue"
    cfg["ui_mode"] = mode_name
    cfg["ui_accent"] = accent_name
    save_manager_config(cfg)
    sync_cli_theme_with_ui(mode_name)
    return {"mode": mode_name, "accent": accent_name}


# ==== 凭据与 Provider 密钥：失效分类 / 环境变量解析 / 运行时凭据 ====


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
    provider = (provider or "").strip()
    if not provider:
        return {"env": {}, "key_id": ""}
    entry = get_provider_config(provider)
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
                    models_path(), {"providers": {}}, persist_reference
                )
                _invalidate_config_cache(models_path())
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
    cfg = load_models_config()
    providers = cfg.get("providers") or {}
    result: dict[str, str] = {}
    for provider in providers if isinstance(providers, dict) else {}:
        try:
            result.update(provider_runtime_env(str(provider)))
        except ProviderKeyError:
            if strict:
                raise
    return result


# ==== HTTP 工具：URL 规范化 / SSL 上下文 / 端点脱敏 / 友好错误 ====
# 已抽到 pi_manager/core_http.py，此处通过顶部 import 重新导出，保持 core.xxx 兼容。


# vision 子系统（智谱识图管道）已抽到 pi_manager/core_vision.py，
# 顶部重新导出保持 core.xxx 兼容。_effective_proxy_url 留在 core（被
# fetch_remote_models / _http_json_request 共用）。


def _effective_proxy_url(explicit: str = "") -> str:
    """Resolve the proxy for an outgoing request (explicit > config > env).

    Invalid (non-http(s) scheme or missing host) values are dropped with a
    warning instead of being handed to urllib.
    """
    candidates: list[str] = []
    explicit = (explicit or "").strip()
    if explicit:
        candidates.append(explicit)
    try:
        cfg = load_manager_config()
        if not explicit and cfg.get("proxy_enabled") and cfg.get("proxy_url"):
            candidates.append(str(cfg.get("proxy_url") or "").strip())
    except Exception:
        pass
    if not candidates:
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = (os.environ.get(var) or "").strip()
            if value:
                candidates.append(value)
                break
    for value in candidates:
        error = validate_proxy_url(value)
        if error:
            logger.warning("忽略无效代理地址「%s」: %s", value, error)
            continue
        return value
    return ""



# ==== Provider 配置查询 / 密钥池管理 / 配置备份 ====


def get_provider_config(provider: str) -> dict[str, Any] | None:
    """Return custom provider entry from models.json, if any."""
    if not provider:
        return None
    cfg = load_models_config()
    providers = cfg.get("providers") or {}
    entry = providers.get(provider)
    return entry if isinstance(entry, dict) else None


def list_orphaned_provider_keys() -> list[dict[str, Any]]:
    """Return key pools stored in the secret store with no matching provider config.

    A provider deleted outside this app (or by an older version) leaves its
    key pool behind; this surfaces those leftovers so they can be cleaned.
    """
    from . import secrets as secretstore

    cfg = load_models_config()
    providers = cfg.get("providers") or {}
    orphaned: list[dict[str, Any]] = []
    for provider, _pool_name, _single_name in secretstore.provider_pool_names():
        if provider in providers:
            continue
        keys = secretstore.list_provider_keys(provider)
        orphaned.append(
            {
                "provider": provider,
                "key_count": len(keys),
                "statuses": sorted({str(k.get("status") or "") for k in keys}),
                "masked": [str(k.get("masked") or "") for k in keys][:3],
            }
        )
    return orphaned


def delete_orphaned_provider_keys() -> int:
    """Delete key pools whose provider no longer exists in models.json."""
    from . import secrets as secretstore

    cfg = load_models_config()
    providers = cfg.get("providers") or {}
    deleted = 0
    for provider, _pool_name, _single_name in secretstore.provider_pool_names():
        if provider in providers:
            continue
        try:
            secretstore.delete_provider_api_keys(provider)
            deleted += 1
        except Exception:
            pass
    return deleted


_BACKUP_TARGETS = frozenset(
    {
        "settings.json",
        "models.json",
        "pi-manager.json",
        "pi-manager-test-history.json",
        "pi-manager-health.json",
        "auth.json",
    }
)


def list_config_backups() -> list[dict[str, str]]:
    """List recoverable ``.bak.*`` config backups inside the agent directory."""
    from datetime import datetime

    rows: list[dict[str, str]] = []
    root = pi_agent_dir()
    if not root.exists():
        return rows
    for path in sorted(root.glob("*.bak.*")):
        if not path.is_file():
            continue
        name = path.name
        target_name = ""
        for target in _BACKUP_TARGETS:
            if name.startswith(target + ".bak."):
                target_name = target
                break
        if not target_name:
            continue
        try:
            st = path.stat()
            mtime_s = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            st = None
            mtime_s = ""
        rows.append(
            {
                "path": str(path),
                "name": name,
                "target": target_name,
                "mtime": mtime_s,
                "size": str(st.st_size) if st is not None else "",
            }
        )
    return rows


def restore_config_backup(backup_path: str | Path) -> dict[str, Any]:
    """Restore a ``.bak.*`` backup back to its target config file (atomic).

    The backup must live in the agent directory and map to a known JSON config
    target, so no path traversal or arbitrary overwrite is possible.
    """
    src = Path(backup_path).resolve()
    root = pi_agent_dir().resolve()
    if src.parent != root:
        return {"ok": False, "error": "备份文件必须在配置目录内"}
    name = src.name
    target_name = ""
    for target in _BACKUP_TARGETS:
        if name.startswith(target + ".bak."):
            target_name = target
            break
    if not target_name:
        return {"ok": False, "error": "不是可恢复的配置备份"}
    try:
        data = load_json(src, None)
    except Exception as exc:
        return {"ok": False, "error": f"备份内容无法解析：{exc}"}
    try:
        save_json(root / target_name, data)
    except Exception as exc:
        return {"ok": False, "error": f"恢复失败：{exc}"}
    return {"ok": True, "target": target_name, "backup": name}


def list_provider_api_keys(provider: str) -> list[dict[str, Any]]:
    from . import secrets as secretstore

    return secretstore.list_provider_keys(provider)


def add_provider_api_key(provider: str, value: str) -> dict[str, Any]:
    from . import secrets as secretstore

    result = secretstore.add_provider_api_key(provider, value)
    cfg = load_models_config()
    providers = cfg.get("providers") or {}
    entry = providers.get(provider)
    if isinstance(entry, dict):
        reference = secretstore.provider_api_key_reference(provider)
        if entry.get("apiKey") != reference:
            entry["apiKey"] = reference
            save_models_config(cfg)
    return result


def remove_provider_api_key(provider: str, key_id: str) -> bool:
    from . import secrets as secretstore

    return secretstore.remove_provider_api_key(provider, key_id)


def restore_provider_api_key(provider: str, key_id: str) -> bool:
    from . import secrets as secretstore

    return secretstore.restore_provider_key(provider, key_id)


def restore_all_provider_api_keys(provider: str) -> int:
    from . import secrets as secretstore

    return secretstore.restore_all_provider_keys(provider)


