"""broker token 的 Windows ACL 加固与授权模型回归测试。

为什么需要这一组用例：历史上 `config_broker._restrict_windows_acl` 因为引用了并不
存在的 `ctypes.wintypes.PVOID`，在设置 argtypes 时就抛 AttributeError，又被
`except Exception: pass` 吞掉 —— **在任何 Windows 机器上都是彻底的 no-op**，而
docstring 与历史修复记录都声称它生效了。既有测试只断言「token 排他创建 + 比对成功」，
完全没有断言 ACL，所以这个 no-op 潜伏了很久。

因此本文件的 Windows 用例一律断言 **DACL 的实际效果**（受保护位、ACE 条数、
继承 ACE 条数、受托者 SID），而不是「函数没抛异常」。
"""
from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from pi_manager import config_broker, helper_registry, platform_util, provider_env, storage

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL 专属")
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX 权限位专属")


def _assert_owner_only(summary: dict | None, *, what: str) -> None:
    """断言 DACL 已收紧为「仅当前用户、无继承」。"""
    assert summary is not None, f"{what}: 无法读回 DACL"
    assert summary["null_dacl"] is False, (
        f"{what}: DACL 为 NULL —— Microsoft 明确说明这会授予所有本地用户完全访问，"
        "是本项目历史上的严重漏洞，绝不允许回归"
    )
    assert summary["protected"] is True, (
        f"{what}: DACL 未设置 SE_DACL_PROTECTED，父目录的继承 ACE 没有被剥离"
    )
    assert summary["inherited_ace_count"] == 0, (
        f"{what}: 仍有 {summary['inherited_ace_count']} 条继承 ACE"
    )
    assert summary["ace_count"] == 1, f"{what}: 期望恰好 1 条 ACE，实际 {summary['ace_count']}"
    own_sid = platform_util.current_user_sid_string()
    assert own_sid, f"{what}: 无法取得当前用户 SID"
    assert summary["trustee_sids"] == [own_sid], (
        f"{what}: 受托者应只有当前用户 {own_sid}，实际 {summary['trustee_sids']}"
    )


# --------------------------------------------------------------------------
# P1-1　ACL 加固的实际效果
# --------------------------------------------------------------------------
@WINDOWS_ONLY
def test_restrict_windows_acl_actually_strips_inherited_aces(tmp_path):
    """加固前后必须真的不一样：这是能识别出 no-op 的唯一断言方式。"""
    target = tmp_path / "sensitive.txt"
    target.write_text("secret", encoding="utf-8")
    before = platform_util.windows_dacl_summary(target)
    assert before is not None

    assert platform_util.restrict_windows_acl(target) is True

    after = platform_util.windows_dacl_summary(target)
    _assert_owner_only(after, what="restrict_windows_acl 之后")
    if before["inherited_ace_count"] > 0:
        # 旧的 no-op 实现会让 before == after，这条断言正是它当年应该失败的地方。
        assert after != before, "加固前后 DACL 完全一致 —— 加固很可能是 no-op"


@WINDOWS_ONLY
def test_restrict_windows_acl_hardens_directories_with_inheritance(tmp_path):
    """目录加固必须带 (OI)(CI)，否则此后新建的文件仍会继承宽松 ACE。"""
    directory = tmp_path / "agentdir"
    directory.mkdir()
    assert platform_util.restrict_windows_acl(directory) is True
    _assert_owner_only(platform_util.windows_dacl_summary(directory), what="目录加固之后")

    child = directory / "inherited.json"
    child.write_text("{}", encoding="utf-8")
    child_summary = platform_util.windows_dacl_summary(child)
    assert child_summary is not None
    assert child_summary["null_dacl"] is False
    assert child_summary["trustee_sids"] == [platform_util.current_user_sid_string()], (
        "受保护目录中新建的文件应只继承当前用户这一条 ACE"
    )


@WINDOWS_ONLY
def test_broker_token_file_is_owner_only(isolated_home):
    """真实创建路径的端到端断言：token 落盘后 DACL 必须是 owner-only。"""
    config_broker._create_broker_token()
    path = config_broker.broker_token_path()
    assert path.is_file()
    _assert_owner_only(platform_util.windows_dacl_summary(path), what="broker token")
    _assert_owner_only(
        platform_util.windows_dacl_summary(path.parent), what="~/.pi/agent 目录"
    )


@WINDOWS_ONLY
def test_helper_registry_file_is_owner_only(isolated_home):
    """pi-manager-helper.json 的 command 字段会被编辑器扩展执行，必须 owner-only。"""
    helper_registry.register_current_helper()
    _assert_owner_only(
        platform_util.windows_dacl_summary(helper_registry.registry_path()),
        what="pi-manager-helper.json",
    )


@WINDOWS_ONLY
def test_harden_agent_dir_covers_preexisting_sensitive_files(isolated_home):
    """目录继承只覆盖此后新建的文件，已存在的敏感文件必须被逐个补上。"""
    from pi_manager import core

    agent_dir = core.pi_agent_dir()
    agent_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for name in ("secrets.vault", ".vault_master_key", "secrets.index.json", "auth.json"):
        target = agent_dir / name
        target.write_bytes(b"x" * 32)
        created.append(target)

    helper_registry.harden_agent_dir_best_effort()

    for target in created:
        _assert_owner_only(platform_util.windows_dacl_summary(target), what=target.name)


@WINDOWS_ONLY
def test_win_acl_api_prototypes_build():
    """直接护住历史根因：_win_acl_api() 在真实 Windows 上必须能建起全部原型。"""
    api = platform_util._win_acl_api()
    for name in ("advapi32", "kernel32", "EXPLICIT_ACCESS_W", "ACCESS_ALLOWED_ACE"):
        assert name in api


def test_source_never_reintroduces_nonexistent_win32_symbols():
    """源码级回归护栏：这两个名字都不存在，写上去就是保证 no-op。

    - ctypes.wintypes 没有 PVOID（只有 LPVOID）。
    - LocalFree 由 kernel32 导出，advapi32 并不导出它。
    两者任一出现在 argtypes 里都会在第一段就抛 AttributeError。
    """
    import io
    import tokenize
    from pathlib import Path

    root = Path(platform_util.__file__).parent
    for name in ("platform_util.py", "config_broker.py", "provider_env.py"):
        source = (root / name).read_text(encoding="utf-8")
        # 只看真正的代码 token：注释与 docstring 里必须允许提到这两个名字，
        # 否则「记录历史根因」的注释本身会让这条护栏失效。
        code_text = " ".join(
            token.string
            for token in tokenize.generate_tokens(io.StringIO(source).readline)
            if token.type not in (tokenize.COMMENT, tokenize.STRING)
        )
        assert "wintypes . PVOID" not in code_text, f"{name} 引用了不存在的 wintypes.PVOID"
        assert "advapi32 . LocalFree" not in code_text, (
            f"{name} 引用了不存在的 advapi32.LocalFree"
        )


def test_acl_hardening_failure_is_logged_not_swallowed(monkeypatch, caplog):
    """安全加固失败绝不允许静默伪装成成功 —— 这是本次审查发现的系统性反模式。"""
    monkeypatch.setattr(platform_util, "restrict_windows_acl", lambda path: False)
    with caplog.at_level("WARNING", logger="pi_manager.config_broker"):
        assert config_broker._restrict_windows_acl(config_broker.broker_token_path()) is False
    assert any(record.levelname == "WARNING" for record in caplog.records), (
        "加固失败必须留下 WARNING 日志"
    )


@POSIX_ONLY
def test_harden_private_path_uses_permission_bits_on_posix(tmp_path):
    """POSIX 分支不得被 Windows 改动破坏：文件 0600、目录 0700。"""
    target = tmp_path / "secret.txt"
    target.write_text("x", encoding="utf-8")
    target.chmod(0o666)
    assert platform_util.harden_private_path(target) is True
    assert stat.S_IMODE(target.stat().st_mode) == 0o600

    directory = tmp_path / "dir"
    directory.mkdir(mode=0o777)
    assert platform_util.harden_private_path(directory) is True
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700


@POSIX_ONLY
def test_windows_only_helpers_are_inert_on_posix(tmp_path):
    target = tmp_path / "x.txt"
    target.write_text("x", encoding="utf-8")
    assert platform_util.restrict_windows_acl(target) is False
    assert platform_util.windows_dacl_summary(target) is None
    assert platform_util.current_user_sid_string() == ""


def test_restrict_windows_acl_reports_failure_for_missing_path(tmp_path, caplog):
    """不存在的路径必须返回 False（而不是被当作加固成功）。"""
    with caplog.at_level("WARNING", logger="pi_manager.platform_util"):
        assert platform_util.restrict_windows_acl(tmp_path / "nope.txt") is False


# --------------------------------------------------------------------------
# P2-9　broker token 读取的形态校验
# --------------------------------------------------------------------------
def test_verify_broker_token_rejects_non_regular_file(isolated_home):
    """token 路径是目录/重解析点时必须拒绝，而不是当作读取失败之外的任何结果。"""
    path = config_broker.broker_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()
    assert config_broker._verify_broker_token("anything") is False


def test_verify_broker_token_rejects_oversized_file(isolated_home):
    path = config_broker.broker_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("a" * (config_broker._BROKER_TOKEN_MAX_BYTES + 10), encoding="utf-8")
    assert config_broker._verify_broker_token("a" * 64) is False


def test_verify_broker_token_accepts_created_token(isolated_home):
    token = config_broker._create_broker_token()
    assert config_broker._verify_broker_token(token) is True
    assert config_broker._verify_broker_token("") is False
    assert config_broker._verify_broker_token("0" * 64) is False


# --------------------------------------------------------------------------
# P2-10　.config-revisions.json 的 sha256 真的被校验
# --------------------------------------------------------------------------
def test_recorded_revision_sha256_detects_external_modification(isolated_home):
    token = config_broker._create_broker_token()
    request = {
        "schema_version": 1,
        "token": token,
        "request_id": "r1",
        "operation": "set_manager_fields",
        "arguments": {"fields": {"failover_enabled": True}},
    }
    assert config_broker.mutate(request)["ok"] is True

    from pi_manager import core

    target = core.manager_config_path()
    assert config_broker.verify_recorded_revision(target) is True
    # broker 之外的写入者改动文件后，记录的 sha256 必须不再匹配。
    storage.save_json(target, {"failover_enabled": False, "tampered": 1})
    assert config_broker.verify_recorded_revision(target) is False


# --------------------------------------------------------------------------
# P2-11　请求文件的形态校验
# --------------------------------------------------------------------------
def test_mutate_file_rejects_directory_and_oversized_request(isolated_home, tmp_path):
    directory = tmp_path / "req-as-dir"
    directory.mkdir()
    result = config_broker.mutate_file(directory)
    assert result["ok"] is False
    assert "regular_file" in result["error"] or result["error"] == "invalid_request_file"

    big = tmp_path / "big.json"
    big.write_text("x" * (config_broker._REQUEST_FILE_MAX_BYTES + 1), encoding="utf-8")
    assert config_broker.mutate_file(big) == {"ok": False, "error": "invalid_request_file"}

    assert config_broker.mutate_file(tmp_path / "missing.json") == {
        "ok": False,
        "error": "invalid_request_file",
    }


# --------------------------------------------------------------------------
# P1-5　--print-provider-env 的授权模型不再倒置
# --------------------------------------------------------------------------
def test_provider_env_requires_broker_token(isolated_home, tmp_path, capsys):
    """无 token 时必须拒绝：此入口直接吐明文 Key，不能比 --config-mutate 更宽松。"""
    output = tmp_path / "resp.json"
    output.write_text("", encoding="utf-8")
    code = provider_env.main(["--output", str(output), "openai"])
    assert code == 2
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert "broker token" in payload["error"]


def test_provider_env_rejects_wrong_token(isolated_home, tmp_path):
    config_broker._create_broker_token()
    output = tmp_path / "resp.json"
    output.write_text("", encoding="utf-8")
    code = provider_env.main(["--token", "0" * 64, "--output", str(output), "openai"])
    assert code == 2
    assert "校验失败" in json.loads(output.read_text(encoding="utf-8"))["error"]


def test_provider_env_token_file_cannot_point_at_broker_token(isolated_home, tmp_path):
    """混淆代理防护：helper 以用户身份运行，直接给出 token 路径等于不出示凭据。"""
    config_broker._create_broker_token()
    output = tmp_path / "resp.json"
    output.write_text("", encoding="utf-8")
    code = provider_env.main(
        [
            "--token-file",
            str(config_broker.broker_token_path()),
            "--output",
            str(output),
            "openai",
        ]
    )
    assert code == 2
    assert "不能指向 broker token 本身" in json.loads(output.read_text(encoding="utf-8"))["error"]


def test_provider_env_accepts_token_file_with_copied_value(isolated_home, tmp_path):
    """按值出示 token 应通过鉴权（随后因 provider 不存在而失败，说明已越过鉴权）。"""
    token = config_broker._create_broker_token()
    token_file = tmp_path / "tok"
    token_file.write_text(token, encoding="utf-8")
    output = tmp_path / "resp.json"
    output.write_text("", encoding="utf-8")
    code = provider_env.main(
        ["--token-file", str(token_file), "--output", str(output), "no-such-provider"]
    )
    payload = json.loads(output.read_text(encoding="utf-8"))
    # 越过鉴权即算通过：未配置的 provider 会得到空 env（不是鉴权拒绝）。
    assert code == 0
    assert payload == {"ok": True, "env": {}, "key_id": ""}


def test_provider_env_refuses_plaintext_key_on_stdout(isolated_home, capsys):
    """强制 --output：明文 Key 绝不写 stdout（管道/日志/终端回滚都会留痕）。"""
    token = config_broker._create_broker_token()
    code = provider_env.main(["--token", token, "openai"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is False
    assert "--output" in payload["error"]


def test_provider_env_argparse_error_stays_json_only(isolated_home, capsys):
    """P3-3：argparse 的 usage 文本会破坏扩展依赖的 JSON-only 契约。"""
    code = provider_env.main(["--unknown-flag"])
    assert code == 2
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload == {"ok": False, "error": "invalid_arguments"}


def test_provider_env_token_file_must_be_regular_file(isolated_home, tmp_path):
    config_broker._create_broker_token()
    directory = tmp_path / "tokdir"
    directory.mkdir()
    output = tmp_path / "resp.json"
    output.write_text("", encoding="utf-8")
    code = provider_env.main(
        ["--token-file", str(directory), "--output", str(output), "openai"]
    )
    assert code == 2
    assert "普通文件" in json.loads(output.read_text(encoding="utf-8"))["error"]


def test_provider_env_does_not_import_pyside6():
    """AGENTS.md 硬边界：Cursor 扩展热路径不得拖起 GUI。"""
    assert "PySide6" not in sys.modules or True  # 说明性断言，真正的检查在下面
    import ast
    from pathlib import Path

    root = Path(platform_util.__file__).parent
    seen: set[str] = set()

    def walk(module: str) -> None:
        if module in seen:
            return
        seen.add(module)
        path = root / f"{module}.py"
        if not path.exists():
            return
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert "PySide6" not in alias.name, (module, alias.name)
            elif isinstance(node, ast.ImportFrom):
                assert "PySide6" not in (node.module or ""), (module, node.module)
                if node.level == 1 and node.module:
                    walk(node.module)

    walk("provider_env")
    walk("config_broker")
    walk("helper_registry")
    assert "config_broker" in seen


def test_isolated_home_is_actually_isolated(isolated_home):
    """守住测试污染真实 ~/.pi/agent/ 的历史事故。"""
    from pi_manager import core

    assert str(isolated_home) in str(core.pi_agent_dir())
    assert str(isolated_home) in str(config_broker.broker_token_path())
    assert os.path.expanduser("~") == str(isolated_home)
