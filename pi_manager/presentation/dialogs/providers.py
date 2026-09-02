"""Provider 相关对话框：编辑、密钥池、从 API 拉取模型。

从 ``ui.py`` 下沉。``pi_manager.ui`` 继续 re-export，保持现有测试导入稳定。
"""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import core
from ... import provider_presets
from ...remote_models import models_from_json_text
from ..components import CollapsibleSection, RemoteModelPicker
from ..geometry import clamp_dialog_to_screen
from ..workers import Worker, WorkerTrackerMixin


class ProviderEditorDialog(WorkerTrackerMixin, QDialog):
    def __init__(self, parent=None, existing: dict[str, Any] | None = None, name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("编辑自定义 Provider" if existing else "添加自定义 Provider")
        clamp_dialog_to_screen(self, 720, 700)
        self.existing = existing or {}
        self._worker = None
        self._init_workers()
        self._fetched_models: list[dict[str, Any]] = []
        self._use_picker = False
        self._syncing_json = False
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("自定义（手动填写）", "")
        for preset in provider_presets.list_presets():
            self.preset_combo.addItem(
                f"{preset.get('region') or ''} · {preset.get('label')}",
                preset.get("name"),
            )
        self.preset_combo.currentIndexChanged.connect(self._apply_preset)
        form.addRow("常用模板", self.preset_combo)
        preset_tip = QLabel("模板会填好 Base URL、API 类型和常用模型；你只需粘贴 API Key。")
        preset_tip.setObjectName("subtitle")
        preset_tip.setWordWrap(True)
        form.addRow("", preset_tip)

        self.name_edit = QLineEdit(name)
        self.name_edit.setEnabled(not bool(name))
        self.base_url = QLineEdit(self.existing.get("baseUrl", "https://api.openai.com/v1"))
        self.base_url.setPlaceholderText("例如 https://api.openai.com/v1 或 http://localhost:11434/v1")
        self.api = QComboBox()
        self.api.addItems([
            "openai-completions",
            "openai-responses",
            "anthropic-messages",
            "google-generative-ai",
        ])
        api_val = self.existing.get("api", "openai-completions")
        idx = self.api.findText(api_val)
        if idx >= 0:
            self.api.setCurrentIndex(idx)
        self.api_key = QLineEdit(str(self.existing.get("apiKey", "")))
        self.api_key.setPlaceholderText("字面量 / 环境变量名 / !command")
        self.api_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)

        self.compat_dev = QCheckBox("Developer 角色")
        self.compat_dev.setToolTip(
            "接口是否支持 developer 角色消息。多数中转可不勾选。"
        )
        self.compat_reason = QCheckBox("推理强度 / Thinking")
        self.compat_reason.setToolTip(
            "接口是否支持调节 thinking/reasoning 强度。支持思考的模型建议勾选。"
        )
        compat = self.existing.get("compat") or {}
        self.compat_dev.setChecked(bool(compat.get("supportsDeveloperRole", False)))
        self.compat_reason.setChecked(bool(compat.get("supportsReasoningEffort", True)))
        compat_box = QWidget()
        compat_row = QHBoxLayout(compat_box)
        compat_row.setContentsMargins(0, 0, 0, 0)
        compat_row.addWidget(self.compat_dev)
        compat_row.addWidget(self.compat_reason)
        compat_row.addStretch(1)

        form.addRow("名称", self.name_edit)
        form.addRow("Base URL", self.base_url)
        form.addRow("API", self.api)
        form.addRow("API Key", self.api_key)
        form.addRow("兼容选项", compat_box)
        layout.addLayout(form)

        fetch_row = QHBoxLayout()
        self.btn_fetch = QPushButton("拉取上游模型")
        self.btn_fetch.setProperty("success", True)
        self.btn_fetch.clicked.connect(self.fetch_models)
        self.fetch_status = QLabel("拉取后可搜索、勾选；保存时只写入已勾选的模型。")
        self.fetch_status.setObjectName("subtitle")
        self.fetch_status.setWordWrap(True)
        fetch_row.addWidget(self.btn_fetch)
        fetch_row.addWidget(self.fetch_status, 1)
        layout.addLayout(fetch_row)

        self.picker = RemoteModelPicker()
        self.model_pick = self.picker.list
        self.picker.checkedChanged.connect(self._sync_json_from_picker)
        layout.addWidget(self.picker, 1)

        self.models_text = QPlainTextEdit()
        models = self.existing.get("models") or []
        self.models_text.setPlainText(
            json.dumps(models, ensure_ascii=False, indent=2) if models else "[]"
        )
        self.models_text.setMinimumHeight(90)
        self.models_text.textChanged.connect(self._on_json_edited)
        advanced = CollapsibleSection(
            "高级：直接编辑 Models JSON",
            "一般不用打开。用上面的搜索勾选即可；这里留给手改或模板预填。",
        )
        advanced.body_layout.addWidget(self.models_text)
        layout.addWidget(advanced)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        # 显式默认按钮：不依赖各平台 QDialogButtonBox 的隐式行为，回车即保存。
        buttons.button(QDialogButtonBox.Save).setDefault(True)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_preset(self) -> None:
        """选择模板后自动填充接口参数与模型列表（编辑已有 Provider 时保留名称）。"""
        name = str(self.preset_combo.currentData() or "")
        preset = provider_presets.find_preset(name) if name else None
        if not preset:
            return
        if self.name_edit.isEnabled():
            self.name_edit.setText(str(preset.get("name") or ""))
        self.base_url.setText(str(preset.get("base_url") or ""))
        api = str(preset.get("api") or "openai-completions")
        idx = self.api.findText(api)
        self.api.setCurrentIndex(idx if idx >= 0 else 0)
        compat = preset.get("compat") or {}
        self.compat_dev.setChecked(bool(compat.get("supportsDeveloperRole", False)))
        self.compat_reason.setChecked(bool(compat.get("supportsReasoningEffort", True)))
        self._use_picker = False
        self._syncing_json = True
        self.models_text.setPlainText(
            json.dumps(preset.get("models") or [], ensure_ascii=False, indent=2)
        )
        self._syncing_json = False
        self._fetched_models = []
        self.picker.set_models([])
        hint = str(preset.get("hint") or "")
        key_url = str(preset.get("key_url") or "")
        text = hint
        if key_url:
            text += f"\n获取 API Key：{key_url}"
        text += "\n填写 API Key 后可直接保存，或拉取上游目录后搜索勾选。"
        self.fetch_status.setText(text)

    def closeEvent(self, event):
        if self._reap_workers():
            # 网络请求卡住（防火墙丢包时 socket 超时可远超预算）：worker 已脱钩，
            # 关闭窗口不再连带销毁运行中的 QThread。
            self._note_detached_workers()
        super().closeEvent(event)

    def fetch_models(self):
        base = self.base_url.text().strip()
        key = self.api_key.text().strip()
        api = self.api.currentText()
        if not base:
            QMessageBox.warning(self, "缺少 Base URL", "请先填写 Base URL")
            return
        self.btn_fetch.setEnabled(False)
        self.fetch_status.setText("正在请求模型列表…")
        self._worker = self._track(
            Worker(
                core.fetch_remote_models,
                base,
                key,
                api=api,
                provider=self.name_edit.text().strip(),
            )
        )
        self._worker.done.connect(self._on_fetch_done)
        self._worker.failed.connect(self._on_fetch_fail)
        self._worker.start()

    def _on_fetch_done(self, result: dict):
        self.btn_fetch.setEnabled(True)
        if not result.get("ok"):
            self.fetch_status.setText(f"拉取失败：{result.get('error')}")
            QMessageBox.warning(self, "拉取失败", str(result.get("error") or "unknown"))
            return
        models = result.get("models") or []
        self._fetched_models = models
        self._use_picker = True
        already: set[str] = set()
        try:
            already = {
                str(item.get("id") or item.get("name") or "").strip()
                for item in models_from_json_text(self.models_text.toPlainText())
                if isinstance(item, dict)
            }
            already.discard("")
        except Exception:
            already = set()
        self.picker.set_models(models, checked_ids=already)
        self.fetch_status.setText(
            f"成功：{len(models)} 个模型  |  endpoint: {result.get('endpoint')}"
            "  ·  搜索后勾选要接入的，不会默认写入全部。"
        )
        self.picker.search.setFocus()

    def _on_fetch_fail(self, err: str):
        self.btn_fetch.setEnabled(True)
        self.fetch_status.setText(f"拉取失败：{err}")
        QMessageBox.warning(self, "拉取失败", err)

    def _selected_ids(self) -> set[str]:
        return self.picker.checked_ids()

    def apply_selected_models(self):
        if not self._fetched_models:
            QMessageBox.information(self, "提示", "请先拉取模型")
            return
        chosen = self.picker.checked_models()
        if not chosen:
            QMessageBox.information(self, "提示", "请至少勾选一个模型")
            return
        self._use_picker = True
        self._sync_json_from_picker()
        self.fetch_status.setText(f"已勾选 {len(chosen)} 个模型，保存时写入 Provider")

    def apply_all_models(self):
        if not self._fetched_models:
            QMessageBox.information(self, "提示", "请先拉取模型")
            return
        self.picker.set_models(
            self._fetched_models,
            checked_ids={str(item.get("id") or "") for item in self._fetched_models},
        )
        self._use_picker = True
        self.fetch_status.setText(
            f"已勾选全部 {len(self._fetched_models)} 个模型，保存时会全部写入"
        )

    def _sync_json_from_picker(self, _count: int = 0) -> None:
        if not self._use_picker:
            return
        chosen = self.picker.checked_models()
        if not chosen:
            return
        self._syncing_json = True
        try:
            self.models_text.setPlainText(
                json.dumps(chosen, ensure_ascii=False, indent=2)
            )
        finally:
            self._syncing_json = False

    def _on_json_edited(self) -> None:
        if self._syncing_json:
            return
        self._use_picker = False

    def result_data(self) -> tuple[str, dict[str, Any]]:
        name = self.name_edit.text().strip()
        if self._use_picker:
            models = self.picker.checked_models()
            if not models:
                raise ValueError("请先搜索并勾选要接入的模型；不会默认写入全部上游模型")
        else:
            try:
                models = models_from_json_text(self.models_text.toPlainText())
            except json.JSONDecodeError as e:
                raise ValueError(f"Models JSON 无效: {e}") from e
        if not name:
            raise ValueError("名称不能为空")
        if not self.base_url.text().strip():
            raise ValueError("Base URL 不能为空")
        if not isinstance(models, list):
            raise ValueError("Models 必须是数组")
        data = {
            "baseUrl": self.base_url.text().strip(),
            "api": self.api.currentText(),
            "apiKey": self.api_key.text().strip(),
            "models": models,
            "compat": {
                "supportsDeveloperRole": self.compat_dev.isChecked(),
                "supportsReasoningEffort": self.compat_reason.isChecked(),
            },
        }
        return name, data


class ProviderKeysDialog(QDialog):
    def __init__(self, provider: str, parent=None):
        super().__init__(parent)
        self.provider = provider
        self._reveal = False
        self.setWindowTitle(f"API Keys · {provider}")
        clamp_dialog_to_screen(self, 760, 460)

        layout = QVBoxLayout(self)
        title = QLabel(f"Provider「{provider}」的 API Key 池")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        hint = QLabel(
            "请求遇到鉴权、限流或额度错误时，会将当前 Key 暂时标记为失效并切换下一把。"
            "Key 默认掩码显示，可切换明文或直接复制。"
        )
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["API Key", "状态", "当前", "失败时间", "失败原因"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("subtitle")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        row = QHBoxLayout()
        add_btn = QPushButton("添加 Key")
        add_btn.clicked.connect(self.add_key)
        self.reveal_btn = QPushButton("显示明文")
        self.reveal_btn.setProperty("secondary", True)
        self.reveal_btn.setCheckable(True)
        self.reveal_btn.clicked.connect(self.on_reveal_toggled)
        copy_btn = QPushButton("复制选中")
        copy_btn.setProperty("secondary", True)
        copy_btn.clicked.connect(self.copy_key)
        delete_btn = QPushButton("删除")
        delete_btn.setProperty("danger", True)
        delete_btn.clicked.connect(self.delete_key)
        restore_btn = QPushButton("恢复选中")
        restore_btn.setProperty("secondary", True)
        restore_btn.clicked.connect(self.restore_key)
        restore_all_btn = QPushButton("恢复全部失效 Key")
        restore_all_btn.setProperty("secondary", True)
        restore_all_btn.clicked.connect(self.restore_all)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        row.addWidget(add_btn)
        row.addWidget(self.reveal_btn)
        row.addWidget(copy_btn)
        row.addWidget(delete_btn)
        row.addWidget(restore_btn)
        row.addWidget(restore_all_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)
        self.refresh()

    def on_reveal_toggled(self, checked: bool) -> None:
        self._reveal = bool(checked)
        self.reveal_btn.setText("隐藏明文" if self._reveal else "显示明文")
        if self._reveal:
            self.status_label.setText("已切换为明文显示，请注意周围环境与屏幕共享风险。")
        else:
            self.status_label.setText("已恢复掩码显示。")
        current = self.table.currentRow()
        self.refresh()
        if 0 <= current < self.table.rowCount():
            self.table.selectRow(current)

    def copy_key(self) -> None:
        """复制选中 Key 的明文到剪贴板（掩码模式下同样可用）。"""
        key_id = self.selected_key_id()
        if not key_id:
            self.status_label.setText("请先选择一把 Key。")
            return
        rows = core.list_provider_api_keys(self.provider, reveal=True)
        value = next(
            (str(row.get("value") or "") for row in rows if row.get("id") == key_id), ""
        )
        if not value:
            self.status_label.setText("未找到选中的 Key，可能已被删除。")
            return
        QApplication.clipboard().setText(value)
        self.status_label.setText("已复制选中 Key 的明文到剪贴板，请注意保管。")

    def refresh(self):
        rows = core.list_provider_api_keys(self.provider, reveal=self._reveal)
        self.table.setRowCount(len(rows))
        for index, meta in enumerate(rows):
            display = str(meta.get("value") or "") if self._reveal else str(meta.get("masked") or "")
            key_item = QTableWidgetItem(display)
            key_item.setData(Qt.UserRole, str(meta.get("id") or ""))
            self.table.setItem(index, 0, key_item)
            status = "可用" if meta.get("status") == "available" else "失效"
            self.table.setItem(index, 1, QTableWidgetItem(status))
            self.table.setItem(index, 2, QTableWidgetItem("是" if meta.get("active") else ""))
            self.table.setItem(index, 3, QTableWidgetItem(str(meta.get("failed_at") or "")))
            reason_item = QTableWidgetItem(str(meta.get("failure_reason") or ""))
            reason_item.setToolTip(str(meta.get("failure_reason") or ""))
            self.table.setItem(index, 4, reason_item)
        if rows:
            self.table.selectRow(0)

    def selected_key_id(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return str(item.data(Qt.UserRole) or "") if item else ""

    def add_key(self):
        value, ok = QInputDialog.getText(
            self, "添加 API Key", "API Key：", QLineEdit.Password
        )
        if not ok or not value.strip():
            return
        try:
            core.add_provider_api_key(self.provider, value.strip())
            self.refresh()
        except Exception as exc:
            QMessageBox.warning(self, "添加失败", str(exc))

    def delete_key(self):
        key_id = self.selected_key_id()
        if not key_id:
            QMessageBox.information(self, "提示", "请先选择一把 Key")
            return
        if QMessageBox.question(
            self,
            "删除 API Key",
            f"将从「{self.provider}」的密钥池中永久删除选中的 Key。\n\n"
            "该 Provider 将无法再用这把 Key 发起请求；不会删除 models.json 里的模型列表。确定删除？",
        ) != QMessageBox.Yes:
            return
        core.remove_provider_api_key(self.provider, key_id)
        self.refresh()

    def restore_key(self):
        key_id = self.selected_key_id()
        if not key_id:
            QMessageBox.information(self, "提示", "请先选择一把 Key")
            return
        core.restore_provider_api_key(self.provider, key_id)
        self.refresh()

    def restore_all(self):
        restored = core.restore_all_provider_api_keys(self.provider)
        self.refresh()
        QMessageBox.information(self, "恢复完成", f"已恢复 {restored} 把 Key")


class FetchModelsDialog(WorkerTrackerMixin, QDialog):
    """Standalone: baseUrl + apiKey -> list models -> save provider."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("从 BaseURL + API Key 获取模型")
        clamp_dialog_to_screen(self, 720, 620)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit("custom")
        self.base_url = QLineEdit("https://api.openai.com/v1")
        self.base_url.setPlaceholderText("https://api.openai.com/v1  或你的中转地址/v1")
        self.api = QComboBox()
        self.api.addItems([
            "openai-completions",
            "openai-responses",
            "anthropic-messages",
            "google-generative-ai",
        ])
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.api_key.setPlaceholderText("必填：sk-... 真实密钥，或 OPENAI_API_KEY 环境变量名")
        self.proxy = QLineEdit()
        self.proxy.setPlaceholderText("可选：http://127.0.0.1:7890（留空则用系统 HTTPS_PROXY）")
        self.insecure_ssl = QCheckBox("忽略 SSL 证书校验（仅排查网络/中转问题时使用）")
        form.addRow("Provider 名称", self.name_edit)
        form.addRow("Base URL", self.base_url)
        form.addRow("API 类型", self.api)
        form.addRow("API Key", self.api_key)
        form.addRow("代理 Proxy", self.proxy)
        form.addRow("", self.insecure_ssl)
        layout.addLayout(form)

        tip = QLabel(
            "空 API Key 会 401。拉取后请搜索并勾选要接入的模型，保存时只写入已勾选项。"
        )
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.status = QLabel("填写 Base URL 与 API Key 后点击拉取")
        self.status.setObjectName("subtitle")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.picker = RemoteModelPicker()
        self.list = self.picker.list
        layout.addWidget(self.picker, 1)

        row = QHBoxLayout()
        self.btn_fetch = QPushButton("拉取可用模型")
        self.btn_fetch.setProperty("success", True)
        self.btn_fetch.clicked.connect(self._fetch)
        self.btn_save = QPushButton("保存到 models.json")
        self.btn_save.setProperty("success", True)
        self.btn_save.clicked.connect(self._save)
        self.btn_close = QPushButton("关闭")
        self.btn_close.setProperty("secondary", True)
        self.btn_close.clicked.connect(self.reject)
        row.addWidget(self.btn_fetch)
        row.addWidget(self.btn_save)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        layout.addLayout(row)
        self._models: list[dict[str, Any]] = []
        self._worker = None
        self._init_workers()

    def closeEvent(self, event):
        if self._reap_workers():
            # 网络请求卡住（防火墙丢包时 socket 超时可远超预算）：worker 已脱钩，
            # 关闭窗口不再连带销毁运行中的 QThread。
            self._note_detached_workers()
        super().closeEvent(event)

    def _fetch(self):
        base = self.base_url.text().strip()
        key = self.api_key.text().strip()
        if not base:
            QMessageBox.warning(self, "提示", "请填写 Base URL")
            return
        if not key and self.api.currentText() not in ("google-generative-ai",):
            QMessageBox.warning(
                self,
                "提示",
                "请填写 API Key。\n\n"
                "第一张报错「Missing bearer authentication」就是因为没有带上 Bearer Token。\n"
                "可直接粘贴 sk-...，或填已设置的环境变量名。",
            )
            return
        self.btn_fetch.setEnabled(False)
        self.status.setText("请求中…（若长时间无响应，请检查网络/代理）")
        self._worker = self._track(
            Worker(
                core.fetch_remote_models,
                base,
                key,
                api=self.api.currentText(),
                insecure_ssl=self.insecure_ssl.isChecked(),
                proxy=self.proxy.text().strip(),
            )
        )
        self._worker.done.connect(self._done)
        self._worker.failed.connect(lambda e: self._fail(e))
        self._worker.start()

    def _done(self, result: dict):
        self.btn_fetch.setEnabled(True)
        if not result.get("ok"):
            err = str(result.get("error") or "unknown")
            endpoint = result.get("endpoint") or ""
            proxy = result.get("proxy") or ""
            extra = ""
            if endpoint:
                extra += f"\n\nendpoint: {endpoint}"
            if proxy:
                extra += f"\nproxy: {proxy}"
            self.status.setText(f"失败：{err}{extra}")
            QMessageBox.warning(self, "拉取失败", err + extra)
            return
        self._models = result.get("models") or []
        self.picker.set_models(self._models)
        proxy = result.get("proxy") or ""
        px = f" | proxy={proxy}" if proxy else ""
        self.status.setText(
            f"成功获取 {len(self._models)} 个模型 | {result.get('endpoint')}{px}"
            "  ·  搜索后勾选再保存"
        )
        self.picker.search.setFocus()

    def _fail(self, e: str):
        self.btn_fetch.setEnabled(True)
        self.status.setText(f"失败：{e}")
        QMessageBox.warning(self, "失败", e)

    def _save(self):
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "提示", "请填写 Provider 名称")
            return
        if not self._models:
            QMessageBox.warning(self, "提示", "请先拉取模型")
            return
        chosen = self.picker.checked_models()
        if not chosen:
            QMessageBox.warning(self, "提示", "请先搜索并勾选要接入的模型")
            return
        core.upsert_custom_provider(
            name,
            base_url=self.base_url.text().strip(),
            api=self.api.currentText(),
            api_key=self.api_key.text().strip(),
            models=chosen,
            compat={"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        )
        parent = self.parent()
        notify = getattr(parent, "notify_success", None)
        msg = f"Provider「{name}」已写入，共 {len(chosen)} 个模型"
        if callable(notify):
            notify(msg)
        else:
            QMessageBox.information(self, "已保存", msg)
        self.accept()
