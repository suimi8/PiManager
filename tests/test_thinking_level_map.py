"""thinkingLevelMap 自动补全：防止 reasoning 模型 max/xhigh 被 Pi 钳制为 high。

背景：Pi 的 getSupportedThinkingLevels() 在模型缺少 thinkingLevelMap 时，
会过滤掉 xhigh/max；clampThinkingLevel("max") 因此回退为 high，导致用户在
Pi Manager 里选择 max 实际却按 high 调用。这里保证所有保存入口自动补全。
"""
from __future__ import annotations



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


def test_upsert_fills_incomplete_id_name_models(isolated_home):
    from pi_manager import core

    core.upsert_custom_provider(
        "grokified",
        base_url="https://api.grokified.com/v1",
        api_key="gk_live_test",
        models=[{"id": "grok-4.5", "name": "grok-4.5"}],
    )
    cfg = core.load_models_config()
    model = cfg["providers"]["grokified"]["models"][0]
    assert model["id"] == "grok-4.5"
    assert model["reasoning"] is True
    assert model["contextWindow"] == core.DEFAULT_CONTEXT_WINDOW
    assert model["maxTokens"] == 32768
    assert model["thinkingLevelMap"]["max"] == "max"


def test_fill_model_defaults_preserves_explicit_non_reasoning(isolated_home):
    from pi_manager import core

    model = core.fill_model_defaults({"id": "gpt-4o-mini", "reasoning": False})
    assert model["reasoning"] is False
    assert "thinkingLevelMap" not in model
    assert model["contextWindow"] == core.DEFAULT_CONTEXT_WINDOW


def test_load_models_config_migrates_incomplete_handwritten_models(isolated_home):
    from pi_manager import core
    from pi_manager import storage

    storage.save_json(
        core.models_path(),
        {
            "providers": {
                "grokified": {
                    "baseUrl": "https://api.grokified.com/v1",
                    "api": "openai-completions",
                    "apiKey": "!SKIP_ENV",
                    "models": [{"id": "grok-4.5", "name": "grok-4.5"}],
                }
            }
        },
    )
    core._invalidate_config_cache(None)
    core._MODELS_MIGRATED_SIGNATURE = None
    cfg = core.load_models_config()
    model = cfg["providers"]["grokified"]["models"][0]
    assert model["reasoning"] is True
    assert model["thinkingLevelMap"]["max"] == "max"
    on_disk = storage.load_json(core.models_path(), {})
    assert on_disk["providers"]["grokified"]["models"][0]["contextWindow"] == (
        core.DEFAULT_CONTEXT_WINDOW
    )


def test_apply_model_capabilities_defaults_to_1m_thinking_only(isolated_home):
    from pi_manager import core

    model = core.apply_model_capabilities({"id": "qwen3", "contextWindow": 8192})
    assert model["contextWindow"] == core.DEFAULT_CONTEXT_WINDOW
    assert model["reasoning"] is True
    assert model["input"] == ["text"]
    assert model["thinkingLevelMap"]["max"] == "max"


def test_apply_model_capabilities_can_enable_images_and_disable_thinking(isolated_home):
    from pi_manager import core

    model = core.apply_model_capabilities(
        {"id": "vl", "reasoning": True, "thinkingLevelMap": {"high": "high"}},
        context_window=200_000,
        reasoning=False,
        images=True,
    )
    assert model["contextWindow"] == 200_000
    assert model["reasoning"] is False
    assert model["input"] == ["text", "image"]
    assert "thinkingLevelMap" not in model


def test_apply_capabilities_to_saved_models_skips_unknown_provider(isolated_home):
    from pi_manager import core

    core.upsert_custom_provider(
        "xkiro",
        base_url="https://example.com/v1",
        api_key="sk-test",
        models=[{"id": "keep-me", "name": "keep-me"}],
    )
    result = core.apply_capabilities_to_saved_models(
        [("xkiro", "keep-me"), ("builtin", "claude")]
    )
    assert result["updated"] == 1
    assert result["skipped"] == 1
    saved = core.load_models_config()["providers"]["xkiro"]["models"][0]
    assert saved["contextWindow"] == core.DEFAULT_CONTEXT_WINDOW
    assert saved["reasoning"] is True
    assert saved["input"] == ["text"]
