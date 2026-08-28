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

import pytest

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


def test_print_provider_env_with_unknown_provider_never_leaks_keys(tmp_path):
    """未配置的 provider：stdout 必须是 JSON，且绝不含任何密钥材料。

    授权模型由 provider_env.py 决定（P1-5 正在收紧为 broker token），因此这里
    只锁定对扩展的稳定契约：stdout 是可解析 JSON、无密钥；未授权时给出可读
    原因，已授权时 env 为空 dict。
    """
    proc = _run_cli(tmp_path, "--print-provider-env", "no-such-provider")
    payload = json.loads(proc.stdout.strip())
    assert "API_KEY" not in json.dumps(payload)
    if payload.get("ok") is True:
        assert payload.get("env") == {}
    else:
        assert str(payload.get("error") or "").strip()


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


# ---- AGENTS.md 硬边界：轻量 CLI 入口不得导入 PySide6 --------------------


def _run_python(tmp_path: Path, code: str) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["HOME"] = str(tmp_path)
    env["USERPROFILE"] = str(tmp_path)
    env.setdefault("QT_QPA_PLATFORM", "offscreen")
    # 探针 stdout 含中文；Windows 上不指定编码会走 ANSI 代码页导致乱码。
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        cwd=str(Path(__file__).resolve().parents[1]),
    )


_NOGUI_PROBE = """
import sys
sys.argv = ["x"] + {argv!r}
import main
try:
    rc = main.main()
except SystemExit as exc:
    rc = exc.code
gui = sorted(m for m in sys.modules if m.split(".")[0] == "PySide6")
print("PROBE_GUI " + repr((rc, gui)))
"""


@pytest.mark.parametrize(
    "argv",
    [
        ["--vision-describe"],
        ["--vision-describe", "no-such-file.png"],
        ["--config-mutate"],
        ["--config-mutate", "no-such-request.json"],
        ["--print-provider-env"],
        ["--print-provider-env", "no-such-provider"],
        ["--provider-env", "no-such-provider"],
    ],
)
def test_light_cli_entrypoints_never_import_pyside6(tmp_path, argv):
    """Cursor 扩展热路径：这三个入口一律不得把 GUI 依赖拖进来。"""
    proc = _run_python(tmp_path, _NOGUI_PROBE.format(argv=argv))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE_GUI ")]
    assert line, proc.stdout + proc.stderr
    rc, gui = eval(line[-1][len("PROBE_GUI "):])  # noqa: S307 - 自产自销的字面量
    assert gui == [], f"{argv} 导入了 GUI 模块: {gui}"


_VISION_PROBE = """
import json, sys
from pi_manager import core_vision

captured = {{}}


def fake_request(model, api_key, body_obj, timeout, proxy):
    captured["body"] = body_obj
    return {{"ok": True, "description": "OK-DESC", "model": model}}


core_vision._zhipu_vision_request = fake_request
core_vision.zhipu_api_key = lambda: "sk-fake-not-a-real-key"

import main

sys.argv = ["x", "--vision-describe", {path!r}] + {prompt!r}
rc = main.main()
content = captured["body"]["messages"][0]["content"]
print("PROBE_VISION " + json.dumps({{
    "rc": rc,
    "text": content[0]["text"],
    "data_uri_prefix": content[1]["image_url"]["url"].split(",")[0],
    "gui": sorted(m for m in sys.modules if m.split(".")[0] == "PySide6"),
}}, ensure_ascii=False))
"""


def _run_vision_probe(tmp_path: Path, image: Path, prompt: list[str]) -> dict:
    proc = _run_python(
        tmp_path, _VISION_PROBE.format(path=str(image), prompt=prompt)
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    line = [ln for ln in proc.stdout.splitlines() if ln.startswith("PROBE_VISION ")]
    assert line, proc.stdout + proc.stderr
    return json.loads(line[-1][len("PROBE_VISION "):])


def test_vision_describe_cli_sends_a_real_prompt_not_null(tmp_path):
    """P1-3 回归：CLI 无提示词时必须发送 build_vision_prompt 的默认指令。"""
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    info = _run_vision_probe(tmp_path, image, [])
    assert info["rc"] == 0
    assert isinstance(info["text"], str) and info["text"].strip()
    assert "null" != info["text"]
    # 与 GUI 路径一致：build_vision_prompt 的「原样转录」约束必须在场
    assert "转录" in info["text"]
    assert info["gui"] == []


def test_vision_describe_cli_forwards_the_user_question(tmp_path):
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nfake")
    info = _run_vision_probe(tmp_path, image, ["这个", "报错", "什么意思"])
    assert info["rc"] == 0
    assert "这个 报错 什么意思" in info["text"]
    assert "转录" in info["text"]


@pytest.mark.parametrize(
    "name,expected",
    [
        ("shot.png", "data:image/png;base64"),
        ("shot.jpg", "data:image/jpeg;base64"),
        ("shot.webp", "data:image/webp;base64"),
    ],
)
def test_vision_describe_cli_uses_the_real_mime(tmp_path, name, expected):
    """P1-3 第二个缺陷：不得把 JPEG/WebP 一律标成 image/png。"""
    image = tmp_path / name
    image.write_bytes(b"fake-image-bytes")
    info = _run_vision_probe(tmp_path, image, [])
    assert info["data_uri_prefix"] == expected


def test_config_mutate_shreds_the_request_file(tmp_path):
    """P2-11：请求文件带着 broker token，调用后必须被覆盖擦除并删除。"""
    request = tmp_path / "req.json"
    request.write_text(
        json.dumps({"token": "a" * 64, "file": "settings", "key": "theme", "value": "dark"}),
        encoding="utf-8",
    )
    proc = _run_cli(tmp_path, "--config-mutate", str(request))
    assert proc.stdout.strip(), proc.stderr
    json.loads(proc.stdout.strip())  # 契约：stdout 必须是 JSON
    assert not request.exists(), "含 token 的请求文件被留在磁盘上"


def test_config_mutate_does_not_touch_a_non_regular_request_path(tmp_path):
    """擦除只针对普通文件：目录路径不得被当成文件去零覆盖/删除。"""
    target = tmp_path / "as-dir"
    target.mkdir()
    proc = _run_cli(tmp_path, "--config-mutate", str(target))
    payload = json.loads(proc.stdout.strip())
    assert payload.get("ok") is False
    assert target.is_dir()
