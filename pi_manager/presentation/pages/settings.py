"""Modern settings page with advanced groups folded by default."""
from __future__ import annotations

import json
import logging

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ... import core
from ... import extras
from ..design import ACCENT_LABELS
from ..components import CollapsibleSection, SectionHeading, StatusBadge, SurfaceCard

logger = logging.getLogger(__name__)


def _form() -> QFormLayout:
    form = QFormLayout()
    form.setSpacing(10)
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
    return form


def build_settings_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(26, 22, 26, 26)
    layout.setSpacing(12)

    intro = SurfaceCard(elevated=True, margins=(16, 14, 16, 14), spacing=8)
    intro_row = QHBoxLayout()
    intro_row.addWidget(SectionHeading("偏好设置", "常用配置保持展开，高级网络、故障切换和系统行为默认折叠。"), 1)
    window.settings_dirty_badge = StatusBadge("已保存", "success")
    window.settings_saved_label = QLabel("")
    window.settings_saved_label.setObjectName("subtitle")
    intro_row.addWidget(window.settings_saved_label, 0, Qt.AlignTop)
    intro_row.addWidget(window.settings_dirty_badge, 0, Qt.AlignTop)
    intro.content.addLayout(intro_row)
    layout.addWidget(intro)

    model_section = CollapsibleSection(
        "默认模型与回复",
        "设置启动时使用的模型、Thinking 级别、启用列表和默认回复语言。",
        expanded=True,
    )
    model_form = _form()
    window.set_provider = QLineEdit()
    window.set_provider.setPlaceholderText("例如 openai-codex")
    window.set_model = QLineEdit()
    window.set_model.setPlaceholderText("例如 gpt-5.4")
    window.set_thinking = QComboBox()
    window.set_thinking.addItems(["off", "minimal", "low", "medium", "high", "xhigh", "max"])
    window.set_enabled = QPlainTextEdit()
    window.set_enabled.setPlaceholderText("每行一个 provider/model")
    window.set_enabled.setFixedHeight(84)
    window.set_language = QComboBox()
    window.set_language.addItem("简体中文（优先）", "zh-CN")
    window.set_language.addItem("English", "en")
    window.set_language.addItem("不附加语言偏好", "auto")
    model_form.addRow("默认 Provider", window.set_provider)
    model_form.addRow("默认模型", window.set_model)
    model_form.addRow("Thinking 级别", window.set_thinking)
    model_form.addRow("启用模型列表", window.set_enabled)
    model_form.addRow("默认回复语言", window.set_language)
    model_section.body_layout.addLayout(model_form)
    layout.addWidget(model_section)

    appearance = CollapsibleSection(
        "\u5168\u5c40\u4e3b\u9898\u4e0e\u663e\u793a",
        "\u663c\u591c\u6a21\u5f0f\u5c06\u540c\u65f6\u5e94\u7528\u5230\u7ba1\u7406\u5668\u3001\u6240\u6709\u5f39\u7a97\u3001\u5e2e\u52a9\u9875\u4e0e Pi CLI\uff0c\u4e0d\u518d\u5206\u5f00\u8bbe\u7f6e\u3002",
        expanded=True,
    )
    appearance_form = _form()
    window.set_ui_mode = QComboBox()
    window.set_ui_mode.addItem("\u591c\u95f4\u6a21\u5f0f\uff08\u5168\u5c40\uff09", "night")
    window.set_ui_mode.addItem("\u767d\u5929\u6a21\u5f0f\uff08\u5168\u5c40\uff09", "day")
    window.set_ui_accent = QComboBox()
    for key, label in ACCENT_LABELS.items():
        window.set_ui_accent.addItem(label, key)
    appearance_form.addRow("\u5168\u5c40\u663c\u591c\u6a21\u5f0f", window.set_ui_mode)
    appearance_form.addRow("\u5168\u5c40\u4e3b\u9898\u8272", window.set_ui_accent)
    appearance.body_layout.addLayout(appearance_form)
    theme_actions = QHBoxLayout()
    theme_actions.setSpacing(8)
    theme_actions.addWidget(window._btn("\u5e94\u7528\u5168\u5c40\u4e3b\u9898", window.apply_ui_theme_from_settings, success=True))
    theme_actions.addWidget(window._btn("\u5207\u6362\u663c\u591c", window.toggle_ui_mode, secondary=True))
    theme_actions.addStretch(1)
    appearance.body_layout.addLayout(theme_actions)
    layout.addWidget(appearance)

    reliability = CollapsibleSection(
        "网络与故障切换",
        "代理会影响模型拉取、连接测试与 Pi 子进程；故障切换仅作用于快速提问。",
        expanded=False,
    )
    reliability_form = _form()
    window.proxy_enabled = QCheckBox("启用全局代理")
    window.proxy_url = QLineEdit()
    window.proxy_url.setPlaceholderText("http://127.0.0.1:7890")
    window.test_concurrency = QSpinBox()
    window.test_concurrency.setRange(1, 8)
    window.test_concurrency.setValue(3)
    window.failover_enabled = QCheckBox("快速提问失败时自动切换模型")
    window.failover_enabled.setChecked(True)
    window.failover_threshold = QSpinBox()
    window.failover_threshold.setRange(1, 10)
    window.failover_threshold.setValue(3)
    window.failover_silent = QCheckBox("无感切换，仅在状态栏提示")
    window.failover_silent.setChecked(True)
    window.chat_persistent_session = QCheckBox(
        "快速提问使用常驻会话（多轮上下文 + 会话内热切模型）"
    )
    window.chat_persistent_session.setChecked(True)
    reliability_form.addRow("全局代理", window.proxy_enabled)
    reliability_form.addRow("代理地址", window.proxy_url)
    reliability_form.addRow("批量测试并发", window.test_concurrency)
    reliability_form.addRow("故障切换", window.failover_enabled)
    reliability_form.addRow("连续失败阈值", window.failover_threshold)
    reliability_form.addRow("", window.failover_silent)
    reliability_form.addRow("", window.chat_persistent_session)
    reliability.body_layout.addLayout(reliability_form)
    layout.addWidget(reliability)

    vision = CollapsibleSection(
        "识图模型",
        "快速提问中粘贴/拖入图片时，自动用内置免费识图模型 GLM-4.6V-Flash（智谱 AI）识别，再转成文本交给当前对话模型。",
        expanded=False,
    )
    vision_form = _form()
    window.zhipu_key_edit = QLineEdit()
    window.zhipu_key_edit.setEchoMode(QLineEdit.Password)
    window.zhipu_key_edit.setPlaceholderText("智谱 API Key（免费申请：https://bigmodel.cn）")
    window.vision_model_combo = QComboBox()
    window.vision_model_combo.addItem("自动（GLM-4.6V-Flash 优先，限流时切换 4.1V-Thinking）", "")
    window.vision_model_combo.addItem("GLM-4.6V-Flash", "glm-4.6v-flash")
    window.vision_model_combo.addItem("GLM-4.1V-Thinking-Flash（免费 · 深度思考）", "glm-4.1v-thinking-flash")
    vision_form.addRow("智谱 API Key", window.zhipu_key_edit)
    vision_form.addRow("识图模型", window.vision_model_combo)
    vision_hint = QLabel(
        "内置两个免费视觉模型：GLM-4.6V-Flash（快速）与 GLM-4.1V-Thinking-Flash（深度思考，免费版）。\n"
        "识图模型默认用于识图管道，不自动出现在模型列表中：粘贴/拖入图片时由 Pi skill 自动调用识图转文字，\n"
        "再把结果交给当前默认对话模型回答；如要在模型列表中使用智谱模型，请在 Provider 管理中手动添加。\n"
        "「自动」模式在 4.6V-Flash 限流（429）时自动切换到 4.1V-Thinking 重试；Key 加密保存在本机密钥库。"
    )
    vision_hint.setObjectName("subtitle")
    vision_hint.setWordWrap(True)
    vision_form.addRow("", vision_hint)
    vision.body_layout.addLayout(vision_form)
    vision_actions = QHBoxLayout()
    vision_actions.setSpacing(8)
    vision_actions.addWidget(
        window._btn("用红色测试图验证识图", window.vision_test_run, success=True)
    )
    vision_actions.addWidget(
        window._btn("检查识图配置", window.vision_check_config, secondary=True)
    )
    vision_actions.addStretch(1)
    vision.body_layout.addLayout(vision_actions)
    window.vision_test_status = QLabel("尚未测试 — 点击按钮将生成一张红色图片并调用识图模型")
    window.vision_test_status.setObjectName("subtitle")
    window.vision_test_status.setWordWrap(True)
    vision.body_layout.addWidget(window.vision_test_status)
    layout.addWidget(vision)

    system = CollapsibleSection(
        "系统行为与安全",
        "控制托盘、启动行为、密钥加密和 Pi CLI 安装维护。",
        expanded=False,
    )
    system_form = _form()
    window.minimize_to_tray = QCheckBox("关闭窗口时最小化到托盘")
    window.minimize_to_tray.setChecked(True)
    window.start_minimized = QCheckBox("启动时最小化到托盘")
    window.secure_keys_chk = QCheckBox("保存 Provider 时加密 API Key")
    window.secure_keys_chk.setChecked(True)
    system_form.addRow("", window.minimize_to_tray)
    system_form.addRow("", window.start_minimized)
    system_form.addRow("", window.secure_keys_chk)
    system.body_layout.addLayout(system_form)
    maintenance = QHBoxLayout()
    maintenance.setSpacing(8)
    maintenance.addWidget(window._btn("检查 Pi 更新", window.check_pi_update, secondary=True))
    maintenance.addWidget(window._btn("安装 / 升级 Pi", window.open_install_dialog, secondary=True))
    maintenance.addWidget(window._btn("检查 Manager 更新", window.check_manager_update, secondary=True))
    maintenance.addWidget(window._btn("打开配置向导", window.open_setup_wizard, ghost=True))
    maintenance.addStretch(1)
    system.body_layout.addLayout(maintenance)
    layout.addWidget(system)

    raw = CollapsibleSection(
        "settings.json 预览",
        "只读展示最终写入 Pi 的配置；敏感密钥不会由此区域编辑。",
        expanded=False,
    )
    window.settings_raw = QPlainTextEdit()
    window.settings_raw.setReadOnly(True)
    window.settings_raw.setObjectName("mono")
    window.settings_raw.setMinimumHeight(140)
    raw.body_layout.addWidget(window.settings_raw)
    layout.addWidget(raw)

    actions = SurfaceCard(elevated=True, margins=(14, 12, 14, 12), spacing=8)
    action_row = QHBoxLayout()
    action_row.setSpacing(8)
    action_row.addWidget(window._btn("保存设置", window.settings_save, success=True))
    action_row.addWidget(window._btn("从文件重新加载", window.settings_load, secondary=True))
    action_row.addWidget(window._btn("打开 settings.json", window.open_settings_json, ghost=True))
    action_row.addStretch(1)
    actions.content.addLayout(action_row)
    layout.addWidget(actions)
    layout.addStretch(1)

    scroll.setWidget(body)
    outer.addWidget(scroll)
    return page


class SettingsPageMixin:
    """设置页行为：settings.json、主题与代理等偏好。从 ``ui.py`` / ``ui_features.py`` 下沉。"""

    def apply_ui_theme_from_settings(self):
        mode = self.set_ui_mode.currentData() if hasattr(self, "set_ui_mode") else "night"
        accent = self.set_ui_accent.currentData() if hasattr(self, "set_ui_accent") else "blue"
        core.set_ui_theme(mode=mode, accent=accent)
        self.apply_ui_theme(mode, accent)

    def settings_load(self):
        self._settings_loading = True
        try:
            self._settings_load_fields()
        finally:
            self._settings_loading = False
            self._bind_settings_dirty()
            self._clear_settings_dirty()

    def _settings_load_fields(self):
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
        self._clear_settings_dirty()
        self.status.showMessage("设置已保存，管理器与 Pi CLI 已同步主题", 5000)
        notify = getattr(self, "notify_success", None)
        if callable(notify):
            notify("设置已保存")

    def load_feature_settings_fields(self):
        mgr = core.load_manager_config()
        self.mgr = mgr
        if hasattr(self, "proxy_enabled"):
            self.proxy_enabled.setChecked(bool(mgr.get("proxy_enabled")))
        if hasattr(self, "proxy_url"):
            self.proxy_url.setText(str(mgr.get("proxy_url") or ""))
        if hasattr(self, "test_concurrency"):
            self.test_concurrency.setValue(int(mgr.get("test_concurrency") or 3))
        if hasattr(self, "failover_enabled"):
            self.failover_enabled.setChecked(bool(mgr.get("failover_enabled", True)))
        if hasattr(self, "failover_threshold"):
            self.failover_threshold.setValue(int(mgr.get("failover_fail_threshold") or 3))
        if hasattr(self, "failover_silent"):
            self.failover_silent.setChecked(bool(mgr.get("failover_silent", True)))
        if hasattr(self, "chat_persistent_session"):
            self.chat_persistent_session.setChecked(bool(mgr.get("chat_persistent_session", True)))
        if hasattr(self, "minimize_to_tray"):
            self.minimize_to_tray.setChecked(bool(mgr.get("minimize_to_tray", True)))
        if hasattr(self, "start_minimized"):
            self.start_minimized.setChecked(bool(mgr.get("start_minimized", False)))
        if hasattr(self, "secure_keys_chk"):
            self.secure_keys_chk.setChecked(bool(mgr.get("secure_keys", True)))
        if hasattr(self, "update_url_edit"):
            self.update_url_edit.setText(str(mgr.get("update_manifest_url") or ""))
        if hasattr(self, "mgr_version_lbl"):
            self.mgr_version_lbl.setText(f"当前版本：{extras.APP_VERSION}")

    def save_feature_settings_fields(self):
        fields = {}
        if hasattr(self, "proxy_enabled"):
            fields["proxy_enabled"] = self.proxy_enabled.isChecked()
        if hasattr(self, "proxy_url"):
            fields["proxy_url"] = self.proxy_url.text().strip()
        if hasattr(self, "test_concurrency"):
            fields["test_concurrency"] = int(self.test_concurrency.value())
        if hasattr(self, "failover_enabled"):
            fields["failover_enabled"] = self.failover_enabled.isChecked()
        if hasattr(self, "failover_threshold"):
            fields["failover_fail_threshold"] = int(self.failover_threshold.value())
        if hasattr(self, "failover_silent"):
            fields["failover_silent"] = self.failover_silent.isChecked()
        if hasattr(self, "chat_persistent_session"):
            fields["chat_persistent_session"] = (
                self.chat_persistent_session.isChecked()
            )
        if hasattr(self, "minimize_to_tray"):
            fields["minimize_to_tray"] = self.minimize_to_tray.isChecked()
        if hasattr(self, "start_minimized"):
            fields["start_minimized"] = self.start_minimized.isChecked()
        if hasattr(self, "secure_keys_chk"):
            fields["secure_keys"] = self.secure_keys_chk.isChecked()
        if hasattr(self, "update_url_edit"):
            fields["update_manifest_url"] = self.update_url_edit.text().strip()
        self.persist_mgr(**fields)
        extras.set_proxy_settings(
            bool(self.mgr.get("proxy_enabled")),
            str(self.mgr.get("proxy_url") or ""),
        )
        extras.set_test_concurrency(int(self.mgr.get("test_concurrency") or 3))
        self._setup_health_timer()
        self.rebuild_tray_favorites()

    def _bind_settings_dirty(self) -> None:
        if getattr(self, "_settings_dirty_bound", False):
            return
        self._settings_dirty_bound = True
        for widget in (
            getattr(self, "set_provider", None),
            getattr(self, "set_model", None),
            getattr(self, "set_enabled", None),
            getattr(self, "proxy_url", None),
            getattr(self, "zhipu_key_edit", None),
        ):
            if widget is not None and hasattr(widget, "textChanged"):
                widget.textChanged.connect(self._mark_settings_dirty)
        for widget in (
            getattr(self, "set_thinking", None),
            getattr(self, "set_language", None),
            getattr(self, "set_ui_mode", None),
            getattr(self, "set_ui_accent", None),
            getattr(self, "vision_model_combo", None),
        ):
            if widget is not None:
                widget.currentIndexChanged.connect(self._mark_settings_dirty)
        for widget in (
            getattr(self, "proxy_enabled", None),
            getattr(self, "failover_enabled", None),
            getattr(self, "failover_silent", None),
            getattr(self, "chat_persistent_session", None),
            getattr(self, "minimize_to_tray", None),
            getattr(self, "start_minimized", None),
            getattr(self, "secure_keys_chk", None),
        ):
            if widget is not None:
                widget.toggled.connect(self._mark_settings_dirty)
        for widget in (
            getattr(self, "failover_threshold", None),
            getattr(self, "test_concurrency", None),
        ):
            if widget is not None:
                widget.valueChanged.connect(self._mark_settings_dirty)

    def _mark_settings_dirty(self, *_args) -> None:
        if getattr(self, "_settings_loading", False):
            return
        self._settings_dirty = True
        badge = getattr(self, "settings_dirty_badge", None)
        if badge is not None:
            badge.set_status("warning", "未保存")

    def _clear_settings_dirty(self) -> None:
        from datetime import datetime

        self._settings_dirty = False
        badge = getattr(self, "settings_dirty_badge", None)
        if badge is not None:
            badge.set_status("success", "已保存")
        label = getattr(self, "settings_saved_label", None)
        if label is not None:
            label.setText(f"保存于 {datetime.now().strftime('%H:%M:%S')}")

    def confirm_leave_settings(self) -> bool:
        if not getattr(self, "_settings_dirty", False):
            return True
        box = QMessageBox(self)
        box.setWindowTitle("未保存的更改")
        box.setText("设置页有未保存的更改。离开前要保存吗？")
        box.setInformativeText("选择「取消」将留在设置页。")
        save_btn = box.addButton("保存", QMessageBox.AcceptRole)
        box.addButton("放弃更改", QMessageBox.DestructiveRole)
        cancel_btn = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is cancel_btn:
            return False
        if clicked is save_btn:
            self.settings_save()
        else:
            self.settings_load()
        return True

    def vision_check_config(self):
        """校验识图配置就绪（设置页的识图模型默认使用，不写入模型列表）。

        设置页配置的智谱 API Key 与识图模型选择只用于识图管道：
        Pi vision skill（--vision-describe）默认调用它们把图片转为文字。
        这些模型不会自动出现在 provider 模型列表中；如需在列表中使用，
        请在 Provider 管理中手动添加。
        """
        if not core.zhipu_api_key():
            QMessageBox.warning(
                self,
                "未配置识图模型",
                "请先在「设置 → 识图模型」填入智谱 API Key（免费申请：https://bigmodel.cn）",
            )
            return
        try:
            core.ensure_zhipu_provider()
        except Exception as exc:
            QMessageBox.warning(self, "配置未就绪", str(exc))
            return
        if hasattr(self, "vision_test_status"):
            self.vision_test_status.setText(
                "识图配置就绪：模型列表不受影响；粘贴/拖入图片时由 Pi skill 默认调用识图模型"
                "（GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash）转文字后交给默认对话模型。"
            )
        self.status.showMessage("识图配置就绪（不写入模型列表）")
        body = (
            "设置中的识图模型（GLM-4.6V-Flash / GLM-4.1V-Thinking-Flash）已默认用于识图管道：\n\n"
            "· 粘贴/拖入图片时，Pi skill 自动调用识图转文字，再交给默认对话模型回答；\n"
            "· 识图模型不会自动出现在模型列表中（除非你在 Provider 管理中手动添加）；\n"
            "· 可在「设置 → 识图模型」切换识图模型，无需改动模型列表。"
        )
        show = getattr(self, "show_result", None)
        if callable(show):
            show("识图配置就绪", body, tone="info")
        else:
            QMessageBox.information(self, "识图配置就绪", body)

    def vision_test_run(self):
        key = ""
        if hasattr(self, "zhipu_key_edit"):
            key = self.zhipu_key_edit.text().strip()
        if key:
            # 测试前同步输入框中的 Key，避免用户忘记点「保存设置」
            try:
                core.set_zhipu_api_key(key)
            except Exception as e:
                logger.warning("sync zhipu api key from input failed: %s", e)
        if not core.zhipu_api_key():
            QMessageBox.warning(
                self,
                "未配置识图模型",
                "请先在「设置 → 识图模型」填入智谱 API Key（免费申请：https://bigmodel.cn）",
            )
            return
        if hasattr(self, "vision_test_status"):
            self.vision_test_status.setText("正在生成红色测试图并调用识图模型…")
        self.status.showMessage("正在验证识图模型可用性…")

        def job():
            return core.test_vision()

        w = self._track(self._worker_fn(job))
        w.done.connect(self._on_vision_test_done)
        w.failed.connect(self._on_vision_test_fail)
        w.start()

    def _on_vision_test_done(self, result: dict):
        self.status.showMessage("识图测试完成")
        if not hasattr(self, "vision_test_status"):
            return
        if result.get("ok"):
            desc = str(result.get("description") or "").strip()
            model = str(result.get("model") or "") or "自动"
            self.vision_test_status.setText(
                f"识图正常（{model}）：模型返回「{desc[:100]}」"
            )
            self.notify_success(
                f"识图正常（{model}）：{desc[:80]}"
            )
        else:
            err = str(result.get("error") or "未知错误")
            self.vision_test_status.setText(f"识图失败：{err[:140]}")
            QMessageBox.warning(self, "识图测试失败", err)

    def _on_vision_test_fail(self, err: str):
        self.status.showMessage("识图测试失败")
        if hasattr(self, "vision_test_status"):
            self.vision_test_status.setText(f"识图测试异常：{err[:140]}")
        QMessageBox.warning(self, "识图测试失败", err)

