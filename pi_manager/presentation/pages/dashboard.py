"""Modern dashboard page while preserving the legacy behavior contract."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import core
from ..components import InlineBanner, MetricCard, SectionHeading, StatusBadge, SurfaceCard
from ..workers import Worker

logger = logging.getLogger(__name__)


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
    layout.setContentsMargins(24, 22, 24, 24)
    layout.setSpacing(24)

    banner = QFrame()
    banner.setObjectName("updateBanner")
    banner.setVisible(False)
    banner_layout = QVBoxLayout(banner)
    banner_layout.setContentsMargins(14, 10, 14, 10)
    banner_layout.setSpacing(4)
    window.update_banner = banner
    window.pi_banner_label = QLabel("—")
    window.pi_banner_label.setObjectName("bannerText")
    window.pi_banner_label.setWordWrap(True)
    window.pi_banner_btn = window._btn("查看", window.on_pi_banner_action, success=True)
    window.pi_banner_btn.setVisible(False)
    window.pi_banner_close = window._btn("✕", window.dismiss_pi_update, ghost=True)
    window.pi_banner_close.setToolTip("忽略本次更新提示")
    pi_row = QHBoxLayout()
    pi_row.setSpacing(8)
    pi_row.addWidget(window.pi_banner_label, 1)
    pi_row.addWidget(window.pi_banner_btn)
    pi_row.addWidget(window.pi_banner_close)
    banner_layout.addLayout(pi_row)
    window.mgr_banner_label = QLabel("—")
    window.mgr_banner_label.setObjectName("bannerText")
    window.mgr_banner_label.setWordWrap(True)
    window.mgr_banner_btn = window._btn("查看更新", window.on_mgr_banner_action, success=True)
    window.mgr_banner_btn.setVisible(False)
    window.mgr_banner_close = window._btn("✕", window.dismiss_manager_update, ghost=True)
    window.mgr_banner_close.setToolTip("忽略本次更新提示")
    mgr_row = QHBoxLayout()
    mgr_row.setSpacing(8)
    mgr_row.addWidget(window.mgr_banner_label, 1)
    mgr_row.addWidget(window.mgr_banner_btn)
    mgr_row.addWidget(window.mgr_banner_close)
    banner_layout.addLayout(mgr_row)
    layout.addWidget(banner, 0)

    summary = QFrame()
    summary.setObjectName("summaryStrip")
    overview = QHBoxLayout(summary)
    overview.setContentsMargins(0, 0, 0, 0)
    overview.setSpacing(8)

    window.dashboard_model_metric = MetricCard("默认模型")
    window.lbl_current = window.dashboard_model_metric.value_label
    window.lbl_current.setObjectName("metricValue")
    window.lbl_current.setWordWrap(True)
    window.lbl_thinking = QLabel("Thinking: —")
    window.lbl_thinking.setObjectName("subtitle")
    window.lbl_thinking.setWordWrap(True)
    window.dashboard_model_metric.content.addWidget(window.lbl_thinking)

    window.dashboard_status_metric = MetricCard("连接状态", "尚未测试")
    window.default_status_badge = StatusBadge("尚未测试", "warning")
    window.dashboard_status_metric.content.addWidget(window.default_status_badge, 0, Qt.AlignLeft)

    window.dashboard_provider_metric = MetricCard("已配置 Provider", "0")
    window.dashboard_tested_metric = MetricCard("最近测试", "从未测试")

    overview.addWidget(window.dashboard_model_metric)
    overview.addWidget(window.dashboard_status_metric)
    overview.addWidget(window.dashboard_provider_metric)
    overview.addWidget(window.dashboard_tested_metric)
    layout.addWidget(summary)

    window.availability_banner = InlineBanner()
    window.availability_banner_label = window.availability_banner.message
    window.availability_banner_btn = window.availability_banner.action_btn
    layout.addWidget(window.availability_banner)

    hero_actions = QHBoxLayout()
    hero_actions.setSpacing(8)
    launch = window._btn("启动完整 Pi", window.launch_default, success=True)
    launch.setProperty("large", True)
    hero_actions.addWidget(launch)
    hero_actions.addWidget(
        window._btn("选择模型", lambda: window._goto_page("models"), secondary=True)
    )
    hero_actions.addWidget(
        window._btn("刷新状态", window.refresh_dashboard, ghost=True)
    )
    window.version_pill = QLabel("Pi CLI · 检查中")
    window.version_pill.setObjectName("subtitle")
    window.version_update_label = QLabel("")
    window.version_update_label.setObjectName("versionUpdateLabel")
    window.version_update_label.setWordWrap(True)
    window.version_update_label.setVisible(False)
    window.version_update_btn = window._btn(
        "立即更新", window.on_pi_banner_action, success=True
    )
    window.version_update_btn.setVisible(False)
    window.version_update_btn.setToolTip("打开 Pi 安装 / 升级面板")
    hero_actions.addWidget(window.version_pill)
    hero_actions.addWidget(window.version_update_label)
    hero_actions.addWidget(window.version_update_btn)
    hero_actions.addStretch(1)
    layout.addLayout(hero_actions)

    middle = QHBoxLayout()
    middle.setSpacing(16)

    quick = SurfaceCard(margins=(16, 16, 16, 16), spacing=8)
    quick.content.addWidget(
        SectionHeading("快速接入 Provider")
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
    window.quick_status = QLabel("等待连接 · 保存默认 1M 上下文、只开思考")
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

    workspace = SurfaceCard(margins=(16, 16, 16, 16), spacing=8)
    workspace.content.addWidget(
        SectionHeading("项目与启动方式")
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
    window.drop_zone.setMinimumHeight(88)
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
    lower.setSpacing(16)
    favorites = SurfaceCard(margins=(16, 16, 16, 16), spacing=8)
    fav_header = QHBoxLayout()
    fav_header.addWidget(SectionHeading("收藏模型"), 1)
    window.dashboard_favorite_metric = MetricCard("收藏")
    window.dashboard_favorite_metric.setMaximumWidth(140)
    fav_header.addWidget(window.dashboard_favorite_metric)
    favorites.content.addLayout(fav_header)
    window.fav_list = QListWidget()
    window.fav_list.setMinimumHeight(120)
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

    auth = SurfaceCard(margins=(16, 16, 16, 16), spacing=8)
    auth_header = QHBoxLayout()
    auth_header.addWidget(SectionHeading("认证状态"), 1)
    window.dashboard_auth_metric = MetricCard("登录态")
    window.dashboard_auth_metric.setMaximumWidth(140)
    auth_header.addWidget(window.dashboard_auth_metric)
    auth.content.addLayout(auth_header)
    window.auth_table = QTableWidget(0, 2)
    window.auth_table.setHorizontalHeaderLabels(["Provider", "状态"])
    window.auth_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    window._polish_table(window.auth_table)
    window.auth_table.setMinimumHeight(120)
    auth.content.addWidget(window.auth_table, 1)
    auth_actions = QHBoxLayout()
    auth_actions.setSpacing(8)
    auth_actions.addWidget(window._btn("登出选中", window.auth_logout_selected, danger=True))
    auth_actions.addStretch(1)
    auth.content.addLayout(auth_actions)
    auth_hint = QLabel(
        "内置 Provider（如 openai-codex）需单独登录后才可用；登出仅移除 Pi 的登录态，"
        "不影响本机其他工具（OpenAI / Claude 等各自独立存储凭证）。"
    )
    auth_hint.setObjectName("subtitle")
    auth_hint.setWordWrap(True)
    auth.content.addWidget(auth_hint)
    lower.addWidget(auth, 1)
    layout.addLayout(lower)
    layout.addStretch(1)

    scroll.setWidget(body)
    outer_layout.addWidget(scroll)
    return outer


class DashboardPageMixin:
    """仪表盘行为：快速接入、工作目录拖放、默认启动与收藏。从 ``ui.py`` 下沉。"""

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
        models = [
            core.apply_model_capabilities(item) if isinstance(item, dict) else item
            for item in (result.get("models") or [])
        ]
        if not models:
            self.quick_status.setText("成功但模型列表为空")
            self.notify_warning("接口返回空模型列表，请检查 Base URL 是否正确")
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
        self.notify_success(f"Provider「{name}」已写入，共 {len(models)} 个模型")

    def _on_quick_fetch_fail(self, err: str):
        self.quick_status.setText(f"失败：{err}")
        QMessageBox.warning(self, "拉取失败", err)

    def persist_mgr(self, **fields: Any) -> dict[str, Any]:
        """原子合并写入 pi-manager.json。

        只改本次关心的键，外加仪表盘工作目录/终端控件当前值。
        """
        last_workdir = None
        workdir_edit = getattr(self, "workdir_edit", None)
        if workdir_edit is not None:
            last_workdir = workdir_edit.text().strip()
        terminal = None
        terminal_combo = getattr(self, "terminal_combo", None)
        if terminal_combo is not None:
            terminal = terminal_combo.currentData() or terminal_combo.currentText()

        def _apply(cfg: dict[str, Any]) -> dict[str, Any]:
            if last_workdir is not None:
                cfg["last_workdir"] = last_workdir
            if terminal is not None:
                cfg["terminal"] = terminal
            cfg.update(fields)
            return cfg

        self.mgr = core.update_manager_config(_apply)
        return self.mgr

    def _on_drop_auto_launch_toggled(self, checked: bool):
        self.persist_mgr(drop_auto_launch=bool(checked))

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
            self.notify_warning("工作目录已更新，但尚未设置默认模型。请先在「模型列表」中设为默认。")
            return
        self._launch(provider, model, thinking or None)

    def browse_workdir(self):
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", self.workdir_edit.text())
        if d:
            self.workdir_edit.setText(d)
            self.persist_mgr()

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
        filtered = [x for x in (self.mgr.get("favorites") or []) if x != key]
        self.persist_mgr(favorites=filtered)
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

    def auth_logout_selected(self):
        if not hasattr(self, "auth_table"):
            return
        sm = self.auth_table.selectionModel()
        if not sm:
            return
        rows = sm.selectedRows()
        if not rows:
            self.notify_warning("请先在认证状态表中选择一个 Provider")
            return
        providers = []
        for idx in rows:
            item = self.auth_table.item(idx.row(), 0)
            if item and item.text().strip():
                providers.append(item.text().strip())
        if not providers:
            return
        if QMessageBox.question(
            self,
            "登出确认",
            f"将从 Pi 中移除以下 Provider 的登录状态：\n\n{chr(10).join(providers)}\n\n"
            "仅影响 Pi 的 auth.json；本机 OpenAI / Claude 等其他工具的登录不受影响。继续？",
        ) != QMessageBox.Yes:
            return
        ok_n = 0
        errors = []
        with self._busy(f"正在登出 {len(providers)} 个 Provider…"):
            for provider in providers:
                try:
                    if core.delete_provider_auth(provider) is not None:
                        ok_n += 1
                except Exception as e:
                    errors.append(f"{provider}: {e}")
        self.refresh_dashboard()
        # 内置 Provider 登出后 Pi 不再认为其已认证，模型列表随之收敛
        try:
            self.refresh_models()
        except Exception as e:
            logger.warning("refresh models after logout failed: %s", e)
        msg = f"已登出 {ok_n} 个 Provider。"
        if errors:
            msg += f"\n失败：{'；'.join(errors)}"
        if ok_n:
            msg += "\nPi 的模型列表已刷新，登出的内置 Provider 将不再显示。"
        if errors:
            show = getattr(self, "show_result", None)
            if callable(show):
                show("登出完成", msg, tone="warning")
            else:
                QMessageBox.information(self, "完成", msg)
        else:
            self.notify_success(msg.split("\n")[0])

