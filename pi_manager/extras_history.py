# -*- coding: utf-8 -*-
"""测试历史与模型健康检查。

从 ``extras.py`` 下沉。``pi_manager.extras`` 继续 re-export，保持现有导入与
monkeypatch 点（``extras.xxx``）稳定。对会被测试 patch 的符号走 ``_extras().xxx``。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import core
from . import storage


def _extras():
    from . import extras

    return extras


def history_path() -> Path:
    return core.pi_agent_dir() / "pi-manager-test-history.json"


def health_path() -> Path:
    return core.pi_agent_dir() / "pi-manager-health.json"


def load_history() -> list[dict[str, Any]]:
    data = core.load_json(history_path(), [])
    return data if isinstance(data, list) else []


def save_history(items: list[dict[str, Any]]) -> None:
    # keep last 500
    core.save_json(history_path(), items[-500:])


def append_test_history(results: list[dict[str, Any]]) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    additions = []
    for r in results:
        additions.append(
            {
                "time": ts,
                "provider": r.get("provider"),
                "model": r.get("model"),
                "available": bool(r.get("available")),
                "latency_ms": r.get("latency_ms"),
                "mode": r.get("mode"),
                "error": (str(r.get("error") or "").splitlines()[0][:200] if not r.get("available") else ""),
                "preview": (r.get("preview") or "")[:120],
            }
        )

    def update(current: Any) -> list[dict[str, Any]]:
        hist = current if isinstance(current, list) else []
        return [*hist, *additions][-500:]

    storage.update_json(history_path(), [], update)


def history_for_model(provider: str, model: str, limit: int = 30) -> list[dict[str, Any]]:
    key_p, key_m = provider, model
    rows = [h for h in load_history() if h.get("provider") == key_p and h.get("model") == key_m]
    return rows[-limit:]


def load_health() -> dict[str, Any]:
    return core.load_json(health_path(), {"models": {}, "updated_at": ""})


def save_health(data: dict[str, Any]) -> None:
    core.save_json(health_path(), data)


def collect_model_pairs(scope: str = "favorites", selected: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """scope: favorites|default|custom|all_listed|selected"""
    scope = (scope or "favorites").lower().strip()
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(p: str, m: str):
        p, m = (p or "").strip(), (m or "").strip()
        if not p or not m:
            return
        key = f"{p}/{m}"
        if key in seen:
            return
        seen.add(key)
        pairs.append((p, m))

    if scope == "selected":
        for p, m in selected or []:
            add(p, m)
        return pairs

    if scope == "default":
        p, m, _ = core.get_default_model()
        add(p, m)
        return pairs

    if scope == "favorites":
        mgr = core.load_manager_config()
        for key in mgr.get("favorites") or []:
            parsed = core.parse_favorite_key(str(key))
            if parsed:
                add(parsed[0], parsed[1])
        if not pairs:
            p, m, _ = core.get_default_model()
            add(p, m)
        return pairs

    if scope == "custom":
        cfg = core.load_models_config()
        for name, entry in (cfg.get("providers") or {}).items():
            if not isinstance(entry, dict):
                continue
            models = entry.get("models") or []
            if not models:
                continue
            # test up to first 5 models per provider for batch health
            for item in models[:8]:
                mid = item.get("id") if isinstance(item, dict) else str(item)
                add(str(name), str(mid))
        return pairs

    if scope == "all_listed":
        try:
            for mi in core.list_models():
                add(mi.provider, mi.model)
        except Exception:
            pass
        return pairs

    # fallback favorites
    return collect_model_pairs("favorites")


def _health_entry_from_result(r: dict[str, Any], *, scope: str, ts: str) -> dict[str, Any]:
    return {
        "available": bool(r.get("available")),
        "latency_ms": r.get("latency_ms"),
        "mode": r.get("mode"),
        "error": (
            str(r.get("error") or "").splitlines()[0][:200]
            if not r.get("available")
            else (r.get("preview") or "")[:120]
        ),
        "checked_at": ts,
        "scope": scope,
    }


def run_health_check(
    pairs: list[tuple[str, str]] | None = None,
    *,
    mode: str = "auto",
    scope: str = "favorites",
    selected: list[tuple[str, str]] | None = None,
    on_one: Callable[[dict[str, Any]], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """批量健康检查。

    ``is_cancelled`` 是与 ``ui.Worker`` 约定的协作式取消入口：Worker 检测到本函数
    声明了这个形参就会自动注入 ``isInterruptionRequested``。健康检查是最长的后台
    任务之一（每个模型最多 90s），此前不接这个契约，导致 ``requestInterruption()``
    对它完全是空操作、关闭时的 2.5s 预算形同虚设（R2 UI 审计 P1）。
    取消时**保留已完成的部分结果**并照常写入 health —— 已经花掉的探测不该白费。
    """
    if pairs is None:
        pairs = collect_model_pairs(scope, selected=selected)
    if not pairs:
        return {"ok": False, "results": [], "health": load_health(), "error": "没有可检查的模型（请先收藏、设默认或选择范围）"}

    def _on_one(res: dict[str, Any]):
        if on_one:
            try:
                on_one(res)
            except Exception:
                pass

    results = _extras().test_models_batch_concurrent(
        pairs,
        mode=mode,
        timeout=90 if (mode or "auto").lower().strip() == "pi" else 45,
        max_workers=_extras().get_test_concurrency(),
        on_one=_on_one,
        append_history_each=False,
        is_cancelled=is_cancelled,
    )
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def update_health(current: Any) -> dict[str, Any]:
        health = current if isinstance(current, dict) else {}
        models = health.get("models")
        if not isinstance(models, dict):
            models = {}
        for res in results:
            key = f"{res.get('provider')}/{res.get('model')}"
            models[key] = _health_entry_from_result(res, scope=scope, ts=ts)
        health["models"] = models
        health["updated_at"] = ts
        health["last_scope"] = scope
        return health

    health = storage.update_json(
        health_path(),
        {"models": {}, "updated_at": ""},
        update_health,
    )
    return {
        "ok": True,
        "results": results,
        "health": health,
        "scope": scope,
        "count": len(pairs),
        # 被取消时 results 会短于 pairs：调用方据此区分「全部跑完」与「中途停下」，
        # 否则界面会把部分结果当成完整结论展示。
        "cancelled": bool(is_cancelled and is_cancelled()),
    }
