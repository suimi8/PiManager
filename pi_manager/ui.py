"""Main window UI for Pi Manager."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread, Signal, QUrl
from PySide6.QtGui import QColor, QDragEnterEvent, QDropEvent, QDragMoveEvent
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from . import core
from . import extras
from .presentation.design import ACCENT_LABELS, apply_app_font, normalize_mode
from .ui_features import FeatureMixin


APP_STYLE = ""  # styling comes from presentation.design.build_stylesheet



class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs

    def run(self):
        try:
            self.done.emit(self.fn(*self.args, **self.kwargs))
        except Exception as e:
            self.failed.emit(str(e))


class BatchTestWorker(QThread):
    """Run concurrent model tests and emit each result as it completes."""

    progress = Signal(object)  # one result dict
    done = Signal(object)  # full ordered list
    failed = Signal(str)

    def __init__(
        self,
        pairs: list[tuple[str, str]],
        *,
        mode: str = "auto",
        workdir: str = "",
        timeout: float | None = None,
        kind: str = "model",  # model | health
        health_scope: str = "favorites",
        health_selected: list[tuple[str, str]] | None = None,
    ):
        super().__init__()
        self.pairs = list(pairs or [])
        self.mode = mode
        self.workdir = workdir
        self.timeout = timeout
        self.kind = kind
        self.health_scope = health_scope
        self.health_selected = health_selected or []

    def run(self):
        try:
            if self.kind == "health":
                def on_one(res):
                    self.progress.emit(res)

                result = extras.run_health_check(
                    pairs=self.pairs or None,
                    mode=self.mode,
                    scope=self.health_scope,
                    selected=self.health_selected,
                    on_one=on_one,
                )
                self.done.emit(result)
                return

            timeout = self.timeout if self.timeout is not None else (90 if self.mode == "pi" else 45)

            def on_one(res):
                self.progress.emit(res)

            results = extras.test_models_batch_concurrent(
                self.pairs,
                mode=self.mode,
                timeout=timeout,
                workdir=self.workdir or None,
                max_workers=extras.get_test_concurrency(),
                on_one=on_one,
                append_history_each=True,
                is_cancelled=self.isInterruptionRequested,
            )
            self.done.emit(results)
        except Exception as e:
            self.failed.emit(str(e))



class ProviderEditorDialog(QDialog):
    def __init__(self, parent=None, existing: dict[str, Any] | None = None, name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("编辑自定义 Provider" if existing else "添加自定义 Provider")
        self.resize(680, 640)
        self.existing = existing or {}
        self._worker = None
        layout = QVBoxLayout(self)
        form = QFormLayout()

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

        self.models_text = QPlainTextEdit()
        models = self.existing.get("models") or []
        self.models_text.setPlainText(json.dumps(models, ensure_ascii=False, indent=2) if models else "[]")

        self.compat_dev = QCheckBox("支持 Developer 角色（supportsDeveloperRole）")
        self.compat_dev.setToolTip(
            "接口是否支持 developer 角色消息。\n"
            "部分 OpenAI 兼容中转支持；不确定时请关闭，避免请求被拒。"
        )
        self.compat_reason = QCheckBox("支持推理强度 / Thinking（supportsReasoningEffort）")
        self.compat_reason.setToolTip(
            "接口是否支持调节 reasoning/thinking 强度。\n"
            "支持 thinking 的模型建议勾选；不支持时请关闭，防止参数报错。"
        )
        compat = self.existing.get("compat") or {}
        self.compat_dev.setChecked(bool(compat.get("supportsDeveloperRole", False)))
        self.compat_reason.setChecked(bool(compat.get("supportsReasoningEffort", True)))

        self.fetch_status = QLabel("")
        self.fetch_status.setObjectName("subtitle")
        self.fetch_status.setWordWrap(True)

        self.model_pick = QListWidget()
        self.model_pick.setSelectionMode(QAbstractItemView.MultiSelection)
        self.model_pick.setMinimumHeight(140)

        form.addRow("名称", self.name_edit)
        form.addRow("Base URL", self.base_url)
        form.addRow("API", self.api)
        form.addRow("API Key", self.api_key)
        form.addRow("兼容选项", self.compat_dev)
        form.addRow("", self.compat_reason)
        compat_hint = QLabel(
            "兼容选项说明：\n"
            "· 支持 Developer 角色：能否使用 developer 消息角色（多数中转可不勾选）。\n"
            "· 支持推理强度：能否设置 thinking/reasoning 级别（支持思考的模型建议勾选）。\n"
            "这两个开关会写入 models.json 的 compat 字段，供官方 Pi 识别接口能力。"
        )
        compat_hint.setObjectName("subtitle")
        compat_hint.setWordWrap(True)
        form.addRow("", compat_hint)
        layout.addLayout(form)

        fetch_row = QHBoxLayout()
        self.btn_fetch = QPushButton("用 BaseURL + API Key 拉取可用模型")
        self.btn_fetch.setProperty("success", True)
        self.btn_fetch.clicked.connect(self.fetch_models)
        self.btn_apply_selected = QPushButton("将勾选模型写入 Models JSON")
        self.btn_apply_selected.setProperty("secondary", True)
        self.btn_apply_selected.clicked.connect(self.apply_selected_models)
        self.btn_apply_all = QPushButton("全部写入")
        self.btn_apply_all.setProperty("secondary", True)
        self.btn_apply_all.clicked.connect(self.apply_all_models)
        fetch_row.addWidget(self.btn_fetch)
        fetch_row.addWidget(self.btn_apply_selected)
        fetch_row.addWidget(self.btn_apply_all)
        layout.addLayout(fetch_row)
        layout.addWidget(self.fetch_status)
        layout.addWidget(QLabel("远程模型列表（多选）"))
        layout.addWidget(self.model_pick, 1)
        layout.addWidget(QLabel("Models JSON（可手改）"))
        layout.addWidget(self.models_text, 1)

        self._fetched_models: list[dict[str, Any]] = []

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def fetch_models(self):
        base = self.base_url.text().strip()
        key = self.api_key.text().strip()
        api = self.api.currentText()
        if not base:
            QMessageBox.warning(self, "缺少 Base URL", "请先填写 Base URL")
            return
        self.btn_fetch.setEnabled(False)
        self.fetch_status.setText("正在请求模型列表…")
        self._worker = Worker(
            core.fetch_remote_models,
            base,
            key,
            api=api,
            provider=self.name_edit.text().strip(),
        )
        self._worker.done.connect(self._on_fetch_done)
        self._worker.failed.connect(self._on_fetch_fail)
        self._worker.start()

    def _on_fetch_done(self, result: dict):
        self.btn_fetch.setEnabled(True)
        if not result.get("ok"):
            self.fetch_status.setText(f"拉取失败：{result.get('error')}\nendpoint: {result.get('endpoint')}")
            QMessageBox.warning(self, "拉取失败", str(result.get("error") or "unknown"))
            return
        models = result.get("models") or []
        self._fetched_models = models
        self.model_pick.clear()
        for m in models:
            item = QListWidgetItem(f"{m.get('id')}")
            item.setSelected(True)
            self.model_pick.addItem(item)
        # select all
        for i in range(self.model_pick.count()):
            self.model_pick.item(i).setSelected(True)
        self.fetch_status.setText(
            f"成功：{len(models)} 个模型  |  endpoint: {result.get('endpoint')}"
        )
        # auto-fill JSON with all if empty
        try:
            cur = json.loads(self.models_text.toPlainText() or "[]")
        except Exception:
            cur = []
        if not cur:
            self.models_text.setPlainText(json.dumps(models, ensure_ascii=False, indent=2))

    def _on_fetch_fail(self, err: str):
        self.btn_fetch.setEnabled(True)
        self.fetch_status.setText(f"拉取失败：{err}")
        QMessageBox.warning(self, "拉取失败", err)

    def _selected_ids(self) -> set[str]:
        ids = set()
        for item in self.model_pick.selectedItems():
            ids.add(item.text().strip())
        return ids

    def apply_selected_models(self):
        if not self._fetched_models:
            QMessageBox.information(self, "提示", "请先拉取模型")
            return
        ids = self._selected_ids()
        chosen = [m for m in self._fetched_models if m.get("id") in ids]
        if not chosen:
            QMessageBox.information(self, "提示", "请至少选择一个模型")
            return
        self.models_text.setPlainText(json.dumps(chosen, ensure_ascii=False, indent=2))
        self.fetch_status.setText(f"已写入 {len(chosen)} 个模型到 Models JSON")

    def apply_all_models(self):
        if not self._fetched_models:
            QMessageBox.information(self, "提示", "请先拉取模型")
            return
        self.models_text.setPlainText(json.dumps(self._fetched_models, ensure_ascii=False, indent=2))
        for i in range(self.model_pick.count()):
            self.model_pick.item(i).setSelected(True)
        self.fetch_status.setText(f"已写入全部 {len(self._fetched_models)} 个模型")

    def result_data(self) -> tuple[str, dict[str, Any]]:
        name = self.name_edit.text().strip()
        try:
            models = json.loads(self.models_text.toPlainText() or "[]")
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
        self.setWindowTitle(f"API Keys · {provider}")
        self.resize(760, 460)

        layout = QVBoxLayout(self)
        title = QLabel(f"Provider「{provider}」的 API Key 池")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        hint = QLabel("请求遇到鉴权、限流或额度错误时，会将当前 Key 暂时标记为失效并切换下一把。")
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

        row = QHBoxLayout()
        add_btn = QPushButton("添加 Key")
        add_btn.clicked.connect(self.add_key)
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
        row.addWidget(delete_btn)
        row.addWidget(restore_btn)
        row.addWidget(restore_all_btn)
        row.addStretch(1)
        row.addWidget(close_btn)
        layout.addLayout(row)
        self.refresh()

    def refresh(self):
        rows = core.list_provider_api_keys(self.provider)
        self.table.setRowCount(len(rows))
        for index, meta in enumerate(rows):
            key_item = QTableWidgetItem(str(meta.get("masked") or ""))
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
        if QMessageBox.question(self, "确认删除", "确定从 Key 池中永久删除选中的 Key？") != QMessageBox.Yes:
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


class FetchModelsDialog(QDialog):
    """Standalone: baseUrl + apiKey -> list models -> save provider."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("从 BaseURL + API Key 获取模型")
        self.resize(720, 620)
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
            "说明：\n"
            "1) 空 API Key 会 401（Missing bearer authentication）——必须填写有效密钥。\n"
            "2) SSL UNEXPECTED_EOF 多为网络/防火墙/直连 OpenAI 不稳定，请用代理或可访问的中转 Base URL。\n"
            "3) 拉取成功后可多选模型再保存到 models.json。"
        )
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        layout.addWidget(tip)

        self.status = QLabel("填写 Base URL 与 API Key 后点击拉取")
        self.status.setObjectName("subtitle")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.MultiSelection)
        layout.addWidget(self.list, 1)

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
        self._worker = Worker(
            core.fetch_remote_models,
            base,
            key,
            api=self.api.currentText(),
            insecure_ssl=self.insecure_ssl.isChecked(),
            proxy=self.proxy.text().strip(),
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
        self.list.clear()
        for m in self._models:
            self.list.addItem(m.get("id", ""))
        for i in range(self.list.count()):
            self.list.item(i).setSelected(True)
        proxy = result.get("proxy") or ""
        px = f" | proxy={proxy}" if proxy else ""
        self.status.setText(f"成功获取 {len(self._models)} 个模型 | {result.get('endpoint')}{px}")

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
        ids = {i.text() for i in self.list.selectedItems()}
        chosen = [m for m in self._models if m.get("id") in ids] or list(self._models)
        core.upsert_custom_provider(
            name,
            base_url=self.base_url.text().strip(),
            api=self.api.currentText(),
            api_key=self.api_key.text().strip(),
            models=chosen,
            compat={"supportsDeveloperRole": False, "supportsReasoningEffort": True},
        )
        QMessageBox.information(self, "已保存", f"Provider「{name}」已写入 models.json，共 {len(chosen)} 个模型")
        self.accept()




NAV_PAGES = [
    ("simple", "简化配置", "默认模型 / 快速接入 / 启动"),
    ("models", "模型列表", "切换、收藏、批量测试"),
    ("providers", "Provider", "自定义与密钥管理"),
    ("chat", "快速提问", "轻量多轮问答"),
    ("sessions", "会话", "继续历史会话"),
    ("health", "健康监控", "可用性巡检"),
    ("history", "测试历史", "延迟记录"),
    ("tools", "工具", "导入导出 / 自检"),
    ("settings", "设置", "语言 / 主题 / 代理"),
    ("help", "使用教程", "教程与常见问题"),
]


class InstallPiDialog(QDialog):
    """Install or upgrade the Node-compatible official Pi npm channel."""

    def __init__(self, parent=None, status: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("\u5b89\u88c5 / \u5347\u7ea7 Pi")
        self.resize(620, 460)
        self._worker = None
        self.install_succeeded = False
        self.status_info = dict(status or {})
        node_version = self.status_info.get("node_version") or core.get_node_version()
        npm_version = self.status_info.get("npm_version") or core.get_npm_version()
        channel = self.status_info.get("channel") or core.select_pi_install_channel(node_version)
        self.package_spec = self.status_info.get("package_spec") or core.pi_package_spec(channel)
        target = self.status_info.get("latest") or "\u68c0\u67e5\u540e\u786e\u5b9a"
        command_text = (
            f"npm install -g {self.package_spec}"
            if self.package_spec
            else "npm install -g <\u9700\u5148\u5347\u7ea7 Node.js>"
        )

        layout = QVBoxLayout(self)
        tip = QLabel(
            f"\u5b89\u88c5\u547d\u4ee4\uff1a\n{command_text}\n\n"
            f"Node.js\uff1a{node_version or '\u672a\u68c0\u6d4b\u5230'}    "
            f"npm\uff1a{npm_version or '\u672a\u68c0\u6d4b\u5230'}\n"
            f"\u517c\u5bb9\u901a\u9053\uff1a{channel or '\u4e0d\u53ef\u7528'}    "
            f"\u76ee\u6807\u7248\u672c\uff1a{target}"
        )
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        if self.status_info:
            status_label = QLabel(str(self.status_info.get("message") or ""))
            status_label.setWordWrap(True)
            layout.addWidget(status_label)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log, 1)
        row = QHBoxLayout()
        self.btn_install = QPushButton("\u5f00\u59cb\u5b89\u88c5/\u5347\u7ea7")
        self.btn_install.setProperty("success", True)
        self.btn_install.clicked.connect(self._run)
        self.btn_close = QPushButton("\u5173\u95ed")
        self.btn_close.setProperty("secondary", True)
        self.btn_close.clicked.connect(self.accept)
        row.addWidget(self.btn_install)
        row.addStretch(1)
        row.addWidget(self.btn_close)
        layout.addLayout(row)

        blocked = bool(self.status_info.get("blocked")) or not self.package_spec or not npm_version
        if blocked:
            self.btn_install.setEnabled(False)
            self.btn_install.setToolTip(
                str(self.status_info.get("error") or "\u8bf7\u5148\u4fee\u590d Node.js/npm \u73af\u5883\u3002")
            )

    def _run(self):
        self.install_succeeded = False
        self.btn_install.setEnabled(False)
        command_text = f"npm install -g {self.package_spec}" if self.package_spec else "npm install -g"
        self.log.appendPlainText(f"\u6b63\u5728\u6267\u884c {command_text} ...")
        self._worker = Worker(core.install_or_update_pi)
        self._worker.done.connect(self._done)
        self._worker.failed.connect(self._fail)
        self._worker.start()

    def _done(self, result):
        code, out, err = result if isinstance(result, tuple) else (1, "", str(result))
        if out:
            self.log.appendPlainText(out)
        if err:
            self.log.appendPlainText(err)
        self.btn_install.setEnabled(True)
        if code == 0:
            self.install_succeeded = True
            self.log.appendPlainText("\n\u5b8c\u6210\uff1a\u5b89\u88c5/\u5347\u7ea7\u5df2\u9a8c\u8bc1\uff0c\u6b63\u5728\u8fd4\u56de\u7ba1\u7406\u5668\u9762\u677f\u3002")
            self.accept()
        else:
            self.log.appendPlainText(f"\n\u5931\u8d25\uff1a\u9000\u51fa\u7801 {code}")
            detail = str(err or out or "\u672a\u77e5\u9519\u8bef")[-1200:]
            QMessageBox.warning(
                self,
                "\u5b89\u88c5\u5931\u8d25",
                f"Pi \u5b89\u88c5\u6216\u9a8c\u8bc1\u5931\u8d25\uff08code={code}\uff09\u3002\n\n{detail}",
            )

    def _fail(self, err: str):
        self.btn_install.setEnabled(True)
        self.log.appendPlainText(f"\u5931\u8d25\uff1a{err}")
        QMessageBox.warning(self, "\u5931\u8d25", err)


class SetupWizardDialog(QDialog):
    """First-run simplified setup wizard."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pi Manager 基础配置向导")
        self.resize(560, 480)
        layout = QVBoxLayout(self)
        title = QLabel("欢迎使用 Pi Manager")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        sub = QLabel("完成以下基础项后即可使用「简化配置」接入自定义 Provider 并启动官方 Pi。")
        sub.setObjectName("subtitle")
        sub.setWordWrap(True)
        layout.addWidget(sub)

        form = QFormLayout()
        self.lang = QComboBox()
        self.lang.addItem("简体中文（优先）", "zh-CN")
        self.lang.addItem("English", "en")
        self.lang.addItem("不附加语言偏好", "auto")
        lang0 = core.get_language()
        for i in range(self.lang.count()):
            if self.lang.itemData(i) == lang0:
                self.lang.setCurrentIndex(i)
                break

        self.ui_mode = QComboBox()
        self.ui_mode.addItem("夜间模式（全局）", "night")
        self.ui_mode.addItem("白天模式（全局）", "day")
        ut = core.get_ui_theme()
        for i in range(self.ui_mode.count()):
            if self.ui_mode.itemData(i) == ut.get("mode"):
                self.ui_mode.setCurrentIndex(i)
                break

        self.ui_accent = QComboBox()
        for key, label in ACCENT_LABELS.items():
            self.ui_accent.addItem(label, key)
        for i in range(self.ui_accent.count()):
            if self.ui_accent.itemData(i) == ut.get("accent"):
                self.ui_accent.setCurrentIndex(i)
                break


        self.secure = QCheckBox("保存 Provider 时加密 API Key（系统密钥库 / 安全保险库）")
        self.secure.setChecked(True)
        self.auto_update = QCheckBox("启动时检查 Pi 更新")
        self.auto_update.setChecked(True)

        form.addRow("默认语言（Pi 回复）", self.lang)
        form.addRow("全局昼夜模式", self.ui_mode)
        form.addRow("全局主题色", self.ui_accent)
        form.addRow("", self.secure)
        form.addRow("", self.auto_update)
        layout.addLayout(form)

        tip2 = QLabel("下一步：在「简化配置」页用 Base URL + API Key 拉取模型，设为默认后即可启动。")
        tip2.setObjectName("subtitle")
        tip2.setWordWrap(True)
        layout.addWidget(tip2)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存并开始")
        buttons.button(QDialogButtonBox.Cancel).setText("稍后")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        core.set_language(self.lang.currentData() or "zh-CN")
        core.apply_language_preference(self.lang.currentData() or "zh-CN")
        core.set_ui_theme(
            mode=self.ui_mode.currentData() or "night",
            accent=self.ui_accent.currentData() or "blue",
        )
        cfg = core.load_manager_config()
        cfg["secure_keys"] = self.secure.isChecked()
        cfg["auto_check_update"] = self.auto_update.isChecked()
        core.save_manager_config(cfg)
        core.mark_setup_done(True)
        try:
            core.run_first_time_bootstrap()
        except Exception:
            pass
        self.accept()


class MainWindow(FeatureMixin, QMainWindow):
    def __init__(self, *, start_background: bool = True):
        """Create the window.

        ``start_background=False`` is intentionally supported for offscreen UI
        tests and embedders: construction then has no network workers, tray icon,
        update prompt, or startup timer side effects.
        """
        super().__init__()
        self.setWindowTitle("Pi Manager — 简化配置 · 跨平台 Pi 启动器")
        try:
            from .ui_features import app_icon
            self.setWindowIcon(app_icon())
        except Exception:
            pass
        self.resize(1320, 880)
        self.setMinimumSize(1080, 720)
        self.models: list[core.ModelInfo] = []
        self.workers: list[Worker] = []
        self.test_results: dict[str, dict[str, Any]] = {}
        self.mgr = core.load_manager_config()
        self.setAcceptDrops(True)
        self.init_feature_state()
        self._build_ui()
        self._background_enabled = bool(start_background)
        if self._background_enabled:
            self.refresh_all()
            self.setup_system_tray()
            # Defer first-run / update checks so the shell paints first.
            from PySide6.QtCore import QTimer
            QTimer.singleShot(400, self._startup_checks)
            if bool(self.mgr.get("start_minimized")) and self.tray:
                QTimer.singleShot(0, self.hide)

    def _build_ui(self):
        raise NotImplementedError(
            "MainWindow 仅作为行为基类使用；请实例化 presentation.ModernMainWindow"
        )

    def _on_nav_changed(self, row: int):
        if row < 0 or row >= len(self._page_keys):
            return
        key = self._page_keys[row]
        self.pages.setCurrentIndex(self._page_index[key])
        title, desc = next(((t, d) for k, t, d in NAV_PAGES if k == key), ("", ""))
        if hasattr(self, "page_heading"):
            self.page_heading.setText(title)
            self.page_subheading.setText(desc)
        # 切换到对应页时自动加载本地数据
        if key == "health":
            try:
                self.health_refresh_table()
            except Exception:
                pass
        elif key == "history":
            try:
                self.history_refresh()
            except Exception:
                pass

    def _goto_page(self, key: str):
        if key in getattr(self, "_page_index", {}) and hasattr(self, "nav"):
            self.nav.setCurrentRow(self._page_keys.index(key))
        elif key in getattr(self, "_page_index", {}):
            self.pages.setCurrentIndex(self._page_index[key])

    def _card(self, *, elevated: bool = False) -> QFrame:
        f = QFrame()
        f.setObjectName("card")
        if elevated:
            f.setProperty("elevated", True)
        return f

    def _btn(self, text: str, slot, *, secondary=False, danger=False, success=False, ghost=False) -> QPushButton:
        b = QPushButton(text)
        b.setCursor(Qt.PointingHandCursor)
        if secondary:
            b.setProperty("secondary", True)
        if danger:
            b.setProperty("danger", True)
        if success:
            b.setProperty("success", True)
        if ghost:
            b.setProperty("ghost", True)
        b.clicked.connect(slot)
        return b

    def _polish_table(self, table: QTableWidget) -> None:
        """统一表格观感（跨平台）。"""
        table.setShowGrid(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setWordWrap(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(36)
        table.setFocusPolicy(Qt.StrongFocus)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

    def quick_fetch_and_save(self):
        name = self.quick_name.text().strip()
        base = self.quick_base.text().strip()
        key = self.quick_key.text().strip()
        api = self.quick_api.currentText()
        if not name:
            QMessageBox.warning(self, "提示", "请填写 Provider 名称")
            return
        if not base:
            QMessageBox.warning(self, "提示", "请填写 Base URL")
            return
        if not key and api != "google-generative-ai":
            QMessageBox.warning(self, "提示", "请填写 API Key（空密钥会导致 401 Missing bearer）")
            return
        self.quick_status.setText("正在拉取模型…")
        self.status.showMessage("快速接入：拉取模型中…")

        def job():
            return core.fetch_remote_models(base, key, api=api)

        w = self._track(Worker(job))
        w.done.connect(lambda result: self._on_quick_fetch_done(result, name, base, key, api))
        w.failed.connect(self._on_quick_fetch_fail)
        w.start()

    def _on_quick_fetch_done(self, result: dict, name: str, base: str, key: str, api: str):
        if not result.get("ok"):
            err = str(result.get("error") or "unknown")
            endpoint = result.get("endpoint") or ""
            msg = err + (f"\nendpoint: {endpoint}" if endpoint else "")
            self.quick_status.setText(f"失败：{err}")
            QMessageBox.warning(self, "拉取失败", msg)
            return
        models = result.get("models") or []
        if not models:
            self.quick_status.setText("成功但模型列表为空")
            QMessageBox.information(self, "提示", "接口返回空模型列表，请检查 Base URL 是否正确")
            return
        try:
            core.upsert_custom_provider(
                name,
                base_url=base,
                api=api,
                api_key=key,
                models=models,
                compat={"supportsDeveloperRole": False, "supportsReasoningEffort": True},
            )
        except Exception as e:
            self.quick_status.setText(f"保存失败：{e}")
            QMessageBox.warning(self, "保存失败", str(e))
            return
        self.quick_status.setText(f"已保存「{name}」· {len(models)} 个模型")
        self.status.showMessage(f"快速接入完成：{name}（{len(models)} 模型）")
        self.refresh_models()
        self.refresh_providers()
        try:
            s = core.load_settings()
            if not s.get("defaultModel") or not s.get("defaultProvider"):
                mid = models[0].get("id") or models[0].get("name")
                if mid:
                    core.set_default_model(name, str(mid))
                    self.refresh_dashboard()
        except Exception:
            pass
        QMessageBox.information(
            self,
            "已接入",
            f"Provider「{name}」已写入，共 {len(models)} 个模型。\n"
            f"可在「模型列表」设为默认，或直接启动完整 Pi。",
        )

    def _on_quick_fetch_fail(self, err: str):
        self.quick_status.setText(f"失败：{err}")
        QMessageBox.warning(self, "拉取失败", err)

    def _model_capability_text(self, m: core.ModelInfo) -> str:
        """紧凑能力标签：上下文 + 思考/图像符号。"""
        parts: list[str] = []
        ctx = (m.context or "").strip()
        if ctx:
            # 统一长数字：272000 -> 272K
            compact = ctx
            try:
                n = int(str(ctx).replace(",", "").replace("k", "000").replace("K", "000"))
                if n >= 1000:
                    compact = f"{n // 1000}K" if n % 1000 == 0 else f"{n / 1000:.1f}K".rstrip("0").rstrip(".")
                else:
                    compact = str(n)
            except Exception:
                compact = ctx.replace(" tokens", "").replace("token", "").strip()
            parts.append(compact)
        th = (m.thinking or "").lower()
        if th in {"yes", "true", "y", "1"}:
            parts.append("思")
        elif th and th not in {"no", "false", "n", "0", "-"}:
            parts.append(f"思:{m.thinking}")
        img = (m.images or "").lower()
        if img in {"yes", "true", "y", "1"}:
            parts.append("图")
        elif img and img not in {"no", "false", "n", "0", "-"}:
            parts.append(f"图:{m.images}")
        return " ".join(parts) if parts else "—"

    def _model_status_cells(self, m: core.ModelInfo) -> tuple[QTableWidgetItem, QTableWidgetItem]:
        """状态 / 延迟：短文案 + 当前全局主题的语义色。"""
        from .presentation.design import tokens_for

        theme = core.get_ui_theme()
        colors = tokens_for(theme.get("mode"), theme.get("accent"))
        res = self.test_results.get(m.key)
        st = QTableWidgetItem("—")
        lat_item = QTableWidgetItem("—")
        st.setTextAlignment(Qt.AlignCenter)
        lat_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        if not res:
            st.setForeground(QColor(colors.text_muted))
            lat_item.setForeground(QColor(colors.text_muted))
            return st, lat_item
        if res.get("pending"):
            st.setText("…")
            st.setForeground(QColor(colors.warning))
            lat_item.setText("…")
            lat_item.setForeground(QColor(colors.warning))
            return st, lat_item
        if res.get("available") is True:
            st.setText("✓")
            st.setToolTip("可用")
            st.setForeground(QColor(colors.success))
        elif res.get("available") is False:
            st.setText("✗")
            err = str(res.get("error") or res.get("preview") or "不可用")
            st.setToolTip(err[:300])
            st.setForeground(QColor(colors.danger))
        else:
            st.setText("?")
            st.setForeground(QColor(colors.text_muted))
        lat = res.get("latency_ms")
        if isinstance(lat, (int, float)):
            lat_item.setText(f"{lat:.0f}ms")
            if lat < 800:
                lat_item.setForeground(QColor(colors.success))
            elif lat < 2000:
                lat_item.setForeground(QColor(colors.warning))
            else:
                lat_item.setForeground(QColor(colors.danger))
        else:
            lat_item.setForeground(QColor(colors.text_muted))
        return st, lat_item

    def _model_row_key(self, row: int) -> tuple[str, str] | None:
        # 兼容：UserRole 存在于「模型」列（第 0 列）
        item = self.models_table.item(row, 0)
        if not item:
            return None
        data = item.data(Qt.UserRole)
        if isinstance(data, (list, tuple)) and len(data) == 2:
            return str(data[0]), str(data[1])
        # 兜底：Provider 列 + 去掉标记后的模型名
        prov_item = self.models_table.item(row, 1)
        if prov_item and item.text():
            name = item.text().lstrip("●★· ").strip()
            if name:
                return prov_item.text().strip(), name
        text = item.text().lstrip("●★· ").strip()
        if "/" in text:
            p, m = text.split("/", 1)
            return p.strip(), m.strip()
        return None

    def _track(self, worker: Worker):
        self.workers.append(worker)
        worker.finished.connect(lambda: self._untrack(worker))
        return worker

    def _untrack(self, worker: Worker):
        if worker in self.workers:
            self.workers.remove(worker)

    def selected_model_row(self) -> core.ModelInfo | None:
        rows = self.models_table.selectionModel().selectedRows()
        if not rows:
            return None
        parsed = self._model_row_key(rows[0].row())
        if not parsed:
            return None
        provider, model = parsed
        for m in self.models:
            if m.provider == provider and m.model == model:
                return m
        return core.ModelInfo(provider, model)

    def selected_model_rows(self) -> list[core.ModelInfo]:
        sm = self.models_table.selectionModel()
        if not sm:
            return []
        out: list[core.ModelInfo] = []
        seen: set[str] = set()
        for idx in sm.selectedRows():
            parsed = self._model_row_key(idx.row())
            if not parsed:
                continue
            provider, model = parsed
            key = f"{provider}/{model}"
            if key in seen:
                continue
            seen.add(key)
            found = None
            for m in self.models:
                if m.provider == provider and m.model == model:
                    found = m
                    break
            out.append(found or core.ModelInfo(provider, model))
        return out

    def _test_mode(self) -> str:
        if hasattr(self, "test_mode_combo"):
            data = self.test_mode_combo.currentData()
            if data:
                return str(data)
            return self.test_mode_combo.currentText()
        return "auto"

    def _parse_favorite_key(self, key: str) -> tuple[str, str] | None:
        key = (key or "").strip()
        if "/" not in key:
            return None
        provider, model = key.split("/", 1)
        provider, model = provider.strip(), model.strip()
        if not provider or not model:
            return None
        return provider, model

    def model_test_selected(self):
        rows = self.selected_model_rows()
        if not rows:
            QMessageBox.information(self, "提示", "请先在模型列表中选择一个或多个模型")
            return
        self._run_model_tests([(m.provider, m.model) for m in rows])

    def model_test_default(self):
        provider, model, _thinking = core.get_default_model()
        if not provider or not model:
            QMessageBox.information(self, "提示", "尚未设置默认模型")
            return
        self._run_model_tests([(provider, model)])

    def model_test_favorites(self):
        favs = list(self.mgr.get("favorites") or [])
        pairs: list[tuple[str, str]] = []
        for key in favs:
            parsed = self._parse_favorite_key(key)
            if parsed:
                pairs.append(parsed)
        if not pairs:
            QMessageBox.information(self, "提示", "收藏列表为空，请先收藏模型")
            return
        self._run_model_tests(pairs)

    def model_add_favorite_batch(self):
        rows = self.selected_model_rows()
        if not rows:
            # fallback single
            m = self.selected_model_row()
            rows = [m] if m else []
        if not rows:
            QMessageBox.information(self, "提示", "请先多选模型（Ctrl/Shift）")
            return
        favs = list(self.mgr.get("favorites") or [])
        n = 0
        for m in rows:
            if m.key not in favs:
                favs.append(m.key)
                n += 1
        self.mgr["favorites"] = favs
        self.persist_mgr()
        self.fill_favorites()
        self.fill_models_table()
        self.status.showMessage(f"批量收藏 +{n}，共 {len(favs)}")

    def model_select_visible(self):
        self.models_table.selectAll()

    def _visible_model_pairs(self) -> list[tuple[str, str]]:
        q = (self.model_filter.text() or "").lower().strip()
        rows = [
            m
            for m in self.models
            if not q or q in m.key.lower() or q in m.provider.lower() or q in m.model.lower()
        ]
        return [(m.provider, m.model) for m in rows]

    def model_test_filtered(self):
        pairs = self._visible_model_pairs()
        if not pairs:
            QMessageBox.information(self, "提示", "当前过滤结果为空")
            return
        if len(pairs) > 30:
            if QMessageBox.question(
                self, "确认", f"将测试 {len(pairs)} 个模型，可能较久并产生费用。继续？"
            ) != QMessageBox.Yes:
                return
        self._run_model_tests(pairs)

    def model_test_all(self):
        pairs = [(m.provider, m.model) for m in self.models]
        if not pairs:
            QMessageBox.information(self, "提示", "请先刷新模型列表")
            return
        if len(pairs) > 20:
            if QMessageBox.question(
                self, "确认", f"将测试全部 {len(pairs)} 个模型，确认？"
            ) != QMessageBox.Yes:
                return
        self._run_model_tests(pairs)

    def model_fav_filtered(self):
        pairs = self._visible_model_pairs()
        if not pairs:
            return
        favs = list(self.mgr.get("favorites") or [])
        n = 0
        for p, m in pairs:
            key = f"{p}/{m}"
            if key not in favs:
                favs.append(key)
                n += 1
        self.mgr["favorites"] = favs
        self.persist_mgr()
        self.fill_favorites()
        QMessageBox.information(self, "收藏", f"过滤结果新增收藏 {n} 个，共 {len(favs)}")

    def session_delete_batch(self):
        sm = self.sessions_table.selectionModel()
        if not sm:
            return
        paths = []
        for idx in sm.selectedRows():
            path = self._session_path_at(idx.row())
            if path:
                paths.append(path)
        if not paths:
            QMessageBox.information(self, "提示", "请多选要删除的会话")
            return
        if QMessageBox.question(self, "批量删除", f"删除 {len(paths)} 个会话文件？") != QMessageBox.Yes:
            return
        ok = 0
        for p in paths:
            if extras.session_delete(p):
                ok += 1
        self.refresh_sessions()
        self.status.showMessage(f"已删除 {ok}/{len(paths)} 个会话")


    def fav_test(self):
        item = self.fav_list.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择一个收藏模型")
            return
        parsed = self._parse_favorite_key(item.text())
        if not parsed:
            QMessageBox.warning(self, "提示", f"无法解析收藏项：{item.text()}")
            return
        self._run_model_tests([parsed])

    def _run_model_tests(self, pairs: list[tuple[str, str]]):
        if not pairs:
            return
        if getattr(self, "_test_running", False):
            QMessageBox.information(self, "提示", "已有测试进行中，请稍候完成后再试。")
            return
        mode = self._test_mode()
        workdir = self.workdir_edit.text().strip() or str(core.user_home())
        n = len(pairs)
        self._test_running = True
        self._test_total = n
        self._test_done = 0
        self._test_ok = 0
        self._test_lines: list[str] = []
        # mark pending rows so UI shows 测试中 immediately
        for p, m in pairs:
            key = f"{p}/{m}"
            self.test_results[key] = {
                "provider": p,
                "model": m,
                "available": None,
                "pending": True,
                "latency_ms": None,
                "mode": mode,
            }
        self.fill_models_table()
        self.status.showMessage(f"测试中 0/{n}（{mode}，完成一项刷新一项）…")
        if hasattr(self, "test_status"):
            self.test_status.setText(f"实时测试：0/{n} 完成 …")

        w = self._track(
            BatchTestWorker(
                pairs,
                mode=mode,
                workdir=workdir,
                timeout=90 if mode == "pi" else 45,
                kind="model",
            )
        )
        w.progress.connect(self._on_model_test_progress, Qt.QueuedConnection)
        w.done.connect(self._on_model_tests_done, Qt.QueuedConnection)
        w.failed.connect(self._on_model_tests_fail, Qt.QueuedConnection)
        w.start()

    def _on_model_test_progress(self, r: dict):
        if not isinstance(r, dict):
            return
        key = f"{r.get('provider')}/{r.get('model')}"
        self.test_results[key] = r
        self._test_done = int(getattr(self, "_test_done", 0)) + 1
        if r.get("available"):
            self._test_ok = int(getattr(self, "_test_ok", 0)) + 1
        total = int(getattr(self, "_test_total", 1) or 1)
        done = self._test_done
        ok_n = self._test_ok
        summary = core.format_test_summary(r) if hasattr(core, "format_test_summary") else (
            "可用" if r.get("available") else "不可用"
        )
        line = f"{key}: {summary}"
        self._test_lines = list(getattr(self, "_test_lines", []) or [])
        self._test_lines.append(line)
        # live UI update
        self.fill_models_table()
        try:
            self.history_refresh()
        except Exception:
            pass
        self.status.showMessage(f"测试中 {done}/{total} · 可用 {ok_n} · 刚完成 {key}")
        if hasattr(self, "test_status"):
            recent = " | ".join(self._test_lines[-4:])
            self.test_status.setText(f"进度 {done}/{total}（可用 {ok_n}） · {recent}")

    def _on_model_tests_done(self, results: list):
        self._test_running = False
        if isinstance(results, dict):
            # safety if health payload ever routed here
            results = results.get("results") or []
        if not isinstance(results, list):
            results = [results]
        lines = list(getattr(self, "_test_lines", []) or [])
        ok_n = int(getattr(self, "_test_ok", 0))
        # ensure final table state
        for r in results:
            if isinstance(r, dict):
                key = f"{r.get('provider')}/{r.get('model')}"
                self.test_results[key] = r
                if r.get("available") and key not in "".join(lines):
                    pass
        self.fill_models_table()
        try:
            self.history_refresh()
        except Exception:
            pass
        summary = f"测试完成：{ok_n}/{len(results)} 可用（已实时写入列表与历史）"
        self.status.showMessage(summary)
        if hasattr(self, "test_status"):
            self.test_status.setText(summary + (" · " + " | ".join(lines[-6:]) if lines else ""))
        # only popup for very small batches; large ones already streamed to UI
        if len(results) <= 2:
            nl = chr(10)
            QMessageBox.information(self, "测试结果", summary + nl + nl.join(lines))

    def _on_model_tests_fail(self, err: str):
        self._test_running = False
        # clear pending markers
        for k, v in list(self.test_results.items()):
            if isinstance(v, dict) and v.get("pending"):
                del self.test_results[k]
        self.fill_models_table()
        self.status.showMessage("测试失败")
        if hasattr(self, "test_status"):
            self.test_status.setText(f"测试失败：{err}")
        QMessageBox.warning(self, "测试失败", err)

    def persist_mgr(self):
        self.mgr["last_workdir"] = self.workdir_edit.text().strip()
        self.mgr["terminal"] = self.terminal_combo.currentData() or self.terminal_combo.currentText()
        core.save_manager_config(self.mgr)

    def _on_drop_auto_launch_toggled(self, checked: bool):
        self.mgr["drop_auto_launch"] = bool(checked)
        self.persist_mgr()

    def _set_drop_active(self, active: bool):
        if hasattr(self, "drop_zone"):
            self.drop_zone.setProperty("active", "true" if active else "false")
            self.drop_zone.style().unpolish(self.drop_zone)
            self.drop_zone.style().polish(self.drop_zone)
            self.drop_zone.update()

    def _extract_local_paths(self, event) -> list[str]:
        md = event.mimeData()
        paths: list[str] = []
        if md.hasUrls():
            for url in md.urls():
                if isinstance(url, QUrl):
                    local = url.toLocalFile()
                else:
                    local = str(url)
                if local:
                    paths.append(local)
        elif md.hasText():
            # support plain path text paste/drag
            for line in md.text().splitlines():
                line = line.strip().strip('"')
                if line:
                    paths.append(line)
        return paths

    def _resolve_workdir_from_paths(self, paths: list[str]) -> str | None:
        for p in paths:
            path = Path(p)
            try:
                if path.is_dir():
                    return str(path.resolve())
                if path.is_file():
                    return str(path.parent.resolve())
            except OSError:
                continue
        # path may not exist yet but look like a dir
        for p in paths:
            s = p.strip().strip('"')
            if s and not Path(s).suffix:
                return s
        return None

    def dragEnterEvent(self, event: QDragEnterEvent):
        paths = self._extract_local_paths(event)
        if paths:
            event.acceptProposedAction()
            self._set_drop_active(True)
        else:
            event.ignore()

    def dragMoveEvent(self, event: QDragMoveEvent):
        paths = self._extract_local_paths(event)
        if paths:
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event):
        self._set_drop_active(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent):
        self._set_drop_active(False)
        paths = self._extract_local_paths(event)
        workdir = self._resolve_workdir_from_paths(paths)
        if not workdir:
            QMessageBox.warning(self, "无法识别", "请拖入本地文件夹（或文件，将使用其所在目录）。")
            event.ignore()
            return
        event.acceptProposedAction()
        self.apply_workdir_and_maybe_launch(workdir, auto_launch=self.chk_drop_launch.isChecked())

    def apply_workdir_and_maybe_launch(self, workdir: str, *, auto_launch: bool = True):
        """Set workdir in UI/config, optionally launch Pi with default provider there."""
        path = Path(workdir)
        if path.exists() and path.is_file():
            path = path.parent
            workdir = str(path)
        if not Path(workdir).exists():
            QMessageBox.warning(self, "目录不存在", f"路径不存在：\n{workdir}")
            return
        self.workdir_edit.setText(workdir)
        self.persist_mgr()
        provider, model, thinking = core.get_default_model()
        self.status.showMessage(f"工作目录已设为：{workdir}")
        if hasattr(self, "drop_hint"):
            self.drop_hint.setText(f"当前：{workdir}  |  默认 {provider}/{model}")
        if not auto_launch:
            return
        if not provider or not model:
            QMessageBox.information(
                self,
                "未设置默认模型",
                "工作目录已更新，但尚未设置 defaultProvider/defaultModel。\n请先在「模型切换」中设为默认。",
            )
            return
        self._launch(provider, model, thinking or None)

    def browse_workdir(self):
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", self.workdir_edit.text())
        if d:
            self.workdir_edit.setText(d)
            self.persist_mgr()

    def open_config_dir(self):
        core.ensure_agent_dir()
        core.open_path(str(core.pi_agent_dir()))

    def open_models_json(self):
        core.ensure_agent_dir()
        if not core.models_path().exists():
            core.save_models_config({"providers": {}})
        core.open_path(str(core.models_path()))

    def open_settings_json(self):
        core.ensure_agent_dir()
        if not core.settings_path().exists():
            core.save_settings({})
        core.open_path(str(core.settings_path()))

    def refresh_all(self):
        self.refresh_dashboard()
        self.refresh_models()
        self.refresh_providers()
        self.refresh_sessions()
        self.settings_load()
        # 健康监控 / 测试历史：默认加载本地缓存，无需手动点刷新
        try:
            self.health_refresh_table()
        except Exception:
            pass
        try:
            self.history_refresh()
        except Exception:
            pass
        self.status.showMessage("已刷新（含健康监控与测试历史）")

    def refresh_dashboard(self):
        provider, model, thinking = core.get_default_model()
        self.lbl_current.setText(f"{provider}/{model}" if provider else "(未设置)")
        self.lbl_thinking.setText(f"Thinking: {thinking or '-'}")
        self.chat_fill_default()
        w = self._track(Worker(core.get_pi_version))
        w.done.connect(lambda v: self.version_pill.setText(f"pi: {v}"))
        w.failed.connect(lambda e: self.version_pill.setText(f"pi: {e}"))
        w.start()
        rows = core.auth_summary()
        self.auth_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.auth_table.setItem(i, 0, QTableWidgetItem(r["provider"]))
            self.auth_table.setItem(i, 1, QTableWidgetItem(r["status"]))
        self.fill_favorites()

    def fill_favorites(self):
        try:
            self.rebuild_tray_favorites()
        except Exception:
            pass
        self.fav_list.clear()
        for key in self.mgr.get("favorites") or []:
            self.fav_list.addItem(key)

    def refresh_models(self):
        self.status.showMessage("正在读取 pi --list-models …")
        w = self._track(Worker(core.list_models))
        w.done.connect(self._on_models_loaded)
        w.failed.connect(lambda e: QMessageBox.warning(self, "错误", e))
        w.start()

    def _on_models_loaded(self, models: list[core.ModelInfo]):
        self.models = models
        self.fill_models_table()
        try:
            self.refresh_chat_model_choices()
        except Exception:
            pass
        self.status.showMessage(f"已加载 {len(models)} 个模型")

    def fill_models_table(self):
        q = (self.model_filter.text() or "").lower().strip()
        only_fav = bool(getattr(self, "model_only_favorites", None) and self.model_only_favorites.isChecked())
        fav_set = {str(x) for x in (self.mgr.get("favorites") or [])}
        try:
            def_p, def_m, _ = core.get_default_model()
        except Exception:
            def_p, def_m = "", ""
        default_key = f"{def_p}/{def_m}" if def_p and def_m else ""

        prov = ""
        if hasattr(self, "model_provider_filter"):
            prov = str(self.model_provider_filter.currentData() or "")
            # rebuild provider list options if models changed
            current = prov
            providers = sorted({m.provider for m in self.models})
            existing = []
            for i in range(self.model_provider_filter.count()):
                existing.append(str(self.model_provider_filter.itemData(i) or ""))
            want = [""] + providers
            if existing != want:
                self.model_provider_filter.blockSignals(True)
                self.model_provider_filter.clear()
                self.model_provider_filter.addItem("全部 Provider", "")
                for p in providers:
                    self.model_provider_filter.addItem(p, p)
                idx = self.model_provider_filter.findData(current)
                self.model_provider_filter.setCurrentIndex(idx if idx >= 0 else 0)
                self.model_provider_filter.blockSignals(False)
                prov = str(self.model_provider_filter.currentData() or "")

        rows: list[core.ModelInfo] = []
        for m in self.models:
            if prov and m.provider != prov:
                continue
            if only_fav and m.key not in fav_set:
                continue
            if q and q not in m.key.lower() and q not in m.provider.lower() and q not in m.model.lower():
                continue
            rows.append(m)

        # 默认模型置顶，其次收藏，再按 provider + model 名
        def _sort_key(m: core.ModelInfo) -> tuple:
            is_def = 0 if m.key == default_key else 1
            is_fav = 0 if m.key in fav_set else 1
            return (is_def, is_fav, m.provider.lower(), m.model.lower())

        rows.sort(key=_sort_key)

        # 选中单一 Provider 时隐藏 Provider 列，减少重复
        hide_provider_col = bool(prov)
        if self.models_table.columnCount() >= 2:
            self.models_table.setColumnHidden(1, hide_provider_col)

        from .presentation.design import tokens_for

        theme = core.get_ui_theme()
        colors = tokens_for(theme.get("mode"), theme.get("accent"))
        self.models_table.setRowCount(len(rows))
        for i, m in enumerate(rows):
            is_default = m.key == default_key
            is_fav = m.key in fav_set
            prefix = ""
            if is_default:
                prefix += "● "
            if is_fav:
                prefix += "★ "
            name_item = QTableWidgetItem(f"{prefix}{m.model}")
            name_item.setData(Qt.UserRole, [m.provider, m.model])
            tip_bits = [m.key]
            if is_default:
                tip_bits.append("当前默认")
            if is_fav:
                tip_bits.append("已收藏")
            name_item.setToolTip(" · ".join(tip_bits))
            if is_default:
                name_item.setForeground(QColor(colors.accent_text))
            self.models_table.setItem(i, 0, name_item)

            prov_item = QTableWidgetItem(m.provider)
            prov_item.setToolTip(m.provider)
            prov_item.setForeground(QColor(colors.text_muted))
            self.models_table.setItem(i, 1, prov_item)

            cap = QTableWidgetItem(self._model_capability_text(m))
            cap.setToolTip(
                f"context={m.context or '-'}  thinking={m.thinking or '-'}  images={m.images or '-'}"
            )
            self.models_table.setItem(i, 2, cap)

            st, lat_item = self._model_status_cells(m)
            self.models_table.setItem(i, 3, st)
            self.models_table.setItem(i, 4, lat_item)

        if hasattr(self, "models_count_lbl"):
            total = len(self.models)
            fav_n = sum(1 for m in self.models if m.key in fav_set)
            extra = f" · 收藏 {fav_n}"
            if only_fav:
                extra += " · 仅收藏"
            if prov:
                extra += f" · {prov}"
            self.models_count_lbl.setText(f"显示 {len(rows)} / 共 {total}{extra}")

    def refresh_providers(self):
        cfg = core.load_models_config()
        providers = cfg.get("providers") or {}
        self.provider_list.clear()
        for name in sorted(providers.keys()):
            self.provider_list.addItem(name)
        safe_cfg = core.redact_sensitive_config(cfg)
        self.provider_detail.setPlainText(json.dumps(safe_cfg, ensure_ascii=False, indent=2) if providers else "（暂无自定义 provider）")
        try:
            self.refresh_chat_model_choices()
        except Exception:
            pass

    def on_provider_selected(self, cur: QListWidgetItem | None, _prev):
        if not cur:
            return
        name = cur.text()
        cfg = core.load_models_config()
        data = (cfg.get("providers") or {}).get(name, {})
        preview = core.redact_sensitive_config(data)
        keys = core.list_provider_api_keys(name)
        preview["apiKeys"] = {
            "available": sum(1 for item in keys if item.get("status") == "available"),
            "invalid": sum(1 for item in keys if item.get("status") == "invalid"),
            "items": keys,
        }
        self.provider_detail.setPlainText(json.dumps(preview, ensure_ascii=False, indent=2))

    def refresh_sessions(self):
        if hasattr(self, "session_filter_wd"):
            self.sessions_apply_filter()
            return
        self._fill_sessions_table(core.list_sessions())

    def _session_path_at(self, row: int) -> str | None:
        item = self.sessions_table.item(row, 0)
        if not item:
            # 兼容旧 3 列布局：路径在第 2 列
            legacy = self.sessions_table.item(row, 2)
            return legacy.text() if legacy else None
        data = item.data(Qt.UserRole)
        if data:
            return str(data)
        legacy = self.sessions_table.item(row, 2)
        return legacy.text() if legacy else None

    def _session_cwd_at(self, row: int) -> str | None:
        item = self.sessions_table.item(row, 0)
        if item:
            cwd = item.data(Qt.UserRole + 1)
            if cwd:
                return str(cwd)
        # 工作目录列
        wd = self.sessions_table.item(row, 1)
        return wd.text() if wd and wd.text() else None

    def _fill_sessions_table(self, rows: list[dict[str, str]]) -> None:
        self.sessions_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            project = r.get("project") or core._project_name_from_path(r.get("cwd") or r.get("folder") or "")
            cwd = r.get("cwd") or r.get("folder") or ""
            model = r.get("model") or "—"
            when = r.get("started") or r.get("mtime_text") or ""
            preview = r.get("preview") or ""
            path = r.get("path") or ""

            proj_item = QTableWidgetItem(project)
            proj_item.setData(Qt.UserRole, path)
            proj_item.setData(Qt.UserRole + 1, cwd)
            tip = f"项目: {project}\n目录: {cwd}\n文件: {path}"
            if r.get("session_id"):
                tip += f"\nID: {r.get('session_id')}"
            proj_item.setToolTip(tip)
            self.sessions_table.setItem(i, 0, proj_item)

            cwd_item = QTableWidgetItem(cwd)
            cwd_item.setToolTip(cwd)
            self.sessions_table.setItem(i, 1, cwd_item)

            model_item = QTableWidgetItem(model)
            model_item.setToolTip(model)
            self.sessions_table.setItem(i, 2, model_item)

            time_item = QTableWidgetItem(when)
            time_item.setToolTip(when)
            self.sessions_table.setItem(i, 3, time_item)

            prev_item = QTableWidgetItem(preview or r.get("name") or "")
            prev_item.setToolTip(preview or r.get("name") or path)
            self.sessions_table.setItem(i, 4, prev_item)

    def launch_default(self):
        provider, model, thinking = core.get_default_model()
        self._launch(provider or None, model or None, thinking or None)

    def launch_selected(self):
        m = self.selected_model_row()
        if m:
            self._launch(m.provider, m.model, self.thinking_combo.currentText())
        else:
            self.launch_default()

    def _launch(self, provider, model, thinking):
        self.persist_mgr()
        try:
            cmd = core.launch_pi_interactive(
                self.workdir_edit.text().strip() or str(core.user_home()),
                provider=provider,
                model=model,
                thinking=thinking,
                terminal=str(self.terminal_combo.currentData() or self.terminal_combo.currentText() or "auto"),
            )
            self.status.showMessage(f"已启动: {cmd}")
        except Exception as e:
            QMessageBox.critical(self, "启动失败", str(e))

    def model_set_default(self):
        m = self.selected_model_row()
        if not m:
            QMessageBox.information(self, "提示", "请先选择模型")
            return
        core.set_default_model(m.provider, m.model, self.thinking_combo.currentText())
        self.refresh_dashboard()
        self.settings_load()
        self.fill_models_table()
        self.status.showMessage(f"默认模型已切换为 {m.key}")

    def model_add_favorite(self):
        # multi-select aware
        rows = self.selected_model_rows()
        if rows:
            self.model_add_favorite_batch()
            return
        m = self.selected_model_row()
        if not m:
            return
        favs = list(self.mgr.get("favorites") or [])
        if m.key not in favs:
            favs.append(m.key)
            self.mgr["favorites"] = favs
            self.persist_mgr()
            self.fill_favorites()
            self.fill_models_table()
        self.status.showMessage(f"已收藏 {m.key}")

    def model_launch(self):
        m = self.selected_model_row()
        if not m:
            return
        self._launch(m.provider, m.model, self.thinking_combo.currentText())

    def model_set_enabled(self):
        favs = list(self.mgr.get("favorites") or [])
        m = self.selected_model_row()
        if m and m.key not in favs:
            favs.append(m.key)
        if not favs:
            QMessageBox.information(self, "提示", "请先收藏一些模型，或选中一个模型")
            return
        core.set_enabled_models(favs)
        self.settings_load()
        self.status.showMessage(f"enabledModels = {favs}")
        QMessageBox.information(self, "已更新", "已写入 settings.enabledModels。\n在 Pi 会话中可用 Ctrl+P 在列表中循环切换。")

    def on_fav_double(self, item: QListWidgetItem):
        self._apply_favorite(item.text(), launch=False)

    def fav_set_default(self):
        item = self.fav_list.currentItem()
        if item:
            self._apply_favorite(item.text(), launch=False)

    def fav_launch(self):
        item = self.fav_list.currentItem()
        if item:
            self._apply_favorite(item.text(), launch=True)

    def fav_remove(self):
        item = self.fav_list.currentItem()
        if not item:
            return
        key = item.text()
        parsed = self._parse_favorite_key(key)
        if parsed:
            purge = core.purge_favorites(provider=parsed[0], model=parsed[1], redefault=True)
            self.mgr = core.load_manager_config()
            self.fill_favorites()
            self.fill_models_table()
            try:
                self.refresh_dashboard()
                self.settings_load()
            except Exception:
                pass
            if purge.get("default_changed"):
                np = purge.get("default_provider") or ""
                nm = purge.get("default_model") or ""
                if np and nm:
                    self.status.showMessage(f"已移除收藏 {key}；默认切换为 {np}/{nm}")
                else:
                    self.status.showMessage(f"已移除收藏 {key}；默认模型已清空")
            else:
                self.status.showMessage(f"已移除收藏 {key}")
            return
        self.mgr["favorites"] = [x for x in (self.mgr.get("favorites") or []) if x != key]
        self.persist_mgr()
        self.fill_favorites()
        self.fill_models_table()

    def _apply_favorite(self, key: str, launch: bool):
        if "/" not in key:
            return
        provider, model = key.split("/", 1)
        core.set_default_model(provider, model, self.thinking_combo.currentText())
        self.refresh_dashboard()
        self.settings_load()
        self.fill_models_table()
        self.status.showMessage(f"已切换到 {key}")
        if launch:
            self._launch(provider, model, self.thinking_combo.currentText())


    def provider_fetch_api(self):
        dlg = FetchModelsDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self.refresh_providers()
            self.refresh_models()
            self.status.showMessage("已从 API 拉取并保存 provider")

    def provider_add(self):
        dlg = ProviderEditorDialog(self)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            name, data = dlg.result_data()
            core.upsert_custom_provider(
                name,
                base_url=data["baseUrl"],
                api=data["api"],
                api_key=data["apiKey"],
                models=data["models"],
                compat=data["compat"],
            )
            self.refresh_providers()
            self.refresh_models()
            self.status.showMessage(f"已添加 provider: {name}")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def provider_edit(self):
        item = self.provider_list.currentItem()
        if not item:
            return
        name = item.text()
        cfg = core.load_models_config()
        existing = (cfg.get("providers") or {}).get(name, {})
        dlg = ProviderEditorDialog(self, existing=existing, name=name)
        if dlg.exec() != QDialog.Accepted:
            return
        try:
            _, data = dlg.result_data()
            core.upsert_custom_provider(
                name,
                base_url=data["baseUrl"],
                api=data["api"],
                api_key=data["apiKey"],
                models=data["models"],
                compat=data["compat"],
            )
            self.refresh_providers()
            self.refresh_models()
            self.status.showMessage(f"已更新 provider: {name}")
        except Exception as e:
            QMessageBox.warning(self, "保存失败", str(e))

    def provider_manage_keys(self):
        item = self.provider_list.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择 provider")
            return
        ProviderKeysDialog(item.text(), self).exec()
        self.on_provider_selected(item, None)

    def provider_delete(self):
        item = self.provider_list.currentItem()
        if not item:
            return
        name = item.text()
        if QMessageBox.question(
            self,
            "确认",
            f"删除自定义 provider「{name}」？\n\n将同时移除收藏中该 Provider 的全部模型；\n若当前默认属于该 Provider，会自动切换到下一个收藏模型。",
        ) != QMessageBox.Yes:
            return
        result = core.delete_custom_provider(name)
        purge = result.get("_purge") if isinstance(result, dict) else None
        # 重新加载 manager 配置（收藏可能已变）
        try:
            self.mgr = core.load_manager_config()
        except Exception:
            pass
        self.refresh_providers()
        self.refresh_models()
        try:
            self.fill_favorites()
            self.refresh_dashboard()
            self.settings_load()
            self.refresh_chat_model_choices()
        except Exception:
            pass
        removed_n = len((purge or {}).get("removed_favorites") or [])
        msg = f"已删除 Provider「{name}」"
        if removed_n:
            msg += f"，清理收藏 {removed_n} 项"
        if (purge or {}).get("default_changed"):
            np = (purge or {}).get("default_provider") or ""
            nm = (purge or {}).get("default_model") or ""
            if np and nm:
                msg += f"；默认已切换为 {np}/{nm}"
            else:
                msg += "；默认模型已清空（无剩余收藏）"
        self.status.showMessage(msg)

    def provider_add_model(self):
        item = self.provider_list.currentItem()
        if not item:
            QMessageBox.information(self, "提示", "请先选择 provider")
            return
        name = item.text()
        model_id, ok = QInputDialog.getText(self, "添加模型", "模型 ID：")
        if not ok or not model_id.strip():
            return
        tpl = core.default_model_template(model_id.strip())
        mid = tpl.pop("id")
        core.add_model_to_provider(name, mid, **tpl)
        self.refresh_providers()
        self.refresh_models()

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

        providers = sorted({m.provider for m in (self.models or []) if m.provider})
        # 也并入 models.json 自定义 providers（即使尚未 list-models）
        try:
            cfg = core.load_models_config()
            for name in (cfg.get("providers") or {}):
                if name and name not in providers:
                    providers.append(str(name))
            providers = sorted(set(providers))
        except Exception:
            pass

        self.chat_provider.blockSignals(True)
        self.chat_provider.clear()
        for p in providers:
            self.chat_provider.addItem(p)
        self.chat_provider.blockSignals(False)

        if cur_p:
            self._set_chat_combo_text(self.chat_provider, cur_p)
        elif providers:
            # 默认选中当前默认 provider
            try:
                dp, _, _ = core.get_default_model()
            except Exception:
                dp = ""
            self._set_chat_combo_text(self.chat_provider, dp or providers[0])

        self._reload_chat_models_for_provider(self._chat_combo_text(self.chat_provider), prefer_model=cur_m)

    def _on_chat_provider_changed(self, _text: str = "") -> None:
        if not hasattr(self, "chat_model") or not isinstance(self.chat_model, QComboBox):
            return
        prefer = self._chat_combo_text(self.chat_model)
        self._reload_chat_models_for_provider(self._chat_combo_text(self.chat_provider), prefer_model=prefer)

    def _reload_chat_models_for_provider(self, provider: str, prefer_model: str = "") -> None:
        if not hasattr(self, "chat_model") or not isinstance(self.chat_model, QComboBox):
            return
        provider = (provider or "").strip()
        models: list[str] = []
        for m in self.models or []:
            if not provider or m.provider == provider:
                if m.model and m.model not in models:
                    models.append(m.model)
        # models.json 兜底
        if not models and provider:
            try:
                cfg = core.load_models_config()
                pdata = (cfg.get("providers") or {}).get(provider) or {}
                for item in pdata.get("models") or []:
                    mid = ""
                    if isinstance(item, dict):
                        mid = str(item.get("id") or item.get("model") or "")
                    elif isinstance(item, str):
                        mid = item
                    if mid and mid not in models:
                        models.append(mid)
            except Exception:
                pass

        self.chat_model.blockSignals(True)
        self.chat_model.clear()
        for mid in models:
            self.chat_model.addItem(mid)
        self.chat_model.blockSignals(False)

        if prefer_model and (not provider or any(m.provider == provider and m.model == prefer_model for m in (self.models or [])) or prefer_model in models):
            self._set_chat_combo_text(self.chat_model, prefer_model)
        elif models:
            try:
                dp, dm, _ = core.get_default_model()
            except Exception:
                dp, dm = "", ""
            if provider and dp == provider and dm in models:
                self._set_chat_combo_text(self.chat_model, dm)
            else:
                self._set_chat_combo_text(self.chat_model, models[0])
        else:
            self.chat_model.setEditText(prefer_model or "")

    def chat_fill_default(self):
        p, m, _t = core.get_default_model()
        if hasattr(self, "chat_provider") and isinstance(self.chat_provider, QComboBox):
            # 确保下拉有数据
            if self.chat_provider.count() == 0:
                self.refresh_chat_model_choices()
            self._set_chat_combo_text(self.chat_provider, p)
            self._reload_chat_models_for_provider(p, prefer_model=m)
            self._set_chat_combo_text(self.chat_model, m)
        else:
            # 旧控件兼容
            try:
                self.chat_provider.setText(p)
                self.chat_model.setText(m)
            except Exception:
                pass

    def chat_send(self):
        prompt = self.chat_input.toPlainText().strip()
        if not prompt:
            return
        provider = self._chat_combo_text(self.chat_provider) or None
        model = self._chat_combo_text(self.chat_model) or None
        workdir = self.workdir_edit.text().strip() or str(core.user_home())
        self.chat_output.appendPlainText(f"\n>>> {prompt}\n…请求中，请稍候…\n")
        self.status.showMessage("Pi 快速提问运行中…")

        def job():
            return extras.chat_with_failover(
                prompt,
                provider=provider,
                model=model,
                workdir=workdir,
                thinking=self.thinking_combo.currentText(),
            )

        w = self._track(Worker(job))
        self.chat_input.setEnabled(False)
        w.done.connect(self._on_basic_chat_done)
        w.failed.connect(self._on_basic_chat_fail)
        w.start()

    def _on_basic_chat_done(self, result):
        self.chat_input.setEnabled(True)
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
            self.chat_output.appendPlainText(f"\n[exit {code} · {p}/{m}]")
            self.status.showMessage("快速提问完成" if result.get("ok") else "快速提问失败")
            return
        # 兼容旧 tuple 返回
        code, out, err = result
        if out.strip():
            self.chat_output.appendPlainText(out.strip())
        if err.strip():
            self.chat_output.appendPlainText(f"[stderr]\n{err.strip()}")
        self.chat_output.appendPlainText(f"\n[exit {code}]")
        self.status.showMessage("快速提问完成")

    def _on_basic_chat_fail(self, e: str):
        self.chat_input.setEnabled(True)
        self.chat_output.appendPlainText(f"[错误] {e}")
        self.status.showMessage("快速提问失败")

    def session_reveal(self):
        rows = self.sessions_table.selectionModel().selectedRows()
        if not rows:
            return
        path = self._session_path_at(rows[0].row())
        if path:
            core.open_in_explorer(path)

    def session_open_project(self):
        rows = self.sessions_table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "提示", "请先选择会话")
            return
        cwd = self._session_cwd_at(rows[0].row())
        if not cwd:
            QMessageBox.information(self, "提示", "无法解析该会话的项目目录")
            return
        p = Path(cwd)
        if not p.exists():
            QMessageBox.warning(self, "目录不存在", f"项目目录不存在：\n{cwd}")
            return
        core.open_path(str(p))

    def session_continue(self):
        rows = self.sessions_table.selectionModel().selectedRows()
        if not rows:
            return
        path = self._session_path_at(rows[0].row())
        if not path:
            return
        cwd = self._session_cwd_at(rows[0].row()) or self.workdir_edit.text().strip() or str(core.user_home())
        self.persist_mgr()
        try:
            cmd = core.launch_pi_interactive(
                cwd,
                terminal=str(self.terminal_combo.currentData() or self.terminal_combo.currentText() or "auto"),
                extra=["--session", path],
            )
            self.status.showMessage(f"继续会话: {cmd}")
        except Exception as e:
            QMessageBox.critical(self, "启动失败", str(e))



    def apply_ui_theme_from_settings(self):
        mode = self.set_ui_mode.currentData() if hasattr(self, "set_ui_mode") else "night"
        accent = self.set_ui_accent.currentData() if hasattr(self, "set_ui_accent") else "blue"
        core.set_ui_theme(mode=mode, accent=accent)
        self.apply_ui_theme(mode, accent)

    def toggle_ui_mode(self):
        ut = core.get_ui_theme()
        mode = "day" if normalize_mode(ut.get("mode")) == "night" else "night"
        accent = ut.get("accent") or "blue"
        core.set_ui_theme(mode=mode, accent=accent)
        self.apply_ui_theme(mode, accent)

    def _startup_checks(self):
        try:
            core.apply_language_preference(core.get_language())
            from pi_manager.builtin_themes import ensure_builtin_themes
            ensure_builtin_themes()
        except Exception:
            pass
        # first-run wizard
        if not core.is_setup_done():
            self.open_setup_wizard(force=True)
        # update check：官方 Pi CLI + Pi Manager 自身
        cfg = core.load_manager_config()
        if cfg.get("auto_check_update", True):
            w = self._track(Worker(core.needs_pi_install_or_update))
            w.done.connect(self._on_update_status)
            w.failed.connect(lambda e: self.status.showMessage(f"检查 Pi 更新失败: {e}"))
            w.start()
            # Manager 自身：静默检查，有新版本再弹窗
            try:
                self.check_manager_update(silent=True)
            except Exception:
                pass

    def _on_update_status(self, st: dict):
        self.status.showMessage(st.get("message") or "")
        if st.get("blocked") or st.get("check_failed"):
            return
        needs_action = st.get("missing") or st.get("outdated") or st.get("repair_required")
        if needs_action and st.get("installable"):
            ret = QMessageBox.question(
                self,
                "Pi \u5b89\u88c5 / \u66f4\u65b0",
                f"{st.get('message')}\n\n\u662f\u5426\u73b0\u5728\u6267\u884c\u517c\u5bb9\u901a\u9053\u7684\u5b89\u88c5/\u4fee\u590d\uff1f\n"
                "\uff08\u4e5f\u53ef\u7a0d\u540e\u5728\u4fa7\u8fb9\u680f\u300c\u8bbe\u7f6e\u300d\u4e2d\u64cd\u4f5c\uff09",
            )
            if ret == QMessageBox.Yes:
                self.open_install_dialog(st)

    def open_setup_wizard(self, force: bool = False):
        dlg = SetupWizardDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self.settings_load()
            self.apply_ui_theme()
            self.refresh_dashboard()
            self.status.showMessage("基础配置已保存")
        elif force:
            # still mark soft skip? keep setup_done false so next launch asks again
            pass

    def open_install_dialog(self, status: dict | None = None):
        if not isinstance(status, dict):
            status = None
        dlg = InstallPiDialog(self, status=status)
        dlg.exec()
        self.refresh_dashboard()
        if dlg.install_succeeded:
            self.status.showMessage("Pi 已安装或升级完成，已返回管理器面板。", 6000)

    def check_pi_update(self):
        self.status.showMessage("正在检查 Pi 版本…")
        w = self._track(Worker(core.needs_pi_install_or_update))
        w.done.connect(self._on_manual_update_status)
        w.failed.connect(lambda e: QMessageBox.warning(self, "检查失败", e))
        w.start()

    def _on_manual_update_status(self, st: dict):
        message = st.get("message") or ""
        self.status.showMessage(message)
        if st.get("check_failed"):
            QMessageBox.warning(self, "Pi \u7248\u672c\u68c0\u67e5\u5931\u8d25", message)
            return
        if st.get("blocked"):
            QMessageBox.warning(self, "Pi \u66f4\u65b0\u73af\u5883\u4e0d\u517c\u5bb9", message)
            return
        if st.get("ok"):
            QMessageBox.information(self, "Pi \u72b6\u6001", message or "\u5df2\u662f\u517c\u5bb9\u901a\u9053\u6700\u65b0\u7248")
            return
        needs_action = st.get("missing") or st.get("outdated") or st.get("repair_required")
        if needs_action and st.get("installable"):
            ret = QMessageBox.question(
                self,
                "Pi \u72b6\u6001",
                f"{message}\n\n\u662f\u5426\u6267\u884c\u5b89\u88c5/\u5347\u7ea7/\u4fee\u590d\uff1f",
            )
            if ret == QMessageBox.Yes:
                self.open_install_dialog(st)
            return
        QMessageBox.warning(self, "Pi \u72b6\u6001", message or "\u65e0\u6cd5\u5b8c\u6210 Pi \u7248\u672c\u68c0\u67e5\u3002")

    def settings_load(self):
        s = core.load_settings()
        self.set_provider.setText(str(s.get("defaultProvider") or ""))
        self.set_model.setText(str(s.get("defaultModel") or ""))
        th = str(s.get("defaultThinkingLevel") or "medium")
        i = self.set_thinking.findText(th)
        if i >= 0:
            self.set_thinking.setCurrentIndex(i)
        # The Pi CLI theme is derived from the global day/night mode and has
        # no independent setting control.
        if hasattr(self, "set_language"):
            lang = core.get_language()
            for i in range(self.set_language.count()):
                if self.set_language.itemData(i) == lang:
                    self.set_language.setCurrentIndex(i)
                    break
        enabled = s.get("enabledModels") or []
        if isinstance(enabled, list):
            self.set_enabled.setPlainText("\n".join(str(x) for x in enabled))
        else:
            self.set_enabled.setPlainText(str(enabled))

        if hasattr(self, "set_ui_mode"):
            ut = core.get_ui_theme()
            for i in range(self.set_ui_mode.count()):
                if self.set_ui_mode.itemData(i) == ut.get("mode"):
                    self.set_ui_mode.setCurrentIndex(i)
                    break
            for i in range(self.set_ui_accent.count()):
                if self.set_ui_accent.itemData(i) == ut.get("accent"):
                    self.set_ui_accent.setCurrentIndex(i)
                    break
        self.settings_raw.setPlainText(json.dumps(s, ensure_ascii=False, indent=2))
        self.load_feature_settings_fields()

    def settings_save(self):
        current_theme = core.get_ui_theme()
        mode = (
            self.set_ui_mode.currentData()
            if hasattr(self, "set_ui_mode")
            else current_theme.get("mode")
        ) or "night"
        accent = (
            self.set_ui_accent.currentData()
            if hasattr(self, "set_ui_accent")
            else current_theme.get("accent")
        ) or "blue"
        core.set_ui_theme(mode=mode, accent=accent)
        settings = core.load_settings()
        settings["defaultProvider"] = self.set_provider.text().strip()
        settings["defaultModel"] = self.set_model.text().strip()
        settings["defaultThinkingLevel"] = self.set_thinking.currentText()
        settings["theme"] = core.cli_theme_for_ui_mode(mode)
        if hasattr(self, "set_language"):
            core.set_language(self.set_language.currentData() or "zh-CN")
        lines = [x.strip() for x in self.set_enabled.toPlainText().splitlines() if x.strip()]
        if lines:
            settings["enabledModels"] = lines
        elif "enabledModels" in settings:
            del settings["enabledModels"]
        core.save_settings(settings)
        self.save_feature_settings_fields()
        self.apply_ui_theme(mode, accent)
        final_settings = core.load_settings()
        self.settings_raw.setPlainText(json.dumps(final_settings, ensure_ascii=False, indent=2))
        self.refresh_dashboard()
        self.status.showMessage("\u8bbe\u7f6e\u5df2\u4fdd\u5b58\uff0c\u7ba1\u7406\u5668\u4e0e Pi CLI \u5df2\u540c\u6b65\u4e3b\u9898")
        QMessageBox.information(
            self,
            "\u5df2\u4fdd\u5b58",
            "\u5168\u5c40\u663c\u591c\u4e3b\u9898\u3001settings.json \u4e0e Pi Manager \u504f\u597d\u5df2\u540c\u6b65\u3002",
        )


def run_app():
    import sys

    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass
    try:
        QApplication.setAttribute(Qt.AA_DontUseNativeDialogs, True)
    except Exception:
        pass
    app = QApplication(sys.argv)
    app.setApplicationName("Pi Manager")
    app.setOrganizationName("PiManager")
    app.setQuitOnLastWindowClosed(False)
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    apply_app_font(app)
    try:
        from .ui_features import app_icon
        app.setWindowIcon(app_icon())
    except Exception:
        pass
    theme = core.get_ui_theme()
    core.sync_cli_theme_with_ui(theme.get("mode"))
    from .presentation.design import apply_application_theme

    apply_application_theme(app, theme.get("mode"), theme.get("accent"))
    from .presentation.main_window import ModernMainWindow

    win = ModernMainWindow()
    win.show()
    return app.exec()
