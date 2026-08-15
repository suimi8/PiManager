"""Modern quick-chat page."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QBuffer, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QShortcut
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

_IMAGE_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
}


class ImageAttachEdit(QPlainTextEdit):
    """Plain-text editor that also captures pasted/dropped images as attachments.

    Images are not inserted into the document; they are collected so the chat
    pipeline can run them through a vision model before asking the text model.
    """

    imagesAttached = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._attachments: list[dict] = []

    def attachments(self) -> list[dict]:
        return list(self._attachments)

    def add_image_bytes(self, data: bytes, mime: str, name: str = "") -> None:
        if not data:
            return
        self._attachments.append(
            {
                "bytes": data,
                "mime": mime or "image/png",
                "name": name or f"图片 {len(self._attachments) + 1}",
            }
        )
        self.imagesAttached.emit()

    def clear_attachments(self) -> None:
        if self._attachments:
            self._attachments = []
            self.imagesAttached.emit()

    def insertFromMimeData(self, source) -> None:
        handled = False
        if source.hasImage():
            image = source.imageData()
            if isinstance(image, QImage):
                buffer = QBuffer()
                buffer.open(QBuffer.ReadWrite)
                image.save(buffer, "PNG")
                self.add_image_bytes(bytes(buffer.data()), "image/png")
                handled = True
        if source.hasUrls():
            for url in source.urls():
                if not url.isLocalFile():
                    continue
                path = Path(url.toLocalFile())
                suffix = path.suffix.lower()
                if suffix not in _IMAGE_MIME_BY_SUFFIX:
                    continue
                try:
                    data = path.read_bytes()
                except OSError:
                    continue
                self.add_image_bytes(data, _IMAGE_MIME_BY_SUFFIX[suffix], path.name)
                handled = True
        if handled:
            return
        super().insertFromMimeData(source)


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
    attach_row = QHBoxLayout()
    attach_row.setSpacing(8)
    window.chat_attach_bar = QWidget()
    window.chat_attach_bar.setVisible(False)
    window.chat_attach_layout = QHBoxLayout(window.chat_attach_bar)
    window.chat_attach_layout.setContentsMargins(0, 0, 0, 0)
    window.chat_attach_layout.setSpacing(6)
    attach_hint = QLabel("支持粘贴 / 拖入图片，发送时自动用识图模型识别后转文本")
    attach_hint.setObjectName("subtitle")
    attach_row.addWidget(attach_hint, 1)
    attach_row.addWidget(window._btn("添加图片", window.chat_pick_images, ghost=True))
    attach_row.addWidget(window._btn("清除图片", window.chat_clear_attachments, ghost=True))
    composer.content.addLayout(attach_row)
    composer.content.addWidget(window.chat_attach_bar)
    window.chat_input = ImageAttachEdit()
    window.chat_input.setPlaceholderText("输入问题…（Ctrl+Enter 发送；可直接粘贴截图）")
    window.chat_input.setMinimumHeight(100)
    window.chat_input.setMaximumHeight(150)
    window.chat_input.imagesAttached.connect(window._on_chat_attachments_changed)
    composer.content.addWidget(window.chat_input)
    # Ctrl+Enter / Cmd+Enter sends without leaving the keyboard.
    for seq in ("Ctrl+Return", "Ctrl+Enter"):
        shortcut = QShortcut(QKeySequence(seq), window.chat_input)
        shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        shortcut.activated.connect(window.chat_send_enhanced)
    send_row = QHBoxLayout()
    send_row.setSpacing(8)
    send_row.addWidget(window._btn("发送到 Pi", window.chat_send_enhanced, success=True))
    send_row.addWidget(window._btn("单次发送", window.chat_send, secondary=True))
    send_hint = QLabel("多轮模式会携带近期上下文（Ctrl+Enter 发送）；单次发送不会读取历史。")
    send_hint.setObjectName("subtitle")
    send_row.addWidget(send_hint)
    send_row.addStretch(1)
    composer.content.addLayout(send_row)
    splitter.addWidget(composer)
    splitter.setSizes([480, 190])
    layout.addWidget(splitter, 1)
    return page
