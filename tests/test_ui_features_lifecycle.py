# -*- coding: utf-8 -*-
"""托盘/生命周期：健康检查 QTimer 与后台线程收割（offscreen）。

断言基于公开行为：健康检查 QTimer 的创建/启停、_shutdown_background_tasks
停止定时器并中断 worker、closeEvent 走关闭路径不泄漏线程。
"""
from __future__ import annotations

import os
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import QApplication

from pi_manager import core
from pi_manager.presentation.main_window import ModernMainWindow
from pi_manager.ui import Worker


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def window(qapp, isolated_home):
    w = ModernMainWindow(start_background=False)
    yield w
    w._shutdown_background_tasks()
    w.hide()
    w.deleteLater()
    qapp.processEvents()


def _apply_health_interval(window, minutes: int) -> None:
    window.mgr["health_interval_min"] = minutes
    window._setup_health_timer()


# ---- 健康检查 QTimer 启停 ------------------------------------------------

def test_health_timer_starts_when_interval_configured(window):
    _apply_health_interval(window, 1)
    assert window.health_timer is not None
    assert window.health_timer.isActive() is True
    assert window.health_timer.interval() == 60 * 1000


def test_health_timer_disabled_at_zero_interval(window):
    _apply_health_interval(window, 0)
    assert window.health_timer is None


def test_health_timer_restart_replaces_previous(window):
    _apply_health_interval(window, 5)
    first = window.health_timer
    assert first is not None and first.isActive()
    _apply_health_interval(window, 10)
    assert window.health_timer is not first
    assert window.health_timer.interval() == 10 * 60 * 1000


def test_health_interval_saved_through_settings_fields(window, qapp):
    # 公开路径：设置页字段 → save → timer 重启
    window.health_interval.setValue(3)
    window.health_save_interval()
    qapp.processEvents()
    assert core.load_manager_config()["health_interval_min"] == 3
    assert window.health_timer is not None and window.health_timer.isActive()
    assert window.health_timer.interval() == 3 * 60 * 1000


# ---- _shutdown_background_tasks 生命周期 ----------------------------------

def test_shutdown_stops_health_timer_and_is_idempotent(window):
    _apply_health_interval(window, 1)
    assert window.health_timer.isActive()
    window._shutdown_background_tasks()
    assert window.health_timer.isActive() is False
    # 幂等：再次调用不抛错
    window._shutdown_background_tasks()


def test_shutdown_interrupts_and_reaps_running_worker(window, qapp):
    started = time.monotonic()

    def slow_job():
        time.sleep(0.4)
        return "done"

    worker = window._track(Worker(slow_job))
    worker.start()
    qapp.processEvents()
    assert worker.isRunning() or worker.isFinished()
    window._shutdown_background_tasks()
    # _shutdown_background_tasks 已通过 worker.wait() 等待 run() 返回；
    # 但 finished → _untrack 的 queued 连接需要事件循环处理才能生效，
    # 轮询 processEvents 直到 worker 从跟踪列表移除（含总 timeout 上限）。
    deadline = time.monotonic() + 3.0
    while worker in window.workers and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert worker.isFinished() is True
    assert window.workers == []
    assert time.monotonic() - started < 5.0, "关闭不应等待慢任务超过预算"


def test_close_event_runs_shutdown_path_without_tray(window, qapp):
    # 无托盘（start_background=False）时 closeEvent 走真正关闭路径
    window.mgr["minimize_to_tray"] = False

    def slow_job():
        time.sleep(0.2)
        return "done"

    worker = window._track(Worker(slow_job))
    worker.start()
    event = QCloseEvent()
    window.closeEvent(event)
    # closeEvent → _shutdown_background_tasks 已 wait() 完 worker.run()；
    # finished 信号的 queued 槽需轮询 processEvents 处理后才移除 worker。
    deadline = time.monotonic() + 3.0
    while worker in window.workers and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert event.isAccepted() is True
    assert worker.isFinished() is True
    assert window.workers == []


def test_track_untrack_removes_completed_worker(window, qapp):
    worker = window._track(Worker(lambda: 42))
    assert worker in window.workers
    worker.start()
    deadline = time.monotonic() + 3
    while worker in window.workers and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert worker not in window.workers, "完成的 worker 应从跟踪列表移除"


# ---- P0：单一 Worker 登记表覆盖插件页 --------------------------------------

def test_plugin_page_workers_join_the_single_registry(window, qapp):
    """插件页曾用独立的 window._active_workers 登记表，退出时从不被收割。

    这里走插件页真实的登记入口 _track_worker，断言它落进 window.workers，
    并被 _shutdown_background_tasks 中断 + join。
    """
    from pi_manager.presentation.pages import plugins as plugins_page

    assert not hasattr(window, "_active_workers"), "第二套登记表应已彻底消失"

    def slow_job():
        time.sleep(0.3)
        return {"plugins": []}

    worker = Worker(slow_job)
    plugins_page._track_worker(window, worker)
    assert worker in window.workers, "插件页 Worker 必须进入唯一登记表"
    worker.start()
    qapp.processEvents()

    window._shutdown_background_tasks()
    assert worker.isFinished() is True, "插件页 Worker 必须在关闭预算内被 join"
    assert worker.isRunning() is False

    deadline = time.monotonic() + 3.0
    while worker in window.workers and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert window.workers == []


def test_shutdown_detaches_uninterruptible_worker_instead_of_destroying_it(window, qapp):
    """不可中断的长任务超出预算时必须脱钩延寿，而不是随窗口析构于运行态。

    QThread 析构于运行态会触发 qFatal("QThread: Destroyed while thread is
    still running") → abort()，Windows 上表现为崩溃弹窗。
    """
    from pi_manager import ui as ui_module

    release = threading.Event()

    def stubborn_job():
        # 不查询 is_cancelled：模拟 npm install / 子进程 / 网络请求
        release.wait(20)
        return "late"

    worker = window._track(Worker(stubborn_job))
    assert worker.cancellable is False, "未声明 is_cancelled 的 job 应标为不可中断"
    worker.start()
    qapp.processEvents()
    try:
        started = time.monotonic()
        window._shutdown_background_tasks()
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, f"关闭不应无限等待不可中断任务（用了 {elapsed:.1f}s）"
        assert worker.isRunning() is True, "任务确实还在跑（这正是崩溃场景）"
        assert worker in ui_module._ORPHANED_WORKERS, "超预算的 worker 必须被脱钩延寿"
        assert worker.parent() is None, "脱钩后不应再随窗口析构链被销毁"
        assert worker not in window.workers, "脱钩后应从窗口登记表移除"
    finally:
        release.set()
        worker.wait(5000)
        try:
            ui_module._ORPHANED_WORKERS.remove(worker)
        except ValueError:
            pass


# ---- P1-2：Worker 的协作式取消契约 ------------------------------------------

def test_worker_injects_is_cancelled_for_jobs_that_declare_it(window, qapp):
    seen: dict[str, object] = {}
    stop = threading.Event()

    def cancellable_job(is_cancelled=None):
        seen["callable"] = callable(is_cancelled)
        while not stop.is_set():
            if is_cancelled and is_cancelled():
                seen["observed_cancel"] = True
                return "cancelled"
            time.sleep(0.01)
        return "finished"

    worker = window._track(Worker(cancellable_job))
    assert worker.cancellable is True
    worker.start()
    deadline = time.monotonic() + 2.0
    while "callable" not in seen and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    assert seen.get("callable") is True, "run() 必须把中断信号注入 is_cancelled"

    window._shutdown_background_tasks()
    stop.set()
    worker.wait(3000)
    assert seen.get("observed_cancel") is True, "requestInterruption 必须真的被 job 观察到"
    assert worker.isFinished() is True


def test_cancelled_worker_does_not_emit_done(window, qapp):
    """取消后不再发 done：接收槽可能已随窗口关闭而失效。"""
    emitted: list[object] = []

    def job(is_cancelled=None):
        while not (is_cancelled and is_cancelled()):
            time.sleep(0.01)
        return "value-after-cancel"

    worker = window._track(Worker(job))
    worker.done.connect(emitted.append)
    worker.start()
    qapp.processEvents()
    window._shutdown_background_tasks()
    worker.wait(3000)
    for _ in range(20):
        qapp.processEvents()
        time.sleep(0.005)
    assert emitted == [], "被取消的任务不应再把结果推给 UI 槽"


def test_batch_test_worker_reports_health_cancellability_from_extras(window):
    """健康检查是否可取消取决于 extras.run_health_check 是否声明 is_cancelled。"""
    from pi_manager import extras
    from pi_manager.ui import BatchTestWorker, _accepts_is_cancelled

    worker = BatchTestWorker([], kind="health")
    assert worker.cancellable == _accepts_is_cancelled(extras.run_health_check)
    assert BatchTestWorker([], kind="model").cancellable is True


# ---- P1-7：start_background=False 真的不起线程 ------------------------------

def test_offscreen_construction_starts_no_threads(qapp, isolated_home):
    from pi_manager.presentation.main_window import ModernMainWindow

    w = ModernMainWindow(start_background=False)
    try:
        assert w._background_enabled is False
        # 包含插件页：以前 build_plugins_page 结尾无条件 _refresh() 起线程
        assert w.workers == [], f"构造期不应有后台线程，实际 {w.workers}"
        assert not hasattr(w, "_active_workers")
        assert "刷新" in w.plugins_global_status.text()
    finally:
        w._shutdown_background_tasks()
        w.hide()
        w.deleteLater()
        qapp.processEvents()


def test_background_enabled_is_set_before_pages_are_built(qapp, isolated_home):
    """_background_enabled 必须先于 _build_ui() 赋值，否则页面构建器判断不到。"""
    from pi_manager.presentation.main_window import ModernMainWindow

    observed: list[object] = []
    original = ModernMainWindow._build_ui

    def spy(self):
        observed.append(getattr(self, "_background_enabled", "<unset>"))
        return original(self)

    ModernMainWindow._build_ui = spy
    try:
        w = ModernMainWindow(start_background=False)
    finally:
        ModernMainWindow._build_ui = original
    try:
        assert observed == [False]
    finally:
        w._shutdown_background_tasks()
        w.hide()
        w.deleteLater()
        qapp.processEvents()
