# -*- coding: utf-8 -*-
"""http_client 安全原语（响应限额 + 防重定向）的直接测试。

这些原语是所有联网路径（模型列表拉取 / Provider 测试 / 更新清单）的
共同安全边界，此前零直接覆盖。测试只依赖标准库与轻量 test double，
不发起任何真实网络请求。
"""
from __future__ import annotations

from io import BytesIO
from urllib import request
from urllib.error import HTTPError
from urllib.response import addinfourl

import pytest

from pi_manager import http_client


class _FakeHeaders:
    def __init__(self, mapping: dict[str, str] | None = None):
        self._mapping = dict(mapping or {})

    def get(self, name: str, default=None):
        return self._mapping.get(name, default)


class _LimitedReader:
    """按 read(size) 语义分块返回内容的文件类对象。"""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def read(self, size: int = -1) -> bytes:
        if self._pos >= len(self._data):
            return b""
        end = len(self._data) if size is None or size < 0 else self._pos + size
        chunk = self._data[self._pos : end]
        self._pos = end
        return chunk


class _NoSizeReader:
    """暴露 read() 但不接受 size 参数（触发 TypeError 回退分支）。"""

    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


def _response(data: bytes, content_length: str | None) -> _LimitedReader:
    headers = _FakeHeaders()
    if content_length is not None:
        headers._mapping["Content-Length"] = content_length
    resp = _LimitedReader(data)
    resp.headers = headers
    return resp


# ---- Content-Length 预检 -------------------------------------------------

def test_content_length_parses_header():
    resp = _response(b"", "123")
    assert http_client.content_length(resp) == 123


def test_content_length_missing_returns_none():
    resp = _response(b"", None)
    assert http_client.content_length(resp) is None


def test_content_length_garbage_returns_none():
    resp = _response(b"", "not-a-number")
    assert http_client.content_length(resp) is None


def test_content_length_missing_headers_attribute_returns_none():
    # 某些 test double 没有 .headers
    reader = _LimitedReader(b"x")
    assert http_client.content_length(reader) is None


def test_read_limited_rejects_declared_over_limit_before_reading():
    """声明超限：预检直接拒绝，不读取 body。"""
    reader = _LimitedReader(b"x" * 100)
    reader.headers = _FakeHeaders({"Content-Length": "100"})
    with pytest.raises(http_client.ResponseTooLargeError):
        http_client.read_limited(reader, max_bytes=50)
    assert reader._pos == 0, "预检失败时不应消费 body"


def test_read_limited_accepts_declared_exactly_at_limit():
    data = b"y" * 64
    resp = _response(data, str(len(data)))
    assert http_client.read_limited(resp, max_bytes=64) == data


def test_read_limited_accepts_declared_under_limit():
    data = b"z" * 32
    resp = _response(data, str(len(data)))
    assert http_client.read_limited(resp, max_bytes=64) == data


# ---- 无长度流式累计限额 ------------------------------------------------

def test_read_limited_streams_under_limit_without_content_length():
    data = b"a" * 200_000  # 多块（每块 64KB）
    resp = _response(data, None)
    assert http_client.read_limited(resp, max_bytes=300_000) == data


def test_read_limited_streams_over_limit_raises():
    data = b"b" * 100
    resp = _response(data, None)
    with pytest.raises(http_client.ResponseTooLargeError):
        http_client.read_limited(resp, max_bytes=64)


def test_read_limited_exactly_at_limit_without_content_length():
    data = b"c" * 64
    resp = _response(data, None)
    assert http_client.read_limited(resp, max_bytes=64) == data


def test_read_limited_zero_limit_rejects_any_body():
    data = b"d"
    resp = _response(data, None)
    with pytest.raises(http_client.ResponseTooLargeError):
        http_client.read_limited(resp, max_bytes=0)


def test_read_limited_zero_limit_allows_empty_body():
    resp = _response(b"", None)
    assert http_client.read_limited(resp, max_bytes=0) == b""


def test_read_limited_negative_max_bytes_raises_value_error():
    resp = _response(b"", None)
    with pytest.raises(ValueError, match="non-negative"):
        http_client.read_limited(resp, max_bytes=-1)


# ---- TypeError 回退分支（read 不接受 size 参数的 one-shot reader） -------

def test_read_limited_typeerror_fallback_single_read():
    reader = _NoSizeReader(b"short")
    reader.headers = _FakeHeaders({"Content-Length": "5"})
    assert http_client.read_limited(reader, max_bytes=64) == b"short"


def test_read_limited_typeerror_fallback_over_limit():
    reader = _NoSizeReader(b"x" * 128)
    reader.headers = _FakeHeaders({"Content-Length": "128"})
    with pytest.raises(http_client.ResponseTooLargeError):
        http_client.read_limited(reader, max_bytes=64)


def test_read_limited_typeerror_fallback_without_content_length():
    reader = _NoSizeReader(b"y" * 8)
    reader.headers = _FakeHeaders()
    assert http_client.read_limited(reader, max_bytes=64) == b"y" * 8


# ---- DenyRedirectHandler：拒绝跟随 302（不转发凭据） ---------------------

def test_deny_redirect_handler_returns_none_for_redirect():
    """redirect_request 返回 None 表示不跟随 Location。"""
    handler = http_client.DenyRedirectHandler()
    req = request.Request("https://example.com/start")
    result = handler.redirect_request(
        req, None, 302, "Found", _FakeHeaders({"Location": "https://example.com/evil"}), "https://example.com/evil"
    )
    assert result is None


def test_deny_redirect_handler_is_an_http_redirect_handler():
    assert issubclass(http_client.DenyRedirectHandler, request.HTTPRedirectHandler)


def test_deny_redirect_handler_never_follows_via_opener():
    """端到端：302 响应原样返回给调用者，绝不带 Authorization 访问新地址。"""
    called: list[str] = []

    class FakeOpener:
        def __init__(self, handler: http_client.DenyRedirectHandler):
            self._handler = handler

        def open(self, req, timeout=None):
            called.append(req.full_url)
            fp = BytesIO(b"moved")
            resp = addinfourl(
                fp,
                {"Location": "https://example.com/evil", "Content-Length": "6"},
                req.full_url,
                code=302,
            )
            resp.msg = "Found"
            # 模拟 urllib 的 HTTPError 触发路径：redirect_request 返回 None 时抛 HTTPError
            if self._handler.redirect_request(req, fp, 302, "Found", resp.headers, "https://example.com/evil"):
                raise AssertionError("不应跟随重定向")
            raise HTTPError(req.full_url, 302, "Found", resp.headers, fp)

    handler = http_client.DenyRedirectHandler()
    opener = FakeOpener(handler)
    req = request.Request(
        "https://example.com/start",
        headers={"Authorization": "Bearer <REDACTED>"},
    )
    with pytest.raises(HTTPError) as excinfo:
        opener.open(req)
    assert excinfo.value.code == 302
    assert called == ["https://example.com/start"], "重定向目标不应被访问（凭据不转发）"


def test_response_too_large_error_is_value_error():
    # 调用方依赖 ValueError 捕获语义
    assert issubclass(http_client.ResponseTooLargeError, ValueError)


def test_error_max_bytes_is_below_manifest_budget():
    # 错误摘要限额必须远小于正常清单限额，避免错误正文撑爆内存
    assert http_client.ERROR_MAX_BYTES < http_client.MANIFEST_MAX_BYTES
    assert http_client.MANIFEST_MAX_BYTES <= 1024 * 1024


# ---- 上游瞬时故障识别 / Retry-After ---------------------------------------

def test_transient_http_helpers_distinguish_overload_from_auth():
    from pi_manager import core_http

    assert core_http.is_transient_http_status(503) is True
    assert core_http.is_transient_http_status(429) is False
    assert core_http.is_transient_http_status(401) is False
    assert core_http.is_transient_http_status("503") is True
    assert core_http.is_transient_http_status(None) is False
    assert core_http.is_transient_upstream_error(
        'HTTP 503\n{"error":{"code":"upstream_overloaded"}}'
    )
    assert not core_http.is_transient_upstream_error("HTTP 401 invalid API key")
    assert core_http.parse_retry_after_seconds("2") == 2.0
    assert core_http.parse_retry_after_seconds("Wed, 21 Oct 2015 07:28:00 GMT") is None
    assert core_http.transient_retry_delay(0, "99") == 8.0
    assert core_http.transient_retry_delay(0) == 1.0
    assert core_http.transient_retry_delay(2) == 4.0
