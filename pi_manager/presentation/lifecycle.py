"""托盘、关闭生命周期与共用 Worker 助手。"""
from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from .. import core
from .. import extras

logger = logging.getLogger(__name__)


def _make_tray_icon(color: str = "#3d8bfd") -> QIcon:
    # Prefer branded assets; fall back to painted glyph.
    try:
        from .. import resources as res
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
    """托盘、关闭生命周期与共用 Worker 助手。从 ``ui_features.py`` 下沉。"""

    chat_history: list[dict[str, str]]
    tray: QSystemTrayIcon | None
    health_timer: QTimer | None

    def init_feature_state(self):
        self.chat_history = []
        self.tray = None
        self.health_timer = None
        try:
            extras.apply_proxy_env()
        except Exception as e:
            logger.warning("apply proxy env failed: %s", e)

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
        except Exception as e:
            # 以前静默：托盘提示「已切换」但界面没变，用户无从判断哪个是真的。
            logger.warning("refresh after tray model switch failed: %s", e)
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
        saver = getattr(self, "_save_window_geometry", None)
        if callable(saver):
            saver()
        if self.tray:
            self.tray.hide()
        self._shutdown_background_tasks()
        self._close_rpc_session()
        from PySide6.QtWidgets import QApplication

        QApplication.instance().quit()

    def _shutdown_background_tasks(self):
        """收割全部后台 Worker：请求中断 → 等待预算 → 对赖着不走的脱钩延寿。

        单一登记表（``self._workers`` / 别名 ``self.workers``）覆盖包括插件页
        在内的所有 Worker。``Worker.requestInterruption()`` 只对声明了
        ``is_cancelled`` 的 job 真正有效（见 ``ui.Worker`` 的取消契约）；对不可
        中断的任务（npm install、子进程、网络请求）预算必然超时，此时不能让
        QThread 析构于运行态——统一交给 ``detach_running_worker`` 脱离 parent
        并保持强引用到进程结束，避免 qFatal 崩溃退出。
        """
        if getattr(self, "_background_shutdown", False):
            return
        self._background_shutdown = True
        if self.health_timer:
            self.health_timer.stop()
        workers = list(getattr(self, "workers", []))
        # InstallPiDialog 等模态子对话框的 Worker 经 _adopt_worker 挂在其自身名下
        # （_init_workers + 自持登记表），不在主窗口的 workers 登记表里；安装/升级
        # Pi（npm install -g，最长 300s）期间用户经托盘退出应用时，主窗口销毁链会
        # 连带销毁运行中的 QThread → qFatal("QThread: Destroyed while thread is
        # still running") 直接崩溃退出。这里用 findChildren 收割全部后代 Worker。
        try:
            from .workers import Worker as _WorkerClass
        except Exception:  # pragma: no cover - 防御性，正常路径必有 workers 模块
            _WorkerClass = None
        if _WorkerClass is not None:
            for child in self.findChildren(_WorkerClass):
                if child not in workers:
                    workers.append(child)
        for worker in workers:
            if worker.isRunning():
                worker.requestInterruption()
        deadline = time.monotonic() + 2.5
        for worker in workers:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if worker.isRunning() and remaining:
                worker.wait(remaining)
        from .workers import detach_running_worker

        for worker in workers:
            if detach_running_worker(worker):
                # 已脱钩的 worker 不再属于本窗口的登记表：它的 finished →
                # _untrack 连接仍会在完成时触发，但窗口可能已销毁，故先移除。
                self._untrack(worker)

    def closeEvent(self, event):
        # 先落盘窗口几何：最小化到托盘也算「用户当前想要的窗口位置」。
        saver = getattr(self, "_save_window_geometry", None)
        if callable(saver):
            saver()
        # minimize to tray if enabled
        if bool((self.mgr or {}).get("minimize_to_tray", True)) and self.tray and self.tray.isVisible():
            event.ignore()
            self.hide()
            self.tray.showMessage("Pi Manager", "已最小化到托盘。右键可切换模型/启动 Pi。", QSystemTrayIcon.Information, 2000)
            return
        if self.tray:
            self.tray.hide()
        self._shutdown_background_tasks()
        self._close_rpc_session()
        event.accept()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()

    def _close_rpc_session(self):
        """Terminate the persistent pi RPC child so it does not outlive the app."""
        try:
            from .. import rpc_session

            rpc_session.reset_chat_session()
        except Exception as e:
            logger.warning("close rpc session failed: %s", e)

    def _worker_fn(self, fn):
        from .workers import Worker

        return Worker(fn)

    @contextmanager
    def _busy(self, message: str):
        """长任务的忙碌反馈：等待光标 + 状态栏文案。

        ZIP 打包/解包、PBKDF/AES、逐 provider 写系统密钥库都在主线程同步执行，
        窗口会白屏数秒。这些都是用户主动点击的一次性动作，搬进 Worker 的收益
        不抵引入跨线程状态的风险；先把「还在动」这件事说清楚。
        """
        from PySide6.QtWidgets import QApplication

        if hasattr(self, "status"):
            self.status.showMessage(message)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            yield
        finally:
            QApplication.restoreOverrideCursor()
