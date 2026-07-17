"""Modern window shell layered over the stable Pi Manager behavior implementation.

The class deliberately reuses the mature command/configuration methods from the
legacy window while moving navigation, page composition, and visual state into a
separate presentation package. Pages can therefore migrate independently.
"""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import core, ui_theme
from ..ui import MainWindow as LegacyMainWindow
from ..ui import NAV_PAGES, Worker
from .components import AppButton, CollapsibleSection, NavigationRail, PageHeader
from .components.navigation import NavPage
from .design import apply_application_theme, normalize_accent, normalize_mode, tokens_for
from .design.icons import clear_icon_cache, icon
from .pages import (
    build_chat_page,
    build_dashboard_page,
    build_health_page,
    build_help_page,
    build_history_page,
    build_models_page,
    build_providers_page,
    build_sessions_page,
    build_settings_page,
    build_tools_page,
)


_PAGE_META = {
    "simple": ("home", "工作区"),
    "models": ("models", "工作区"),
    "providers": ("providers", "工作区"),
    "chat": ("chat", "工作区"),
    "sessions": ("sessions", "运行与诊断"),
    "health": ("health", "运行与诊断"),
    "history": ("history", "运行与诊断"),
    "tools": ("tools", "系统"),
    "settings": ("settings", "系统"),
    "help": ("help", "系统"),
}


class ModernMainWindow(LegacyMainWindow):
    """Modern shell with compatibility adapters for the existing behavior layer."""

    def _build_ui(self) -> None:
        self.setWindowTitle("Pi Manager")
        self.apply_ui_theme()
        central = QWidget()
        central.setObjectName("appRoot")
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)

        pages = [
            NavPage(key, title, description, _PAGE_META[key][0], _PAGE_META[key][1])
            for key, title, description in NAV_PAGES
        ]
        self.nav = NavigationRail(pages)
        self._page_keys = [page.key for page in pages]
        self.nav.pageChanged.connect(self._activate_page)
        self.nav.launchRequested.connect(self.launch_default)
        self.nav.refreshRequested.connect(self.refresh_all)
        self.nav.themeRequested.connect(self.toggle_ui_mode)
        self.nav.configRequested.connect(self.open_config_dir)
        self.nav.collapsedChanged.connect(self._persist_navigation_state)
        self.nav.set_collapsed(bool(self.mgr.get("ui_nav_collapsed", False)), emit=False)
        shell.addWidget(self.nav)

        content = QFrame()
        content.setObjectName("contentShell")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(0)

        self.page_header = PageHeader()
        self.page_heading = self.page_header.title
        self.page_subheading = self.page_header.description
        self.header_launch_btn = self._btn("启动 Pi", self.launch_default, success=True)
        self.header_launch_btn.setProperty("large", True)
        self.page_header.actions.addWidget(self.header_launch_btn)
        self.page_header.actions.addWidget(self._btn("自检", self.self_check_run, secondary=True))
        self.page_header.actions.addWidget(self._btn("健康检查", self.health_run_now, ghost=True))
        content_layout.addWidget(self.page_header)

        self.pages = QStackedWidget()
        self.pages.setObjectName("pages")
        self.tabs = self.pages
        builders = {
            "simple": self._build_dashboard_tab,
            "models": self._build_models_tab,
            "providers": self._build_providers_tab,
            "chat": self._build_chat_tab,
            "sessions": self._build_sessions_tab,
            "health": self._build_health_tab,
            "history": self._build_history_tab,
            "tools": self._build_tools_tab,
            "settings": self._build_settings_tab,
            "help": self._build_help_tab,
        }
        self._page_index: dict[str, int] = {}
        for key, _title, _description in NAV_PAGES:
            widget = builders[key]()
            widget.setProperty("pageKey", key)
            self._page_index[key] = self.pages.addWidget(widget)
        content_layout.addWidget(self.pages, 1)
        shell.addWidget(content, 1)

        self.status = QStatusBar()
        self.status.setSizeGripEnabled(False)
        self.setStatusBar(self.status)
        self.status.showMessage("就绪 · 配置、模型与会话彼此独立")
        self.nav.set_current_key("simple")
        # Initialize quick-chat from the default pair once. Later dashboard/default
        # refreshes must not overwrite an explicit chat-page selection.
        self.chat_fill_default()

    # ---- migrated page factories -------------------------------------------------
    def _build_dashboard_tab(self) -> QWidget:
        return build_dashboard_page(self)

    def _build_models_tab(self) -> QWidget:
        return build_models_page(self)

    def _build_providers_tab(self) -> QWidget:
        return build_providers_page(self)

    def _build_chat_tab(self) -> QWidget:
        return build_chat_page(self)

    def _build_sessions_tab(self) -> QWidget:
        return build_sessions_page(self)

    def _build_health_tab(self) -> QWidget:
        return build_health_page(self)

    def _build_history_tab(self) -> QWidget:
        return build_history_page(self)

    def _build_tools_tab(self) -> QWidget:
        return build_tools_page(self)

    def _build_settings_tab(self) -> QWidget:
        return build_settings_page(self)

    def _build_help_tab(self) -> QWidget:
        return build_help_page(self)

    # ---- shell/navigation --------------------------------------------------------
    def _activate_page(self, key: str) -> None:
        if key not in self._page_index:
            return
        self.pages.setCurrentIndex(self._page_index[key])
        title, description = next(
            ((title, desc) for page_key, title, desc in NAV_PAGES if page_key == key),
            ("", ""),
        )
        self.page_header.set_page(title, description)
        if key == "health":
            try:
                self.health_refresh_table()
            except Exception:
                pass
        elif key == "history":
            try:
                self.history_refresh()
            except Exception:
                pass

    def _on_nav_changed(self, row: int) -> None:
        if 0 <= row < len(self._page_keys):
            self.nav.set_current_key(self._page_keys[row])

    def _goto_page(self, key: str) -> None:
        if key in self._page_index:
            self.nav.set_current_key(key)

    def _persist_navigation_state(self, collapsed: bool) -> None:
        self.mgr["ui_nav_collapsed"] = bool(collapsed)
        try:
            self.persist_mgr()
        except Exception:
            pass

    # ---- component compatibility -------------------------------------------------
    def _card(self, *, elevated: bool = False) -> QFrame:
        frame = QFrame()
        frame.setObjectName("surfaceCard")
        frame.setProperty("elevated", elevated)
        return frame

    def _btn(
        self,
        text: str,
        slot,
        *,
        secondary: bool = False,
        danger: bool = False,
        success: bool = False,
        ghost: bool = False,
    ) -> AppButton:
        icon_name = self._button_icon(text)
        colors = tokens_for(*self._theme_pair())
        if danger and not secondary:
            icon_color = colors.danger
        elif secondary or ghost:
            icon_color = colors.text_muted
        else:
            icon_color = "#FFFFFF"
        return AppButton(
            text,
            slot,
            icon_name=icon_name,
            icon_color=icon_color,
            secondary=secondary,
            danger=danger,
            success=success,
            ghost=ghost,
        )

    @staticmethod
    def _button_icon(text: str) -> str | None:
        value = text.lower()
        pairs = (
            (("启动", "继续会话"), "rocket"),
            (("刷新", "重新"), "refresh"),
            (("浏览", "打开"), "folder"),
            (("添加", "新建"), "plus"),
            (("编辑",), "edit"),
            (("删除", "移除", "清空"), "trash"),
            (("key", "密钥"), "key"),
            (("收藏",), "star"),
            (("测试", "检查", "自检", "健康"), "activity"),
            (("默认", "保存", "应用", "确定"), "check"),
        )
        for needles, name in pairs:
            if any(needle in value for needle in needles):
                return name
        return None

    # ---- theme -------------------------------------------------------------------
    def _theme_pair(self) -> tuple[str, str]:
        value = core.get_ui_theme()
        return (
            normalize_mode(value.get("mode")),
            normalize_accent(value.get("accent")),
        )

    def apply_ui_theme(self, mode: str | None = None, accent: str | None = None) -> None:
        stored = core.get_ui_theme()
        mode_name = normalize_mode(mode or stored.get("mode"))
        accent_name = normalize_accent(accent or stored.get("accent"))
        if mode is not None or accent is not None:
            persisted = core.set_ui_theme(mode_name, accent_name)
            mode_name = normalize_mode(persisted.get("mode"))
            accent_name = normalize_accent(persisted.get("accent"))
        clear_icon_cache()
        app = QApplication.instance()
        if app is not None:
            apply_application_theme(app, mode_name, accent_name)
        if hasattr(self, "nav"):
            self.nav.update_icons(mode_name, accent_name)
        self._refresh_dynamic_button_icons(mode_name, accent_name)
        for section in self.findChildren(CollapsibleSection):
            section.refresh_theme(mode_name, accent_name)
        if hasattr(self, "model_more_button"):
            colors = tokens_for(mode_name, accent_name)
            self.model_more_button.setIcon(icon("ellipsis", colors.text_muted, 17))
        if hasattr(self, "models_table"):
            self._apply_model_table_colors()
            self._refresh_model_status_colors()
        if hasattr(self, "set_ui_mode"):
            for index in range(self.set_ui_mode.count()):
                if self.set_ui_mode.itemData(index) == mode_name:
                    self.set_ui_mode.setCurrentIndex(index)
                    break
            for index in range(self.set_ui_accent.count()):
                if self.set_ui_accent.itemData(index) == accent_name:
                    self.set_ui_accent.setCurrentIndex(index)
                    break
        try:
            self.refresh_help_theme(mode_name)
        except Exception:
            pass
        if hasattr(self, "status") and self.status is not None:
            cli_theme = core.cli_theme_for_ui_mode(mode_name)
            self.status.showMessage(
                f"\u5168\u5c40\u4e3b\u9898\uff1a{ui_theme.MODE_LABELS.get(mode_name, mode_name)} / "
                f"{ui_theme.ACCENT_LABELS.get(accent_name, accent_name)}\uff1bPi CLI {cli_theme}"
            )

    def _refresh_dynamic_button_icons(self, mode: str, accent: str) -> None:
        for button in self.findChildren(AppButton):
            button.refresh_theme(mode, accent)

    # ---- dashboard view model adapters -------------------------------------------
    def refresh_dashboard(self) -> None:
        provider, model, thinking = core.get_default_model()
        self.lbl_current.setText(f"{provider}/{model}" if provider else "尚未设置默认模型")
        self.lbl_thinking.setText(f"Thinking level · {thinking or '未设置'}")
        self.default_status_badge.set_status("success" if provider and model else "warning")
        if getattr(self, "_background_enabled", True):
            worker = self._track(Worker(core.get_pi_version))
            worker.done.connect(self._set_pi_version)
            worker.failed.connect(lambda error: self._set_pi_version(f"不可用 · {error}", failed=True))
            worker.start()
        rows = core.auth_summary()
        self.auth_table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            from PySide6.QtWidgets import QTableWidgetItem
            self.auth_table.setItem(index, 0, QTableWidgetItem(row["provider"]))
            self.auth_table.setItem(index, 1, QTableWidgetItem(row["status"]))
        self.dashboard_auth_metric.value_label.setText(str(len(rows)))
        try:
            providers = (core.load_models_config().get("providers") or {})
            self.dashboard_provider_metric.value_label.setText(str(len(providers)))
        except Exception:
            self.dashboard_provider_metric.value_label.setText("—")
        self.fill_favorites()

    def _set_pi_version(self, value: Any, *, failed: bool = False) -> None:
        text = str(value or "未知")
        self.version_pill.setText(text)
        if hasattr(self, "nav"):
            self.nav.set_version(f"pi: {text}")
        if failed:
            self.version_pill.setToolTip(text)

    def fill_favorites(self) -> None:
        super().fill_favorites()
        if hasattr(self, "dashboard_favorite_metric"):
            self.dashboard_favorite_metric.value_label.setText(str(self.fav_list.count()))

    # ---- model view model adapters -----------------------------------------------
    def fill_models_table(self) -> None:
        super().fill_models_table()
        self._apply_model_table_colors()
        self._on_model_selection_changed()

    def _apply_model_table_colors(self) -> None:
        if not hasattr(self, "models_table"):
            return
        colors = tokens_for(*self._theme_pair())
        try:
            default_provider, default_model, _ = core.get_default_model()
        except Exception:
            default_provider, default_model = "", ""
        default_key = f"{default_provider}/{default_model}" if default_provider and default_model else ""
        for row in range(self.models_table.rowCount()):
            name_item = self.models_table.item(row, 0)
            provider_item = self.models_table.item(row, 1)
            if name_item is not None:
                data = name_item.data(Qt.UserRole) or []
                key = f"{data[0]}/{data[1]}" if len(data) >= 2 else ""
                name_item.setForeground(QColor(colors.accent_text if key == default_key else colors.text))
            if provider_item is not None:
                provider_item.setForeground(QColor(colors.text_muted))

    def _model_status_cells(self, m: core.ModelInfo):
        status_item, latency_item = super()._model_status_cells(m)
        colors = tokens_for(*self._theme_pair())
        result = self.test_results.get(m.key)
        if not result:
            status_item.setForeground(QColor(colors.text_muted))
            latency_item.setForeground(QColor(colors.text_muted))
            return status_item, latency_item
        if result.get("pending"):
            status_item.setForeground(QColor(colors.warning))
            latency_item.setForeground(QColor(colors.warning))
            return status_item, latency_item
        available = result.get("available")
        status_item.setForeground(
            QColor(
                colors.success
                if available is True
                else colors.danger
                if available is False
                else colors.text_muted
            )
        )
        latency = result.get("latency_ms")
        if isinstance(latency, (int, float)):
            latency_item.setForeground(
                QColor(
                    colors.success
                    if latency < 800
                    else colors.warning
                    if latency < 2000
                    else colors.danger
                )
            )
        else:
            latency_item.setForeground(QColor(colors.text_muted))
        return status_item, latency_item

    def _refresh_model_status_colors(self) -> None:
        if not hasattr(self, "models_table"):
            return
        by_key = {model.key: model for model in self.models}
        for row in range(self.models_table.rowCount()):
            name_item = self.models_table.item(row, 0)
            data = name_item.data(Qt.UserRole) if name_item is not None else None
            if not isinstance(data, (list, tuple)) or len(data) < 2:
                continue
            model = by_key.get(f"{data[0]}/{data[1]}")
            if model is None:
                continue
            status_color, latency_color = self._model_status_cells(model)
            current_status = self.models_table.item(row, 3)
            current_latency = self.models_table.item(row, 4)
            if current_status is not None:
                current_status.setForeground(status_color.foreground())
            if current_latency is not None:
                current_latency.setForeground(latency_color.foreground())

    def _on_model_selection_changed(self) -> None:
        if not hasattr(self, "model_detail_title"):
            return
        info = self.selected_model_row()
        if not info:
            self.model_detail_title.setText("选择一个模型")
            self.model_detail_provider.setText("—")
            self.model_detail_badge.set_status("neutral", "未选择")
            self.model_detail_text.setPlainText("选择模型后显示配置与测试状态。")
            return
        provider, model = info.provider, info.model
        self.model_detail_title.setText(model)
        self.model_detail_provider.setText(provider)
        result = self.test_results.get(f"{provider}/{model}") or {}
        if result.get("ok"):
            self.model_detail_badge.set_status("success", "连接正常")
        elif result:
            self.model_detail_badge.set_status("danger", "连接失败")
        else:
            self.model_detail_badge.set_status("info", "尚未测试")
        payload = {
            "provider": provider,
            "model": model,
            "context": getattr(info, "context", None),
            "thinking": getattr(info, "thinking", None),
            "images": getattr(info, "images", None),
            "test": result or None,
        }
        self.model_detail_text.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))

    # ---- health view model adapters ----------------------------------------------
    def _on_health_progress(self, result: dict) -> None:
        super()._on_health_progress(result)
        if hasattr(self, "health_status_badge"):
            done = int(getattr(self, "_health_done", 0))
            ok_count = int(getattr(self, "_health_ok", 0))
            self.health_status_badge.set_status("info", f"检查中 · {ok_count}/{done} 可用")

    def _on_health_fail(self, error: str) -> None:
        if hasattr(self, "health_status_badge"):
            self.health_status_badge.set_status("danger", "检查失败")
        super()._on_health_fail(error)

    def _on_health_done(self, result: dict, show_dialog: bool) -> None:
        if hasattr(self, "health_status_badge"):
            rows = result.get("results") or []
            ok_count = sum(1 for row in rows if row.get("available"))
            if not result.get("ok") and result.get("error"):
                self.health_status_badge.set_status("danger", "检查失败")
            elif rows and ok_count == len(rows):
                self.health_status_badge.set_status("success", f"全部可用 · {ok_count}/{len(rows)}")
            elif rows:
                self.health_status_badge.set_status("warning", f"部分可用 · {ok_count}/{len(rows)}")
            else:
                self.health_status_badge.set_status("neutral", "无检查项")
        super()._on_health_done(result, show_dialog)

    def health_refresh_table(self) -> None:
        super().health_refresh_table()
        if not hasattr(self, "health_status_badge") or getattr(self, "_health_running", False):
            return
        total = self.health_table.rowCount()
        if not total:
            self.health_status_badge.set_status("neutral", "暂无本地结果")
            return
        ok_count = sum(
            1
            for row in range(total)
            if self.health_table.item(row, 1) is not None
            and self.health_table.item(row, 1).text() == "可用"
        )
        tone = "success" if ok_count == total else "warning"
        self.health_status_badge.set_status(tone, f"本地结果 · {ok_count}/{total} 可用")

    # ---- provider view model adapters --------------------------------------------
    def refresh_providers(self) -> None:
        current = self.provider_list.currentItem().text() if self.provider_list.currentItem() else ""
        super().refresh_providers()
        count = self.provider_list.count()
        if hasattr(self, "provider_summary_badge"):
            self.provider_summary_badge.set_status("info", f"{count} 个 Provider")
        if current:
            matches = self.provider_list.findItems(current, Qt.MatchExactly)
            if matches:
                self.provider_list.setCurrentItem(matches[0])
        elif count:
            self.provider_list.setCurrentRow(0)
        else:
            self.provider_detail_title.setText("尚无自定义 Provider")
            self.provider_key_badge.set_status("warning", "等待配置")

    def on_provider_selected(self, current, previous) -> None:
        super().on_provider_selected(current, previous)
        if not current or not hasattr(self, "provider_detail_title"):
            return
        name = current.text()
        self.provider_detail_title.setText(name)
        keys = core.list_provider_api_keys(name)
        available = sum(1 for item in keys if item.get("status") == "available")
        invalid = sum(1 for item in keys if item.get("status") == "invalid")
        if invalid:
            self.provider_key_badge.set_status("danger", f"{invalid} 个失效 · {available} 个可用")
        elif available:
            self.provider_key_badge.set_status("success", f"{available} 个 Key 可用")
        else:
            self.provider_key_badge.set_status("warning", "尚未配置 Key")
