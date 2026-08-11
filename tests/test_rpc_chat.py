from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pi_manager import core, extras, rpc_session


@pytest.fixture(autouse=True)
def _reset_rpc_state():
    rpc_session.reset_chat_session()
    rpc_session._runtime_disabled = False
    rpc_session._runtime_disabled_since = 0.0
    yield
    rpc_session.reset_chat_session()
    rpc_session._runtime_disabled = False
    rpc_session._runtime_disabled_since = 0.0


def test_rpc_chat_disabled_by_manager_config(isolated_home):
    mgr = core.load_manager_config()
    mgr["chat_persistent_session"] = False
    core.save_manager_config(mgr)
    assert rpc_session.rpc_chat_enabled() is False


def test_chat_attempt_falls_back_when_rpc_runtime_disabled(isolated_home, monkeypatch):
    import time

    rpc_session._runtime_disabled = True
    rpc_session._runtime_disabled_since = time.monotonic()
    calls: list[str] = []

    def fake_chat_once(prompt, **kwargs):
        calls.append(prompt)
        return {"ok": True, "returncode": 0, "stdout": "one-shot", "stderr": "", "latency_ms": 1, "error": ""}

    monkeypatch.setattr(extras, "chat_once", fake_chat_once)
    result = extras._chat_attempt(
        "hello", provider="P", model="m", workdir=None, timeout=5, thinking="off"
    )
    assert result["stdout"] == "one-shot"
    assert calls == ["hello"]


def test_idle_reaper_closes_idle_session_but_spares_busy_one(isolated_home, monkeypatch):
    import time

    class FakeSession:
        def __init__(self):
            self.alive = True
            self.busy = False

        def is_alive(self):
            return self.alive

        def is_busy(self):
            return self.busy

        def close(self):
            self.alive = False

    monkeypatch.setattr(rpc_session, "_idle_ttl_seconds", lambda: 0.05)
    session = FakeSession()
    with rpc_session._manager_lock:
        rpc_session._entry = {
            "session": session,
            "session_id": "sid",
            "env_by_provider": {},
            "current": ("P", "m"),
            "workdir": "",
            "thinking": "off",
            "last_used": time.monotonic(),
        }
        session.busy = True
        rpc_session._schedule_idle_reaper()
    time.sleep(0.15)
    assert session.alive is True, "busy session must not be reaped"

    session.busy = False
    with rpc_session._manager_lock:
        rpc_session._entry["last_used"] = time.monotonic() - 10
        rpc_session._schedule_idle_reaper()
    time.sleep(0.15)
    assert session.alive is False
    assert rpc_session._entry is None


def _sse_payload(text: str, model: str) -> bytes:
    chunks = [
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}
            ],
        },
        {
            "id": "c1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    ]
    return b"".join(
        f"data: {json.dumps(chunk)}\n\n".encode("utf-8") for chunk in chunks
    ) + b"data: [DONE]\n\n"


def test_rpc_chat_hot_switches_models_with_real_pi(isolated_home, monkeypatch, tmp_path):
    if not core.find_pi_command():
        pytest.skip("official Pi CLI is not installed")

    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("PI_OFFLINE", "1")

    seen: list[tuple[str, str]] = []

    class ProviderHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            seen.append((self.path, self.headers.get("Authorization", "")))
            if self.path.startswith("/b/"):
                payload = _sse_payload("REPLY-FROM-B", "model-b")
            else:
                payload = _sse_payload("REPLY-FROM-A", "model-a")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        core.upsert_custom_provider(
            "ProvA", base_url=f"{base}/a/v1", api_key="secret-a", models=[{"id": "model-a"}]
        )
        core.upsert_custom_provider(
            "ProvB", base_url=f"{base}/b/v1", api_key="secret-b", models=[{"id": "model-b"}]
        )
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(core.pi_agent_dir()))

        first = rpc_session.rpc_chat_once(
            "say hi", provider="ProvA", model="model-a", workdir=str(tmp_path), timeout=60
        )
        assert first["ok"] is True, first
        assert "REPLY-FROM-A" in first["stdout"]
        session_after_first = rpc_session._entry["session"]

        # Different provider: respawn with only that provider's env (sticky
        # session id keeps the conversation).
        second = rpc_session.rpc_chat_once(
            "and again", provider="ProvB", model="model-b", workdir=str(tmp_path), timeout=60
        )
        assert second["ok"] is True, second
        assert "REPLY-FROM-B" in second["stdout"]

        # Back to ProvA: the running process only carries ProvB's env, so the
        # process is respawned with ProvA's env; the sticky session id keeps
        # the conversation and the hot set_model path is not used across
        # providers (each process holds exactly one provider's keys).
        session_before_third = rpc_session._entry["session"]
        third = rpc_session.rpc_chat_once(
            "one more", provider="ProvA", model="model-a", workdir=str(tmp_path), timeout=60
        )
        assert third["ok"] is True, third
        assert "REPLY-FROM-A" in third["stdout"]
        assert rpc_session._entry["session"] is not session_before_third
        assert rpc_session._entry["session"] is not session_after_first

        paths = [path.split("/")[1] for path, _auth in seen]
        assert paths == ["a", "b", "a"]
        by_provider = {path.split("/")[1]: auth for path, auth in seen}
        assert by_provider["a"] == "Bearer secret-a"
        assert by_provider["b"] == "Bearer secret-b"
    finally:
        rpc_session.reset_chat_session()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
