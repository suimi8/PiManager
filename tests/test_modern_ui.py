from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from pi_manager import core
from pi_manager.presentation.main_window import ModernMainWindow


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


def _dispose(window: ModernMainWindow, app: QApplication) -> None:
    window._shutdown_background_tasks()
    window.hide()
    window.deleteLater()
    app.processEvents()


def test_modern_window_builds_without_background_side_effects(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        assert window._background_enabled is False
        assert window.windowTitle() == "Pi Manager"
        assert window.workers == []
        assert window.pages.count() == 10
        assert window.nav.current_key() == "simple"
        assert window.page_heading.text() == "简化配置"
        for attribute in (
            "lbl_current",
            "models_table",
            "provider_list",
            "chat_input",
            "sessions_table",
            "status",
        ):
            assert hasattr(window, attribute), attribute
    finally:
        _dispose(window, qapp)


def test_navigation_is_grouped_collapsible_and_keeps_page_stack_in_sync(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        for key in window._page_keys:
            window._goto_page(key)
            qapp.processEvents()
            assert window.nav.current_key() == key
            assert window.pages.currentIndex() == window._page_index[key]
        window.nav.set_collapsed(True)
        assert window.nav.is_collapsed() is True
        assert window.nav.width() == window.nav.COLLAPSED_WIDTH
        window.nav.set_collapsed(False)
        assert window.nav.width() == window.nav.EXPANDED_WIDTH
    finally:
        _dispose(window, qapp)


def test_model_catalog_updates_details_panel(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        window.models = [
            core.ModelInfo("provider-a", "model-one", context="128k", thinking="yes"),
            core.ModelInfo("provider-b", "model-two", context="64k", images="yes"),
        ]
        window.fill_models_table()
        assert window.models_table.rowCount() == 2
        window.models_table.selectRow(0)
        qapp.processEvents()
        assert window.model_detail_title.text() in {"model-one", "model-two"}
        assert window.model_detail_provider.text() in {"provider-a", "provider-b"}
        assert "context" in window.model_detail_text.toPlainText()
    finally:
        _dispose(window, qapp)


def test_modern_theme_is_applied_to_application(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        window.apply_ui_theme("day", "purple")
        assert "#F4F6F8" in qapp.styleSheet()
        window.apply_ui_theme("night", "blue")
        assert "#090C10" in qapp.styleSheet()
    finally:
        _dispose(window, qapp)


def test_chat_selection_stays_independent_until_failover_switches_the_pair(qapp, isolated_home):
    core.set_default_model("provider-a", "model-one", "high")
    window = ModernMainWindow(start_background=False)
    try:
        window.models = [
            core.ModelInfo("provider-a", "model-one"),
            core.ModelInfo("provider-b", "model-two"),
        ]
        window.refresh_chat_model_choices()
        window._set_chat_combo_text(window.chat_provider, "provider-a")
        window._reload_chat_models_for_provider("provider-a", prefer_model="model-one")
        window._set_chat_combo_text(window.chat_model, "model-one")

        # A default-model change elsewhere updates the dashboard but must not
        # overwrite an explicit quick-chat selection.
        core.set_default_model("provider-b", "model-two", "low")
        window.refresh_dashboard()
        assert window._chat_combo_text(window.chat_provider) == "provider-a"
        assert window._chat_combo_text(window.chat_model) == "model-one"
        assert window.workers == []

        # If the request itself fails over, both provider and model move as one
        # atomic pair so the UI matches the model actually used.
        window._on_basic_chat_done(
            {
                "ok": True,
                "switched": True,
                "provider": "provider-b",
                "model": "model-two",
                "stdout": "ok",
                "stderr": "",
                "returncode": 0,
            }
        )
        assert window._chat_combo_text(window.chat_provider) == "provider-b"
        assert window._chat_combo_text(window.chat_model) == "model-two"
    finally:
        _dispose(window, qapp)
