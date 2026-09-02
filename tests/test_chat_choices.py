"""快速提问：Provider/模型合并去重与选中规则（无 Qt、不写 HOME）。"""
from __future__ import annotations

from pi_manager.chat_choices import (
    config_models_for_provider,
    merge_model_ids,
    merge_provider_names,
    pick_model,
    pick_provider,
    providers_from_models_config,
)


def test_merge_provider_names_includes_json_only_and_sorts():
    listed = ["zhipu", "openai"]
    config = ["deepseek", "openai", "anthropic"]
    assert merge_provider_names(listed, config) == [
        "anthropic",
        "deepseek",
        "openai",
        "zhipu",
    ]


def test_merge_provider_names_drops_empty_and_duplicates():
    assert merge_provider_names(["b", "a", "b", ""], ["", "a", "c"]) == ["a", "b", "c"]
    assert merge_provider_names([], []) == []


def test_providers_from_models_config_skips_empty_keys():
    cfg = {"providers": {"zhipu": {}, "": {}, "openai": {}}}
    assert providers_from_models_config(cfg) == ["zhipu", "openai"]


def test_pick_provider_keeps_current_when_still_listed():
    assert pick_provider("openai", ["anthropic", "openai"], "anthropic") == "openai"


def test_pick_provider_falls_back_to_default_then_first():
    providers = ["anthropic", "zhipu"]
    assert pick_provider("gone", providers, "zhipu") == "zhipu"
    assert pick_provider("gone", providers, "missing") == "anthropic"
    assert pick_provider("", [], "") == ""


def test_merge_model_ids_listed_first_then_json_variants():
    listed = ["glm-4.6v-flash", "glm-4-plus"]
    config = [
        {"id": "glm-4-plus"},
        {"id": "glm-4-air"},
        {"model": "custom-from-model"},
        "bare-str",
        "",
        {"id": ""},
        {"id": "glm-4.6v-flash"},
        None,
        123,
    ]
    assert merge_model_ids(listed, config) == [
        "glm-4.6v-flash",
        "glm-4-plus",
        "glm-4-air",
        "custom-from-model",
        "bare-str",
    ]


def test_merge_model_ids_dict_id_falls_back_to_model():
    assert merge_model_ids([], [{"id": "", "model": "via-model"}]) == ["via-model"]


def test_config_models_for_provider_returns_raw_list():
    cfg = {
        "providers": {
            "zhipu": {"models": [{"id": "glm-4-plus"}, "bare"]},
            "other": {"models": [{"id": "skip-me"}]},
        }
    }
    assert config_models_for_provider(cfg, "zhipu") == [{"id": "glm-4-plus"}, "bare"]
    assert config_models_for_provider(cfg, "missing") == []


def test_pick_model_prefer_wins():
    models = ["glm-4-air", "glm-4-plus"]
    assert (
        pick_model(
            "glm-4-plus",
            models,
            provider="zhipu",
            default_provider="zhipu",
            default_model="glm-4-air",
        )
        == "glm-4-plus"
    )


def test_pick_model_uses_same_provider_default_when_prefer_missing():
    models = ["glm-4-air", "glm-4-plus"]
    assert (
        pick_model(
            "gone",
            models,
            provider="zhipu",
            default_provider="zhipu",
            default_model="glm-4-plus",
        )
        == "glm-4-plus"
    )


def test_pick_model_ignores_default_of_other_provider():
    models = ["glm-4-air", "glm-4-plus"]
    assert (
        pick_model(
            "gone",
            models,
            provider="zhipu",
            default_provider="deepseek",
            default_model="glm-4-plus",
        )
        == "glm-4-air"
    )


def test_pick_model_empty_list():
    assert (
        pick_model(
            "any",
            [],
            provider="zhipu",
            default_provider="zhipu",
            default_model="glm-4-plus",
        )
        == ""
    )
