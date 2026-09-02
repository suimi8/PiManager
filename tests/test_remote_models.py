from __future__ import annotations

from pi_manager.remote_models import (
    checked_models,
    filter_remote_models,
    ids_from_models,
    model_id,
    models_from_json_text,
)


def test_filter_remote_models_matches_id_tokens():
    models = [
        {"id": "openai/gpt-5.6-terra", "name": "GPT"},
        {"id": "qwen/qwen3-vl-plus:free"},
        {"id": "minimax/minimax-m3:free"},
    ]
    assert [model_id(item) for item in filter_remote_models(models, "qwen :free")] == [
        "qwen/qwen3-vl-plus:free"
    ]
    assert len(filter_remote_models(models, "")) == 3
    assert filter_remote_models(models, "no-such-model") == []


def test_checked_models_and_json_roundtrip():
    models = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    chosen = checked_models(models, {"b", "c", "missing"})
    assert ids_from_models(chosen) == {"b", "c"}
    parsed = models_from_json_text('[{"id": "x"}]')
    assert model_id(parsed[0]) == "x"
