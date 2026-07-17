"""Modern quick-chat page."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..components import SectionHeading, StatusBadge, SurfaceCard


def build_chat_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    context = SurfaceCard(margins=(14, 12, 14, 12), spacing=8)
    model_row = QHBoxLayout()
    model_row.setSpacing(8)
    window.chat_context_badge = StatusBadge("独立上下文", "info")
    model_row.addWidget(window.chat_context_badge)
    model_row.addWidget(QLabel("Provider"))
    window.chat_provider = QComboBox()
    window.chat_provider.setEditable(True)
    window.chat_provider.setInsertPolicy(QComboBox.NoInsert)
    window.chat_provider.setMinimumWidth(170)
    window.chat_provider.setPlaceholderText("选择 Provider")
    window.chat_provider.currentTextChanged.connect(window._on_chat_provider_changed)
    model_row.addWidget(window.chat_provider, 1)
    model_row.addWidget(QLabel("模型"))
    window.chat_model = QComboBox()
    window.chat_model.setEditable(True)
    window.chat_model.setInsertPolicy(QComboBox.NoInsert)
    window.chat_model.setPlaceholderText("选择模型")
    model_row.addWidget(window.chat_model, 2)
    model_row.addWidget(window._btn("使用默认模型", window.chat_fill_default, secondary=True))
    model_row.addWidget(window._btn("刷新", window.refresh_chat_model_choices, ghost=True))
    context.content.addLayout(model_row)
    context_tip = QLabel("快速提问拥有独立的模型选择与最近 6 轮上下文；切换模型不会修改其他页面的编辑状态。")
    context_tip.setObjectName("subtitle")
    context_tip.setWordWrap(True)
    context.content.addWidget(context_tip)
    layout.addWidget(context)

    splitter = QSplitter(Qt.Vertical)
    splitter.setChildrenCollapsible(False)
    output = SurfaceCard(margins=(17, 15, 17, 15), spacing=9)
    output_header = QHBoxLayout()
    output_header.addWidget(SectionHeading("Pi 回复", "适合短问答；代码代理任务建议启动完整 Pi 会话。"), 1)
    output_header.addWidget(window._btn("清空对话", window.chat_clear_history, ghost=True), 0, Qt.AlignTop)
    output.content.addLayout(output_header)
    window.chat_output = QPlainTextEdit()
    window.chat_output.setMaximumBlockCount(10_000)
    window.chat_output.setReadOnly(True)
    window.chat_output.setObjectName("mono")
    window.chat_output.setPlaceholderText("回复将在这里显示")
    output.content.addWidget(window.chat_output, 1)
    splitter.addWidget(output)

    composer = SurfaceCard(elevated=True, margins=(17, 14, 17, 14), spacing=9)
    composer.content.addWidget(SectionHeading("发送消息"))
    window.chat_input = QPlainTextEdit()
    window.chat_input.setPlaceholderText("输入问题…")
    window.chat_input.setMinimumHeight(100)
    window.chat_input.setMaximumHeight(150)
    composer.content.addWidget(window.chat_input)
    send_row = QHBoxLayout()
    send_row.setSpacing(8)
    send_row.addWidget(window._btn("发送到 Pi", window.chat_send_enhanced, success=True))
    send_row.addWidget(window._btn("单次发送", window.chat_send, secondary=True))
    send_hint = QLabel("多轮模式会携带近期上下文；单次发送不会读取历史。")
    send_hint.setObjectName("subtitle")
    send_row.addWidget(send_hint)
    send_row.addStretch(1)
    composer.content.addLayout(send_row)
    splitter.addWidget(composer)
    splitter.setSizes([480, 190])
    layout.addWidget(splitter, 1)
    return page
