"""Modern Provider management page."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..components import SectionHeading, StatusBadge, SurfaceCard


def build_providers_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    summary = QHBoxLayout()
    summary.setSpacing(10)
    window.provider_summary_badge = StatusBadge("0 个 Provider", "info")
    summary.addWidget(window.provider_summary_badge)
    summary_text = QLabel("Provider 配置与 API Key 独立保存；界面预览会自动脱敏。")
    summary_text.setObjectName("subtitle")
    summary.addWidget(summary_text)
    summary.addStretch(1)
    summary.addWidget(window._btn("从 API 拉取", window.provider_fetch_api, success=True))
    summary.addWidget(window._btn("新建 Provider", window.provider_add, secondary=True))
    layout.addLayout(summary)

    splitter = QSplitter(Qt.Horizontal)
    splitter.setChildrenCollapsible(False)

    provider_card = SurfaceCard(margins=(15, 15, 15, 15), spacing=10)
    provider_card.setMinimumWidth(280)
    provider_card.setMaximumWidth(380)
    provider_card.content.addWidget(
        SectionHeading("Provider 列表", "读取 models.json；选择项目后在右侧查看与编辑。")
    )
    window.provider_list = QListWidget()
    window.provider_list.setSpacing(2)
    window.provider_list.currentItemChanged.connect(window.on_provider_selected)
    provider_card.content.addWidget(window.provider_list, 1)
    list_actions = QHBoxLayout()
    list_actions.setSpacing(7)
    list_actions.addWidget(window._btn("添加", window.provider_add, secondary=True))
    list_actions.addWidget(window._btn("编辑", window.provider_edit, secondary=True))
    list_actions.addWidget(window._btn("删除", window.provider_delete, danger=True))
    provider_card.content.addLayout(list_actions)
    splitter.addWidget(provider_card)

    detail = SurfaceCard(margins=(18, 16, 18, 16), spacing=11)
    detail_header = QHBoxLayout()
    title_box = QVBoxLayout()
    title_box.setSpacing(3)
    label = QLabel("PROVIDER DETAILS")
    label.setObjectName("sectionKicker")
    title_box.addWidget(label)
    window.provider_detail_title = QLabel("选择一个 Provider")
    window.provider_detail_title.setObjectName("sectionTitle")
    title_box.addWidget(window.provider_detail_title)
    detail_header.addLayout(title_box, 1)
    window.provider_key_badge = StatusBadge("API Key 未检查", "neutral")
    detail_header.addWidget(window.provider_key_badge, 0, Qt.AlignTop)
    detail.content.addLayout(detail_header)

    key_surface = QFrame()
    key_surface.setObjectName("metricCard")
    key_layout = QHBoxLayout(key_surface)
    key_layout.setContentsMargins(13, 10, 13, 10)
    key_layout.setSpacing(10)
    key_copy = QVBoxLayout()
    key_copy.setSpacing(2)
    key_title = QLabel("API Key 池")
    key_title.setObjectName("sectionTitle")
    key_hint = QLabel("密钥与模型配置相互独立，可轮换、禁用或标记失效。")
    key_hint.setObjectName("subtitle")
    key_hint.setWordWrap(True)
    key_copy.addWidget(key_title)
    key_copy.addWidget(key_hint)
    key_layout.addLayout(key_copy, 1)
    key_layout.addWidget(window._btn("管理 Keys", window.provider_manage_keys, secondary=True))
    detail.content.addWidget(key_surface)

    window.provider_detail = QPlainTextEdit()
    window.provider_detail.setReadOnly(True)
    window.provider_detail.setObjectName("mono")
    window.provider_detail.setPlaceholderText("Provider 配置预览（敏感值将自动隐藏）")
    detail.content.addWidget(window.provider_detail, 1)
    detail_actions = QHBoxLayout()
    detail_actions.setSpacing(8)
    detail_actions.addWidget(window._btn("添加模型", window.provider_add_model, secondary=True))
    detail_actions.addWidget(window._btn("打开 models.json", window.open_models_json, ghost=True))
    detail_actions.addStretch(1)
    detail.content.addLayout(detail_actions)
    splitter.addWidget(detail)
    splitter.setStretchFactor(0, 0)
    splitter.setStretchFactor(1, 1)
    splitter.setSizes([320, 780])
    layout.addWidget(splitter, 1)
    return page
