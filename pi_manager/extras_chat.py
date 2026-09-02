# -*- coding: utf-8 -*-
"""快速提问、失败计数与故障切换。

从 ``extras.py`` 下沉。``pi_manager.extras`` 继续 re-export，保持现有导入与
monkeypatch 点（``extras.xxx``）稳定。对会被测试 patch 的符号走 ``_extras().xxx``。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from . import core


def _extras():
    from . import extras

    return extras


def _cancelled(flag: Callable[[], bool] | None) -> bool:
    return bool(flag and flag())


def chat_once(
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    workdir: str | None = None,
    timeout: float = 180,
    thinking: str | None = "off",
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    if _cancelled(is_cancelled):
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "已停止生成",
            "latency_ms": 0,
            "provider": provider,
            "model": model,
            "error": "已停止生成",
            "cancelled": True,
        }
    _extras().apply_proxy_env()
    t0 = time.perf_counter()
    try:
        code, out, err = core.run_pi_print(
            prompt,
            workdir=workdir or str(core.user_home()),
            provider=provider,
            model=model,
            thinking=thinking or "off",
            timeout=timeout,
            is_cancelled=is_cancelled,
        )
    except Exception as exc:
        code, out, err = -1, "", str(exc)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    text = (out or "").strip()
    err_text = (err or "").strip()
    cancelled = _cancelled(is_cancelled) or "已停止生成" in err_text
    ok = (not cancelled) and code == 0 and bool(text)
    if code == 0 and not text and err_text and "error" not in err_text.lower():
        text = err_text
        ok = not cancelled
    return {
        "ok": ok,
        "returncode": code,
        "stdout": out,
        "stderr": err,
        "latency_ms": ms,
        "provider": provider,
        "model": model,
        "error": "" if ok else (err_text or text or f"退出码 {code}"),
        "cancelled": cancelled,
    }


def failover_chain(start_provider: str | None = None, start_model: str | None = None) -> list[tuple[str, str]]:
    """故障切换候选链：当前模型 → 收藏 → enabledModels → 默认，去重保序。"""
    chain: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(p: str | None, m: str | None):
        p = (p or "").strip()
        m = (m or "").strip()
        if not p or not m:
            return
        k = f"{p}/{m}"
        if k in seen:
            return
        seen.add(k)
        chain.append((p, m))

    add(start_provider, start_model)
    mgr = core.load_manager_config()
    for key in mgr.get("favorites") or []:
        parsed = core.parse_favorite_key(str(key))
        if parsed:
            add(parsed[0], parsed[1])
    try:
        settings = core.load_settings()
        for key in settings.get("enabledModels") or []:
            parsed = core.parse_favorite_key(str(key))
            if parsed:
                add(parsed[0], parsed[1])
        dp = str(settings.get("defaultProvider") or "")
        dm = str(settings.get("defaultModel") or "")
        add(dp, dm)
    except Exception:
        pass
    return chain


def _model_pair_key(provider: str | None, model: str | None) -> str:
    try:
        pair = core.normalize_model_pair(provider, model)
    except ValueError:
        return ""
    return f"{pair[0]}/{pair[1]}" if pair is not None else ""


def _parse_fail_counts(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except Exception:
            out[str(k)] = 0
    return out


def _fail_counts() -> dict[str, int]:
    mgr = core.load_manager_config()
    return _parse_fail_counts(mgr.get("failover_fail_counts"))


# 串行化 fail_count 的读-改-写，避免跨线程并发丢失更新。
# 注意：file lock 不可重入，updater 内部禁止再 load_manager_config。
_fail_counts_lock = threading.Lock()


def _save_fail_counts(counts: dict[str, int]) -> None:
    """只更新 failover_fail_counts 键，不覆盖 pi-manager.json 其它字段。"""

    def _apply(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg["failover_fail_counts"] = dict(counts)
        return cfg

    core.update_manager_config(_apply)


def record_model_success(provider: str, model: str) -> None:
    key = _model_pair_key(provider, model)
    if not key:
        return
    with _fail_counts_lock:

        def _apply(cfg: dict[str, Any]) -> dict[str, Any]:
            counts = _parse_fail_counts(cfg.get("failover_fail_counts"))
            if key not in counts:
                return cfg
            counts[key] = 0
            cfg["failover_fail_counts"] = counts
            return cfg

        core.update_manager_config(_apply)


def record_model_failure(provider: str, model: str) -> int:
    """累计失败次数并返回当前计数。"""
    key = _model_pair_key(provider, model)
    if not key:
        return 0
    new_count = 0
    with _fail_counts_lock:

        def _apply(cfg: dict[str, Any]) -> dict[str, Any]:
            nonlocal new_count
            counts = _parse_fail_counts(cfg.get("failover_fail_counts"))
            new_count = int(counts.get(key) or 0) + 1
            counts[key] = new_count
            cfg["failover_fail_counts"] = counts
            return cfg

        core.update_manager_config(_apply)
        return new_count


def should_failover(provider: str, model: str) -> bool:
    mgr = core.load_manager_config()
    if not bool(mgr.get("failover_enabled", True)):
        return False
    thr = int(mgr.get("failover_fail_threshold") or 3)
    thr = max(1, thr)
    key = _model_pair_key(provider, model)
    return bool(key) and int(_fail_counts().get(key) or 0) >= thr


def _chat_attempt(
    prompt: str,
    *,
    provider: str | None,
    model: str | None,
    workdir: str | None,
    timeout: float,
    thinking: str | None,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """One chat attempt: persistent RPC session when available, else one-shot.

    The RPC session keeps conversation context in-process and lets failover
    hot-switch models via set_model; when `pi --mode rpc` is unusable the
    session layer disables itself for the rest of the run and every attempt
    falls back to the classic one-shot `pi -p` path.

    上游 5xx / ``upstream_overloaded`` 会在同一模型上短暂重试，避免 Grokified
    这类中转把瞬时过载误报成「无法对话」。
    """
    from . import rpc_session
    from .core_http import is_transient_upstream_error, transient_retry_delay

    def _once() -> dict[str, Any]:
        _extras().apply_proxy_env()
        if rpc_session.rpc_chat_enabled():
            result = rpc_session.rpc_chat_once(
                prompt,
                provider=provider,
                model=model,
                workdir=workdir,
                timeout=timeout,
                thinking=thinking,
                is_cancelled=is_cancelled,
            )
            if result.get("ok") or rpc_session.rpc_chat_enabled():
                return result
            # rpc became unavailable during this attempt — retry one-shot
        return _extras().chat_once(
            prompt,
            provider=provider,
            model=model,
            workdir=workdir,
            timeout=timeout,
            thinking=thinking,
            is_cancelled=is_cancelled,
        )

    if _cancelled(is_cancelled):
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "已停止生成",
            "latency_ms": 0,
            "provider": provider,
            "model": model,
            "error": "已停止生成",
            "cancelled": True,
        }
    last = _once()
    max_attempts = core.TRANSIENT_HTTP_MAX_ATTEMPTS
    for attempt in range(max_attempts - 1):
        if last.get("ok") or last.get("cancelled"):
            return last
        if _cancelled(is_cancelled):
            last = dict(last)
            last["cancelled"] = True
            last["error"] = last.get("error") or "已停止生成"
            return last
        blob = "\n".join(
            str(last.get(key) or "") for key in ("error", "stderr", "stdout")
        )
        if not is_transient_upstream_error(blob):
            return last
        core.sleep_transient_retry(transient_retry_delay(attempt))
        last = _once()
    return last


def chat_with_failover(
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    workdir: str | None = None,
    timeout: float = 180,
    thinking: str | None = "off",
    set_as_default_on_switch: bool = True,
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """快速提问 + 连续失败自动切换下一个模型。

    规则：同一模型累计失败达到 failover_fail_threshold（默认 3）后，
    自动跳到候选链下一个模型重试同一 prompt，尽量无感继续对话。
    """
    mgr = core.load_manager_config()
    enabled = bool(mgr.get("failover_enabled", True))
    thr = max(1, int(mgr.get("failover_fail_threshold") or 3))
    silent = bool(mgr.get("failover_silent", True))

    try:
        requested_pair = core.normalize_model_pair(provider, model)
        if requested_pair is not None:
            provider, model = requested_pair
        else:
            dp, dm, _ = core.get_default_model()
            default_pair = core.normalize_model_pair(dp, dm)
            if default_pair is not None:
                provider, model = default_pair
    except ValueError as exc:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
            "latency_ms": 0,
            "provider": provider,
            "model": model,
            "switched": False,
            "attempts": [],
            "error": str(exc),
        }

    chain = failover_chain(provider, model)
    if not chain:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "无可用模型（请配置默认或收藏）",
            "latency_ms": 0,
            "provider": provider,
            "model": model,
            "switched": False,
            "attempts": [],
            "error": "无可用模型",
        }

    # 从当前模型在链中的位置开始；若已达失败阈值，则直接从下一个开始
    start_idx = 0
    for i, (p, m) in enumerate(chain):
        if p == (provider or "") and m == (model or ""):
            start_idx = i
            break
    if enabled and should_failover(chain[start_idx][0], chain[start_idx][1]):
        start_idx = min(start_idx + 1, len(chain) - 1)

    attempts: list[dict[str, Any]] = []
    last: dict[str, Any] = {}
    switched_from: str | None = None

    for idx in range(start_idx, len(chain)):
        if _cancelled(is_cancelled):
            last = last or {
                "ok": False,
                "returncode": -1,
                "stdout": "",
                "stderr": "已停止生成",
                "latency_ms": 0,
                "provider": provider,
                "model": model,
                "error": "已停止生成",
            }
            last = dict(last)
            last["cancelled"] = True
            last["switched"] = bool(switched_from)
            last["switched_from"] = switched_from
            last["attempts"] = attempts
            last["error"] = last.get("error") or "已停止生成"
            return last
        p, m = chain[idx]
        # 若该模型已达阈值且不是链尾唯一选择，跳过
        if enabled and idx > start_idx and should_failover(p, m) and idx < len(chain) - 1:
            attempts.append({"provider": p, "model": m, "skipped": True, "reason": f"已连续失败≥{thr}"})
            continue

        result = _chat_attempt(
            prompt,
            provider=p,
            model=m,
            workdir=workdir,
            timeout=timeout,
            thinking=thinking,
            is_cancelled=is_cancelled,
        )
        result = dict(result)
        result["attempt_index"] = idx
        attempts.append(
            {
                "provider": p,
                "model": m,
                "ok": result.get("ok"),
                "returncode": result.get("returncode"),
                "latency_ms": result.get("latency_ms"),
                "error": result.get("error") or "",
            }
        )
        last = result
        if result.get("cancelled"):
            last["switched"] = bool(switched_from)
            last["switched_from"] = switched_from
            last["attempts"] = attempts
            last["silent"] = silent
            last["failover_enabled"] = enabled
            last["notice"] = ""
            return last

        if result.get("ok"):
            record_model_success(p, m)
            switched = bool(switched_from) or (p != (provider or "") or m != (model or ""))
            if switched and set_as_default_on_switch:
                try:
                    core.set_default_model(p, m)
                except Exception:
                    pass
            last["switched"] = switched
            last["switched_from"] = switched_from
            last["attempts"] = attempts
            last["silent"] = silent
            last["failover_enabled"] = enabled
            if switched and not silent:
                last["notice"] = f"已自动切换：{switched_from or f'{provider}/{model}'} → {p}/{m}"
            elif switched and silent:
                last["notice"] = ""  # 无感：不在正文强调
            else:
                last["notice"] = ""
            return last

        # 失败：累计
        count = record_model_failure(p, m)
        attempts[-1]["fail_count"] = count
        if not enabled:
            break
        if count < thr:
            # 未达阈值：本轮返回失败，下次继续累计
            break
        # 达阈值：本轮内立刻切下一个模型重试同一问题（无感继续）
        if switched_from is None:
            switched_from = f"{p}/{m}"
        continue

    if last:
        last["switched"] = bool(switched_from)
        last["switched_from"] = switched_from
        last["attempts"] = attempts
        last["silent"] = silent
        last["failover_enabled"] = enabled
        last["notice"] = "" if silent else (f"尝试切换失败，已用尽候选（自 {switched_from}）" if switched_from else "")
        return last
    return {
        "ok": False,
        "returncode": -1,
        "stdout": "",
        "stderr": "全部候选模型失败",
        "latency_ms": 0,
        "provider": provider,
        "model": model,
        "switched": False,
        "attempts": attempts,
        "error": "全部候选模型失败",
    }
