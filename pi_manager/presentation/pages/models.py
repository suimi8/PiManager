"""Modern model catalog page."""
from __future__ import annotations

import logging

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QSplitter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import core
from ..components import (
    EmptyState,
    ErrorActionPanel,
    PropertyTable,
    StatusBadge,
    SurfaceCard,
)
from ..design.icons import icon
from ..design.tokens import tokens_for
from ..status import classify_test_result, format_capability
from ..workers import BATCH_TEST_TIMEOUT_DIRECT, BATCH_TEST_TIMEOUT_PI, BatchTestWorker, Worker

LATENCY_OK_MS = 800
LATENCY_WARN_MS = 2000

logger = logging.getLogger(__name__)


class _ModelsPageBody(QWidget):
    """把窗口宽度变化转给列自适应，避免窄窗口把延迟列挤没。"""

    def __init__(self, window, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._window = window

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        adapt = getattr(self._window, "_adapt_models_columns", None)
        if callable(adapt):
            adapt()


def build_models_page(window) -> QWidget:
    page = _ModelsPageBody(window)
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(24, 22, 24, 24)
    layout.setSpacing(16)

    filters = QHBoxLayout()
    filters.setSpacing(8)
    window.model_filter = QLineEdit()
    window.model_filter.setPlaceholderText("搜索模型名称、Provider 或能力…")
    try:
        window.model_filter.setClearButtonEnabled(True)
    except Exception:
        pass
    window._model_filter_debounce = QTimer(window)
    window._model_filter_debounce.setSingleShot(True)
    window._model_filter_debounce.setInterval(180)
    window._model_filter_debounce.timeout.connect(window.fill_models_table)
    window.model_filter.textChanged.connect(
        lambda _text: window._model_filter_debounce.start()
    )
    window.model_provider_filter = QComboBox()
    window.model_provider_filter.setMinimumWidth(120)
    window.model_provider_filter.addItem("全部 Provider", "")
    window.model_provider_filter.currentIndexChanged.connect(window.fill_models_table)
    window.model_capability_filter = QComboBox()
    window.model_capability_filter.addItem("全部能力", "")
    window.model_capability_filter.addItem("支持思考", "thinking")
    window.model_capability_filter.addItem("支持图片", "images")
    window.model_capability_filter.currentIndexChanged.connect(window.fill_models_table)
    window.model_only_favorites = QCheckBox("仅看收藏")
    window.model_only_favorites.toggled.connect(window.fill_models_table)
    filters.addWidget(window.model_filter, 1)
    filters.addWidget(window.model_provider_filter)
    filters.addWidget(window.model_capability_filter)
    filters.addWidget(window.model_only_favorites)
    window.models_refresh_btn = window._btn("刷新", window.refresh_models, secondary=True)
    window.models_refresh_cancel_btn = window._btn(
        "取消刷新", window.refresh_models_cancel, ghost=True
    )
    window.models_refresh_cancel_btn.setEnabled(False)
    window.models_refresh_cancel_btn.setToolTip("停止尚未完成的模型列表读取")
    filters.addWidget(window.models_refresh_btn)
    filters.addWidget(window.models_refresh_cancel_btn)
    layout.addLayout(filters)

    meta = QHBoxLayout()
    window.models_count_lbl = QLabel("0 个模型")
    window.models_count_lbl.setObjectName("subtitle")
    meta.addWidget(window.models_count_lbl, 1)
    window.model_detail_toggle = window._btn(
        "收起详情", window.toggle_model_detail, ghost=True
    )
    window.model_detail_toggle.setToolTip("窄窗口时可收起右侧详情栏")
    meta.addWidget(window.model_detail_toggle)
    layout.addLayout(meta)

    actions = QHBoxLayout()
    actions.setSpacing(8)
    actions.addWidget(window._btn("设为默认", window.model_set_default, success=True))
    actions.addWidget(window._btn("启动 Pi", window.model_launch, secondary=True))
    actions.addWidget(
        window._btn("测试选中", window.model_test_selected, secondary=True)
    )
    window.model_test_cancel_btn = window._btn(
        "停止测试", window.model_test_cancel, danger=True
    )
    window.model_test_cancel_btn.setEnabled(False)
    window.model_test_cancel_btn.setToolTip(
        "停止正在进行的批量测试；未开始的项不再执行"
    )
    actions.addWidget(window.model_test_cancel_btn)
    actions.addWidget(window._btn("收藏", window.model_add_favorite_batch, ghost=True))
    actions.addStretch(1)
    think_lbl = QLabel("Thinking")
    think_lbl.setObjectName("muted")
    actions.addWidget(think_lbl)
    window.thinking_combo = QComboBox()
    window.thinking_combo.addItems(
        ["off", "minimal", "low", "medium", "high", "xhigh", "max"]
    )
    try:
        from ... import core as _core

        _default_thinking = _core.get_default_model()[2]
        if _default_thinking:
            window.thinking_combo.setCurrentText(_default_thinking)
    except Exception:
        window.thinking_combo.setCurrentText("high")
    window.thinking_combo.setMaximumWidth(100)
    actions.addWidget(window.thinking_combo)
    test_lbl = QLabel("测试")
    test_lbl.setObjectName("muted")
    actions.addWidget(test_lbl)
    window.test_mode_combo = QComboBox()
    window.test_mode_combo.addItem("自动", "auto")
    window.test_mode_combo.addItem("HTTP", "http")
    window.test_mode_combo.addItem("Pi", "pi")
    window.test_mode_combo.setMaximumWidth(90)
    actions.addWidget(window.test_mode_combo)

    more = QToolButton()
    window.model_more_button = more
    more.setText("更多")
    more.setPopupMode(QToolButton.InstantPopup)
    more.setProperty("secondary", True)
    more.setCursor(Qt.PointingHandCursor)
    more.setToolTip("高风险批量操作")
    colors = tokens_for(*_theme_pair(window))
    more.setIcon(icon("ellipsis", colors.text_muted, 17))
    menu = QMenu(more)
    menu.addAction("全选可见", window.model_select_visible)
    menu.addAction("收藏当前过滤结果", window.model_fav_filtered)
    menu.addAction("写入循环列表 (enabledModels)", window.model_set_enabled)
    menu.addSeparator()
    menu.addAction("测试默认模型", window.model_test_default)
    menu.addAction("测试过滤结果", window.model_test_filtered)
    menu.addAction("批量测试收藏", window.model_test_favorites)
    menu.addAction("测试全部模型", window.model_test_all)
    more.setMenu(menu)
    actions.addWidget(more)
    layout.addLayout(actions)

    window.test_status = QLabel("可使用 Ctrl / Shift 多选模型")
    window.test_status.setObjectName("subtitle")
    window.test_status.setWordWrap(True)
    layout.addWidget(window.test_status)

    splitter = QSplitter(Qt.Horizontal)
    splitter.setChildrenCollapsible(True)
    window.models_splitter = splitter

    table_host = QWidget()
    table_layout = QVBoxLayout(table_host)
    table_layout.setContentsMargins(0, 0, 0, 0)
    table_layout.setSpacing(8)
    window.models_table = QTreeWidget()
    window.models_table.setColumnCount(5)
    window.models_table.setHeaderLabels(["模型", "Provider", "能力", "状态", "延迟"])
    header = window.models_table.header()
    header.setSectionResizeMode(0, QHeaderView.Stretch)
    header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(4, QHeaderView.ResizeToContents)
    window._polish_tree(window.models_table)
    window.models_table.itemExpanded.connect(
        lambda item: window._remember_tree_expanded(item, True)
    )
    window.models_table.itemCollapsed.connect(
        lambda item: window._remember_tree_expanded(item, False)
    )

    def _on_model_double_clicked(item, _column):
        data = item.data(0, Qt.UserRole) if item is not None else None
        if isinstance(data, (list, tuple)) and len(data) == 2 and data[1]:
            window.model_set_default()
        else:
            item.setExpanded(not item.isExpanded())

    window.models_table.itemDoubleClicked.connect(_on_model_double_clicked)
    if hasattr(window, "_on_model_selection_changed"):
        window.models_table.itemSelectionChanged.connect(
            window._on_model_selection_changed
        )
    window.models_empty = EmptyState(
        "尚未发现模型",
        "请先配置至少一个 Provider，然后同步可用模型。",
    )
    window.models_empty.add_action(
        window._btn("配置 Provider", lambda: window._goto_page("providers"), success=True)
    )
    window.models_empty.add_action(
        window._btn("重新扫描", window.refresh_models, secondary=True)
    )
    window.models_empty.setVisible(False)
    table_layout.addWidget(window.models_table, 1)
    table_layout.addWidget(window.models_empty, 1)
    splitter.addWidget(table_host)

    detail = SurfaceCard(margins=(16, 16, 16, 16), spacing=8)
    window.model_detail_panel = detail
    detail.setMinimumWidth(0)
    window.model_detail_badge = StatusBadge("未选择", "neutral")
    detail.content.addWidget(window.model_detail_badge, 0, Qt.AlignLeft)
    window.model_detail_title = QLabel("选择一个模型")
    window.model_detail_title.setObjectName("sectionTitle")
    window.model_detail_title.setWordWrap(True)
    detail.content.addWidget(window.model_detail_title)
    window.model_detail_provider = QLabel("—")
    window.model_detail_provider.setObjectName("heroProvider")
    detail.content.addWidget(window.model_detail_provider)
    window.model_prop_table = PropertyTable()
    window.model_prop_table.set_rows([("提示", "选择模型后显示关键属性")])
    detail.content.addWidget(window.model_prop_table, 1)
    window.model_error_panel = ErrorActionPanel()
    window.model_error_panel.setVisible(False)
    window.model_error_retry_btn = window._btn(
        "重新测试", window.model_test_selected, secondary=True
    )
    window.model_error_provider_btn = window._btn(
        "前往 Provider 设置", window.model_goto_provider, ghost=True
    )
    window.model_error_detail_btn = window._btn(
        "查看详情", window.model_toggle_error_detail, ghost=True
    )
    window.model_error_copy_btn = window._btn(
        "复制错误", window.model_copy_error, ghost=True
    )
    window.model_error_panel.add_action(window.model_error_retry_btn)
    window.model_error_panel.add_action(window.model_error_provider_btn)
    window.model_error_panel.add_action(window.model_error_detail_btn)
    window.model_error_panel.add_action(window.model_error_copy_btn)
    detail.content.addWidget(window.model_error_panel)
    window.model_raw_toggle = window._btn(
        "查看原始配置", window.toggle_model_raw, ghost=True
    )
    detail.content.addWidget(window.model_raw_toggle)
    window.model_detail_text = QPlainTextEdit()
    window.model_detail_text.setReadOnly(True)
    window.model_detail_text.setObjectName("mono")
    window.model_detail_text.setPlainText("选择模型后显示配置与测试状态。")
    window.model_detail_text.setVisible(False)
    window.model_detail_text.setMaximumHeight(160)
    detail.content.addWidget(window.model_detail_text)
    detail_actions = QHBoxLayout()
    detail_actions.setSpacing(8)
    detail_actions.addWidget(
        window._btn("使用此模型", window.model_set_default, success=True)
    )
    detail_actions.addWidget(
        window._btn("测试连接", window.model_test_selected, secondary=True)
    )
    detail.content.addLayout(detail_actions)
    splitter.addWidget(detail)
    splitter.setStretchFactor(0, 1)
    splitter.setStretchFactor(1, 0)
    collapsed = bool((getattr(window, "mgr", None) or {}).get("ui_models_detail_collapsed"))
    splitter.setSizes([900, 0] if collapsed else [900, 280])
    detail.setVisible(not collapsed)
    window.model_detail_toggle.setText("展开详情" if collapsed else "收起详情")
    layout.addWidget(splitter, 1)

    find_shortcut = QShortcut(QKeySequence("Ctrl+F"), page)
    find_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
    find_shortcut.activated.connect(window.model_filter.setFocus)
    return page


def _theme_pair(window) -> tuple[str, str]:
    try:
        from ... import core
        value = core.get_ui_theme()
        return str(value.get("mode") or "night"), str(value.get("accent") or "blue")
    except Exception:
        return "night", "blue"


class ModelsPageMixin:
    """模型页行为：目录、筛选、测试与默认模型。从 ``ui.py`` 下沉。"""

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
        view = classify_test_result(res)
        if view.tone == "success":
            status_color = QColor(colors.success)
        elif view.tone == "danger":
            status_color = QColor(colors.danger)
        elif view.tone == "warning":
            status_color = QColor(colors.warning)
        elif view.tone == "info":
            status_color = QColor(colors.info)
        else:
            status_color = muted
        table_label = {
            "连接正常": "可用",
            "连接失败": "失败",
            "尚未测试": "未测",
            "测试中": "测试中",
        }.get(view.label, view.label)
        status_tip = view.reason or view.label
        if view.detail:
            status_tip = f"{view.reason}\n{view.detail}"[:300]
        lat = None if not res else res.get("latency_ms")
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
        if not res:
            latency_text, latency_color = "—", muted
        return table_label, latency_text, status_color, latency_color, status_tip, ""

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
            self.notify_warning("请先在模型列表中选择一个或多个模型")
            return
        self._run_model_tests([(m.provider, m.model) for m in rows])

    def model_test_default(self):
        provider, model, _thinking = core.get_default_model()
        if not provider or not model:
            self.notify_warning("尚未设置默认模型")
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
            self.notify_warning("收藏列表为空，请先收藏模型")
            return
        self._run_model_tests(pairs)

    def model_add_favorite_batch(self):
        rows = self.selected_model_rows()
        if not rows:
            # fallback single
            m = self.selected_model_row()
            rows = [m] if m else []
        if not rows:
            self.notify_warning("请先多选模型（Ctrl/Shift）")
            return
        favs = list(self.mgr.get("favorites") or [])
        n = 0
        for m in rows:
            if m.key not in favs:
                favs.append(m.key)
                n += 1
        self.persist_mgr(favorites=favs)
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

    def _model_matches_capability(self, m: core.ModelInfo) -> bool:
        cap = ""
        combo = getattr(self, "model_capability_filter", None)
        if combo is not None:
            cap = str(combo.currentData() or "")
        if not cap:
            return True
        if cap == "thinking":
            return format_capability(m.thinking) == "支持"
        if cap == "images":
            return format_capability(m.images) == "支持"
        return True

    def _visible_model_pairs(self) -> list[tuple[str, str]]:
        q = (self.model_filter.text() or "").lower().strip()
        rows = [
            m
            for m in self.models
            if self._model_matches_capability(m)
            and (
                not q
                or q in m.key.lower()
                or q in m.provider.lower()
                or q in m.model.lower()
            )
        ]
        return [(m.provider, m.model) for m in rows]

    def model_test_filtered(self):
        pairs = self._visible_model_pairs()
        if not pairs:
            self.notify_warning("当前过滤结果为空")
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
            self.notify_warning("请先刷新模型列表")
            return
        if len(pairs) > 20:
            if QMessageBox.question(
                self,
                "测试全部模型",
                f"将向各 Provider 发送 {len(pairs)} 次真实请求。\n\n"
                "这可能产生费用，并占用较长时间。已完成的结果会即时写入列表。"
                "确定继续？",
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
        self.persist_mgr(favorites=favs)
        self.fill_favorites()
        self.notify_success(f"过滤结果新增收藏 {n} 个，共 {len(favs)}")

    def fav_test(self):
        item = self.fav_list.currentItem()
        if not item:
            self.notify_warning("请先选择一个收藏模型")
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
            self.notify_warning("已有测试进行中，请稍候完成后再试。")
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
        self.status.showMessage(f"正在测试模型 0 / {n}（{mode}）…")
        if hasattr(self, "test_status"):
            self.test_status.setText(f"正在测试模型 0 / {n} …")

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
        self._test_running = False
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
        self.status.showMessage(f"正在测试模型 {done} / {total} · 可用 {ok_n} · 刚完成 {key}")
        if hasattr(self, "test_status"):
            recent = " | ".join(self._test_lines[-4:])
            self.test_status.setText(
                f"正在测试模型 {done} / {total}（可用 {ok_n}） · {recent}"
            )

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
        expected = int(getattr(self, "_test_total", 0) or 0)
        cancelled = expected > 0 and len(results) < expected
        verb = "已停止" if cancelled else "完成"
        summary = f"测试{verb}：{ok_n}/{len(results)} 可用（已实时写入列表与历史）"
        self.status.showMessage(summary)
        if hasattr(self, "test_status"):
            self.test_status.setText(summary + (" · " + " | ".join(lines[-6:]) if lines else ""))
        try:
            self.refresh_dashboard()
        except Exception as e:
            logger.warning("dashboard refresh after model tests failed: %s", e)
        # only popup for very small batches; large ones already streamed to UI
        if len(results) <= 2:
            body = summary + ("\n\n" + "\n".join(lines) if lines else "")
            tone = "success" if ok_n else "warning"
            show = getattr(self, "show_result", None)
            if callable(show):
                show("测试结果", body, tone=tone)
            else:
                QMessageBox.information(self, "测试结果", body)

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

    def refresh_models(self):
        existing = getattr(self, "_models_refresh_worker", None)
        try:
            if existing is not None and existing.isRunning():
                self.refresh_models_cancel()
                return
        except RuntimeError:
            pass

        def job(is_cancelled=None):
            return core.list_models(is_cancelled=is_cancelled)

        self._set_models_refresh_busy(True)
        self.status.showMessage("正在读取模型列表…")
        w = self._track(Worker(job))
        self._models_refresh_worker = w
        w.done.connect(self._on_models_loaded)
        w.failed.connect(self._on_models_load_fail)
        w.start()

    def refresh_models_cancel(self) -> None:
        worker = getattr(self, "_models_refresh_worker", None)
        try:
            if worker is None or not worker.isRunning():
                self._set_models_refresh_busy(False)
                return
            worker.requestInterruption()
        except RuntimeError:
            self._set_models_refresh_busy(False)
            return
        self.status.showMessage("正在取消刷新…")

    def _set_models_refresh_busy(self, busy: bool) -> None:
        setter = getattr(self, "_set_action_busy", None)
        if callable(setter):
            setter(getattr(self, "models_refresh_btn", None), busy, idle="刷新", busy_text="正在刷新…")
        cancel = getattr(self, "models_refresh_cancel_btn", None)
        if cancel is not None:
            cancel.setEnabled(bool(busy))

    def _on_models_load_fail(self, err: str) -> None:
        self._models_refresh_worker = None
        self._set_models_refresh_busy(False)
        QMessageBox.warning(self, "错误", err)

    def _on_models_loaded(self, models: list[core.ModelInfo]):
        self._models_refresh_worker = None
        self._set_models_refresh_busy(False)
        self.models = models
        self.fill_models_table()
        try:
            self.refresh_chat_model_choices()
        except Exception:
            pass
        summary = f"已加载 {len(models)} 个模型"
        self.status.showMessage(summary)
        notify = getattr(self, "notify_success", None)
        if callable(notify):
            notify(summary)

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
            if not self._model_matches_capability(m):
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
        self._sync_models_empty_state(shown=sum(len(v) for v in groups.values()))
        self._adapt_models_columns()

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
        current = self._model_item_key(self.models_table.currentItem())
        if current == (provider, model) and hasattr(self, "_on_model_selection_changed"):
            self._on_model_selection_changed()
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

    def model_set_default(self):
        m = self.selected_model_row()
        if not m:
            self.notify_warning("请先选择模型")
            return
        core.set_default_model(m.provider, m.model, self.thinking_combo.currentText())
        self.refresh_dashboard()
        self.settings_load()
        self.fill_models_table()
        self.status.showMessage(f"默认模型已切换为 {m.key}")
        notify = getattr(self, "notify_success", None)
        if callable(notify):
            notify(f"默认模型已切换为 {m.key}")

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
            self.notify_warning("请先收藏一些模型，或选中一个模型")
            return
        if QMessageBox.question(
            self,
            "覆盖 enabledModels",
            "将用当前收藏列表覆盖 settings.json 中的 enabledModels。\n\n"
            "Pi 会话里 Ctrl+P 循环切换将只包含这些模型，"
            "不会删除 models.json 里的其它模型。确定写入？",
        ) != QMessageBox.Yes:
            return
        core.set_enabled_models(favs)
        self.settings_load()
        self.status.showMessage(f"enabledModels = {favs}")
        self.notify_success("已写入 enabledModels，Pi 会话里可用 Ctrl+P 循环切换")

    def toggle_model_detail(self) -> None:
        panel = getattr(self, "model_detail_panel", None)
        splitter = getattr(self, "models_splitter", None)
        if panel is None:
            return
        visible = panel.isHidden()
        panel.setVisible(visible)
        if splitter is not None:
            splitter.setSizes([900, 280] if visible else [900, 0])
        button = getattr(self, "model_detail_toggle", None)
        if button is not None:
            button.setText("收起详情" if visible else "展开详情")
        try:
            self.persist_mgr(ui_models_detail_collapsed=not visible)
        except Exception:
            pass

    def toggle_model_raw(self) -> None:
        editor = getattr(self, "model_detail_text", None)
        if editor is None:
            return
        editor.setVisible(editor.isHidden())
        button = getattr(self, "model_raw_toggle", None)
        if button is not None:
            button.setText("隐藏原始配置" if not editor.isHidden() else "查看原始配置")

    def model_goto_provider(self) -> None:
        info = self.selected_model_row()
        self._goto_page("providers")
        if info is None or not hasattr(self, "provider_list"):
            return
        matches = self.provider_list.findItems(info.provider, Qt.MatchExactly)
        if matches:
            self.provider_list.setCurrentItem(matches[0])

    def model_toggle_error_detail(self) -> None:
        panel = getattr(self, "model_error_panel", None)
        if panel is not None:
            panel.toggle_detail()

    def model_copy_error(self) -> None:
        panel = getattr(self, "model_error_panel", None)
        text = panel.detail_text() if panel is not None else ""
        if not text:
            return
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(text)
        self.status.showMessage("已复制错误详情", 4000)

    def _sync_models_empty_state(self, *, shown: int) -> None:
        empty = getattr(self, "models_empty", None)
        table = getattr(self, "models_table", None)
        if empty is None or table is None:
            return
        if shown:
            empty.setVisible(False)
            table.setVisible(True)
            return
        table.setVisible(False)
        empty.setVisible(True)
        if self.models:
            empty.set_copy(
                "没有匹配的模型",
                "当前搜索或筛选条件下没有结果。可以清除筛选后再试。",
            )
        else:
            empty.set_copy(
                "尚未发现模型",
                "请先配置至少一个 Provider，然后同步可用模型。",
            )

    def _adapt_models_columns(self) -> None:
        table = getattr(self, "models_table", None)
        if table is None:
            return
        width = max(table.width(), self.width() if hasattr(self, "width") else 0)
        try:
            table.setColumnHidden(1, True)
            table.setColumnHidden(2, width < 1100)
            table.setColumnHidden(4, width < 900)
        except Exception:
            pass
