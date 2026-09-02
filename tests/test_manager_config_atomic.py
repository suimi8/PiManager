"""pi-manager.json 原子合并写入：禁止 load → 改 → 整份 save 丢失更新。"""
from __future__ import annotations

from typing import Any

from pi_manager import core, extras

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _isolate_proxy_env(monkeypatch) -> None:
    """避免 set_proxy_settings → apply_proxy_env 把代理泄漏到后续用例。"""
    for name in _PROXY_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


def _seed_manager(**fields: Any) -> dict[str, Any]:
    def _apply(cfg: dict[str, Any]) -> dict[str, Any]:
        cfg.update(fields)
        return cfg

    return core.update_manager_config(_apply)


def test_set_proxy_settings_preserves_failover_counts(isolated_home, monkeypatch):
    """改代理不得覆盖 failover 计数与更新检查时间。"""
    _isolate_proxy_env(monkeypatch)
    _seed_manager(
        failover_fail_counts={"A/m": 2},
        last_manager_update_check="stale",
    )
    extras.set_proxy_settings(True, "http://proxy.example:8080")
    mgr = core.load_manager_config()
    assert mgr.get("failover_fail_counts") == {"A/m": 2}
    assert mgr.get("last_manager_update_check") == "stale"
    assert mgr.get("proxy_enabled") is True
    assert mgr.get("proxy_url") == "http://proxy.example:8080"


def test_set_test_concurrency_preserves_failover_counts(isolated_home):
    """改测试并发不得丢掉 failover_fail_counts。"""
    _seed_manager(failover_fail_counts={"A/m": 2})
    extras.set_test_concurrency(4)
    mgr = core.load_manager_config()
    assert mgr.get("failover_fail_counts") == {"A/m": 2}
    assert mgr.get("test_concurrency") == 4


def test_fail_count_survives_interleaved_unrelated_update(isolated_home):
    """失败计数必须在 updater 内读-改，交错写入无关键不得抹掉其它模型。"""
    extras.record_model_failure("P", "x")
    _seed_manager(last_manager_update_check="after-p")
    extras.record_model_failure("Q", "y")
    mgr = core.load_manager_config()
    counts = mgr.get("failover_fail_counts") or {}
    assert counts.get("P/x") == 1
    assert counts.get("Q/y") == 1
    assert mgr.get("last_manager_update_check") == "after-p"


def test_check_manager_update_preserves_failover_counts(isolated_home, monkeypatch):
    """检查更新只改时间戳/状态键，不得覆盖已有 failover 计数。"""
    _seed_manager(
        failover_fail_counts={"A/m": 2},
        update_manifest_url="https://updates.example.com/manifest.json",
    )
    monkeypatch.setattr(
        extras,
        "_http_get_json",
        lambda url, **kw: {"version": "1.0.0"},
    )
    extras.check_manager_update()
    mgr = core.load_manager_config()
    assert mgr.get("failover_fail_counts") == {"A/m": 2}
    assert mgr.get("last_manager_update_check")
    status = mgr.get("manager_update_status") or {}
    assert status.get("state") == "ok"
    assert status.get("message")
