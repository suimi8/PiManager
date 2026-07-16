from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from pi_manager import config_broker, core, secrets, storage


def _clear_proxy_environment(monkeypatch):
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)


def test_corrupt_json_is_explicit_and_cannot_be_overwritten(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"truncated":', encoding="utf-8")

    result = storage.load_json_result(path, {})
    assert result.status == "corrupt"
    with pytest.raises(storage.CorruptJsonError):
        storage.load_json(path, {})
    with pytest.raises(storage.CorruptJsonError):
        storage.save_json(path, {"replacement": True})
    assert path.read_text(encoding="utf-8") == '{"truncated":'


def test_json_writes_keep_two_valid_backups(tmp_path):
    path = tmp_path / "settings.json"
    storage.save_json(path, {"version": 1})
    storage.save_json(path, {"version": 2})
    storage.save_json(path, {"version": 3})

    assert storage.load_json(path, {}) == {"version": 3}
    assert json.loads((tmp_path / "settings.json.bak.1").read_text(encoding="utf-8")) == {"version": 2}
    assert json.loads((tmp_path / "settings.json.bak.2").read_text(encoding="utf-8")) == {"version": 1}


def test_tampered_vault_fails_closed_and_is_not_overwritten(isolated_home):
    secrets.set_secret("existing", "keep-me")
    vault = secrets._vault_path()
    original = vault.read_bytes()
    vault.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    tampered = vault.read_bytes()

    with pytest.raises(secrets.VaultCorruptError):
        secrets.load_vault()
    with pytest.raises(secrets.VaultCorruptError):
        secrets.set_secret("new", "must-not-write")
    assert vault.read_bytes() == tampered


def test_config_broker_concurrent_field_mutations_preserve_settings(isolated_home):
    core.save_settings({"unrelated": "keep", "enabledModels": ["Base/m"]})

    def switch(index: int):
        result = config_broker.mutate(
            {
                "schema_version": 1,
                "request_id": str(index),
                "operation": "set_default_model",
                "arguments": {
                    "provider": "P",
                    "model": f"m-{index}",
                    "favorites": ["Fav/m"],
                    "sync_enabled": True,
                },
            }
        )
        assert result["ok"] is True

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(switch, range(40)))

    settings = core.load_settings()
    assert settings["unrelated"] == "keep"
    assert "Base/m" in settings["enabledModels"]
    assert "Fav/m" in settings["enabledModels"]
    assert settings["defaultProvider"] == "P"
    revisions = storage.load_json(core.pi_agent_dir() / ".config-revisions.json", {})
    assert revisions["settings.json"]["revision"] == 40


def test_provider_redirect_does_not_replay_credentials(monkeypatch):
    _clear_proxy_environment(monkeypatch)
    target_requests = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            target_requests.append(dict(self.headers))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    target = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header(
                "Location", f"http://127.0.0.1:{target.server_port}/stolen"
            )
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in (source, target)
    ]
    for thread in threads:
        thread.start()
    try:
        result = core.fetch_remote_models(
            f"http://127.0.0.1:{source.server_port}/v1",
            "redirect-secret",
        )
    finally:
        for server in (source, target):
            server.shutdown()
            server.server_close()
        for thread in threads:
            thread.join(timeout=5)

    assert result["ok"] is False
    assert result["http_status"] == 302
    assert target_requests == []
    assert "redirect-secret" not in json.dumps(result)


def test_model_response_without_length_stops_at_limit(monkeypatch):
    _clear_proxy_environment(monkeypatch)

    class LargeHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            chunk = b"x" * (64 * 1024)
            try:
                for _ in range(80):
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), LargeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = core.fetch_remote_models(
            f"http://127.0.0.1:{server.server_port}/v1",
            "bounded-secret",
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["ok"] is False
    assert "超过" in result["error"]
    assert "bounded-secret" not in json.dumps(result)


def test_sensitive_provider_headers_are_vaulted_and_resolved(isolated_home, tmp_path):
    from pi_manager import extras

    core.upsert_custom_provider(
        "Headers",
        base_url="https://example.invalid/v1",
        api_key="sk-provider",
        headers={
            "Authorization": "Bearer custom-header-secret",
            "X-Api-Token": "token-secret",
            "User-Agent": "Custom/1.0",
        },
        models=[{"id": "m"}],
    )
    entry = core.get_provider_config("Headers")
    serialized = core.models_path().read_text(encoding="utf-8")
    assert "custom-header-secret" not in serialized
    assert "token-secret" not in serialized
    assert entry["headers"]["Authorization"].startswith("${PI_MANAGER_PROVIDER_")
    assert entry["headers"]["User-Agent"] == "Custom/1.0"

    env = core.provider_runtime_env("Headers")
    assert "Bearer custom-header-secret" in env.values()
    assert "token-secret" in env.values()

    bundle = tmp_path / "headers.zip"
    extras.export_config_bundle(str(bundle))
    assert b"custom-header-secret" not in bundle.read_bytes()
    assert b"token-secret" not in bundle.read_bytes()

    names_before = secrets.list_secret_names()
    assert any(":header:" in name for name in names_before)
    core.delete_custom_provider("Headers")
    assert not any(
        name.startswith("provider:Headers:header:")
        for name in secrets.list_secret_names()
    )


def test_provider_key_state_machine(isolated_home):
    core.upsert_custom_provider(
        "Demo",
        base_url="https://example.invalid/v1",
        api_key="sk-first",
        models=[{"id": "m"}],
    )
    core.add_provider_api_key("Demo", "sk-second")
    rows = core.list_provider_api_keys("Demo")

    assert secrets.mark_provider_key_failed("Demo", rows[0]["id"], "HTTP 429")
    first = core.list_provider_api_keys("Demo")[0]
    assert first["status"] == "cooldown"
    assert first["failure_kind"] == "rate_limit"
    assert first["retry_at"]

    assert secrets.mark_provider_key_failed("Demo", rows[1]["id"], "quota exceeded")
    second = core.list_provider_api_keys("Demo")[1]
    assert second["status"] == "restricted"
    assert second["failure_kind"] == "account_restricted"

    assert core.classify_provider_key_failure(1, "", "HTTP 500 upstream")["status"] == ""
    assert core.classify_provider_key_failure(1, "", "connect timed out")["status"] == ""
