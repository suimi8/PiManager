"""Modern session browser page."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ..components import SurfaceCard


def build_sessions_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    filter_card = SurfaceCard(margins=(14, 12, 14, 12), spacing=8)
    filters = QHBoxLayout()
    filters.setSpacing(8)
    window.session_filter_wd = QLineEdit()
    window.session_filter_wd.setPlaceholderText("筛选项目或工作目录…")
    window.session_filter_name = QLineEdit()
    window.session_filter_name.setPlaceholderText("筛选模型、预览或文件名…")
    window.session_filter_wd.textChanged.connect(window.sessions_apply_filter)
    window.session_filter_name.textChanged.connect(window.sessions_apply_filter)
    filters.addWidget(window.session_filter_wd, 1)
    filters.addWidget(window.session_filter_name, 1)
    filters.addWidget(window._btn("刷新", window.refresh_sessions, secondary=True))
    filter_card.content.addLayout(filters)
    filter_tip = QLabel("会话记录仅用于恢复上下文；项目目录和模型信息从本地会话文件解析。")
    filter_tip.setObjectName("subtitle")
    filter_card.content.addWidget(filter_tip)
    layout.addWidget(filter_card)

    table_card = SurfaceCard(margins=(0, 0, 0, 12), spacing=10)
    window.sessions_table = QTableWidget(0, 5)
    window.sessions_table.setHorizontalHeaderLabels(["项目", "工作目录", "模型", "时间", "首条预览"])
    header = window.sessions_table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(1, QHeaderView.Stretch)
    header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(4, QHeaderView.Stretch)
    window._polish_table(window.sessions_table)
    table_card.content.addWidget(window.sessions_table, 1)
    actions = QHBoxLayout()
    actions.setContentsMargins(12, 0, 12, 0)
    actions.setSpacing(8)
    actions.addWidget(window._btn("继续会话", window.session_continue, success=True))
    actions.addWidget(window._btn("打开项目目录", window.session_open_project, secondary=True))
    actions.addWidget(window._btn("在资源管理器显示", window.session_reveal, secondary=True))
    actions.addWidget(window._btn("重命名", window.session_rename, ghost=True))
    actions.addStretch(1)
    actions.addWidget(window._btn("删除选中", window.session_delete, danger=True))
    actions.addWidget(window._btn("批量删除", window.session_delete_batch, danger=True))
    table_card.content.addLayout(actions)
    layout.addWidget(table_card, 1)
    return page
