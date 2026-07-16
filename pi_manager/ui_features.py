# -*- coding: utf-8 -*-
"""UI feature mixins: tray, health, history, proxy, export, self-check, sessions, chat."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QIcon, QPixmap, QPainter, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QTabWidget,
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
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QSystemTrayIcon,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QGroupBox,
)

from . import core
from . import extras
from . import help_docs


def _make_tray_icon(color: str = "#3d8bfd") -> QIcon:
    # Prefer branded assets; fall back to painted glyph.
    try:
        from . import resources as res
        for path in res.icon_candidates():
            if path.suffix.lower() in {".png", ".ico", ".svg"}:
                icon = QIcon(str(path))
                if not icon.isNull():
                    return icon
    except Exception:
        pass
    pm = QPixmap(64, 64)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setPen(QColor("#ffffff"))
    font = p.font()
    font.setBold(True)
    font.setPointSize(22)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignCenter, "Pi")
    p.end()
    return QIcon(pm)


def app_icon() -> QIcon:
    return _make_tray_icon()


class FeatureMixin:
    """Mixed into MainWindow."""

    chat_history: list[dict[str, str]]
    tray: QSystemTrayIcon | None
    health_timer: QTimer | None

    def init_feature_state(self):
        self.chat_history = []
        self.tray = None
        self.health_timer = None
        try:
            extras.apply_proxy_env()
        except Exception:
            pass

    def setup_system_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(_make_tray_icon())
        self.tray.setToolTip("Pi Manager")
        menu = QMenu()
        act_show = QAction("显示主窗口", self)
        act_show.triggered.connect(self.show_from_tray)
        menu.addAction(act_show)
        act_launch = QAction("启动完整 Pi（默认模型）", self)
        act_launch.triggered.connect(self.launch_default)
        menu.addAction(act_launch)
        menu.addSeparator()
        self.tray_fav_menu = menu.addMenu("切换默认模型")
        self.rebuild_tray_favorites()
        menu.addSeparator()
        act_health = QAction("运行健康检查", self)
        act_health.triggered.connect(self.health_run_now)
        menu.addAction(act_health)
        act_quit = QAction("退出", self)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()
        self._setup_health_timer()

    def rebuild_tray_favorites(self):
        if not hasattr(self, "tray_fav_menu") or self.tray_fav_menu is None:
            return
        self.tray_fav_menu.clear()
        favs = list((self.mgr or {}).get("favorites") or [])
        if not favs:
            a = QAction("（无收藏，请先在模型页收藏）", self)
            a.setEnabled(False)
            self.tray_fav_menu.addAction(a)
            return
        for key in favs:
            act = QAction(key, self)
            act.triggered.connect(lambda checked=False, k=key: self._tray_switch_model(k))
            self.tray_fav_menu.addAction(act)

    def _tray_switch_model(self, key: str):
        if "/" not in key:
            return
        provider, model = key.split("/", 1)
        core.set_default_model(provider.strip(), model.strip())
        try:
            self.refresh_dashboard()
            self.settings_load()
        except Exception:
            pass
        if self.tray:
            self.tray.showMessage("Pi Manager", f"已切换默认：{key}", QSystemTrayIcon.Information, 2500)
        self.status.showMessage(f"托盘切换默认模型：{key}")

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.DoubleClick:
            self.show_from_tray()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def quit_app(self):
        if self.tray:
            self.tray.hide()
        self._shutdown_background_tasks()
        from PySide6.QtWidgets import QApplication

        QApplication.instance().quit()

    def _shutdown_background_tasks(self):
        if getattr(self, "_background_shutdown", False):
            return
        self._background_shutdown = True
        if self.health_timer:
            self.health_timer.stop()
        workers = list(getattr(self, "workers", []))
        for worker in workers:
            if worker.isRunning():
                worker.requestInterruption()
        deadline = time.monotonic() + 2.5
        for worker in workers:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if worker.isRunning() and remaining:
                worker.wait(remaining)
        # Do not terminate Python threads: running calls finish cooperatively.
        # QThreads are parented/tracked and their finished signals remove them.

    def closeEvent(self, event):
        # minimize to tray if enabled
        if bool((self.mgr or {}).get("minimize_to_tray", True)) and self.tray and self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage("Pi Manager", "已最小化到托盘。右键可切换模型/启动 Pi。", QSystemTrayIcon.Information, 2000)
            return
        if self.tray:
            self.tray.hide()
        self._shutdown_background_tasks()
        event.accept()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()

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

    # ---- tabs ----
    def _build_health_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        tip = QLabel("批量巡检可用性/延迟。启动时会自动加载上次结果；点「立即健康检查」才会重新探测。范围若选「收藏」，只会测收藏里的模型（OAuth 未登录会失败）。")
        tip.setObjectName("subtitle")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.health_scope = QComboBox()
        self.health_scope.addItem("收藏列表", "favorites")
        self.health_scope.addItem("默认模型", "default")
        self.health_scope.addItem("自定义 Provider", "custom")
        self.health_scope.addItem("全部已加载模型", "all_listed")
        self.health_scope.addItem("模型页当前选中", "selected")
        # default to custom if user mostly uses custom providers
        self.health_scope.setCurrentIndex(2)
        row.addWidget(QLabel("检查范围"))
        row.addWidget(self.health_scope)
        row.addWidget(self._btn("立即健康检查", self.health_run_now, success=True))
        row.addWidget(self._btn("刷新显示", self.health_refresh_table, secondary=True))
        self.health_interval = QSpinBox()
        self.health_interval.setRange(0, 1440)
        self.health_interval.setSuffix(" 分钟（0=关）")
        self.health_interval.setValue(int((self.mgr or {}).get("health_interval_min") or 0))
        row.addWidget(QLabel("定时"))
        row.addWidget(self.health_interval)
        row.addWidget(self._btn("保存定时", self.health_save_interval, secondary=True))
        row.addStretch(1)
        layout.addLayout(row)
        self.health_table = QTableWidget(0, 6)
        self.health_table.setHorizontalHeaderLabels(["模型", "状态", "延迟", "方式", "检查时间", "错误/预览"])
        self.health_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        if hasattr(self, "_polish_table"):
            self._polish_table(self.health_table)
        else:
            self.health_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.health_table.setSelectionBehavior(QAbstractItemView.SelectRows)
            self.health_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
            self.health_table.setShowGrid(False)
            self.health_table.setAlternatingRowColors(True)
            self.health_table.verticalHeader().setVisible(False)
        layout.addWidget(self.health_table, 1)
        brow = QHBoxLayout()
        brow.setSpacing(8)
        brow.addWidget(self._btn("将可用项加入收藏", self.health_add_ok_to_favorites, success=True))
        brow.addWidget(self._btn("重测表格选中", self.health_retest_selected, secondary=True))
        brow.addStretch(1)
        layout.addLayout(brow)
        self.health_status = QLabel("尚未检查 — 建议范围选「自定义 Provider」或「默认模型」")
        self.health_status.setObjectName("subtitle")
        self.health_status.setWordWrap(True)
        layout.addWidget(self.health_status)
        return w

    def _build_help_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.setSpacing(8)
        help_title = QLabel("使用教程（分类 Tab）· 内置 Markdown")
        help_title.setObjectName("sectionTitle")
        top.addWidget(help_title)
        top.addStretch(1)
        top.addWidget(self._btn("复制全部 Markdown", self.help_copy_md, secondary=True))
        top.addWidget(self._btn("导出为 .md 文件", self.help_export_md, secondary=True))
        layout.addLayout(top)

        self.help_tabs = QTabWidget()
        self.help_browsers: list[QTextBrowser] = []
        self._help_section_mds: list[str] = []
        sections = help_docs.help_sections()
        mode = "night"
        try:
            mode = str(core.get_ui_theme().get("mode") or "night")
        except Exception:
            pass
        for title, md in sections:
            page = QWidget()
            pl = QVBoxLayout(page)
            pl.setContentsMargins(4, 8, 4, 4)
            browser = QTextBrowser()
            browser.setOpenExternalLinks(True)
            browser.setHtml(help_docs.help_section_html(md, mode=mode))
            pl.addWidget(browser, 1)
            self.help_browsers.append(browser)
            self._help_section_mds.append(md)
            self.help_tabs.addTab(page, title)
        # keep first browser as help_browser for any legacy refs
        self.help_browser = self.help_browsers[0] if self.help_browsers else QTextBrowser()
        layout.addWidget(self.help_tabs, 1)
        return w

    def refresh_help_theme(self, mode: str | None = None) -> None:
        """昼夜切换后重渲帮助 HTML，避免白天模式浅底深色字看不清。"""
        if not getattr(self, "help_browsers", None):
            return
        if mode is None:
            try:
                mode = str(core.get_ui_theme().get("mode") or "night")
            except Exception:
                mode = "night"
        mds = getattr(self, "_help_section_mds", None) or []
        if not mds:
            mds = [md for _, md in help_docs.help_sections()]
            self._help_section_mds = mds
        for browser, md in zip(self.help_browsers, mds):
            try:
                browser.setHtml(help_docs.help_section_html(md, mode=mode))
            except Exception:
                pass

    def help_copy_md(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(help_docs.HELP_MARKDOWN)
        self.status.showMessage("已复制教程 Markdown 到剪贴板")

    def help_export_md(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出使用教程", str(Path.home() / "PiManager-使用教程.md"), "Markdown (*.md)"
        )
        if not path:
            return
        Path(path).write_text(help_docs.HELP_MARKDOWN, encoding="utf-8")
        QMessageBox.information(self, "已导出", path)

    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        hist_tip = QLabel("模型测试历史（启动时自动加载本地记录，也可手动刷新）")
        hist_tip.setObjectName("subtitle")
        hist_tip.setWordWrap(True)
        layout.addWidget(hist_tip)
        filt = QHBoxLayout()
        filt.setSpacing(8)
        self.history_filter = QLineEdit()
        self.history_filter.setPlaceholderText("过滤 provider/model…")
        self.history_filter.setMinimumHeight(34)
        try:
            self.history_filter.setClearButtonEnabled(True)
        except Exception:
            pass
        self.history_filter.textChanged.connect(self.history_refresh)
        filt.addWidget(self.history_filter, 1)
        filt.addWidget(self._btn("刷新", self.history_refresh, secondary=True))
        filt.addWidget(self._btn("清空历史", self.history_clear, danger=True))
        layout.addLayout(filt)
        self.history_table = QTableWidget(0, 6)
        self.history_table.setHorizontalHeaderLabels(["时间", "模型", "可用", "延迟", "方式", "错误/预览"])
        self.history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        if hasattr(self, "_polish_table"):
            self._polish_table(self.history_table)
        else:
            self.history_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.history_table.setShowGrid(False)
            self.history_table.setAlternatingRowColors(True)
            self.history_table.verticalHeader().setVisible(False)
        layout.addWidget(self.history_table, 1)
        return w

    def _build_tools_tab(self) -> QWidget:
        """Self-check + export/import + secure keys."""
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        box1 = QGroupBox("启动自检")
        l1 = QVBoxLayout(box1)
        l1.setSpacing(10)
        l1.addWidget(self._btn("运行自检", self.self_check_run, success=True))
        self.selfcheck_table = QTableWidget(0, 3)
        self.selfcheck_table.setHorizontalHeaderLabels(["项目", "状态", "详情"])
        self.selfcheck_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        if hasattr(self, "_polish_table"):
            self._polish_table(self.selfcheck_table)
        else:
            self.selfcheck_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
            self.selfcheck_table.setShowGrid(False)
            self.selfcheck_table.setAlternatingRowColors(True)
            self.selfcheck_table.verticalHeader().setVisible(False)
        l1.addWidget(self.selfcheck_table)
        layout.addWidget(box1, 1)

        box2 = QGroupBox("配置导入/导出")
        l2 = QHBoxLayout(box2)
        l2.setSpacing(8)
        l2.addWidget(self._btn("导出配置包", self.export_config, success=True))
        l2.addWidget(self._btn("导出（含密钥）", self.export_config_with_secrets, secondary=True))
        l2.addWidget(self._btn("导入配置包", self.import_config))
        l2.addWidget(self._btn("加密现有明文 Key", self.secure_keys_now, secondary=True))
        l2.addStretch(1)
        layout.addWidget(box2)

        box3 = QGroupBox("Pi Manager 更新")
        l3 = QVBoxLayout(box3)
        l3.setSpacing(10)
        self.mgr_version_lbl = QLabel(f"当前版本：v{extras.APP_VERSION}")
        l3.addWidget(self.mgr_version_lbl)
        tip_upd = QLabel(
            "默认从 GitHub Releases 检查更新；也可自定义版本清单 URL（JSON: version/notes/url）。"
            "检测到新版本可下载安装包，需手动替换运行中的程序。"
        )
        tip_upd.setObjectName("subtitle")
        tip_upd.setWordWrap(True)
        l3.addWidget(tip_upd)
        row = QHBoxLayout()
        row.setSpacing(8)
        self.update_url_edit = QLineEdit(str((self.mgr or {}).get("update_manifest_url") or ""))
        self.update_url_edit.setPlaceholderText("可选：自定义 manifest URL（留空=GitHub Releases）")
        row.addWidget(self.update_url_edit, 1)
        row.addWidget(self._btn("检查更新", self.check_manager_update, success=True))
        l3.addLayout(row)
        self.update_status = QLabel("")
        self.update_status.setObjectName("subtitle")
        self.update_status.setWordWrap(True)
        l3.addWidget(self.update_status)
        self._last_manager_update: dict = {}
        layout.addWidget(box3)
        return w

    def enhance_sessions_tab_widgets(self, layout: QVBoxLayout):
        # called if we rebuild sessions - instead patch methods only
        pass

    # ---- health ----
    def health_save_interval(self):
        self.mgr["health_interval_min"] = int(self.health_interval.value())
        self.persist_mgr()
        self._setup_health_timer()
        self.status.showMessage("健康检查定时已保存")

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
        self.status.showMessage("健康检查进行中（逐项实时更新）…")
        if hasattr(self, "health_status"):
            self.health_status.setText("健康检查中：0 完成 …")

        from .ui import BatchTestWorker

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
        w.start()

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
                self.fill_models_table()
            except Exception:
                pass
        try:
            self.health_refresh_table()
        except Exception:
            pass
        try:
            self.history_refresh()
        except Exception:
            pass
        done = self._health_done
        ok_n = self._health_ok
        self.status.showMessage(f"健康检查 {done} 完成 · 可用 {ok_n} · 刚完成 {key}")
        if hasattr(self, "health_status"):
            self.health_status.setText(f"进行中：已完成 {done}（可用 {ok_n}）· 最近 {key}")

    def _on_health_fail(self, err: str):
        self._health_running = False
        QMessageBox.warning(self, "健康检查失败", err)

    def _worker_fn(self, fn):
        from .ui import Worker

        return Worker(fn)

    def _on_health_done(self, result: dict, show_dialog: bool):
        self._health_running = False
        if not result.get("ok") and result.get("error"):
            QMessageBox.warning(self, "健康检查", str(result.get("error")))
            return
        self.health_refresh_table()
        results = result.get("results") or []
        ok_n = sum(1 for r in results if r.get("available"))
        scope = result.get("scope") or self._health_scope_value()
        msg = f"健康检查完成：{ok_n}/{len(results)} 可用（范围: {scope}）"
        self.status.showMessage(msg)
        if hasattr(self, "health_status"):
            self.health_status.setText(msg + f" | {result.get('health', {}).get('updated_at', '')}")
        for r in results:
            key = f"{r.get('provider')}/{r.get('model')}"
            self.test_results[key] = r
        try:
            self.fill_models_table()
            self.history_refresh()
        except Exception:
            pass
        if show_dialog:
            hint = ""
            if ok_n == 0 and scope == "favorites":
                hint = "\n\n提示：收藏可能是未登录的 openai-codex。可改范围「默认模型」或「自定义 Provider」，或把可用模型加入收藏。"
            QMessageBox.information(self, "健康检查", msg + hint)

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
            self.health_status.setText(f"更新于 {data.get('updated_at') or '—'} | 上次范围 {sc}")

    def health_add_ok_to_favorites(self):
        data = extras.load_health()
        models = data.get("models") or {}
        favs = list((self.mgr or {}).get("favorites") or [])
        n = 0
        for key, info in models.items():
            if info.get("available") and key not in favs:
                favs.append(key)
                n += 1
        self.mgr["favorites"] = favs
        self.persist_mgr()
        try:
            self.fill_favorites()
        except Exception:
            pass
        QMessageBox.information(self, "收藏", f"新增 {n} 个可用模型到收藏（共 {len(favs)}）")

    def health_retest_selected(self):
        if not hasattr(self, "health_table"):
            return
        pairs = []
        for idx in self.health_table.selectionModel().selectedRows():
            item = self.health_table.item(idx.row(), 0)
            if not item:
                continue
            key = item.text()
            if "/" in key:
                p, m = key.split("/", 1)
                pairs.append((p, m))
        if not pairs:
            QMessageBox.information(self, "提示", "请先在健康表中选中行")
            return
        self._run_model_tests(pairs)

    # ---- history ----
    def history_refresh(self):
        if not hasattr(self, "history_table"):
            return
        q = (self.history_filter.text() if hasattr(self, "history_filter") else "") or ""
        q = q.lower().strip()
        rows = extras.load_history()
        if q:
            rows = [r for r in rows if q in f"{r.get('provider')}/{r.get('model')}".lower()]
        rows = list(reversed(rows[-200:]))
        self.history_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.history_table.setItem(i, 0, QTableWidgetItem(str(r.get("time") or "")))
            self.history_table.setItem(i, 1, QTableWidgetItem(f"{r.get('provider')}/{r.get('model')}"))
            self.history_table.setItem(i, 2, QTableWidgetItem("是" if r.get("available") else "否"))
            lat = r.get("latency_ms")
            self.history_table.setItem(i, 3, QTableWidgetItem(f"{lat:.0f}" if isinstance(lat, (int, float)) else "—"))
            self.history_table.setItem(i, 4, QTableWidgetItem(str(r.get("mode") or "")))
            extra = r.get("error") or r.get("preview") or ""
            self.history_table.setItem(i, 5, QTableWidgetItem(str(extra)[:120]))

    def history_clear(self):
        if QMessageBox.question(self, "确认", "清空全部测试历史？") != QMessageBox.Yes:
            return
        extras.save_history([])
        self.history_refresh()

    # ---- self check / export ----
    def self_check_run(self):
        def job():
            return extras.run_self_check()

        w = self._track(self._worker_fn(job))
        w.done.connect(self._on_selfcheck_done)
        w.failed.connect(lambda e: QMessageBox.warning(self, "自检失败", e))
        w.start()
        self.status.showMessage("正在自检…")

    def _on_selfcheck_done(self, checks: list):
        if not hasattr(self, "selfcheck_table"):
            return
        self.selfcheck_table.setRowCount(len(checks))
        for i, c in enumerate(checks):
            ok = bool(c.get("ok"))
            self.selfcheck_table.setItem(i, 0, QTableWidgetItem(str(c.get("name"))))
            self.selfcheck_table.setItem(i, 1, QTableWidgetItem("通过" if ok else "注意"))
            self.selfcheck_table.setItem(i, 2, QTableWidgetItem(str(c.get("detail") or "")))
        bad = sum(1 for c in checks if not c.get("ok"))
        self.status.showMessage(f"自检完成：{len(checks) - bad}/{len(checks)} 通过")

    def export_config(self):
        path, _ = QFileDialog.getSaveFileName(self, "导出配置", str(Path.home() / "pi-manager-config.zip"), "ZIP (*.zip)")
        if not path:
            return
        try:
            out = extras.export_config_bundle(path, include_secrets=False)
            QMessageBox.information(self, "已导出", f"已导出到：\n{out}\n（密钥已脱敏/占位）")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def export_config_with_secrets(self):
        if QMessageBox.question(self, "确认", "将导出包含 API Key 的配置包，请妥善保管。继续？") != QMessageBox.Yes:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出配置（含密钥）", str(Path.home() / "pi-manager-config-secrets.zip"), "ZIP (*.zip)")
        if not path:
            return
        password, ok = QInputDialog.getText(
            self,
            "设置密钥包密码",
            "请输入至少 10 个字符的密码：",
            QLineEdit.Password,
        )
        if not ok:
            return
        confirm, ok = QInputDialog.getText(
            self,
            "确认密钥包密码",
            "请再次输入密码：",
            QLineEdit.Password,
        )
        if not ok or password != confirm:
            QMessageBox.warning(self, "导出失败", "两次密码不一致")
            return
        try:
            out = extras.export_config_bundle(path, include_secrets=True, password=password)
            QMessageBox.information(self, "已导出", f"已导出到：\n{out}")
        except Exception as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def import_config(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入配置包", str(Path.home()), "ZIP (*.zip)")
        if not path:
            return
        restore_secrets = extras.bundle_contains_secrets(path) and (
            QMessageBox.question(self, "密钥", "配置包包含加密密钥，是否恢复？") == QMessageBox.Yes
        )
        password = ""
        if restore_secrets:
            password, ok = QInputDialog.getText(
                self,
                "输入密钥包密码",
                "请输入导出时设置的密码：",
                QLineEdit.Password,
            )
            if not ok:
                return
        res = extras.import_config_bundle(
            path,
            restore_secrets=restore_secrets,
            password=password,
        )
        if not res.get("ok"):
            QMessageBox.critical(self, "导入失败", str(res.get("error")))
            return
        self.mgr = core.load_manager_config()
        self.refresh_all()
        self.settings_load()
        QMessageBox.information(self, "导入成功", "已恢复：\n" + "\n".join(res.get("restored") or []))

    def secure_keys_now(self):
        res = extras.secure_existing_keys()
        QMessageBox.information(
            self,
            "加密完成",
            f"已处理 provider 明文 Key。\n密钥库条目：{len(res.get('secrets') or [])}",
        )
        self.refresh_providers()

    def check_manager_update(self, silent: bool = False):
        if hasattr(self, "update_url_edit"):
            url = self.update_url_edit.text().strip()
            self.mgr["update_manifest_url"] = url
            self.persist_mgr()

        def job():
            return extras.check_manager_update()

        w = self._track(self._worker_fn(job))
        w.done.connect(lambda res: self._on_mgr_update(res, silent=silent))
        w.failed.connect(
            lambda e: (
                self.status.showMessage(f"检查更新失败: {e}")
                if silent
                else QMessageBox.warning(self, "检查失败", e)
            )
        )
        w.start()

    def _on_mgr_update(self, res: dict, silent: bool = False):
        self._last_manager_update = dict(res or {})
        msg = res.get("message") or ""
        if hasattr(self, "update_status"):
            self.update_status.setText(msg)
        if hasattr(self, "mgr_version_lbl"):
            remote = res.get("remote") or ""
            if remote:
                self.mgr_version_lbl.setText(
                    f"当前版本：v{extras.APP_VERSION}  ·  远程：v{remote}"
                )
            else:
                self.mgr_version_lbl.setText(f"当前版本：v{extras.APP_VERSION}")
        self.status.showMessage(msg)

        if not res.get("has_update"):
            if not silent:
                QMessageBox.information(self, "更新检查", msg)
            return

        notes = str(res.get("notes") or "").strip()
        notes_short = (notes[:500] + "…") if len(notes) > 500 else notes
        body = msg
        if notes_short:
            body += f"\n\n更新说明：\n{notes_short}"
        body += "\n\n签名更新链完成前已禁用自动下载和原地安装，请从官方 Release 页面手动更新。"

        box = QMessageBox(self)
        box.setWindowTitle("发现 Pi Manager 新版本")
        box.setIcon(QMessageBox.Information)
        box.setText(body)
        btn_open = box.addButton("打开 Release 页", QMessageBox.ActionRole)
        box.addButton("稍后", QMessageBox.RejectRole)
        box.exec()
        clicked = box.clickedButton()
        if clicked == btn_open:
            page = str(res.get("url") or extras.GITHUB_RELEASES_PAGE)
            try:
                from PySide6.QtGui import QDesktopServices
                from PySide6.QtCore import QUrl

                QDesktopServices.openUrl(QUrl(page))
            except Exception:
                core.open_path(page)

    def _download_manager_update(self, res: dict | None = None, apply_inplace: bool = False):
        res = res or getattr(self, "_last_manager_update", {}) or {}
        url = str(res.get("download") or res.get("url") or "").strip()
        if not url:
            QMessageBox.information(self, "提示", "没有可用的下载地址")
            return
        self.status.showMessage("正在下载更新包…")

        def job():
            return extras.download_manager_update(url)

        w = self._track(self._worker_fn(job))

        def _done(r: dict):
            path = str((r or {}).get("path") or "")
            self.status.showMessage((r or {}).get("message") or "下载完成")
            if apply_inplace and path:
                self._apply_manager_update_inplace(path)
                return
            ret = QMessageBox.question(
                self,
                "下载完成",
                f"{(r or {}).get('message') or path}\n\n"
                "可「立即更新并重启」（推荐打包版），或打开文件夹手动替换。\n是否打开所在文件夹？",
            )
            if ret == QMessageBox.Yes and path:
                core.open_in_explorer(path)

        w.done.connect(_done)
        w.failed.connect(lambda e: QMessageBox.warning(self, "下载失败", e))
        w.start()

    def _apply_manager_update_inplace(self, archive_path: str):
        try:
            out = extras.apply_manager_update_inplace(archive_path)
        except Exception as e:
            QMessageBox.warning(self, "更新失败", str(e))
            return
        if not out.get("ok"):
            QMessageBox.information(self, "无法自动更新", out.get("message") or "请手动替换安装包")
            if out.get("source"):
                try:
                    core.open_in_explorer(str(out.get("source")))
                except Exception:
                    pass
            return
        QMessageBox.information(
            self,
            "即将重启更新",
            out.get("message") or "程序将退出，更新器会覆盖安装目录并重新启动。",
        )
        # 退出，让外部脚本完成覆盖
        try:
            from PySide6.QtWidgets import QApplication

            QApplication.instance().quit()
        except Exception:
            import sys

            sys.exit(0)

    # ---- sessions extras ----
    def session_selected_path(self) -> str | None:
        rows = self.sessions_table.selectionModel().selectedRows()
        if not rows:
            return None
        r = rows[0].row()
        if hasattr(self, "_session_path_at"):
            return self._session_path_at(r)
        item = self.sessions_table.item(r, 0)
        if item and item.data(Qt.UserRole):
            return str(item.data(Qt.UserRole))
        legacy = self.sessions_table.item(r, 2)
        return legacy.text() if legacy else None

    def session_delete(self):
        path = self.session_selected_path()
        if not path:
            QMessageBox.information(self, "提示", "请先选择会话")
            return
        if QMessageBox.question(self, "确认删除", f"删除会话文件？\n{path}") != QMessageBox.Yes:
            return
        if extras.session_delete(path):
            self.refresh_sessions()
            self.status.showMessage("会话已删除")
        else:
            QMessageBox.warning(self, "失败", "无法删除")

    def session_rename(self):
        path = self.session_selected_path()
        if not path:
            QMessageBox.information(self, "提示", "请先选择会话")
            return
        name, ok = QInputDialog.getText(self, "重命名", "新文件名：", text=Path(path).name)
        if not ok or not name.strip():
            return
        try:
            newp = extras.session_rename(path, name.strip())
            self.refresh_sessions()
            self.status.showMessage(f"已重命名为 {newp}")
        except Exception as e:
            QMessageBox.warning(self, "重命名失败", str(e))

    def sessions_apply_filter(self):
        wd = self.session_filter_wd.text().strip() if hasattr(self, "session_filter_wd") else ""
        nm = self.session_filter_name.text().strip() if hasattr(self, "session_filter_name") else ""
        rows = extras.list_sessions_filtered(limit=100, workdir_substr=wd, name_substr=nm)
        if hasattr(self, "_fill_sessions_table"):
            self._fill_sessions_table(rows)
            return
        self.sessions_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.sessions_table.setItem(i, 0, QTableWidgetItem(r.get("project") or r.get("name") or ""))
            self.sessions_table.setItem(i, 1, QTableWidgetItem(r.get("cwd") or r.get("folder") or ""))
            self.sessions_table.setItem(i, 2, QTableWidgetItem(r.get("model") or r.get("path") or ""))

    # ---- chat multi-turn (context via prompt assembly) ----
    def chat_clear_history(self):
        self.chat_history = []
        if hasattr(self, "chat_output"):
            self.chat_output.setPlainText("")
        self.status.showMessage("已清空对话历史")

    def chat_send_enhanced(self):
        prompt = self.chat_input.toPlainText().strip()
        if not prompt:
            return
        if hasattr(self, "_chat_combo_text"):
            provider = self._chat_combo_text(self.chat_provider) or None
            model = self._chat_combo_text(self.chat_model) or None
        else:
            provider = self.chat_provider.currentText().strip() if hasattr(self.chat_provider, "currentText") else self.chat_provider.text().strip()
            model = self.chat_model.currentText().strip() if hasattr(self.chat_model, "currentText") else self.chat_model.text().strip()
            provider = provider or None
            model = model or None
        # Keep the request context within both turn and byte budgets.
        history_lines = []
        context_bytes = 0
        for turn in reversed(self.chat_history[-6:]):
            lines = [
                f"User: {turn.get('user', '')}",
                f"Assistant: {turn.get('assistant', '')}",
            ]
            size = len("\n".join(lines).encode("utf-8"))
            if context_bytes + size > 128 * 1024:
                break
            history_lines[0:0] = lines
            context_bytes += size
        if history_lines:
            full = "以下是近期对话，请承接上下文简要回答。\n" + "\n".join(history_lines) + f"\nUser: {prompt}\nAssistant:"
        else:
            full = prompt
        encoded = full.encode("utf-8")
        if len(encoded) > 128 * 1024:
            full = encoded[-128 * 1024 :].decode("utf-8", errors="ignore")
        self.chat_output.appendPlainText(f"\n你: {prompt}\n…思考中…")
        self.chat_input.setEnabled(False)
        workdir = self.workdir_edit.text().strip() or str(core.user_home())
        thinking = "off"
        try:
            thinking = self.thinking_combo.currentText() or "off"
        except Exception:
            pass

        def job():
            # 连续失败达阈值后自动切换下一个收藏/启用模型并重试（无感）
            return extras.chat_with_failover(
                full,
                provider=provider,
                model=model,
                workdir=workdir,
                thinking=thinking,
            )

        w = self._track(self._worker_fn(job))
        w.done.connect(lambda r, u=prompt: self._on_enhanced_chat_done(r, u))
        w.failed.connect(self._on_enhanced_chat_fail)
        w.start()

    def _on_enhanced_chat_done(self, result: dict, user_prompt: str):
        self.chat_input.setEnabled(True)
        text = (result.get("stdout") or "").strip() or (result.get("stderr") or "").strip()
        p = result.get("provider") or ""
        m = result.get("model") or ""
        # 若发生故障切换，同步 UI 下拉与默认，但不刷屏打扰
        if result.get("switched") and p and m:
            try:
                if hasattr(self, "_set_chat_combo_text"):
                    self._set_chat_combo_text(self.chat_provider, str(p))
                    self._reload_chat_models_for_provider(str(p), prefer_model=str(m))
                    self._set_chat_combo_text(self.chat_model, str(m))
                self.refresh_dashboard()
                self.settings_load()
            except Exception:
                pass
            notice = (result.get("notice") or "").strip()
            if notice:
                self.chat_output.appendPlainText(f"[{notice}]")
            else:
                # 无感：仅状态栏轻提示
                self.status.showMessage(f"已自动切换模型 → {p}/{m}", 5000)
        if not result.get("ok"):
            err = (result.get("error") or text or "未知错误")[:500]
            self.chat_output.appendPlainText(f"失败({result.get('returncode')}): {err}")
            return
        self.chat_history.append({"user": user_prompt, "assistant": text})
        self.chat_history = self.chat_history[-20:]
        while self.chat_history and len(
            json.dumps(self.chat_history, ensure_ascii=False).encode("utf-8")
        ) > 512 * 1024:
            self.chat_history.pop(0)
        lat = result.get("latency_ms")
        tag = f"{p}/{m} · {lat} ms" if p and m else f"{lat} ms"
        self.chat_output.appendPlainText(f"Pi ({tag}):\n{text}\n")

    def _on_enhanced_chat_fail(self, err: str):
        self.chat_input.setEnabled(True)
        self.chat_output.appendPlainText(f"错误: {err}")

    # ---- settings helpers for proxy etc ----
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
        if hasattr(self, "proxy_enabled"):
            self.mgr["proxy_enabled"] = self.proxy_enabled.isChecked()
        if hasattr(self, "proxy_url"):
            self.mgr["proxy_url"] = self.proxy_url.text().strip()
        if hasattr(self, "test_concurrency"):
            self.mgr["test_concurrency"] = int(self.test_concurrency.value())
        if hasattr(self, "failover_enabled"):
            self.mgr["failover_enabled"] = self.failover_enabled.isChecked()
        if hasattr(self, "failover_threshold"):
            self.mgr["failover_fail_threshold"] = int(self.failover_threshold.value())
        if hasattr(self, "failover_silent"):
            self.mgr["failover_silent"] = self.failover_silent.isChecked()
        if hasattr(self, "minimize_to_tray"):
            self.mgr["minimize_to_tray"] = self.minimize_to_tray.isChecked()
        if hasattr(self, "start_minimized"):
            self.mgr["start_minimized"] = self.start_minimized.isChecked()
        if hasattr(self, "secure_keys_chk"):
            self.mgr["secure_keys"] = self.secure_keys_chk.isChecked()
        if hasattr(self, "update_url_edit"):
            self.mgr["update_manifest_url"] = self.update_url_edit.text().strip()
        self.persist_mgr()
        extras.set_proxy_settings(bool(self.mgr.get("proxy_enabled")), str(self.mgr.get("proxy_url") or ""))
        extras.set_test_concurrency(int(self.mgr.get("test_concurrency") or 3))
        self._setup_health_timer()
        self.rebuild_tray_favorites()
