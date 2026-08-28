"""Provider 预设模板库的单元测试。"""
from __future__ import annotations

from pi_manager import provider_presets


def test_presets_cover_domestic_and_overseas():
    presets = provider_presets.list_presets()
    assert len(presets) >= 20
    regions = {str(p.get("region")) for p in presets}
    assert "国内" in regions
    assert "国外" in regions


def test_every_preset_has_required_fields():
    valid_apis = {"openai-completions", "openai-responses", "anthropic-messages", "google-generative-ai"}
    for preset in provider_presets.list_presets():
        assert preset.get("name"), preset
        assert preset.get("label"), preset
        assert preset.get("base_url"), preset
        api = str(preset.get("api") or "")
        assert api in valid_apis, f"{preset['name']} api={api!r}"
        assert isinstance(preset.get("models"), list) and preset["models"], preset["name"]
        for model in preset["models"]:
            assert model.get("id"), f"{preset['name']}: {model}"
            assert "reasoning" in model
            assert model.get("contextWindow", 0) > 0
            assert model.get("maxTokens", 0) > 0


def test_find_preset_by_name_or_label():
    deepseek = provider_presets.find_preset("deepseek")
    assert deepseek is not None
    assert deepseek["base_url"].startswith("https://api.deepseek.com")
    by_label = provider_presets.find_preset("DeepSeek 深度求索")
    assert by_label is not None
    assert provider_presets.find_preset("not-exists") is None
    assert provider_presets.find_preset("") is None


def test_apply_preset_returns_models_json_entry_without_key():
    entry = provider_presets.apply_preset("openai")
    assert entry is not None
    assert entry["api"] == "openai-completions"
    assert entry["baseUrl"] == "https://api.openai.com/v1"
    assert isinstance(entry["models"], list) and entry["models"]
    assert "apiKey" not in entry
    assert isinstance(entry["compat"], dict)


def test_apply_preset_unknown_returns_none():
    assert provider_presets.apply_preset("nope") is None


def test_grokified_preset_covers_current_grok_slugs():
    preset = provider_presets.find_preset("grokified")
    assert preset is not None
    assert preset["base_url"] == "https://api.grokified.com/v1"
    assert preset["api"] == "openai-completions"
    ids = [str(m.get("id")) for m in preset["models"]]
    assert "grok-4.6" in ids
    assert "grok-4.5" in ids
    assert "grok-4.3" in ids


def test_upsert_custom_provider_from_preset(isolated_home):
    """模板可直接喂给 core.upsert_custom_provider 完成一键接入。

    ``isolated_home`` 是必需的：本用例会真实写入 ``models.json`` 并把 API Key 交给
    安全存储。缺它时会污染开发者真实的 ``~/.pi/agent/`` 与真实 OS keyring
    （2026-08 审查 P0-1 已实际发生过）。仓库根 ``conftest.py`` 的 autouse 守卫与
    ``test_plugin_standards.py`` 的静态门禁负责让这类遗漏不再可能。
    """
    from pi_manager import core

    entry = provider_presets.apply_preset("zhipu")
    assert entry is not None
    cfg = core.upsert_custom_provider(
        "zhipu",
        base_url=entry["baseUrl"],
        api=entry["api"],
        api_key="sk-test-123",
        models=entry["models"],
        compat=entry["compat"],
    )
    saved = cfg["providers"]["zhipu"]
    assert saved["api"] == "openai-completions"
    assert saved["baseUrl"].startswith("https://open.bigmodel.cn")
    assert saved["models"][0]["id"] == "glm-4-plus"
