# -*- coding: utf-8 -*-
"""远程模型获取与 HTTP 连通性测试。

从 ``core.py`` 抽出：fetch_remote_models（拉取 provider 模型列表）与
test_model_http / test_model_via_pi / test_model（模型可用性测试）。
对 core 配置/凭据函数用函数内延迟 import，避免循环依赖；对 _http_json_request
等被测试 monkeypatch 的符号走 core.xxx 动态查找，保持 patch 兼容。
core.py 顶部重新导出这些符号。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .core_http import (
    _friendly_fetch_error,
    _ssl_context,
    is_transient_http_status,
    normalize_openai_base_url,
    redact_endpoint_url,
    redact_secret_values,
    transient_retry_delay,
)
from .core_process import _check_request_scheme


def _resolve_provider_runtime_key(
    provider: str, raw_key: str
) -> tuple[str, str, str]:
    """Resolve the API key (and managed-key id) for a provider request.

    Shared by :func:`fetch_remote_models` and :func:`test_model_http` to keep the
    managed-key detection, credential fetch, and plain-key fallback identical.

    Returns ``(key_id, api_key, error_or_empty)``. ``key_id`` is non-empty only
    when the key came from the managed credential pool, so callers can wire it
    into failover. On a :class:`ProviderKeyError` the key is ``""`` and the
    third element carries the message — the caller decides how to surface it,
    since the two call sites return differently shaped error dicts.
    """
    from . import core

    key_id = ""
    if provider:
        from . import secrets as secretstore

        managed_key = raw_key.startswith("__DPAPI__:") or (
            secretstore.referenced_env_name(raw_key)
            == secretstore.provider_env_name(provider)
        )
        if managed_key:
            try:
                credential = core.provider_runtime_credential(provider)
                key_id = str(credential.get("key_id") or "")
                # Read the resolved API key explicitly rather than the first env
                # value: provider_runtime_credential may also carry sensitive
                # header secrets, whose value must never be used as the API key.
                key = str(credential.get("key") or "")
            except core.ProviderKeyError as exc:
                return "", "", str(exc)
            return key_id, key, ""
    # No provider, or a plain/literal key: resolve through the shared path.
    # resolve_api_key_value normalizes the value itself, so the already-stripped
    # raw_key is safe to pass.
    return "", core.resolve_api_key_value(raw_key, provider=provider), ""


def fetch_remote_models(
    base_url: str,
    api_key: str = "",
    *,
    api: str = "openai-completions",
    timeout: float = 30,
    headers: dict[str, str] | None = None,
    insecure_ssl: bool = False,
    proxy: str = "",
    provider: str = "",
    _attempted_key_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch available models from provider endpoint using baseUrl + apiKey.

    Returns: { ok, models: [{id, name, ...}], endpoint, error, raw_count }
    """
    import urllib.error
    import urllib.request

    from . import core
    from . import http_client

    base = normalize_openai_base_url(base_url)
    if not base:
        return {"ok": False, "models": [], "endpoint": "", "error": "Base URL 为空", "raw_count": 0}
    scheme_error = _check_request_scheme(base)
    if scheme_error:
        return {
            "ok": False,
            "models": [],
            "endpoint": "",
            "error": scheme_error,
            "raw_count": 0,
        }

    raw_key = (api_key or "").strip()
    key_id, key, key_error = _resolve_provider_runtime_key(provider, raw_key)
    if key_error:
        return {
            "ok": False,
            "models": [],
            "endpoint": "",
            "error": key_error,
            "raw_count": 0,
        }
    api = (api or "openai-completions").lower()

    # OpenAI / Anthropic always need a key for /models
    if api in {"openai-completions", "openai-responses", "openai", "anthropic-messages", "anthropic"}:
        if not raw_key:
            return {
                "ok": False,
                "models": [],
                "endpoint": "",
                "error": (
                    "未填写 API Key。\n"
                    "请粘贴真实密钥（如 sk-...），或填写已配置的环境变量名（如 OPENAI_API_KEY）。\n"
                    "空 Key 会返回 HTTP 401：Missing bearer authentication。"
                ),
                "raw_count": 0,
            }
        if not key:
            return {
                "ok": False,
                "models": [],
                "endpoint": "",
                "error": (
                    f"环境变量「{raw_key}」未设置或为空。\n"
                    "请先在系统/用户环境变量中配置，或直接粘贴 API Key 本身。"
                ),
                "raw_count": 0,
            }

    # Build endpoint
    if api in {"openai-completions", "openai-responses", "openai"}:
        endpoint = base + ("/models" if base.endswith("/v1") or base.endswith("/v1beta") else "/v1/models")
        if base.endswith("/models"):
            endpoint = base
        req_headers = {
            "Accept": "application/json",
            "User-Agent": core.DEFAULT_OPENAI_COMPAT_USER_AGENT,
        }
        if key:
            req_headers["Authorization"] = f"Bearer {key}"
    elif api in {"anthropic-messages", "anthropic"}:
        if base.endswith("/v1"):
            endpoint = base + "/models"
        elif base.endswith("/models"):
            endpoint = base
        else:
            endpoint = base.rstrip("/") + "/v1/models"
        req_headers = {
            "Accept": "application/json",
            "User-Agent": core.DEFAULT_OPENAI_COMPAT_USER_AGENT,
            "anthropic-version": "2023-06-01",
        }
        if key:
            req_headers["x-api-key"] = key
    elif api in {"google-generative-ai", "google"}:
        if "key=" in base:
            endpoint = base
        else:
            root = base.rstrip("/")
            if not root.endswith("/models"):
                endpoint = root + "/models"
            else:
                endpoint = root
        req_headers = {"Accept": "application/json", "User-Agent": core.DEFAULT_OPENAI_COMPAT_USER_AGENT}
        # Google 支持以 x-goog-api-key 头部传递密钥，避免泄露到 URL/代理日志
        if key and "key=" not in endpoint:
            req_headers["x-goog-api-key"] = key
        if not key and "key=" not in endpoint:
            return {
                "ok": False,
                "models": [],
                "endpoint": redact_endpoint_url(endpoint),
                "error": "Google 接口需要 API Key（查询参数 key=...）。",
                "raw_count": 0,
            }
    else:
        endpoint = base + ("/models" if base.endswith("/v1") else "/v1/models")
        req_headers = {"Accept": "application/json", "User-Agent": core.DEFAULT_OPENAI_COMPAT_USER_AGENT}
        if key:
            req_headers["Authorization"] = f"Bearer {key}"

    public_endpoint = redact_endpoint_url(endpoint)

    if headers:
        from . import secrets as secretstore

        for k, v in headers.items():
            req_headers[k] = secretstore.resolve_provider_header_value(
                provider, str(k), str(v)
            )

    proxy = core._effective_proxy_url(proxy)

    handlers: list[Any] = []
    if proxy:
        handlers.append(
            urllib.request.ProxyHandler(
                {
                    "http": proxy,
                    "https": proxy,
                }
            )
        )
    else:
        # 与 ``_http_json_request`` 一致：显式空 ProxyHandler，避免 urllib
        # 再去读环境变量或 Windows 系统代理，把本机 HTTP 桩变成 502。
        handlers.append(urllib.request.ProxyHandler({}))
    # Provider credentials must never be replayed by urllib across redirects.
    handlers.append(http_client.DenyRedirectHandler())
    handlers.append(urllib.request.HTTPSHandler(context=_ssl_context(insecure_ssl)))
    opener = urllib.request.build_opener(*handlers)

    body = ""
    status = 200
    max_attempts = core.TRANSIENT_HTTP_MAX_ATTEMPTS
    for attempt in range(max_attempts):
        try:
            req = urllib.request.Request(endpoint, headers=req_headers, method="GET")
            with opener.open(req, timeout=timeout) as resp:
                body = http_client.read_limited(
                    resp, http_client.MODEL_LIST_MAX_BYTES
                ).decode("utf-8", errors="replace")
                status = getattr(resp, "status", 200)
            break
        except urllib.error.HTTPError as e:
            err_body = ""
            try:
                err_body = http_client.read_limited(
                    e, http_client.ERROR_MAX_BYTES
                ).decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            err_body = redact_secret_values(err_body, [key])
            err_body = re.sub(
                r"(?i)(authorization\s*:\s*bearer\s+)[^\s,;]+", r"\1***", err_body
            )
            detail = f"HTTP {e.code}: {e.reason}"
            if err_body:
                detail += f"\n{err_body}"
            attempted = set(_attempted_key_ids or ())
            if key_id and key_id not in attempted and core.is_provider_key_error(
                1, detail, ""
            ):
                from . import secrets as secretstore

                attempted.add(key_id)
                secretstore.mark_provider_key_failed(
                    provider, key_id, core.provider_key_failure_reason(1, detail, "")
                )
                next_credential = secretstore.get_active_provider_credential(provider)
                if next_credential and next_credential["key_id"] not in attempted:
                    return fetch_remote_models(
                        base_url,
                        api_key,
                        api=api,
                        timeout=timeout,
                        headers=headers,
                        insecure_ssl=insecure_ssl,
                        proxy=proxy,
                        provider=provider,
                        _attempted_key_ids=attempted,
                    )
            retry_after = ""
            headers_obj = getattr(e, "headers", None)
            if headers_obj is not None:
                try:
                    retry_after = str(headers_obj.get("Retry-After") or "")
                except Exception:
                    retry_after = ""
            if is_transient_http_status(e.code) and attempt < max_attempts - 1:
                core.sleep_transient_retry(
                    transient_retry_delay(attempt, retry_after)
                )
                continue
            friendly = _friendly_fetch_error(Exception(detail), endpoint)
            if attempt > 0:
                friendly += "\n（已自动重试，上游仍过载或不可用）"
            return {
                "ok": False,
                "models": [],
                "endpoint": public_endpoint,
                "error": friendly,
                "raw_count": 0,
                "http_status": e.code,
                "proxy": proxy or "",
            }
        except Exception as e:
            return {
                "ok": False,
                "models": [],
                "endpoint": public_endpoint,
                "error": _friendly_fetch_error(e, endpoint),
                "raw_count": 0,
                "proxy": proxy or "",
            }

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "models": [],
            "endpoint": public_endpoint,
            "error": f"响应不是 JSON: {body[:200]}",
            "raw_count": 0,
        }

    models: list[dict[str, Any]] = []

    def _to_int(value: Any) -> int | None:
        """Coerce provider-supplied numeric fields without crashing.

        Providers occasionally return non-numeric strings such as "128K",
        "unknown", or "unlimited" for context_window / max_tokens. A bare
        int() would raise ValueError and abort the whole model fetch.
        """
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        if value is None:
            return None
        s = str(value).strip()
        if not s:
            return None
        mult = 1
        upper = s.upper()
        for suffix, factor in (("K", 1024), ("M", 1024 * 1024), ("G", 1024 ** 3)):
            if upper.endswith(suffix):
                mult = factor
                s = s[:-1]
                break
        try:
            return int(float(s) * mult)
        except (TypeError, ValueError):
            return None

    def add_model(mid: str, name: str | None = None, extra: dict | None = None):
        if not mid:
            return
        item = {
            "id": mid,
            "name": name or mid,
            "reasoning": True,
            "input": ["text"],
            "contextWindow": 128000,
            "maxTokens": 32768,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        }
        if extra:
            # common optional fields — providers may return non-numeric
            # strings (e.g. "128K", "unknown"), so coerce defensively.
            for src_key, dst_key in (
                ("context_window", "contextWindow"),
                ("contextWindow", "contextWindow"),
                ("max_tokens", "maxTokens"),
                ("maxTokens", "maxTokens"),
            ):
                if src_key in extra:
                    coerced = _to_int(extra[src_key])
                    if coerced is not None:
                        item[dst_key] = coerced
        models.append(core.fill_model_defaults(item))

    # OpenAI style: { data: [ {id} ] }
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        for m in data["data"]:
            if isinstance(m, dict):
                add_model(str(m.get("id") or m.get("name") or ""), str(m.get("id") or m.get("name") or ""), m)
            elif isinstance(m, str):
                add_model(m)
    # Anthropic style sometimes { data: [...] } same
    # Google: { models: [ { name: "models/xxx", displayName } ] }
    elif isinstance(data, dict) and isinstance(data.get("models"), list):
        for m in data["models"]:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "")
            mid = name.split("/")[-1] if name else str(m.get("displayName") or "")
            display = str(m.get("displayName") or mid)
            add_model(mid, display, m)
    # plain list
    elif isinstance(data, list):
        for m in data:
            if isinstance(m, dict):
                add_model(str(m.get("id") or m.get("name") or ""), None, m)
            elif isinstance(m, str):
                add_model(m)
    else:
        return {
            "ok": False,
            "models": [],
            "endpoint": public_endpoint,
            "error": f"无法识别模型列表结构，keys={list(data.keys()) if isinstance(data, dict) else type(data)}",
            "raw_count": 0,
        }

    # de-dupe by id
    seen = set()
    uniq = []
    for m in models:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        uniq.append(m)

    return {
        "ok": True,
        "models": uniq,
        "endpoint": public_endpoint,
        "error": "",
        "raw_count": len(uniq),
        "http_status": status,
    }


# ==== HTTP 连通性测试与模型可用性测试 ====


def _http_json_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 45,
    insecure_ssl: bool = False,
    proxy: str = "",
) -> dict[str, Any]:
    """Low-level HTTP helper with latency measurement.

    When a proxy is in use and the request fails at the network layer (no
    server response — e.g. a configured-but-stopped Clash), the request is
    retried once without the proxy so an inactive proxy does not turn into a
    bogus "unavailable" test result.
    """
    import time
    import urllib.error
    import urllib.request

    from . import core
    from . import http_client

    scheme_error = _check_request_scheme(url)
    if scheme_error:
        return {
            "ok": False,
            "status": 0,
            "body": "",
            "latency_ms": 0,
            "bytes": 0,
            "proxy": redact_endpoint_url((proxy or "").strip()),
            "error": scheme_error,
        }

    req_headers = dict(headers or {})
    proxy = core._effective_proxy_url(proxy)

    def request_once(active_proxy: str) -> dict[str, Any]:
        handlers: list[Any] = []
        if active_proxy:
            handlers.append(
                urllib.request.ProxyHandler({"http": active_proxy, "https": active_proxy})
            )
        else:
            # Explicit no-proxy opener (urllib would otherwise honor env vars).
            handlers.append(urllib.request.ProxyHandler({}))
        handlers.append(http_client.DenyRedirectHandler())
        handlers.append(
            urllib.request.HTTPSHandler(context=_ssl_context(insecure_ssl))
        )
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
        t0 = time.perf_counter()
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = http_client.read_limited(resp, http_client.MODEL_TEST_MAX_BYTES)
                t1 = time.perf_counter()
                status = getattr(resp, "status", 200)
                text = raw.decode("utf-8", errors="replace")
                return {
                    "ok": 200 <= int(status) < 300,
                    "status": int(status),
                    "body": text,
                    "latency_ms": round((t1 - t0) * 1000, 1),
                    "bytes": len(raw),
                    "proxy": redact_endpoint_url(active_proxy),
                    "error": "",
                }
        except urllib.error.HTTPError as e:
            t1 = time.perf_counter()
            err_body = ""
            try:
                err_body = http_client.read_limited(
                    e, http_client.ERROR_MAX_BYTES
                ).decode("utf-8", errors="replace")[:800]
            except Exception:
                pass
            return {
                "ok": False,
                "status": int(getattr(e, "code", 0) or 0),
                "body": err_body,
                "latency_ms": round((t1 - t0) * 1000, 1),
                "bytes": len(err_body.encode("utf-8", errors="ignore")),
                "proxy": redact_endpoint_url(active_proxy),
                "error": f"HTTP {e.code}: {e.reason}",
            }
        except Exception as e:
            t1 = time.perf_counter()
            return {
                "ok": False,
                "status": 0,
                "body": "",
                "latency_ms": round((t1 - t0) * 1000, 1),
                "bytes": 0,
                "proxy": redact_endpoint_url(active_proxy),
                "error": _friendly_fetch_error(e, url),
            }

    result = request_once(proxy)
    if not result.get("ok") and result.get("status") == 0 and proxy:
        # Network-layer failure through the proxy (e.g. stopped Clash):
        # retry directly once so the test does not report bogus unavailability.
        result = request_once("")
    max_attempts = core.TRANSIENT_HTTP_MAX_ATTEMPTS
    attempt = 0
    while (
        not result.get("ok")
        and is_transient_http_status(result.get("status"))
        and attempt < max_attempts - 1
    ):
        core.sleep_transient_retry(transient_retry_delay(attempt))
        attempt += 1
        result = request_once(proxy)
        if not result.get("ok") and result.get("status") == 0 and proxy:
            result = request_once("")
    return result


def _extract_reply_preview(api: str, body_text: str, limit: int = 120) -> str:
    try:
        data = json.loads(body_text or "{}")
    except Exception:
        return (body_text or "")[:limit]

    api = (api or "").lower()
    try:
        if api in {"openai-completions", "openai", "openai-responses"} or "choices" in data:
            # chat completions
            choices = data.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("text"):
                            parts.append(str(c["text"]))
                        elif isinstance(c, str):
                            parts.append(c)
                    content = "".join(parts)
                if content:
                    return str(content).strip()[:limit]
                if choices[0].get("text"):
                    return str(choices[0]["text"]).strip()[:limit]
            # responses API
            if data.get("output_text"):
                return str(data["output_text"]).strip()[:limit]
            output = data.get("output") or []
            for item in output:
                if not isinstance(item, dict):
                    continue
                for c in item.get("content") or []:
                    if isinstance(c, dict) and c.get("text"):
                        return str(c["text"]).strip()[:limit]
        if api in {"anthropic-messages", "anthropic"}:
            content = data.get("content") or []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    return str(c.get("text") or "").strip()[:limit]
                if isinstance(c, dict) and c.get("text"):
                    return str(c.get("text") or "").strip()[:limit]
        if api in {"google-generative-ai", "google"}:
            cands = data.get("candidates") or []
            if cands:
                parts = cands[0].get("content", {}).get("parts") or []
                texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict)]
                joined = "".join(texts).strip()
                if joined:
                    return joined[:limit]
    except Exception:
        pass
    return (body_text or "")[:limit]


def test_model_http(
    provider: str,
    model: str,
    *,
    timeout: float = 45,
    insecure_ssl: bool = False,
    proxy: str = "",
    prompt: str = "Reply with exactly: OK",
    _attempted_key_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Test model via provider BaseURL HTTP (custom providers in models.json)."""
    from . import core
    from . import secrets as secretstore

    entry = core.get_provider_config(provider)
    if not entry:
        return {
            "ok": False,
            "available": False,
            "mode": "http",
            "provider": provider,
            "model": model,
            "latency_ms": None,
            "error": f"models.json 中没有自定义 provider「{provider}」，无法走 HTTP 直连测试。可改用 Pi 实测。",
            "preview": "",
            "endpoint": "",
            "http_status": 0,
        }

    base = normalize_openai_base_url(str(entry.get("baseUrl") or ""))
    api = str(entry.get("api") or "openai-completions").lower()
    raw_key = str(entry.get("apiKey") or "").strip()

    key_id, key, key_error = _resolve_provider_runtime_key(provider, raw_key)
    if key_error:
        return {
            "ok": False,
            "available": False,
            "mode": "http",
            "provider": provider,
            "model": model,
            "latency_ms": None,
            "error": key_error,
            "preview": "",
            "endpoint": base,
            "http_status": 0,
        }
    extra_headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}

    if not base:
        return {
            "ok": False,
            "available": False,
            "mode": "http",
            "provider": provider,
            "model": model,
            "latency_ms": None,
            "error": "provider 缺少 baseUrl",
            "preview": "",
            "endpoint": "",
            "http_status": 0,
        }

    scheme_error = _check_request_scheme(base)
    if scheme_error:
        return {
            "ok": False,
            "available": False,
            "mode": "http",
            "provider": provider,
            "model": model,
            "latency_ms": None,
            "error": scheme_error,
            "preview": "",
            "endpoint": base,
            "http_status": 0,
        }

    effective_headers = core._openai_compat_headers(api, extra_headers)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    if not any(str(key).strip().lower() == "user-agent" for key in effective_headers):
        headers["User-Agent"] = core.DEFAULT_OPENAI_COMPAT_USER_AGENT
    for k, v in effective_headers.items():
        headers[str(k)] = secretstore.resolve_provider_header_value(
            provider, str(k), str(v)
        )

    body_obj: dict[str, Any]
    if api in {"openai-completions", "openai"}:
        endpoint = base + ("/chat/completions" if not base.endswith("/chat/completions") else "")
        if base.endswith("/v1") or "/v1/" in base or base.endswith("/v1beta"):
            endpoint = base.rstrip("/") + "/chat/completions"
        headers["Authorization"] = f"Bearer {key}" if key else headers.get("Authorization", "")
        if not key and "Authorization" not in (extra_headers or {}):
            return {
                "ok": False,
                "available": False,
                "mode": "http",
                "provider": provider,
                "model": model,
                "latency_ms": None,
                "error": "缺少 API Key，无法 HTTP 测试",
                "preview": "",
                "endpoint": endpoint,
                "http_status": 0,
            }
        body_obj = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "temperature": 0,
        }
    elif api in {"openai-responses"}:
        endpoint = base.rstrip("/") + "/responses"
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body_obj = {
            "model": model,
            "input": prompt,
            "max_output_tokens": 16,
        }
    elif api in {"anthropic-messages", "anthropic"}:
        endpoint = base.rstrip("/") + "/messages"
        if base.endswith("/messages"):
            endpoint = base
        if key:
            headers["x-api-key"] = key
        headers["anthropic-version"] = headers.get("anthropic-version") or "2023-06-01"
        body_obj = {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif api in {"google-generative-ai", "google"}:
        # generateContent
        root = base.rstrip("/")
        if root.endswith("/models"):
            endpoint = f"{root}/{model}:generateContent"
        else:
            endpoint = f"{root}/models/{model}:generateContent"
        # 优先以 x-goog-api-key 头部传递密钥，避免泄露到 URL/代理日志；
        # 若用户直接传入带 key= 的 URL 则保留原样
        if key and "key=" not in endpoint:
            headers["x-goog-api-key"] = key
        body_obj = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 16, "temperature": 0},
        }
    else:
        endpoint = base.rstrip("/") + "/chat/completions"
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body_obj = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "temperature": 0,
        }

    payload = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    public_endpoint = redact_endpoint_url(endpoint)
    # 走 core._http_json_request 动态查找，使测试 monkeypatch core._http_json_request 生效。
    result = core._http_json_request(
        endpoint,
        method="POST",
        headers=headers,
        body=payload,
        timeout=timeout,
        insecure_ssl=insecure_ssl,
        proxy=proxy,
    )
    attempted = set(_attempted_key_ids or ())
    failure_text = f"HTTP {result.get('status') or 0}\n{result.get('error') or ''}\n{result.get('body') or ''}"
    if (
        not result.get("ok")
        and key_id
        and key_id not in attempted
        and core.is_provider_key_error(1, failure_text, "")
    ):
        attempted.add(key_id)
        secretstore.mark_provider_key_failed(
            provider, key_id, core.provider_key_failure_reason(1, failure_text, "")
        )
        next_credential = secretstore.get_active_provider_credential(provider)
        if next_credential and next_credential["key_id"] not in attempted:
            return test_model_http(
                provider,
                model,
                timeout=timeout,
                insecure_ssl=insecure_ssl,
                proxy=proxy,
                prompt=prompt,
                _attempted_key_ids=attempted,
            )
    secret_values = [key, *[str(value) for value in headers.values()]]
    result["body"] = redact_secret_values(str(result.get("body") or ""), secret_values)
    result["error"] = redact_secret_values(str(result.get("error") or ""), secret_values)
    preview = _extract_reply_preview(api, result.get("body") or "") if result.get("ok") else (result.get("body") or "")[:160]
    available = bool(result.get("ok"))
    err = result.get("error") or ""
    if not available and result.get("body"):
        err = (err + "\n" + str(result.get("body"))[:400]).strip()

    return {
        "ok": available,
        "available": available,
        "mode": "http",
        "provider": provider,
        "model": model,
        "latency_ms": result.get("latency_ms"),
        "error": err if not available else "",
        "preview": preview,
        "endpoint": public_endpoint,
        "http_status": result.get("status") or 0,
        "proxy": result.get("proxy") or "",
        "api": api,
    }


def test_model_via_pi(
    provider: str,
    model: str,
    *,
    timeout: float = 90,
    prompt: str = "只回复两个字符：OK",
    workdir: str | None = None,
) -> dict[str, Any]:
    """Test model availability via official pi -p (covers OAuth/built-in providers)."""
    import time
    from . import core

    t0 = time.perf_counter()
    try:
        code, out, err = core.run_pi_print(
            prompt,
            workdir=workdir or str(core.user_home()),
            provider=provider,
            model=model,
            thinking="off",
            timeout=timeout,
        )
        t1 = time.perf_counter()
    except Exception as e:
        t1 = time.perf_counter()
        return {
            "ok": False,
            "available": False,
            "mode": "pi",
            "provider": provider,
            "model": model,
            "latency_ms": round((t1 - t0) * 1000, 1),
            "error": str(e),
            "preview": "",
            "endpoint": "pi -p",
            "http_status": 0,
            "returncode": -1,
        }

    text = (out or "").strip()
    err_text = (err or "").strip()
    # Consider available if exit 0 and some non-empty model output
    available = code == 0 and bool(text)
    # Some pi versions write assistant text only to stdout
    if code == 0 and not text and err_text and "error" not in err_text.lower():
        text = err_text
        available = True

    combined_err = ""
    if not available:
        combined_err = err_text or text or f"pi 退出码 {code}"
        low = combined_err.lower()
        if "auth" in low or "login" in low or "api key" in low or "unauthorized" in low:
            combined_err += "\n提示：该 provider 可能未登录/未配置 API Key。"

    return {
        "ok": available,
        "available": available,
        "mode": "pi",
        "provider": provider,
        "model": model,
        "latency_ms": round((t1 - t0) * 1000, 1),
        "error": combined_err if not available else "",
        "preview": (text[:160] if text else ""),
        "endpoint": "pi -p --no-session --approve",
        "http_status": 0,
        "returncode": code,
        "stderr": err_text[:300],
    }


def test_model(
    provider: str,
    model: str,
    *,
    mode: str = "auto",
    timeout: float = 60,
    insecure_ssl: bool = False,
    proxy: str = "",
    workdir: str | None = None,
) -> dict[str, Any]:
    """Test one model. mode: auto|http|pi

    auto: custom provider with baseUrl -> HTTP first; on failure also try pi if installed.
          otherwise pi only.
    """
    from . import core

    mode = (mode or "auto").lower().strip()
    entry = core.get_provider_config(provider)

    if mode == "http":
        return core.test_model_http(
            provider, model, timeout=timeout, insecure_ssl=insecure_ssl, proxy=proxy
        )
    if mode == "pi":
        return core.test_model_via_pi(provider, model, timeout=timeout, workdir=workdir)

    # auto
    if entry and entry.get("baseUrl"):
        http_res = core.test_model_http(
            provider, model, timeout=min(timeout, 45), insecure_ssl=insecure_ssl, proxy=proxy
        )
        if http_res.get("available"):
            http_res["note"] = "HTTP 直连成功"
            return http_res
        # fallback to pi for better diagnosis / oauth hybrids
        if core.find_pi_command():
            pi_res = core.test_model_via_pi(
                provider, model, timeout=timeout, workdir=workdir
            )
            pi_res["http_fallback"] = http_res
            if pi_res.get("available"):
                pi_res["note"] = "HTTP 失败但 Pi 实测成功"
            else:
                pi_res["note"] = "HTTP 与 Pi 均失败"
                # prefer richer error
                if http_res.get("error") and not pi_res.get("error"):
                    pi_res["error"] = http_res.get("error")
                elif http_res.get("error"):
                    pi_res["error"] = (
                        f"[HTTP] {http_res.get('error')}\n[Pi] {pi_res.get('error')}"
                    )
            return pi_res
        return http_res

    return core.test_model_via_pi(provider, model, timeout=timeout, workdir=workdir)


def format_test_summary(result: dict[str, Any]) -> str:
    """Human-readable one-line summary for table/status."""
    if result.get("available"):
        lat = result.get("latency_ms")
        lat_s = f"{lat:.0f} ms" if isinstance(lat, (int, float)) else "?"
        mode = result.get("mode") or ""
        return f"可用 · {lat_s} · {mode}"
    err = (result.get("error") or "失败").splitlines()[0][:80]
    return f"不可用 · {err}"
