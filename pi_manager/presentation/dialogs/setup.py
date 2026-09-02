"""首次配置向导与 Pi CLI 安装/升级对话框。

从 ``ui.py`` 下沉。``pi_manager.ui`` 继续 re-export，保持现有测试导入稳定。
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ... import core
from ..design import ACCENT_LABELS
from ..geometry import clamp_dialog_to_screen
from ..workers import Worker, WorkerTrackerMixin

logger = logging.getLogger(__name__)


class InstallPiDialog(WorkerTrackerMixin, QDialog):
    """Install or upgrade the Node-compatible official Pi npm channel."""

    def __init__(self, parent=None, status: dict | None = None):
        super().__init__(parent)
        self.setWindowTitle("\u5b89\u88c5 / \u5347\u7ea7 Pi")
        clamp_dialog_to_screen(self, 620, 460)
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


class SetupWizardDialog(WorkerTrackerMixin, QDialog):
    """首次运行向导：每一步只解决一个问题，允许跳过。"""

    STEPS = ("工作目录", "配置 Provider", "验证 API Key", "选择默认模型", "完成")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pi Manager 配置向导")
        clamp_dialog_to_screen(self, 620, 520)
        self._init_workers()
        self._models: list[tuple[str, str]] = []
        self._verified = False
        self._pending_verify: dict[str, str] = {}
        layout = QVBoxLayout(self)
        self.step_index = QLabel("第 1 步 / 共 5 步")
        self.step_index.setObjectName("wizardStepIndex")
        layout.addWidget(self.step_index)
        title = QLabel("欢迎使用 Pi Manager")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.sub = QLabel("每一步只做一件事。可以跳过，稍后在对应页面继续完成。")
        self.sub.setObjectName("subtitle")
        self.sub.setWordWrap(True)
        layout.addWidget(self.sub)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_workdir_step())
        self.stack.addWidget(self._build_provider_step())
        self.stack.addWidget(self._build_verify_step())
        self.stack.addWidget(self._build_model_step())
        self.stack.addWidget(self._build_finish_step())
        layout.addWidget(self.stack, 1)

        nav = QHBoxLayout()
        self.btn_back = QPushButton("上一步")
        self.btn_back.setProperty("secondary", True)
        self.btn_back.clicked.connect(self._back)
        self.btn_skip = QPushButton("跳过")
        self.btn_skip.setProperty("ghost", True)
        self.btn_skip.clicked.connect(self._skip)
        self.btn_next = QPushButton("下一步")
        self.btn_next.setProperty("success", True)
        self.btn_next.setDefault(True)
        self.btn_next.clicked.connect(self._next)
        nav.addWidget(self.btn_back)
        nav.addStretch(1)
        nav.addWidget(self.btn_skip)
        nav.addWidget(self.btn_next)
        layout.addLayout(nav)
        self._show_step(0)

    def _build_workdir_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("选择 Pi 启动时使用的工作目录。"))
        row = QHBoxLayout()
        self.workdir = QLineEdit(str(core.user_home()))
        self.workdir.setPlaceholderText("项目目录")
        browse = QPushButton("浏览")
        browse.setProperty("secondary", True)
        browse.clicked.connect(self._browse_workdir)
        row.addWidget(self.workdir, 1)
        row.addWidget(browse)
        layout.addLayout(row)
        hint = QLabel("可随时在概览页更改；拖入文件夹也会更新这里。")
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return page

    def _build_provider_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("填写兼容 API 的地址和密钥。可以跳过，稍后在 Provider 页完成。"))
        form = QFormLayout()
        self.quick_name = QLineEdit("custom")
        self.quick_base = QLineEdit("https://api.openai.com/v1")
        self.quick_key = QLineEdit()
        self.quick_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
        self.quick_key.setPlaceholderText("sk-… 不会写入 models.json")
        self.quick_api = QComboBox()
        self.quick_api.addItems(
            [
                "openai-completions",
                "openai-responses",
                "anthropic-messages",
                "google-generative-ai",
            ]
        )
        form.addRow("名称", self.quick_name)
        form.addRow("Base URL", self.quick_base)
        form.addRow("API Key", self.quick_key)
        form.addRow("API 类型", self.quick_api)
        layout.addLayout(form)
        layout.addStretch(1)
        return page

    def _build_verify_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("用刚才的密钥拉取模型列表，确认连接可用。"))
        self.verify_status = QLabel("尚未验证，可跳过。")
        self.verify_status.setObjectName("subtitle")
        self.verify_status.setWordWrap(True)
        layout.addWidget(self.verify_status)
        self.btn_verify = QPushButton("验证 API Key")
        self.btn_verify.setProperty("success", True)
        self.btn_verify.setToolTip("用当前 Base URL 与 API Key 拉取模型列表")
        self.btn_verify.clicked.connect(self._verify_key)
        layout.addWidget(self.btn_verify)
        layout.addStretch(1)
        return page

    def _build_model_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("选择启动 Pi 时使用的默认模型。"))
        self.default_model = QComboBox()
        self.default_model.setEditable(True)
        layout.addWidget(self.default_model)
        hint = QLabel("列表来自刚才验证到的模型，或已保存在 models.json 中的配置。")
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        layout.addStretch(1)
        return page

    def _build_finish_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("外观与安全偏好。保存后即可开始使用。"))
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
        layout.addStretch(1)
        return page

    def _browse_workdir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(self, "选择工作目录", self.workdir.text())
        if chosen:
            self.workdir.setText(chosen)

    def _show_step(self, index: int) -> None:
        index = max(0, min(index, self.stack.count() - 1))
        self.stack.setCurrentIndex(index)
        total = self.stack.count()
        name = self.STEPS[index]
        done = "、".join(self.STEPS[:index]) if index else "无"
        self.step_index.setText(f"第 {index + 1} 步 / 共 {total} 步 · {name}")
        self.sub.setText(f"已完成：{done}。当前步骤可以跳过。" if index < total - 1 else "保存后即可开始使用。")
        self.btn_back.setEnabled(index > 0)
        self.btn_skip.setVisible(index < total - 1)
        self.btn_next.setText("保存并开始" if index == total - 1 else "下一步")
        if index == 3:
            self._fill_model_choices()

    def _back(self) -> None:
        self._show_step(self.stack.currentIndex() - 1)

    def _skip(self) -> None:
        current = self.stack.currentIndex()
        if current == 1:
            self._show_step(4)
            return
        self._show_step(current + 1)

    def _next(self) -> None:
        current = self.stack.currentIndex()
        if current == 0:
            path = self.workdir.text().strip()
            if path:

                def _apply(cfg: dict) -> dict:
                    cfg["last_workdir"] = path
                    return cfg

                core.update_manager_config(_apply)
            self._show_step(1)
            return
        if current == 1:
            if self.quick_name.text().strip() and self.quick_base.text().strip():
                self._show_step(2)
            else:
                self._show_step(4)
            return
        if current == 2:
            self._show_step(3)
            return
        if current == 3:
            self._apply_default_model()
            self._show_step(4)
            return
        self._save()

    def closeEvent(self, event):
        stuck = self._reap_workers(budget=2.5)
        if stuck:
            self._note_detached_workers()
        super().closeEvent(event)

    def _set_verify_busy(self, busy: bool) -> None:
        self.btn_verify.setEnabled(not busy)
        self.btn_verify.setText("正在验证…" if busy else "验证 API Key")

    def _verify_key(self) -> None:
        name = self.quick_name.text().strip() or "custom"
        base = self.quick_base.text().strip()
        key = self.quick_key.text().strip()
        api = self.quick_api.currentText()
        if not base:
            self.verify_status.setText("请先填写 Base URL。")
            return
        self._pending_verify = {"name": name, "base": base, "key": key, "api": api}
        self.verify_status.setText("正在验证 API Key…")
        self._set_verify_busy(True)
        worker = self._track(Worker(core.fetch_remote_models, base, key, api=api))
        worker.done.connect(self._on_verify_done)
        worker.failed.connect(self._on_verify_fail)
        worker.start()

    def _on_verify_fail(self, err: str) -> None:
        self._set_verify_busy(False)
        self._verified = False
        self.verify_status.setText(f"验证失败：{err}")

    def _on_verify_done(self, result) -> None:
        self._set_verify_busy(False)
        pending = dict(self._pending_verify or {})
        if not isinstance(result, dict) or not result.get("ok"):
            self._verified = False
            error = ""
            if isinstance(result, dict):
                error = str(result.get("error") or "")
            self.verify_status.setText(f"验证失败：{error or '未知错误'}")
            return
        name = pending.get("name") or "custom"
        models = result.get("models") or []
        self._models = [
            (name, str(item.get("id") or item.get("name") or ""))
            for item in models
            if isinstance(item, dict)
        ]
        try:
            core.upsert_custom_provider(
                name,
                base_url=pending.get("base") or "",
                api=pending.get("api") or "openai-completions",
                api_key=pending.get("key") or "",
                models=models,
                compat={"supportsDeveloperRole": False, "supportsReasoningEffort": True},
            )
        except Exception as exc:
            self.verify_status.setText(f"密钥可用，但保存失败：{exc}")
            return
        self._verified = True
        self.verify_status.setText(f"验证成功，已保存「{name}」· {len(models)} 个模型。")
        self._fill_model_choices()

    def _fill_model_choices(self) -> None:
        self.default_model.clear()
        seen: set[str] = set()
        for provider, model in self._models:
            key = f"{provider}/{model}"
            if model and key not in seen:
                self.default_model.addItem(key, (provider, model))
                seen.add(key)
        try:
            cfg = core.load_models_config()
        except Exception:
            cfg = {}
        for name, entry in (cfg.get("providers") or {}).items():
            if not isinstance(entry, dict):
                continue
            for item in entry.get("models") or []:
                mid = item.get("id") if isinstance(item, dict) else str(item)
                key = f"{name}/{mid}"
                if mid and key not in seen:
                    self.default_model.addItem(key, (name, str(mid)))
                    seen.add(key)

    def _apply_default_model(self) -> None:
        data = self.default_model.currentData()
        if isinstance(data, (tuple, list)) and len(data) == 2:
            core.set_default_model(str(data[0]), str(data[1]))

    def _save(self):
        core.set_language(self.lang.currentData() or "zh-CN")
        core.apply_language_preference(self.lang.currentData() or "zh-CN")
        core.set_ui_theme(
            mode=self.ui_mode.currentData() or "night",
            accent=self.ui_accent.currentData() or "blue",
        )
        secure_keys = self.secure.isChecked()
        auto_check = self.auto_update.isChecked()
        workdir = self.workdir.text().strip()

        def _apply_setup(cfg: dict) -> dict:
            cfg["secure_keys"] = secure_keys
            cfg["auto_check_update"] = auto_check
            if workdir:
                cfg["last_workdir"] = workdir
            return cfg

        core.update_manager_config(_apply_setup)
        core.mark_setup_done(True)
        try:
            core.run_first_time_bootstrap()
        except Exception as e:
            logger.warning("first-run bootstrap failed: %s", e)
        self.accept()
