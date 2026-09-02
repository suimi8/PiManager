# -*- coding: utf-8 -*-
"""代理设置、测试并发与批量模型探测。

从 ``extras.py`` 下沉。``pi_manager.extras`` 继续 re-export，保持现有导入与
monkeypatch 点（``extras.xxx``）稳定。对会被测试 patch 的符号走 ``_extras().xxx``。
"""
from __future__ import annotations

import concurrent.futures
import os
from typing import Any, Callable

from . import core


def _extras():
    from . import extras

    return extras


def get_proxy_settings() -> dict[str, Any]:
    cfg = core.load_manager_config()
    enabled = bool(cfg.get("proxy_enabled"))
    url = str(cfg.get("proxy_url") or "").strip()
    # also surface env
    env = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    ).strip()
    effective = url if enabled and url else env
    return {
        "enabled": enabled,
        "url": url,
        "env": env,
        "effective": effective,
    }


def _validate_proxy_url(url: str) -> str:
    """Validate a proxy URL; return an error message, or "" when acceptable."""
    url = (url or "").strip()
    if not url:
        return ""
    return core.validate_proxy_url(url)


def set_proxy_settings(enabled: bool, url: str) -> dict[str, Any]:
    url = (url or "").strip()
    if url:
        proxy_error = _validate_proxy_url(url)
        if proxy_error:
            raise ValueError(proxy_error)

    def _apply(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["proxy_enabled"] = bool(enabled)
        cfg["proxy_url"] = url
        return cfg

    core.update_manager_config(_apply)
    # apply to process env for child pi processes when enabled
    apply_proxy_env()
    return get_proxy_settings()


def apply_proxy_env() -> None:
    ps = get_proxy_settings()
    eff = ps.get("effective") or ""
    if eff:
        os.environ["HTTPS_PROXY"] = eff
        os.environ["HTTP_PROXY"] = eff
        os.environ["https_proxy"] = eff
        os.environ["http_proxy"] = eff
    # do not delete user env if manager proxy disabled — leave system env alone


def effective_proxy(explicit: str = "") -> str:
    if (explicit or "").strip():
        return explicit.strip()
    return str(get_proxy_settings().get("effective") or "")


def get_test_concurrency() -> int:
    cfg = core.load_manager_config()
    try:
        n = int(cfg.get("test_concurrency") or 3)
    except Exception:
        n = 3
    return max(1, min(n, 8))


def set_test_concurrency(n: int) -> None:
    value = max(1, min(int(n), 8))

    def _apply(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["test_concurrency"] = value
        return cfg

    core.update_manager_config(_apply)


def test_models_batch_concurrent(
    pairs: list[tuple[str, str]],
    *,
    mode: str = "auto",
    timeout: float = 60,
    insecure_ssl: bool = False,
    proxy: str = "",
    workdir: str | None = None,
    max_workers: int | None = None,
    on_one: Callable[[dict[str, Any]], None] | None = None,
    append_history_each: bool = False,
    is_cancelled: Callable[[], bool] | None = None,
) -> list[dict[str, Any]]:
    """Concurrent model tests with ordered result list matching input pairs.

    on_one: called as each model finishes (from worker threads).
    append_history_each: write history per result; otherwise batch-append at end.
    """
    if not pairs:
        return []
    apply_proxy_env()
    workers = max_workers or get_test_concurrency()
    proxy = effective_proxy(proxy)

    def one(idx_pair: tuple[int, tuple[str, str]]) -> tuple[int, dict[str, Any]]:
        idx, (provider, model) = idx_pair
        try:
            res = core.test_model(
                provider,
                model,
                mode=mode,
                timeout=timeout,
                insecure_ssl=insecure_ssl,
                proxy=proxy,
                workdir=workdir,
            )
        except Exception as e:
            res = {
                "ok": False,
                "available": False,
                "mode": mode,
                "provider": provider,
                "model": model,
                "latency_ms": None,
                "error": str(e),
                "preview": "",
                "endpoint": "",
                "http_status": 0,
            }
        if append_history_each:
            try:
                _extras().append_test_history([res])
            except Exception:
                pass
        if on_one:
            try:
                on_one(res)
            except Exception:
                pass
        return idx, res

    results: list[dict[str, Any] | None] = [None] * len(pairs)
    indexed = iter(enumerate(pairs))
    in_flight: set[concurrent.futures.Future[tuple[int, dict[str, Any]]]] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        def submit_until_budget() -> None:
            while len(in_flight) < workers * 2 and not (is_cancelled and is_cancelled()):
                try:
                    item = next(indexed)
                except StopIteration:
                    return
                in_flight.add(pool.submit(one, item))

        submit_until_budget()
        while in_flight:
            done, in_flight = concurrent.futures.wait(
                in_flight,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done:
                idx, res = fut.result()
                results[idx] = res
            if is_cancelled and is_cancelled():
                for fut in in_flight:
                    fut.cancel()
                break
            submit_until_budget()
    out = [r if r is not None else {"ok": False, "available": False, "error": "missing"} for r in results]
    if not append_history_each:
        try:
            _extras().append_test_history(out)
        except Exception:
            pass
    return out
