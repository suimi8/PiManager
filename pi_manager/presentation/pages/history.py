"""测试历史页：本地延迟与可用性记录。"""
from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import extras
from ..components import SurfaceCard


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
    # 防抖：history_refresh 每次都 extras.load_history() 读磁盘 + 重建 200 行表，
    # 以前按每次击键触发。
    window._history_filter_debounce = QTimer(window)
    window._history_filter_debounce.setSingleShot(True)
    window._history_filter_debounce.setInterval(180)
    window._history_filter_debounce.timeout.connect(window.history_refresh)
    window.history_filter.textChanged.connect(
        lambda _text: window._history_filter_debounce.start()
    )
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


class HistoryPageMixin:
    """测试历史页行为。从 ``DiagnosticsPageMixin`` 拆出。"""

    def history_refresh(self):
        if not hasattr(self, "history_table"):
            return
        q = (self.history_filter.text() if hasattr(self, "history_filter") else "") or ""
        q = q.lower().strip()
        rows = extras.load_history()
        if q:
            rows = [r for r in rows if q in f"{r.get('provider')}/{r.get('model')}".lower()]
        rows = list(reversed(rows[-200:]))
        self.history_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.history_table.setItem(i, 0, QTableWidgetItem(str(r.get("time") or "")))
            self.history_table.setItem(i, 1, QTableWidgetItem(f"{r.get('provider')}/{r.get('model')}"))
            self.history_table.setItem(i, 2, QTableWidgetItem("是" if r.get("available") else "否"))
            lat = r.get("latency_ms")
            self.history_table.setItem(i, 3, QTableWidgetItem(f"{lat:.0f}" if isinstance(lat, (int, float)) else "—"))
            self.history_table.setItem(i, 4, QTableWidgetItem(str(r.get("mode") or "")))
            extra = r.get("error") or r.get("preview") or ""
            self.history_table.setItem(i, 5, QTableWidgetItem(str(extra)[:120]))

    def history_clear(self):
        if QMessageBox.question(self, "确认", "清空全部测试历史？") != QMessageBox.Yes:
            return
        extras.save_history([])
        self.history_refresh()
