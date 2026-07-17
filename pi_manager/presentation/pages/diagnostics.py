"""Health monitoring, test history, and system tools pages."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ... import extras
from ..components import SectionHeading, StatusBadge, SurfaceCard


def build_health_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    controls = SurfaceCard(margins=(14, 12, 14, 12), spacing=9)
    row = QHBoxLayout()
    row.setSpacing(8)
    window.health_scope = QComboBox()
    window.health_scope.addItem("收藏列表", "favorites")
    window.health_scope.addItem("默认模型", "default")
    window.health_scope.addItem("自定义 Provider", "custom")
    window.health_scope.addItem("全部已加载模型", "all_listed")
    window.health_scope.addItem("模型页当前选中", "selected")
    window.health_scope.setCurrentIndex(2)
    row.addWidget(QLabel("巡检范围"))
    row.addWidget(window.health_scope)
    row.addWidget(window._btn("立即健康检查", window.health_run_now, success=True))
    row.addWidget(window._btn("刷新结果", window.health_refresh_table, secondary=True))
    row.addStretch(1)
    window.health_interval = QSpinBox()
    window.health_interval.setRange(0, 1440)
    window.health_interval.setSuffix(" 分钟")
    window.health_interval.setSpecialValueText("关闭定时")
    window.health_interval.setValue(int((window.mgr or {}).get("health_interval_min") or 0))
    row.addWidget(QLabel("定时"))
    row.addWidget(window.health_interval)
    row.addWidget(window._btn("保存", window.health_save_interval, ghost=True))
    controls.content.addLayout(row)
    explanation = QLabel("巡检只在点击检查或定时触发时访问模型；打开页面仅加载本地缓存结果。")
    explanation.setObjectName("subtitle")
    explanation.setWordWrap(True)
    controls.content.addWidget(explanation)
    layout.addWidget(controls)

    table_card = SurfaceCard(margins=(0, 0, 0, 12), spacing=10)
    window.health_table = QTableWidget(0, 6)
    window.health_table.setHorizontalHeaderLabels(["模型", "状态", "延迟", "方式", "检查时间", "错误 / 预览"])
    window.health_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    window._polish_table(window.health_table)
    table_card.content.addWidget(window.health_table, 1)
    action_row = QHBoxLayout()
    action_row.setContentsMargins(12, 0, 12, 0)
    action_row.setSpacing(8)
    action_row.addWidget(window._btn("收藏可用项", window.health_add_ok_to_favorites, secondary=True))
    action_row.addWidget(window._btn("重测选中", window.health_retest_selected, secondary=True))
    action_row.addStretch(1)
    window.health_status_badge = StatusBadge("等待检查", "neutral")
    action_row.addWidget(window.health_status_badge)
    table_card.content.addLayout(action_row)
    window.health_status = QLabel("尚未检查 — 推荐先检查默认模型或自定义 Provider")
    window.health_status.setObjectName("subtitle")
    window.health_status.setWordWrap(True)
    window.health_status.setContentsMargins(12, 0, 12, 0)
    table_card.content.addWidget(window.health_status)
    layout.addWidget(table_card, 1)
    return page


def build_history_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    filters = SurfaceCard(margins=(14, 12, 14, 12), spacing=8)
    row = QHBoxLayout()
    row.setSpacing(8)
    window.history_filter = QLineEdit()
    window.history_filter.setPlaceholderText("搜索 Provider 或模型…")
    try:
        window.history_filter.setClearButtonEnabled(True)
    except Exception:
        pass
    window.history_filter.textChanged.connect(window.history_refresh)
    row.addWidget(window.history_filter, 1)
    row.addWidget(window._btn("刷新", window.history_refresh, secondary=True))
    row.addWidget(window._btn("清空历史", window.history_clear, danger=True))
    filters.content.addLayout(row)
    hint = QLabel("历史记录来自本地测试结果，用于比较可用性和延迟趋势。")
    hint.setObjectName("subtitle")
    filters.content.addWidget(hint)
    layout.addWidget(filters)

    table_card = SurfaceCard(margins=(0, 0, 0, 0))
    window.history_table = QTableWidget(0, 6)
    window.history_table.setHorizontalHeaderLabels(["时间", "模型", "可用", "延迟", "方式", "错误 / 预览"])
    window.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    window._polish_table(window.history_table)
    table_card.content.addWidget(window.history_table, 1)
    layout.addWidget(table_card, 1)
    return page


def build_tools_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    selfcheck = SurfaceCard(margins=(17, 15, 17, 15), spacing=10)
    selfcheck_header = QHBoxLayout()
    selfcheck_header.addWidget(SectionHeading("环境自检", "验证 Pi CLI、配置路径、密钥存储与运行环境。"), 1)
    selfcheck_header.addWidget(window._btn("运行自检", window.self_check_run, success=True), 0, Qt.AlignTop)
    selfcheck.content.addLayout(selfcheck_header)
    window.selfcheck_table = QTableWidget(0, 3)
    window.selfcheck_table.setHorizontalHeaderLabels(["项目", "状态", "详情"])
    window.selfcheck_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    window._polish_table(window.selfcheck_table)
    window.selfcheck_table.setMinimumHeight(210)
    selfcheck.content.addWidget(window.selfcheck_table)
    layout.addWidget(selfcheck)

    transfers = SurfaceCard(margins=(17, 15, 17, 15), spacing=10)
    transfers.content.addWidget(SectionHeading("配置迁移与安全", "导入导出配置，或将现有明文 API Key 迁移到安全存储。"))
    transfer_row = QHBoxLayout()
    transfer_row.setSpacing(8)
    transfer_row.addWidget(window._btn("导出配置包", window.export_config, secondary=True))
    transfer_row.addWidget(window._btn("导出（含密钥）", window.export_config_with_secrets, secondary=True))
    transfer_row.addWidget(window._btn("导入配置包", window.import_config, secondary=True))
    transfer_row.addWidget(window._btn("加密现有 Key", window.secure_keys_now, success=True))
    transfer_row.addStretch(1)
    transfers.content.addLayout(transfer_row)
    layout.addWidget(transfers)

    updates = SurfaceCard(elevated=True, margins=(17, 15, 17, 15), spacing=10)
    update_header = QHBoxLayout()
    update_header.addWidget(SectionHeading("Pi Manager 更新", "默认检查 GitHub Releases，也可以提供自定义版本清单。"), 1)
    window.mgr_version_lbl = QLabel(f"当前版本 · v{extras.APP_VERSION}")
    window.mgr_version_lbl.setObjectName("statusBadge")
    update_header.addWidget(window.mgr_version_lbl, 0, Qt.AlignTop)
    updates.content.addLayout(update_header)
    update_row = QHBoxLayout()
    update_row.setSpacing(8)
    window.update_url_edit = QLineEdit(str((window.mgr or {}).get("update_manifest_url") or ""))
    window.update_url_edit.setPlaceholderText("自定义 manifest URL（留空使用 GitHub Releases）")
    update_row.addWidget(window.update_url_edit, 1)
    update_row.addWidget(window._btn("检查更新", window.check_manager_update, success=True))
    updates.content.addLayout(update_row)
    window.update_status = QLabel("尚未检查")
    window.update_status.setObjectName("subtitle")
    window.update_status.setWordWrap(True)
    updates.content.addWidget(window.update_status)
    window._last_manager_update = {}
    layout.addWidget(updates)
    layout.addStretch(1)
    scroll.setWidget(body)
    outer.addWidget(scroll)
    return page
