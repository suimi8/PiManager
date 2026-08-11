# -*- coding: utf-8 -*-
"""Keyring-priority behavior for pi_manager.secrets (no vault involvement when
keyring works; clean vault fallback when keyring is broken)."""
from __future__ import annotations

import threading

import keyring
import pytest
from keyring.backend import KeyringBackend

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


class _PlaintextFileKeyring(_FakeKeyring):
    """Stand-in for the keyrings.alt plaintext file fallback backend."""


class _ReturnsEmptyKeyring(_FakeKeyring):
    def get_password(self, service: str, username: str) -> str | None:
        return ""


class _HangingKeyring(_FakeKeyring):
    def __init__(self) -> None:
        super().__init__()
        self.gate = threading.Event()

    def get_password(self, service: str, username: str) -> str | None:
        self.gate.wait()
        return None

    def set_password(self, service: str, username: str, password: str) -> None:
        self.gate.wait()

    def delete_password(self, service: str, username: str) -> None:
        self.gate.wait()


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = _FakeKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(fake)
    # Re-enable the probe the isolated_home fixture disabled on purpose.
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", False)
    yield fake
    keyring.set_keyring(previous)


def test_keyring_read_wins_over_vault(isolated_home, fake_keyring):
    secretstore.set_secret("test:key", "keyring-value")
    secretstore.save_vault({"test:key": "vault-value"})
    assert secretstore.get_secret("test:key") == "keyring-value"
    assert fake_keyring.store[(secretstore.SERVICE, "test:key")] == "keyring-value"
    assert secretstore.load_vault().get("test:key") == "vault-value"


def test_keyring_write_skips_vault(isolated_home, fake_keyring):
    secretstore.set_secret("test:key", "keyring-value")
    assert fake_keyring.store[(secretstore.SERVICE, "test:key")] == "keyring-value"
    assert secretstore.load_vault().get("test:key") is None


def test_keyring_delete_removes_from_keyring(isolated_home, fake_keyring):
    secretstore.set_secret("test:key", "keyring-value")
    secretstore.delete_secret("test:key")
    assert (secretstore.SERVICE, "test:key") not in fake_keyring.store
    assert secretstore.get_secret("test:key") == ""


def test_broken_keyring_falls_back_to_vault(isolated_home, monkeypatch):
    broken = _BrokenKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(broken)
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", False)
    try:
        secretstore.set_secret("test:key", "fallback-value")
        assert secretstore.get_secret("test:key") == "fallback-value"
        assert secretstore.load_vault().get("test:key") == "fallback-value"
    finally:
        keyring.set_keyring(previous)


def test_plaintext_file_backend_is_rejected_and_vault_is_used(
    isolated_home, monkeypatch
):
    fake = _PlaintextFileKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(fake)
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", False)
    try:
        assert secretstore._get_keyring() is None
        secretstore.set_secret("test:key", "vault-value")
        assert secretstore.get_secret("test:key") == "vault-value"
        assert secretstore.load_vault().get("test:key") == "vault-value"
        assert fake.store == {}
    finally:
        keyring.set_keyring(previous)


def test_keyring_that_returns_empty_is_not_trusted(isolated_home, monkeypatch):
    fake = _ReturnsEmptyKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(fake)
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", False)
    try:
        secretstore.set_secret("test:key", "fallback-value")
        assert secretstore.get_secret("test:key") == "fallback-value"
        assert secretstore.load_vault().get("test:key") == "fallback-value"
    finally:
        keyring.set_keyring(previous)


def test_hanging_keyring_probe_times_out_and_uses_vault(isolated_home, monkeypatch):
    fake = _HangingKeyring()
    previous = keyring.get_keyring()
    keyring.set_keyring(fake)
    monkeypatch.setattr(secretstore, "_KEYRING_PROBE_TIMEOUT", 0.2)
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", False)
    try:
        secretstore.set_secret("test:key", "timed-out-value")
        assert secretstore.get_secret("test:key") == "timed-out-value"
        assert secretstore.load_vault().get("test:key") == "timed-out-value"
    finally:
        keyring.set_keyring(previous)
