"""后台任务线程与生命周期跟踪。

从 ``ui.py`` 下沉：页面模块与对话框需要 ``Worker`` 时不应反向依赖行为基类。
``pi_manager.ui`` 继续 re-export 本模块的公共符号，保持现有测试与外部导入稳定。
"""
from __future__ import annotations

import inspect
import logging
import time

from PySide6.QtCore import QThread, Signal

from .. import extras

BATCH_TEST_TIMEOUT_PI = 90
BATCH_TEST_TIMEOUT_DIRECT = 45

logger = logging.getLogger(__name__)


def _accepts_is_cancelled(fn) -> bool:
    """判断 job 是否声明了 ``is_cancelled`` 形参（协作式取消契约的入口）。

    只做静态签名检查：内置/C 扩展等取不到签名的可调用体一律视为不可取消。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    if "is_cancelled" in params:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())


# 关闭预算耗尽后仍在运行的 Worker 会被移到这里：脱离 parent、保持强引用，
# 直到进程结束。宁可泄漏一个线程，也不要让 QThread 析构于运行态触发
# qFatal("QThread: Destroyed while thread is still running") 导致崩溃退出。
_ORPHANED_WORKERS: list[QThread] = []


def detach_running_worker(worker) -> bool:
    """把仍在运行的 worker 从 Qt 对象树与登记表中摘出并延寿到进程结束。"""
    if worker is None:
        return False
    try:
        if not worker.isRunning():
            return False
    except RuntimeError:
        # C++ 侧已销毁：无需处理
        return False
    try:
        worker.setParent(None)
    except (RuntimeError, TypeError) as e:
        logger.warning("detach worker parent failed: %s", e)
    if worker not in _ORPHANED_WORKERS:
        _ORPHANED_WORKERS.append(worker)
    logger.warning(
        "background worker %s did not finish within the shutdown budget; "
        "detached to avoid destroying a running QThread",
        type(worker).__name__,
    )
    return True


class Worker(QThread):
    """后台任务线程，带显式的协作式取消契约。

    ``requestInterruption()`` 本身只是给 QThread 置一个标志位；只有 job 主动
    查询才有效果。因此：

    * 若 ``fn`` 声明了 ``is_cancelled`` 形参（或接受 ``**kwargs``），``run()``
      会自动注入 ``self.isInterruptionRequested``，job 可在分段点自行退出；
      此时 ``cancellable`` 为 True。
    * 否则 ``cancellable`` 为 False —— 关闭流程据此知道该任务无法被打断，
      不再假装 2.5s 预算能把它收走（见 ``detach_running_worker``）。
    """

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, **kwargs):
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self._inject_cancel = "is_cancelled" not in kwargs and _accepts_is_cancelled(fn)
        self.cancellable = bool(self._inject_cancel or "is_cancelled" in kwargs)

    def run(self):
        kwargs = dict(self.kwargs)
        if self._inject_cancel:
            kwargs["is_cancelled"] = self.isInterruptionRequested
        try:
            result = self.fn(*self.args, **kwargs)
        except Exception as e:
            if self.isInterruptionRequested():
                # 取消导致的异常不再上报：接收槽可能已随窗口关闭而失效。
                logger.info("Worker task aborted after interruption request")
                return
            logger.exception("Worker task failed")
            self.failed.emit(str(e)[:500])
            return
        if self.isInterruptionRequested():
            return
        self.done.emit(result)


class WorkerTrackerMixin:
    """管理后台 Worker 生命周期：登记、完成时 deleteLater + 移除。

    子类需在 __init__ 中调用 ``_init_workers`` 初始化跟踪列表。
    ``_adopt_worker`` 默认把 worker 的 parent 设为 self（随窗口清理）；
    MainWindow 重写为 no-op（窗口关闭即应用退出）。
    各子类应保留自己的 ``closeEvent``（超时/拒绝逻辑各不相同，不在此统一）。
    """

    def _init_workers(self) -> None:
        self._workers: list[Worker] = []

    def _adopt_worker(self, worker: Worker) -> None:
        worker.setParent(self)

    def _track(self, worker: Worker) -> Worker:
        self._adopt_worker(worker)
        self._workers.append(worker)
        worker.finished.connect(lambda: self._untrack(worker))
        worker.finished.connect(worker.deleteLater)
        return worker

    def _untrack(self, worker: Worker) -> None:
        try:
            self._workers.remove(worker)
        except ValueError:
            pass

    def _reap_workers(self, budget: float = 5.0) -> list[Worker]:
        """关闭前收割本对象的 Worker；返回预算耗尽后仍在运行者（已脱钩）。

        默认 ``_adopt_worker`` 会把 worker 的 parent 设为 self，对话框销毁时
        Qt 会连带销毁子对象 —— 包括仍在运行的 QThread，触发
        qFatal("QThread: Destroyed while thread is still running")。
        以前这里 ``wait()`` 的返回值被忽略、无论是否等到都放行；现在超时的
        worker 一律脱离 parent 并延寿到进程结束（``detach_running_worker``），
        既不阻塞用户关闭窗口，也不会崩溃。
        """
        running = [w for w in self._workers if w.isRunning()]
        if not running:
            return []
        for w in running:
            w.requestInterruption()
        deadline = time.monotonic() + budget
        for w in running:
            remaining = max(0, int((deadline - time.monotonic()) * 1000))
            if w.isRunning() and remaining:
                w.wait(remaining)
        stuck = []
        for w in running:
            if detach_running_worker(w):
                self._untrack(w)
                stuck.append(w)
        return stuck

    def _note_detached_workers(self) -> None:
        """把「请求仍在后台收尾」写到对话框自己的状态标签（可被子类覆写）。"""
        for attr in ("fetch_status", "verify_status", "status", "log"):
            label = getattr(self, attr, None)
            setter = getattr(label, "setText", None) if label is not None else None
            if callable(setter):
                setter("网络请求未能在 5 秒内取消，已转入后台收尾；窗口可安全关闭。")
                return


class BatchTestWorker(QThread):
    """Run concurrent model tests and emit each result as it completes."""

    progress = Signal(object)  # one result dict
    done = Signal(object)  # full ordered list
    failed = Signal(str)

    def __init__(
        self,
        pairs: list[tuple[str, str]],
        *,
        mode: str = "auto",
        workdir: str = "",
        timeout: float | None = None,
        kind: str = "model",  # model | health
        health_scope: str = "favorites",
        health_selected: list[tuple[str, str]] | None = None,
    ):
        super().__init__()
        self.pairs = list(pairs or [])
        self.mode = mode
        self.workdir = workdir
        self.timeout = timeout
        self.kind = kind
        self.health_scope = health_scope
        self.health_selected = health_selected or []
        # 模型批测走 test_models_batch_concurrent，一直支持 is_cancelled；
        # 健康检查取决于 extras.run_health_check 是否已声明该形参（见下）。
        self.cancellable = kind != "health" or _accepts_is_cancelled(extras.run_health_check)

    def run(self):
        try:
            if self.kind == "health":
                def on_one(res):
                    self.progress.emit(res)

                health_kwargs = {}
                if self.cancellable:
                    # extras.run_health_check 目前尚未声明 is_cancelled；一旦补上
                    # 该形参，这里会自动把中断信号透传下去，无需再改 UI 层。
                    health_kwargs["is_cancelled"] = self.isInterruptionRequested
                result = extras.run_health_check(
                    pairs=self.pairs or None,
                    mode=self.mode,
                    scope=self.health_scope,
                    selected=self.health_selected,
                    on_one=on_one,
                    **health_kwargs,
                )
                # 取消后仍回传已完成项：界面要立刻解除「进行中」，并展示部分结果。
                self.done.emit(result)
                return

            timeout = self.timeout if self.timeout is not None else (
                BATCH_TEST_TIMEOUT_PI if self.mode == "pi" else BATCH_TEST_TIMEOUT_DIRECT
            )

            def on_one(res):
                self.progress.emit(res)

            results = extras.test_models_batch_concurrent(
                self.pairs,
                mode=self.mode,
                timeout=timeout,
                workdir=self.workdir or None,
                max_workers=extras.get_test_concurrency(),
                on_one=on_one,
                append_history_each=True,
                is_cancelled=self.isInterruptionRequested,
            )
            self.done.emit(results)
        except Exception as e:
            logger.exception("BatchTestWorker failed")
            self.failed.emit(str(e))
