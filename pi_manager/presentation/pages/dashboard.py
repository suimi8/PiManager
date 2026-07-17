"""Modern dashboard page while preserving the legacy behavior contract."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QScrollArea,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from ... import core
from ..components import MetricCard, SectionHeading, StatusBadge, SurfaceCard


def build_dashboard_page(window) -> QWidget:
    outer = QWidget()
    outer.setObjectName("pageBody")
    outer_layout = QVBoxLayout(outer)
    outer_layout.setContentsMargins(0, 0, 0, 0)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(26, 22, 26, 26)
    layout.setSpacing(14)

    overview = QHBoxLayout()
    overview.setSpacing(14)

    hero = SurfaceCard(elevated=True, object_name="heroCard", margins=(20, 18, 20, 18), spacing=9)
    hero_top = QHBoxLayout()
    hero_top.setSpacing(8)
    kicker = QLabel("CURRENT WORKSPACE")
    kicker.setObjectName("sectionKicker")
    hero_top.addWidget(kicker)
    hero_top.addStretch(1)
    window.default_status_badge = StatusBadge("默认模型", "info")
    hero_top.addWidget(window.default_status_badge)
    hero.content.addLayout(hero_top)
    window.lbl_current = QLabel("—")
    window.lbl_current.setObjectName("heroValue")
    window.lbl_current.setWordWrap(True)
    hero.content.addWidget(window.lbl_current)
    window.lbl_thinking = QLabel("Thinking: —")
    window.lbl_thinking.setObjectName("subtitle")
    hero.content.addWidget(window.lbl_thinking)
    hero.content.addStretch(1)
    hero_actions = QHBoxLayout()
    hero_actions.setSpacing(8)
    launch = window._btn("启动完整 Pi", window.launch_default, success=True)
    launch.setProperty("large", True)
    hero_actions.addWidget(launch)
    hero_actions.addWidget(window._btn("选择模型", lambda: window._goto_page("models"), secondary=True))
    hero_actions.addWidget(window._btn("刷新状态", window.refresh_dashboard, ghost=True))
    hero_actions.addStretch(1)
    hero.content.addLayout(hero_actions)
    overview.addWidget(hero, 2)

    metrics = QWidget()
    metrics_layout = QVBoxLayout(metrics)
    metrics_layout.setContentsMargins(0, 0, 0, 0)
    metrics_layout.setSpacing(8)
    metric_row_1 = QHBoxLayout()
    metric_row_1.setSpacing(8)
    version_card = MetricCard("PI CLI 版本", "检查中")
    window.version_pill = version_card.value_label
    window.version_pill.setObjectName("metricValue")
    window.dashboard_provider_metric = MetricCard("自定义 Provider", "0")
    metric_row_1.addWidget(version_card)
    metric_row_1.addWidget(window.dashboard_provider_metric)
    metric_row_2 = QHBoxLayout()
    metric_row_2.setSpacing(8)
    window.dashboard_favorite_metric = MetricCard("收藏模型", "0")
    window.dashboard_auth_metric = MetricCard("认证状态", "0")
    metric_row_2.addWidget(window.dashboard_favorite_metric)
    metric_row_2.addWidget(window.dashboard_auth_metric)
    metrics_layout.addLayout(metric_row_1)
    metrics_layout.addLayout(metric_row_2)
    overview.addWidget(metrics, 1)
    layout.addLayout(overview)

    middle = QHBoxLayout()
    middle.setSpacing(14)

    quick = SurfaceCard(margins=(18, 17, 18, 17), spacing=11)
    quick.content.addWidget(
        SectionHeading("快速接入 Provider", "使用兼容 API 地址和密钥拉取模型，保存后可立即切换。")
    )
    form = QFormLayout()
    form.setSpacing(9)
    form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
    window.quick_name = QLineEdit("custom")
    window.quick_name.setPlaceholderText("Provider 名称")
    window.quick_base = QLineEdit("https://api.openai.com/v1")
    window.quick_base.setPlaceholderText("https://你的中转地址/v1")
    window.quick_key = QLineEdit()
    window.quick_key.setEchoMode(QLineEdit.PasswordEchoOnEdit)
    window.quick_key.setPlaceholderText("sk-... 或环境变量名")
    window.quick_api = QComboBox()
    window.quick_api.addItems(
        [
            "openai-completions",
            "openai-responses",
            "anthropic-messages",
            "google-generative-ai",
        ]
    )
    form.addRow("名称", window.quick_name)
    form.addRow("Base URL", window.quick_base)
    form.addRow("API Key", window.quick_key)
    form.addRow("API 类型", window.quick_api)
    quick.content.addLayout(form)
    window.quick_status = QLabel("等待连接")
    window.quick_status.setObjectName("subtitle")
    window.quick_status.setWordWrap(True)
    quick.content.addWidget(window.quick_status)
    quick_actions = QHBoxLayout()
    quick_actions.setSpacing(8)
    quick_actions.addWidget(window._btn("拉取并保存", window.quick_fetch_and_save, success=True))
    quick_actions.addWidget(window._btn("高级设置", window.provider_fetch_api, secondary=True))
    quick_actions.addWidget(window._btn("Provider 管理", lambda: window._goto_page("providers"), ghost=True))
    quick_actions.addStretch(1)
    quick.content.addLayout(quick_actions)
    middle.addWidget(quick, 1)

    workspace = SurfaceCard(margins=(18, 17, 18, 17), spacing=11)
    workspace.content.addWidget(
        SectionHeading("项目与启动方式", "指定 Pi 的工作目录、终端，并支持拖入项目后直接启动。")
    )
    path_row = QHBoxLayout()
    path_row.setSpacing(8)
    window.workdir_edit = QLineEdit(window.mgr.get("last_workdir") or str(core.user_home()))
    window.workdir_edit.setPlaceholderText("选择项目目录")
    path_row.addWidget(window.workdir_edit, 1)
    path_row.addWidget(window._btn("浏览", window.browse_workdir, secondary=True))
    workspace.content.addLayout(path_row)

    terminal_row = QHBoxLayout()
    terminal_row.setSpacing(8)
    terminal_label = QLabel("启动终端")
    terminal_label.setObjectName("muted")
    terminal_row.addWidget(terminal_label)
    window.terminal_combo = QComboBox()
    for value, label in core.list_terminal_options():
        window.terminal_combo.addItem(label, value)
    terminal = window.mgr.get("terminal", "auto")
    index = window.terminal_combo.findData(terminal)
    if index < 0:
        index = window.terminal_combo.findText(terminal)
    if index >= 0:
        window.terminal_combo.setCurrentIndex(index)
    terminal_row.addWidget(window.terminal_combo, 1)
    workspace.content.addLayout(terminal_row)

    window.drop_zone = QFrame()
    window.drop_zone.setObjectName("dropZone")
    window.drop_zone.setMinimumHeight(112)
    drop_layout = QVBoxLayout(window.drop_zone)
    drop_layout.setContentsMargins(16, 14, 16, 14)
    drop_layout.setSpacing(5)
    window.drop_title = QLabel("拖入项目文件夹")
    window.drop_title.setObjectName("sectionTitle")
    window.drop_title.setAlignment(Qt.AlignCenter)
    window.drop_hint = QLabel("自动识别工作目录；可按下方开关立即使用默认模型启动")
    window.drop_hint.setObjectName("subtitle")
    window.drop_hint.setAlignment(Qt.AlignCenter)
    window.drop_hint.setWordWrap(True)
    window.chk_drop_launch = QCheckBox("拖入后立即启动 Pi")
    window.chk_drop_launch.setChecked(bool(window.mgr.get("drop_auto_launch", True)))
    window.chk_drop_launch.toggled.connect(window._on_drop_auto_launch_toggled)
    drop_layout.addStretch(1)
    drop_layout.addWidget(window.drop_title)
    drop_layout.addWidget(window.drop_hint)
    drop_layout.addWidget(window.chk_drop_launch, 0, Qt.AlignCenter)
    drop_layout.addStretch(1)
    workspace.content.addWidget(window.drop_zone)
    middle.addWidget(workspace, 1)
    layout.addLayout(middle)

    lower = QHBoxLayout()
    lower.setSpacing(14)
    favorites = SurfaceCard(margins=(18, 17, 18, 17), spacing=10)
    favorites.content.addWidget(SectionHeading("收藏模型", "双击设为默认，或对收藏模型执行批量健康测试。"))
    window.fav_list = QListWidget()
    window.fav_list.setMinimumHeight(155)
    window.fav_list.itemDoubleClicked.connect(window.on_fav_double)
    favorites.content.addWidget(window.fav_list, 1)
    fav_actions = QHBoxLayout()
    fav_actions.setSpacing(8)
    fav_actions.addWidget(window._btn("设为默认", window.fav_set_default, success=True))
    fav_actions.addWidget(window._btn("启动", window.fav_launch, secondary=True))
    fav_actions.addWidget(window._btn("测试", window.fav_test, secondary=True))
    fav_actions.addWidget(window._btn("移除", window.fav_remove, ghost=True))
    fav_actions.addStretch(1)
    favorites.content.addLayout(fav_actions)
    lower.addWidget(favorites, 1)

    auth = SurfaceCard(margins=(18, 17, 18, 17), spacing=10)
    auth.content.addWidget(SectionHeading("认证状态", "OAuth 与本机登录态概览，不展示任何敏感凭据。"))
    window.auth_table = QTableWidget(0, 2)
    window.auth_table.setHorizontalHeaderLabels(["Provider", "状态"])
    window.auth_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    window._polish_table(window.auth_table)
    window.auth_table.setMinimumHeight(155)
    auth.content.addWidget(window.auth_table, 1)
    lower.addWidget(auth, 1)
    layout.addLayout(lower)
    layout.addStretch(1)

    scroll.setWidget(body)
    outer_layout.addWidget(scroll)
    return outer
