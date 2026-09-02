"""Modern quick-chat page."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import QBuffer, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ... import core
from ... import extras
from ...chat_choices import (
    config_models_for_provider,
    merge_model_ids,
    merge_provider_names,
    pick_model,
    pick_provider,
    providers_from_models_config,
)
from ..components import SectionHeading, StatusBadge, SurfaceCard
from ..workers import Worker

logger = logging.getLogger(__name__)

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
    page.setObjectName("chatPage")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(24, 22, 24, 24)
    layout.setSpacing(16)

    context = QFrame()
    context_layout = QVBoxLayout(context)
    context_layout.setContentsMargins(0, 0, 0, 0)
    context_layout.setSpacing(8)
    model_row = QHBoxLayout()
    model_row.setSpacing(8)
    window.chat_context_badge = StatusBadge("独立上下文", "info")
    model_row.addWidget(window.chat_context_badge)
    model_row.addWidget(QLabel("Provider"))
    window.chat_provider = QComboBox()
    window.chat_provider.setEditable(True)
    window.chat_provider.setInsertPolicy(QComboBox.NoInsert)
    window.chat_provider.setMinimumWidth(120)
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
    context_layout.addLayout(model_row)
    meta_row = QHBoxLayout()
    meta_row.setSpacing(16)
    window.chat_meta_label = QLabel("Thinking: —  ·  工作目录: —")
    window.chat_meta_label.setObjectName("subtitle")
    window.chat_meta_label.setWordWrap(True)
    meta_row.addWidget(window.chat_meta_label, 1)
    context_layout.addLayout(meta_row)
    layout.addWidget(context)

    splitter = QSplitter(Qt.Vertical)
    splitter.setChildrenCollapsible(False)
    output = QWidget()
    output_layout = QVBoxLayout(output)
    output_layout.setContentsMargins(0, 0, 0, 0)
    output_layout.setSpacing(8)
    output_header = QHBoxLayout()
    output_header.addWidget(SectionHeading("Pi 回复"), 1)
    window.chat_stop_btn = window._btn("停止生成", window.chat_stop, danger=True)
    window.chat_stop_btn.setEnabled(False)
    window.chat_stop_btn.setToolTip("停止当前生成；已输出的内容会保留")
    output_header.addWidget(window.chat_stop_btn, 0, Qt.AlignTop)
    output_header.addWidget(
        window._btn("复制结果", window.chat_copy_output, ghost=True), 0, Qt.AlignTop
    )
    output_header.addWidget(
        window._btn("导出会话", window.chat_export_session, ghost=True), 0, Qt.AlignTop
    )
    output_header.addWidget(
        window._btn("清空会话", window.chat_clear_history, ghost=True), 0, Qt.AlignTop
    )
    output_layout.addLayout(output_header)
    window.chat_output = QPlainTextEdit()
    window.chat_output.setMaximumBlockCount(10_000)
    window.chat_output.setReadOnly(True)
    window.chat_output.setObjectName("mono")
    window.chat_output.setPlaceholderText("回复将在这里显示")
    output_layout.addWidget(window.chat_output, 1)
    splitter.addWidget(output)

    composer = SurfaceCard(elevated=True, margins=(16, 14, 16, 14), spacing=8)
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
    window.chat_send_enhanced_btn = window._btn(
        "发送到 Pi", window.chat_send_enhanced, success=True
    )
    window.chat_regenerate_btn = window._btn(
        "重新生成", window.chat_regenerate, secondary=True
    )
    window.chat_send_btn = window._btn("单次发送", window.chat_send, ghost=True)
    send_row.addWidget(window.chat_send_enhanced_btn)
    send_row.addWidget(window.chat_regenerate_btn)
    send_row.addWidget(window.chat_send_btn)
    send_hint = QLabel("Ctrl+Enter 发送；复杂改代码请启动完整 Pi。")
    send_hint.setObjectName("subtitle")
    send_row.addWidget(send_hint)
    send_row.addStretch(1)
    composer.content.addLayout(send_row)
    splitter.addWidget(composer)
    splitter.setSizes([480, 190])
    layout.addWidget(splitter, 1)
    return page


class ChatPageMixin:
    """快速提问页行为：模型选择、附件、单次/多轮发送。从 ``ui.py`` / ``ui_features.py`` 下沉。"""

    def _chat_combo_text(self, combo: QComboBox | None) -> str:
        if combo is None:
            return ""
        try:
            return (combo.currentText() or "").strip()
        except Exception:
            return ""

    def _set_chat_combo_text(self, combo: QComboBox | None, text: str) -> None:
        if combo is None:
            return
        text = (text or "").strip()
        if not text:
            combo.setCurrentIndex(-1)
            combo.setEditText("")
            return
        idx = combo.findText(text)
        if idx < 0:
            combo.addItem(text)
            idx = combo.findText(text)
        combo.setCurrentIndex(idx if idx >= 0 else 0)
        combo.setEditText(text)

    def refresh_chat_model_choices(self) -> None:
        """用当前 models 列表填充快速提问的 Provider / Model 下拉。"""
        if not hasattr(self, "chat_provider") or not isinstance(self.chat_provider, QComboBox):
            return
        cur_p = self._chat_combo_text(self.chat_provider)
        cur_m = self._chat_combo_text(self.chat_model)

        listed = [m.provider for m in (self.models or []) if m.provider]
        cfg_names: list[str] = []
        try:
            cfg = core.load_models_config()
            cfg_names = providers_from_models_config(cfg)
        except Exception:
            pass
        providers = merge_provider_names(listed, cfg_names)

        self.chat_provider.blockSignals(True)
        self.chat_provider.clear()
        for p in providers:
            self.chat_provider.addItem(p)
        self.chat_provider.blockSignals(False)

        try:
            dp, _, _ = core.get_default_model()
        except Exception:
            dp = ""
        target = pick_provider(cur_p, providers, dp)
        self._set_chat_combo_text(self.chat_provider, target)

        self._reload_chat_models_for_provider(
            self._chat_combo_text(self.chat_provider), prefer_model=cur_m
        )
        self._refresh_chat_context()

    def _on_chat_provider_changed(self, _text: str = "") -> None:
        if not hasattr(self, "chat_model") or not isinstance(self.chat_model, QComboBox):
            return
        prefer = self._chat_combo_text(self.chat_model)
        self._reload_chat_models_for_provider(self._chat_combo_text(self.chat_provider), prefer_model=prefer)
        self._refresh_chat_context()

    def _reload_chat_models_for_provider(self, provider: str, prefer_model: str = "") -> None:
        """填充快速提问的模型下拉：list-models 结果 + models.json 手动添加的模型。"""
        if not hasattr(self, "chat_model") or not isinstance(self.chat_model, QComboBox):
            return
        provider = (provider or "").strip()
        listed_ids: list[str] = []
        for m in self.models or []:
            if not provider or m.provider == provider:
                if m.model:
                    listed_ids.append(m.model)
        config_models: list = []
        if provider:
            try:
                cfg = core.load_models_config()
                config_models = config_models_for_provider(cfg, provider)
            except Exception:
                pass
        models = merge_model_ids(listed_ids, config_models)

        self.chat_model.blockSignals(True)
        self.chat_model.clear()
        for mid in models:
            self.chat_model.addItem(mid)
        self.chat_model.blockSignals(False)

        if not models:
            self.chat_model.setCurrentIndex(-1)
            self.chat_model.setEditText("")
            self.chat_model.setPlaceholderText("该 Provider 暂无可用模型")
            return
        try:
            dp, dm, _ = core.get_default_model()
        except Exception:
            dp, dm = "", ""
        target = pick_model(
            prefer_model,
            models,
            provider=provider,
            default_provider=dp,
            default_model=dm,
        )
        self._set_chat_combo_text(self.chat_model, target)

    def chat_fill_default(self):
        p, m, t = core.get_default_model()
        if hasattr(self, "thinking_combo") and t:
            idx = self.thinking_combo.findText(t)
            if idx >= 0:
                self.thinking_combo.setCurrentIndex(idx)
        if hasattr(self, "chat_provider") and isinstance(self.chat_provider, QComboBox):
            # 确保下拉有数据
            if self.chat_provider.count() == 0:
                self.refresh_chat_model_choices()
            # 默认 provider 可能已不存在（配置残留）：回退到第一个可用 provider
            available = [
                self.chat_provider.itemText(i) for i in range(self.chat_provider.count())
            ]
            if p not in available:
                p = available[0] if available else ""
                m = ""
            self._set_chat_combo_text(self.chat_provider, p)
            self._reload_chat_models_for_provider(p, prefer_model=m)
        else:
            # 旧控件兼容
            try:
                self.chat_provider.setText(p)
                self.chat_model.setText(m)
            except Exception:
                pass
        self._refresh_chat_context()

    def _on_chat_attachments_changed(self):
        bar = getattr(self, "chat_attach_bar", None)
        if bar is None:
            return
        attachments = self.chat_input.attachments() if hasattr(self.chat_input, "attachments") else []
        layout = getattr(self, "chat_attach_layout", None)
        if layout is None:
            return
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        from PySide6.QtCore import QSize
        from PySide6.QtGui import QPixmap

        for index, att in enumerate(attachments, start=1):
            thumb = QLabel()
            thumb.setFixedSize(56, 56)
            thumb.setToolTip(f"{att.get('name') or '图片'} · {len(att.get('bytes') or b'') // 1024} KB")
            pixmap = QPixmap()
            if pixmap.loadFromData(att.get("bytes") or b""):
                thumb.setPixmap(
                    pixmap.scaled(QSize(56, 56), Qt.KeepAspectRatio, Qt.SmoothTransformation)
                )
            else:
                thumb.setText(f"[{index}]")
                thumb.setAlignment(Qt.AlignCenter)
            thumb.setObjectName("chatThumb")
            layout.addWidget(thumb)
        count = QLabel(f"已附加 {len(attachments)} 张图片" if attachments else "")
        count.setObjectName("subtitle")
        layout.addWidget(count)
        layout.addStretch(1)
        bar.setVisible(bool(attachments))

    def chat_pick_images(self):
        if not hasattr(self, "chat_input"):
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择图片",
            "",
            "图片 (*.png *.jpg *.jpeg *.gif *.bmp *.webp)",
        )
        for path in files:
            try:
                data = Path(path).read_bytes()
            except OSError:
                continue
            suffix = Path(path).suffix.lower()
            mime = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif": "image/gif",
                ".bmp": "image/bmp",
                ".webp": "image/webp",
            }.get(suffix, "image/png")
            self.chat_input.add_image_bytes(data, mime, Path(path).name)
        self._on_chat_attachments_changed()

    def chat_clear_attachments(self):
        if hasattr(self, "chat_input") and hasattr(self.chat_input, "clear_attachments"):
            self.chat_input.clear_attachments()
        self._on_chat_attachments_changed()

    def chat_clear_history(self):
        self.chat_history = []
        if hasattr(self, "chat_output"):
            self.chat_output.setPlainText("")
        try:
            from ... import rpc_session

            rpc_session.reset_chat_session()
        except Exception as e:
            logger.warning("reset chat session failed: %s", e)
        self.status.showMessage("已清空对话历史")

    def _refresh_chat_context(self) -> None:
        label = getattr(self, "chat_meta_label", None)
        if label is None:
            return
        thinking = "—"
        try:
            thinking = self.thinking_combo.currentText() or "—"
        except Exception:
            pass
        workdir = "—"
        try:
            workdir = self.workdir_edit.text().strip() or str(core.user_home())
        except Exception:
            pass
        provider = self._chat_combo_text(self.chat_provider) or "—"
        model = self._chat_combo_text(self.chat_model) or "—"
        label.setText(
            f"Provider: {provider}  ·  Model: {model}  ·  "
            f"Thinking: {thinking}  ·  工作目录: {workdir}"
        )

    def _set_chat_busy(self, busy: bool) -> None:
        if hasattr(self, "chat_input"):
            self.chat_input.setEnabled(not busy)
        button = getattr(self, "chat_stop_btn", None)
        if button is not None:
            button.setEnabled(bool(busy))
            button.setText("停止生成")
        setter = getattr(self, "_set_action_busy", None)
        if callable(setter):
            setter(
                getattr(self, "chat_send_enhanced_btn", None),
                busy,
                idle="发送到 Pi",
                busy_text="正在发送…",
            )
            setter(
                getattr(self, "chat_send_btn", None),
                busy,
                idle="单次发送",
                busy_text="正在发送…",
            )
            setter(
                getattr(self, "chat_regenerate_btn", None),
                busy,
                idle="重新生成",
                busy_text="正在生成…",
            )

    def chat_stop(self) -> None:
        worker = getattr(self, "_chat_worker", None)
        if worker is None:
            self._set_chat_busy(False)
            return
        try:
            if worker.isRunning():
                worker.requestInterruption()
        except RuntimeError:
            pass
        self._chat_worker = None
        self._set_chat_busy(False)
        self.status.showMessage("已请求停止生成", 4000)
        if hasattr(self, "chat_output"):
            self.chat_output.appendPlainText("[已停止]")
        notify = getattr(self, "notify_warning", None)
        if callable(notify):
            notify("已请求停止生成")

    def chat_copy_output(self) -> None:
        if not hasattr(self, "chat_output"):
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(self.chat_output.toPlainText())
        self.status.showMessage("已复制对话", 4000)
        notify = getattr(self, "notify_success", None)
        if callable(notify):
            notify("已复制对话")

    def chat_export_session(self) -> None:
        if not hasattr(self, "chat_output"):
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出会话", str(Path.home() / "pi-chat.md"), "Markdown (*.md)"
        )
        if not path:
            return
        Path(path).write_text(self.chat_output.toPlainText(), encoding="utf-8")
        self.notify_success(f"已导出：{path}")

    def chat_regenerate(self) -> None:
        prompt = str(getattr(self, "_last_chat_prompt", "") or "").strip()
        if not prompt:
            self.notify_warning("还没有可以重新生成的上一条消息")
            return
        self.chat_input.setPlainText(prompt)
        self.chat_send_enhanced()

    def _describe_attachments(
        self,
        attachments: list[dict],
        user_prompt: str = "",
    ) -> tuple[str | None, str | None]:
        """Run image attachments through the built-in vision model.

        The vision instruction is tailored to the user's question and demands
        verbatim transcription of any text in the screenshot.

        Returns ``(description_text, error_text)`` — exactly one is not None.
        """
        if not attachments:
            return None, None
        if not core.zhipu_api_key():
            return None, (
                "未配置智谱 API Key。请在「设置 → 识图模型」填入（免费申请："
                "https://bigmodel.cn，GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash 免费额度）。"
            )
        vision_prompt = core.build_vision_prompt(user_prompt)
        parts = []
        for index, att in enumerate(attachments, start=1):
            desc_result = core.describe_image(
                att.get("bytes") or b"",
                att.get("mime") or "image/png",
                prompt=vision_prompt,
            )
            if not desc_result.get("ok"):
                return None, (
                    f"识图失败（第 {index} 张，{desc_result.get('model') or '自动'}）："
                    f"{desc_result.get('error') or '未知错误'}"
                )
            parts.append(
                f"[图片{index}识别结果 · {desc_result.get('model') or '自动'}] "
                f"{desc_result.get('description') or ''}"
            )
        return "\n".join(parts), None

    def _append_image_prompt(self, description: str, prompt: str) -> str:
        if not description:
            return prompt
        if prompt:
            return (
                f"用户附加了截图，图片内容已由识图模型完整转录如下"
                f"（图片本身不可直接查看，请完全基于转录内容回答，不要声称看不到图片）：\n"
                f"{description}\n\n"
                f"用户的问题：{prompt}"
            )
        return f"用户附加了截图，图片内容已由识图模型完整转录如下（请完全基于转录内容回答）：\n{description}"

    def chat_send(self):
        prompt = self.chat_input.toPlainText().strip()
        attachments = (
            self.chat_input.attachments() if hasattr(self.chat_input, "attachments") else []
        )
        if not prompt and not attachments:
            return
        provider = self._chat_combo_text(self.chat_provider) or None
        model = self._chat_combo_text(self.chat_model) or None
        workdir = self.workdir_edit.text().strip() or str(core.user_home())
        # Read widget values on the UI thread; the worker must not touch Qt.
        try:
            thinking = self.thinking_combo.currentText() or "off"
        except Exception:
            thinking = "off"
        if attachments:
            if not core.zhipu_api_key():
                QMessageBox.warning(
                    self,
                    "未配置识图模型",
                    "已附加图片，但未配置智谱 API Key。请在「设置 → 识图模型」填入。",
                )
                return
            self.chat_output.appendPlainText(
                f"\n你: {prompt or '[图片]'}\n…正在用内置免费识图模型识别图片…"
            )
        else:
            self.chat_output.appendPlainText(f"\n>>> {prompt}\n…请求中，请稍候…\n")
        self.status.showMessage("Pi 快速提问运行中…")

        def job(is_cancelled=None):
            full_prompt = prompt
            if attachments:
                description, error = self._describe_attachments(attachments, prompt)
                if error is not None:
                    return {
                        "ok": False,
                        "returncode": -1,
                        "stdout": "",
                        "stderr": "",
                        "latency_ms": 0,
                        "error": error,
                    }
                full_prompt = self._append_image_prompt(description or "", prompt)
            result = extras.chat_with_failover(
                full_prompt,
                provider=provider,
                model=model,
                workdir=workdir,
                thinking=thinking,
                is_cancelled=is_cancelled,
            )
            if attachments:
                result["vision_text"] = description or ""
            return result

        w = self._track(Worker(job))
        self._chat_worker = w
        self._set_chat_busy(True)
        w.done.connect(self._on_basic_chat_done)
        w.failed.connect(self._on_basic_chat_fail)
        w.start()

    def _on_basic_chat_done(self, result):
        self._chat_worker = None
        self._set_chat_busy(False)
        if isinstance(result, dict):
            p = result.get("provider") or ""
            m = result.get("model") or ""
            if result.get("switched") and p and m:
                try:
                    self._set_chat_combo_text(self.chat_provider, str(p))
                    self._reload_chat_models_for_provider(str(p), prefer_model=str(m))
                    self._set_chat_combo_text(self.chat_model, str(m))
                    self.refresh_dashboard()
                except Exception:
                    pass
                if result.get("notice"):
                    self.chat_output.appendPlainText(f"[{result.get('notice')}]")
                else:
                    self.status.showMessage(f"已自动切换模型 → {p}/{m}", 5000)
            out = (result.get("stdout") or "").strip()
            err = (result.get("stderr") or "").strip()
            code = result.get("returncode")
            if out:
                self.chat_output.appendPlainText(out)
            if err and not result.get("ok"):
                self.chat_output.appendPlainText(f"[stderr]\n{err}")
            vision_text = result.get("vision_text") or ""
            if vision_text:
                self.chat_output.appendPlainText(
                    f"— 识图结果（已作为上下文交给对话模型）—\n{vision_text[:2000]}\n"
                )
            self.chat_output.appendPlainText(f"\n[exit {code} · {p}/{m}]")
            self.status.showMessage("快速提问完成" if result.get("ok") else "快速提问失败")
            if result.get("ok") and hasattr(self.chat_input, "clear_attachments"):
                self.chat_input.clear_attachments()
                try:
                    self._on_chat_attachments_changed()
                except Exception:
                    pass
            return
        # 兼容旧 tuple 返回
        try:
            code, out, err = result
        except (TypeError, ValueError):
            self.chat_output.appendPlainText("[错误] 返回格式无法解析")
            self.status.showMessage("快速提问失败")
            return
        if out.strip():
            self.chat_output.appendPlainText(out.strip())
        if err.strip():
            self.chat_output.appendPlainText(f"[stderr]\n{err.strip()[:500]}")
        self.chat_output.appendPlainText(f"\n[exit {code}]")
        self.status.showMessage("快速提问完成")

    def _on_basic_chat_fail(self, e: str):
        self._chat_worker = None
        self._set_chat_busy(False)
        self.chat_output.appendPlainText(f"[错误] {e}")
        self.status.showMessage("快速提问失败")

    def chat_send_enhanced(self):
        prompt = self.chat_input.toPlainText().strip()
        attachments = (
            self.chat_input.attachments() if hasattr(self.chat_input, "attachments") else []
        )
        if not prompt and not attachments:
            return
        if hasattr(self, "_chat_combo_text"):
            provider = self._chat_combo_text(self.chat_provider) or None
            model = self._chat_combo_text(self.chat_model) or None
        else:
            provider = self.chat_provider.currentText().strip() if hasattr(self.chat_provider, "currentText") else self.chat_provider.text().strip()
            model = self.chat_model.currentText().strip() if hasattr(self.chat_model, "currentText") else self.chat_model.text().strip()
            provider = provider or None
            model = model or None
        # Images first: run each attachment through the built-in free vision
        # model (Zhipu GLM-4.6V-Flash) and turn descriptions into text the
        # chat model can understand.
        if attachments:
            if not core.zhipu_api_key():
                QMessageBox.warning(
                    self,
                    "未配置识图模型",
                    "已附加图片，但未配置智谱 API Key。\n\n"
                    "请在「设置 → 识图模型」填入智谱 API Key（免费申请：\n"
                    "https://bigmodel.cn，GLM-4.6V-Flash 免费额度）。",
                )
                return
            self.chat_output.appendPlainText(
                f"…正在用内置免费识图模型识别 {len(attachments)} 张图片…"
            )
        # A persistent RPC session already holds the conversation in-process;
        # only the legacy one-shot path needs history stitched into the prompt.
        use_rpc = False
        try:
            from ... import rpc_session

            use_rpc = rpc_session.rpc_chat_enabled()
        except Exception:
            use_rpc = False
        if use_rpc:
            full = prompt
        else:
            # Keep the request context within both turn and byte budgets.
            history_lines = []
            context_bytes = 0
            for turn in reversed(self.chat_history[-6:]):
                lines = [
                    f"User: {turn.get('user', '')}",
                    f"Assistant: {turn.get('assistant', '')}",
                ]
                size = len("\n".join(lines).encode("utf-8"))
                if context_bytes + size > 128 * 1024:
                    break
                history_lines[0:0] = lines
                context_bytes += size
            if history_lines:
                full = "以下是近期对话，请承接上下文简要回答。\n" + "\n".join(history_lines) + f"\nUser: {prompt}\nAssistant:"
            else:
                full = prompt
            encoded = full.encode("utf-8")
            if len(encoded) > 128 * 1024:
                full = encoded[-128 * 1024 :].decode("utf-8", errors="ignore")
        if hasattr(self, "chat_context_badge") and self.chat_context_badge is not None:
            if use_rpc:
                self.chat_context_badge.set_status("success", "常驻会话 · 上下文保留")
            else:
                self.chat_context_badge.set_status("info", "一次性模式")
        self.chat_output.appendPlainText(f"\n你: {prompt or '[图片]'}\n…思考中…")
        self._last_chat_prompt = prompt
        workdir = self.workdir_edit.text().strip() or str(core.user_home())
        thinking = "off"
        try:
            thinking = self.thinking_combo.currentText() or "off"
        except Exception:
            pass

        def job(is_cancelled=None):
            # 连续失败达阈值后自动切换下一个收藏/启用模型并重试（无感）
            full_prompt = prompt
            if attachments:
                description, error = self._describe_attachments(attachments, prompt)
                if error is not None:
                    return {
                        "ok": False,
                        "returncode": -1,
                        "stdout": "",
                        "stderr": "",
                        "latency_ms": 0,
                        "error": error,
                    }
                full_prompt = self._append_image_prompt(description or "", prompt)
            result = extras.chat_with_failover(
                full_prompt,
                provider=provider,
                model=model,
                workdir=workdir,
                thinking=thinking,
                is_cancelled=is_cancelled,
            )
            if attachments:
                result["vision_text"] = description or ""
            return result

        w = self._track(self._worker_fn(job))
        self._chat_worker = w
        self._set_chat_busy(True)
        w.done.connect(lambda r, u=prompt: self._on_enhanced_chat_done(r, u))
        w.failed.connect(self._on_enhanced_chat_fail)
        w.start()

    def _on_enhanced_chat_done(self, result: dict, user_prompt: str):
        self._chat_worker = None
        self._set_chat_busy(False)
        text = (result.get("stdout") or "").strip() or (result.get("stderr") or "").strip()
        p = result.get("provider") or ""
        m = result.get("model") or ""
        # 若发生故障切换，同步 UI 下拉与默认，但不刷屏打扰
        if result.get("switched") and p and m:
            try:
                if hasattr(self, "_set_chat_combo_text"):
                    self._set_chat_combo_text(self.chat_provider, str(p))
                    self._reload_chat_models_for_provider(str(p), prefer_model=str(m))
                    self._set_chat_combo_text(self.chat_model, str(m))
                self.refresh_dashboard()
                self.settings_load()
            except Exception as e:
                # 自动换模已经生效但界面没跟上：静默会让用户以为还在用原模型。
                logger.warning("refresh after chat model failover failed: %s", e)
            notice = (result.get("notice") or "").strip()
            if notice:
                self.chat_output.appendPlainText(f"[{notice}]")
            else:
                # 无感：仅状态栏轻提示
                self.status.showMessage(f"已自动切换模型 → {p}/{m}", 5000)
        if not result.get("ok"):
            err = (result.get("error") or text or "未知错误")[:500]
            self.chat_output.appendPlainText(f"失败({result.get('returncode')}): {err}")
            return
        vision_text = result.get("vision_text") or ""
        if vision_text:
            self.chat_output.appendPlainText(f"— 识图结果（已作为上下文交给对话模型）—\n{vision_text[:2000]}\n")
        if hasattr(self, "chat_input") and hasattr(self.chat_input, "clear_attachments"):
            self.chat_input.clear_attachments()
        self._on_chat_attachments_changed()
        self.chat_history.append({"user": user_prompt, "assistant": text})
        self.chat_history = self.chat_history[-20:]
        while self.chat_history and len(
            json.dumps(self.chat_history, ensure_ascii=False).encode("utf-8")
        ) > 512 * 1024:
            self.chat_history.pop(0)
        lat = result.get("latency_ms")
        tag = f"{p}/{m} · {lat} ms" if p and m else f"{lat} ms"
        self.chat_output.appendPlainText(f"Pi ({tag}):\n{text}\n")

    def _on_enhanced_chat_fail(self, err: str):
        self._chat_worker = None
        self._set_chat_busy(False)
        self.chat_output.appendPlainText(f"错误: {err}")
