"""Main window UI for Pi Manager."""
from __future__ import annotations

import inspect
import json
import logging
import os
import time
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
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from . import core
from . import extras
from . import provider_presets
from .presentation.design import ACCENT_LABELS, apply_app_font, normalize_mode
from .ui_features import FeatureMixin


LATENCY_OK_MS = 800
LATENCY_WARN_MS = 2000
BATCH_TEST_TIMEOUT_PI = 90
BATCH_TEST_TIMEOUT_DIRECT = 45
SINGLE_INSTANCE_SERVER_NAME = "PiManager"

logger = logging.getLogger(__name__)


def _accepts_is_cancelled(fn) -> bool:
    """判断 job 是否声明了 ``is_cancelled`` 形参（协作式取消契约的入口）。

    只做静态签名检查：内置/C 扩展等取不到签名的可调用体一律视为不可取消。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "is_cancelled" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


# 关闭预算耗尽后仍在运行的 Worker 会被移到这里：脱离 parent、保持强引用，
# 直到进程结束。宁可泄漏一个线程，也不要让 QThread 析构于运行态触发
# qFatal("QThread: Destroyed while thread is still running") 导致崩溃退出。
_ORPHANED_WORKERS: list[QThread] = []


def detach_running_worker(worker) -> bool:
    """把仍在运行的 worker 从 Qt 对象树与登记表中摘出并延寿到进程结束。"""
    if worker is None:
        return False
    try:
        if not worker.isRunning():
            return False
    except RuntimeError:
        # C++ 侧已销毁：无需处理
        return False
    try:
        worker.setParent(None)
    except (RuntimeError, TypeError) as e:
        logger.warning("detach worker parent failed: %s", e)
    if worker not in _ORPHANED_WORKERS:
        _ORPHANED_WORKERS.append(worker)
    logger.warning(
        "background worker %s did not finish within the shutdown budget; "
        "detached to avoid destroying a running QThread",
        type(worker).__name__,
    )
    return True


def drain_pending_connections(server) -> int:
    """取走并关闭 QLocalServer 的全部挂起连接，返回处理数量。

    ``QLocalServer`` 在 ``newConnection`` 发出后把已建立的 ``QLocalSocket`` 放进
    内部 pending 队列，**必须**由使用者 ``nextPendingConnection()`` 取走并负责
    销毁。以前的唤醒槽从不取走：每次双击图标唤醒都泄漏一个 socket / 命名管道
    句柄，默认 ``maxPendingConnections()=30``，队列满后 ``newConnection`` 不再
    发出 —— 双击图标彻底不再唤醒窗口，且第二实例连不上后静默退出。
    """
    drained = 0
    while True:
        conn = server.nextPendingConnection()
        if conn is None:
            break
        conn.disconnected.connect(conn.deleteLater)
        conn.close()
        drained += 1
    return drained


class Worker(QThread):
    """后台任务线程，带显式的协作式取消契约。

    ``requestInterruption()`` 本身只是给 QThread 置一个标志位；只有 job 主动
    查询才有效果。因此：

    * 若 ``fn`` 声明了 ``is_cancelled`` 形参（或接受 ``**kwargs``），``run()``
      会自动注入 ``self.isInterruptionRequested``，job 可在分段点自行退出；
      此时 ``cancellable`` 为 True。
    * 否则 ``cancellable`` 为 False —— 关闭流程据此知道该任务无法被打断，
      不再假装 2.5s 预算能把它收走（见 ``detach_running_worker``）。
    """

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self._inject_cancel = "is_cancelled" not in kwargs and _accepts_is_cancelled(fn)
        self.cancellable = bool(self._inject_cancel or "is_cancelled" in kwargs)

    def run(self):
        kwargs = dict(self.kwargs)
        if self._inject_cancel:
            kwargs["is_cancelled"] = self.isInterruptionRequested
        try:
            result = self.fn(*self.args, **kwargs)
        except Exception as e:
            if self.isInterruptionRequested():
                # 取消导致的异常不再上报：接收槽可能已随窗口关闭而失效。
                logger.info("Worker task aborted after interruption request")
                return
            logger.exception("Worker task failed")
            self.failed.emit(str(e)[:500])
            return
        if self.isInterruptionRequested():
            return
        self.done.emit(result)


class WorkerTrackerMixin:
    """管理后台 Worker 生命周期：登记、完成时 deleteLater + 移除。

    子类需在 __init__ 中调用 ``_init_workers`` 初始化跟踪列表。
    ``_adopt_worker`` 默认把 worker 的 parent 设为 self（随窗口清理）；
    MainWindow 重写为 no-op（窗口关闭即应用退出）。
    各子类应保留自己的 ``closeEvent``（超时/拒绝逻辑各不相同，不在此统一）。
    """

    def _init_workers(self) -> None:
        self._workers: list[Worker] = []

    def _adopt_worker(self, worker: Worker) -> None:
        worker.setParent(self)

    def _track(self, worker: Worker) -> Worker:
        self._adopt_worker(worker)
        self._workers.append(worker)
        worker.finished.connect(lambda: self._untrack(worker))
        worker.finished.connect(worker.deleteLater)
        return worker

    def _untrack(self, worker: Worker) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass

    def _reap_workers(self, budget: float = 5.0) -> list[Worker]:
        """关闭前收割本对象的 Worker；返回预算耗尽后仍在运行者（已脱钩）。

        默认 ``_adopt_worker`` 会把 worker 的 parent 设为 self，对话框销毁时
        Qt 会连带销毁子对象 —— 包括仍在运行的 QThread，触发
        qFatal("QThread: Destroyed while thread is still running")。
        以前这里 ``wait()`` 的返回值被忽略、无论是否等到都放行；现在超时的
        worker 一律脱离 parent 并延寿到进程结束（``detach_running_worker``），
        既不阻塞用户关闭窗口，也不会崩溃。
        """
        running = [w for w in self._workers if w.isRunning()]
        if not running:
            return []
        for w in running:
            w.requestInterruption()
        deadline = time.monotonic() + budget
        for w in running:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if w.isRunning() and remaining:
                w.wait(remaining)
        stuck = []
        for w in running:
            if detach_running_worker(w):
                self._untrack(w)
                stuck.append(w)
        return stuck

    def _note_detached_workers(self) -> None:
        """把「请求仍在后台收尾」写到对话框自己的状态标签（可被子类覆写）。"""
        for attr in ("fetch_status", "status", "log"):
            label = getattr(self, attr, None)
            setter = getattr(label, "setText", None) if label is not None else None
            if callable(setter):
                setter("网络请求未能在 5 秒内取消，已转入后台收尾；窗口可安全关闭。")
                return


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
        # 模型批测走 test_models_batch_concurrent，一直支持 is_cancelled；
        # 健康检查取决于 extras.run_health_check 是否已声明该形参（见下）。
        self.cancellable = kind != "health" or _accepts_is_cancelled(extras.run_health_check)

    def run(self):
        try:
            if self.kind == "health":
                def on_one(res):
                    self.progress.emit(res)

                health_kwargs = {}
                if self.cancellable:
                    # extras.run_health_check 目前尚未声明 is_cancelled；一旦补上
                    # 该形参，这里会自动把中断信号透传下去，无需再改 UI 层。
                    health_kwargs["is_cancelled"] = self.isInterruptionRequested
                result = extras.run_health_check(
                    pairs=self.pairs or None,
                    mode=self.mode,
                    scope=self.health_scope,
                    selected=self.health_selected,
                    on_one=on_one,
                    **health_kwargs,
                )
                if self.isInterruptionRequested():
                    return
                self.done.emit(result)
                return

            timeout = self.timeout if self.timeout is not None else (
                BATCH_TEST_TIMEOUT_PI if self.mode == "pi" else BATCH_TEST_TIMEOUT_DIRECT
            )

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
            if self.isInterruptionRequested():
                return
            self.done.emit(results)
        except Exception as e:
            logger.exception("BatchTestWorker failed")
            self.failed.emit(str(e))



class ProviderEditorDialog(WorkerTrackerMixin, QDialog):
    def __init__(self, parent=None, existing: dict[str, Any] | None = None, name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("编辑自定义 Provider" if existing else "添加自定义 Provider")
        self.resize(680, 640)
        self.existing = existing or {}
        self._worker = None
        self._init_workers()
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
        preset_tip = QLabel(
            "选择模板会自动填充 Base URL / API 类型 / 模型列表，\n"
            "你只需粘贴自己的 API Key 后保存即可接入（模板含国内外主流大模型）。"
        )
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
        self.models_text.setPlainText(
            json.dumps(preset.get("models") or [], ensure_ascii=False, indent=2)
        )
        self._fetched_models = []
        self.model_pick.clear()
        hint = str(preset.get("hint") or "")
        key_url = str(preset.get("key_url") or "")
        text = hint
        if key_url:
            text += f"\n获取 API Key：{key_url}"
        text += "\n填写 API Key 后可直接保存，或点击下方按钮拉取最新模型列表。"
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
        self._reveal = False
        self.setWindowTitle(f"API Keys · {provider}")
        self.resize(760, 460)

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


class FetchModelsDialog(WorkerTrackerMixin, QDialog):
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
    ("plugins", "插件", "内置 skills / extensions 一键安装"),
    ("settings", "设置", "语言 / 主题 / 代理"),
    ("help", "使用教程", "教程与常见问题"),
]


class InstallPiDialog(WorkerTrackerMixin, QDialog):
    """Install or upgrade the Node-compatible official Pi npm channel."""

    def __init__(self, parent=None, status: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("\u5b89\u88c5 / \u5347\u7ea7 Pi")
        self.resize(620, 460)
        self._worker = None
        self._init_workers()
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
            f"Node.js\uff1a{node_version or '未检测到'}    "
            f"npm\uff1a{npm_version or '未检测到'}\n"
            f"\u517c\u5bb9\u901a\u9053\uff1a{channel or '不可用'}    "
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
        # 不确定态进度条：npm install -g 可跑数分钟，此前界面只有一行文本，
        # 两个按钮同时置灰、closeEvent 拒绝关闭，用户无法判断是否还在动。
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setVisible(False)
        self.progress.setAccessibleName("安装进度")
        layout.addWidget(self.progress)
        row = QHBoxLayout()
        self.btn_install = QPushButton("\u5f00\u59cb\u5b89\u88c5/\u5347\u7ea7")
        self.btn_install.setProperty("success", True)
        self.btn_install.clicked.connect(self._run)
        self.btn_close = QPushButton("\u5173\u95ed")
        self.btn_close.setProperty("secondary", True)
        self.btn_close.clicked.connect(self.accept)
        # \u952e\u76d8\u53ef\u8fbe\u6027\uff1a\u6b64\u524d\u672c\u5bf9\u8bdd\u6846\u7528\u88f8 QPushButton\uff0c\u6ca1\u6709\u9ed8\u8ba4\u6309\u94ae\uff0c\u56de\u8f66\u952e\u65e0\u54cd\u5e94\u3002
        self.btn_install.setAutoDefault(True)
        self.btn_install.setDefault(True)
        self.btn_close.setAutoDefault(False)
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

    def closeEvent(self, event):
        # \u67e5\u8be2\u767b\u8bb0\u8868\u800c\u975e self._worker\uff1a_track \u8fde\u63a5\u4e86 finished \u2192 deleteLater\uff0c
        # \u5b89\u88c5\u5b8c\u6210\u540e self._worker \u4f1a\u53d8\u6210\u60ac\u5782\u7684 Python \u5305\u88c5\u5668\uff0c\u518d\u6b21\u89e6\u53d1
        # closeEvent\uff08X / Alt+F4 / \u7236\u7a97\u53e3\u9500\u6bc1\u94fe\uff09\u65f6 isRunning() \u4f1a\u629b
        # RuntimeError: Internal C++ object (Worker) already deleted\u3002
        # _untrack \u5728 deleteLater \u4e4b\u524d\u89e6\u53d1\uff0c\u6545 self._workers \u5929\u7136\u5b89\u5168\u3002
        # \u4e3b\u52a8\u5173\u95ed\u65f6\u5148\u6536\u5272\u8fd0\u884c\u4e2d\u7684 worker\uff08\u9884\u7b97 2.5s\uff0c\u8d85\u65f6\u8005\u8131\u94a9\u5ef6\u5bff\uff09\uff0c\u65e2\u4e0d
        # \u95ea\u9000\u4e5f\u4e0d\u5361\u6b7b\uff1a\u4ecd\u5728\u8fd0\u884c\u7684\u4efb\u52a1\u8f6c\u5165\u540e\u53f0\u6536\u5c3e\uff0c\u5bf9\u8bdd\u6846\u53ef\u5b89\u5168\u5173\u95ed\u3002
        stuck = self._reap_workers(budget=2.5)
        if stuck:
            self._note_detached_workers()
        super().closeEvent(event)

    def _run(self):
        self.install_succeeded = False
        self.btn_install.setEnabled(False)
        self.btn_close.setEnabled(False)
        command_text = f"npm install -g {self.package_spec}" if self.package_spec else "npm install -g"
        self.log.appendPlainText(f"\u6b63\u5728\u6267\u884c {command_text} ...")
        self.progress.setVisible(True)
        self._worker = self._track(Worker(core.install_or_update_pi))
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
        self.btn_close.setEnabled(True)
        self.progress.setVisible(False)
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
        self.btn_close.setEnabled(True)
        self.progress.setVisible(False)
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
        buttons.button(QDialogButtonBox.Save).setDefault(True)
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
        # 持锁读改写：裸的 load → 改 → save 会覆盖掉 load 与 save 之间
        # 其它写者（测试线程池、健康检查、helper 进程）对 pi-manager.json 的写入。
        secure_keys = self.secure.isChecked()
        auto_check = self.auto_update.isChecked()

        def _apply_setup(cfg: dict) -> dict:
            cfg["secure_keys"] = secure_keys
            cfg["auto_check_update"] = auto_check
            return cfg

        core.update_manager_config(_apply_setup)
        core.mark_setup_done(True)
        try:
            core.run_first_time_bootstrap()
        except Exception as e:
            logger.warning("first-run bootstrap failed: %s", e)
        self.accept()


class MainWindow(WorkerTrackerMixin, FeatureMixin, QMainWindow):
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
        # 1080×720 的最小尺寸在常见笔记本上不可用：1366×768 @125% 的可用逻辑
        # 高度约 614 px（PassThrough 舍入策略下逻辑尺寸直接受缩放影响），窗口
        # 无法缩到屏幕内，底部状态栏与页面底部按钮会被推到屏幕外且无法找回。
        # 多数页面已用 QScrollArea 承担内容溢出，故下调下限。
        self.setMinimumSize(960, 600)
        self.models: list[core.ModelInfo] = []
        self._init_workers()
        self.workers = self._workers  # 公共别名，兼容现有测试与外部引用
        self.test_results: dict[str, dict[str, Any]] = {}
        self.mgr = core.load_manager_config()
        self._pi_update_status = core.load_pi_update_status()
        self._last_manager_update: dict[str, Any] = {}
        self._prompted_manager_versions: set[str] = set()
        self.setAcceptDrops(True)
        self.init_feature_state()
        # 必须在 _build_ui() 之前赋值：页面构建器（如插件页）需要据此判断
        # 是否允许在构造期起后台线程，否则 start_background=False 契约失效。
        self._background_enabled = bool(start_background)
        self._build_ui()
        self._refresh_update_indicators()
        self._restore_window_geometry()
        if self._background_enabled:
            self.refresh_all()
            self.setup_system_tray()
            # Defer first-run / update checks so the shell paints first.
            # 用有 parent 的 QTimer 而非裸 singleShot：窗口若在 400 ms 内被销毁
            # （嵌入场景 / 快速退出），无 parent 的 singleShot 会在已删除对象上
            # 触发槽；parented timer 随窗口一并销毁。
            from PySide6.QtCore import QTimer
            self._startup_timer = QTimer(self)
            self._startup_timer.setSingleShot(True)
            self._startup_timer.timeout.connect(self._startup_checks)
            self._startup_timer.start(400)
            if bool(self.mgr.get("start_minimized")) and self.tray:
                QTimer.singleShot(0, self.hide)

    def _build_ui(self):
        raise NotImplementedError(
            "MainWindow 仅作为行为基类使用；请实例化 presentation.ModernMainWindow"
        )

    # ---- 窗口几何持久化 -----------------------------------------------------
    def _restore_window_geometry(self) -> None:
        """恢复上次的窗口大小/位置。

        以前完全没有几何持久化（全仓库无 saveGeometry/restoreGeometry），每次
        启动都回到 1320×880 居中；而侧边栏折叠状态倒是持久化了，对比之下突兀。
        恢复后校验窗口是否落在某块可用屏幕内（显示器拔掉/分辨率变化时可能落到
        屏幕外），不可见则回退默认几何。
        """
        raw = str((self.mgr or {}).get("ui_geometry") or "")
        if not raw:
            return
        try:
            from PySide6.QtCore import QByteArray

            if not self.restoreGeometry(QByteArray.fromHex(raw.encode("ascii"))):
                return
        except Exception as e:
            logger.warning("restore window geometry failed: %s", e)
            return
        try:
            from PySide6.QtGui import QGuiApplication

            center = self.frameGeometry().center()
            if QGuiApplication.screenAt(center) is None:
                logger.info("saved window geometry is off-screen; falling back to default")
                self.resize(1320, 880)
                primary = QGuiApplication.primaryScreen()
                if primary is not None:
                    available = primary.availableGeometry()
                    frame = self.frameGeometry()
                    frame.moveCenter(available.center())
                    self.move(frame.topLeft())
        except Exception as e:
            logger.warning("validate restored geometry failed: %s", e)

    def _save_window_geometry(self) -> None:
        # 只在窗口真的显示过时落盘：offscreen 测试与嵌入场景的几何值没有意义，
        # 写进去只会用默认尺寸污染用户配置。closeEvent 与 quit_app 都发生在
        # hide() 之前，正常路径上窗口仍是可见的。
        if not self.isVisible():
            return
        try:
            geometry = bytes(self.saveGeometry().toHex()).decode("ascii")
            if geometry == str((self.mgr or {}).get("ui_geometry") or ""):
                return  # 未变化：不必重写配置
            self.mgr["ui_geometry"] = geometry
            core.save_manager_config(self.mgr)
        except Exception as e:
            logger.warning("save window geometry failed: %s", e)

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

    def _polish_tree(self, tree: QTreeWidget) -> None:
        """统一树状列表观感（模型按 Provider 分组）。"""
        tree.setAlternatingRowColors(True)
        tree.setSelectionBehavior(QAbstractItemView.SelectRows)
        tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tree.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tree.setWordWrap(False)
        tree.setRootIsDecorated(True)
        tree.setUniformRowHeights(True)
        tree.setIndentation(20)
        tree.setAllColumnsShowFocus(True)
        tree.setExpandsOnDoubleClick(False)
        tree.setFocusPolicy(Qt.StrongFocus)
        tree.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        tree.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        tree.header().setStretchLastSection(False)
        tree.header().setMinimumSectionSize(60)

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
        except Exception as e:
            logger.warning("auto set default model failed: %s", e)
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

    #: ``_table_colors`` 缓存的兜底存活时间（秒）。主题变更都会走
    #: ``apply_ui_theme`` 显式失效，TTL 只是防御「有人绕过该入口改主题」。
    THEME_CACHE_TTL = 2.0

    def invalidate_theme_cache(self) -> None:
        self._table_colors_cache = None

    def _table_colors(self):
        """当前主题的 token（带缓存）。

        ``core.get_ui_theme()`` 会读配置（os.stat + deepcopy），实测量级从
        数十 µs 到 2 ms 不等。以前模型表每行调 2 次（本类一次、presentation
        覆写再一次），200 模型的批量测试仅此一项就烧掉数秒主线程。

        现在：一次重建只求一次并逐行注入；跨调用再由本缓存兜住（批量测试逐项
        增量刷新会连续命中）。缓存在 ``apply_ui_theme`` 中显式失效。
        """
        from .presentation.design import tokens_for

        cached = getattr(self, "_table_colors_cache", None)
        if cached is not None:
            colors, stamp = cached
            if time.monotonic() - stamp < self.THEME_CACHE_TTL:
                return colors
        theme = core.get_ui_theme()
        colors = tokens_for(theme.get("mode"), theme.get("accent"))
        self._table_colors_cache = (colors, time.monotonic())
        return colors

    def _model_status_cells(
        self, m: core.ModelInfo, colors=None
    ) -> tuple[str, str, QColor, QColor, str, str]:
        """状态 / 延迟：返回 (状态文本, 延迟文本, 状态色, 延迟色, 状态提示, 延迟提示)。

        ``colors`` 由调用方注入（每次重建只求一次）；省略时回退到自行查询。
        """
        if colors is None:
            colors = self._table_colors()
        res = self.test_results.get(m.key)
        muted = QColor(colors.text_muted)
        if not res:
            return "—", "—", muted, muted, "", ""
        if res.get("pending"):
            return "…", "…", QColor(colors.warning), QColor(colors.warning), "", ""
        if res.get("available") is True:
            status_text, status_color, status_tip = "✓", QColor(colors.success), "可用"
        elif res.get("available") is False:
            status_text, status_color = "✗", QColor(colors.danger)
            status_tip = str(res.get("error") or res.get("preview") or "不可用")[:300]
        else:
            status_text, status_color, status_tip = "?", muted, ""
        lat = res.get("latency_ms")
        if isinstance(lat, (int, float)):
            latency_text = f"{lat:.0f}ms"
            if lat < LATENCY_OK_MS:
                latency_color = QColor(colors.success)
            elif lat < LATENCY_WARN_MS:
                latency_color = QColor(colors.warning)
            else:
                latency_color = QColor(colors.danger)
        else:
            latency_text, latency_color = "—", muted
        return status_text, latency_text, status_color, latency_color, status_tip, ""

    def _model_item_key(self, item) -> tuple[str, str] | None:
        """从树节点读取 (provider, model)；组节点（model 为空）返回 None。"""
        if item is None:
            return None
        data = item.data(0, Qt.UserRole)
        if isinstance(data, (list, tuple)) and len(data) == 2:
            provider = str(data[0] or "").strip()
            model = str(data[1] or "").strip()
            if provider and model:
                return provider, model
        return None

    def _adopt_worker(self, worker: Worker) -> None:
        # MainWindow 是顶层窗口：不设 parent，关闭即应用退出。
        pass

    def selected_model_row(self) -> core.ModelInfo | None:
        item = self.models_table.currentItem()
        parsed = self._model_item_key(item)
        if not parsed:
            return None
        provider, model = parsed
        for m in self.models:
            if m.provider == provider and m.model == model:
                return m
        return core.ModelInfo(provider, model)

    def selected_model_rows(self) -> list[core.ModelInfo]:
        out: list[core.ModelInfo] = []
        seen: set[str] = set()
        for item in self.models_table.selectedItems():
            parsed = self._model_item_key(item)
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
            parsed = core.parse_favorite_key(key)
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
        tree = self.models_table
        for i in range(tree.topLevelItemCount()):
            group = tree.topLevelItem(i)
            if group is None:
                continue
            for j in range(group.childCount()):
                group.child(j).setSelected(True)

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
        parsed = core.parse_favorite_key(item.text())
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
                timeout=BATCH_TEST_TIMEOUT_PI if mode == "pi" else BATCH_TEST_TIMEOUT_DIRECT,
                kind="model",
            )
        )
        w.progress.connect(self._on_model_test_progress, Qt.QueuedConnection)
        w.done.connect(self._on_model_tests_done, Qt.QueuedConnection)
        w.failed.connect(self._on_model_tests_fail, Qt.QueuedConnection)
        self._test_worker = w
        self._set_test_cancel_enabled(True)
        w.start()

    def _set_test_cancel_enabled(self, enabled: bool) -> None:
        button = getattr(self, "model_test_cancel_btn", None)
        if button is not None:
            button.setEnabled(bool(enabled))

    def model_test_cancel(self) -> None:
        """停止批量测试。

        ``BatchTestWorker`` 一直支持 ``isInterruptionRequested``，但此前 UI 上
        没有任何入口 —— 已实现的取消能力没人能用到。
        """
        worker = getattr(self, "_test_worker", None)
        if worker is None or not worker.isRunning():
            self._set_test_cancel_enabled(False)
            return
        worker.requestInterruption()
        self._set_test_cancel_enabled(False)
        self.status.showMessage("已请求停止测试，正在收尾已发起的请求…")
        if hasattr(self, "test_status"):
            self.test_status.setText("已请求停止测试；未开始的项不再执行。")

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
        # 增量刷新命中行；未命中才回退整树重建（见 update_model_row_status）
        if not self.update_model_row_status(str(r.get("provider") or ""), str(r.get("model") or "")):
            self.fill_models_table()
        try:
            self.history_refresh()
        except Exception as e:
            logger.warning("history refresh during model test failed: %s", e)
        self.status.showMessage(f"测试中 {done}/{total} · 可用 {ok_n} · 刚完成 {key}")
        if hasattr(self, "test_status"):
            recent = " | ".join(self._test_lines[-4:])
            self.test_status.setText(f"进度 {done}/{total}（可用 {ok_n}） · {recent}")

    def _on_model_tests_done(self, results: list):
        self._test_running = False
        self._set_test_cancel_enabled(False)
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
        self.fill_models_table()
        try:
            self.history_refresh()
        except Exception as e:
            logger.warning("history refresh after model tests failed: %s", e)
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
        self._set_test_cancel_enabled(False)
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
        except Exception as e:
            # 以前静默：表格保持旧数据，用户会以为看到的是最新结果。
            logger.warning("health table refresh failed: %s", e)
        try:
            self.history_refresh()
        except Exception as e:
            logger.warning("history refresh failed: %s", e)
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

        # 按 Provider 分组收集符合条件的模型（用户手动添加的模型均正常展示）
        groups: dict[str, list[core.ModelInfo]] = {}
        for m in self.models:
            if prov and m.provider != prov:
                continue
            if only_fav and m.key not in fav_set:
                continue
            if q and q not in m.key.lower() and q not in m.provider.lower() and q not in m.model.lower():
                continue
            groups.setdefault(m.provider, []).append(m)

        # 组内排序：默认模型置顶 → 收藏 → 模型名
        def _sort_key(m: core.ModelInfo) -> tuple:
            is_def = 0 if m.key == default_key else 1
            is_fav = 0 if m.key in fav_set else 1
            return (is_def, is_fav, m.model.lower())

        for provider_name in groups:
            groups[provider_name].sort(key=_sort_key)

        # 组排序：默认 Provider 置顶 → 组内含收藏 → Provider 名
        ordered_providers = sorted(
            groups.keys(),
            key=lambda p: (
                0 if p == def_p else 1,
                0 if any(m.key in fav_set for m in groups[p]) else 1,
                str(p).lower(),
            ),
        )

        # 记忆展开状态：首次构建默认全部展开
        expanded = set(getattr(self, "_models_tree_expanded", None) or ())
        if not expanded and ordered_providers:
            expanded = set(ordered_providers)

        # 每次重建只求一次主题色，逐行注入（详见 _table_colors 注释）。
        colors = self._table_colors()
        tree = self.models_table
        # tree.clear() 会销毁全部 QTreeWidgetItem：选中集合与滚动位置必须自行
        # 保存/恢复，否则批量测试期间每完成一项就把用户的多选清空、列表跳回
        # 顶部，「重测选中 / 设为默认」随即报「请先选择模型」。
        prev_selected = {
            f"{p}/{m}"
            for p, m in (
                self._model_item_key(item) or ("", "")
                for item in tree.selectedItems()
            )
            if p and m
        }
        prev_current = self._model_item_key(tree.currentItem())
        scrollbar = tree.verticalScrollBar()
        prev_scroll = scrollbar.value() if scrollbar is not None else 0
        tree.clear()
        row_index: dict[str, QTreeWidgetItem] = {}
        # Provider 列由树状分组体现，隐藏重复列
        try:
            tree.setColumnHidden(1, True)
        except Exception:
            pass
        for provider_name in ordered_providers:
            models = groups[provider_name]
            is_default_group = provider_name == def_p
            group = QTreeWidgetItem(tree)
            group.setText(0, f"{provider_name}  ({len(models)})")
            group.setText(1, provider_name)
            group.setData(0, Qt.UserRole, [provider_name, ""])
            group.setData(0, Qt.UserRole + 1, "group")
            group.setToolTip(
                0,
                f"Provider：{provider_name}\n{len(models)} 个模型"
                + ("\n当前默认 Provider" if is_default_group else ""),
            )
            group.setForeground(0, QColor(colors.accent_text if is_default_group else colors.text))
            group.setForeground(1, QColor(colors.text_muted))
            font = group.font(0)
            font.setBold(True)
            group.setFont(0, font)
            group.setExpanded(provider_name in expanded)

            for m in models:
                is_default = m.key == default_key
                is_fav = m.key in fav_set
                prefix = ""
                if is_default:
                    prefix += "● "
                if is_fav:
                    prefix += "★ "
                child = QTreeWidgetItem(group)
                child.setText(0, f"{prefix}{m.model}")
                child.setData(0, Qt.UserRole, [m.provider, m.model])
                tip_bits = [m.key]
                if is_default:
                    tip_bits.append("当前默认")
                if is_fav:
                    tip_bits.append("已收藏")
                child.setToolTip(0, " · ".join(tip_bits))
                # 第 0 列显式着色（默认模型用强调色，其余用正文色），使
                # presentation 层不必再整树走一遍 setForeground 覆写。
                child.setForeground(
                    0, QColor(colors.accent_text if is_default else colors.text)
                )
                child.setText(1, m.provider)
                child.setForeground(1, QColor(colors.text_muted))
                cap = self._model_capability_text(m)
                child.setText(2, cap)
                child.setToolTip(
                    2,
                    f"context={m.context or '-'}  thinking={m.thinking or '-'}  images={m.images or '-'}",
                )
                status_text, latency_text, sc, lc, status_tip, _lt = self._model_status_cells(
                    m, colors
                )
                child.setText(3, status_text)
                child.setText(4, latency_text)
                child.setForeground(3, sc)
                child.setForeground(4, lc)
                if status_tip:
                    child.setToolTip(3, status_tip)
                row_index[m.key] = child

        # 增量刷新用的行索引（tree.clear() 后一并重建，天然与树同寿）
        self._model_row_index = row_index
        # 恢复顺序很关键：setCurrentItem 在 ExtendedSelection 下按
        # ClearAndSelect 处理，必须先设 current 再补选中集合。
        # blockSignals 避免 N 次 itemSelectionChanged 风暴；调用方（
        # ModernMainWindow.fill_models_table）随后会显式刷新一次详情面板。
        tree.blockSignals(True)
        try:
            if prev_current:
                restored = row_index.get(f"{prev_current[0]}/{prev_current[1]}")
                if restored is not None:
                    tree.setCurrentItem(restored)
            for key in prev_selected:
                item = row_index.get(key)
                if item is not None:
                    item.setSelected(True)
        finally:
            tree.blockSignals(False)
        if scrollbar is not None and prev_scroll:
            scrollbar.setValue(min(prev_scroll, scrollbar.maximum()))

        if hasattr(self, "models_count_lbl"):
            total = len(self.models)
            shown = sum(len(v) for v in groups.values())
            fav_n = sum(1 for m in self.models if m.key in fav_set)
            extra = f" · 收藏 {fav_n}"
            if only_fav:
                extra += " · 仅收藏"
            if prov:
                extra += f" · {prov}"
            self.models_count_lbl.setText(f"显示 {shown} / 共 {total}{extra}")

    def update_model_row_status(self, provider: str, model: str) -> bool:
        """增量刷新单行的状态/延迟列；命中返回 True，未命中返回 False。

        批量测试 / 健康检查每完成一项原本调 ``fill_models_table()`` 整树重建，
        代价是 O(N²)（N 行 × 每行主题查询）并且抹掉选中与滚动位置。命中索引时
        只改 3/4 两列，未命中（新模型、过滤条件变化）才由调用方回退全量重建。
        """
        index = getattr(self, "_model_row_index", None)
        if not index:
            return False
        item = index.get(f"{provider}/{model}")
        if item is None:
            return False
        info = None
        for m in self.models:
            if m.provider == provider and m.model == model:
                info = m
                break
        if info is None:
            info = core.ModelInfo(provider, model)
        try:
            status_text, latency_text, sc, lc, status_tip, _ = self._model_status_cells(
                info, self._table_colors()
            )
            item.setText(3, status_text)
            item.setText(4, latency_text)
            item.setForeground(3, sc)
            item.setForeground(4, lc)
            item.setToolTip(3, status_tip or "")
        except RuntimeError:
            # 树已在别处重建，索引失效：让调用方走全量重建
            self._model_row_index = {}
            return False
        return True

    def _remember_tree_expanded(self, item, expanded: bool) -> None:
        """记录用户手动展开 / 收起的 Provider 分组，刷新时保持状态。"""
        if item is None:
            return
        data = item.data(0, Qt.UserRole)
        if not isinstance(data, (list, tuple)) or not data:
            return
        provider = str(data[0] or "")
        if not provider:
            return
        state = set(getattr(self, "_models_tree_expanded", None) or ())
        if expanded:
            state.add(provider)
        else:
            state.discard(provider)
        self._models_tree_expanded = state

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
        # 唯一的磁盘遍历入口：会话目录遍历 + 会话文件解析只在这里发生，
        # 结果缓存供 sessions_apply_filter 做内存过滤（见其 docstring）。
        self._sessions_cache = list(core.list_sessions(limit=200))
        if hasattr(self, "session_filter_wd"):
            self.sessions_apply_filter()
            return
        self._fill_sessions_table(self._sessions_cache)

    def _filter_sessions_cache(
        self, workdir_substr: str = "", name_substr: str = "", *, limit: int = 100
    ) -> list[dict[str, str]]:
        """对 refresh_sessions() 的缓存做内存过滤（语义同 extras.list_sessions_filtered）。"""
        rows = getattr(self, "_sessions_cache", None)
        if rows is None:
            rows = self._sessions_cache = list(core.list_sessions(limit=200))
        wd = (workdir_substr or "").lower().strip()
        nm = (name_substr or "").lower().strip()
        out: list[dict[str, str]] = []
        for r in rows:
            if wd or nm:
                blob = " ".join(
                    str(r.get(k) or "")
                    for k in ("path", "folder", "name", "cwd", "project", "model", "preview")
                ).lower()
                if wd and wd not in blob:
                    continue
                if nm and nm not in blob:
                    continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

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
            project = r.get("project") or core.project_name_from_path(r.get("cwd") or r.get("folder") or "")
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
        parsed = core.parse_favorite_key(key)
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
        except Exception as e:
            logger.warning("reload manager config after provider delete failed: %s", e)
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

        # 决定要选中的 Provider：当前选择（仍存在）→ 默认 Provider（存在）→ 第一个
        try:
            dp, _, _ = core.get_default_model()
        except Exception:
            dp = ""
        target = ""
        if cur_p and cur_p in providers:
            target = cur_p
        elif dp and dp in providers:
            target = dp
        elif providers:
            target = providers[0]
        self._set_chat_combo_text(self.chat_provider, target)

        self._reload_chat_models_for_provider(self._chat_combo_text(self.chat_provider), prefer_model=cur_m)

    def _on_chat_provider_changed(self, _text: str = "") -> None:
        if not hasattr(self, "chat_model") or not isinstance(self.chat_model, QComboBox):
            return
        prefer = self._chat_combo_text(self.chat_model)
        self._reload_chat_models_for_provider(self._chat_combo_text(self.chat_provider), prefer_model=prefer)

    def _reload_chat_models_for_provider(self, provider: str, prefer_model: str = "") -> None:
        """填充快速提问的模型下拉：list-models 结果 + models.json 手动添加的模型。"""
        if not hasattr(self, "chat_model") or not isinstance(self.chat_model, QComboBox):
            return
        provider = (provider or "").strip()
        models: list[str] = []
        seen: set[str] = set()
        # 1) pi --list-models 枚举到的模型
        for m in self.models or []:
            if not provider or m.provider == provider:
                if m.model and m.model not in seen:
                    seen.add(m.model)
                    models.append(m.model)
        # 2) models.json 中该 provider 手动添加的模型（始终合并，保证可选择）
        if provider:
            try:
                cfg = core.load_models_config()
                pdata = (cfg.get("providers") or {}).get(provider) or {}
                for item in pdata.get("models") or []:
                    mid = ""
                    if isinstance(item, dict):
                        mid = str(item.get("id") or item.get("model") or "")
                    elif isinstance(item, str):
                        mid = item
                    if mid and mid not in seen:
                        seen.add(mid)
                        models.append(mid)
            except Exception:
                pass

        self.chat_model.blockSignals(True)
        self.chat_model.clear()
        for mid in models:
            self.chat_model.addItem(mid)
        self.chat_model.blockSignals(False)

        if prefer_model and prefer_model in models:
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
            # 无可用模型：清空，不残留不存在的模型文本
            self.chat_model.setCurrentIndex(-1)
            self.chat_model.setEditText("")
            self.chat_model.setPlaceholderText("该 Provider 暂无可用模型")

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

        def job():
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
            )
            if attachments:
                result["vision_text"] = description or ""
            return result

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
        self.chat_input.setEnabled(True)
        self.chat_output.appendPlainText(f"[错误] {e}")
        self.status.showMessage("快速提问失败")

    def session_reveal(self):
        sm = self.sessions_table.selectionModel()
        if not sm:
            return
        rows = sm.selectedRows()
        if not rows:
            return
        path = self._session_path_at(rows[0].row())
        if path:
            core.open_in_explorer(path)

    def session_open_project(self):
        sm = self.sessions_table.selectionModel()
        if not sm:
            return
        rows = sm.selectedRows()
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
        sm = self.sessions_table.selectionModel()
        if not sm:
            return
        rows = sm.selectedRows()
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
        except Exception as e:
            logger.warning("startup language/theme bootstrap failed: %s", e)
        # Ensure the Pi vision skill is installed (idempotent; regenerates the
        # helper command if this installation moved).
        try:
            core.install_vision_skill()
        except Exception as e:
            logger.warning("install vision skill failed: %s", e)
        # first-run wizard
        if not core.is_setup_done():
            self.open_setup_wizard(force=True)
        # update check：官方 Pi CLI + Pi Manager 自身
        cfg = core.load_manager_config()
        if cfg.get("auto_check_update", True):
            w = self._track(Worker(core.check_pi_status))
            w.done.connect(self._on_update_status)
            w.failed.connect(lambda e: self.status.showMessage(f"检查 Pi 更新失败: {e}"))
            w.start()
            # Manager 自身：静默检查，有新版本再弹窗
            try:
                self.check_manager_update(silent=True)
            except Exception as e:
                # silent 只是「无更新时不打扰用户」，不该连失败都无痕。
                logger.warning("startup manager update check failed: %s", e)

    def _on_update_status(self, st: dict):
        self._pi_update_status = dict(st or {})
        self.status.showMessage(st.get("message") or "")
        try:
            self._refresh_update_indicators()
        except Exception as e:
            logger.warning("refresh update indicators failed: %s", e)
        if st.get("blocked") or st.get("check_failed"):
            return
        needs_action = st.get("missing") or st.get("outdated") or st.get("repair_required")
        if needs_action and st.get("installable"):
            if core.is_update_dismissed("pi", str(st.get("latest") or "")):
                return
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
        w = self._track(Worker(core.check_pi_status))
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
        th = str(s.get("defaultThinkingLevel") or core.DEFAULT_THINKING_LEVEL)
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

        if hasattr(self, "zhipu_key_edit"):
            self.zhipu_key_edit.setText(core.zhipu_api_key())
        if hasattr(self, "vision_model_combo"):
            chosen = core.vision_model_choice()
            index = self.vision_model_combo.findData(chosen)
            if index >= 0:
                self.vision_model_combo.setCurrentIndex(index)
            else:
                self.vision_model_combo.setCurrentIndex(0)
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
        self.settings_raw.setPlainText(
            json.dumps(core.redact_sensitive_config(s), ensure_ascii=False, indent=2)
        )
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
        # 先把界面上的值全部取出来，再在锁内一次性套用：updater 可能被
        # _update_config 重试调用，闭包里不能再去读 widget（那是主线程状态，
        # 且重试期间用户可能已经改动）。
        new_provider = self.set_provider.text().strip()
        new_model = self.set_model.text().strip()
        new_thinking = self.set_thinking.currentText()
        # Keep the model-page Thinking dropdown in sync with the global default
        # so chat/test/launch use the configured level instead of a stale value.
        if hasattr(self, "thinking_combo"):
            saved_thinking = self.set_thinking.currentText()
            current_index = self.thinking_combo.findText(saved_thinking)
            if current_index >= 0:
                self.thinking_combo.setCurrentIndex(current_index)
        new_theme = core.cli_theme_for_ui_mode(mode)
        if hasattr(self, "set_language"):
            core.set_language(self.set_language.currentData() or "zh-CN")
        lines = [x.strip() for x in self.set_enabled.toPlainText().splitlines() if x.strip()]

        def _apply_settings(settings: dict) -> dict:
            settings["defaultProvider"] = new_provider
            settings["defaultModel"] = new_model
            settings["defaultThinkingLevel"] = new_thinking
            settings["theme"] = new_theme
            if lines:
                settings["enabledModels"] = lines
            elif "enabledModels" in settings:
                del settings["enabledModels"]
            return settings

        # 持锁读改写：settings.json 同时被 Pi CLI 与本应用读写，裸的
        # load → 改 → save 会丢掉并发写入（审查 P1-2）。
        core.update_settings(_apply_settings)
        if hasattr(self, "zhipu_key_edit"):
            core.set_zhipu_api_key(self.zhipu_key_edit.text())
        if hasattr(self, "vision_model_combo"):
            core.set_vision_model_choice(self.vision_model_combo.currentData() or "")
        self.save_feature_settings_fields()
        self.apply_ui_theme(mode, accent)
        final_settings = core.load_settings()
        self.settings_raw.setPlainText(
            json.dumps(core.redact_sensitive_config(final_settings), ensure_ascii=False, indent=2)
        )
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
        # 关闭原生文件对话框：Windows 上的 IFileDialog 在部分带 shell 扩展
        # （云盘/杀软右键菜单注入）的机器上会在 QFileDialog 返回后崩溃整个
        # 进程，且崩在 Qt 之外无法捕获。代价是失去「最近位置」「快速访问」侧栏
        # 与系统搜索；若将来能确认目标平台无此问题，可按平台放开此开关。
        QApplication.setAttribute(Qt.AA_DontUseNativeDialogs, True)
    except Exception as e:
        logger.warning("disable native dialogs failed: %s", e)
    app = QApplication(sys.argv)
    app.setApplicationName("Pi Manager")
    app.setOrganizationName("PiManager")
    app.setQuitOnLastWindowClosed(False)
    # 单实例保护：只允许一个桌面实例；后启动的实例向已运行实例发送唤醒消息
    # 后退出，避免多实例并发写回 models.json / settings.json 造成数据冲突。
    # PI_MANAGER_DISABLE_SINGLE_INSTANCE=1 可跳过（供测试与嵌入场景使用）。
    server = None
    if os.environ.get("PI_MANAGER_DISABLE_SINGLE_INSTANCE") != "1":
        from PySide6.QtNetwork import QLocalServer, QLocalSocket

        core.ensure_agent_dir()
        server = QLocalServer(app)
        server.setSocketOptions(QLocalServer.UserAccessOption)
        if not server.listen(SINGLE_INSTANCE_SERVER_NAME):
            socket = QLocalSocket()
            socket.connectToServer(SINGLE_INSTANCE_SERVER_NAME)
            if socket.waitForConnected(500):
                socket.write(b"wake")
                socket.flush()
                socket.waitForBytesWritten(500)
                socket.close()
                return 0
            socket.close()
            # listen() 失败且无法连接到已有实例：上一个实例崩溃后遗留了
            # 残旧的 Unix-domain socket 文件（Linux/macOS）。删除它后再重试，
            # 否则用户必须手动删除该 socket 才能再次启动。removeServer 在
            # Windows 的命名管道上是 no-op，故跨平台安全。
            QLocalServer.removeServer(SINGLE_INSTANCE_SERVER_NAME)
            if not server.listen(SINGLE_INSTANCE_SERVER_NAME):
                # 既连不上已有实例，也拿不到监听名：不能静默 return 0（用户双击
                # 图标毫无反应且无提示）。放弃单实例保护继续启动，并留下日志。
                logger.warning(
                    "single-instance server '%s' unavailable (%s); "
                    "starting without single-instance protection",
                    SINGLE_INSTANCE_SERVER_NAME,
                    server.errorString(),
                )
                server = None
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
    if server is not None:

        def _wake_primary():
            drain_pending_connections(server)
            win.showNormal()
            win.raise_()
            win.activateWindow()

        server.newConnection.connect(_wake_primary)
    return app.exec()
