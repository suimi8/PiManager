"""Modern settings page with advanced groups folded by default."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..design import ACCENT_LABELS
from ..components import CollapsibleSection, SectionHeading, StatusBadge, SurfaceCard


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
    intro_row.addWidget(StatusBadge("自动保存到本地", "info"), 0, Qt.AlignTop)
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
    window.settings_raw.setMinimumHeight(190)
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
