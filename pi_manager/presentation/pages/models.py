"""Modern model catalog page."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QSplitter,
    QTableWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..components import SectionHeading, StatusBadge, SurfaceCard
from ..design.icons import icon
from ..design.tokens import tokens_for


def build_models_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    filter_card = SurfaceCard(margins=(14, 12, 14, 12), spacing=8)
    filters = QHBoxLayout()
    filters.setSpacing(8)
    window.model_provider_filter = QComboBox()
    window.model_provider_filter.setMinimumWidth(160)
    window.model_provider_filter.addItem("全部 Provider", "")
    window.model_provider_filter.currentIndexChanged.connect(window.fill_models_table)
    window.model_filter = QLineEdit()
    window.model_filter.setPlaceholderText("搜索模型名称、Provider 或能力…")
    try:
        window.model_filter.setClearButtonEnabled(True)
    except Exception:
        pass
    window.model_filter.textChanged.connect(window.fill_models_table)
    window.model_only_favorites = QCheckBox("仅看收藏")
    window.model_only_favorites.toggled.connect(window.fill_models_table)
    filters.addWidget(window.model_provider_filter)
    filters.addWidget(window.model_filter, 1)
    filters.addWidget(window.model_only_favorites)
    filters.addWidget(window._btn("刷新", window.refresh_models, secondary=True))
    filter_card.content.addLayout(filters)
    meta = QHBoxLayout()
    window.models_count_lbl = QLabel("0 个模型")
    window.models_count_lbl.setObjectName("subtitle")
    meta.addWidget(window.models_count_lbl, 1)
    legend = QLabel("默认模型优先 · 收藏其次 · 双击可直接设为默认")
    legend.setObjectName("subtitle")
    meta.addWidget(legend)
    filter_card.content.addLayout(meta)
    layout.addWidget(filter_card)

    splitter = QSplitter(Qt.Horizontal)
    splitter.setChildrenCollapsible(False)

    table_card = SurfaceCard(margins=(0, 0, 0, 12), spacing=10)
    window.models_table = QTableWidget(0, 5)
    window.models_table.setHorizontalHeaderLabels(["模型", "Provider", "能力", "状态", "延迟"])
    header = window.models_table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
    window._polish_table(window.models_table)
    window.models_table.doubleClicked.connect(window.model_set_default)
    if hasattr(window, "_on_model_selection_changed"):
        window.models_table.itemSelectionChanged.connect(window._on_model_selection_changed)
    table_card.content.addWidget(window.models_table, 1)

    action_row = QHBoxLayout()
    action_row.setContentsMargins(12, 0, 12, 0)
    action_row.setSpacing(8)
    action_row.addWidget(window._btn("设为默认", window.model_set_default, success=True))
    action_row.addWidget(window._btn("启动 Pi", window.model_launch, secondary=True))
    action_row.addWidget(window._btn("测试选中", window.model_test_selected, secondary=True))
    action_row.addWidget(window._btn("收藏", window.model_add_favorite_batch, ghost=True))
    action_row.addStretch(1)
    action_row.addWidget(QLabel("Thinking"))
    window.thinking_combo = QComboBox()
    window.thinking_combo.addItems(["off", "minimal", "low", "medium", "high", "xhigh", "max"])
    window.thinking_combo.setCurrentText("high")
    window.thinking_combo.setMaximumWidth(100)
    action_row.addWidget(window.thinking_combo)
    action_row.addWidget(QLabel("测试"))
    window.test_mode_combo = QComboBox()
    window.test_mode_combo.addItem("自动", "auto")
    window.test_mode_combo.addItem("HTTP", "http")
    window.test_mode_combo.addItem("Pi", "pi")
    window.test_mode_combo.setMaximumWidth(90)
    action_row.addWidget(window.test_mode_combo)

    more = QToolButton()
    window.model_more_button = more
    more.setText("更多")
    more.setPopupMode(QToolButton.InstantPopup)
    more.setProperty("secondary", True)
    more.setCursor(Qt.PointingHandCursor)
    colors = tokens_for(*_theme_pair(window))
    more.setIcon(icon("ellipsis", colors.text_muted, 17))
    menu = QMenu(more)
    menu.addAction("全选可见", window.model_select_visible)
    menu.addAction("收藏当前过滤结果", window.model_fav_filtered)
    menu.addAction("写入循环列表 (enabledModels)", window.model_set_enabled)
    menu.addSeparator()
    menu.addAction("测试默认模型", window.model_test_default)
    menu.addAction("测试过滤结果", window.model_test_filtered)
    menu.addAction("批量测试收藏", window.model_test_favorites)
    menu.addAction("测试全部模型", window.model_test_all)
    more.setMenu(menu)
    action_row.addWidget(more)
    table_card.content.addLayout(action_row)
    window.test_status = QLabel("可使用 Ctrl / Shift 多选模型")
    window.test_status.setObjectName("subtitle")
    window.test_status.setWordWrap(True)
    window.test_status.setContentsMargins(12, 0, 12, 0)
    table_card.content.addWidget(window.test_status)
    splitter.addWidget(table_card)

    detail = SurfaceCard(margins=(17, 16, 17, 16), spacing=10)
    detail.setMinimumWidth(255)
    detail.setMaximumWidth(340)
    detail.content.addWidget(SectionHeading("模型详情", "当前选中模型的能力、状态与快捷操作。"))
    window.model_detail_badge = StatusBadge("未选择", "neutral")
    detail.content.addWidget(window.model_detail_badge, 0, Qt.AlignLeft)
    window.model_detail_title = QLabel("选择一个模型")
    window.model_detail_title.setObjectName("heroValue")
    window.model_detail_title.setWordWrap(True)
    detail.content.addWidget(window.model_detail_title)
    window.model_detail_provider = QLabel("—")
    window.model_detail_provider.setObjectName("heroProvider")
    detail.content.addWidget(window.model_detail_provider)
    divider = QFrame()
    divider.setObjectName("divider")
    divider.setFixedHeight(1)
    detail.content.addWidget(divider)
    window.model_detail_text = QPlainTextEdit()
    window.model_detail_text.setReadOnly(True)
    window.model_detail_text.setObjectName("mono")
    window.model_detail_text.setPlainText("选择模型后显示配置与测试状态。")
    detail.content.addWidget(window.model_detail_text, 1)
    detail_actions = QVBoxLayout()
    detail_actions.setSpacing(7)
    detail_actions.addWidget(window._btn("使用此模型", window.model_set_default, success=True))
    detail_actions.addWidget(window._btn("测试连接", window.model_test_selected, secondary=True))
    detail.content.addLayout(detail_actions)
    splitter.addWidget(detail)
    splitter.setStretchFactor(0, 1)
    splitter.setStretchFactor(1, 0)
    splitter.setSizes([900, 290])
    layout.addWidget(splitter, 1)
    return page


def _theme_pair(window) -> tuple[str, str]:
    try:
        from ... import core
        value = core.get_ui_theme()
        return str(value.get("mode") or "night"), str(value.get("accent") or "blue")
    except Exception:
        return "night", "blue"
