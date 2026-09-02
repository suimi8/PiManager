"""Persistent `pi --mode rpc` session for the desktop quick chat.

One long-lived pi process holds the conversation; model changes are applied
hot via set_model (context preserved in-process). Credential changes require
a respawn because env cannot be injected into a running child — the sticky
--session-id makes pi reload the same session file, so context survives
restarts too.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

from . import core
from . import proc
from . import secrets as secretstore

logger = logging.getLogger(__name__)

_COMMAND_TIMEOUT = 30.0
_PROMPT_TIMEOUT = 180.0
_RUNTIME_RETRY_COOLDOWN = 30.0
_MAX_STDOUT_LINE = 10 * 1024 * 1024
_STDIN_WRITE_TIMEOUT = 10.0
_STDIN_WRITE_GRACE = 0.2
_MAX_PROVIDER_ENV_CACHE = 8


class RpcSessionError(RuntimeError):
    def __init__(self, message: str, *, unavailable: bool = False) -> None:
        super().__init__(message)
        self.unavailable = unavailable


def _extract_text(message: dict[str, Any] | None) -> str:
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        str(block.get("text") or "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


class PiRpcSession:
    def __init__(self, argv: list[str], *, env: dict[str, str], cwd: str | None = None) -> None:
        creationflags = proc.create_no_window_flag()
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd or None,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        self._stdin_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._turn: dict[str, Any] | None = None
        self._next_id = 1
        self._alive = True
        self._ever_responded = False
        self._exit_info = ""
        self._stderr_tail = ""
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def is_alive(self) -> bool:
        with self._state_lock:
            return self._alive

    def is_busy(self) -> bool:
        with self._state_lock:
            return self._turn is not None

    def close(self) -> None:
        try:
            if self._proc.poll() is None:
                self._proc.kill()
        except Exception as exc:
            logger.warning("关闭 Pi RPC 进程失败: %s", exc)
        self._on_exit("closed")

    def _read_stdout(self) -> None:
        try:
            for raw in self._proc.stdout or []:
                line = raw.strip()
                if not line.startswith("{"):
                    continue
                if len(line) > _MAX_STDOUT_LINE:
                    logger.warning("Pi RPC stdout 行过长 (%d 字符)，跳过", len(line))
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self._handle(message)
        except Exception as exc:
            logger.warning("读取 Pi RPC stdout 失败: %s", exc)
        self._on_exit(f"exit code={self._proc.poll()}")

    def _read_stderr(self) -> None:
        try:
            for line in self._proc.stderr or []:
                self._stderr_tail = (self._stderr_tail + line)[-4000:]
        except Exception as exc:
            logger.warning("读取 Pi RPC stderr 失败: %s", exc)

    def _handle(self, message: Any) -> None:
        if not isinstance(message, dict):
            return
        if message.get("type") == "response":
            with self._state_lock:
                self._ever_responded = True
                pending = self._pending.pop(str(message.get("id") or ""), None)
            if pending is not None:
                pending["response"] = message
                pending["event"].set()
            return
        with self._state_lock:
            turn = self._turn
        if not turn:
            return
        if message.get("type") == "message_end":
            inner = message.get("message")
            if isinstance(inner, dict) and inner.get("role") == "assistant":
                with self._state_lock:
                    if self._turn is turn:
                        turn["last_assistant"] = inner
            return
        if message.get("type") == "agent_end" and not message.get("willRetry"):
            for inner in reversed(message.get("messages") or []):
                if isinstance(inner, dict) and inner.get("role") == "assistant":
                    with self._state_lock:
                        if self._turn is turn:
                            turn["last_assistant"] = inner
                    break
            with self._state_lock:
                if self._turn is turn:
                    self._turn = None
            turn["event"].set()

    def _on_exit(self, info: str) -> None:
        with self._state_lock:
            if not self._alive:
                return
            self._alive = False
            self._exit_info = info
            pendings = list(self._pending.values())
            self._pending.clear()
            turn = self._turn
            self._turn = None
        for pending in pendings:
            pending["event"].set()
        if turn is not None:
            turn["dead"] = True
            turn["event"].set()

    def _dead_error(self) -> RpcSessionError:
        detail = f"：{self._stderr_tail[-400:]}" if self._stderr_tail else ""
        return RpcSessionError(
            f"Pi RPC 会话已退出（{self._exit_info or '未知原因'}）{detail}",
            unavailable=not self._ever_responded,
        )

    def _write_payload(self, payload: dict[str, Any]) -> None:
        """Write one JSON line to the child stdin with a bounded flush.

        A wedged child that stops reading stdin would otherwise block
        write()/flush() forever and bypass the command timeout. The write
        runs on a worker thread; on timeout the child is killed (a child
        that cannot read stdin cannot serve further RPC anyway) and the
        caller receives a normal RpcSessionError instead of hanging.
        """
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._stdin_lock:
            if self._proc.stdin is None:
                raise RpcSessionError("Pi RPC stdin 不可用")
            outcome: dict[str, Any] = {}

            def do_write() -> None:
                try:
                    assert self._proc.stdin is not None
                    self._proc.stdin.write(line)
                    self._proc.stdin.flush()
                    outcome["ok"] = True
                except Exception as exc:  # BrokenPipeError / OSError ...
                    outcome["exc"] = exc

            worker = threading.Thread(target=do_write, daemon=True)
            worker.start()
            worker.join(_STDIN_WRITE_TIMEOUT)
            if worker.is_alive():
                # Grace tick: the worker may have just finished its flush but
                # not yet returned; only declare a timeout if still stuck.
                worker.join(_STDIN_WRITE_GRACE)
            if worker.is_alive():
                self.close()
                raise RpcSessionError(
                    f"写入 Pi RPC stdin 超时（{_STDIN_WRITE_TIMEOUT:.0f}s），子进程已终止"
                )
            if "exc" in outcome:
                raise RpcSessionError(
                    f"发送 RPC 命令失败：{outcome['exc']}"
                ) from outcome["exc"]

    def send(self, command: dict[str, Any], timeout: float = _COMMAND_TIMEOUT) -> dict[str, Any]:
        with self._state_lock:
            if not self._alive:
                raise self._dead_error()
            command_id = str(self._next_id)
            self._next_id += 1
            pending: dict[str, Any] = {"event": threading.Event(), "response": None}
            self._pending[command_id] = pending
        payload = dict(command)
        payload["id"] = command_id
        try:
            self._write_payload(payload)
        except Exception as exc:
            with self._state_lock:
                self._pending.pop(command_id, None)
            raise RpcSessionError(f"发送 RPC 命令失败：{exc}") from exc
        if not pending["event"].wait(timeout):
            with self._state_lock:
                self._pending.pop(command_id, None)
            raise RpcSessionError(f"Pi RPC 命令超时：{command.get('type')}")
        if pending["response"] is None:
            raise self._dead_error()
        return pending["response"]

    def set_model(self, provider: str, model_id: str) -> Any:
        response = self.send({"type": "set_model", "provider": provider, "modelId": model_id})
        if response.get("success") is False:
            raise RpcSessionError(f"切换模型失败：{response.get('error') or '未知错误'}")
        return response.get("data")

    def prompt(
        self,
        text: str,
        timeout: float = _PROMPT_TIMEOUT,
        *,
        is_cancelled: Callable[[], bool] | None = None,
    ) -> dict[str, Any]:
        with self._state_lock:
            if self._turn is not None:
                raise RpcSessionError("上一个 Pi 请求仍在进行")
            turn: dict[str, Any] = {
                "event": threading.Event(),
                "last_assistant": None,
                "dead": False,
            }
            self._turn = turn
        started = time.perf_counter()

        def finish(**patch: Any) -> dict[str, Any]:
            result: dict[str, Any] = {
                "ok": False,
                "returncode": -1,
                "stdout": "",
                "stderr": "",
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "error": "",
            }
            result.update(patch)
            return result

        def clear_turn() -> None:
            with self._state_lock:
                if self._turn is turn:
                    self._turn = None

        try:
            ack = self.send({"type": "prompt", "message": str(text)})
        except RpcSessionError:
            clear_turn()
            raise
        if ack.get("success") is False:
            clear_turn()
            return finish(error=str(ack.get("error") or "prompt 预检失败"))

        deadline = time.monotonic() + max(0.05, float(timeout))
        cancelled = False
        while not turn["event"].is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if is_cancelled and is_cancelled():
                cancelled = True
                break
            turn["event"].wait(min(0.15, remaining))
        if cancelled or not turn["event"].is_set():
            clear_turn()
            try:
                self.send({"type": "abort"})
            except RpcSessionError:
                pass
            if cancelled:
                return finish(error="已停止生成", cancelled=True)
            return finish(error=f"Pi 响应超时（{int(timeout)}s）")
        if turn.get("dead"):
            raise self._dead_error()

        message = turn.get("last_assistant") or {}
        stop_reason = message.get("stopReason")
        if stop_reason in ("error", "aborted"):
            return finish(
                error=str(
                    message.get("errorMessage")
                    or ("模型返回已中止" if stop_reason == "aborted" else "模型返回错误")
                )
            )

        answer = ""
        try:
            response = self.send({"type": "get_last_assistant_text"})
            if response.get("success") is not False:
                answer = str((response.get("data") or {}).get("text") or "")
        except RpcSessionError:
            pass
        if not answer:
            answer = _extract_text(message)
        if not answer.strip():
            return finish(error="模型没有返回文本")
        return finish(ok=True, returncode=0, stdout=answer, error="")


# ---- desktop chat session manager -----------------------------------------

_manager_lock = threading.Lock()
_entry: dict[str, Any] | None = None
_runtime_disabled = False
_runtime_disabled_since = 0.0
_idle_timer: threading.Timer | None = None
# 专用于 _runtime_disabled / _runtime_disabled_since 的跨线程同步：
# 这两个变量由 Worker 线程写、主线程读，必须成对一致读取，避免快照错位。
_runtime_lock = threading.Lock()


def _idle_ttl_seconds() -> float:
    """Idle minutes before the persistent pi process is reclaimed (0 = never)."""
    try:
        minutes = float(core.load_manager_config().get("chat_session_idle_min") or 10)
    except Exception:
        minutes = 10.0
    return max(0.0, minutes) * 60.0


def _schedule_idle_reaper() -> None:
    """(Re)arm the idle reaper. Caller must hold _manager_lock."""
    global _idle_timer
    if _idle_timer is not None:
        _idle_timer.cancel()
        _idle_timer = None
    ttl = _idle_ttl_seconds()
    if ttl <= 0 or _entry is None:
        return
    timer = threading.Timer(ttl, _reap_idle_session)
    timer.daemon = True
    _idle_timer = timer
    timer.start()


def _reap_idle_session() -> None:
    """Close the persistent session once it has sat idle for a full TTL.

    The sticky --session-id means a later prompt transparently reloads the
    same conversation, so reclaiming the process costs nothing but latency.
    """
    global _entry, _idle_timer
    with _manager_lock:
        entry = _entry
        _idle_timer = None
        if entry is None:
            return
        session = entry["session"]
        if session.is_busy() or time.monotonic() - entry.get("last_used", 0.0) < _idle_ttl_seconds() - 1:
            _schedule_idle_reaper()
            return
        _entry = None
    session.close()


def rpc_chat_enabled() -> bool:
    global _runtime_disabled
    with _runtime_lock:
        if _runtime_disabled:
            if time.monotonic() - _runtime_disabled_since < _RUNTIME_RETRY_COOLDOWN:
                return False
            _runtime_disabled = False
    try:
        mgr = core.load_manager_config()
    except Exception:
        return False
    return bool(mgr.get("chat_persistent_session", True))


def reset_chat_session() -> None:
    """Drop the persistent conversation (new session id on next prompt)."""
    global _entry, _idle_timer
    with _manager_lock:
        entry = _entry
        _entry = None
        if _idle_timer is not None:
            _idle_timer.cancel()
            _idle_timer = None
    if entry is not None:
        entry["session"].close()


def _ensure(
    provider: str,
    model: str,
    provider_env: dict[str, str],
    workdir: str | None,
    thinking: str,
) -> dict[str, Any]:
    global _entry
    entry = _entry
    workdir_key = str(workdir or "")
    env_snapshot = dict(provider_env)
    # A1: keep one env snapshot per provider so the env comparison is precise.
    # The child process is started with a fixed --provider, so a switch to a
    # different provider must respawn regardless of the cached env (pi's RPC
    # set_model cannot find models of another provider); within the same
    # provider, respawn only when its env genuinely changed (e.g. key
    # rotation), never because only the last provider's env was remembered.
    env_changed = entry is not None and entry["env_by_provider"].get(provider) != env_snapshot
    respawn = (
        entry is None
        or not entry["session"].is_alive()
        or entry["current"][0] != provider
        or env_changed
        or entry["workdir"] != workdir_key
        or entry["thinking"] != thinking
    )
    if respawn:
        session_id = entry["session_id"] if entry else str(uuid.uuid4())
        base = core.pi_base_cmd()
        argv = base + core.escape_cmd_shim_args(
            ["--mode", "rpc", "--provider", provider, "--model", model],
            base,
        )
        if thinking:
            # escape_cmd_shim_args 只校验传入的 provider/model 列表，thinking 在
            # 转义之后单独 append 会绕过白名单，成为 argv 参数注入面（thinking 值
            # 可经 settings.json 的 defaultThinkingLevel 被配置包导入控制）。此处
            # 显式校验后再拼接，非法值直接抛 ValueError 拒绝启动。
            core.validate_launch_tokens(["--thinking", thinking])
            argv += ["--thinking", thinking]
        argv += ["--session-id", session_id, "-n", "PiManager 快速提问"]
        spawn_env = proc.spawn_env(provider_env, sanitize_after_merge=False)
        # A2: spawn the replacement first; only tear the old session down
        # after the new process is up, so a spawn failure keeps the old
        # conversation recoverable instead of losing it.
        session = PiRpcSession(argv, env=spawn_env, cwd=workdir or None)
        if entry is not None:
            entry["session"].close()
        # A1: carry over the per-provider env cache and bound its size so the
        # cache cannot grow without limit across provider switches.
        env_by_provider = dict(entry["env_by_provider"]) if entry else {}
        env_by_provider[provider] = env_snapshot
        if len(env_by_provider) > _MAX_PROVIDER_ENV_CACHE:
            for stale in list(env_by_provider):
                if stale == provider:
                    continue
                del env_by_provider[stale]
                if len(env_by_provider) <= _MAX_PROVIDER_ENV_CACHE:
                    break
        _entry = {
            "session": session,
            "session_id": session_id,
            "env_by_provider": env_by_provider,
            "current": (provider, model),
            "workdir": workdir_key,
            "thinking": thinking,
        }
        return _entry
    if entry["current"] != (provider, model):
        entry["session"].set_model(provider, model)
        entry["current"] = (provider, model)
    return entry


def _failed(provider: str | None, model: str | None, error: str) -> dict[str, Any]:
    return {
        "ok": False,
        "returncode": -1,
        "stdout": "",
        "stderr": "",
        "latency_ms": 0,
        "provider": provider,
        "model": model,
        "error": error,
    }


def rpc_chat_once(
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    workdir: str | None = None,
    timeout: float = 180,
    thinking: str | None = "off",
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """chat_once-shaped attempt over the persistent RPC session.

    Same key-rotation semantics as run_pi_print: auth/limit failures mark the
    managed key and retry with the next one (respawn keeps the conversation
    via the sticky session id); other failures are returned for the model
    failover layer to count.
    """
    global _entry, _runtime_disabled, _runtime_disabled_since
    if not provider or not model:
        return _failed(provider, model, "Provider 和 Model 必须成对指定")
    if _runtime_disabled and time.monotonic() - _runtime_disabled_since >= _RUNTIME_RETRY_COOLDOWN:
        with _runtime_lock:
            _runtime_disabled = False
    attempted: set[str] = set()
    last: dict[str, Any] | None = None
    while True:
        try:
            credential = core.provider_runtime_credential(provider)
        except Exception as exc:
            return _failed(provider, model, str(exc))
        key_id = str(credential.get("key_id") or "")
        if key_id and key_id in attempted:
            return last or _failed(provider, model, "API Key 轮换未提供新的可用 Key")
        try:
            with _manager_lock:
                entry = _ensure(
                    provider, model, dict(credential.get("env") or {}), workdir, str(thinking or "off")
                )
            session = entry["session"]
            result = session.prompt(prompt, timeout=timeout, is_cancelled=is_cancelled)
        except RpcSessionError as exc:
            with _manager_lock:
                if _entry is not None and not _entry["session"].is_alive():
                    _entry = None
            if exc.unavailable:
                with _runtime_lock:
                    _runtime_disabled = True
                    _runtime_disabled_since = time.monotonic()
            result = _failed(provider, model, str(exc))
        except FileNotFoundError as exc:
            with _runtime_lock:
                _runtime_disabled = True
                _runtime_disabled_since = time.monotonic()
            return _failed(provider, model, str(exc))
        with _manager_lock:
            if _entry is not None:
                _entry["last_used"] = time.monotonic()
                _schedule_idle_reaper()
        result["provider"], result["model"] = provider, model
        if result.get("cancelled"):
            return result
        if result.get("ok") or not key_id:
            if result.get("ok"):
                with _runtime_lock:
                    _runtime_disabled = False
            return result
        classification = core.classify_provider_key_failure(
            int(result.get("returncode") or -1),
            str(result.get("stdout") or ""),
            str(result.get("error") or result.get("stderr") or ""),
        )
        if not classification.get("status"):
            return result
        try:
            changed = secretstore.mark_provider_key_failed(
                provider, key_id, str(result.get("error") or "")[:200]
            )
        except Exception:
            return result
        if not changed:
            return result
        attempted.add(key_id)
        last = result
