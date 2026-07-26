"""Persistent `pi --mode rpc` session for the desktop quick chat.

One long-lived pi process holds the conversation; model changes are applied
hot via set_model (context preserved in-process). Credential changes require
a respawn because env cannot be injected into a running child — the sticky
--session-id makes pi reload the same session file, so context survives
restarts too.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from typing import Any

from . import core
from . import secrets as secretstore

_COMMAND_TIMEOUT = 30.0
_PROMPT_TIMEOUT = 180.0


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
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        )
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

    def close(self) -> None:
        try:
            self._proc.kill()
        except Exception:
            pass
        self._on_exit("closed")

    def _read_stdout(self) -> None:
        try:
            for raw in self._proc.stdout or []:
                line = raw.strip()
                if not line.startswith("{"):
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                self._handle(message)
        except Exception:
            pass
        self._on_exit(f"exit code={self._proc.poll()}")

    def _read_stderr(self) -> None:
        try:
            for line in self._proc.stderr or []:
                self._stderr_tail = (self._stderr_tail + line)[-4000:]
        except Exception:
            pass

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
                turn["last_assistant"] = inner
            return
        if message.get("type") == "agent_end" and not message.get("willRetry"):
            for inner in reversed(message.get("messages") or []):
                if isinstance(inner, dict) and inner.get("role") == "assistant":
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
            with self._stdin_lock:
                assert self._proc.stdin is not None
                self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
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

    def prompt(self, text: str, timeout: float = _PROMPT_TIMEOUT) -> dict[str, Any]:
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

        if not turn["event"].wait(timeout):
            clear_turn()
            try:
                self.send({"type": "abort"})
            except RpcSessionError:
                pass
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


def rpc_chat_enabled() -> bool:
    if _runtime_disabled:
        return False
    try:
        mgr = core.load_manager_config()
    except Exception:
        return False
    return bool(mgr.get("chat_persistent_session", True))


def reset_chat_session() -> None:
    """Drop the persistent conversation (new session id on next prompt)."""
    global _entry
    with _manager_lock:
        entry = _entry
        _entry = None
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
    respawn = (
        entry is None
        or not entry["session"].is_alive()
        or entry["env_by_provider"].get(provider) != provider_env
        or entry["workdir"] != workdir_key
        or entry["thinking"] != thinking
    )
    if respawn:
        session_id = entry["session_id"] if entry else str(uuid.uuid4())
        env_by_provider: dict[str, dict[str, str]] = dict(entry["env_by_provider"]) if entry else {}
        env_by_provider[provider] = dict(provider_env)
        if entry is not None:
            entry["session"].close()
        merged: dict[str, str] = {}
        for env in env_by_provider.values():
            merged.update(env)
        argv = core.pi_base_cmd() + ["--mode", "rpc", "--provider", provider, "--model", model]
        if thinking:
            argv += ["--thinking", thinking]
        argv += ["--session-id", session_id, "-n", "PiManager 快速提问"]
        spawn_env = os.environ.copy()
        spawn_env.update(merged)
        session = PiRpcSession(argv, env=spawn_env, cwd=workdir or None)
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
) -> dict[str, Any]:
    """chat_once-shaped attempt over the persistent RPC session.

    Same key-rotation semantics as run_pi_print: auth/limit failures mark the
    managed key and retry with the next one (respawn keeps the conversation
    via the sticky session id); other failures are returned for the model
    failover layer to count.
    """
    global _entry, _runtime_disabled
    if not provider or not model:
        return _failed(provider, model, "Provider 和 Model 必须成对指定")
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
            result = session.prompt(prompt, timeout=timeout)
        except RpcSessionError as exc:
            with _manager_lock:
                if _entry is not None and not _entry["session"].is_alive():
                    _entry = None
            if exc.unavailable:
                _runtime_disabled = True
            result = _failed(provider, model, str(exc))
        except FileNotFoundError as exc:
            _runtime_disabled = True
            return _failed(provider, model, str(exc))
        result["provider"], result["model"] = provider, model
        if result.get("ok") or not key_id:
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
