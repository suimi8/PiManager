# -*- coding: utf-8 -*-
"""HTTP 工具：URL 规范化 / SSL 上下文 / 端点脱敏 / 友好错误。

从 ``core.py`` 抽出的纯 HTTP 工具函数，无配置/状态依赖，可独立测试。
``core.py`` 在顶部重新导出这些符号以保持 ``core.xxx`` 调用兼容。
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)


def normalize_openai_base_url(base_url: str) -> str:
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return ""
    # If user passed full chat completions path, strip to /v1 root-ish
    for suffix in ("/chat/completions", "/completions", "/responses", "/messages"):
        if u.lower().endswith(suffix):
            u = u[: -len(suffix)]
            break
    return u.rstrip("/")


def _ssl_context(insecure: bool = False):
    import ssl

    if insecure:
        logger.warning("SSL 证书校验已被用户显式禁用（insecure_ssl=True），存在中间人攻击风险")
        ctx = ssl._create_unverified_context()
        return ctx
    cafile = None
    try:
        import certifi

        cafile = certifi.where()
    except Exception as exc:
        logger.warning("加载 certifi 证书失败，回退系统默认证书: %s", exc)
        cafile = None
    if cafile and os.path.isfile(cafile):
        ctx = ssl.create_default_context(cafile=cafile)
    else:
        ctx = ssl.create_default_context()
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except Exception as exc:
        logger.warning("设置最低 TLS 版本失败: %s", exc)
    return ctx


def redact_endpoint_url(url: str) -> str:
    """Redact URL userinfo and credential-like query parameters."""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    raw = str(url or "")
    if not raw:
        return raw
    try:
        parts = urlsplit(raw)
        host = parts.hostname or ""
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        netloc = host + (f":{parts.port}" if parts.port is not None else "")
        query = parse_qsl(parts.query, keep_blank_values=True)
        redacted: list[tuple[str, str]] = []
        for name, value in query:
            normalized = name.lower().replace("-", "").replace("_", "")
            sensitive = normalized == "key" or normalized.endswith(
                ("apikey", "token", "secret", "password")
            )
            redacted.append((name, "***" if sensitive else value))
        safe_query = urlencode(redacted, doseq=True, safe="*")
        return urlunsplit(
            (parts.scheme, netloc, parts.path, safe_query, parts.fragment)
        )
    except (TypeError, ValueError):
        return raw


def redact_secret_values(text: str, secret_values: list[str]) -> str:
    result = str(text or "")
    # 过滤掉空值与长度小于 4 的短密钥，避免误伤无关文本
    unique = {
        str(item) for item in secret_values if item and len(str(item)) >= 4
    }
    if not unique:
        return result
    # 按长度降序构造正则，避免短密钥替换掉长密钥中的残片
    pattern = "|".join(re.escape(secret) for secret in sorted(unique, key=len, reverse=True))
    return re.sub(pattern, "***", result)


def _friendly_fetch_error(exc: BaseException, endpoint: str = "") -> str:
    safe_endpoint = redact_endpoint_url(endpoint)
    msg = str(exc)
    if endpoint and endpoint != safe_endpoint:
        msg = msg.replace(endpoint, safe_endpoint)
    low = msg.lower()
    tips: list[str] = []
    if "missing bearer" in low or "unauthorized" in low or "401" in low:
        tips.append("未带上有效 API Key，或 Key 无效/已过期。")
        tips.append("请在「API Key」填入 sk-... 真实密钥，或已存在的环境变量名（如 OPENAI_API_KEY）。")
    if "ssl" in low or "eof occurred" in low or "certificate" in low or "wrong version number" in low:
        tips.append("TLS/SSL 握手失败：常见于网络拦截、公司代理、或直连 api.openai.com 不稳定。")
        tips.append("可尝试：1) 设置系统/用户环境变量 HTTPS_PROXY；2) 改用可访问的中转 Base URL；3) 勾选「忽略 SSL 校验」仅作排查。")
    if "timed out" in low or "timeout" in low:
        tips.append("请求超时：检查网络、代理或 Base URL 是否可达。")
    if "name or service not known" in low or "getaddrinfo failed" in low or "nodename nor servname" in low:
        tips.append("域名解析失败：检查 DNS / 是否需要代理。")
    if "10061" in low or "connection refused" in low:
        tips.append("连接被拒绝：代理地址错误或目标服务未开放。")
    if "proxy" in low:
        tips.append("代理相关错误：检查 HTTP_PROXY / HTTPS_PROXY。")

    header = f"{type(exc).__name__}: {msg}"
    if tips:
        return header + "\n\n排查建议：\n- " + "\n- ".join(tips)
    if safe_endpoint:
        return header + f"\nendpoint: {safe_endpoint}"
    return header
