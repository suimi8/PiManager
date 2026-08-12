# -*- coding: utf-8 -*-
"""main.py CLI 分发黑盒测试（subprocess 级）。

覆盖发布不变量 --self-check 以及 Cursor 扩展热路径
--vision-describe / --config-mutate / --print-provider-env 的参数行为。
main.py 可能被并行修改（参数语义不变），本测试只断言参数行为，
不依赖任何内部实现。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parents[1] / "main.py"


def _run_cli(tmp_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # 隔离用户目录，避免触碰 ~/.pi/agent
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    return subprocess.run(
        [sys.executable, str(MAIN_PY), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


def test_self_check_reports_ok_and_exits_zero(tmp_path):
    """--self-check 是 AGENTS.md 发布不变量：必须输出 self-check: OK 且退出码 0。"""
    proc = _run_cli(tmp_path, "--self-check")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "self-check: OK" in proc.stdout
    assert "FAILED" not in proc.stderr
    # 版本信息随自检输出
    assert "version=" in proc.stdout


def test_self_check_alias_smoke_test_works(tmp_path):
    proc = _run_cli(tmp_path, "--smoke-test")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "self-check: OK" in proc.stdout


def test_vision_describe_without_args_returns_error_json_and_exit_2(tmp_path):
    proc = _run_cli(tmp_path, "--vision-describe")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload.get("ok") is False
    assert "usage" in str(payload.get("error", "")).lower()


def test_vision_describe_with_non_image_path_returns_error_json_and_exit_2(tmp_path):
    bogus = tmp_path / "notes.txt"
    bogus.write_text("not an image", encoding="utf-8")
    proc = _run_cli(tmp_path, "--vision-describe", str(bogus))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload.get("ok") is False


def test_config_mutate_without_args_returns_error_json_and_exit_2(tmp_path):
    proc = _run_cli(tmp_path, "--config-mutate")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload.get("ok") is False
    assert "request" in str(payload.get("error", "")).lower()


def test_config_mutate_with_missing_request_file_returns_error_json(tmp_path):
    proc = _run_cli(tmp_path, "--config-mutate", str(tmp_path / "no-such-file.json"))
    assert proc.returncode == 2, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload.get("ok") is False
    assert payload.get("error") == "invalid_request_file"


def test_print_provider_env_without_args_prints_usage_and_exit_2(tmp_path):
    """provider 是必选参数：无参数时 argparse 输出 usage 并退出码 2（不触发 GUI）。"""
    proc = _run_cli(tmp_path, "--print-provider-env")
    assert proc.returncode == 2, proc.stdout + proc.stderr
    assert "usage" in (proc.stderr + proc.stdout).lower()


def test_print_provider_env_with_unknown_provider_returns_empty_env_json(tmp_path):
    """未配置的 provider 返回 ok:true + 空 env（扩展可继续启动 pi，不崩溃、不泄漏密钥）。"""
    proc = _run_cli(tmp_path, "--print-provider-env", "no-such-provider")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    payload = json.loads(proc.stdout.strip())
    assert payload.get("ok") is True
    assert payload.get("env") == {}
    assert "API_KEY" not in json.dumps(payload)


def test_cli_never_leaks_secret_material_to_stdout(tmp_path):
    """黑盒校验：所有 CLI 入口的 stdout 不得出现密钥字样。"""
    for args in (
        ("--self-check",),
        ("--vision-describe",),
        ("--config-mutate",),
        ("--print-provider-env",),
    ):
        proc = _run_cli(tmp_path, *args)
        lowered = (proc.stdout + proc.stderr).lower()
        for secret_marker in ("sk-", "api_key=", "authorization: bearer", "password"):
            assert secret_marker not in lowered, f"{args} 泄漏了 {secret_marker!r}"
