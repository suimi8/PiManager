# -*- coding: utf-8 -*-
"""Vault 热路径缓存、keyring 回退可见性与自检 warn。"""
from __future__ import annotations

import logging
import os
import urllib.request

import keyring
import pytest
from keyring.backend import KeyringBackend

from pi_manager import extras
from pi_manager import secrets as secretstore


class _FakeKeyring(KeyringBackend):
    priority = 10

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self.store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop((service, username), None)


class _BrokenKeyring(_FakeKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        raise RuntimeError("keyring unavailable")

    def set_password(self, service: str, username: str, password: str) -> None:
        raise RuntimeError("keyring unavailable")

    def delete_password(self, service: str, username: str) -> None:
        raise RuntimeError("keyring unavailable")


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = _FakeKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(fake)
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", False)
    yield fake
    keyring.set_keyring(previous)


def _block_self_check_network(monkeypatch) -> None:
    class _BoomOpener:
        def open(self, *_args, **_kwargs):
            raise OSError("blocked in test")

    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *_a, **_kw: _BoomOpener()
    )


def _secret_backend_item(checks: list[dict]) -> dict:
    return next(item for item in checks if item["name"] == "安全密钥库")


def test_kdf_derivation_is_cached_per_salt(isolated_home, monkeypatch):
    monkeypatch.setattr(secretstore, "_KDF_ITERATIONS", 2)
    calls = {"n": 0}
    original = secretstore.hashlib.pbkdf2_hmac

    def counting(*args, **kwargs):
        calls["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(secretstore.hashlib, "pbkdf2_hmac", counting)
    secretstore._clear_runtime_caches()

    first = secretstore._get_master_key()
    second = secretstore._get_master_key()
    assert first == second
    assert calls["n"] == 1

    same = secretstore._derive_key_from_salt(secretstore._load_or_create_master_key())
    assert same == first
    assert calls["n"] == 1

    other_salt = os.urandom(32)
    other = secretstore._derive_key_from_salt(other_salt)
    assert other != first
    assert calls["n"] == 2
    assert secretstore._derive_key_from_salt(other_salt) == other
    assert calls["n"] == 2


def test_load_vault_cache_skips_decrypt_until_write(isolated_home, monkeypatch):
    secretstore.set_secret("cache:key", "v1")
    secretstore._clear_runtime_caches()
    reads = {"n": 0}
    original = secretstore._read_vault_file

    def wrapped(path, *, rewrite_legacy_format=False):
        reads["n"] += 1
        return original(path, rewrite_legacy_format=rewrite_legacy_format)

    monkeypatch.setattr(secretstore, "_read_vault_file", wrapped)
    assert secretstore.load_vault().get("cache:key") == "v1"
    assert reads["n"] == 1
    assert secretstore.load_vault().get("cache:key") == "v1"
    assert reads["n"] == 1

    poisoned = secretstore.load_vault()
    poisoned["cache:key"] = "mutated"
    assert secretstore.load_vault().get("cache:key") == "v1"

    secretstore.set_secret("cache:key", "v2")
    assert secretstore.get_secret("cache:key") == "v2"
    assert secretstore.load_vault().get("cache:key") == "v2"


def test_broken_keyring_fallback_logs_warning(isolated_home, monkeypatch, caplog):
    broken = _BrokenKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(broken)
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", False)
    try:
        with caplog.at_level(logging.WARNING, logger="pi_manager.secrets"):
            secretstore.set_secret("test:key", "vault-value")
            assert secretstore.get_secret("test:key") == "vault-value"
        text = caplog.text.lower()
        assert "keyring" in text or "回退" in caplog.text
        assert "vault-value" not in caplog.text
        assert secretstore.using_os_keyring() is False
        desc = secretstore.backend_description()
        assert "回退" in desc
        assert "OS keyring 不可用" in desc
    finally:
        keyring.set_keyring(previous)


def test_self_check_warns_without_os_keyring(isolated_home, monkeypatch):
    _block_self_check_network(monkeypatch)
    assert secretstore.using_os_keyring() is False
    item = _secret_backend_item(extras.run_self_check())
    assert item["ok"] is False
    assert item["level"] == "warn"
    detail = item["detail"]
    assert "回退" in detail or "vault" in detail.lower()
    assert "SECURITY.md" in detail


def test_self_check_ok_when_os_keyring_works(isolated_home, fake_keyring, monkeypatch):
    _block_self_check_network(monkeypatch)
    assert secretstore.using_os_keyring() is True
    item = _secret_backend_item(extras.run_self_check())
    assert item["ok"] is True
    assert "OS keyring" in item["detail"]
