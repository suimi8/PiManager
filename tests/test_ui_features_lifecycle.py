# -*- coding: utf-8 -*-
"""ui_features 的 QTimer 健康检查与后台线程生命周期测试（offscreen）。

断言基于公开行为：健康检查 QTimer 的创建/启停、_shutdown_background_tasks
停止定时器并中断 worker、closeEvent 走关闭路径不泄漏线程。
ui.py / ui_features.py 可能被并行修改（行为不变）。
"""
from __future__ import annotations

import os
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
