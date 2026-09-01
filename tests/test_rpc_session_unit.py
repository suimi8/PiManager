# -*- coding: utf-8 -*-
"""PiRpcSession 类级路径的单元测试。

不依赖真实 pi 命令：用一个可脚本化的 fake Popen（stdout 行由测试手动
推送、stderr 行可注入、stdin 写入可观察）驱动 reader 线程，覆盖：
send 超时、prompt 超时 abort、进程死亡 dead-error（stderr 尾部进入
错误信息）、正常往返与并发互斥。CI 无外网/无 pi 也可运行。
"""
from __future__ import annotations

import json
import queue
import threading
import time

import pytest

from pi_manager import rpc_session


class _ScriptedStdin:
    def __init__(self):
        self.writes: list[str] = []
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        with self._lock:
            self.writes.append(text)
        return len(text)

    def flush(self) -> None:
        return


class _ScriptedStream:
    """可推送行的伪 stdout/stderr；finish() 后迭代结束（触发进程死亡路径）。"""

    def __init__(self):
        self._queue: queue.Queue[str] = queue.Queue()
        self._done = threading.Event()

    def push(self, line: str) -> None:
        self._queue.put(line if line.endswith("\n") else line + "\n")

    def finish(self) -> None:
        self._done.set()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        try:
            return self._queue.get(timeout=0.02)
        except queue.Empty:
            if self._done.is_set():
                raise StopIteration
            return "\n"  # 空行让 reader 循环继续，保持进程存活


class _FakePopen:
    def __init__(self):
        self.stdin = _ScriptedStdin()
        self.stdout = _ScriptedStream()
        self.stderr = _ScriptedStream()
        self._killed = False

    def poll(self):
        return None

    def kill(self):
        self._killed = True


@pytest.fixture
def fake_proc(monkeypatch):
    fake = _FakePopen()

    def fake_popen(argv, **kwargs):
        return fake

    monkeypatch.setattr(rpc_session.subprocess, "Popen", fake_popen)
    return fake


def _wait_writes(fake: _FakePopen, n: int, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while len(fake.stdin.writes) < n:
        if time.monotonic() > deadline:
            raise AssertionError(f"等待第 {n} 次 stdin 写入超时，当前 {len(fake.stdin.writes)} 次")
        time.sleep(0.002)


def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待条件超时")
        time.sleep(0.002)


def _teardown(session, fake: _FakePopen) -> None:
    fake.stdout.finish()
    fake.stderr.finish()
    session.close()


# ---- send 往返与超时 ------------------------------------------------------

def test_send_roundtrip_gets_json_response(fake_proc):
    fake = fake_proc
    session = rpc_session.PiRpcSession(["pi", "--mode", "rpc"], env={"PATH": ""})
    try:
        results: dict = {}

        def run():
            results["resp"] = session.send({"type": "ping"})

        t = threading.Thread(target=run)
        t.start()
        _wait_writes(fake, 1)
        assert json.loads(fake.stdin.writes[0])["type"] == "ping"
        fake.stdout.push(
            json.dumps({"type": "response", "id": "1", "success": True, "data": {"pong": 1}})
        )
        t.join(2)
        assert not t.is_alive(), "send 应已返回"
        assert results["resp"]["data"]["pong"] == 1
    finally:
        _teardown(session, fake)


def test_send_timeout_raises_and_cleans_pending(fake_proc):
    fake = fake_proc
    session = rpc_session.PiRpcSession(["pi"], env={})
    try:
        with pytest.raises(rpc_session.RpcSessionError, match="命令超时"):
            session.send({"type": "ping"}, timeout=0.2)
        # 超时后 pending 已清理，再次 send 应重新分配 id
        results: dict = {}

        def run():
            results["resp"] = session.send({"type": "ping"}, timeout=1.0)

        t = threading.Thread(target=run)
        t.start()
        _wait_writes(fake, 2)
        fake.stdout.push(json.dumps({"type": "response", "id": "2", "success": True}))
        t.join(2)
        assert not t.is_alive()
        assert results["resp"].get("success") is True
    finally:
        _teardown(session, fake)


# ---- prompt 生命周期 ------------------------------------------------------

def test_prompt_success_path_collects_answer(fake_proc):
    fake = fake_proc
    session = rpc_session.PiRpcSession(["pi"], env={})
    try:
        results: dict = {}

        def run():
            results["r"] = session.prompt("hello", timeout=2.0)

        t = threading.Thread(target=run)
        t.start()
        _wait_writes(fake, 1)  # prompt 命令
        fake.stdout.push(json.dumps({"type": "response", "id": "1", "success": True}))
        # agent_end 结束 turn（assistant 消息携带文本）
        fake.stdout.push(
            json.dumps(
                {
                    "type": "agent_end",
                    "willRetry": False,
                    "messages": [
                        {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "hi there"}],
                        }
                    ],
                }
            )
        )
        _wait_writes(fake, 2)  # get_last_assistant_text
        fake.stdout.push(
            json.dumps(
                {"type": "response", "id": "2", "success": True, "data": {"text": "hi there"}}
            )
        )
        t.join(2)
        assert not t.is_alive()
        r = results["r"]
        assert r["ok"] is True
        assert r["stdout"] == "hi there"
        assert r["returncode"] == 0
        assert session.is_busy() is False
    finally:
        _teardown(session, fake)


def test_prompt_timeout_sends_abort_and_returns_error(fake_proc):
    fake = fake_proc
    session = rpc_session.PiRpcSession(["pi"], env={})
    try:
        results: dict = {}

        def run():
            results["r"] = session.prompt("hello", timeout=0.2)

        t = threading.Thread(target=run)
        t.start()
        _wait_writes(fake, 1)
        fake.stdout.push(json.dumps({"type": "response", "id": "1", "success": True}))
        # turn 永不结束 → prompt 超时 → 发送 abort
        _wait_writes(fake, 2)
        abort_cmd = json.loads(fake.stdin.writes[1])
        assert abort_cmd["type"] == "abort"
        fake.stdout.push(json.dumps({"type": "response", "id": "2", "success": True}))
        t.join(2)
        assert not t.is_alive()
        r = results["r"]
        assert r["ok"] is False
        assert "超时" in r["error"]
        assert session.is_busy() is False
    finally:
        _teardown(session, fake)


def test_prompt_rejects_overlapping_turns(fake_proc):
    fake = fake_proc
    session = rpc_session.PiRpcSession(["pi"], env={})
    try:
        results: dict = {}

        def first():
            results["first"] = session.prompt("one", timeout=1.0)

        t = threading.Thread(target=first)
        t.start()
        _wait_writes(fake, 1)
        fake.stdout.push(json.dumps({"type": "response", "id": "1", "success": True}))
        _wait_until(lambda: session.is_busy())
        with pytest.raises(rpc_session.RpcSessionError, match="仍在进行"):
            session.prompt("two", timeout=0.1)
        # 结束第一个 turn
        fake.stdout.push(
            json.dumps(
                {
                    "type": "agent_end",
                    "willRetry": False,
                    "messages": [{"role": "assistant", "content": [{"type": "text", "text": "a"}]}],
                }
            )
        )
        _wait_writes(fake, 2)
        fake.stdout.push(
            json.dumps({"type": "response", "id": "2", "success": True, "data": {"text": "a"}})
        )
        t.join(2)
        assert not t.is_alive()
    finally:
        _teardown(session, fake)


# ---- 进程死亡（dead-error）与 stderr 尾部 ---------------------------------

def test_dead_process_raises_error_with_stderr_tail(fake_proc):
    fake = fake_proc
    session = rpc_session.PiRpcSession(["pi"], env={})
    try:
        fake.stderr.push("Traceback (most recent call last):")
        fake.stderr.push("RuntimeError: boom-boom")
        _wait_until(lambda: "boom-boom" in session._stderr_tail)
        fake.stdout.finish()  # 进程退出
        _wait_until(lambda: not session.is_alive())
        assert session.is_alive() is False
        with pytest.raises(rpc_session.RpcSessionError) as excinfo:
            session.send({"type": "ping"})
        message = str(excinfo.value)
        assert "已退出" in message
        assert "boom-boom" in message, "stderr 尾部应进入错误信息"
        assert excinfo.value.unavailable is True, "从未成功响应过 → unavailable"
    finally:
        fake.stderr.finish()
        session.close()


def test_dead_process_during_prompt_raises_dead_error(fake_proc):
    fake = fake_proc
    session = rpc_session.PiRpcSession(["pi"], env={})
    try:
        results: dict = {}

        def run():
            try:
                session.prompt("hello", timeout=2.0)
            except rpc_session.RpcSessionError as exc:
                results["err"] = exc

        t = threading.Thread(target=run)
        t.start()
        _wait_writes(fake, 1)
        fake.stdout.push(json.dumps({"type": "response", "id": "1", "success": True}))
        _wait_until(lambda: session.is_busy())
        fake.stdout.finish()  # turn 进行中进程死亡
        t.join(2)
        assert not t.is_alive()
        assert "err" in results
        assert "已退出" in str(results["err"])
    finally:
        fake.stderr.finish()
        session.close()


def test_close_kills_process_and_marks_dead(fake_proc):
    fake = fake_proc
    session = rpc_session.PiRpcSession(["pi"], env={})
    try:
        assert session.is_alive() is True
        session.close()
        assert fake._killed is True
        assert session.is_alive() is False
        with pytest.raises(rpc_session.RpcSessionError):
            session.send({"type": "ping"})
    finally:
        fake.stdout.finish()
        fake.stderr.finish()


# ---- set_model ------------------------------------------------------------

def test_set_model_success_returns_data(fake_proc):
    fake = fake_proc
    session = rpc_session.PiRpcSession(["pi"], env={})
    try:
        results: dict = {}

        def run():
            results["data"] = session.set_model("P", "m")

        t = threading.Thread(target=run)
        t.start()
        _wait_writes(fake, 1)
        cmd = json.loads(fake.stdin.writes[0])
        assert cmd["type"] == "set_model"
        assert cmd["provider"] == "P" and cmd["modelId"] == "m"
        fake.stdout.push(
            json.dumps({"type": "response", "id": "1", "success": True, "data": {"switched": True}})
        )
        t.join(2)
        assert results["data"] == {"switched": True}
    finally:
        _teardown(session, fake)


def test_set_model_failure_raises(fake_proc):
    fake = fake_proc
    session = rpc_session.PiRpcSession(["pi"], env={})
    try:
        results: dict = {}

        def run():
            try:
                session.set_model("P", "m")
            except rpc_session.RpcSessionError as exc:
                results["err"] = exc

        t = threading.Thread(target=run)
        t.start()
        _wait_writes(fake, 1)
        fake.stdout.push(
            json.dumps({"type": "response", "id": "1", "success": False, "error": "model not found"})
        )
        t.join(2)
        assert "model not found" in str(results["err"])
    finally:
        _teardown(session, fake)


def test_rpc_session_rejects_illegal_thinking_before_spawn(monkeypatch):
    """P0-1 回归：RPC 路径的 --thinking 必须先过白名单，非法值在 spawn 前拒绝。"""
    import pytest

    from pi_manager import core as pi_core

    monkeypatch.setattr(pi_core, "pi_base_cmd", lambda: ["pi"])
    with pytest.raises(ValueError):
        rpc_session._ensure(
            "ProviderX", "model-1", {}, None, "high&calc"
        )
