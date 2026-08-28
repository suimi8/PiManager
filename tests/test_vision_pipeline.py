"""智谱识图管道测试。

规则：
- 设置页配置的智谱 API Key / 识图模型选择只用于识图管道（默认使用），
  绝不自动写入 models.json 的 provider 列表；
- 用户手动添加的 provider（即使包含识图模型）在模型列表中正常展示；
- Pi vision skill 默认启用识图（--vision-describe 调用 describe_image）。
"""
from __future__ import annotations

import pytest

from pi_manager import core


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def zhipu_key(isolated_home):
    core.set_zhipu_api_key("sk-zhipu-test")
    yield "sk-zhipu-test"
    core.set_zhipu_api_key("")


def test_ensure_zhipu_provider_does_not_touch_models_json(isolated_home, zhipu_key):
    """设置识图配置不会自动创建/修改 provider 列表。"""
    before = core.load_models_config()
    info = core.ensure_zhipu_provider()
    assert info["ok"] is True
    assert info["base_url"] == core.ZHIPU_BASE_URL
    assert set(info["models"]) == set(core.ZHIPU_VISION_MODELS)
    after = core.load_models_config()
    # models.json 保持不变：没有 zhipu provider 被自动写入
    assert after == before
    assert "zhipu" not in (after.get("providers") or {})


def test_setting_vision_key_does_not_register_provider(isolated_home, zhipu_key):
    """设置页填写智谱 Key 只进密钥库，不写 provider 列表。"""
    cfg = core.load_models_config()
    assert "zhipu" not in (cfg.get("providers") or {})
    assert core.zhipu_api_key() == "sk-zhipu-test"


def test_ensure_zhipu_provider_requires_key(isolated_home):
    with pytest.raises(ValueError, match="智谱 API Key"):
        core.ensure_zhipu_provider()


def test_manual_provider_with_vision_models_shown_in_tree(qapp, isolated_home):
    """用户手动添加的 provider（含识图模型）在模型列表中正常展示。"""
    from pi_manager.presentation.main_window import ModernMainWindow

    window = ModernMainWindow(start_background=False)
    try:
        window.models = [
            core.ModelInfo("zhipu", "glm-4-plus", context="128k"),
            core.ModelInfo("zhipu", "glm-4.6v-flash", context="128k"),
            core.ModelInfo("zhipu", "glm-4.1v-thinking-flash", context="64k"),
            core.ModelInfo("deepseek", "deepseek-chat", context="128k"),
        ]
        window.fill_models_table()
        zhipu_group = None
        for i in range(window.models_table.topLevelItemCount()):
            group = window.models_table.topLevelItem(i)
            if "zhipu" in group.text(0):
                zhipu_group = group
                break
        assert zhipu_group is not None
        ids = [
            zhipu_group.child(j).text(0).lstrip("●★ ").strip()
            for j in range(zhipu_group.childCount())
        ]
        # 手动添加的模型（含识图模型）全部展示
        assert "glm-4-plus" in ids
        assert "glm-4.6v-flash" in ids
        assert "glm-4.1v-thinking-flash" in ids
    finally:
        window._shutdown_background_tasks()
        window.hide()
        window.deleteLater()


def test_vision_model_choice_is_default_for_pipeline(isolated_home, zhipu_key):
    """识图管道默认使用设置中选择的识图模型（空 = 自动链）。"""
    assert core.vision_model_choice() == ""
    core.set_vision_model_choice("glm-4.6v-flash")
    assert core.vision_model_choice() == "glm-4.6v-flash"
    core.set_vision_model_choice("")
    assert core.vision_model_choice() == ""


@pytest.fixture
def captured_vision_calls(monkeypatch):
    """拦住网络层，捕获 describe_image 真实构造的请求体（不联网）。

    以前这里的测试在用例内**重新实现**了一遍候选链再自我断言，删掉
    describe_image 照样通过（r2-testing P0-5「假绿测试」）。现在一律通过真实
    调用 describe_image 来观察它的行为。
    """
    from pi_manager import core_vision

    calls: list[dict] = []
    responses: dict[str, dict] = {}

    def fake_request(model, api_key, body_obj, timeout, proxy):
        calls.append(
            {
                "model": model,
                "api_key": api_key,
                "body": body_obj,
                "timeout": timeout,
                "proxy": proxy,
            }
        )
        return dict(
            responses.get(
                model,
                {"ok": True, "description": f"{model} 的描述", "model": model},
            )
        )

    monkeypatch.setattr(core_vision, "_zhipu_vision_request", fake_request)
    return {"calls": calls, "responses": responses}


def _first_text_part(call: dict) -> dict:
    return call["body"]["messages"][0]["content"][0]


def _image_url(call: dict) -> str:
    return call["body"]["messages"][0]["content"][1]["image_url"]["url"]


def test_describe_image_walks_the_free_model_chain(isolated_home, zhipu_key, captured_vision_calls):
    """真实调用 describe_image：不依赖 models.json，按内置免费模型链依次尝试。"""
    calls = captured_vision_calls["calls"]
    assert "zhipu" not in (core.load_models_config().get("providers") or {})

    result = core.describe_image(b"\x89PNG-fake", "image/png", prompt="这是什么？")

    assert result["ok"] is True
    assert [c["model"] for c in calls] == ["glm-4.6v-flash"]
    assert calls[0]["api_key"] == "sk-zhipu-test"
    assert calls[0]["body"]["model"] == "glm-4.6v-flash"
    assert calls[0]["body"]["max_tokens"] == 8192
    assert _first_text_part(calls[0]) == {"type": "text", "text": "这是什么？"}


def test_describe_image_falls_back_to_backup_model_on_429(
    isolated_home, zhipu_key, captured_vision_calls
):
    """免费额度被限流（429）时必须切到备用模型，而不是直接失败。"""
    calls = captured_vision_calls["calls"]
    captured_vision_calls["responses"]["glm-4.6v-flash"] = {
        "ok": False,
        "description": "",
        "error": "HTTP 429",
        "http_status": 429,
        "model": "glm-4.6v-flash",
    }

    result = core.describe_image(b"png", "image/png", prompt="读图")

    assert result["ok"] is True
    assert result["model"] == "glm-4.1v-thinking-flash"
    assert [c["model"] for c in calls] == ["glm-4.6v-flash", "glm-4.1v-thinking-flash"]


def test_describe_image_stops_on_non_retryable_error(
    isolated_home, zhipu_key, captured_vision_calls
):
    """401 等非限流错误不得盲目重试第二个模型。"""
    calls = captured_vision_calls["calls"]
    captured_vision_calls["responses"]["glm-4.6v-flash"] = {
        "ok": False,
        "description": "",
        "error": "HTTP 401: Unauthorized",
        "http_status": 401,
        "model": "glm-4.6v-flash",
    }

    result = core.describe_image(b"png", "image/png", prompt="读图")

    assert result["ok"] is False
    assert "401" in result["error"]
    assert [c["model"] for c in calls] == ["glm-4.6v-flash"]


def test_describe_image_never_sends_null_text(isolated_home, zhipu_key, captured_vision_calls):
    """P1-3 回归：prompt=None / '' 绝不能变成请求体里的 "text": null。"""
    import json

    from pi_manager import core_vision

    calls = captured_vision_calls["calls"]
    for prompt in (None, "", "   "):
        core.describe_image(b"png", "image/png", prompt=prompt)
    for call in calls:
        part = _first_text_part(call)
        assert isinstance(part["text"], str) and part["text"].strip()
        assert part["text"] == core_vision.DEFAULT_VISION_PROMPT
        assert '"text": null' not in json.dumps(call["body"], ensure_ascii=False)


def test_describe_image_uses_the_given_mime(isolated_home, zhipu_key, captured_vision_calls):
    """data URI 必须反映真实图片类型，不能把 JPEG/WebP 都标成 image/png。"""
    calls = captured_vision_calls["calls"]
    core.describe_image(b"jpegdata", "image/jpeg", prompt="q")
    core.describe_image(b"webpdata", "image/webp", prompt="q")
    core.describe_image(b"unknown", "", prompt="q")

    assert _image_url(calls[0]).startswith("data:image/jpeg;base64,")
    assert _image_url(calls[1]).startswith("data:image/webp;base64,")
    # 空 mime 回退到 image/png
    assert _image_url(calls[2]).startswith("data:image/png;base64,")


def test_describe_image_honors_configured_model_choice(
    isolated_home, zhipu_key, captured_vision_calls
):
    """设置页选定识图模型后，只调用该模型（不再走自动链）。"""
    calls = captured_vision_calls["calls"]
    core.set_vision_model_choice("glm-4.1v-thinking-flash")
    try:
        core.describe_image(b"png", "image/png", prompt="q")
    finally:
        core.set_vision_model_choice("")
    assert [c["model"] for c in calls] == ["glm-4.1v-thinking-flash"]


def test_describe_image_without_key_makes_no_request(isolated_home, captured_vision_calls):
    """未配置 Key 时给出中文引导，且绝不发起请求。"""
    result = core.describe_image(b"png", "image/png", prompt="q")
    assert result["ok"] is False
    assert "智谱 API Key" in result["error"]
    assert captured_vision_calls["calls"] == []


# ---- load_image_for_describe：CLI 热路径的单一真相点 ---------------------


@pytest.mark.parametrize(
    "name,mime",
    [
        ("a.png", "image/png"),
        ("a.jpg", "image/jpeg"),
        ("a.JPEG", "image/jpeg"),
        ("a.webp", "image/webp"),
        ("a.gif", "image/gif"),
        ("a.bmp", "image/bmp"),
        ("a.tiff", "image/tiff"),
    ],
)
def test_load_image_for_describe_reports_detected_mime(tmp_path, name, mime):
    path = tmp_path / name
    path.write_bytes(b"payload")
    loaded = core.load_image_for_describe(str(path))
    assert loaded["ok"] is True
    assert loaded["data"] == b"payload"
    assert loaded["mime"] == mime


def test_load_image_for_describe_rejects_non_image_extension(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("x", encoding="utf-8")
    loaded = core.load_image_for_describe(str(path))
    assert loaded["ok"] is False
    assert "仅支持图片文件" in loaded["error"]


def test_load_image_for_describe_rejects_oversize(tmp_path):
    path = tmp_path / "big.png"
    with open(path, "wb") as fh:
        fh.truncate(21 * 1024 * 1024)
    loaded = core.load_image_for_describe(str(path))
    assert loaded["ok"] is False
    assert "上限 20MB" in loaded["error"]


def test_load_image_for_describe_reports_missing_file(tmp_path):
    loaded = core.load_image_for_describe(str(tmp_path / "nope.png"))
    assert loaded["ok"] is False
    assert "无法读取图片" in loaded["error"]
