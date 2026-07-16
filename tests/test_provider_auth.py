from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import urllib.error
from pathlib import Path
from types import SimpleNamespace
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


def test_openai_compatible_provider_uses_waf_safe_user_agent(isolated_home):
    core.upsert_custom_provider(
        "Responses",
        base_url="https://example.invalid/v1",
        api="openai-responses",
        api_key="sk-test-secret",
        models=[{"id": "model-a"}],
    )
    entry = core.get_provider_config("Responses")
    assert entry["headers"]["User-Agent"] == core.DEFAULT_OPENAI_COMPAT_USER_AGENT

    core.upsert_custom_provider(
        "Responses",
        base_url="https://example.invalid/v1",
        api="openai-responses",
        api_key=None,
        headers={"user-agent": "CustomClient/2.0"},
    )
    assert core.get_provider_config("Responses")["headers"] == {
        "user-agent": "CustomClient/2.0"
    }


def test_existing_openai_provider_is_migrated_to_safe_user_agent(isolated_home):
    core.save_json(
        core.models_path(),
        {
            "providers": {
                "Legacy": {
                    "baseUrl": "https://example.invalid/v1",
                    "api": "openai-responses",
                    "apiKey": "EXTERNAL_TEST_KEY",
                    "models": [{"id": "model-a"}],
                }
            }
        },
    )

    entry = core.load_models_config()["providers"]["Legacy"]
    assert entry["headers"]["User-Agent"] == core.DEFAULT_OPENAI_COMPAT_USER_AGENT
    persisted = json.loads(core.models_path().read_text(encoding="utf-8"))
    assert persisted["providers"]["Legacy"]["headers"] == entry["headers"]


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
    assert payload["ok"] is True
    assert payload["env"] == {
        secretstore.provider_env_name("Demo"): "sk-test-secret"
    }
    assert payload["key_id"]


def test_provider_env_helper_rotates_pool_across_processes(
    isolated_home, tmp_path
):
    _save_provider("Cross Process", "sk-process-first")
    core.add_provider_api_key("Cross Process", "sk-process-second")
    rows = core.list_provider_api_keys("Cross Process")
    repo_root = Path(core.__file__).resolve().parents[1]
    child_code = (
        "import sys;"
        "from pi_manager import secrets as s;"
        "s._KEYRING=None;s._KEYRING_TRIED=True;"
        "from pi_manager.provider_env import main;"
        "raise SystemExit(main(sys.argv[1:]))"
    )
    env = dict(os.environ)
    env["HOME"] = str(isolated_home)
    env["USERPROFILE"] = str(isolated_home)

    def invoke(name: str, *args: str) -> tuple[int, dict[str, object]]:
        output = tmp_path / name
        output.touch()
        completed = subprocess.run(
            [sys.executable, "-c", child_code, "--output", str(output), *args],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        return completed.returncode, json.loads(output.read_text(encoding="utf-8"))

    code, first = invoke("first.json", "Cross Process")
    assert code == 0
    assert first["key_id"] == rows[0]["id"]
    assert first["env"] == {
        secretstore.provider_env_name("Cross Process"): "sk-process-first"
    }

    code, marked_first = invoke(
        "marked-first.json",
        "--mark-failed",
        "--key-id",
        rows[0]["id"],
        "--reason",
        "HTTP 401",
        "Cross Process",
    )
    assert code == 0
    assert marked_first["has_available"] is True

    code, second = invoke("second.json", "Cross Process")
    assert code == 0
    assert second["key_id"] == rows[1]["id"]
    assert second["env"] == {
        secretstore.provider_env_name("Cross Process"): "sk-process-second"
    }

    code, marked_second = invoke(
        "marked-second.json",
        "--mark-failed",
        "--key-id",
        rows[1]["id"],
        "--reason",
        "HTTP 429",
        "Cross Process",
    )
    assert code == 0
    assert marked_second["has_available"] is False

    code, exhausted = invoke("exhausted.json", "Cross Process")
    assert code == 2
    assert exhausted["ok"] is False
    assert "全部暂时失效" in str(exhausted["error"])


def test_multiple_keys_rotate_and_restore_without_plaintext_config(
    isolated_home, monkeypatch
):
    _save_provider("Demo", "sk-first-secret")
    second = core.add_provider_api_key("Demo", "sk-second-secret")
    models_text = core.models_path().read_text(encoding="utf-8")
    assert "sk-first-secret" not in models_text
    assert "sk-second-secret" not in models_text

    env_name = secretstore.provider_env_name("Demo")
    attempted: list[str] = []

    def fake_run_pi(_args, **kwargs):
        value = kwargs["env"][env_name]
        attempted.append(value)
        if value == "sk-first-secret":
            return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 401 unauthorized")
        return SimpleNamespace(returncode=0, stdout="answer", stderr="")

    monkeypatch.setattr(core, "run_pi", fake_run_pi)
    code, stdout, stderr = core.run_pi_print(
        "hello", provider="Demo", model="model-a"
    )
    assert (code, stdout, stderr) == (0, "answer", "")
    assert attempted == ["sk-first-secret", "sk-second-secret"]

    rows = core.list_provider_api_keys("Demo")
    assert [row["status"] for row in rows] == ["invalid", "available"]
    assert rows[1]["id"] == second["id"]
    assert rows[1]["active"] is True
    assert all("secret" not in json.dumps(row) for row in rows)

    first_id = rows[0]["id"]
    assert core.restore_provider_api_key("Demo", first_id) is True
    assert core.restore_all_provider_api_keys("Demo") == 0
    assert all(row["status"] == "available" for row in core.list_provider_api_keys("Demo"))


def test_readding_failed_key_restores_without_duplicate(isolated_home):
    _save_provider("Demo", "sk-first-secret")
    first = core.list_provider_api_keys("Demo")[0]
    assert secretstore.mark_provider_key_failed("Demo", first["id"], "HTTP 429")

    restored = core.add_provider_api_key("Demo", "sk-first-secret")
    rows = core.list_provider_api_keys("Demo")

    assert restored["id"] == first["id"]
    assert len(rows) == 1
    assert rows[0]["status"] == "available"
    assert rows[0]["active"] is True
    assert rows[0]["failed_at"] == ""
    assert rows[0]["failure_reason"] == ""


def test_removing_active_key_promotes_next_available_key(isolated_home):
    _save_provider("Demo", "sk-first-secret")
    second = core.add_provider_api_key("Demo", "sk-second-secret")
    first = next(row for row in core.list_provider_api_keys("Demo") if row["active"])

    assert core.remove_provider_api_key("Demo", first["id"]) is True
    rows = core.list_provider_api_keys("Demo")

    assert len(rows) == 1
    assert rows[0]["id"] == second["id"]
    assert rows[0]["active"] is True
    assert core.provider_runtime_env("Demo") == {
        secretstore.provider_env_name("Demo"): "sk-second-secret"
    }


def test_editing_provider_with_existing_reference_preserves_key_pool(isolated_home):
    _save_provider("Demo", "sk-first-secret")
    core.add_provider_api_key("Demo", "sk-second-secret")
    entry = core.get_provider_config("Demo")

    core.upsert_custom_provider(
        "Demo",
        base_url="https://updated.example.invalid/v1",
        api=str(entry["api"]),
        api_key=str(entry["apiKey"]),
        models=list(entry["models"]),
    )

    assert len(core.list_provider_api_keys("Demo")) == 2
    assert core.provider_runtime_env("Demo") == {
        secretstore.provider_env_name("Demo"): "sk-first-secret"
    }


def test_adding_managed_key_activates_pool_for_external_reference(
    isolated_home, monkeypatch
):
    monkeypatch.setenv("EXTERNAL_TEST_API_KEY", "external-secret")
    _save_provider("External", "EXTERNAL_TEST_API_KEY")

    core.add_provider_api_key("External", "managed-secret")

    assert core.get_provider_config("External")["apiKey"] == (
        secretstore.provider_api_key_reference("External")
    )
    assert core.provider_runtime_env("External") == {
        secretstore.provider_env_name("External"): "managed-secret"
    }
    assert "managed-secret" not in core.models_path().read_text(encoding="utf-8")


def test_blocked_403_is_not_classified_as_provider_key_failure():
    blocked = "OpenAI API error (403): 403 Your request was blocked."
    assert core.is_provider_key_error(1, "", "HTTP 403 invalid API key") is True
    assert core.provider_key_failure_reason(1, "", "HTTP 403 invalid API key") == "HTTP 403"
    assert core.is_provider_key_error(1, "", blocked) is False
    assert core.provider_key_failure_reason(1, "", blocked) == ""


def test_blocked_403_does_not_rotate_or_disable_key(isolated_home, monkeypatch):
    _save_provider("Demo", "sk-first-secret")
    core.add_provider_api_key("Demo", "sk-second-secret")
    env_name = secretstore.provider_env_name("Demo")
    attempted: list[str] = []

    def fake_run_pi(_args, **kwargs):
        attempted.append(kwargs["env"][env_name])
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="OpenAI API error (403): 403 Your request was blocked.",
        )

    monkeypatch.setattr(core, "run_pi", fake_run_pi)
    code, _stdout, stderr = core.run_pi_print(
        "hello", provider="Demo", model="model-a"
    )

    assert code == 1
    assert "blocked" in stderr
    assert attempted == ["sk-first-secret"]
    assert all(row["status"] == "available" for row in core.list_provider_api_keys("Demo"))


def test_non_key_failure_does_not_rotate_or_disable_key(isolated_home, monkeypatch):
    _save_provider("Demo", "sk-first-secret")
    core.add_provider_api_key("Demo", "sk-second-secret")
    env_name = secretstore.provider_env_name("Demo")
    attempted: list[str] = []

    def fake_run_pi(_args, **kwargs):
        attempted.append(kwargs["env"][env_name])
        return SimpleNamespace(returncode=1, stdout="", stderr="HTTP 500 upstream error")

    monkeypatch.setattr(core, "run_pi", fake_run_pi)
    code, _stdout, stderr = core.run_pi_print(
        "hello", provider="Demo", model="model-a"
    )
    assert code == 1
    assert "500" in stderr
    assert attempted == ["sk-first-secret"]
    assert all(row["status"] == "available" for row in core.list_provider_api_keys("Demo"))


def test_http_500_auth_service_error_does_not_disable_key(
    isolated_home, monkeypatch
):
    _save_provider("Demo", "sk-first-secret")
    core.add_provider_api_key("Demo", "sk-second-secret")
    attempts: list[str] = []
    env_name = secretstore.provider_env_name("Demo")

    def fake_run_pi(_args, **kwargs):
        attempts.append(kwargs["env"][env_name])
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="HTTP 500 authentication service unavailable",
        )

    monkeypatch.setattr(core, "run_pi", fake_run_pi)
    code, _stdout, stderr = core.run_pi_print(
        "hello", provider="Demo", model="model-a"
    )

    assert code == 1
    assert "500" in stderr
    assert attempts == ["sk-first-secret"]
    assert all(row["status"] == "available" for row in core.list_provider_api_keys("Demo"))


def test_managed_model_fetch_rotates_to_second_key(isolated_home, monkeypatch):
    _save_provider("Demo", "sk-first-secret")
    core.add_provider_api_key("Demo", "sk-second-secret")
    entry = core.get_provider_config("Demo")
    attempted: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"data":[{"id":"model-a"}]}'

    class Opener:
        def open(self, request, timeout):
            authorization = str(request.get_header("Authorization") or "")
            attempted.append(authorization)
            if authorization == "Bearer sk-first-secret":
                raise urllib.error.HTTPError(
                    request.full_url,
                    401,
                    "Unauthorized",
                    {},
                    io.BytesIO(b'{"error":{"message":"invalid API key"}}'),
                )
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_handlers: Opener())
    result = core.fetch_remote_models(
        str(entry["baseUrl"]),
        str(entry["apiKey"]),
        api=str(entry["api"]),
        provider="Demo",
    )

    assert result["ok"] is True
    assert [item["id"] for item in result["models"]] == ["model-a"]
    assert attempted == [
        "Bearer sk-first-secret",
        "Bearer sk-second-secret",
    ]
    assert [row["status"] for row in core.list_provider_api_keys("Demo")] == [
        "invalid",
        "available",
    ]


def test_model_http_uses_the_same_waf_safe_user_agent(isolated_home, monkeypatch):
    _save_provider("Demo")
    received_user_agents: list[str] = []

    def fake_request(_url, *, headers, **_kwargs):
        received_user_agents.append(str(headers.get("User-Agent") or ""))
        return {
            "ok": True,
            "status": 200,
            "body": '{"choices":[{"message":{"content":"OK"}}]}',
            "latency_ms": 1,
            "proxy": "",
            "error": "",
        }

    monkeypatch.setattr(core, "_http_json_request", fake_request)
    result = core.test_model_http("Demo", "model-a")

    assert result["ok"] is True
    assert received_user_agents == [core.DEFAULT_OPENAI_COMPAT_USER_AGENT]


def test_managed_model_http_test_rotates_to_second_key(
    isolated_home, monkeypatch
):
    _save_provider("Demo", "sk-first-secret")
    core.add_provider_api_key("Demo", "sk-second-secret")
    attempted: list[str] = []

    def fake_request(_url, *, headers, **_kwargs):
        authorization = str(headers.get("Authorization") or "")
        attempted.append(authorization)
        if authorization == "Bearer sk-first-secret":
            return {
                "ok": False,
                "status": 401,
                "body": '{"error":{"message":"invalid API key"}}',
                "latency_ms": 1,
                "proxy": "",
                "error": "HTTP 401 Unauthorized",
            }
        return {
            "ok": True,
            "status": 200,
            "body": '{"choices":[{"message":{"content":"OK"}}]}',
            "latency_ms": 1,
            "proxy": "",
            "error": "",
        }

    monkeypatch.setattr(core, "_http_json_request", fake_request)
    result = core.test_model_http("Demo", "model-a")

    assert result["ok"] is True
    assert result["preview"] == "OK"
    assert attempted == [
        "Bearer sk-first-secret",
        "Bearer sk-second-secret",
    ]
    assert [row["status"] for row in core.list_provider_api_keys("Demo")] == [
        "invalid",
        "available",
    ]


def test_google_model_fetch_uses_real_key_but_redacts_result_endpoint(monkeypatch):
    requested: list[str] = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"models":[{"name":"models/gemini-test"}]}'

    class Opener:
        def open(self, request, timeout):
            requested.append(request.full_url)
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_handlers: Opener())
    result = core.fetch_remote_models(
        "https://generativelanguage.googleapis.com/v1beta",
        "google-real-secret",
        api="google-generative-ai",
    )

    assert result["ok"] is True
    assert requested == [
        "https://generativelanguage.googleapis.com/v1beta/models?key=google-real-secret"
    ]
    assert result["endpoint"].endswith("?key=***")
    assert "google-real-secret" not in json.dumps(result)


def test_google_model_fetch_redacts_endpoint_on_http_error(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 401, "Unauthorized", {}, None
            )

    monkeypatch.setattr("urllib.request.build_opener", lambda *_handlers: Opener())
    result = core.fetch_remote_models(
        "https://generativelanguage.googleapis.com/v1beta",
        "google-real-secret",
        api="google-generative-ai",
    )

    assert result["ok"] is False
    assert result["endpoint"].endswith("?key=***")
    assert "google-real-secret" not in json.dumps(result)


def test_google_model_test_uses_real_key_but_redacts_result_endpoint(
    isolated_home, monkeypatch
):
    core.upsert_custom_provider(
        "Google Demo",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="google-real-secret",
        api="google-generative-ai",
        models=[{"id": "gemini-test"}],
    )
    requested: list[str] = []

    def fake_request(url, **_kwargs):
        requested.append(url)
        return {
            "ok": True,
            "status": 200,
            "body": '{"candidates":[]}',
            "latency_ms": 1,
            "proxy": "",
            "error": "",
        }

    monkeypatch.setattr(core, "_http_json_request", fake_request)
    result = core.test_model_http("Google Demo", "gemini-test")

    assert requested == [
        "https://generativelanguage.googleapis.com/v1beta/models/"
        "gemini-test:generateContent?key=google-real-secret"
    ]
    assert result["endpoint"].endswith("?key=***")
    assert "google-real-secret" not in json.dumps(result)


def test_all_failed_keys_require_manual_restore_and_helper_can_mark(
    isolated_home, tmp_path
):
    _save_provider("Demo", "sk-first-secret")
    core.add_provider_api_key("Demo", "sk-second-secret")
    rows = core.list_provider_api_keys("Demo")

    output = tmp_path / "mark.json"
    output.touch()
    assert provider_env_main(
        [
            "--output",
            str(output),
            "--mark-failed",
            "--key-id",
            rows[0]["id"],
            "--reason",
            "HTTP 429",
            "Demo",
        ]
    ) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["has_available"] is True
    assert secretstore.mark_provider_key_failed("Demo", rows[1]["id"], "HTTP 401")

    with pytest.raises(core.ProviderKeyError, match="全部暂时失效"):
        core.provider_runtime_env("Demo")
    assert core.restore_all_provider_api_keys("Demo") == 2
    assert core.provider_runtime_env("Demo") == {
        secretstore.provider_env_name("Demo"): "sk-first-secret"
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
