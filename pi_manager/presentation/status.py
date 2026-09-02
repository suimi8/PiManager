"""连接 / 测试状态的统一语义，供仪表盘、模型详情与表格共用。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

# 固定色板语义：success 可用、warning 未测/不完整、danger 失败、
# info 进行中、neutral 未选择/禁用。
TONE_SUCCESS = "success"
TONE_WARNING = "warning"
TONE_DANGER = "danger"
TONE_INFO = "info"
TONE_NEUTRAL = "neutral"


@dataclass(frozen=True, slots=True)
class StatusView:
    tone: str
    label: str
    reason: str = ""
    detail: str = ""


def summarize_error(error: str) -> str:
    """把原始错误压成用户可执行的一句话。"""
    text = str(error or "").strip()
    if not text:
        return "未知错误"
    low = text.lower()
    if any(
        token in low
        for token in (
            "missing bearer",
            "missing api key",
            "api key not",
            "no api key",
            "empty api key",
        )
    ):
        return "API Key 未配置"
    if any(
        token in low
        for token in ("401", "unauthorized", "invalid api", "invalid_api", "authentication")
    ):
        return "API 返回 401：认证失败"
    if any(
        token in low
        for token in ("403", "forbidden", "permission denied")
    ):
        return "没有访问该模型的权限"
    if any(
        token in low
        for token in ("404", "not found", "model_not_found", "does not exist", "unknown model")
    ):
        return "模型不存在"
    if "429" in low or "rate limit" in low or "too many requests" in low:
        return "请求过于频繁"
    if any(
        token in low
        for token in ("timeout", "timed out", "deadline exceeded", "timedout")
    ):
        return "请求超时"
    if any(
        token in low
        for token in (
            "connection refused",
            "connection reset",
            "network",
            "dns",
            "unreachable",
            "nameresolution",
        )
    ):
        return "网络不可达"
    first = text.splitlines()[0].strip()
    return first[:80] if first else "未知错误"


def classify_test_result(result: dict[str, Any] | None) -> StatusView:
    """把内存中的测试记录映射成状态色、短标签和失败原因。"""
    if not result:
        return StatusView(
            TONE_WARNING,
            "尚未测试",
            "还没有测过这条连接",
            "测试后才能确认当前是否可用",
        )
    if result.get("pending"):
        return StatusView(TONE_INFO, "测试中", "正在检查连接")
    available = result.get("available")
    ok = result.get("ok")
    if available is True or ok is True:
        latency = result.get("latency_ms")
        extra = f"{latency:.0f} ms" if isinstance(latency, (int, float)) else ""
        return StatusView(
            TONE_SUCCESS,
            "连接正常",
            extra or "最近一次测试通过",
        )
    if available is False or ok is False:
        detail = str(result.get("error") or result.get("preview") or "").strip()
        return StatusView(
            TONE_DANGER,
            "连接失败",
            summarize_error(detail),
            detail,
        )
    return StatusView(
        TONE_WARNING,
        "尚未测试",
        "测试结果不完整",
    )


def format_capability(value: str) -> str:
    """把 yes/no/空值收成「支持 / 不支持 / —」。"""
    text = str(value or "").strip()
    if not text or text in {"-", "n/a", "NA"}:
        return "—"
    low = text.lower()
    if low in {"yes", "true", "y", "1", "supported"}:
        return "支持"
    if low in {"no", "false", "n", "0", "unsupported"}:
        return "不支持"
    return text


def format_context_length(value: str) -> str:
    """把 272000 / 272k tokens 收成 272K。"""
    text = str(value or "").strip()
    if not text:
        return "—"
    compact = (
        text.replace(",", "")
        .replace(" tokens", "")
        .replace("token", "")
        .strip()
    )
    try:
        normalized = compact.lower().replace("k", "000")
        number = int(normalized)
        if number >= 1000:
            if number % 1000 == 0:
                return f"{number // 1000}K"
            return f"{number / 1000:.1f}K".rstrip("0").rstrip(".")
        return str(number)
    except ValueError:
        return compact or "—"


def parse_history_time(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            continue
    return None


def format_relative_time(
    value: datetime | str | None,
    *,
    now: datetime | None = None,
) -> str:
    """把时间戳收成「刚刚 / 2 分钟前 / 昨天」。"""
    stamp = value if isinstance(value, datetime) else parse_history_time(value)
    if stamp is None:
        return "从未测试"
    current = now or datetime.now()
    seconds = int((current - stamp).total_seconds())
    if seconds < 0:
        return stamp.strftime("%Y-%m-%d %H:%M")
    if seconds < 15:
        return "刚刚"
    if seconds < 60:
        return f"{seconds} 秒前"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} 分钟前"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} 小时前"
    days = hours // 24
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days} 天前"
    return stamp.strftime("%Y-%m-%d %H:%M")


def latest_history_entry(
    history: list[dict[str, Any]] | None,
    *,
    provider: str = "",
    model: str = "",
) -> dict[str, Any] | None:
    rows = list(history or [])
    if provider and model:
        key_p, key_m = provider, model
        rows = [
            row
            for row in rows
            if str(row.get("provider") or "") == key_p
            and str(row.get("model") or "") == key_m
        ]
    return rows[-1] if rows else None
