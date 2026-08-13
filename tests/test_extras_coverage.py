# -*- coding: utf-8 -*-
"""补充覆盖 pi_manager/extras.py 的低覆盖区域（failover / 自检 / 会话 / 代理）。

所有用例均通过 ``isolated_home`` 隔离 HOME，不依赖真实 Pi CLI 或外网。
"""
from __future__ import annotations

import urllib.request

import pytest

from pi_manager import core, extras


# ---------------------------------------------------------------------------
# failover_chain
# ---------------------------------------------------------------------------

def test_failover_chain_builds(isolated_home):
    """failover_chain 返回 当前模型 → 收藏 → 默认 的去重候选链。"""
    mgr = core.load_manager_config()
    mgr["favorites"] = ["FavP/favM", "StartP/startM"]  # startM 与起点重复，验证去重
    core.save_manager_config(mgr)
    core.set_default_model("DefP", "defM")

    chain = extras.failover_chain("StartP", "startM")

    assert chain[0] == ("StartP", "startM")
    assert ("FavP", "favM") in chain
    assert ("DefP", "defM") in chain
    # 去重：startM 不应重复出现
    assert chain.count(("StartP", "startM")) == 1


def test_failover_chain_empty_when_nothing_configured(isolated_home):
    """无起点、无收藏、无默认时返回空链。"""
    assert extras.failover_chain() == []


def test_failover_chain_dedups_favorites_and_default(isolated_home):
    """收藏与默认指向同一模型时只出现一次。"""
    mgr = core.load_manager_config()
    mgr["favorites"] = ["P/m"]
    core.save_manager_config(mgr)
    core.set_default_model("P", "m")

    chain = extras.failover_chain("P", "m")
    assert chain == [("P", "m")]


# ---------------------------------------------------------------------------
# should_failover / record_model_failure / record_model_success
# ---------------------------------------------------------------------------

def test_should_failover_threshold(isolated_home):
    """should_failover 在失败计数达到阈值时返回 True，未达时返回 False。"""
    mgr = core.load_manager_config()
    mgr["failover_enabled"] = True
    mgr["failover_fail_threshold"] = 3
    mgr["failover_fail_counts"] = {}
    core.save_manager_config(mgr)

    assert extras.record_model_failure("P", "x") == 1
    assert extras.record_model_failure("P", "x") == 2
    assert extras.should_failover("P", "x") is False  # 2 < 3
    assert extras.record_model_failure("P", "x") == 3
    assert extras.should_failover("P", "x") is True  # 3 >= 3


def test_should_failover_disabled_flag(isolated_home):
    """failover_enabled=False 时 should_failover 永远返回 False。"""
    mgr = core.load_manager_config()
    mgr["failover_enabled"] = False
    mgr["failover_fail_threshold"] = 1
    mgr["failover_fail_counts"] = {"P/x": 5}
    core.save_manager_config(mgr)

    assert extras.should_failover("P", "x") is False


def test_record_model_failure_then_success(isolated_home):
    """失败计数累加，成功后清零。"""
    mgr = core.load_manager_config()
    mgr["failover_enabled"] = True
    mgr["failover_fail_threshold"] = 2
    mgr["failover_fail_counts"] = {}
    core.save_manager_config(mgr)

    assert extras.record_model_failure("P", "x") == 1
    assert extras.record_model_failure("P", "x") == 2
    extras.record_model_success("P", "x")
    assert extras._fail_counts().get("P/x") == 0


def test_record_model_success_no_op_when_absent(isolated_home):
    """成功但无失败计数时不写入。"""
    extras.record_model_success("Never", "failed")
    assert extras._fail_counts() == {}


# ---------------------------------------------------------------------------
# run_self_check
# ---------------------------------------------------------------------------

def test_run_self_check_returns_list(isolated_home, monkeypatch):
    """run_self_check 返回 list，每项含 name/ok/detail/level，且不发外网。"""

    # 屏蔽网络探测：让所有 probe URL 立即失败，走 else 分支
    class _BoomOpener:
        def open(self, *_args, **_kwargs):
            raise OSError("blocked in test")

    monkeypatch.setattr(
        urllib.request, "build_opener", lambda *_a, **_kw: _BoomOpener()
    )

    checks = extras.run_self_check()

    assert isinstance(checks, list)
    assert len(checks) > 0
    for item in checks:
        assert {"name", "ok", "detail", "level"} <= set(item)
        assert isinstance(item["name"], str)
        assert isinstance(item["ok"], bool)
        assert isinstance(item["detail"], str)
        assert item["level"] in ("info", "warn", "error")
    # 至少应包含这些检查项
    names = {c["name"] for c in checks}
    assert "Pi Manager 版本" in names
    assert "配置目录" in names


# ---------------------------------------------------------------------------
# list_sessions_filtered
# ---------------------------------------------------------------------------

def test_list_sessions_filtered_empty(isolated_home):
    """空会话目录返回空列表。"""
    assert extras.list_sessions_filtered() == []


def test_list_sessions_filtered_matches_substring(isolated_home):
    """按 workdir/name 子串过滤会话。"""
    root = core.sessions_dir()
    root.mkdir(parents=True, exist_ok=True)
    # 写一个 .jsonl 会话文件（被 list_sessions 识别为 preferred）
    (root / "proj-A.jsonl").write_text("{}", encoding="utf-8")

    rows = extras.list_sessions_filtered(name_substr="proj-A")
    assert len(rows) == 1
    assert rows[0]["name"] == "proj-A.jsonl"

    # 不匹配的子串返回空
    assert extras.list_sessions_filtered(name_substr="nope") == []


def test_list_sessions_filtered_respects_limit(isolated_home):
    """limit 截断结果。"""
    root = core.sessions_dir()
    root.mkdir(parents=True, exist_ok=True)
    for i in range(5):
        (root / f"s{i}.jsonl").write_text("{}", encoding="utf-8")

    rows = extras.list_sessions_filtered(limit=2)
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# effective_proxy 优先级
# ---------------------------------------------------------------------------

def test_effective_proxy_priority_explicit_wins(isolated_home, monkeypatch):
    """explicit 参数优先于 config 与 env。"""
    # config 里启用代理
    mgr = core.load_manager_config()
    mgr["proxy_enabled"] = True
    mgr["proxy_url"] = "http://from-config:8080"
    core.save_manager_config(mgr)
    # env 也设一个
    monkeypatch.setenv("HTTPS_PROXY", "http://from-env:8090")

    assert extras.effective_proxy("http://explicit:1234") == "http://explicit:1234"


def test_effective_proxy_falls_back_to_config(isolated_home, monkeypatch):
    """无 explicit 时用 config 的 effective（enabled + url）。"""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)

    mgr = core.load_manager_config()
    mgr["proxy_enabled"] = True
    mgr["proxy_url"] = "http://from-config:8080"
    core.save_manager_config(mgr)

    assert extras.effective_proxy() == "http://from-config:8080"


def test_effective_proxy_falls_back_to_env(isolated_home, monkeypatch):
    """无 explicit、config 未启用时回退到环境变量。"""
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.setenv("HTTPS_PROXY", "http://from-env:9090")

    # config 默认未启用代理
    assert extras.effective_proxy() == "http://from-env:9090"


def test_effective_proxy_empty_when_nothing(isolated_home, monkeypatch):
    """无 explicit、无 config、无 env 时返回空串。"""
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(var, raising=False)

    assert extras.effective_proxy() == ""
