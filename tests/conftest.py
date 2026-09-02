from __future__ import annotations

import time

import pytest

from pi_manager import secrets as secretstore


def pytest_configure(config):
    """注册 integration marker：需要真实 Pi CLI 或外网服务的集成测试。

    默认不跑：CI 只需在安装了官方 Pi CLI（或允许本地 HTTP 服务）的
    runner 上执行 `pytest -m integration`（或 `-m ''` 跑非集成用例）
    时才会真正执行这些用例；否则用例内会以可见原因 skip。
    """
    config.addinivalue_line(
        "markers",
        "integration: 需要真实 Pi CLI / 外网或长时间真实子进程的集成测试（默认 skip，CI 按需启用）",
    )


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setattr(secretstore, "_KEYRING", None)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED", True)
    monkeypatch.setattr(secretstore, "_KEYRING_TRIED_AT", time.monotonic())
    # 默认 60s 冷却后 _get_keyring 会再次探测真实 OS keyring。
    # 自检 / UI 用例若超过这个窗口，会把「无 keyring」断言打成偶发失败。
    monkeypatch.setattr(secretstore, "_KEYRING_RETRY_COOLDOWN", 10**9)
    secretstore._clear_runtime_caches()
    return tmp_path
