# -*- coding: utf-8 -*-
"""更新可见性：检查结果落盘 / 状态分类 / 忽略版本测试。

覆盖新增的“显眼更新提示”数据层：check_pi_status 快照落盘、
pi_update_state 分类、dismiss 记录，以及 Manager 更新结果持久化。
"""
from __future__ import annotations

from pi_manager import core, extras


def _fake_status(**overrides):
    base = {
        "ok": False,
        "installed": None,
        "latest": None,
        "channel": "latest",
        "message": "",
        "check_failed": False,
        "blocked": False,
        "missing": False,
        "runtime_broken": False,
        "repair_required": False,
        "outdated": False,
    }
    base.update(overrides)
    return base


def test_pi_update_state_classification():
    assert core.pi_update_state(_fake_status(check_failed=True)) == "check_failed"
    assert core.pi_update_state(_fake_status(blocked=True)) == "blocked"
    assert core.pi_update_state(_fake_status(missing=True)) == "missing"
    assert core.pi_update_state(_fake_status(runtime_broken=True)) == "repair_required"
    assert core.pi_update_state(_fake_status(repair_required=True)) == "repair_required"
    assert core.pi_update_state(_fake_status(outdated=True)) == "outdated"
    assert core.pi_update_state(_fake_status(ok=True)) == "ok"
    assert core.pi_update_state(_fake_status()) == "unknown"


def test_check_pi_status_persists_snapshot(isolated_home, monkeypatch):
    monkeypatch.setattr(
        core,
        "needs_pi_install_or_update",
        lambda: _fake_status(ok=True, installed="1.2.3", latest="1.2.3", message="ready"),
    )
    result = core.check_pi_status()

    snap = core.load_pi_update_status()
    assert snap["state"] == "ok"
    assert snap["installed"] == "1.2.3"
    assert snap["latest"] == "1.2.3"
    assert snap["channel"] == "latest"
    assert snap["message"] == "ready"
    assert snap["checked_at"]
    assert core.load_manager_config()["last_update_check"] == snap["checked_at"]
    assert result["ok"] is True


def test_check_pi_status_persists_outdated_state(isolated_home, monkeypatch):
    monkeypatch.setattr(
        core,
        "needs_pi_install_or_update",
        lambda: _fake_status(
            outdated=True,
            installed="1.2.3",
            latest="1.4.0",
            message="Pi CLI 有新版本",
        ),
    )
    core.check_pi_status()

    snap = core.load_pi_update_status()
    assert snap["state"] == "outdated"
    assert snap["installed"] == "1.2.3"
    assert snap["latest"] == "1.4.0"
    assert "有新版本" in snap["message"]


def test_check_pi_status_persists_check_failed(isolated_home, monkeypatch):
    monkeypatch.setattr(
        core,
        "needs_pi_install_or_update",
        lambda: _fake_status(check_failed=True, installed="1.2.3", latest=None),
    )
    core.check_pi_status()
    assert core.load_pi_update_status()["state"] == "check_failed"


def test_dismiss_update_roundtrip(isolated_home):
    assert core.is_update_dismissed("pi", "1.4.0") is False
    core.dismiss_update("pi", "1.4.0")
    assert core.is_update_dismissed("pi", "1.4.0") is True
    assert core.is_update_dismissed("pi", "1.5.0") is False
    assert core.is_update_dismissed("mgr", "1.4.0") is False


def test_dismiss_update_ignores_empty_and_is_idempotent(isolated_home):
    core.dismiss_update("pi", "")
    core.dismiss_update("pi", "  ")
    assert core.is_update_dismissed("pi", "") is False
    core.dismiss_update("pi", "1.4.0")
    core.dismiss_update("pi", "1.4.0")
    entries = core.load_manager_config().get("dismissed_updates") or []
    assert entries.count("pi@1.4.0") == 1


def test_manager_update_result_is_persisted(isolated_home, monkeypatch):
    mgr = core.load_manager_config()
    mgr["update_manifest_url"] = "https://updates.example.com/manifest.json"
    core.save_manager_config(mgr)
    monkeypatch.setattr(
        extras,
        "_http_get_json",
        lambda url, **kw: {"version": "9.9.9", "notes": "新版本", "url": "https://example.com/dl"},
    )

    result = extras.check_manager_update()

    snap = core.load_manager_config()["manager_update_status"]
    assert snap["state"] == "ok"
    assert snap["has_update"] is True
    assert snap["remote"] == "9.9.9"
    assert snap["notes"] == "新版本"
    assert "发现新版本" in snap["message"]
    assert result["has_update"] is True


def test_manager_update_failure_is_persisted(isolated_home, monkeypatch):
    monkeypatch.setattr(
        extras,
        "_http_get_json",
        lambda url, **kw: (_ for _ in ()).throw(ValueError("manifest broken")),
    )
    extras.check_manager_update()

    snap = core.load_manager_config()["manager_update_status"]
    assert snap["state"] == "failed"
    assert snap["has_update"] is False
    assert "检查失败" in snap["message"]
