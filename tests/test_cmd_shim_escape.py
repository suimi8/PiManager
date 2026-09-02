# -*- coding: utf-8 -*-
"""P0-1 回归：cmd.exe 批处理 shim 参数注入（任意命令执行）。

审计实测的注入向量：provider 名为 ``x" & <payload> & "y`` 时，
``subprocess.list2cmdline`` 把 ``"`` 写成 ``\\"``，而 cmd.exe **不认这个转义**
→ 引号提前闭合 → ``&`` 成为命令分隔符 → payload 以当前用户权限执行。

本文件用**真实 cmd.exe + 真实 npm 风格 shim** 跑完整链路：
1. 旧实现（只把 ``%`` → ``%%``）必须表现出不安全；
2. 现实现必须做到「注入不成立」且「合法参数仍能原样送达子进程」。

不写任何配置文件，因此不需要 isolated_home；仅在 tmp_path 内活动。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pi_manager import core_process

WINDOWS_ONLY = pytest.mark.skipif(
    sys.platform != "win32", reason="cmd.exe 批处理 shim 是 Windows 专属路径"
)


# ---- 真实 shim 夹具 ------------------------------------------------------


def _make_shim(tmp_path: Path) -> tuple[list[str], Path, Path]:
    """建一个转发 ``%*`` 的 npm 风格 pi.cmd，并返回 (base, argv 文件, 标记文件)。

    shim 把收到的 argv 落盘成 JSON —— 这样既能断言「注入未执行」，也能断言
    「合法参数原样送达」。
    """
    dump = tmp_path / "dump_args.py"
    dump.write_text(
        textwrap.dedent(
            """
            import json, os, sys
            with open(os.environ["PROBE_ARGV"], "w", encoding="utf-8") as fh:
                json.dump(sys.argv[1:], fh, ensure_ascii=False)
            """
        ).strip(),
        encoding="utf-8",
    )
    shim = tmp_path / "pi.cmd"
    # 与 npm 生成的 shim 同构：末行用 %* 把命令行尾部整体转发。
    shim.write_text(
        '@ECHO off\r\n"%PROBE_PY%" "%~dp0dump_args.py" %*\r\n',
        encoding="utf-8",
    )
    return ["cmd.exe", "/c", str(shim)], tmp_path / "argv.json", tmp_path / "PWNED.txt"


def _run_shim(base: list[str], escaped: list[str], argv_file: Path) -> dict:
    env = os.environ.copy()
    env["PROBE_PY"] = sys.executable
    env["PROBE_ARGV"] = str(argv_file)
    env["PI_INJECT_PROBE"] = "INJECTED_ENV_VALUE"
    proc = subprocess.run(
        base + escaped,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=60,
    )
    child = None
    if argv_file.exists():
        child = json.loads(argv_file.read_text(encoding="utf-8"))
    return {"rc": proc.returncode, "child": child, "stderr": proc.stderr or ""}


def _legacy_escape(args: list[str]) -> list[str]:
    """修复前的实现（core_process.py:53-67 原样）：只把 % 换成 %%。"""
    return [arg.replace("%", "%%") for arg in args]


# ---- 修复前 / 修复后对比 -------------------------------------------------


@WINDOWS_ONLY
def test_legacy_percent_only_escaping_is_unsafe(tmp_path):
    """基线：旧实现放行引号 → 命令行被破坏或 payload 真实执行。"""
    base, argv_file, marker = _make_shim(tmp_path)
    payload = f'x" & echo pwned>{marker} & "y'
    escaped = _legacy_escape([payload])
    # 旧实现把 " 原样放行，list2cmdline 于是产出 cmd.exe 不认的 \"
    assert '"' in escaped[0]
    assert '\\"' in subprocess.list2cmdline(base + escaped)
    result = _run_shim(base, escaped, argv_file)
    assert marker.exists() or result["rc"] != 0, (
        "旧实现应表现出不安全：payload 执行或命令行被引号破坏，"
        f"实际 rc={result['rc']} child={result['child']}"
    )
    if marker.exists():
        marker.unlink()


@WINDOWS_ONLY
@pytest.mark.parametrize(
    "payload_tpl",
    [
        'x" & echo pwned>{mark} & "y',   # 审计原始 PoC：引号提前闭合
        'x" | echo pwned>{mark} | "y',   # 管道变体
        'a" && echo pwned>{mark} && "b',  # 条件串联
        'a"&echo pwned>{mark}&"b',       # 无空格紧凑写法
        'a"^&echo pwned>{mark}^&"b',     # ^ 转义变体
        'a"&&echo pwned>{mark}&&"b%PI_INJECT_PROBE%',  # 混入 % 展开
    ],
)
def test_injection_never_executes_after_fix(tmp_path, payload_tpl):
    """修复后：同一批注入向量必须「要么被拒，要么原样当数据传递」。"""
    base, argv_file, marker = _make_shim(tmp_path)
    payload = payload_tpl.format(mark=marker)
    try:
        escaped = core_process.escape_cmd_shim_args([payload], base)
    except ValueError:
        # 无法安全表达 → 拒绝执行，也是合格结果（绝不放行成可注入命令行）。
        assert not marker.exists()
        return
    # 转义结果里绝不能残留裸引号（list2cmdline 会把它变成 cmd.exe 不认的 \"）
    assert '"' not in escaped[0]
    result = _run_shim(base, escaped, argv_file)
    assert not marker.exists(), f"注入仍然成立！child={result['child']}"
    assert result["child"] is not None, f"shim 未收到参数：rc={result['rc']}"
    assert len(result["child"]) == 1, f"参数被拆成多段：{result['child']}"


@WINDOWS_ONLY
@pytest.mark.parametrize(
    "payload_tpl",
    ["a>{mark}", "a&echo pwned>{mark}", "a|echo pwned>{mark}", "a^&echo pwned>{mark}"],
)
def test_bare_metachar_args_never_inject(tmp_path, payload_tpl):
    """无引号的元字符参数（``a>file`` 也能写文件）同样不得注入。"""
    base, argv_file, marker = _make_shim(tmp_path)
    payload = payload_tpl.format(mark=marker)
    try:
        escaped = core_process.escape_cmd_shim_args([payload], base)
    except ValueError:
        assert not marker.exists()
        return
    _run_shim(base, escaped, argv_file)
    assert not marker.exists(), "裸元字符参数导致注入"


@WINDOWS_ONLY
def test_legitimate_args_still_reach_the_child_verbatim(tmp_path):
    """合法启动参数必须原样送达（含 OpenRouter / Vertex 风格模型 ID）。"""
    base, argv_file, _marker = _make_shim(tmp_path)
    args = [
        "--provider",
        "openrouter",
        "--model",
        "deepseek/deepseek-chat:free",
        "--thinking",
        "medium",
    ]
    escaped = core_process.escape_cmd_shim_args(args, base)
    assert escaped == args
    result = _run_shim(base, escaped, argv_file)
    assert result["rc"] == 0, result["stderr"]
    assert result["child"] == args


@WINDOWS_ONLY
def test_vision_rule_prompt_survives_the_shim(tmp_path):
    """识图规则 system prompt（含引号 + 空格 + 中文）必须能通过 shim。

    修复前这条参数会让整条命令行崩掉（实测 rc=1「系统找不到指定的文件」），
    也就是说 .cmd shim 安装上「启动 Pi」本身就是坏的。
    """
    base, argv_file, _marker = _make_shim(tmp_path)
    prompt = (
        "## 图片处理规则（必须遵守）\n"
        '- 运行识图命令：C:\\py.exe main.py --vision-describe "<图片路径>" "<用户问题，可空>"'
    ).replace("\n", "；")  # shim 无法传换行，规则文本本身用分号连接
    escaped = core_process.escape_cmd_shim_args(["--append-system-prompt", prompt], base)
    result = _run_shim(base, escaped, argv_file)
    assert result["rc"] == 0, result["stderr"]
    assert result["child"] is not None
    assert len(result["child"]) == 2
    assert "vision-describe" in result["child"][1]
    assert "图片路径" in result["child"][1]


# ---- provider / model 字符白名单（跨平台） -------------------------------


@pytest.mark.parametrize(
    "flag,value",
    [
        ("--provider", 'x" & calc & "y'),
        ("--provider", "a&b"),
        ("--provider", "a|b"),
        ("--provider", "a%PATH%b"),
        ("--provider", "a^b"),
        ("--provider", "a b"),
        ("--provider", "x" * 65),
        ("--provider", ""),
        ("--model", 'm"&calc&"x'),
        ("--model", "m;calc"),
        ("--model", "m$(calc)"),
        ("--model", "m`calc`"),
        ("--thinking", "high&calc"),
        ("--thinking", "high|calc"),
    ],
)
def test_launch_token_whitelist_rejects_metacharacters(flag, value):
    with pytest.raises(ValueError):
        core_process.validate_launch_tokens([flag, value])
    # 同样必须在 escape_cmd_shim_args 这个统一入口被拒（POSIX 也生效）
    with pytest.raises(ValueError):
        core_process.escape_cmd_shim_args([flag, value], ["/usr/bin/node", "cli.js"])


@pytest.mark.parametrize(
    "args",
    [
        ["--provider", "openrouter", "--model", "gpt-4o"],
        ["--provider", "google-vertex", "--model", "gemini-1.5-pro@002"],
        ["--provider", "z.ai", "--model", "google/gemini-2.0-flash-exp:free"],
        ["--provider", "deep_seek", "--model", "qwen2.5-72b-instruct", "--thinking", "high"],
        ["--model", "meta-llama/Llama-3.3-70B-Instruct"],
        ["--thinking", "medium"],
        # 尾部缺值不得越界崩溃
        ["--provider"],
        ["--model"],
    ],
)
def test_launch_token_whitelist_accepts_real_world_names(args):
    core_process.validate_launch_tokens(args)


def test_free_form_args_are_not_whitelisted():
    """只有 provider/model/thinking 受白名单约束，其他文本参数放行。"""
    args = ["--append-system-prompt", "任意中文 & 符号 | 都可以", "--approve"]
    assert core_process.escape_cmd_shim_args(args, ["node", "cli.js"]) == args


def test_posix_branch_is_passthrough():
    """POSIX / 非 shim 基座：参数一律原样返回，转义逻辑不得越界生效。"""
    nasty = ['x" & echo hi & "y', "a%b%c", "a^b", "多行\n文本"]
    assert core_process.escape_cmd_shim_args(nasty, ["/usr/bin/node", "cli.js"]) == nasty
    assert core_process.escape_cmd_shim_args(nasty, ["/usr/local/bin/pi"]) == nasty


# ---- pi_base_cmd：优先绕开 cmd.exe --------------------------------------


@WINDOWS_ONLY
def test_pi_base_cmd_resolves_npm_shim_to_direct_node(tmp_path, monkeypatch):
    """.cmd shim 应被解析成 [node, cli.js]，从根本上不经过 cmd.exe。"""
    from pi_manager import platform_util as pu

    pkg = tmp_path / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist"
    pkg.mkdir(parents=True)
    cli = pkg / "cli.js"
    cli.write_text("// cli", encoding="utf-8")
    node = tmp_path / "node.exe"
    node.write_bytes(b"MZ")
    shim = tmp_path / "pi.cmd"
    shim.write_text(
        '@ECHO off\r\nSET dp0=%~dp0\r\n'
        '"%_prog%"  "%dp0%\\node_modules\\@earendil-works\\pi-coding-agent'
        '\\dist\\cli.js" %*\r\n',
        encoding="utf-8",
    )
    # 屏蔽真机上的全局 npm 安装，确保走 shim 文本解析
    monkeypatch.setattr(pu, "npm_global_roots", lambda: [])
    monkeypatch.setattr(pu, "find_pi_cli_js", lambda: None)
    monkeypatch.setattr(core_process, "find_pi_command", lambda: str(shim))

    base = core_process.pi_base_cmd()
    assert base[0].lower().endswith("node.exe")
    assert base[1] == str(cli)
    assert "cmd.exe" not in [part.lower() for part in base]


@WINDOWS_ONLY
def test_pi_base_cmd_falls_back_to_cmd_shim_when_unresolvable(tmp_path, monkeypatch):
    """解析不出 cli.js 时仍可退回 shim —— 但转义层会守住注入面。"""
    from pi_manager import platform_util as pu

    shim = tmp_path / "pi.cmd"
    shim.write_text("@ECHO off\r\necho hi\r\n", encoding="utf-8")
    monkeypatch.setattr(pu, "npm_global_roots", lambda: [])
    monkeypatch.setattr(pu, "find_pi_cli_js", lambda: None)
    monkeypatch.setattr(core_process, "find_pi_command", lambda: str(shim))

    base = core_process.pi_base_cmd()
    assert base == ["cmd.exe", "/c", str(shim)]
    with pytest.raises(ValueError):
        core_process.escape_cmd_shim_args(["--provider", 'a" & calc & "b'], base)


def test_pi_base_cmd_prefers_nodecli_marker(monkeypatch, tmp_path):
    """NODECLI:: 直启标记优先，与平台无关。"""
    monkeypatch.setattr(
        core_process, "find_pi_command", lambda: "NODECLI::/usr/bin/node::/pkg/cli.js"
    )
    assert core_process.pi_base_cmd() == ["/usr/bin/node", "/pkg/cli.js"]


# ============================================================================
# core_process 子进程边界：8 MiB 输出限额 / 进程树连带终止 / 超时
# 审计（r2-testing P0-3）指出 core_process.py:78-103 与 run_pi 的限额/超时分支
# **从未被执行过**：所有 run_pi 测试都 monkeypatch 掉它，唯一真实 spawn 走成功
# 快路径。以下用例起真实子进程 + 真实孙进程覆盖这些分支。
# ============================================================================

_GRANDCHILD_SRC = """
import os, sys, time
beat = sys.argv[1]
n = 0
while True:
    n += 1
    with open(beat, "w", encoding="utf-8") as fh:
        fh.write(f"{os.getpid()} {n}")
    time.sleep(0.05)
"""

_PARENT_SRC = """
import os, subprocess, sys
here = os.path.dirname(os.path.abspath(__file__))
# 孙进程继承 stdout/stderr 管道 —— 这正是「只杀 pid 不杀树」会让读取线程
# 永久阻塞的真实场景。
subprocess.Popen([sys.executable, os.path.join(here, "grandchild.py"),
                  os.environ["PROBE_BEAT"]])
with open(os.environ["PROBE_PID"], "w", encoding="utf-8") as fh:
    fh.write(str(os.getpid()))
mode = os.environ["PROBE_MODE"]
if mode == "flood":
    blob = b"x" * (256 * 1024)
    while True:
        sys.stdout.buffer.write(blob)
        sys.stdout.buffer.flush()
else:
    import time
    time.sleep(120)
"""


@pytest.fixture
def child_scripts(tmp_path):
    """真实父/孙进程脚本 + 心跳文件；退出时确保不留僵尸。"""
    (tmp_path / "grandchild.py").write_text(_GRANDCHILD_SRC, encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(_PARENT_SRC, encoding="utf-8")
    beat = tmp_path / "beat.txt"
    pidfile = tmp_path / "parent-pid.txt"
    yield {"parent": parent, "beat": beat, "pidfile": pidfile}
    for path in (beat, pidfile):
        _kill_pid_from(path)


def _kill_pid_from(path: Path) -> None:
    """兜底清理：把心跳/pid 文件里记录的进程杀掉，绝不留僵尸。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    token = text.split(" ")[0] if text else ""
    if not token.isdigit():
        return
    pid = int(token)
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
        else:
            os.kill(pid, 9)
    except Exception:
        pass


def _wait_for(path: Path, timeout: float = 15.0) -> str:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
        time.sleep(0.05)
    raise AssertionError(f"{path.name} 在 {timeout}s 内没有出现内容")


def _assert_heartbeat_stopped(beat: Path, label: str) -> None:
    """心跳不再前进 ⇒ 孙进程已被连带回收。"""
    import time

    def counter() -> int:
        try:
            return int(beat.read_text(encoding="utf-8").strip().split(" ")[1])
        except (OSError, IndexError, ValueError):
            return -1

    # 给 taskkill / SIGKILL 一点落地时间，再取两次样本比对。
    time.sleep(0.8)
    first = counter()
    time.sleep(1.0)
    second = counter()
    assert first == second, (
        f"{label}: 孙进程仍在运行（心跳 {first} -> {second}），进程树未被连带终止"
    )


def test_terminate_process_tree_kills_grandchildren(child_scripts):
    """_terminate_process_tree 必须连带回收孙进程（此前 0 覆盖）。"""
    from pi_manager import proc

    env = os.environ.copy()
    env.update(
        PROBE_BEAT=str(child_scripts["beat"]),
        PROBE_PID=str(child_scripts["pidfile"]),
        PROBE_MODE="sleep",
    )
    creationflags = proc.create_no_window_flag()
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags |= subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(
        [sys.executable, str(child_scripts["parent"])],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        creationflags=creationflags,
        start_new_session=sys.platform != "win32",
    )
    try:
        _wait_for(child_scripts["beat"])
        core_process._terminate_process_tree(process)
        assert process.poll() is not None, "父进程未退出"
        _assert_heartbeat_stopped(child_scripts["beat"], "_terminate_process_tree")
        # 幂等：进程已退出时再调用一次不得抛异常
        core_process._terminate_process_tree(process)
    finally:
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_run_pi_truncates_output_at_limit_and_kills_tree(child_scripts, monkeypatch):
    """8 MiB 输出限额：截断到限额、返回 -1、进程树被连带杀掉。"""
    from pi_manager import core

    monkeypatch.setattr(
        core, "pi_base_cmd", lambda: [sys.executable, str(child_scripts["parent"])]
    )
    result = core_process.run_pi(
        [],
        timeout=180,
        env={
            "PROBE_BEAT": str(child_scripts["beat"]),
            "PROBE_PID": str(child_scripts["pidfile"]),
            "PROBE_MODE": "flood",
        },
    )
    assert result.returncode == -1
    assert result.stderr.startswith("process output limit exceeded")
    assert len(result.stdout) == 8 * 1024 * 1024, len(result.stdout)
    assert set(result.stdout) == {"x"}
    _assert_heartbeat_stopped(child_scripts["beat"], "run_pi 输出超限")


def test_run_pi_timeout_kills_tree(child_scripts, monkeypatch):
    """超时分支：抛 TimeoutExpired，且进程树（含孙进程）被回收。"""
    from pi_manager import core

    monkeypatch.setattr(
        core, "pi_base_cmd", lambda: [sys.executable, str(child_scripts["parent"])]
    )
    with pytest.raises(subprocess.TimeoutExpired):
        core_process.run_pi(
            [],
            timeout=1.0,
            env={
                "PROBE_BEAT": str(child_scripts["beat"]),
                "PROBE_PID": str(child_scripts["pidfile"]),
                "PROBE_MODE": "sleep",
            },
        )
    _assert_heartbeat_stopped(child_scripts["beat"], "run_pi 超时")


def test_run_pi_cancelled_kills_tree(child_scripts, monkeypatch):
    """协作式取消：is_cancelled 为真时终止进程树并返回已停止。"""
    from pi_manager import core

    monkeypatch.setattr(
        core, "pi_base_cmd", lambda: [sys.executable, str(child_scripts["parent"])]
    )
    started = time.monotonic()
    result = core_process.run_pi(
        [],
        timeout=30,
        env={
            "PROBE_BEAT": str(child_scripts["beat"]),
            "PROBE_PID": str(child_scripts["pidfile"]),
            "PROBE_MODE": "sleep",
        },
        is_cancelled=lambda: time.monotonic() - started > 0.2,
    )
    assert result.returncode == -1
    assert "已停止生成" in result.stderr
    _assert_heartbeat_stopped(child_scripts["beat"], "run_pi 取消")


def test_run_pi_success_fast_path_still_works(tmp_path, monkeypatch):
    """回归：正常退出的子进程仍返回完整 stdout / returncode。"""
    from pi_manager import core

    script = tmp_path / "ok.py"
    script.write_text(
        "import sys\nsys.stdout.write('hello 世界')\nsys.stderr.write('warn')\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(core, "pi_base_cmd", lambda: [sys.executable, str(script)])
    result = core_process.run_pi([], timeout=60)
    assert result.returncode == 3
    assert result.stdout == "hello 世界"
    assert result.stderr == "warn"


# ---- 代理相关加固（P2-6 / _is_private_host） -----------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://user:pass@127.0.0.1:7890", "http://***:***@127.0.0.1:7890"),
        ("http://user@proxy.example.com:8080", "http://***@proxy.example.com:8080"),
        ("http://127.0.0.1:7890", "http://127.0.0.1:7890"),
        ("", ""),
    ],
)
def test_redact_proxy_url_strips_userinfo(url, expected):
    assert core_process.redact_proxy_url(url) == expected


@pytest.mark.parametrize(
    "host",
    ["127.0.0.1", "localhost", "10.1.2.3", "192.168.0.1", "::1", "::ffff:127.0.0.1",
     "0.0.0.0", "169.254.1.1"],
)
def test_is_private_host_covers_local_targets(host):
    assert core_process._is_private_host(host) is True


@pytest.mark.parametrize("host", ["1.1.1.1", "example.com", "2001:4860:4860::8888"])
def test_is_private_host_rejects_public_targets(host):
    assert core_process._is_private_host(host) is False
