from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pi_manager import core
from pi_manager import platform_util
from pi_manager import secrets as secretstore
from pi_manager.provider_env import main as provider_env_main


def _save_provider(name: str, key: str | None = "sk-test-secret") -> None:
    core.upsert_custom_provider(
        name,
        base_url="https://example.invalid/v1",
        api_key=key,
        models=[{"id": "model-a"}],
    )


def test_provider_key_is_referenced_and_injected(isolated_home):
    _save_provider("Demo")
    field = json.loads(core.models_path().read_text(encoding="utf-8"))["providers"]["Demo"]["apiKey"]
    assert field == secretstore.provider_api_key_reference("Demo")
    assert "sk-test-secret" not in core.models_path().read_text(encoding="utf-8")
    assert core.provider_runtime_env("Demo") == {
        secretstore.provider_env_name("Demo"): "sk-test-secret"
    }

    _save_provider("Demo", None)
    assert core.get_provider_config("Demo")["apiKey"] == field


def test_clear_and_delete_remove_provider_secret(isolated_home):
    _save_provider("Demo")
    _save_provider("Demo", "")
    assert core.get_provider_config("Demo")["apiKey"] == ""
    assert secretstore.get_secret(secretstore.provider_key_name("Demo")) == ""

    _save_provider("Demo", "replacement")
    core.delete_custom_provider("Demo")
    assert core.get_provider_config("Demo") is None
    assert secretstore.get_secret(secretstore.provider_key_name("Demo")) == ""


def test_legacy_dpapi_marker_is_migrated(isolated_home):
    secretstore.set_secret(secretstore.provider_key_name("Old Provider"), "old-secret")
    core.save_json(
        core.models_path(),
        {
            "providers": {
                "Old Provider": {
                    "baseUrl": "https://example.invalid/v1",
                    "apiKey": "__DPAPI__:Old Provider",
                    "models": [],
                }
            }
        },
    )
    cfg = core.load_models_config()
    assert cfg["providers"]["Old Provider"]["apiKey"] == secretstore.provider_api_key_reference("Old Provider")
    assert core.provider_runtime_env("Old Provider") == {
        secretstore.provider_env_name("Old Provider"): "old-secret"
    }


def test_renamed_legacy_provider_keeps_its_secret(isolated_home):
    secretstore.set_secret(secretstore.provider_key_name("Previous Name"), "old-secret")
    core.save_json(
        core.models_path(),
        {
            "providers": {
                "Current Name": {
                    "baseUrl": "https://example.invalid/v1",
                    "apiKey": "__DPAPI__:Previous Name",
                    "models": [],
                }
            }
        },
    )
    cfg = core.load_models_config()
    assert cfg["providers"]["Current Name"]["apiKey"] == (
        secretstore.provider_api_key_reference("Current Name")
    )
    assert core.provider_runtime_env("Current Name") == {
        secretstore.provider_env_name("Current Name"): "old-secret"
    }


def test_runtime_persists_reference_if_eager_migration_save_failed(
    isolated_home, monkeypatch
):
    secretstore.set_secret(secretstore.provider_key_name("Old Provider"), "old-secret")
    core.save_json(
        core.models_path(),
        {
            "providers": {
                "Old Provider": {
                    "baseUrl": "https://example.invalid/v1",
                    "apiKey": "__DPAPI__:Old Provider",
                    "models": [],
                }
            }
        },
    )
    monkeypatch.setattr(
        core, "save_models_config", lambda _cfg: (_ for _ in ()).throw(OSError("locked"))
    )
    assert core.load_models_config()["providers"]["Old Provider"]["apiKey"].startswith(
        "__DPAPI__"
    )
    assert core.provider_runtime_env("Old Provider") == {
        secretstore.provider_env_name("Old Provider"): "old-secret"
    }
    stored = core.load_json(core.models_path(), {})
    assert stored["providers"]["Old Provider"]["apiKey"] == (
        secretstore.provider_api_key_reference("Old Provider")
    )


def test_missing_referenced_key_has_actionable_error(isolated_home):
    _save_provider("Demo", "")
    cfg = core.load_models_config()
    cfg["providers"]["Demo"]["apiKey"] = secretstore.provider_api_key_reference("Demo")
    core.save_models_config(cfg)
    with pytest.raises(core.ProviderKeyError, match="重新填写 API Key"):
        core.provider_runtime_env("Demo")


def test_external_environment_reference(isolated_home, monkeypatch):
    monkeypatch.setenv("EXTERNAL_TEST_API_KEY", "external-secret")
    _save_provider("External", "EXTERNAL_TEST_API_KEY")
    assert core.get_provider_config("External")["apiKey"] == "${EXTERNAL_TEST_API_KEY}"
    assert core.provider_runtime_env("External") == {
        "EXTERNAL_TEST_API_KEY": "external-secret"
    }
    assert core.resolve_api_key_value("${EXTERNAL_TEST_API_KEY}") == "external-secret"


def test_npm_commands_do_not_invoke_a_shell(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = "1.2.3\n"
        stderr = ""

    monkeypatch.setattr(core.shutil, "which", lambda name: f"C:/tools/{name}")
    monkeypatch.setattr(
        core.subprocess,
        "run",
        lambda argv, **kwargs: calls.append((argv, kwargs)) or Result(),
    )

    assert core.get_latest_pi_version() == "1.2.3"
    assert core.install_or_update_pi() == (0, "1.2.3\n", "")
    assert len(calls) == 2
    assert all(kwargs["shell"] is False for _argv, kwargs in calls)
    assert all(isinstance(argv, list) for argv, _kwargs in calls)
    if sys.platform == "win32":
        assert all(argv[0].endswith("npm.cmd") for argv, _kwargs in calls)


def test_print_mode_and_terminal_receive_runtime_key(isolated_home, monkeypatch, tmp_path):
    _save_provider("Demo")
    env_name = secretstore.provider_env_name("Demo")
    mock_pi = tmp_path / "mock_pi.py"
    mock_pi.write_text(
        "import os, sys\n"
        f"ok = os.environ.get({env_name!r}) == 'sk-test-secret'\n"
        "print('AUTHORIZED' if ok else 'MISSING')\n"
        "raise SystemExit(0 if ok else 7)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "pi_base_cmd", lambda: [sys.executable, str(mock_pi)])
    code, stdout, _stderr = core.run_pi_print(
        "hello", provider="Demo", model="model-a", workdir=str(tmp_path)
    )
    assert code == 0
    assert stdout.strip() == "AUTHORIZED"

    captured = {}

    def fake_terminal(argv, workdir, terminal="auto", env=None):
        captured.update(argv=argv, workdir=workdir, terminal=terminal, env=env)
        return "started"

    monkeypatch.setattr(platform_util, "launch_in_terminal", fake_terminal)
    assert core.launch_pi_interactive(str(tmp_path), provider="Demo", model="model-a") == "started"
    assert captured["env"] == {env_name: "sk-test-secret"}
    assert "sk-test-secret" not in " ".join(captured["argv"])


def test_provider_env_helper_writes_private_response(isolated_home, tmp_path):
    _save_provider("Demo")
    output = tmp_path / "response.json"
    output.touch()
    assert provider_env_main(["--output", str(output), "Demo"]) == 0
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload == {
        "ok": True,
        "env": {secretstore.provider_env_name("Demo"): "sk-test-secret"},
    }


def test_official_pi_sends_real_key_to_provider(isolated_home, monkeypatch, tmp_path):
    if not core.find_pi_command():
        pytest.skip("official Pi CLI is not installed")

    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")
    monkeypatch.setenv("no_proxy", "*")

    received_authorization: list[str] = []

    class ProviderHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            authorization = self.headers.get("Authorization", "")
            received_authorization.append(authorization)
            if authorization != "Bearer real-integration-secret":
                payload = b'{"error":{"message":"Invalid API key"}}'
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            chunks = [
                {
                    "id": "chatcmpl-pi-manager",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "model-a",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": "AUTHORIZED"},
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl-pi-manager",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "model-a",
                    "choices": [
                        {"index": 0, "delta": {}, "finish_reason": "stop"}
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                },
            ]
            payload = b"".join(
                f"data: {json.dumps(chunk)}\n\n".encode("utf-8") for chunk in chunks
            ) + b"data: [DONE]\n\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), ProviderHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        core.upsert_custom_provider(
            "Pi Integration",
            base_url=f"http://127.0.0.1:{server.server_port}/v1",
            api_key="real-integration-secret",
            models=[{"id": "model-a"}],
        )
        monkeypatch.setenv("PI_CODING_AGENT_DIR", str(core.pi_agent_dir()))
        code, stdout, stderr = core.run_pi_print(
            "Reply with AUTHORIZED",
            provider="Pi Integration",
            model="model-a",
            workdir=str(tmp_path),
            timeout=30,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert code == 0, (stdout, stderr, received_authorization)
    assert "AUTHORIZED" in (stdout + stderr), (stdout, stderr, received_authorization)
    assert received_authorization == ["Bearer real-integration-secret"]
    assert "__DPAPI__" not in received_authorization[0]
