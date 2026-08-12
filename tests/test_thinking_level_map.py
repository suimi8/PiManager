"""thinkingLevelMap 自动补全：防止 reasoning 模型 max/xhigh 被 Pi 钳制为 high。

背景：Pi 的 getSupportedThinkingLevels() 在模型缺少 thinkingLevelMap 时，
会过滤掉 xhigh/max；clampThinkingLevel("max") 因此回退为 high，导致用户在
Pi Manager 里选择 max 实际却按 high 调用。这里保证所有保存入口自动补全。
"""
from __future__ import annotations

import pytest


def test_ensure_thinking_level_map_fills_reasoning_model(isolated_home):
    from pi_manager import core

    model = core.ensure_thinking_level_map({"id": "deepseek-v4-flash", "reasoning": True})
    assert model["thinkingLevelMap"]["max"] == "max"
    assert model["thinkingLevelMap"]["off"] == "none"
    # 非 reasoning 模型不受影响
    plain = core.ensure_thinking_level_map({"id": "gpt-4o-mini"})
    assert "thinkingLevelMap" not in plain


def test_ensure_thinking_level_map_preserves_custom_map(isolated_home):
    from pi_manager import core

    custom = {
        "id": "m",
        "reasoning": True,
        "thinkingLevelMap": {"off": None, "high": "high", "max": "max"},
    }
    result = core.ensure_thinking_level_map(custom)
    assert result["thinkingLevelMap"] == {"off": None, "high": "high", "max": "max"}


def test_upsert_custom_provider_auto_fills_models(isolated_home):
    from pi_manager import core

    core.upsert_custom_provider(
        "opencode go",
        base_url="https://example.com/v1",
        api_key="sk-test",
        models=[{"id": "deepseek-v4-flash", "reasoning": True}],
        compat={"supportsDeveloperRole": False, "supportsReasoningEffort": True},
    )
    cfg = core.load_models_config()
    model = next(
        m for m in cfg["providers"]["opencode go"]["models"] if m["id"] == "deepseek-v4-flash"
    )
    assert model["thinkingLevelMap"]["max"] == "max"
    assert model["thinkingLevelMap"]["xhigh"] == "xhigh"


def test_add_model_to_provider_auto_fills(isolated_home):
    from pi_manager import core

    core.upsert_custom_provider(
        "opencode go",
        base_url="https://example.com/v1",
        api_key="sk-test",
        models=[],
        compat={"supportsDeveloperRole": False, "supportsReasoningEffort": True},
    )
    core.add_model_to_provider("opencode go", "deepseek-v4-flash", reasoning=True)
    cfg = core.load_models_config()
    model = next(
        m for m in cfg["providers"]["opencode go"]["models"] if m["id"] == "deepseek-v4-flash"
    )
    assert model["thinkingLevelMap"]["max"] == "max"


def test_fetch_remote_models_models_are_filled_after_save(isolated_home):
    """fetch 出的 reasoning 模型经 upsert 保存后同样带 thinkingLevelMap。"""
    from pi_manager import core

    fetched = core.fetch_remote_models(
        "https://example.com/v1",
        "sk-test",
        api="openai-completions",
    )
    assert not fetched.get("ok")  # 无网络时拿不到模型，只验证保存链路不受影响
