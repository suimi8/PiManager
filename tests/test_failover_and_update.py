from __future__ import annotations

from pi_manager import core, extras


def test_failover_chain_order(isolated_home):
    mgr = core.load_manager_config()
    mgr["favorites"] = ["A/m1", "B/m2", "C/m3"]
    core.save_manager_config(mgr)
    core.set_default_model("B", "m2")
    chain = extras.failover_chain("A", "m1")
    assert chain[0] == ("A", "m1")
    assert ("B", "m2") in chain
    assert ("C", "m3") in chain


def test_fail_count_and_threshold(isolated_home):
    mgr = core.load_manager_config()
    mgr["failover_enabled"] = True
    mgr["failover_fail_threshold"] = 3
    mgr["failover_fail_counts"] = {}
    core.save_manager_config(mgr)

    assert extras.record_model_failure("P", "x") == 1
    assert extras.record_model_failure("P", "x") == 2
    assert not extras.should_failover("P", "x")
    assert extras.record_model_failure("P", "x") == 3
    assert extras.should_failover("P", "x")
    extras.record_model_success("P", "x")
    assert not extras.should_failover("P", "x")


def test_chat_with_failover_switches_after_threshold(isolated_home, monkeypatch):
    mgr = core.load_manager_config()
    mgr["favorites"] = ["Bad/m", "Good/m"]
    mgr["failover_enabled"] = True
    mgr["failover_fail_threshold"] = 3
    mgr["failover_fail_counts"] = {"Bad/m": 2}  # 再失败一次就切
    mgr["failover_silent"] = True
    core.save_manager_config(mgr)

    calls: list[tuple[str, str]] = []

    def fake_chat_once(prompt, *, provider=None, model=None, workdir=None, timeout=180, thinking="off"):
        calls.append((provider, model))
        if provider == "Bad":
            return {
                "ok": False,
                "returncode": 1,
                "stdout": "",
                "stderr": "rate limit",
                "latency_ms": 1,
                "provider": provider,
                "model": model,
                "error": "rate limit",
            }
        return {
            "ok": True,
            "returncode": 0,
            "stdout": "hello from good",
            "stderr": "",
            "latency_ms": 2,
            "provider": provider,
            "model": model,
            "error": "",
        }

    monkeypatch.setattr(extras, "chat_once", fake_chat_once)
    res = extras.chat_with_failover("hi", provider="Bad", model="m")
    assert res["ok"] is True
    assert res["provider"] == "Good"
    assert res["model"] == "m"
    assert res["switched"] is True
    assert ("Bad", "m") in calls and ("Good", "m") in calls
    # 默认应已切到 Good
    assert core.get_default_model()[:2] == ("Good", "m")


def test_chat_with_failover_no_switch_before_threshold(isolated_home, monkeypatch):
    mgr = core.load_manager_config()
    mgr["favorites"] = ["Bad/m", "Good/m"]
    mgr["failover_enabled"] = True
    mgr["failover_fail_threshold"] = 3
    mgr["failover_fail_counts"] = {}
    core.save_manager_config(mgr)

    calls: list[tuple[str, str]] = []

    def fake_chat_once(prompt, *, provider=None, model=None, workdir=None, timeout=180, thinking="off"):
        calls.append((provider, model))
        return {
            "ok": False,
            "returncode": 1,
            "stdout": "",
            "stderr": "err",
            "latency_ms": 1,
            "provider": provider,
            "model": model,
            "error": "err",
        }

    monkeypatch.setattr(extras, "chat_once", fake_chat_once)
    res = extras.chat_with_failover("hi", provider="Bad", model="m")
    assert res["ok"] is False
    assert calls == [("Bad", "m")]  # 未达阈值，不立刻换模
    assert extras._fail_counts().get("Bad/m") == 1


def test_apply_inplace_refuses_source_mode(isolated_home, tmp_path):
    # 源码运行不应覆盖
    dummy = tmp_path / "fake.zip"
    # minimal zip
    import zipfile

    with zipfile.ZipFile(dummy, "w") as zf:
        zf.writestr("PiManager/README.txt", "x")
    out = extras.apply_manager_update_inplace(dummy)
    assert out["ok"] is False
    assert out.get("need_exit") is False
