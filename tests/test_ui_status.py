from __future__ import annotations

from datetime import datetime

from pi_manager.presentation.status import (
    classify_test_result,
    format_capability,
    format_context_length,
    format_relative_time,
    summarize_error,
)


def test_summarize_error_maps_common_failures():
    assert summarize_error("Missing bearer token") == "API Key 未配置"
    assert "401" in summarize_error("HTTP 401 unauthorized")
    assert summarize_error("model_not_found: xyz") == "模型不存在"
    assert summarize_error("Read timed out") == "请求超时"
    assert summarize_error("connection refused") == "网络不可达"


def test_classify_test_result_uses_fixed_tones():
    assert classify_test_result(None).tone == "warning"
    assert classify_test_result({}).tone == "warning"
    assert classify_test_result({"pending": True}).tone == "info"
    assert classify_test_result({"available": True, "latency_ms": 120}).tone == "success"
    failed = classify_test_result({"available": False, "error": "HTTP 401 invalid api key"})
    assert failed.tone == "danger"
    assert failed.reason == "API 返回 401：认证失败"


def test_format_capability_and_context():
    assert format_capability("yes") == "支持"
    assert format_capability("no") == "不支持"
    assert format_context_length("272000") == "272K"
    assert format_context_length("") == "—"


def test_format_relative_time_buckets():
    now = datetime(2026, 9, 2, 18, 0, 0)
    assert format_relative_time(datetime(2026, 9, 2, 17, 58, 0), now=now) == "2 分钟前"
    assert format_relative_time(None) == "从未测试"
