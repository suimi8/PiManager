"""Security policy primitives shared by Pi Manager HTTP callers."""
from __future__ import annotations

from typing import Any
from urllib import request


MODEL_LIST_MAX_BYTES = 4 * 1024 * 1024
MODEL_TEST_MAX_BYTES = 2 * 1024 * 1024
ERROR_MAX_BYTES = 64 * 1024
MANIFEST_MAX_BYTES = 1024 * 1024


class ResponseTooLargeError(ValueError):
    """Raised when an HTTP body exceeds its configured byte budget."""


class DenyRedirectHandler(request.HTTPRedirectHandler):
    """Return redirect responses to the caller without following Location."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def content_length(response: Any) -> int | None:
    try:
        value = response.headers.get("Content-Length")
        return int(value) if value is not None else None
    except (AttributeError, TypeError, ValueError):
        return None


def read_limited(response: Any, max_bytes: int) -> bytes:
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    declared = content_length(response)
    if declared is not None and declared > max_bytes:
        raise ResponseTooLargeError(f"HTTP 响应超过 {max_bytes} 字节限制")

    chunks: list[bytes] = []
    total = 0
    while True:
        try:
            chunk = response.read(min(64 * 1024, max_bytes - total + 1))
        except TypeError:
            # Lightweight test doubles and some file-like adapters expose read()
            # without a size parameter, which denotes a one-shot full read.
            chunk = response.read()
            total += len(chunk)
            if total > max_bytes:
                raise ResponseTooLargeError(f"HTTP 响应超过 {max_bytes} 字节限制")
            chunks.append(chunk)
            break
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ResponseTooLargeError(f"HTTP 响应超过 {max_bytes} 字节限制")
        chunks.append(chunk)
    return b"".join(chunks)
