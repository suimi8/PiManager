# -*- coding: utf-8 -*-
"""S1：未加密导出包必须剥离明文 apiKey（引用化失败时置空并写 warnings）。"""
from __future__ import annotations

import json
import zipfile

from pi_manager import core
from pi_manager import extras
from pi_manager import secrets as secretstore

PLAINTEXT_KEY = "sk-plaintext-leak-123"


def _write_models_with_plaintext_key(key: str = PLAINTEXT_KEY) -> None:
    models = {
        "providers": {
            "Demo": {
                "baseUrl": "https://example.invalid/v1",
                "apiKey": key,
                "models": [{"id": "m"}],
            }
        }
    }
    core.ensure_agent_dir()
    core.models_path().write_text(json.dumps(models), encoding="utf-8")


def _read_bundle(path):
    with zipfile.ZipFile(path) as bundle:
        names = bundle.namelist()
        return {
            name: bundle.read(name)
            for name in names
        }


def test_plaintext_api_key_is_referenced_on_export(isolated_home, tmp_path):
    """vault 可用时：明文 apiKey 应被引用化，导出包不含明文。"""
    _write_models_with_plaintext_key()
    dest = tmp_path / "config.zip"
    extras.export_config_bundle(str(dest))
    raw = dest.read_bytes()
    assert PLAINTEXT_KEY.encode() not in raw
    files = _read_bundle(dest)
    exported = json.loads(files["models.json"].decode("utf-8"))
    meta = json.loads(files["export-meta.json"].decode("utf-8"))
    api_key = exported["providers"]["Demo"]["apiKey"]
    assert api_key.startswith("${PI_MANAGER_PROVIDER_")
    assert api_key.endswith("_API_KEY}")
    assert "warnings" not in meta
    # 明文应已迁入安全存储，可解析回原值
    credential = secretstore.get_active_provider_credential("Demo")
    assert credential is not None
    assert credential["value"] == PLAINTEXT_KEY


def test_unencrypted_export_strips_plaintext_when_vault_unavailable(
    isolated_home, tmp_path, monkeypatch
):
    """vault 不可用时：明文 apiKey 被置空并记录 warnings，绝不进导出包。"""
    _write_models_with_plaintext_key()

    def broken_store(provider: str, api_key: str) -> str:
        raise RuntimeError("simulated vault failure")

    monkeypatch.setattr(secretstore, "store_provider_api_key", broken_store)
    dest = tmp_path / "config.zip"
    extras.export_config_bundle(str(dest))
    raw = dest.read_bytes()
    assert PLAINTEXT_KEY.encode() not in raw
    files = _read_bundle(dest)
    exported = json.loads(files["models.json"].decode("utf-8"))
    meta = json.loads(files["export-meta.json"].decode("utf-8"))
    assert exported["providers"]["Demo"]["apiKey"] == ""
    warnings = meta.get("warnings")
    assert isinstance(warnings, list) and warnings
    assert any("Demo" in item and "apiKey" in item for item in warnings)


def test_reference_api_key_is_kept_without_storage_writes(
    isolated_home, tmp_path, monkeypatch
):
    """已是 ${...} 引用的 apiKey 保持原样，不触发任何存储写入。"""
    models = {
        "providers": {
            "Ref": {
                "baseUrl": "https://example.invalid/v1",
                "apiKey": "${PI_MANAGER_PROVIDER_REF_ABC123DEF456_API_KEY}",
                "models": [],
            }
        }
    }
    core.ensure_agent_dir()
    core.models_path().write_text(json.dumps(models), encoding="utf-8")

    def fail_if_called(provider: str, api_key: str) -> str:
        raise AssertionError("引用形式不应触发 store_provider_api_key")

    monkeypatch.setattr(secretstore, "store_provider_api_key", fail_if_called)
    dest = tmp_path / "config.zip"
    extras.export_config_bundle(str(dest))
    files = _read_bundle(dest)
    exported = json.loads(files["models.json"].decode("utf-8"))
    meta = json.loads(files["export-meta.json"].decode("utf-8"))
    assert exported["providers"]["Ref"]["apiKey"].startswith("${")
    assert "warnings" not in meta


def test_command_style_api_key_is_kept(isolated_home, tmp_path, monkeypatch):
    """`!` 开头的命令引用 apiKey 原样保留（非明文，不剥离）。"""
    models = {
        "providers": {
            "Cmd": {
                "baseUrl": "https://example.invalid/v1",
                "apiKey": "!credential-helper get",
                "models": [],
            }
        }
    }
    core.ensure_agent_dir()
    core.models_path().write_text(json.dumps(models), encoding="utf-8")

    def fail_if_called(provider: str, api_key: str) -> str:
        raise AssertionError("命令引用不应触发 store_provider_api_key")

    monkeypatch.setattr(secretstore, "store_provider_api_key", fail_if_called)
    dest = tmp_path / "config.zip"
    extras.export_config_bundle(str(dest))
    files = _read_bundle(dest)
    exported = json.loads(files["models.json"].decode("utf-8"))
    assert exported["providers"]["Cmd"]["apiKey"] == "!credential-helper get"
