from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import QApplication, QDialog

from pi_manager import core
from pi_manager.presentation.main_window import ModernMainWindow
from pi_manager.ui import InstallPiDialog


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



def test_global_ui_mode_persists_matching_pi_cli_theme(isolated_home):
    core.set_ui_theme("day", "blue")
    assert core.get_ui_theme()["mode"] == "day"
    assert core.load_settings()["theme"] == "light"

    core.set_ui_theme("night", "purple")
    assert core.get_ui_theme()["mode"] == "night"
    assert core.load_settings()["theme"] == "dark"


def test_settings_page_has_no_independent_cli_theme_controls(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        assert hasattr(window, "set_ui_mode")
        assert not hasattr(window, "set_theme")
        assert not hasattr(window, "set_cli_theme")
    finally:
        _dispose(window, qapp)


def test_open_dialogs_and_install_dialog_follow_application_theme(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    plain_dialog = QDialog()
    plain_dialog.resize(220, 120)
    install_dialog = InstallPiDialog(
        status={
            "node_version": "22.20.0",
            "npm_version": "11.0.0",
            "channel": "latest",
            "package_spec": "@earendil-works/pi-coding-agent@latest",
            "latest": "0.80.10",
        },
    )
    plain_dialog.show()
    install_dialog.show()
    try:
        window.apply_ui_theme("day", "blue")
        qapp.processEvents()
        assert qapp.palette().color(QPalette.Window).name().upper() == "#F4F6F8"
        assert plain_dialog.grab().toImage().pixelColor(10, 10).name().upper() == "#F4F6F8"
        assert install_dialog.grab().toImage().pixelColor(10, 10).name().upper() == "#F4F6F8"

        window.apply_ui_theme("night", "blue")
        qapp.processEvents()
        assert qapp.palette().color(QPalette.Window).name().upper() == "#090C10"
        assert plain_dialog.grab().toImage().pixelColor(10, 10).name().upper() == "#090C10"
        assert install_dialog.grab().toImage().pixelColor(10, 10).name().upper() == "#090C10"
    finally:
        install_dialog.close()
        plain_dialog.close()
        install_dialog.deleteLater()
        plain_dialog.deleteLater()
        _dispose(window, qapp)


def test_dynamic_theme_refreshes_model_status_and_help_html(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        model = core.ModelInfo("provider-a", "model-one")
        window.models = [model]
        window.test_results = {
            model.key: {"available": True, "latency_ms": 120, "pending": False}
        }
        window.fill_models_table()

        window.apply_ui_theme("day", "blue")
        qapp.processEvents()
        day_status = window.models_table.item(0, 3).foreground().color().name().upper()
        day_html = window.help_browser.toHtml().lower()

        window.apply_ui_theme("night", "blue")
        qapp.processEvents()
        night_status = window.models_table.item(0, 3).foreground().color().name().upper()
        night_html = window.help_browser.toHtml().lower()

        assert day_status == "#16A34A"
        assert night_status == "#35C56F"
        assert day_status != night_status
        assert "#f3f4f6" in day_html
        assert "#1a222d" in night_html
        assert day_html != night_html
    finally:
        _dispose(window, qapp)
