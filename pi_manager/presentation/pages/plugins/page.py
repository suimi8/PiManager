"""插件页入口：构造页面并触发扫描。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QScrollArea, QVBoxLayout, QWidget

from ...components import SectionHeading, SurfaceCard
from . import ops


def build_plugins_page(window) -> QWidget:
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

    header = SurfaceCard(margins=(17, 15, 17, 15), spacing=10)
    header_row = QHBoxLayout()
    header_row.addWidget(
        SectionHeading(
            "插件管理",
            "统一管理 PiManager 内置插件与用户自定义插件；自定义插件导入前会先做静态预览和风险确认。",
        ),
        1,
    )
    window.plugins_add_btn = window._btn(
        "添加插件", lambda checked=False: ops._add_plugin(window), success=True
    )
    window.plugins_refresh_btn = window._btn(
        "刷新状态", lambda checked=False: ops._refresh(window), secondary=True
    )
    window.plugins_cancel_btn = window._btn(
        "取消扫描", lambda checked=False: ops._cancel_refresh(window), ghost=True
    )
    window.plugins_cancel_btn.setEnabled(False)
    window.plugins_cancel_btn.setToolTip("停止尚未完成的扫描；已读到的插件会保留")
    window.plugins_install_all_btn = window._btn(
        "全部安装", lambda checked=False: ops._install_all(window), success=True
    )
    header_row.addWidget(window.plugins_add_btn, 0, Qt.AlignTop)
    header_row.addWidget(window.plugins_refresh_btn, 0, Qt.AlignTop)
    header_row.addWidget(window.plugins_cancel_btn, 0, Qt.AlignTop)
    header_row.addWidget(window.plugins_install_all_btn, 0, Qt.AlignTop)
    header.content.addLayout(header_row)

    window.plugins_global_status = QLabel("加载中…")
    window.plugins_global_status.setObjectName("subtitle")
    window.plugins_global_status.setWordWrap(True)
    header.content.addWidget(window.plugins_global_status)
    window.plugins_backend_status = QLabel("")
    window.plugins_backend_status.setObjectName("subtitle")
    window.plugins_backend_status.setWordWrap(True)
    window.plugins_backend_status.setVisible(False)
    header.content.addWidget(window.plugins_backend_status)
    layout.addWidget(header)

    window.plugins_list_container = QVBoxLayout()
    window.plugins_list_container.setSpacing(10)
    layout.addLayout(window.plugins_list_container)
    layout.addStretch(1)

    scroll.setWidget(body)
    outer.addWidget(scroll)

    window._plugin_cards = {}
    window._plugin_refreshing = False
    window._plugin_operation_keys = set()
    window._plugin_pending_after = []
    # 尊重 MainWindow 的 start_background=False 契约：offscreen 测试与嵌入场景
    # 构造期不得起线程、不得扫描插件目录。空态给出手动入口即可。
    if getattr(window, "_background_enabled", True):
        ops._refresh(window)
    else:
        window.plugins_global_status.setText("点击「刷新」扫描插件状态。")
    return page


def refresh_plugins_page(window, *, only_if_empty: bool = False) -> None:
    """公开入口：触发插件页后台扫描。

    ``only_if_empty=True`` 用于「首次进入该页时补扫」——构造期因
    ``start_background=False`` 跳过扫描后，页面处于空态，进入时才真正扫描；
    已有卡片则不重复扫描，避免每次切页都起线程。
    """
    if only_if_empty and getattr(window, "_plugin_cards", None):
        return
    ops._refresh(window)
