"""Collapsible grouped navigation rail for Pi Manager."""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
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
        self.collapse_button.clicked.connect(lambda: self.set_collapsed(not self._collapsed))
        brand_row.addWidget(self.collapse_button)
        root.addWidget(brand)

        current_group = None
        for page in self._pages:
            if page.group != current_group:
                current_group = page.group
                label = QLabel(current_group.upper())
                label.setObjectName("navSection")
                self._group_labels.append(label)
                root.addWidget(label)
            button = QToolButton()
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.setAutoExclusive(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
            button.setIconSize(QSize(18, 18))
            button.setText(page.title)
            button.setToolTip(f"{page.title}\n{page.description}")
            button.setFixedHeight(39)
            button.clicked.connect(lambda checked=False, key=page.key: self.set_current_key(key))
            self._buttons[page.key] = button
            root.addWidget(button)
        root.addStretch(1)

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
        self.version_label.setWordWrap(True)
        footer.addWidget(self.version_label)
        root.addWidget(self.footer)
        self.update_icons()

    def _utility_button(self, tooltip: str, signal: Signal) -> QToolButton:
        button = QToolButton()
        button.setObjectName("iconButton")
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(tooltip)
        button.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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
        self.version_label.setVisible(not self._collapsed)
        self.footer.layout().setContentsMargins(7 if self._collapsed else 9, 9, 7 if self._collapsed else 9, 9)
        self.update_icons()
        if emit:
            self.collapsedChanged.emit(self._collapsed)

    def is_collapsed(self) -> bool:
        return self._collapsed

    def set_version(self, text: str) -> None:
        self.version_label.setText(text)
