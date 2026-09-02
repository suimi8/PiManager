"""模型目录与自定义 Provider：列表、默认模型、收藏、增删。"""
from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import storage

logger = logging.getLogger(__name__)


def _core():
    from . import core

    return core



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



def list_models(
    search: str | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> list[ModelInfo]:
    args = ["--list-models"]
    if search:
        args.append(search)
    try:
        p = _core().run_pi(
            args,
            timeout=45,
            env=_core().all_provider_runtime_env(strict=False),
            is_cancelled=is_cancelled,
        )
        if (is_cancelled and is_cancelled()) or "已停止生成" in (p.stderr or ""):
            return []
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
    if thinking:
        # 与启动白名单同规则：defaultThinkingLevel 会作为 --thinking 进入 pi
        # 命令行（rpc_session._ensure），非法值必须在此拦截而非等到启动时。
        _core().validate_launch_tokens(["--thinking", thinking])

    def _apply(settings: dict[str, Any]) -> Any:
        settings["defaultProvider"] = provider
        settings["defaultModel"] = model
        if thinking:
            settings["defaultThinkingLevel"] = thinking
        return settings

    return _core().update_settings(_apply)



def get_default_model() -> tuple[str, str, str]:
    s = _core().load_settings()
    return (
        str(s.get("defaultProvider") or ""),
        str(s.get("defaultModel") or ""),
        str(s.get("defaultThinkingLevel") or DEFAULT_THINKING_LEVEL),
    )



def set_enabled_models(patterns: list[str]) -> dict[str, Any]:
    def _apply(settings: dict[str, Any]) -> Any:
        if patterns:
            settings["enabledModels"] = list(patterns)
        else:
            settings.pop("enabledModels", None)
        return settings

    return _core().update_settings(_apply)



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
    from . import secrets as secretstore

    # 密钥入库放在锁外：它写的是密钥库（另一份存储），且不依赖 models.json 的
    # 当前内容。只有「沿用已存在的 apiKey」这一支必须在锁内读，见 _apply。
    stored_key: str | None = None
    if api_key is not None:
        stored_key = secretstore.store_provider_api_key(name, str(api_key).strip())

    def _apply(cfg: dict[str, Any]) -> Any:
        providers = cfg.setdefault("providers", {})
        existing = providers.get(name) if isinstance(providers.get(name), dict) else {}
        existing = existing or {}
        raw_key = (
            stored_key
            if stored_key is not None
            else str(existing.get("apiKey") or "")
        )
        saved_models = [
            fill_model_defaults(m) if isinstance(m, dict) else m
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
            effective_headers = _core()._openai_compat_headers(api, header_source)
            if effective_headers or headers is not None or "headers" in existing:
                entry["headers"] = secretstore.store_provider_headers(
                    name, effective_headers
                )
        elif "headers" in existing:
            entry["headers"] = existing["headers"]
        providers[name] = entry
        return cfg

    cfg = _core().update_models_config(_apply)
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
    kept: list[str] = []
    removed: list[str] = []

    def _apply(mgr: dict[str, Any]) -> Any:
        nonlocal kept, removed
        kept, removed = [], []
        favs = list(mgr.get("favorites") or [])
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
        # favs != kept 也算改动：磁盘上可能存着非字符串收藏项，顺手规范化。
        if not removed and favs == kept:
            return storage.UNCHANGED
        mgr["favorites"] = kept
        return mgr

    _core().update_manager_config(_apply)

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
            def _clear_default(settings: dict[str, Any]) -> Any:
                settings["defaultProvider"] = ""
                settings["defaultModel"] = ""
                return settings

            _core().update_settings(_clear_default)
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
    removed: list[str] = []

    def _apply(settings: dict[str, Any]) -> Any:
        nonlocal removed
        removed = []
        patterns = settings.get("enabledModels")
        if not isinstance(patterns, list):
            return storage.UNCHANGED
        kept: list[str] = []
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
        if not removed:
            return storage.UNCHANGED
        if kept:
            settings["enabledModels"] = kept
        else:
            settings.pop("enabledModels", None)
        return settings

    _core().update_settings(_apply)
    return removed



def list_stale_enabled_models(builtin_providers: set[str] | None = None) -> list[str]:
    """返回 settings.enabledModels 中引用已不存在 Provider 的残留模式。

    ``builtin_providers`` 传入 Pi 内置 Provider 名集合时可避免误报；
    不传时仅与 models.json 中的自定义 Provider 比对。
    """
    settings = _core().load_settings()
    patterns = settings.get("enabledModels")
    if not isinstance(patterns, list):
        return []
    cfg = _core().load_models_config()
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
    """删除自定义 Provider 及其收藏 / enabledModels / 密钥池残留。

    跨 4 份持久状态（pi-manager.json、settings.json、密钥库、models.json）无法做
    真正的事务，所以顺序上「先删依赖、后删主体」：中途失败留下的是「Provider 还在
    但引用已清」—— 用户重试即可收敛；反过来（Provider 没了、引用还在）会留下悬挂
    状态，只能靠 `run_self_check` 事后发现。失败原因聚合进 ``_partial_failures``，
    让 UI 能提示用户而不是静默吞掉。
    """
    failures: list[str] = []

    # 1. 收藏（含默认模型改指）
    purge: dict[str, Any] = {
        "removed_favorites": [],
        "favorites": [],
        "default_changed": False,
    }
    try:
        purge = purge_favorites(provider=name, redefault=True)
    except Exception as exc:
        logger.warning("删除 provider「%s」后清理收藏失败: %s", name, exc)
        failures.append(f"清理收藏失败：{exc}")

    # 2. settings.enabledModels 残留模式，避免 Pi 每次启动输出
    #    "No models match pattern" 警告并污染测试结果
    purged_enabled: list[str] = []
    try:
        purged_enabled = purge_enabled_models(provider=name)
    except Exception as exc:
        logger.warning("删除 provider「%s」后清理 enabledModels 失败: %s", name, exc)
        failures.append(f"清理 enabledModels 失败：{exc}")

    # 3. 密钥池 / 头部密钥（要在删掉 provider 条目之前读出 headers）
    existing_entry = _core().get_provider_config(name)
    try:
        from . import secrets as secretstore

        if isinstance(existing_entry, dict) and isinstance(
            existing_entry.get("headers"), dict
        ):
            secretstore.delete_provider_header_secrets(name, existing_entry["headers"])
        secretstore.delete_provider_api_keys(name)
    except Exception as exc:
        logger.warning("删除 provider「%s」的密钥/头清理失败: %s", name, exc)
        failures.append(f"清理密钥失败：{exc}")

    # 4. 主体：models.json 里的 provider 条目
    def _apply(cfg: dict[str, Any]) -> Any:
        providers = cfg.setdefault("providers", {})
        if name not in providers:
            return storage.UNCHANGED
        del providers[name]
        return cfg

    cfg = _core().update_models_config(_apply)

    # _purge / _purged_enabled 是给 UI 的返回通道，不属于 models.json schema；
    # 挂在返回值的副本上，并由 save_models_config 兜底拦截（见 _sanitize_models_config）。
    result = dict(cfg) if isinstance(cfg, dict) else {"providers": {}}
    result["_purge"] = purge
    result["_purged_enabled"] = purged_enabled
    if failures:
        result["_partial_failures"] = failures
    return result



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
    def _apply(cfg: dict[str, Any]) -> Any:
        providers = cfg.setdefault("providers", {})
        entry = providers.get(provider)
        if not isinstance(entry, dict):
            raise KeyError(f"provider not found: {provider}")
        models = entry.get("models")
        # replace if exists（非 dict 条目原样保留，别让脏数据把整个操作打崩）
        kept = [
            m
            for m in (models if isinstance(models, list) else [])
            if not (isinstance(m, dict) and m.get("id") == model_id)
        ]
        item = fill_model_defaults({"id": model_id, **kwargs})
        if "cost" not in item:
            item["cost"] = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        kept.append(item)
        entry["models"] = kept
        return cfg

    return _core().update_models_config(_apply)



def remove_model_from_provider(provider: str, model_id: str) -> dict[str, Any]:
    def _apply(cfg: dict[str, Any]) -> Any:
        providers = cfg.setdefault("providers", {})
        entry = providers.get(provider)
        if not isinstance(entry, dict):
            return storage.UNCHANGED
        models = entry.get("models")
        if not isinstance(models, list):
            return storage.UNCHANGED
        kept = [
            m for m in models if not (isinstance(m, dict) and m.get("id") == model_id)
        ]
        if kept == models:
            return storage.UNCHANGED
        entry["models"] = kept
        return cfg

    cfg = _core().update_models_config(_apply)

    result = dict(cfg) if isinstance(cfg, dict) else {"providers": {}}
    try:
        result["_purge"] = purge_favorites(
            provider=provider, model=model_id, redefault=True
        )
    except Exception as exc:
        logger.warning("移除模型后清理收藏失败: %s", exc)
        result["_purge"] = {
            "removed_favorites": [],
            "favorites": [],
            "default_changed": False,
        }
    try:
        result["_purged_enabled"] = purge_enabled_models(
            provider=provider, model=model_id
        )
    except Exception as exc:
        logger.warning("移除模型后清理 enabledModels 失败: %s", exc)
        result["_purged_enabled"] = []
    return result




# 拉取 / 添加 / 一键配置能力时的缺省值：1M 上下文，默认只开思考、不含图片。
DEFAULT_CONTEXT_WINDOW = 1_048_576
DEFAULT_MAX_TOKENS = 32_768
CONTEXT_WINDOW_PRESETS: tuple[tuple[str, int], ...] = (
    ("128K", 131_072),
    ("200K", 200_000),
    ("256K", 262_144),
    ("512K", 524_288),
    ("1M", DEFAULT_CONTEXT_WINDOW),
)


def default_model_template(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "name": model_id,
        "reasoning": True,
        "input": ["text"],
        "contextWindow": DEFAULT_CONTEXT_WINDOW,
        "maxTokens": DEFAULT_MAX_TOKENS,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }


def apply_model_capabilities(
    model: dict[str, Any] | Any,
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    reasoning: bool = True,
    images: bool = False,
) -> dict[str, Any]:
    """覆盖写入上下文与能力：默认 1M 上下文、仅思考、不含图片。

    与 :func:`fill_model_defaults` 不同，这里会改已有 ``contextWindow`` /
    ``reasoning`` / ``input``，供拉取后一键配置和模型页批量改能力使用。
    """
    if isinstance(model, dict):
        result = dict(model)
        mid = str(result.get("id") or result.get("name") or "").strip()
    else:
        result = {}
        mid = str(model or "").strip()
    if not mid:
        return result if isinstance(model, dict) else {"id": "", "name": ""}
    result["id"] = str(result.get("id") or mid).strip() or mid
    result["name"] = str(result.get("name") or mid).strip() or mid
    result["contextWindow"] = int(context_window)
    result["reasoning"] = bool(reasoning)
    inputs = ["text"]
    if images:
        inputs.append("image")
    result["input"] = inputs
    if "maxTokens" not in result:
        result["maxTokens"] = DEFAULT_MAX_TOKENS
    if "cost" not in result:
        result["cost"] = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    if reasoning:
        return ensure_thinking_level_map(result)
    result.pop("thinkingLevelMap", None)
    return result


def apply_model_capabilities_many(
    models: list[Any],
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    reasoning: bool = True,
    images: bool = False,
) -> list[dict[str, Any]]:
    """对模型列表逐条覆盖能力；无 id 的条目原样跳过。"""
    out: list[dict[str, Any]] = []
    for entry in models or []:
        if isinstance(entry, dict) and not str(entry.get("id") or entry.get("name") or "").strip():
            out.append(entry)
            continue
        out.append(
            apply_model_capabilities(
                entry,
                context_window=context_window,
                reasoning=reasoning,
                images=images,
            )
        )
    return out


def apply_capabilities_to_saved_models(
    pairs: list[tuple[str, str]],
    *,
    context_window: int = DEFAULT_CONTEXT_WINDOW,
    reasoning: bool = True,
    images: bool = False,
) -> dict[str, Any]:
    """把能力写进 ``models.json`` 里已有的自定义 Provider 模型。

    内置 / 未落盘的模型会计入 ``skipped``，不会新建 Provider。
    """
    wanted: dict[str, set[str]] = {}
    for provider, model in pairs:
        name = str(provider or "").strip()
        mid = str(model or "").strip()
        if name and mid:
            wanted.setdefault(name, set()).add(mid)
    updated = 0
    skipped = 0

    def _apply(cfg: dict[str, Any]) -> Any:
        nonlocal updated, skipped
        providers = cfg.get("providers")
        if not isinstance(providers, dict) or not wanted:
            return storage.UNCHANGED
        updated_providers = dict(providers)
        changed = False
        remaining = {name: set(ids) for name, ids in wanted.items()}
        for name, ids in wanted.items():
            entry = providers.get(name)
            if not isinstance(entry, dict):
                skipped += len(ids)
                remaining.pop(name, None)
                continue
            models = entry.get("models")
            if not isinstance(models, list):
                skipped += len(ids)
                remaining.pop(name, None)
                continue
            new_models: list[Any] = []
            seen: set[str] = set()
            any_changed = False
            for item in models:
                if not isinstance(item, dict):
                    new_models.append(item)
                    continue
                mid = str(item.get("id") or item.get("name") or "").strip()
                if mid in ids:
                    new_models.append(
                        apply_model_capabilities(
                            item,
                            context_window=context_window,
                            reasoning=reasoning,
                            images=images,
                        )
                    )
                    seen.add(mid)
                    updated += 1
                    any_changed = True
                else:
                    new_models.append(item)
            skipped += len(ids - seen)
            remaining.pop(name, None)
            if not any_changed:
                continue
            updated_entry = dict(entry)
            updated_entry["models"] = new_models
            updated_providers[name] = updated_entry
            changed = True
        skipped += sum(len(ids) for ids in remaining.values())
        if not changed:
            return storage.UNCHANGED
        result = dict(cfg)
        result["providers"] = updated_providers
        return result

    _core().update_models_config(_apply)
    return {"updated": updated, "skipped": skipped}



def fill_model_defaults(model: dict[str, Any]) -> dict[str, Any]:
    """补全手填模型缺少的 Pi 字段，与拉取 / 「添加模型」使用同一套缺省值。

    缺省为 1M 上下文、``reasoning: true``、仅文本输入。只补缺失键，不覆盖
    用户已写的 ``reasoning: false`` 等显式值。补上 ``reasoning`` 后若仍无
    ``thinkingLevelMap``，再交给 :func:`ensure_thinking_level_map`。
    未改动时返回原对象，供迁移做身份判断。
    """
    if not isinstance(model, dict):
        return model
    mid = str(model.get("id") or model.get("name") or "").strip()
    if not mid:
        return model
    result = dict(model)
    changed = False
    if not str(result.get("id") or "").strip():
        result["id"] = mid
        changed = True
    if not str(result.get("name") or "").strip():
        result["name"] = mid
        changed = True
    for key, value in default_model_template(mid).items():
        if key in {"id", "name"}:
            continue
        if key not in result:
            result[key] = value
            changed = True
    filled = ensure_thinking_level_map(result)
    if filled is not result:
        return filled
    return result if changed else model
