"""Collapsible grouped navigation rail for Pi Manager."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..design.icons import icon
from ..design.tokens import tokens_for


@dataclass(frozen=True, slots=True)
class NavPage:
    key: str
    title: str
    description: str
    icon_name: str
    group: str


class NavigationRail(QFrame):
    pageChanged = Signal(str)
    currentRowChanged = Signal(int)
    collapsedChanged = Signal(bool)
    launchRequested = Signal()
    refreshRequested = Signal()
    themeRequested = Signal()
    configRequested = Signal()

    EXPANDED_WIDTH = 244
    COLLAPSED_WIDTH = 76

    def __init__(self, pages: list[NavPage], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("navRail")
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        self._pages = list(pages)
        self._buttons: dict[str, QToolButton] = {}
        self._badges: dict[str, QLabel] = {}
        self._group_labels: list[QLabel] = []
        self._current_key = ""
        self._collapsed = False
        self._mode = "night"
        self._accent = "blue"
        self._build()
        self.set_collapsed(False, emit=False)

    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 14, 12, 12)
        root.setSpacing(8)

        brand = QFrame()
        brand.setObjectName("brandPanel")
        brand_row = QHBoxLayout(brand)
        brand_row.setContentsMargins(4, 2, 2, 8)
        brand_row.setSpacing(10)
        self.brand_mark = QLabel("π")
        self.brand_mark.setObjectName("brandMark")
        self.brand_mark.setAlignment(Qt.AlignCenter)
        self.brand_mark.setFixedSize(34, 34)
        brand_row.addWidget(self.brand_mark)
        self.brand_copy = QWidget()
        copy_layout = QVBoxLayout(self.brand_copy)
        copy_layout.setContentsMargins(0, 0, 0, 0)
        copy_layout.setSpacing(0)
        brand_title = QLabel("Pi Manager")
        brand_title.setObjectName("navBrand")
        brand_tag = QLabel("AI CLI Control Center")
        brand_tag.setObjectName("navTag")
        copy_layout.addWidget(brand_title)
        copy_layout.addWidget(brand_tag)
        brand_row.addWidget(self.brand_copy, 1)
        self.collapse_button = QToolButton()
        self.collapse_button.setObjectName("navToggle")
        self.collapse_button.setCursor(Qt.PointingHandCursor)
        self.collapse_button.setToolTip("收起侧边栏")
        self.collapse_button.setAccessibleName("折叠侧边栏")
        self.collapse_button.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        brand_row.addWidget(self.collapse_button)
        root.addWidget(brand)

        # Navigation items live in a scroll area so the rail footer always stays
        # visible: when the window is short, menu items scroll instead of the
        # footer (launch button + tool row) being compressed or clipped.
        self.nav_scroll = QScrollArea()
        self.nav_scroll.setObjectName("navScroll")
        self.nav_scroll.setWidgetResizable(True)
        self.nav_scroll.setFrameShape(QFrame.NoFrame)
        self.nav_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.nav_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        nav_host = QWidget()
        nav_host.setObjectName("navHost")
        nav_layout = QVBoxLayout(nav_host)
        nav_layout.setContentsMargins(0, 0, 4, 0)
        nav_layout.setSpacing(8)

        current_group = None
        for page in self._pages:
            if page.group != current_group:
                current_group = page.group
                label = QLabel(current_group.upper())
                label.setObjectName("navSection")
                self._group_labels.append(label)
                nav_layout.addWidget(label)
            button = QToolButton()
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setIconSize(QSize(18, 18))
            button.setText(page.title)
            button.setToolTip(f"{page.title}\n{page.description}")
            button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            button.clicked.connect(lambda checked=False, key=page.key: self.set_current_key(key))
            self._buttons[page.key] = button
            # Badge floats on the button's top-right corner; it must never take
            # layout space, otherwise nav text would truncate and shifting the
            # rail would look broken.
            badge = QLabel("")
            badge.setObjectName("navBadge")
            badge.setFixedSize(8, 8)
            badge.setVisible(False)
            badge.setParent(button)
            button.setProperty("navBadge", badge)
            button.installEventFilter(self)
            self._badges[page.key] = badge
            # Horizontal: Expanding policy fills the rail width uniformly;
            # vertical centering keeps every tab aligned to the same midline.
            nav_layout.addWidget(button, 0, Qt.AlignVCenter)
        nav_layout.addStretch(1)
        self.nav_scroll.setWidget(nav_host)
        root.addWidget(self.nav_scroll, 1)

        self.footer = QFrame()
        self.footer.setObjectName("sidebarFooter")
        footer = QVBoxLayout(self.footer)
        footer.setContentsMargins(9, 9, 9, 9)
        footer.setSpacing(7)
        self.launch_button = QPushButton("启动 Pi")
        self.launch_button.setProperty("success", True)
        self.launch_button.setProperty("large", True)
        self.launch_button.setCursor(Qt.PointingHandCursor)
        self.launch_button.clicked.connect(self.launchRequested)
        footer.addWidget(self.launch_button)
        utility = QHBoxLayout()
        utility.setSpacing(4)
        self.refresh_button = self._utility_button("刷新全部", self.refreshRequested)
        self.theme_button = self._utility_button("切换昼夜", self.themeRequested)
        self.config_button = self._utility_button("打开配置目录", self.configRequested)
        utility.addWidget(self.refresh_button)
        utility.addWidget(self.theme_button)
        utility.addWidget(self.config_button)
        footer.addLayout(utility)
        self.version_label = QLabel("pi: 检查中")
        self.version_label.setObjectName("versionPill")
        self.version_label.setAlignment(Qt.AlignCenter)
        # 固定单行高度：footer 高度用常量算（见 _apply_footer_layout 注释），
        # 换行会让版本标签溢出 footer 边界。长文本靠 tooltip 兜底。
        self.version_label.setWordWrap(False)
        self.version_label.setFixedHeight(20)
        footer.addWidget(self.version_label)
        root.addWidget(self.footer)
        self.update_icons()

    def _utility_button(self, tooltip: str, signal: Signal) -> QToolButton:
        button = QToolButton()
        button.setObjectName("iconButton")
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(tooltip)
        # 纯图标按钮：屏幕阅读器拿不到任何名称，直接复用 tooltip 文案。
        button.setAccessibleName(tooltip)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        button.setFixedHeight(32)
        button.clicked.connect(signal)
        return button

    def update_icons(self, mode: str | None = None, accent: str | None = None) -> None:
        self._mode = mode or self._mode
        self._accent = accent or self._accent
        colors = tokens_for(self._mode, self._accent)
        for page in self._pages:
            selected = page.key == self._current_key
            color = colors.accent_text if selected else colors.text_muted
            self._buttons[page.key].setIcon(icon(page.icon_name, color, 18))
        self.collapse_button.setIcon(
            icon("chevron-right" if self._collapsed else "chevron-left", colors.text_muted, 18)
        )
        self.launch_button.setIcon(icon("rocket", "#FFFFFF", 17))
        self.refresh_button.setIcon(icon("refresh", colors.text_muted, 17))
        self.theme_button.setIcon(
            icon("sun" if self._mode == "night" else "moon", colors.text_muted, 17)
        )
        self.config_button.setIcon(icon("folder", colors.text_muted, 17))

    def set_current_key(self, key: str, *, emit: bool = True) -> None:
        if key not in self._buttons:
            return
        changed = key != self._current_key
        self._current_key = key
        self._buttons[key].setChecked(True)
        self.update_icons()
        if changed and emit:
            row = self._key_index(key)
            self.pageChanged.emit(key)
            self.currentRowChanged.emit(row)

    def current_key(self) -> str:
        return self._current_key

    def setCurrentRow(self, row: int) -> None:  # QListWidget compatibility
        if 0 <= row < len(self._pages):
            self.set_current_key(self._pages[row].key)

    def currentRow(self) -> int:  # QListWidget compatibility
        return self._key_index(self._current_key)

    def _key_index(self, key: str) -> int:
        for index, page in enumerate(self._pages):
            if page.key == key:
                return index
        return -1

    def set_collapsed(self, collapsed: bool, *, emit: bool = True) -> None:
        self._collapsed = bool(collapsed)
        self.setFixedWidth(self.COLLAPSED_WIDTH if self._collapsed else self.EXPANDED_WIDTH)
        self.brand_copy.setVisible(not self._collapsed)
        for label in self._group_labels:
            label.setVisible(not self._collapsed)
        for button in self._buttons.values():
            button.setToolButtonStyle(
                Qt.ToolButtonIconOnly if self._collapsed else Qt.ToolButtonTextBesideIcon
            )
            button.setIconSize(QSize(20 if self._collapsed else 18, 20 if self._collapsed else 18))
        self.collapse_button.setToolTip("展开侧边栏" if self._collapsed else "收起侧边栏")
        self.launch_button.setText("" if self._collapsed else "启动 Pi")
        self.launch_button.setToolTip("启动完整 Pi")
        self._apply_footer_layout()
        self.update_icons()
        if emit:
            self.collapsedChanged.emit(self._collapsed)

    def _apply_footer_layout(self) -> None:
        """Rebuild the footer items for expanded / collapsed rail.

        The root layout object stays the same (replacing it via setLayout is
        unreliable with deferred deletes); only its items change. Collapsed rail
        is only ~38px wide internally, so the three utility icon buttons are
        stacked vertically instead of side by side to avoid overlapping icons.
        """
        layout = self.footer.layout()
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            child = item.layout()
            if child is not None:
                child.deleteLater()
            widget = item.widget()
            if widget is not None and widget not in (
                self.launch_button,
                self.refresh_button,
                self.theme_button,
                self.config_button,
                self.version_label,
            ):
                widget.deleteLater()
        if self._collapsed:
            layout.setContentsMargins(7, 9, 7, 9)
            layout.setSpacing(6)
            # 折叠态内部只有 ~38px 宽，放不下版本文本；版本仍可从 launch_button
            # 的 tooltip 看到（见 set_version）。
            self.version_label.setVisible(False)
            layout.addWidget(self.launch_button)
            for button in (self.refresh_button, self.theme_button, self.config_button):
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                button.setIconSize(QSize(18, 18))
                button.setFixedHeight(32)
                layout.addWidget(button)
        else:
            layout.setContentsMargins(9, 9, 9, 9)
            layout.setSpacing(7)
            # 展开态恢复显示 pi 版本：_apply_footer_layout 曾用 takeAt(0) 清空
            # footer 后没有把 version_label 重新 addWidget 回去，且两个分支都
            # setVisible(False)，导致 set_version() 写入的文本永远不可见。
            self.version_label.setVisible(True)
            layout.addWidget(self.launch_button)
            utility = QHBoxLayout()
            utility.setSpacing(4)
            for button in (self.refresh_button, self.theme_button, self.config_button):
                button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
                button.setIconSize(QSize(18, 18))
                button.setFixedHeight(32)
                utility.addWidget(button)
            layout.addLayout(utility)
            layout.addWidget(self.version_label)
        layout.activate()
        self.footer.updateGeometry()
        # Footer items are built while the rail may still be hidden and before
        # the application stylesheet is applied; Qt layouts treat invisible
        # widgets as taking no space and unpolished buttons report tiny size
        # hints, so the footer height computed from sizeHint() would be too
        # small and the utility button row would overflow the footer boundary
        # and overlap the launch button. Fix the height from stable constants.
        #   launch: 38 min-height + 2 border = 40
        #   tool button: 32 min-height + 14 padding + 2 border = 48
        if self._collapsed:
            hint = 9 + 40 + 6 + 48 * 3 + 6 * 2 + 9 + 2
        else:
            #   version pill: 固定 20 px 单行
            hint = 9 + 40 + 7 + 48 + 7 + 20 + 9 + 2
        self.footer.setFixedHeight(hint)
        self.footer.setMinimumHeight(hint)
        self.footer.updateGeometry()

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_version(self, text: str) -> None:
        self.version_label.setText(text)
        self.version_label.setToolTip(text)
        self.launch_button.setToolTip(
            "启动完整 Pi" + (f"\n{text}" if text else "")
        )

    def set_badge(self, key: str, text: str | None = None) -> None:
        """Show a small attention dot on a navigation item."""
        label = self._badges.get(key)
        if label is None:
            return
        if text is not None:
            label.setText(str(text or ""))
        label.setVisible(True)
        self._position_badge(label)

    def clear_badge(self, key: str) -> None:
        label = self._badges.get(key)
        if label is not None:
            label.setVisible(False)

    @staticmethod
    def _position_badge(label: QLabel) -> None:
        parent = label.parentWidget()
        if parent is not None:
            label.move(parent.width() - label.width() - 4, 3)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """Keep the floating badge glued to its button's top-right corner."""
        if event.type() == QEvent.Resize:
            label = obj.property("navBadge")
            if isinstance(label, QLabel):
                self._position_badge(label)
        return super().eventFilter(obj, event)
