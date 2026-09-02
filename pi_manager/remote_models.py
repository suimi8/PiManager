"""上游模型目录的纯函数：搜索、勾选集合与 JSON 解析。

不读盘、不写 HOME、不依赖 Qt。对话框与测试共用同一套匹配规则。
"""
from __future__ import annotations

import json
from typing import Any


def model_id(entry: Any) -> str:
    if isinstance(entry, dict):
        return str(entry.get("id") or entry.get("name") or "").strip()
    return str(entry or "").strip()


def model_search_text(entry: Any) -> str:
    if isinstance(entry, dict):
        parts = [
            str(entry.get("id") or ""),
            str(entry.get("name") or ""),
            str(entry.get("owned_by") or ""),
        ]
        return " ".join(parts).lower()
    return str(entry or "").lower()


def filter_remote_models(models: list[Any], query: str) -> list[Any]:
    """按空格分词，全部命中 id / 名称才保留。空查询返回原列表副本。"""
    tokens = [part for part in str(query or "").strip().lower().split() if part]
    if not tokens:
        return list(models)
    matched: list[Any] = []
    for entry in models:
        haystack = model_search_text(entry)
        if all(token in haystack for token in tokens):
            matched.append(entry)
    return matched


def ids_from_models(models: list[Any]) -> set[str]:
    return {model_id(entry) for entry in models if model_id(entry)}


def models_from_json_text(text: str) -> list[Any]:
    payload = json.loads(text or "[]")
    if not isinstance(payload, list):
        raise ValueError("Models 必须是数组")
    return payload


def checked_models(models: list[Any], checked_ids: set[str]) -> list[Any]:
    wanted = {str(item).strip() for item in checked_ids if str(item).strip()}
    return [entry for entry in models if model_id(entry) in wanted]
