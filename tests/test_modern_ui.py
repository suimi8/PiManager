from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication, QDialog

from pi_manager import core
from pi_manager.presentation.design.tokens import tokens_for
from pi_manager.presentation.main_window import ModernMainWindow
from pi_manager.ui import InstallPiDialog, Worker


def _color_close(actual: str, expected: str, tolerance: int = 60) -> bool:
    """RGB 曼哈顿距离容差比较：允许主题色微调，避免像素级硬编码。"""
    a, e = QColor(actual), QColor(expected)
    return (
        abs(a.red() - e.red()) + abs(a.green() - e.green()) + abs(a.blue() - e.blue())
        <= tolerance
    )


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
        assert window.pages.count() == 11
        assert window.nav.current_key() == "simple"
        assert window.page_heading.text() == "概览"
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
        # 通过公共接口遍历所有页面：导航行 ↔ 页面栈 index ↔ 标题同步
        for row in range(window.pages.count()):
            window.nav.setCurrentRow(row)
            qapp.processEvents()
            assert window.nav.currentRow() == row
            assert window.nav.current_key()  # 当前 key 非空
            assert window.pages.currentIndex() == row
            assert window.page_heading.text()
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
        # 树状分类：2 个 Provider 顶层分组
        assert window.models_table.topLevelItemCount() == 2
        child = window.models_table.topLevelItem(0).child(0)
        window.models_table.setCurrentItem(child)
        qapp.processEvents()
        assert window.model_detail_title.text() in {"model-one", "model-two"}
        assert window.model_detail_provider.text() in {"provider-a", "provider-b"}
        assert "context" in window.model_detail_text.toPlainText()
        assert "Provider" in window.model_prop_table.value_text()
        assert window.model_detail_badge.text() == "尚未测试"
        assert window.model_error_panel.isVisible() is False
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
        # 主题 token 是单一事实来源；palette 与 stylesheet 必须跟随 token
        day_tokens = tokens_for("day", "blue")
        night_tokens = tokens_for("night", "blue")
        window.apply_ui_theme("day", "blue")
        qapp.processEvents()
        assert qapp.palette().color(QPalette.Window).name().upper() == day_tokens.window.upper()
        assert day_tokens.window in qapp.styleSheet()
        # 单个真实像素冒烟（容差比较，主题微调不破坏测试）
        day_pixel = plain_dialog.grab().toImage().pixelColor(10, 10).name().upper()
        assert _color_close(day_pixel, day_tokens.window), f"{day_pixel} 应接近 {day_tokens.window}"

        window.apply_ui_theme("night", "blue")
        qapp.processEvents()
        assert qapp.palette().color(QPalette.Window).name().upper() == night_tokens.window.upper()
        assert night_tokens.window in qapp.styleSheet()
        night_pixel = plain_dialog.grab().toImage().pixelColor(10, 10).name().upper()
        assert _color_close(night_pixel, night_tokens.window), f"{night_pixel} 应接近 {night_tokens.window}"
        # 昼夜像素必须确实不同
        assert day_pixel != night_pixel
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
        day_child = window.models_table.topLevelItem(0).child(0)
        day_status = day_child.foreground(3).color().name().upper()
        day_html = window.help_browser.toHtml().lower()

        window.apply_ui_theme("night", "blue")
        qapp.processEvents()
        night_child = window.models_table.topLevelItem(0).child(0)
        night_status = night_child.foreground(3).color().name().upper()
        night_html = window.help_browser.toHtml().lower()

        assert day_status == tokens_for("day", "blue").success.upper()
        assert night_status == tokens_for("night", "blue").success.upper()
        assert day_status != night_status
        assert "#f3f4f6" in day_html
        assert "#1a222d" in night_html
        assert day_html != night_html
    finally:
        _dispose(window, qapp)


def test_legacy_ui_theme_module_is_gone():
    import importlib

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("pi_manager.ui_theme")


def test_chat_persistent_toggle_and_key_health_surface(qapp, isolated_home):
    core.upsert_custom_provider(
        "KH", base_url="https://kh.example/v1", api_key="sk-good", models=[{"id": "m"}]
    )
    core.add_provider_api_key("KH", "sk-second")
    rows = core.list_provider_api_keys("KH")
    from pi_manager import secrets as secretstore

    secretstore.mark_provider_key_failed("KH", rows[0]["id"], "HTTP 401")

    window = ModernMainWindow(start_background=False)
    try:
        # New persistent-session toggle is wired to manager config.
        assert hasattr(window, "chat_persistent_session")
        window.chat_persistent_session.setChecked(False)
        window.save_feature_settings_fields()
        assert core.load_manager_config()["chat_persistent_session"] is False
        window.settings_load()
        assert window.chat_persistent_session.isChecked() is False

        # Failed key is surfaced on the dashboard provider metric.
        window.refresh_dashboard()
        assert "密钥失效" in window.dashboard_provider_metric.label_label.text()
    finally:
        _dispose(window, qapp)


def test_provider_keys_dialog_reveal_toggle_and_copy(qapp, isolated_home):
    """密钥管理对话框：默认掩码，可切换明文/隐藏，掩码模式下也能复制明文。"""
    from pi_manager.ui import ProviderKeysDialog

    core.upsert_custom_provider(
        "RV", base_url="https://rv.example/v1", api_key="sk-reveal-first", models=[{"id": "m"}]
    )
    core.add_provider_api_key("RV", "sk-reveal-second")

    dialog = ProviderKeysDialog("RV")
    try:
        assert dialog.table.rowCount() == 2
        assert dialog._reveal is False
        assert dialog.reveal_btn.text() == "显示明文"

        # 默认掩码：不泄露明文，也不暴露长度
        texts = [dialog.table.item(i, 0).text() for i in range(dialog.table.rowCount())]
        assert all("sk-reveal" not in text for text in texts)
        assert all("*" in text for text in texts)

        # 切换到明文显示
        dialog.reveal_btn.click()
        qapp.processEvents()
        shown = {dialog.table.item(i, 0).text() for i in range(dialog.table.rowCount())}
        assert shown == {"sk-reveal-first", "sk-reveal-second"}
        assert dialog.reveal_btn.text() == "隐藏明文"
        assert "明文" in dialog.status_label.text()

        # 切换回掩码
        dialog.reveal_btn.click()
        qapp.processEvents()
        texts = [dialog.table.item(i, 0).text() for i in range(dialog.table.rowCount())]
        assert all("sk-reveal" not in text for text in texts)
        assert dialog.reveal_btn.text() == "显示明文"

        # 掩码模式下「复制选中」仍复制明文到剪贴板
        dialog.table.selectRow(0)
        key_id = dialog.selected_key_id()
        dialog.copy_key()
        expected = next(
            str(row.get("value") or "")
            for row in core.list_provider_api_keys("RV", reveal=True)
            if row.get("id") == key_id
        )
        assert QApplication.clipboard().text() == expected
        assert "剪贴板" in dialog.status_label.text()
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


# ---- P1-3 / P1-4：模型表增量刷新、选中与滚动保留、主题查询次数 --------------

def test_batch_progress_updates_one_row_and_keeps_selection(qapp, isolated_home):
    """批量测试每完成一项原本整树重建：抹掉多选 → 「重测选中」事实失效。"""
    window = ModernMainWindow(start_background=False)
    try:
        window.models = [core.ModelInfo("p", f"m{i}") for i in range(6)]
        window.fill_models_table()
        group = window.models_table.topLevelItem(0)
        picked = [group.child(i) for i in (1, 3)]
        for item in picked:
            item.setSelected(True)
        window.models_table.setCurrentItem(picked[0])
        for item in picked:
            item.setSelected(True)
        selected_before = {m.key for m in window.selected_model_rows()}
        assert len(selected_before) == 2

        window._test_total = 6
        window._on_model_test_progress(
            {"provider": "p", "model": "m3", "available": True, "latency_ms": 111}
        )

        # 增量路径：同一批 QTreeWidgetItem 仍在，选中集合原样保留
        assert {m.key for m in window.selected_model_rows()} == selected_before
        row = window._model_row_index["p/m3"]
        assert row.text(3) == "可用"
        assert row.text(4) == "111ms"
        expected = tokens_for(*window._theme_pair()).success.upper()
        assert row.foreground(3).color().name().upper() == expected
    finally:
        _dispose(window, qapp)


def test_full_rebuild_restores_selection_and_current_row(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        window.models = [core.ModelInfo("p", f"m{i}") for i in range(5)]
        window.fill_models_table()
        group = window.models_table.topLevelItem(0)
        for index in (0, 2, 4):
            group.child(index).setSelected(True)
        window.models_table.setCurrentItem(group.child(2))
        for index in (0, 2, 4):
            group.child(index).setSelected(True)
        before = {m.key for m in window.selected_model_rows()}
        assert len(before) == 3

        window.fill_models_table()  # 全量重建（tree.clear()）

        assert {m.key for m in window.selected_model_rows()} == before
        current = window._model_item_key(window.models_table.currentItem())
        assert current is not None and current[1] == "m2"
    finally:
        _dispose(window, qapp)


def test_fill_models_table_theme_lookups_do_not_scale_with_rows(
    qapp, isolated_home, monkeypatch
):
    """以前每行 2 次 core.get_ui_theme()（实测 58 us/次）→ 200 模型批测 ~5s。"""
    window = ModernMainWindow(start_background=False)
    try:
        calls = {"n": 0}
        real = core.get_ui_theme

        def counted():
            calls["n"] += 1
            return real()

        monkeypatch.setattr(core, "get_ui_theme", counted)

        window.models = [core.ModelInfo("p", f"m{i}") for i in range(5)]
        window.fill_models_table()
        small = calls["n"]

        calls["n"] = 0
        window.models = [core.ModelInfo("p", f"m{i}") for i in range(60)]
        window.fill_models_table()
        large = calls["n"]

        assert large == small, (
            f"主题查询次数必须与行数无关：5 行 {small} 次 vs 60 行 {large} 次"
        )
    finally:
        _dispose(window, qapp)


def test_model_filter_is_debounced(qapp, isolated_home):
    """搜索框以前按每次击键触发整树重建。"""
    window = ModernMainWindow(start_background=False)
    try:
        window.models = [core.ModelInfo("p", "alpha"), core.ModelInfo("p", "beta")]
        window.fill_models_table()
        assert window._model_filter_debounce.isSingleShot() is True
        window.model_filter.setText("alph")
        # 击键后不立即重建，只是把防抖定时器拉起
        assert window._model_filter_debounce.isActive() is True
        assert len(window._model_row_index) == 2
        window._model_filter_debounce.stop()
        window.fill_models_table()
        assert set(window._model_row_index) == {"p/alpha"}
    finally:
        _dispose(window, qapp)


def test_model_test_cancel_button_exists_and_interrupts(qapp, isolated_home):
    """BatchTestWorker 一直支持取消，但此前 UI 上没有任何入口。"""
    window = ModernMainWindow(start_background=False)
    try:
        assert window.model_test_cancel_btn.isEnabled() is False

        class _FakeWorker:
            def __init__(self):
                self.interrupted = False

            def isRunning(self):
                return True

            def requestInterruption(self):
                self.interrupted = True

        fake = _FakeWorker()
        window._test_worker = fake
        window._set_test_cancel_enabled(True)
        assert window.model_test_cancel_btn.isEnabled() is True
        window.model_test_cancel()
        assert fake.interrupted is True
        assert window.model_test_cancel_btn.isEnabled() is False
    finally:
        _dispose(window, qapp)


# ---- P1-5：会话筛选只做内存过滤 ---------------------------------------------

def test_session_filter_does_not_rescan_disk(qapp, isolated_home, monkeypatch):
    window = ModernMainWindow(start_background=False)
    try:
        rows = [
            {"path": "/s/a.json", "cwd": "/proj/alpha", "model": "m1", "preview": "hello"},
            {"path": "/s/b.json", "cwd": "/proj/beta", "model": "m2", "preview": "world"},
        ]
        calls = {"n": 0}

        def fake_list_sessions(limit=100):
            calls["n"] += 1
            return list(rows)

        monkeypatch.setattr(core, "list_sessions", fake_list_sessions)
        window.refresh_sessions()
        assert calls["n"] == 1
        assert window.sessions_table.rowCount() == 2

        for text in ("a", "al", "alp", "alph"):
            window.session_filter_wd.setText(text)
            window.sessions_apply_filter()
        assert calls["n"] == 1, "筛选期间不应再遍历会话目录"
        assert window.sessions_table.rowCount() == 1

        window.session_filter_wd.setText("")
        window.sessions_apply_filter()
        assert window.sessions_table.rowCount() == 2
    finally:
        _dispose(window, qapp)


# ---- P1-6：单实例唤醒必须取走挂起连接 ---------------------------------------

def test_wake_drains_pending_local_connections(qapp):
    from PySide6.QtNetwork import QLocalServer, QLocalSocket

    from pi_manager.ui import drain_pending_connections

    name = f"PiManagerTest-{os.getpid()}"
    QLocalServer.removeServer(name)
    server = QLocalServer()
    assert server.listen(name), server.errorString()
    clients = []
    try:
        for _ in range(3):
            sock = QLocalSocket()
            sock.connectToServer(name)
            assert sock.waitForConnected(2000)
            clients.append(sock)
        deadline = time.monotonic() + 3
        while not server.hasPendingConnections() and time.monotonic() < deadline:
            qapp.processEvents()
        assert server.hasPendingConnections() is True

        assert drain_pending_connections(server) >= 1
        # 反复排空后队列必须为空（以前从不取走 → 满 30 后不再发 newConnection）
        qapp.processEvents()
        drain_pending_connections(server)
        assert server.hasPendingConnections() is False
    finally:
        for sock in clients:
            sock.abort()
        server.close()
        QLocalServer.removeServer(name)


# ---- P2-9：InstallPiDialog 关闭时不再碰悬垂的 self._worker -------------------

def test_install_dialog_close_after_worker_deleted(qapp, isolated_home):
    from PySide6.QtGui import QCloseEvent

    dialog = InstallPiDialog(status={"node_version": "22", "npm_version": "11"})
    try:
        worker = dialog._track(Worker(lambda: (0, "ok", "")))
        dialog._worker = worker
        worker.start()
        deadline = time.monotonic() + 3
        while worker in dialog._workers and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.005)
        assert dialog._workers == []
        qapp.processEvents()  # 处理 deleteLater：dialog._worker 变成悬垂包装器
        event = QCloseEvent()
        dialog.closeEvent(event)  # 以前这里 RuntimeError: C++ object already deleted
        assert event.isAccepted() is True
        assert dialog.btn_install.isDefault() is True  # 回车键有默认按钮
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


# ---- P3-17 / P3-18 / P3-19：主题覆盖、版本标签、按钮参数 --------------------

def test_stylesheet_covers_previously_orphaned_object_names():
    from pi_manager.presentation.design.stylesheet import build_stylesheet

    for mode in ("day", "night"):
        css = build_stylesheet(mode, "blue")
        for selector in (
            "QLabel#cardTitle",
            "QLabel#chatThumb",
            "QSplitter::handle",
            "QFrame#feedbackToast",
            "QFrame#updateBanner[status=\"danger\"]",
            "QFrame#feedbackToast[status=\"info\"]",
            "QFrame#resultSheet",
            "QFrame#resultSheet[status=\"warning\"]",
        ):
            assert selector in css, f"{mode} mode missing {selector}"
        # 迁移遗留的死选择器
        assert "QLabel#pill" not in css
        assert "QFrame#heroCard" not in css


def test_success_button_hover_brightens_in_both_modes():
    for mode in ("day", "night"):
        colors = tokens_for(mode, "blue")
        base = QColor(colors.success)
        hover = QColor(colors.success_hover)
        assert hover.lightness() > base.lightness(), (
            f"{mode} mode: success hover must brighten"
        )


def test_nav_version_label_is_visible_when_expanded(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        window.nav.set_collapsed(False)
        window.nav.set_version("pi: 0.80.10")
        assert window.nav.version_label.isVisibleTo(window.nav) is True
        assert window.nav.version_label.text() == "pi: 0.80.10"
        window.nav.set_collapsed(True)
        assert window.nav.version_label.isVisibleTo(window.nav) is False
    finally:
        _dispose(window, qapp)


def test_app_button_swallows_clicked_checked_argument(qapp):
    from pi_manager.presentation.components.primitives import AppButton

    received = []

    def slot(force=None):
        received.append(force)

    button = AppButton("test", slot)
    try:
        button.click()
        assert received == [None], "clicked(bool) must not fill the slot's first arg"
    finally:
        button.deleteLater()
        qapp.processEvents()


def test_navigation_shortcuts_and_page_headings(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        assert len(window._nav_shortcuts) == 15  # Ctrl+1..9 + Ctrl+Tab 双向 + K/S/F/Enter
        window._goto_page("plugins")
        qapp.processEvents()
        # 页头标题不再依赖两个 pageChanged 槽的连接顺序
        assert window.page_heading.text() == "插件管理"
        window._cycle_page(1)
        qapp.processEvents()
        assert window.nav.current_key() == "chat"
        window._cycle_page(-1)
        qapp.processEvents()
        assert window.nav.current_key() == "plugins"
    finally:
        _dispose(window, qapp)


# ---- 架构收敛第 1 步：隐式 widget 契约显式化 -------------------------------

def test_window_widget_contract_is_complete(qapp, isolated_home):
    from pi_manager.presentation.contract import WINDOW_WIDGET_NAMES

    window = ModernMainWindow(start_background=False)
    try:
        missing = [n for n in WINDOW_WIDGET_NAMES if not hasattr(window, n)]
        assert missing == [], f"presentation layer did not inject: {missing}"
    finally:
        _dispose(window, qapp)


def test_persist_mgr_does_not_clobber_failover_fail_counts(qapp, isolated_home):
    """persist_mgr 只合并当前关心的键，不得把并发写入的失败计数回滚。"""
    window = ModernMainWindow(start_background=False)
    try:
        def _seed(cfg):
            cfg["failover_fail_counts"] = {"A/m": 9}
            return cfg

        core.update_manager_config(_seed)
        window.persist_mgr()
        mgr = core.load_manager_config()
        assert mgr.get("failover_fail_counts") == {"A/m": 9}
    finally:
        _dispose(window, qapp)


def test_dashboard_summary_shows_availability_not_just_config(qapp, isolated_home):
    core.set_default_model("provider-a", "model-one", "high")
    window = ModernMainWindow(start_background=False)
    try:
        window.test_results = {
            "provider-a/model-one": {"available": True, "latency_ms": 80}
        }
        window.refresh_dashboard()
        assert "provider-a/model-one" in window.lbl_current.text()
        assert window.dashboard_status_metric.value_label.text() == "连接正常"
        assert window.default_status_badge.text() == "连接正常"
        assert window.dashboard_tested_metric.value_label.text() == "从未测试"
        assert window.availability_banner.isHidden() is True
        groups = [label.text() for label in window.nav._group_labels]
        assert groups == ["概览", "配置", "运行", "系统"]
    finally:
        _dispose(window, qapp)


def test_dashboard_banner_and_feedback_toast(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        assert window.feedback_toast is not None
        window.notify_success("已保存")
        qapp.processEvents()
        assert window.feedback_toast.isHidden() is False
        assert "已保存" in window.feedback_toast.message.text()
        window.notify_info("请先选择模型")
        qapp.processEvents()
        assert "请先选择模型" in window.feedback_toast.message.text()
        window.refresh_dashboard()
        assert window.availability_banner.isHidden() is False
        assert "尚未设置默认模型" in window.availability_banner_label.text()
        assert window.selfcheck_empty.isHidden() is False
        assert window.selfcheck_table.isHidden() is True
        assert window.selfcheck_cancel_btn.isEnabled() is False
        assert window.update_cancel_btn.isEnabled() is False
        assert window.plugins_cancel_btn.isEnabled() is False
        assert window.models_refresh_cancel_btn.isEnabled() is False
        assert window.health_empty.isHidden() is False
        assert window.chat_send_enhanced_btn.text() == "发送到 Pi"
        from pi_manager.presentation.pages.plugins import ops

        cancelled = ops._collect_plugin_rows(is_cancelled=lambda: True)
        assert cancelled.get("cancelled") is True
        assert cancelled.get("plugins") == []
        ops._render_plugin_rows(window, {"plugins": []})
        empty = window.plugins_list_container.itemAt(0).widget()
        assert "暂无插件" in empty.title.text()
    finally:
        _dispose(window, qapp)


def test_failed_model_detail_exposes_next_actions(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        window.models = [core.ModelInfo("provider-a", "model-one", context="128000", thinking="yes")]
        window.test_results = {
            "provider-a/model-one": {
                "available": False,
                "error": "HTTP 401 unauthorized",
            }
        }
        window.fill_models_table()
        child = window.models_table.topLevelItem(0).child(0)
        window.models_table.setCurrentItem(child)
        qapp.processEvents()
        assert child.text(3) == "失败"
        # 模型页在 QStackedWidget 里，未切过去时 isVisible() 恒为 False；
        # isHidden() 才反映 setVisible 的自身状态。
        assert window.model_error_panel.isHidden() is False
        assert "401" in window.model_error_panel.reason.text()
        assert window.model_error_retry_btn.isHidden() is False
        assert "128K" in window.model_prop_table.value_text()
        assert window.model_detail_text.isHidden() is True
        window.toggle_model_raw()
        assert window.model_detail_text.isHidden() is False
    finally:
        _dispose(window, qapp)


def test_models_empty_state_and_collapsible_detail(qapp, isolated_home):
    window = ModernMainWindow(start_background=False)
    try:
        window.models = []
        window.fill_models_table()
        assert window.models_empty.isHidden() is False
        assert window.models_table.isHidden() is True
        assert "尚未发现模型" in window.models_empty.title.text()
        window.refresh_sessions()
        window.history_refresh()
        assert window.sessions_empty.isHidden() is False
        assert window.history_empty.isHidden() is False
        assert window.health_cancel_btn.isEnabled() is False
        assert window.model_detail_panel.isHidden() is False
        window.toggle_model_detail()
        assert window.model_detail_panel.isHidden() is True
        window.nav.set_collapsed(True)
        button = window.nav._buttons["simple"]
        assert "概览" in button.toolTip()
        assert button.accessibleName() == "概览"
    finally:
        _dispose(window, qapp)


def test_setup_wizard_is_stepped(qapp, isolated_home):
    from pi_manager.presentation.dialogs.setup import SetupWizardDialog

    dialog = SetupWizardDialog()
    try:
        assert dialog.stack.count() == 5
        assert dialog.STEPS[0] == "工作目录"
        assert dialog.btn_skip.isHidden() is False
        dialog._skip()
        assert dialog.stack.currentIndex() == 1
        assert hasattr(dialog, "_track")
        dialog.quick_base.clear()
        dialog._verify_key()
        assert "Base URL" in dialog.verify_status.text()
        assert dialog.btn_verify.isEnabled() is True
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_result_sheet_and_compact_day_window(qapp, isolated_home):
    from pi_manager.presentation.app import NAV_PAGES
    from pi_manager.presentation.geometry import (
        MIN_WINDOW_HEIGHT,
        MIN_WINDOW_WIDTH,
        clamp_dialog_to_screen,
    )

    original_font = QFont(qapp.font())
    window = ModernMainWindow(start_background=False)
    try:
        window.apply_ui_theme("day", "blue")
        assert "#F4F6F8" in qapp.styleSheet()
        assert window.minimumWidth() <= MIN_WINDOW_WIDTH
        assert window.minimumHeight() <= MIN_WINDOW_HEIGHT
        assert window.result_sheet.isHidden() is True
        window.show_result("导入成功", "已恢复：\n  · models.json", tone="success")
        qapp.processEvents()
        assert window.result_sheet.isHidden() is False
        assert "models.json" in window.result_sheet.body.toPlainText()
        window.result_sheet.dismiss()
        qapp.processEvents()
        assert window.result_sheet.isHidden() is True

        font = QFont(original_font)
        base = font.pointSizeF() if font.pointSizeF() > 0 else 9.0
        font.setPointSizeF(base * 1.25)
        qapp.setFont(font)
        window.show()
        qapp.processEvents()
        window.resize(MIN_WINDOW_WIDTH, MIN_WINDOW_HEIGHT)
        qapp.processEvents()
        assert window.width() == MIN_WINDOW_WIDTH
        assert window.height() == MIN_WINDOW_HEIGHT
        assert window.nav.is_collapsed() is True
        assert window.header_health_btn.isHidden() is True
        assert window.page_header.eyebrow.isHidden() is True
        assert core.load_manager_config().get("ui_nav_collapsed") in (None, False)
        window._settings_dirty = False
        for key, _title, _description in NAV_PAGES:
            window._goto_page(key)
            qapp.processEvents()
            assert window.page_header.title.isHidden() is False
            assert window.page_header.title.text()

        window.resize(1320, 880)
        qapp.processEvents()
        assert window.nav.is_collapsed() is False
        assert window.header_health_btn.isHidden() is False

        dialog = QDialog()
        clamp_dialog_to_screen(dialog, 4000, 4000)
        screen = qapp.primaryScreen()
        if screen is not None:
            avail = screen.availableGeometry()
            assert dialog.width() <= avail.width()
            assert dialog.height() <= avail.height()
        dialog.close()
        dialog.deleteLater()
    finally:
        qapp.setFont(original_font)
        _dispose(window, qapp)


def test_provider_editor_searches_upstream_and_saves_checked_only(qapp, isolated_home):
    from pi_manager.presentation.dialogs.providers import ProviderEditorDialog

    dialog = ProviderEditorDialog()
    try:
        dialog.name_edit.setText("xkiro")
        dialog.base_url.setText("https://api.xkiro.com/v1")
        dialog._on_fetch_done(
            {
                "ok": True,
                "models": [
                    {"id": "openai/gpt-5.6-terra"},
                    {"id": "qwen/qwen3-vl-plus:free"},
                    {"id": "minimax/minimax-m3:free"},
                ],
                "endpoint": "https://api.xkiro.com/v1/models",
            }
        )
        qapp.processEvents()
        assert dialog.picker.model_count() == 3
        assert dialog.picker.checked_ids() == set()
        dialog.picker.search.setText("qwen :free")
        qapp.processEvents()
        assert dialog.picker.list.count() == 1
        dialog.picker.check_visible()
        _name, data = dialog.result_data()
        assert [item["id"] for item in data["models"]] == ["qwen/qwen3-vl-plus:free"]
        written = data["models"][0]
        assert written["contextWindow"] == core.DEFAULT_CONTEXT_WINDOW
        assert written["reasoning"] is True
        assert written["input"] == ["text"]
        assert written["thinkingLevelMap"]["max"] == "max"
        dialog.picker.capability.image_check.setChecked(True)
        dialog.picker.capability.think_check.setChecked(True)
        applied = dialog.picker.apply_capabilities()
        assert applied == 1
        _name, data = dialog.result_data()
        assert data["models"][0]["input"] == ["text", "image"]
        assert data["models"][0]["reasoning"] is True
    finally:
        dialog.close()
        dialog.deleteLater()
        qapp.processEvents()


def test_capability_bar_defaults_to_1m_thinking_only(qapp, isolated_home):
    from pi_manager.presentation.components import ModelCapabilityBar

    bar = ModelCapabilityBar()
    try:
        spec = bar.capability_spec()
        assert spec["context_window"] == core.DEFAULT_CONTEXT_WINDOW
        assert spec["reasoning"] is True
        assert spec["images"] is False
        assert bar.context_combo.currentText() == "1M"
        assert "思考" in bar.summary_text()
    finally:
        bar.deleteLater()
        qapp.processEvents()
