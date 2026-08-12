from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path

import pytest

from pi_manager import core
from pi_manager import extras
from pi_manager import secrets as secretstore


def _write_zip(path, entries):
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, value in entries.items():
            bundle.writestr(name, value)


def test_encrypted_secret_bundle_round_trip(isolated_home, tmp_path):
    core.upsert_custom_provider(
        "Demo",
        base_url="https://example.invalid/v1",
        api_key="secret-value-never-in-zip",
        models=[{"id": "m"}],
    )
    second = core.add_provider_api_key("Demo", "second-secret-never-in-zip")
    first_id = core.list_provider_api_keys("Demo")[0]["id"]
    assert secretstore.mark_provider_key_failed("Demo", first_id, "HTTP 429")
    dest = tmp_path / "config.zip"
    extras.export_config_bundle(
        str(dest), include_secrets=True, password="correct horse battery"
    )
    raw = dest.read_bytes()
    assert b"secret-value-never-in-zip" not in raw
    assert b"second-secret-never-in-zip" not in raw
    with zipfile.ZipFile(dest) as bundle:
        assert "secrets.enc.json" in bundle.namelist()
        assert "secrets.vault.json" not in bundle.namelist()

    wrong = extras.import_config_bundle(
        str(dest), restore_secrets=True, password="incorrect password"
    )
    assert wrong["ok"] is False
    assert "密码错误" in wrong["error"]

    secretstore.delete_provider_api_keys("Demo")
    core.models_path().unlink()
    restored = extras.import_config_bundle(
        str(dest), restore_secrets=True, password="correct horse battery"
    )
    assert restored["ok"] is True
    rows = core.list_provider_api_keys("Demo")
    assert [(row["id"], row["status"]) for row in rows] == [
        (first_id, "cooldown"),
        (second["id"], "available"),
    ]
    assert core.provider_runtime_env("Demo") == {
        secretstore.provider_env_name("Demo"): "second-secret-never-in-zip"
    }


def test_tampered_encrypted_bundle_is_rejected(isolated_home, tmp_path):
    secretstore.set_secret("provider:Demo:apiKey", "secret")
    source = tmp_path / "source.zip"
    extras.export_config_bundle(
        str(source), include_secrets=True, password="correct horse battery"
    )
    with zipfile.ZipFile(source) as bundle:
        entries = {name: bundle.read(name) for name in bundle.namelist()}
    payload = json.loads(entries["secrets.enc.json"])
    payload["ciphertext"] = ("A" if payload["ciphertext"][0] != "A" else "B") + payload["ciphertext"][1:]
    entries["secrets.enc.json"] = json.dumps(payload)
    tampered = tmp_path / "tampered.zip"
    _write_zip(tampered, entries)
    result = extras.import_config_bundle(
        str(tampered), restore_secrets=True, password="correct horse battery"
    )
    assert result["ok"] is False
    assert "篡改" in result["error"]


def test_zip_slip_and_oversized_members_are_rejected(isolated_home, tmp_path):
    slipped = tmp_path / "slipped.zip"
    _write_zip(slipped, {"../settings.json": "{}"})
    result = extras.import_config_bundle(str(slipped))
    assert result["ok"] is False
    assert "非法路径" in result["error"]

    oversized = tmp_path / "oversized.zip"
    _write_zip(oversized, {"settings.json": b"x" * (5 * 1024 * 1024 + 1)})
    result = extras.import_config_bundle(str(oversized))
    assert result["ok"] is False
    assert "过大" in result["error"]


def test_invalid_json_does_not_modify_existing_config(isolated_home, tmp_path):
    core.save_settings({"before": True})
    invalid = tmp_path / "invalid.zip"
    _write_zip(invalid, {"settings.json": "{not-json"})
    result = extras.import_config_bundle(str(invalid))
    assert result["ok"] is False
    assert core.load_settings() == {"before": True}


def test_command_keys_are_permanently_rejected(isolated_home, tmp_path):
    variants = [
        "!credential-helper get",
        '"!credential-helper get"',
        "'!credential-helper get'",
        "  \t !credential-helper get  ",
        "\uff01credential-helper get",
    ]
    for index, value in enumerate(variants):
        bundle = tmp_path / f"command-{index}.zip"
        models = {
            "providers": {
                "Command Provider": {
                    "baseUrl": "https://example.invalid/v1",
                    "apiKey": value,
                    "models": [],
                }
            }
        }
        _write_zip(bundle, {"models.json": json.dumps(models)})
        rejected = extras.import_config_bundle(str(bundle), allow_commands=True)
        assert rejected["ok"] is False
        assert "!command" in rejected["error"]
        assert core.get_provider_config("Command Provider") is None


def test_command_headers_and_non_string_headers_are_rejected(isolated_home, tmp_path):
    for index, value in enumerate(['"!helper"', "' !helper '", {"command": "helper"}]):
        bundle = tmp_path / f"header-command-{index}.zip"
        models = {
            "providers": {
                "Command Provider": {
                    "baseUrl": "https://example.invalid/v1",
                    "apiKey": "${SAFE_KEY}",
                    "headers": {"Authorization": value},
                    "models": [],
                }
            }
        }
        _write_zip(bundle, {"models.json": json.dumps(models)})
        rejected = extras.import_config_bundle(str(bundle))
        assert rejected["ok"] is False
        assert core.get_provider_config("Command Provider") is None


def test_import_rejects_non_public_model_base_urls(isolated_home, tmp_path):
    cases = [
        "file:///etc/passwd",
        "ftp://example.invalid/v1",
        "gopher://example.invalid/1",
        "http://127.0.0.1:8080/v1",
        "http://localhost:8080/v1",
        "http://10.0.0.5/v1",
        "http://172.16.3.4/v1",
        "http://192.168.1.10/v1",
        "http://169.254.169.254/latest/meta-data",
        "http://[::1]:8080/v1",
        "http://[fe80::1]/v1",
        "http://ollama.local:11434/v1",
    ]
    for index, base_url in enumerate(cases):
        bundle = tmp_path / f"local-base-{index}.zip"
        models = {
            "providers": {
                "Local Host": {
                    "baseUrl": base_url,
                    "apiKey": "${SAFE_KEY}",
                    "models": [],
                }
            }
        }
        _write_zip(bundle, {"models.json": json.dumps(models)})
        rejected = extras.import_config_bundle(str(bundle))
        assert rejected["ok"] is False
        assert "baseUrl" in rejected["error"]
        assert "手动添加" in rejected["error"]
        assert core.get_provider_config("Local Host") is None


def test_import_accepts_public_model_base_url(isolated_home, tmp_path):
    bundle = tmp_path / "public-base.zip"
    models = {
        "providers": {
            "Public Host": {
                "baseUrl": "https://api.example.invalid/v1",
                "apiKey": "${SAFE_KEY}",
                "models": [],
            }
        }
    }
    _write_zip(bundle, {"models.json": json.dumps(models)})
    imported = extras.import_config_bundle(str(bundle))
    assert imported["ok"] is True
    assert core.get_provider_config("Public Host") is not None


def test_import_rejects_invalid_manager_proxy_url(isolated_home, tmp_path):
    for index, proxy_url in enumerate(
        ["ftp://proxy.example:8080", "file:///etc/passwd", "not-a-url"]
    ):
        bundle = tmp_path / f"proxy-{index}.zip"
        _write_zip(
            bundle,
            {"pi-manager.json": json.dumps({"proxy_url": proxy_url})},
        )
        rejected = extras.import_config_bundle(str(bundle))
        assert rejected["ok"] is False
        assert "代理" in rejected["error"]


def test_normal_export_removes_proxy_credentials(isolated_home, tmp_path):
    manager = core.load_manager_config()
    manager["proxy_url"] = "http://user:password@proxy.example:8080"
    core.save_manager_config(manager)
    bundle_path = tmp_path / "normal.zip"
    extras.export_config_bundle(str(bundle_path))
    with zipfile.ZipFile(bundle_path) as bundle:
        exported = json.loads(bundle.read("pi-manager.json"))
    assert exported["proxy_url"] == "http://proxy.example:8080"


def test_failed_import_rolls_back_files_and_secrets(
    isolated_home, tmp_path, monkeypatch
):
    core.save_settings({"before": True})
    core.save_models_config({"providers": {}})
    secretstore.set_secret("provider:Existing:apiKey", "keep-me")
    bundle = tmp_path / "rollback.zip"
    _write_zip(
        bundle,
        {
            "settings.json": json.dumps({"after": True}),
            "models.json": json.dumps(
                {
                    "providers": {
                        "Imported": {
                            "baseUrl": "https://example.invalid/v1",
                            "apiKey": "new-secret",
                            "models": [],
                        }
                    }
                }
            ),
        },
    )

    real_replace = os.replace
    failed = False

    def fail_once(source, destination):
        nonlocal failed
        if Path(destination) == core.settings_path() and not failed:
            failed = True
            raise OSError("simulated import failure")
        return real_replace(source, destination)

    monkeypatch.setattr(extras.os, "replace", fail_once)
    result = extras.import_config_bundle(str(bundle))
    assert result["ok"] is False
    assert core.load_settings() == {"before": True}
    assert core.load_models_config() == {"providers": {}}
    assert secretstore.get_secret("provider:Existing:apiKey") == "keep-me"
    assert secretstore.get_secret("provider:Imported:apiKey") == ""


def test_tampered_kdf_iterations_below_minimum_rejected(tmp_path, monkeypatch):
    """A bundle with KDF iterations below 100,000 should be rejected."""
    secrets_data = {"test": "value"}
    bundle = extras._encrypt_bundle_secrets(secrets_data, "test_password_123")

    # Tamper: set iterations to 99,999
    bundle["iterations"] = 99999

    # Attempt decryption should fail
    with pytest.raises(ValueError):
        extras._decrypt_bundle_secrets(bundle, "test_password_123")


def test_tampered_kdf_iterations_above_maximum_rejected(tmp_path, monkeypatch):
    """A bundle with KDF iterations above 2,000,000 should be rejected."""
    secrets_data = {"test": "value"}
    bundle = extras._encrypt_bundle_secrets(secrets_data, "test_password_123")
    bundle["iterations"] = 2000001

    with pytest.raises(ValueError):
        extras._decrypt_bundle_secrets(bundle, "test_password_123")
