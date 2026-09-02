"""窗口壳层：几何、主题缓存、启动检查，以及 MainWindow 组装。"""
from __future__ import annotations

import logging
import time
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QMainWindow,
    QMessageBox,
    QTableWidget,
    QTreeWidget,
)

from .. import core
from .design import normalize_mode
from .dialogs.setup import InstallPiDialog, SetupWizardDialog
from .lifecycle import FeatureMixin, app_icon
from .pages.chat import ChatPageMixin
from .pages.dashboard import DashboardPageMixin
from .pages.diagnostics import DiagnosticsPageMixin
from .pages.help import HelpPageMixin
from .pages.models import ModelsPageMixin
from .pages.providers import ProvidersPageMixin
from .pages.sessions import SessionsPageMixin
from .pages.settings import SettingsPageMixin
from .workers import Worker, WorkerTrackerMixin

logger = logging.getLogger(__name__)


class ShellMixin:
    """窗口壳层：几何、表格抛光、主题缓存、启动与 Pi 更新。从 ``ui.py`` 下沉。"""

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
        from .design import tokens_for

        cached = getattr(self, "_table_colors_cache", None)
        if cached is not None:
            colors, stamp = cached
            if time.monotonic() - stamp < self.THEME_CACHE_TTL:
                return colors
        theme = core.get_ui_theme()
        colors = tokens_for(theme.get("mode"), theme.get("accent"))
        self._table_colors_cache = (colors, time.monotonic())
        return colors

    def _adopt_worker(self, worker: Worker) -> None:
        # MainWindow 是顶层窗口：不设 parent，关闭即应用退出。
        pass

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


class MainWindow(WorkerTrackerMixin, FeatureMixin, ShellMixin, SessionsPageMixin, ProvidersPageMixin, ModelsPageMixin, ChatPageMixin, DashboardPageMixin, SettingsPageMixin, DiagnosticsPageMixin, HelpPageMixin, QMainWindow):
    def __init__(self, *, start_background: bool = True):
        """Create the window.

        ``start_background=False`` is intentionally supported for offscreen UI
        tests and embedders: construction then has no network workers, tray icon,
        update prompt, or startup timer side effects.
        """
        super().__init__()
        self.setWindowTitle("Pi Manager — 简化配置 · 跨平台 Pi 启动器")
        try:
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
