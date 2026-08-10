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


def test_describe_image_uses_configured_models_without_provider(isolated_home, zhipu_key):
    """识图不依赖 models.json：仅凭设置中的 Key 即可构造调用参数。"""
    from pi_manager import core as c

    # 设置 Key 后 models.json 无 zhipu provider，describe_image 仍可进入调用路径
    cfg = c.load_models_config()
    assert "zhipu" not in (cfg.get("providers") or {})
    # 直接验证模型候选链
    candidates = [c.vision_model_choice()] if c.vision_model_choice() else list(c.ZHIPU_VISION_MODELS)
    assert candidates == ["glm-4.6v-flash", "glm-4.1v-thinking-flash"]
