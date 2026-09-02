"""主窗口导航壳：侧栏、页堆、快捷键、按钮与主题应用。"""
from __future__ import annotations

import logging

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QStackedWidget,
    QStatusBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .. import core
from .app import NAV_PAGES
from .components import (
    AppButton,
    CollapsibleSection,
    FeedbackToast,
    NavigationRail,
    PageHeader,
    ResultSheet,
)
from .geometry import COMPACT_WINDOW_WIDTH, clamp_dialog_to_screen
from .components.navigation import NavPage
from .design import (
    ACCENT_LABELS,
    MODE_LABELS,
    apply_application_theme,
    normalize_accent,
    normalize_mode,
    tokens_for,
)
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

logger = logging.getLogger(__name__)

_PAGE_META = {
    "simple": ("home", "概览"),
    "providers": ("providers", "配置"),
    "models": ("models", "配置"),
    "tools": ("tools", "配置"),
    "plugins": ("plugins", "配置"),
    "chat": ("chat", "运行"),
    "sessions": ("sessions", "运行"),
    "health": ("health", "运行"),
    "history": ("history", "运行"),
    "settings": ("settings", "系统"),
    "help": ("help", "系统"),
}


# 页头标题覆盖：侧边栏标签求短，页头求完整。以前插件页靠 _bind_page_title
# 再挂一个 nav.pageChanged 槽来覆盖标题，正确性依赖两个槽的连接顺序（隐式
# 契约）；改为在 _activate_page 内部一次查表，顺序依赖消失。
_PAGE_HEADINGS = {
    "simple": ("概览", "确认当前默认模型是否可用，然后启动 Pi。"),
    "plugins": ("插件管理", "内置 skills / extensions 与用户自定义插件的统一管理。"),
}


class WindowChromeMixin:
    """侧栏、页堆、快捷键、按钮工厂与主题应用。"""


    def _build_ui(self) -> None:
        self._compact_layout = False
        self._nav_auto_collapsed = False
        self.setWindowTitle("Pi Manager")
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
        self.header_selfcheck_btn = self._btn("自检", self.self_check_run, secondary=True)
        self.page_header.actions.addWidget(self.header_selfcheck_btn)
        self.header_health_btn = self._btn("健康检查", self.health_run_now, ghost=True)
        self.page_header.actions.addWidget(self.header_health_btn)
        content_layout.addWidget(self.page_header)

        self.result_sheet = ResultSheet()
        content_layout.addWidget(self.result_sheet)

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
            "plugins": self._build_plugins_tab,
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
        self.feedback_toast = FeedbackToast(content)

        self.status = QStatusBar()
        self.status.setSizeGripEnabled(False)
        self.setStatusBar(self.status)
        self.status.showMessage("就绪 · 配置、模型与会话彼此独立")
        self.update_indicator = QToolButton()
        self.update_indicator.setObjectName("updateIndicator")
        self.update_indicator.setCursor(Qt.PointingHandCursor)
        self.update_indicator.setToolTip("更新状态 · 点击重新检查")
        self.update_indicator.setFixedSize(24, 24)
        self.update_indicator.setIconSize(QSize(16, 16))
        self.update_indicator.clicked.connect(self.check_all_updates)
        self.status.addPermanentWidget(self.update_indicator)
        self.update_indicator.setAccessibleName("更新状态")
        self.nav.set_current_key("simple")
        self._install_navigation_shortcuts()
        # 所有 widget 已创建，现在应用主题（此前各 widget 已用 _theme_pair 取色构建，
        # 此处统一刷新图标/样式表/状态栏主题文案）。
        self.apply_ui_theme()
        self._adapt_compact_layout()
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

    def _build_plugins_tab(self) -> QWidget:
        from .pages.plugins import build_plugins_page

        return build_plugins_page(self)

    def _build_settings_tab(self) -> QWidget:
        return build_settings_page(self)

    def _build_help_tab(self) -> QWidget:
        return build_help_page(self)

    # ---- shell/navigation --------------------------------------------------------
    def _install_navigation_shortcuts(self) -> None:
        """键盘可达性：11 个导航页此前只能鼠标点击切换。

        Ctrl+1..9 直达前 9 页，Ctrl+Tab / Ctrl+Shift+Tab 循环切换。
        """
        from PySide6.QtGui import QKeySequence, QShortcut

        self._nav_shortcuts = []
        for position, key in enumerate(self._page_keys[:9], start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{position}"), self)
            shortcut.activated.connect(lambda k=key: self._goto_page(k))
            self._nav_shortcuts.append(shortcut)
        for sequence, step in (
            (QKeySequence("Ctrl+Tab"), 1),
            (QKeySequence("Ctrl+Shift+Tab"), -1),
        ):
            shortcut = QShortcut(sequence, self)
            shortcut.activated.connect(lambda delta=step: self._cycle_page(delta))
            self._nav_shortcuts.append(shortcut)
        jump = QShortcut(QKeySequence("Ctrl+K"), self)
        jump.activated.connect(self._open_jump_dialog)
        self._nav_shortcuts.append(jump)
        save = QShortcut(QKeySequence("Ctrl+S"), self)
        save.activated.connect(self._shortcut_save)
        self._nav_shortcuts.append(save)
        find = QShortcut(QKeySequence("Ctrl+F"), self)
        find.activated.connect(self._shortcut_find)
        self._nav_shortcuts.append(find)
        test = QShortcut(QKeySequence("Ctrl+Return"), self)
        test.activated.connect(self._shortcut_test)
        self._nav_shortcuts.append(test)

    def _open_jump_dialog(self) -> None:
        from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QVBoxLayout

        dialog = QDialog(self)
        dialog.setWindowTitle("快速跳转")
        clamp_dialog_to_screen(dialog, 360, 420)
        root = QVBoxLayout(dialog)
        search = QLineEdit()
        search.setPlaceholderText("输入页面名称…")
        root.addWidget(search)
        listing = QListWidget()
        for key, title, description in NAV_PAGES:
            listing.addItem(f"{title}  ·  {description}")
            listing.item(listing.count() - 1).setData(Qt.UserRole, key)
        root.addWidget(listing, 1)

        def apply_filter(text: str) -> None:
            needle = (text or "").strip().lower()
            for row in range(listing.count()):
                item = listing.item(row)
                item.setHidden(bool(needle) and needle not in item.text().lower())

        def jump_current() -> None:
            item = listing.currentItem()
            if item is None:
                for row in range(listing.count()):
                    candidate = listing.item(row)
                    if candidate is not None and not candidate.isHidden():
                        item = candidate
                        break
            if item is None:
                return
            key = str(item.data(Qt.UserRole) or "")
            dialog.accept()
            if key:
                self._goto_page(key)

        search.textChanged.connect(apply_filter)
        search.returnPressed.connect(jump_current)
        listing.itemActivated.connect(lambda _item: jump_current())
        listing.setCurrentRow(0)
        search.setFocus()
        dialog.exec()

    def notify_success(self, text: str, *, ms: int = 3500) -> None:
        self.status.showMessage(text, ms)
        toast = getattr(self, "feedback_toast", None)
        if toast is not None:
            toast.show_message(text, "success", ms)

    def notify_warning(self, text: str, *, ms: int = 5000) -> None:
        self.status.showMessage(text, ms)
        toast = getattr(self, "feedback_toast", None)
        if toast is not None:
            toast.show_message(text, "warning", ms)

    def notify_error(self, text: str, *, ms: int = 7000) -> None:
        self.status.showMessage(text, ms)
        toast = getattr(self, "feedback_toast", None)
        if toast is not None:
            toast.show_message(text, "danger", ms)

    def notify_info(self, text: str, *, ms: int = 4000) -> None:
        self.status.showMessage(text, ms)
        toast = getattr(self, "feedback_toast", None)
        if toast is not None:
            toast.show_message(text, "info", ms)

    def show_result(self, title: str, body: str = "", *, tone: str = "success") -> None:
        """多行操作结果走页内面板，避免挡住当前页的长对话框。"""
        self.status.showMessage(title)
        sheet = getattr(self, "result_sheet", None)
        if sheet is not None:
            sheet.show_result(title, body or title, tone)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        adapt = getattr(self, "_adapt_compact_layout", None)
        if callable(adapt):
            adapt()

    def _adapt_compact_layout(self) -> None:
        """窄窗口收起侧栏、压缩页头；自动折叠不写入 ui_nav_collapsed。"""
        if self.width() <= 1:
            return
        nav = getattr(self, "nav", None)
        header = getattr(self, "page_header", None)
        if nav is None or header is None:
            return
        compact = self.width() < COMPACT_WINDOW_WIDTH
        was_compact = bool(getattr(self, "_compact_layout", False))
        self._compact_layout = compact
        header.set_compact(compact)
        health_btn = getattr(self, "header_health_btn", None)
        if health_btn is not None:
            health_btn.setVisible(not compact)
        if compact and not was_compact:
            if not nav.is_collapsed():
                self._nav_auto_collapsed = True
                nav.set_collapsed(True, emit=False)
        elif not compact and was_compact:
            if getattr(self, "_nav_auto_collapsed", False):
                self._nav_auto_collapsed = False
                user_pref = bool((getattr(self, "mgr", None) or {}).get("ui_nav_collapsed", False))
                if not user_pref and nav.is_collapsed():
                    nav.set_collapsed(False, emit=False)

    def _set_action_busy(
        self, button, busy: bool, *, idle: str, busy_text: str
    ) -> None:
        if button is None:
            return
        button.setEnabled(not busy)
        button.setText(busy_text if busy else idle)

    def _shortcut_save(self) -> None:
        if getattr(self, "_active_page_key", "") == "settings" or self.nav.current_key() == "settings":
            self.settings_save()
        else:
            try:
                self.persist_mgr()
                self.notify_success("当前工作目录与启动选项已保存")
            except Exception:
                pass

    def _shortcut_find(self) -> None:
        key = getattr(self, "_active_page_key", "") or self.nav.current_key()
        mapping = {
            "models": "model_filter",
            "sessions": "session_filter_wd",
            "history": "history_filter",
            "simple": "quick_name",
        }
        widget = getattr(self, mapping.get(key, ""), None)
        if widget is not None:
            widget.setFocus()

    def _shortcut_test(self) -> None:
        key = getattr(self, "_active_page_key", "") or self.nav.current_key()
        if key == "models":
            self.model_test_selected()

    def _cycle_page(self, delta: int) -> None:
        keys = getattr(self, "_page_keys", None) or []
        if not keys:
            return
        try:
            current = keys.index(self.nav.current_key())
        except ValueError:
            current = 0
        self._goto_page(keys[(current + delta) % len(keys)])

    def _activate_page(self, key: str) -> None:
        if key not in self._page_index:
            return
        previous = getattr(self, "_active_page_key", "")
        if (
            previous == "settings"
            and key != "settings"
            and hasattr(self, "confirm_leave_settings")
            and not self.confirm_leave_settings()
        ):
            self.nav.set_current_key("settings", emit=False)
            return
        self._active_page_key = key
        self.pages.setCurrentIndex(self._page_index[key])
        title, description = next(
            ((title, desc) for page_key, title, desc in NAV_PAGES if page_key == key),
            ("", ""),
        )
        # 页头标题可与侧边栏短标签不同（侧栏要短，页头要完整）。
        title, description = _PAGE_HEADINGS.get(key, (title, description))
        self.page_header.set_page(title, description)
        if key == "health":
            try:
                self.health_refresh_table()
            except Exception as e:
                logger.warning("health table refresh on page switch failed: %s", e)
        elif key == "history":
            try:
                self.history_refresh()
            except Exception as e:
                logger.warning("history refresh on page switch failed: %s", e)
        elif key == "chat":
            try:
                self._refresh_chat_context()
            except Exception:
                pass
        elif key == "plugins" and getattr(self, "_background_enabled", True):
            # 构造期不再扫描插件（start_background 契约）；首次进入该页时补扫。
            try:
                from .pages.plugins import refresh_plugins_page

                refresh_plugins_page(self, only_if_empty=True)
            except Exception as e:
                logger.warning("lazy plugin scan failed: %s", e)

    def _on_nav_changed(self, row: int) -> None:
        if 0 <= row < len(self._page_keys):
            self.nav.set_current_key(self._page_keys[row])

    def _goto_page(self, key: str) -> None:
        if key in self._page_index:
            self.nav.set_current_key(key)

    def _persist_navigation_state(self, collapsed: bool) -> None:
        try:
            self.persist_mgr(ui_nav_collapsed=bool(collapsed))
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
        # 走基类的 token 缓存：_btn() 在构造期被调用数十次，_update_colors /
        # _apply_model_table_colors 等也高频调用，不必每次都读配置。
        colors = self._table_colors()
        return normalize_mode(colors.mode), normalize_accent(colors.accent_name)

    def apply_ui_theme(self, mode: str | None = None, accent: str | None = None) -> None:
        stored = core.get_ui_theme()
        mode_name = normalize_mode(mode or stored.get("mode"))
        accent_name = normalize_accent(accent or stored.get("accent"))
        if mode is not None or accent is not None:
            persisted = core.set_ui_theme(mode_name, accent_name)
            mode_name = normalize_mode(persisted.get("mode"))
            accent_name = normalize_accent(persisted.get("accent"))
        clear_icon_cache()
        # 主题已确定：让模型表的 token 缓存失效（见 MainWindow._table_colors）。
        self.invalidate_theme_cache()
        app = QApplication.instance()
        if app is not None:
            apply_application_theme(app, mode_name, accent_name)
        self.nav.update_icons(mode_name, accent_name)
        # Stylesheet is (re)applied above; the nav footer height was fixed before
        # the theme existed, so recompute it now that buttons are polished.
        try:
            self.nav._apply_footer_layout()
        except Exception:
            pass
        if hasattr(self, "update_indicator") and hasattr(self, "_update_indicator_state"):
            self._set_update_indicator(self._update_indicator_state)
        self._refresh_dynamic_button_icons(mode_name, accent_name)
        for section in self.findChildren(CollapsibleSection):
            section.refresh_theme(mode_name, accent_name)
        colors = tokens_for(mode_name, accent_name)
        self.model_more_button.setIcon(icon("ellipsis", colors.text_muted, 17))
        self._apply_model_table_colors()
        self._refresh_model_status_colors()
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
        cli_theme = core.cli_theme_for_ui_mode(mode_name)
        self.status.showMessage(
            f"\u5168\u5c40\u4e3b\u9898\uff1a{MODE_LABELS.get(mode_name, mode_name)} / "
            f"{ACCENT_LABELS.get(accent_name, accent_name)}\uff1bPi CLI {cli_theme}"
            )

    def _refresh_dynamic_button_icons(self, mode: str, accent: str) -> None:
        for button in self.findChildren(AppButton):
            button.refresh_theme(mode, accent)
