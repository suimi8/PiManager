"""快速提问页的 Provider / 模型列表合并与选中规则（无 Qt）。

纯函数：不读盘、不写 HOME、不依赖 presentation。调用方负责从
``core.load_models_config`` / ``core.get_default_model`` 取数后再传入。
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any


def providers_from_models_config(cfg: dict[str, Any]) -> list[str]:
    """从 models.json 结构取出 provider 名称（未排序、未去重）。

    空名称丢弃。去重与排序交给 ``merge_provider_names``。
    """
    names: list[str] = []
    for name in cfg.get("providers") or {}:
        if name:
            names.append(str(name))
    return names


def config_models_for_provider(cfg: dict[str, Any], provider: str) -> list[Any]:
    """取出 models.json 中某 provider 的原始 ``models`` 列表。

    返回原样条目（dict / str 等），由 ``merge_model_ids`` 解析 id。
    """
    pdata = (cfg.get("providers") or {}).get(provider) or {}
    items = pdata.get("models") or []
    return list(items)


def merge_provider_names(
    listed_providers: Iterable[str],
    config_providers: Iterable[str],
) -> list[str]:
    """合并 list-models 与 models.json 的 provider 名，去重后排序。

    空串丢弃。结果与 ``sorted(set(...))`` 一致。
    """
    names: set[str] = set()
    for source in (listed_providers, config_providers):
        for raw in source:
            name = str(raw or "")
            if name:
                names.add(name)
    return sorted(names)


def pick_provider(current: str, providers: Sequence[str], default_provider: str) -> str:
    """决定 Provider 下拉的选中项。

    当前选择仍在列表中 → 默认 provider 仍在列表中 → 第一项；全空则 ``""``。
    """
    if current and current in providers:
        return current
    if default_provider and default_provider in providers:
        return default_provider
    if providers:
        return providers[0]
    return ""


def _config_model_id(item: Any) -> str:
    """从 models.json 的 models 项取出模型 id。"""
    if isinstance(item, dict):
        return str(item.get("id") or item.get("model") or "")
    if isinstance(item, str):
        return item
    return ""


def merge_model_ids(
    listed_ids: Iterable[str],
    config_models: Iterable[Any],
) -> list[str]:
    """合并 list-models 结果与 models.json 该 provider 的 models，保序去重。

    listed 在前；json 增量在后。不排序。dict 取 ``id`` 或 ``model``，裸 str 用自身；
    空串与已存在项跳过。
    """
    result: list[str] = []
    seen: set[str] = set()
    for raw in listed_ids:
        mid = str(raw or "")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        result.append(mid)
    for item in config_models:
        mid = _config_model_id(item)
        if not mid or mid in seen:
            continue
        seen.add(mid)
        result.append(mid)
    return result


def pick_model(
    prefer: str,
    models: Sequence[str],
    *,
    provider: str,
    default_provider: str,
    default_model: str,
) -> str:
    """决定模型下拉的选中项。

    prefer 仍在列表中 → 同 provider 的默认模型在列表中 → 第一项；全空则 ``""``。
    """
    if prefer and prefer in models:
        return prefer
    if provider and provider == default_provider and default_model in models:
        return default_model
    if models:
        return models[0]
    return ""
