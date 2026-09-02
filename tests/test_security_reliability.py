from __future__ import annotations

import base64
import codecs
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


def test_bom_prefixed_json_is_read_not_treated_as_corrupt(tmp_path):
    """带 UTF-8 BOM 的配置不得被误判 corrupt（Windows 手工编辑很常见）。

    BOM 本身是合法 UTF-8，decode 不会报错，但会留下 U+FEFF 首字符，而它不是
    合法 JSON 起始。若按 utf-8 严格解码，用户用记事本 / PowerShell 编辑过的
    settings.json 会整份被拒、且拒绝写入。
    """
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"version": 7}), encoding="utf-8-sig")
    assert path.read_bytes().startswith(codecs.BOM_UTF8)  # 确认 fixture 真带 BOM

    result = storage.load_json_result(path, {})
    assert result.status == "ok"
    assert result.data == {"version": 7}
    assert storage.load_json(path, {}) == {"version": 7}


def test_bom_is_tolerated_on_read_but_not_propagated_on_write(tmp_path):
    """容忍读入 BOM，但写回必须是无 BOM 的干净 UTF-8，不把 BOM 传播下去。"""
    path = tmp_path / "models.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8-sig")

    storage.save_json(path, {"a": 2})

    assert not path.read_bytes().startswith(codecs.BOM_UTF8)
    assert storage.load_json(path, {}) == {"a": 2}


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


def test_proxy_settings_reject_non_http_schemes(isolated_home, monkeypatch):
    from pi_manager import extras

    _clear_proxy_environment(monkeypatch)
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


def test_legacy_local_xor_vault_is_rejected_permanently(isolated_home, monkeypatch):
    """R2 P0-3：`local:` 旧格式的 XOR key 硬编码在程序里，任何本地进程都能零知识
    伪造出「合法」密文完成凭据注入，且旧实现读取成功后会立刻用 DPAPI 重写把它
    洗白成安全格式。该格式必须永久拒绝——连一次性迁移开关都不给。
    """
    legacy_key = b"PiManagerLocalFallbackKey!v1"
    payload = json.dumps({"provider:demo:apiKey": "sk-injected-by-attacker"}).encode("utf-8")
    blob = b"local:" + base64.b64encode(secrets._xor_stream(payload, legacy_key))
    vault = secrets._vault_path()
    vault.parent.mkdir(parents=True, exist_ok=True)
    vault.write_bytes(blob)

    with pytest.raises(secrets.VaultCorruptError):
        secrets.load_vault()
    # 未被「洗白」重写：原文件保持原样，不会变成 dpapi:/aesgcm: 让人误以为可信。
    assert vault.read_bytes() == blob

    # 即使显式打开一次性迁移开关也仍然拒绝。
    monkeypatch.setenv("PI_MANAGER_ALLOW_LEGACY_VAULT", "1")
    with pytest.raises(secrets.VaultCorruptError):
        secrets.load_vault()
    assert vault.read_bytes() == blob


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
    from pi_manager import platform_util

    def _is_reparse(path) -> bool:
        # Treat the master key / salt file as a reparse point so the
        # hardening check rejects it, regardless of platform.
        name = str(path)
        return name.endswith(".vault_master_key") or name.endswith(
            ".vault_master_key_salt"
        )

    monkeypatch.setattr(platform_util, "is_reparse_point", _is_reparse)
    with pytest.raises(secrets.VaultCorruptError):
        secrets._load_or_create_master_key()


def test_master_key_check_skips_when_api_unavailable(isolated_home, monkeypatch):
    from pi_manager import platform_util

    # When the file-attribute probe is unavailable, is_reparse_point already
    # returns False (no reparse point detected), so master-key creation must
    # proceed normally and still reject only genuine non-regular files.
    monkeypatch.setattr(platform_util, "is_reparse_point", lambda path: False)
    key = secrets._load_or_create_master_key()
    assert len(key) == 32


def test_broker_token_creation_is_exclusive(isolated_home):
    token = config_broker._create_broker_token()
    assert len(token) == 64
    with pytest.raises(FileExistsError):
        config_broker._create_broker_token()
    assert config_broker._verify_broker_token(token) is True
    assert config_broker._verify_broker_token("wrong-token") is False


def test_provider_redirect_does_not_replay_credentials(monkeypatch, isolated_home):
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


def test_model_response_without_length_stops_at_limit(monkeypatch, isolated_home):
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


def test_plaintext_vault_injection_is_rejected_without_master_key(isolated_home):
    """R2 P0-3 ①：明文 JSON 注入守卫不得依赖 `.vault_master_key` 是否存在。

    Windows 上 `encrypt_blob` 优先走 DPAPI，`_get_master_key()` 从不被调用 →
    该盐文件永不创建 → 旧守卫（`if _master_key_path().exists(): raise`）恒为假，
    任何可写 vault 的本地主体直接覆盖一份明文 JSON 就能注入凭据。这里刻意在
    「盐文件不存在」的状态下断言拒绝，即 Windows 上的真实状态。
    """
    assert not secrets._master_key_path().exists()  # 确认 fixture 状态就是守卫失效的前提
    vault = secrets._vault_path()
    vault.parent.mkdir(parents=True, exist_ok=True)
    injected = json.dumps({"provider:openrouter:apiKey": "sk-ATTACKER-CONTROLLED"})
    vault.write_text(injected, encoding="utf-8")

    with pytest.raises(secrets.VaultCorruptError):
        secrets.load_vault()
    # fail closed：读取单条密钥同样报错，不会静默返回注入值。
    with pytest.raises(secrets.VaultCorruptError):
        secrets.get_secret("provider:openrouter:apiKey")
    # 没有被重写成 dpapi:/aesgcm:（不给攻击者「洗白」）。
    assert vault.read_text(encoding="utf-8") == injected


def test_plaintext_vault_readable_only_with_explicit_migration_opt_in(
    isolated_home, monkeypatch
):
    """确属旧版本遗留的明文 vault 仍有迁移出路，但必须用户显式开一次开关。"""
    vault = secrets._vault_path()
    vault.parent.mkdir(parents=True, exist_ok=True)
    vault.write_text(json.dumps({"test_key": "test_value"}), encoding="utf-8")

    monkeypatch.setenv("PI_MANAGER_ALLOW_LEGACY_VAULT", "1")
    assert secrets.load_vault().get("test_key") == "test_value"
    # 读到即立刻升级为认证加密格式，不会一直以明文躺在磁盘上。
    assert vault.read_bytes().startswith((b"dpapi:", b"aesgcm:"))

    # 开关关掉后仍然可读（已经是认证加密格式了）。
    monkeypatch.delenv("PI_MANAGER_ALLOW_LEGACY_VAULT")
    assert secrets.load_vault().get("test_key") == "test_value"


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


def test_legacy_filekey_vault_requires_explicit_migration_opt_in(
    isolated_home, monkeypatch, caplog
):
    """R2 P0-3 / P2-5：`filekey:`（XOR，无认证）默认拒绝，只在显式打开一次性
    迁移开关时可读，且必须留 WARNING 审计记录。"""
    from pi_manager import secrets as secretstore

    secretstore._ensure_dir()
    # 用主密钥 + XOR 构造一份 legacy filekey: vault。第一次调用创建盐文件，
    # 第二次返回读取时 decrypt_blob 会用到的稳定值。
    secretstore._get_master_key()
    key = secretstore._get_master_key()
    assert secretstore._master_key_path().exists()
    payload = json.dumps({"legacy_key": "legacy_value"}, ensure_ascii=False).encode("utf-8")
    blob = b"filekey:" + base64.b64encode(secretstore._xor_stream(payload, key))
    vault = secretstore._vault_path()
    vault.write_bytes(blob)

    # 默认（开关未设置）：拒绝，且不重写文件。
    assert secretstore._LEGACY_DECRYPT_ALLOWED is False
    with pytest.raises(secretstore.VaultCorruptError):
        secretstore.load_vault()
    assert vault.read_bytes() == blob

    monkeypatch.setenv("PI_MANAGER_ALLOW_LEGACY_VAULT", "1")
    with caplog.at_level(logging.WARNING, logger="pi_manager.secrets"):
        result = secretstore.load_vault()

    assert result.get("legacy_key") == "legacy_value"
    assert any(
        record.levelno == logging.WARNING and "filekey" in record.getMessage()
        for record in caplog.records
    )
    # 读到即升级为认证加密格式。
    assert vault.read_bytes().startswith((b"dpapi:", b"aesgcm:"))


# ---- R2 P0-2：__DPAPI__ 跨 Provider 凭据窃取 ----


def test_dpapi_marker_from_untrusted_input_cannot_copy_another_provider_key(
    isolated_home,
):
    """R2 P0-2（已实证）：`__DPAPI__:X` 声明「我的 Key 存在 Provider X 名下」，
    旧实现无条件把 X 的真实凭据复制给当前 provider。外部输入（配置包）走到这条
    分支就等于一次导入完成密钥外泄，因此 trusted=False 必须直接拒绝。
    """
    victim = "sk-victim-real-key-0001"
    secrets.replace_provider_api_keys("openrouter", [victim])

    with pytest.raises(ValueError, match="__DPAPI__"):
        secrets.store_provider_api_key("evil", "__DPAPI__:openrouter", trusted=False)
    assert secrets.get_active_provider_credential("evil") is None

    with pytest.raises(ValueError, match="__DPAPI__"):
        secrets.resolve_provider_api_key("__DPAPI__:openrouter", "evil", trusted=False)

    with pytest.raises(ValueError, match="__DPAPI__"):
        secrets.migrate_plaintext_keys(
            {"evil": {"apiKey": "__DPAPI__:openrouter"}}, trusted=False
        )
    assert secrets.get_active_provider_credential("evil") is None
    # 受害者的凭据仍然只绑定在原 provider 上。
    active = secrets.get_active_provider_credential("openrouter")
    assert active is not None and active["value"] == victim


def test_dpapi_marker_still_migrates_a_renamed_local_provider(isolated_home):
    """本机旧配置的 provider 重命名迁移必须继续成立（不能为了修 P0-2 让升级用户
    丢密钥），但要留下 WARNING 审计痕迹。"""
    secrets.replace_provider_api_keys("Previous Name", ["sk-renamed-legacy-0001"])
    reference = secrets.store_provider_api_key("Current Name", "__DPAPI__:Previous Name")
    assert reference == secrets.provider_api_key_reference("Current Name")
    active = secrets.get_active_provider_credential("Current Name")
    assert active is not None and active["value"] == "sk-renamed-legacy-0001"


# ---- R2 P1-2：全大写 Key 被误判为环境变量引用 ----


def test_uppercase_credentials_are_never_treated_as_env_references(isolated_home):
    """R2 P1-2（已实证）：旧启发式 `[A-Z][A-Z0-9_]{2,}` 把 AWS 风格的真实
    Access Key ID 判成环境变量名 → 删掉安全存储记录、把**密钥本身**包成
    `${KEY}` 明文写进 models.json 并原样进入未加密导出，同时 provider 因变量
    不存在而静默失效。只承认显式 `$NAME` / `${NAME}`。
    """
    for sample in (
        "AKIAIOSFODNN7EXAMPLE",
        "ABCDEF0123456789ABCDEF",
        "SK_LIVE_ABCDEF123456",
        "GLPAT_ABCDEF123456",
    ):
        assert secrets.referenced_env_name(sample) == ""
    # 显式形式仍然照常识别。
    assert secrets.referenced_env_name("${OPENAI_API_KEY}") == "OPENAI_API_KEY"
    assert secrets.referenced_env_name("$OPENAI_API_KEY") == "OPENAI_API_KEY"


def test_uppercase_api_key_lands_in_secure_storage_not_models_json(isolated_home):
    aws_style = "AKIAIOSFODNN7EXAMPLE"
    core.upsert_custom_provider(
        "Upper",
        base_url="https://example.invalid/v1",
        api_key=aws_style,
        models=[{"id": "model-a"}],
    )
    assert aws_style not in core.models_path().read_text(encoding="utf-8")
    assert core.get_provider_config("Upper")["apiKey"] == (
        secrets.provider_api_key_reference("Upper")
    )
    # 功能面：Key 现在真的能解析出来（旧行为下永远解析成空字符串）。
    assert core.provider_runtime_env("Upper") == {
        secrets.provider_env_name("Upper"): aws_style
    }


def test_legacy_bare_env_name_migrates_only_when_the_variable_exists(
    isolated_home, monkeypatch
):
    """向后兼容：旧配置里真的填过裸变量名时给一条迁移路径——但必须是常规变量名
    形态（含下划线分段）且该变量确实存在于当前环境，否则一律当真实密钥保管。
    """
    monkeypatch.setenv("MY_LEGACY_PROVIDER_KEY", "env-secret")
    assert secrets.store_provider_api_key("Bare", "MY_LEGACY_PROVIDER_KEY") == (
        "${MY_LEGACY_PROVIDER_KEY}"
    )

    # 变量不存在：当成真实密钥存进安全存储（可恢复、无泄露）。
    assert secrets.store_provider_api_key("Bare2", "UNSET_LEGACY_NAME_HERE") == (
        secrets.provider_api_key_reference("Bare2")
    )
    active = secrets.get_active_provider_credential("Bare2")
    assert active is not None and active["value"] == "UNSET_LEGACY_NAME_HERE"

    # 无下划线分段（AWS 形态）即使碰巧存在于环境里也不承认。
    monkeypatch.setenv("AKIAIOSFODNN7EXAMPLE", "irrelevant")
    assert secrets._env_reference_name("AKIAIOSFODNN7EXAMPLE") == ""
    # 外部输入一律不做裸变量名兼容。
    assert secrets._env_reference_name("MY_LEGACY_PROVIDER_KEY", trusted=False) == ""


# ---- R2 P3-2：掩码收紧 ----


def test_masked_provider_key_hides_length_and_limits_visible_prefix(isolated_home):
    secrets.replace_provider_api_keys("Masked", ["sk-test-mask-value-xy"])
    masked = secrets.list_provider_keys("Masked")[0]["masked"]
    assert masked == "sk" + "*" * 8 + "xy"
    # 短 Key 与 8 字符 Key 的掩码等长：不通过掩码长度泄露密钥长度。
    assert secrets._masked_provider_key("abc") == secrets._masked_provider_key("a" * 8)



def test_config_broker_rejects_illegal_launch_tokens(isolated_home):
    """P0-1 回归：set_default_model 的 provider/model/thinking 必须过启动白名单。"""
    token = config_broker._create_broker_token()
    cases = [
        {"provider": "P", "model": "m", "thinking": "high&calc"},
        {"provider": "P", "model": "m", "thinking": "high|calc"},
        {"provider": 'x" & calc & "y', "model": "m"},
        {"provider": "P", "model": "m$(calc)"},
    ]
    for arguments in cases:
        result = config_broker.mutate(
            {
                "schema_version": 1,
                "request_id": "illegal-token-test",
                "token": token,
                "operation": "set_default_model",
                "arguments": arguments,
            }
        )
        # mutate 顶层把操作类错误统一折叠为「操作失败」（既有契约）——白名单
        # 拒绝在此表现为 ok=False 且不落库，断言不强绑具体文案。
        assert result["ok"] is False
        assert result["error"]
    assert "defaultThinkingLevel" not in core.load_settings()


def test_config_broker_accepts_legal_thinking_level(isolated_home):
    """合法 thinking（off/low/medium/high）仍可正常落库。"""
    token = config_broker._create_broker_token()
    result = config_broker.mutate(
        {
            "schema_version": 1,
            "request_id": "legal-token-test",
            "token": token,
            "operation": "set_default_model",
            "arguments": {"provider": "P", "model": "m", "thinking": "high"},
        }
    )
    assert result["ok"] is True
    assert core.load_settings()["defaultThinkingLevel"] == "high"
