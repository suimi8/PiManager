"""快速提问：Provider 选择 + 该 Provider 在 models.json 中添加的模型可选。"""
from __future__ import annotations

import pytest


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app


def _dispose(window, app):
    window._shutdown_background_tasks()
    window.hide()
    window.deleteLater()
    app.processEvents()


def test_chat_provider_dropdown_includes_models_json_providers(qapp, isolated_home):
    """快速提问的 Provider 下拉包含 models.json 手动添加的 provider。"""
    from pi_manager import core
    from pi_manager.presentation.main_window import ModernMainWindow

    core.upsert_custom_provider(
        "zhipu",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="sk-test",
        models=[{"id": "glm-4-plus"}, {"id": "glm-4-air"}],
    )
    window = ModernMainWindow(start_background=False)
    try:
        # 即使 list-models 尚未加载（self.models 为空），provider 下拉也有 zhipu
        window.models = []
        window.refresh_chat_model_choices()
        providers = [window.chat_provider.itemText(i) for i in range(window.chat_provider.count())]
        assert "zhipu" in providers
    finally:
        _dispose(window, qapp)


def test_chat_model_dropdown_merges_models_json_models(qapp, isolated_home):
    """模型下拉 = list-models 结果 + models.json 该 provider 手动添加的模型。"""
    from pi_manager import core
    from pi_manager.presentation.main_window import ModernMainWindow

    core.upsert_custom_provider(
        "zhipu",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="sk-test",
        models=[{"id": "glm-4-plus"}, {"id": "glm-4-air"}, {"id": "glm-4.5"}],
    )
    window = ModernMainWindow(start_background=False)
    try:
        # list-models 只枚举到部分模型（模拟刷新不及时/接口差异）
        window.models = [core.ModelInfo("zhipu", "glm-4.6v-flash")]
        window._set_chat_combo_text(window.chat_provider, "zhipu")
        window._reload_chat_models_for_provider("zhipu")
        items = [window.chat_model.itemText(i) for i in range(window.chat_model.count())]
        # list-models 的模型可选
        assert "glm-4.6v-flash" in items
        # models.json 手动添加的模型可选（核心诉求）
        assert "glm-4-plus" in items
        assert "glm-4-air" in items
        assert "glm-4.5" in items
    finally:
        _dispose(window, qapp)


def test_chat_switches_model_dropdown_when_provider_changes(qapp, isolated_home):
    """切换 Provider 后模型下拉只保留该 Provider 的模型（含 models.json 添加项）。"""
    from pi_manager import core
    from pi_manager.presentation.main_window import ModernMainWindow

    core.upsert_custom_provider(
        "zhipu",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        api_key="sk-test",
        models=[{"id": "glm-4-plus"}],
    )
    core.upsert_custom_provider(
        "deepseek",
        base_url="https://api.deepseek.com/v1",
        api_key="sk-test",
        models=[{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}],
    )
    window = ModernMainWindow(start_background=False)
    try:
        window.models = [
            core.ModelInfo("zhipu", "glm-4.6v-flash"),
            core.ModelInfo("deepseek", "deepseek-chat"),
        ]
        window._set_chat_combo_text(window.chat_provider, "deepseek")
        window._reload_chat_models_for_provider("deepseek")
        deepseek_items = [window.chat_model.itemText(i) for i in range(window.chat_model.count())]
        assert "deepseek-chat" in deepseek_items
        assert "deepseek-reasoner" in deepseek_items
        assert "glm-4-plus" not in deepseek_items

        window._set_chat_combo_text(window.chat_provider, "zhipu")
        window._reload_chat_models_for_provider("zhipu")
        zhipu_items = [window.chat_model.itemText(i) for i in range(window.chat_model.count())]
        assert "glm-4.6v-flash" in zhipu_items
        assert "glm-4-plus" in zhipu_items
        assert "deepseek-chat" not in zhipu_items
    finally:
        _dispose(window, qapp)


def test_chat_send_uses_selected_provider_model_pair(qapp, isolated_home):
    """发送时携带所选 provider/model（models.json 手动添加的模型也可直接使用）。"""
    from pi_manager import core
    from pi_manager.presentation.main_window import ModernMainWindow

    core.set_default_model("zhipu", "glm-4-plus", "high")
    window = ModernMainWindow(start_background=False)
    try:
        window.models = []
        window.refresh_chat_model_choices()
        window._set_chat_combo_text(window.chat_provider, "zhipu")
        window._set_chat_combo_text(window.chat_model, "glm-4-plus")
        assert window._chat_combo_text(window.chat_provider) == "zhipu"
        assert window._chat_combo_text(window.chat_model) == "glm-4-plus"
    finally:
        _dispose(window, qapp)


def test_chat_falls_back_when_default_provider_is_stale(qapp, isolated_home):
    """默认 provider 在 models.json 中已不存在（残留配置）时，
    快速提问回退到实际存在的 provider，不把残留值强加进下拉框。"""
    from pi_manager import core
    from pi_manager.presentation.main_window import ModernMainWindow

    core.upsert_custom_provider(
        "opencode go",
        base_url="https://opencode.ai/zen/go/v1",
        api_key="sk-test",
        models=[{"id": "deepseek-v4-flash"}],
    )
    # 残留默认模型：models.json 中没有 deepseek provider
    core.set_default_model("deepseek", "deepseek-chat", "high")

    window = ModernMainWindow(start_background=False)
    try:
        window.models = [core.ModelInfo("opencode go", "deepseek-v4-flash")]
        window.refresh_chat_model_choices()

        providers = [window.chat_provider.itemText(i) for i in range(window.chat_provider.count())]
        assert providers == ["opencode go"], providers
        assert window._chat_combo_text(window.chat_provider) == "opencode go"

        models = [window.chat_model.itemText(i) for i in range(window.chat_model.count())]
        assert models == ["deepseek-v4-flash"], models
        assert window._chat_combo_text(window.chat_model) == "deepseek-v4-flash"

        # 使用默认模型按钮同样回退
        window.chat_fill_default()
        assert window._chat_combo_text(window.chat_provider) == "opencode go"
        assert window._chat_combo_text(window.chat_model) == "deepseek-v4-flash"
    finally:
        _dispose(window, qapp)


def test_chat_model_dropdown_clears_when_provider_has_no_models(qapp, isolated_home):
    """选择没有模型的 Provider 时，模型下拉清空而非残留旧文本。"""
    from pi_manager import core
    from pi_manager.presentation.main_window import ModernMainWindow

    core.upsert_custom_provider("empty-p", base_url="https://example.com/v1", api_key="sk-test")
    window = ModernMainWindow(start_background=False)
    try:
        window.models = []
        window.refresh_chat_model_choices()
        window._set_chat_combo_text(window.chat_provider, "empty-p")
        window._reload_chat_models_for_provider("empty-p")
        assert window.chat_model.count() == 0
        assert window._chat_combo_text(window.chat_model) == ""
    finally:
        _dispose(window, qapp)


def test_single_instance_lock_prevents_second_instance(qapp, isolated_home):
    """单实例锁：第二个 Pi Manager 实例无法获取同一把锁。"""
    from PySide6.QtCore import QLockFile

    from pi_manager import core

    core.ensure_agent_dir()
    lock_path = str(core.pi_agent_dir() / "pi-manager.lock")
    lock1 = QLockFile(lock_path)
    lock1.setStaleLockTime(0)
    assert lock1.tryLock(100) is True
    try:
        # 第二个“实例”获取同一把锁失败
        lock2 = QLockFile(lock_path)
        lock2.setStaleLockTime(0)
        assert lock2.tryLock(100) is False
    finally:
        lock1.unlock()
    # 释放后可重新获取（进程退出后锁自动释放）
    lock3 = QLockFile(lock_path)
    lock3.setStaleLockTime(0)
    assert lock3.tryLock(100) is True
    lock3.unlock()
