from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pi_manager import config_broker, core, helper_registry, secrets, storage


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
    token = config_broker._create_broker_token()

    def switch(index: int):
        result = config_broker.mutate(
            {
                "schema_version": 1,
                "request_id": str(index),
                "token": token,
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


def test_helper_registry_publishes_non_secret_command(isolated_home):
    payload = helper_registry.register_current_helper()
    saved = storage.load_json(helper_registry.registry_path(), {})

    assert saved == payload
    assert payload["schema_version"] == 1
    assert payload["command"][0]
    assert set(payload) == {"schema_version", "command", "updated_at", "pid"}
    if os.name != "nt":
        mode = helper_registry.registry_path().stat().st_mode & 0o777
        assert mode & 0o077 == 0


def test_private_json_mode_survives_updates_and_backups(isolated_home, tmp_path):
    path = tmp_path / "private.json"
    storage.save_json(path, {"v": 1}, private=True)
    # A later writer that does not know about privacy must not widen it.
    storage.update_json(path, {}, lambda data: {**data, "v": 2})
    storage.save_json(path, {"v": 3})
    assert storage.load_json(path, {}) == {"v": 3}
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        backup = tmp_path / "private.json.bak.1"
        assert backup.stat().st_mode & 0o077 == 0


def test_private_write_clamps_backup_of_previously_wide_file(isolated_home, tmp_path):
    path = tmp_path / "cfg.json"
    storage.save_json(path, {"v": 1})
    if os.name != "nt":
        os.chmod(path, 0o644)
    # First private write rotates the old world-readable file into a backup;
    # that backup must be clamped, not inherit the old mode.
    storage.save_json(path, {"v": 2}, private=True)
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
        assert (tmp_path / "cfg.json.bak.1").stat().st_mode & 0o077 == 0


def test_session_delete_and_rename_are_confined_to_sessions_dir(isolated_home):
    from pi_manager import extras

    sessions = core.sessions_dir()
    sessions.mkdir(parents=True)
    inside = sessions / "inside.json"
    inside.write_text("{}", encoding="utf-8")
    outside = Path(isolated_home) / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    assert extras.session_delete(str(inside)) is True
    assert not inside.exists()
    assert extras.session_delete(str(outside)) is False
    assert outside.exists()

    keep = sessions / "keep.json"
    keep.write_text("{}", encoding="utf-8")
    renamed = extras.session_rename(str(keep), "renamed")
    assert Path(renamed).name == "renamed.json"
    assert (sessions / "renamed.json").exists()

    with pytest.raises(ValueError):
        extras.session_rename(str(renamed), "../escape")
    with pytest.raises(ValueError):
        extras.session_rename(str(renamed), "sub/name")
    with pytest.raises(ValueError):
        extras.session_rename(str(renamed), "")


def test_proxy_settings_reject_non_http_schemes(isolated_home):
    from pi_manager import extras

    with pytest.raises(ValueError, match="代理"):
        extras.set_proxy_settings(True, "file:///etc/passwd")
    with pytest.raises(ValueError, match="代理"):
        extras.set_proxy_settings(True, "not-a-url")
    assert extras.set_proxy_settings(True, "http://proxy.example:8080")["url"] == (
        "http://proxy.example:8080"
    )
    assert extras.set_proxy_settings(False, "")["url"] == ""


def test_failure_reason_strips_control_characters(isolated_home):
    core.upsert_custom_provider(
        "Sanitize",
        base_url="https://example.invalid/v1",
        api_key="sk-sanitize",
        models=[{"id": "m"}],
    )
    row = core.list_provider_api_keys("Sanitize")[0]
    evil = "boom\x00\x01\x1b[31m\x07\u0007\nnext"
    secrets.mark_provider_key_failed("Sanitize", row["id"], evil)
    stored = core.list_provider_api_keys("Sanitize")[0]["failure_reason"]
    assert stored == "boom[31m\nnext"


def test_import_write_path_keeps_manager_config_private(isolated_home, tmp_path):
    from pi_manager import extras

    target = tmp_path / "pi-manager.json"
    extras._atomic_replace_bytes(target, b"{}\n", private=True)
    if os.name != "nt":
        assert target.stat().st_mode & 0o077 == 0


def test_manager_config_is_written_owner_only(isolated_home):
    core.save_manager_config({"proxy_enabled": False, "proxy_url": ""})
    assert storage.load_json(core.manager_config_path(), {})["proxy_enabled"] is False
    if os.name != "nt":
        assert core.manager_config_path().stat().st_mode & 0o077 == 0


def test_legacy_vault_is_upgraded_to_authenticated_encryption(isolated_home):
    legacy_key = b"PiManagerLocalFallbackKey!v1"
    payload = json.dumps({"provider:demo:apiKey": "sk-legacy"}).encode("utf-8")
    blob = b"local:" + base64.b64encode(secrets._xor_stream(payload, legacy_key))
    vault = secrets._vault_path()
    vault.parent.mkdir(parents=True, exist_ok=True)
    vault.write_bytes(blob)

    assert secrets.load_vault() == {"provider:demo:apiKey": "sk-legacy"}
    upgraded = vault.read_bytes()
    assert upgraded.startswith((b"dpapi:", b"aesgcm:"))
    assert secrets.load_vault() == {"provider:demo:apiKey": "sk-legacy"}


def test_provider_env_output_must_be_existing_regular_file(isolated_home, tmp_path):
    from pi_manager import provider_env

    with pytest.raises(ValueError):
        provider_env._emit({"ok": True}, str(tmp_path / "missing.json"))

    real = tmp_path / "real.json"
    real.write_text("", encoding="utf-8")
    link = tmp_path / "link.json"
    try:
        link.symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError):
        provider_env._emit({"ok": True}, str(link))


def test_lock_file_symlink_is_never_followed(isolated_home, tmp_path):
    target = tmp_path / "target.bin"
    target.write_bytes(b"")
    lock = tmp_path / ".cfg.json.lock"
    try:
        lock.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    cfg = tmp_path / "cfg.json"
    storage.save_json(cfg, {"ok": True})
    assert target.read_bytes() == b""
    assert json.loads(cfg.read_text(encoding="utf-8")) == {"ok": True}


def test_master_key_reparse_point_is_rejected(isolated_home, monkeypatch):
    if os.name != "nt":
        pytest.skip("reparse-point checks are Windows-only")
    monkeypatch.setattr(
        secrets,
        "_windows_file_attributes",
        lambda path: secrets._FILE_ATTRIBUTE_REPARSE_POINT
        if str(path).endswith(".vault_master_key")
        else None,
    )
    with pytest.raises(secrets.VaultCorruptError):
        secrets._load_or_create_master_key()


def test_master_key_check_skips_when_api_unavailable(isolated_home, monkeypatch):
    monkeypatch.setattr(secrets, "_windows_file_attributes", lambda path: None)
    key = secrets._load_or_create_master_key()
    assert len(key) == 32


def test_broker_token_creation_is_exclusive(isolated_home):
    token = config_broker._create_broker_token()
    assert len(token) == 64
    with pytest.raises(FileExistsError):
        config_broker._create_broker_token()
    assert config_broker._verify_broker_token(token) is True
    assert config_broker._verify_broker_token("wrong-token") is False


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


def test_plaintext_vault_is_accepted_for_backward_compat(tmp_path, monkeypatch):
    """A vault file that is plain JSON (no encryption prefix) is accepted
    for backward compatibility, but encrypted files that fail to decrypt
    raise VaultCorruptError."""
    from pi_manager import secrets as secretstore

    monkeypatch.setattr(secretstore, "_vault_path", lambda: tmp_path / "secrets.vault")
    monkeypatch.setattr(secretstore, "_master_key_path", lambda: tmp_path / ".vault_master_key")
    monkeypatch.setattr(secretstore, "_index_path", lambda: tmp_path / "secrets.index.json")
    monkeypatch.setattr(secretstore, "_legacy_vault_path", lambda: tmp_path / "secrets.dpapi")
    monkeypatch.setattr(secretstore, "_mutation_lock_path", lambda: tmp_path / "secrets.mutation")

    # Plain JSON without encryption prefix should work (backward compat)
    plain_data = {"test_key": "test_value"}
    (tmp_path / "secrets.vault").write_text(json.dumps(plain_data), encoding="utf-8")
    result = secretstore.load_vault()
    assert result.get("test_key") == "test_value"


def test_corrupt_encrypted_vault_fails_closed(tmp_path, monkeypatch):
    """An encrypted vault with tampered ciphertext must fail closed."""
    from pi_manager import secrets as secretstore

    monkeypatch.setattr(secretstore, "_vault_path", lambda: tmp_path / "secrets.vault")
    monkeypatch.setattr(secretstore, "_master_key_path", lambda: tmp_path / ".vault_master_key")
    monkeypatch.setattr(secretstore, "_index_path", lambda: tmp_path / "secrets.index.json")
    monkeypatch.setattr(secretstore, "_legacy_vault_path", lambda: tmp_path / "secrets.dpapi")
    monkeypatch.setattr(secretstore, "_mutation_lock_path", lambda: tmp_path / "secrets.mutation")

    # Save a valid vault
    secretstore._ensure_dir()
    secretstore.save_vault({"secret_key": "secret_value"})

    # Tamper with the encrypted content
    raw = (tmp_path / "secrets.vault").read_bytes()
    if raw.startswith(b"aesgcm:") or raw.startswith(b"dpapi:"):
        # Flip a byte in the ciphertext portion (after the prefix and colon)
        prefix_end = raw.index(b":") + 1
        tampered = raw[:prefix_end + 1] + bytes([raw[prefix_end + 1] ^ 1]) + raw[prefix_end + 2:]
        (tmp_path / "secrets.vault").write_bytes(tampered)

        with pytest.raises(secretstore.VaultCorruptError):
            secretstore.load_vault()


def test_cooldown_expiry_restores_key_to_available(tmp_path, monkeypatch):
    """A key in cooldown state should auto-restore to available when retry_at expires."""
    from pi_manager import secrets as secretstore
    from datetime import datetime, timezone, timedelta

    monkeypatch.setattr(secretstore, "_vault_path", lambda: tmp_path / "secrets.vault")
    monkeypatch.setattr(secretstore, "_master_key_path", lambda: tmp_path / ".vault_master_key")
    monkeypatch.setattr(secretstore, "_index_path", lambda: tmp_path / "secrets.index.json")
    monkeypatch.setattr(secretstore, "_legacy_vault_path", lambda: tmp_path / "secrets.dpapi")
    monkeypatch.setattr(secretstore, "_mutation_lock_path", lambda: tmp_path / "secrets.mutation")
    monkeypatch.setattr(secretstore, "_provider_key_pool_lock_path", lambda: tmp_path / "pk.mutation")

    # Disable keyring so vault is used for storage
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", True)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED_AT", time.monotonic())

    secretstore._ensure_dir()
    # Create a pool with a key in cooldown with expired retry_at
    past_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    pool_data = {
        "version": 1,
        "active_id": "",
        "keys": [
            {
                "id": "test_key_id",
                "value": "sk-test-key-value",
                "status": "cooldown",
                "failed_at": past_time,
                "retry_at": past_time,
                "failure_kind": "rate_limit",
                "failure_count": 1,
                "failure_reason": "rate limited",
            }
        ],
    }
    secretstore.set_secret(
        secretstore.provider_key_pool_name("test_provider"),
        json.dumps(pool_data, ensure_ascii=False, separators=(",", ":")),
    )

    # Load the pool - should auto-restore the key to available
    pool = secretstore.load_provider_key_pool("test_provider")
    assert pool["keys"][0]["status"] == "available"
    assert pool["active_id"] == "test_key_id"


def test_redact_secret_values_handles_substring_secrets():
    """子串密钥场景：短密钥是长密钥子串时，一次性正则替换不留残片。"""
    result = core.redact_secret_values(
        "prefix SHORT postfix LONGSECRET tail", ["SHORT", "LONGSECRET"]
    )
    assert "LONGSECRET" not in result
    assert "SHORT" not in result
    # 长度小于 4 的短密钥应被跳过，不替换无关文本
    assert core.redact_secret_values("ababab", ["ab"]) == "ababab"


def test_redact_secret_values_filters_short_secrets():
    """长度小于 4 的 secret 值不参与替换，避免误伤。"""
    assert core.redact_secret_values("hello world", ["world", "xy"]) == "hello ***"


def test_plaintext_vault_rejected_after_initialization(tmp_path, monkeypatch):
    """When the master key already exists (vault initialized to encrypted),
    a plaintext JSON vault file must be rejected as VaultCorruptError to
    prevent credential-injection by a local attacker swapping in plaintext."""
    from pi_manager import secrets as secretstore

    monkeypatch.setattr(secretstore, "_vault_path", lambda: tmp_path / "secrets.vault")
    monkeypatch.setattr(secretstore, "_master_key_path", lambda: tmp_path / ".vault_master_key")
    monkeypatch.setattr(secretstore, "_index_path", lambda: tmp_path / "secrets.index.json")
    monkeypatch.setattr(secretstore, "_legacy_vault_path", lambda: tmp_path / "secrets.dpapi")
    monkeypatch.setattr(secretstore, "_mutation_lock_path", lambda: tmp_path / "secrets.mutation")

    # Disable keyring so the file vault is the storage backend.
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", True)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED_AT", time.monotonic())

    secretstore._ensure_dir()
    # Initializing an encrypted vault creates the master key salt file. On
    # Windows save_vault() uses DPAPI and never touches the master key, so
    # force-create the salt here to model an initialized encrypted vault on
    # every platform.
    secretstore.save_vault({"real_secret": "real_value"})
    secretstore._get_master_key()
    assert (tmp_path / ".vault_master_key").exists()

    # Attacker replaces the encrypted vault with plaintext JSON.
    plain_data = {"injected_key": "attacker_value"}
    (tmp_path / "secrets.vault").write_text(json.dumps(plain_data), encoding="utf-8")

    with pytest.raises(secretstore.VaultCorruptError):
        secretstore.load_vault()


def test_legacy_xor_vault_logs_warning(tmp_path, monkeypatch, caplog):
    """A filekey: (XOR, unauthenticated) vault stays readable for backward
    compatibility, but a WARNING audit record must be emitted."""
    from pi_manager import secrets as secretstore

    monkeypatch.setattr(secretstore, "_vault_path", lambda: tmp_path / "secrets.vault")
    monkeypatch.setattr(secretstore, "_master_key_path", lambda: tmp_path / ".vault_master_key")
    monkeypatch.setattr(secretstore, "_index_path", lambda: tmp_path / "secrets.index.json")
    monkeypatch.setattr(secretstore, "_legacy_vault_path", lambda: tmp_path / "secrets.dpapi")
    monkeypatch.setattr(secretstore, "_mutation_lock_path", lambda: tmp_path / "secrets.mutation")

    # Disable keyring so the file vault is the storage backend.
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", True)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED_AT", time.monotonic())

    secretstore._ensure_dir()
    # Build a legacy filekey: vault using the master key + XOR stream cipher.
    # The first call to _get_master_key() creates the salt; the second call
    # returns the stable value decrypt_blob will use at read time.
    secretstore._get_master_key()
    key = secretstore._get_master_key()
    assert (tmp_path / ".vault_master_key").exists()
    payload = json.dumps({"legacy_key": "legacy_value"}, ensure_ascii=False).encode("utf-8")
    blob = b"filekey:" + base64.b64encode(secretstore._xor_stream(payload, key))
    (tmp_path / "secrets.vault").write_bytes(blob)

    with caplog.at_level(logging.WARNING, logger="pi_manager.secrets"):
        result = secretstore.load_vault()

    assert result.get("legacy_key") == "legacy_value"
    assert any(
        record.levelno == logging.WARNING and "filekey" in record.getMessage()
        for record in caplog.records
    )
