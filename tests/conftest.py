from __future__ import annotations

import pytest

from pi_manager import secrets as secretstore


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", True)
    return tmp_path
