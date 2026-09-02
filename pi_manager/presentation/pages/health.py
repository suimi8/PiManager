"""健康监控页：巡检范围、定时检查与结果表。"""
from __future__ import annotations

import logging

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import extras
from ..components import EmptyState, StatusBadge, SurfaceCard

logger = logging.getLogger(__name__)


def build_health_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    controls = SurfaceCard(margins=(14, 12, 14, 12), spacing=9)
    row = QHBoxLayout()
    row.setSpacing(8)
    window.health_scope = QComboBox()
    window.health_scope.addItem("收藏列表", "favorites")
    window.health_scope.addItem("默认模型", "default")
    window.health_scope.addItem("自定义 Provider", "custom")
    window.health_scope.addItem("全部已加载模型", "all_listed")
    window.health_scope.addItem("模型页当前选中", "selected")
    window.health_scope.setCurrentIndex(2)
    row.addWidget(QLabel("巡检范围"))
    row.addWidget(window.health_scope)
    row.addWidget(window._btn("立即健康检查", window.health_run_now, success=True))
    window.health_cancel_btn = window._btn("取消检查", window.health_cancel, ghost=True)
    window.health_cancel_btn.setEnabled(False)
    window.health_cancel_btn.setToolTip("停止尚未开始的探测；已完成的结果会保留")
    row.addWidget(window.health_cancel_btn)
    row.addWidget(window._btn("刷新结果", window.health_refresh_table, secondary=True))
    row.addStretch(1)
    window.health_interval = QSpinBox()
    window.health_interval.setRange(0, 1440)
    window.health_interval.setSuffix(" 分钟")
    window.health_interval.setSpecialValueText("关闭定时")
    window.health_interval.setValue(int((window.mgr or {}).get("health_interval_min") or 0))
    row.addWidget(QLabel("定时"))
    row.addWidget(window.health_interval)
    row.addWidget(window._btn("保存", window.health_save_interval, ghost=True))
    controls.content.addLayout(row)
    explanation = QLabel("巡检只在点击检查或定时触发时访问模型；打开页面仅加载本地缓存结果。")
    explanation.setObjectName("subtitle")
    explanation.setWordWrap(True)
    controls.content.addWidget(explanation)
    layout.addWidget(controls)

    table_card = SurfaceCard(margins=(0, 0, 0, 12), spacing=10)
    window.health_table = QTableWidget(0, 6)
    window.health_table.setHorizontalHeaderLabels(["模型", "状态", "延迟", "方式", "检查时间", "错误 / 预览"])
    window.health_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    window._polish_table(window.health_table)
    window.health_empty = EmptyState(
        "还没有健康检查结果",
        "点击「立即健康检查」探测当前范围的模型；已完成的结果会保留在这张表里。",
    )
    window.health_empty.add_action(
        window._btn("立即健康检查", window.health_run_now, success=True)
    )
    table_card.content.addWidget(window.health_table, 1)
    table_card.content.addWidget(window.health_empty)
    window.health_table.setVisible(False)
    action_row = QHBoxLayout()
    action_row.setContentsMargins(12, 0, 12, 0)
    action_row.setSpacing(8)
    action_row.addWidget(window._btn("收藏可用项", window.health_add_ok_to_favorites, secondary=True))
    action_row.addWidget(window._btn("重测选中", window.health_retest_selected, secondary=True))
    action_row.addStretch(1)
    window.health_status_badge = StatusBadge("等待检查", "neutral")
    action_row.addWidget(window.health_status_badge)
    table_card.content.addLayout(action_row)
    window.health_status = QLabel("尚未检查 — 推荐先检查默认模型或自定义 Provider")
    window.health_status.setObjectName("subtitle")
    window.health_status.setWordWrap(True)
    window.health_status.setContentsMargins(12, 0, 12, 0)
    table_card.content.addWidget(window.health_status)
    layout.addWidget(table_card, 1)
    return page


class HealthPageMixin:
    """健康监控页行为。从 ``DiagnosticsPageMixin`` 拆出。"""

    def _setup_health_timer(self):
        mins = 0
        try:
            mins = int((self.mgr or {}).get("health_interval_min") or 0)
        except Exception:
            mins = 0
        if self.health_timer:
            self.health_timer.stop()
            self.health_timer = None
        if mins > 0:
            self.health_timer = QTimer(self)
            self.health_timer.setInterval(mins * 60 * 1000)
            self.health_timer.timeout.connect(self.health_run_silent)
            self.health_timer.start()

    def health_save_interval(self):
        self.persist_mgr(health_interval_min=int(self.health_interval.value()))
        self._setup_health_timer()
        self.status.showMessage("健康检查定时已保存")
        notify = getattr(self, "notify_success", None)
        if callable(notify):
            notify("健康检查定时已保存")

    def health_run_silent(self):
        self._run_health(show_dialog=False)

    def health_run_now(self):
        self._run_health(show_dialog=True)

    def _health_scope_value(self) -> str:
        if hasattr(self, "health_scope"):
            return str(self.health_scope.currentData() or "favorites")
        return "favorites"

    def _run_health(self, show_dialog: bool = True):
        if getattr(self, "_health_running", False):
            if show_dialog:
                notify = getattr(self, "notify_warning", None)
                if callable(notify):
                    notify("健康检查进行中，请稍候。")
                else:
                    QMessageBox.information(self, "提示", "健康检查进行中，请稍候。")
            return
        mode = self._test_mode() if hasattr(self, "_test_mode") else "auto"
        scope = self._health_scope_value()
        selected = []
        if scope == "selected" and hasattr(self, "selected_model_rows"):
            selected = [(m.provider, m.model) for m in self.selected_model_rows()]
        self._health_running = True
        self._health_show_dialog = show_dialog
        self._health_done = 0
        self._health_ok = 0
        self._set_health_cancel_enabled(True)
        self.status.showMessage("健康检查进行中（逐项实时更新）…")
        if hasattr(self, "health_status"):
            self.health_status.setText("健康检查中：0 完成 …")

        from ..workers import BatchTestWorker

        # pairs resolved inside run_health_check; pass empty to let scope collect
        w = self._track(
            BatchTestWorker(
                [],
                mode=mode,
                kind="health",
                health_scope=scope,
                health_selected=selected,
            )
        )
        w.progress.connect(self._on_health_progress, Qt.QueuedConnection)
        w.done.connect(lambda r: self._on_health_done(r, getattr(self, "_health_show_dialog", True)), Qt.QueuedConnection)
        w.failed.connect(self._on_health_fail, Qt.QueuedConnection)
        self._health_worker = w
        w.start()

    def _set_health_cancel_enabled(self, enabled: bool) -> None:
        button = getattr(self, "health_cancel_btn", None)
        if button is not None:
            button.setEnabled(bool(enabled))

    def health_cancel(self) -> None:
        worker = getattr(self, "_health_worker", None)
        if worker is None or not worker.isRunning():
            self._health_running = False
            self._set_health_cancel_enabled(False)
            return
        worker.requestInterruption()
        self._health_running = False
        self._set_health_cancel_enabled(False)
        self.status.showMessage("已请求停止健康检查，正在收尾已发起的请求…")
        if hasattr(self, "health_status"):
            self.health_status.setText("已请求停止；未开始的项不再执行，已完成的结果会保留。")

    def _on_health_progress(self, r: dict):
        if not isinstance(r, dict):
            return
        self._health_done = int(getattr(self, "_health_done", 0)) + 1
        if r.get("available"):
            self._health_ok = int(getattr(self, "_health_ok", 0)) + 1
        key = f"{r.get('provider')}/{r.get('model')}"
        # also mirror into models table if present
        if hasattr(self, "test_results"):
            self.test_results[key] = r
            try:
                # 增量刷新命中行；未命中才回退整树重建（避免 O(N²)）
                updater = getattr(self, "update_model_row_status", None)
                if updater is None or not updater(
                    str(r.get("provider") or ""), str(r.get("model") or "")
                ):
                    self.fill_models_table()
            except Exception as e:
                logger.warning("model table refresh during health check failed: %s", e)
        try:
            self.health_refresh_table()
        except Exception as e:
            logger.warning("health table refresh failed: %s", e)
        try:
            self.history_refresh()
        except Exception as e:
            logger.warning("history refresh during health check failed: %s", e)
        done = self._health_done
        ok_n = self._health_ok
        self.status.showMessage(f"正在检查模型 {done} · 可用 {ok_n} · 刚完成 {key}")
        if hasattr(self, "health_status"):
            self.health_status.setText(f"正在检查模型 {done}（可用 {ok_n}）· 最近 {key}")

    def _on_health_fail(self, err: str):
        self._health_running = False
        self._health_worker = None
        self._set_health_cancel_enabled(False)
        QMessageBox.warning(self, "健康检查失败", err)

    def _on_health_done(self, result: dict, show_dialog: bool):
        self._health_running = False
        self._health_worker = None
        self._set_health_cancel_enabled(False)
        if not result.get("ok") and result.get("error"):
            QMessageBox.warning(self, "健康检查", str(result.get("error")))
            return
        self.health_refresh_table()
        results = result.get("results") or []
        ok_n = sum(1 for r in results if r.get("available"))
        scope = result.get("scope") or self._health_scope_value()
        cancelled = bool(result.get("cancelled"))
        verb = "已停止" if cancelled else "完成"
        msg = f"健康检查{verb}：{ok_n}/{len(results)} 可用（范围: {scope}）"
        self.status.showMessage(msg)
        if hasattr(self, "health_status"):
            self.health_status.setText(msg + f" | {result.get('health', {}).get('updated_at', '')}")
        for r in results:
            key = f"{r.get('provider')}/{r.get('model')}"
            self.test_results[key] = r
        try:
            self.fill_models_table()
            self.history_refresh()
        except Exception as e:
            logger.warning("table refresh after health check failed: %s", e)
        if show_dialog and not cancelled:
            if ok_n == 0 and scope == "favorites":
                hint = (
                    msg
                    + "\n\n提示：收藏可能是未登录的 openai-codex。可改范围「默认模型」"
                    "或「自定义 Provider」，或把可用模型加入收藏。"
                )
                show = getattr(self, "show_result", None)
                if callable(show):
                    show("健康检查", hint, tone="warning")
                else:
                    QMessageBox.information(self, "健康检查", hint)
            else:
                self.notify_success(msg)

    def health_refresh_table(self):
        if not hasattr(self, "health_table"):
            return
        data = extras.load_health()
        models = data.get("models") or {}
        self.health_table.setRowCount(len(models))
        for i, (key, info) in enumerate(sorted(models.items())):
            avail = bool(info.get("available"))
            self.health_table.setItem(i, 0, QTableWidgetItem(key))
            self.health_table.setItem(i, 1, QTableWidgetItem("可用" if avail else "不可用"))
            lat = info.get("latency_ms")
            self.health_table.setItem(i, 2, QTableWidgetItem(f"{lat:.0f} ms" if isinstance(lat, (int, float)) else "—"))
            self.health_table.setItem(i, 3, QTableWidgetItem(str(info.get("mode") or "—")))
            self.health_table.setItem(i, 4, QTableWidgetItem(str(info.get("checked_at") or "—")))
            self.health_table.setItem(i, 5, QTableWidgetItem(str(info.get("error") or "")[:160]))
        if hasattr(self, "health_status"):
            sc = data.get("last_scope") or "—"
            updated = data.get("updated_at") or ""
            stale_hint = ""
            if updated:
                try:
                    from datetime import datetime

                    updated_dt = datetime.strptime(updated, "%Y-%m-%d %H:%M:%S")
                    age_min = (datetime.now() - updated_dt).total_seconds() / 60
                    interval = int((self.mgr or {}).get("health_interval_min") or 0)
                    stale_after = max(interval * 2, 24 * 60) if interval else 24 * 60
                    if age_min > stale_after:
                        stale_hint = "（数据可能已过期，建议重新检查）"
                except Exception:
                    pass
            self.health_status.setText(f"更新于 {updated or '—'} | 上次范围 {sc}{stale_hint}")
        empty = getattr(self, "health_empty", None)
        has_rows = bool(models)
        self.health_table.setVisible(has_rows)
        if empty is not None:
            empty.setVisible(not has_rows)

    def health_add_ok_to_favorites(self):
        data = extras.load_health()
        models = data.get("models") or {}
        favs = list((self.mgr or {}).get("favorites") or [])
        n = 0
        for key, info in models.items():
            if info.get("available") and key not in favs:
                favs.append(key)
                n += 1
        self.persist_mgr(favorites=favs)
        try:
            self.fill_favorites()
        except Exception as e:
            # 吞掉这里等于「提示已收藏、界面没变」，与 R2 UI P3-16 点名的
            # 托盘切换模型同一类误导。
            logger.warning("refresh favorites after health import failed: %s", e)
        self.notify_success(f"新增 {n} 个可用模型到收藏（共 {len(favs)}）")

    def health_retest_selected(self):
        if not hasattr(self, "health_table"):
            return
        sm = self.health_table.selectionModel()
        if not sm:
            return
        pairs = []
        for idx in sm.selectedRows():
            item = self.health_table.item(idx.row(), 0)
            if not item:
                continue
            key = item.text()
            if "/" in key:
                p, m = key.split("/", 1)
                pairs.append((p, m))
        if not pairs:
            self.notify_warning("请先在健康表中选中行")
            return
        self._run_model_tests(pairs)
