"""首次配置向导与 Pi CLI 安装/升级对话框。

从 ``ui.py`` 下沉。``pi_manager.ui`` 继续 re-export，保持现有测试导入稳定。
"""
from __future__ import annotations

import logging

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ... import core
from ..design import ACCENT_LABELS
from ..workers import Worker, WorkerTrackerMixin

logger = logging.getLogger(__name__)


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
