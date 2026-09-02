from __future__ import annotations

import zipfile
from types import SimpleNamespace

import pytest

from pi_manager import core, extras
from pi_manager import secrets as secretstore


def test_pi_launch_args_keep_provider_and_model_atomic():
    assert core.build_pi_launch_args(provider="ProviderB", model="model-b") == [
        "--provider",
        "ProviderB",
        "--model",
        "model-b",
    ]
    with pytest.raises(ValueError, match="成对指定"):
        core.build_pi_launch_args(provider="ProviderB", model=None)


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


def test_fail_counts_are_isolated_by_complete_model_pair(isolated_home):
    mgr = core.load_manager_config()
    mgr["failover_enabled"] = True
    mgr["failover_fail_threshold"] = 1
    mgr["failover_fail_counts"] = {}
    core.save_manager_config(mgr)

    assert extras.record_model_failure("ProviderA", "model-a") == 1
    assert extras.should_failover("ProviderA", "model-a")
    assert not extras.should_failover("ProviderA", "model-b")
    assert not extras.should_failover("ProviderB", "model-a")

    extras.record_model_success("ProviderA", "model-b")
    counts = extras._fail_counts()
    assert counts == {"ProviderA/model-a": 1}


def test_chat_with_failover_switches_complete_model_pair(isolated_home, monkeypatch):
    mgr = core.load_manager_config()
    mgr["favorites"] = ["Bad/model-a", "Good/model-b"]
    mgr["failover_enabled"] = True
    mgr["failover_fail_threshold"] = 3
    mgr["failover_fail_counts"] = {"Bad/model-a": 2}  # 再失败一次就切
    mgr["failover_silent"] = True
    core.save_manager_config(mgr)

    calls: list[tuple[str, str]] = []

    def fake_chat_once(prompt, *, provider=None, model=None, workdir=None, timeout=180, thinking="off", is_cancelled=None, **_kwargs):
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
    res = extras.chat_with_failover("hi", provider="Bad", model="model-a")
    assert res["ok"] is True
    assert res["provider"] == "Good"
    assert res["model"] == "model-b"
    assert res["switched"] is True
    assert calls == [("Bad", "model-a"), ("Good", "model-b")]
    # 默认 Provider 和 Model 必须一起切换，不能沿用旧模型名。
    assert core.get_default_model()[:2] == ("Good", "model-b")


def test_chat_with_failover_rejects_a_partial_model_pair(isolated_home, monkeypatch):
    core.set_default_model("ProviderA", "model-a")
    monkeypatch.setattr(
        extras,
        "chat_once",
        lambda *_args, **_kwargs: pytest.fail("不应使用默认模型拼接不完整的 Provider/Model"),
    )

    result = extras.chat_with_failover("hi", provider="ProviderB", model=None)

    assert result["ok"] is False
    assert "成对指定" in result["error"]
    assert result["attempts"] == []


def test_chat_with_failover_no_switch_before_threshold(isolated_home, monkeypatch):
    mgr = core.load_manager_config()
    mgr["favorites"] = ["Bad/m", "Good/m"]
    mgr["failover_enabled"] = True
    mgr["failover_fail_threshold"] = 3
    mgr["failover_fail_counts"] = {}
    core.save_manager_config(mgr)

    calls: list[tuple[str, str]] = []

    def fake_chat_once(prompt, *, provider=None, model=None, workdir=None, timeout=180, thinking="off", is_cancelled=None, **_kwargs):
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


def test_exhausted_key_pool_counts_failure_and_switches_model(
    isolated_home, monkeypatch
):
    mgr = core.load_manager_config()
    mgr["favorites"] = ["Bad/m", "Good/m"]
    mgr["failover_enabled"] = True
    mgr["failover_fail_threshold"] = 1
    mgr["failover_fail_counts"] = {}
    core.save_manager_config(mgr)
    calls: list[tuple[str, str]] = []

    def fake_run_pi_print(_prompt, *, provider=None, model=None, **_kwargs):
        calls.append((provider, model))
        if provider == "Bad":
            raise core.ProviderKeyError(
                "Provider「Bad」的 API Key 已全部暂时失效。"
            )
        return 0, "answer", ""

    monkeypatch.setattr(core, "run_pi_print", fake_run_pi_print)
    result = extras.chat_with_failover("hello", provider="Bad", model="m")

    assert result["ok"] is True
    assert result["provider"] == "Good"
    assert result["switched"] is True
    assert calls == [("Bad", "m"), ("Good", "m")]
    assert extras._fail_counts()["Bad/m"] == 1
    assert result["attempts"][0]["fail_count"] == 1


def test_unsigned_install_is_disabled(isolated_home, tmp_path):
    dummy = tmp_path / "fake.zip"
    with zipfile.ZipFile(dummy, "w") as zf:
        zf.writestr("PiManager/README.txt", "x")

    out = extras.apply_manager_update_inplace(dummy)
    assert out["ok"] is False
    assert out.get("need_exit") is False
    assert "签名更新链" in out["message"]


def test_two_failed_keys_count_as_one_model_failure_before_switch(
    isolated_home, monkeypatch
):
    for provider, key in (("Bad", "sk-bad-first"), ("Good", "sk-good")):
        core.upsert_custom_provider(
            provider,
            base_url="https://example.invalid/v1",
            api_key=key,
            models=[{"id": "m"}],
        )
    core.add_provider_api_key("Bad", "sk-bad-second")
    mgr = core.load_manager_config()
    mgr["favorites"] = ["Bad/m", "Good/m"]
    mgr["failover_enabled"] = True
    mgr["failover_fail_threshold"] = 1
    mgr["failover_fail_counts"] = {}
    core.save_manager_config(mgr)
    key_attempts: list[str] = []

    def fake_run_pi(_args, **kwargs):
        key = next(iter(kwargs["env"].values()))
        key_attempts.append(key)
        if key.startswith("sk-bad"):
            return SimpleNamespace(
                returncode=1,
                stdout="",
                stderr="HTTP 401 invalid API key",
            )
        return SimpleNamespace(returncode=0, stdout="answer", stderr="")

    monkeypatch.setattr(core, "run_pi", fake_run_pi)
    result = extras.chat_with_failover("hello", provider="Bad", model="m")

    assert result["ok"] is True
    assert result["provider"] == "Good"
    assert result["switched"] is True
    assert key_attempts == ["sk-bad-first", "sk-bad-second", "sk-good"]
    assert extras._fail_counts()["Bad/m"] == 1
    assert result["attempts"][0]["fail_count"] == 1
    assert [row["status"] for row in secretstore.list_provider_keys("Bad")] == [
        "invalid",
        "invalid",
    ]


def test_chat_with_failover_stops_immediately_when_cancelled(isolated_home, monkeypatch):
    mgr = core.load_manager_config()
    mgr["favorites"] = ["A/m1", "B/m2"]
    mgr["failover_enabled"] = True
    core.save_manager_config(mgr)

    calls: list[tuple[str, str]] = []

    def fake_attempt(*_args, **kwargs):
        calls.append((kwargs.get("provider"), kwargs.get("model")))
        return {"ok": True, "stdout": "nope", "error": ""}

    monkeypatch.setattr("pi_manager.extras_chat._chat_attempt", fake_attempt)
    result = extras.chat_with_failover(
        "hi", provider="A", model="m1", is_cancelled=lambda: True
    )
    assert result.get("cancelled") is True
    assert "停止" in str(result.get("error") or "")
    assert calls == []


def test_chat_once_cancelled_skips_run_pi_print(isolated_home, monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("cancelled chat_once must not launch pi")

    monkeypatch.setattr(core, "run_pi_print", boom)
    result = extras.chat_once("hi", provider="A", model="m", is_cancelled=lambda: True)
    assert result.get("cancelled") is True
    assert result["ok"] is False
    assert "停止" in str(result.get("error") or "")
