# -*- coding: utf-8 -*-
"""内置插件（skills / extensions）落盘机制与一键安装的单元测试。

覆盖：
- 清单加载与校验（缺字段、未知类型、目标越界）
- 落盘幂等性、模板渲染、强制重写
- 状态查询（on_disk / npm_installed / ready）
- install_one_click 成功与失败路径
- npm_install 的错误回退（命令拼接）
- self_check 完整性
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from pi_manager import builtin_plugins as bp
from pi_manager import core


# ---- 测试辅助 ----


def _make_plugin(**overrides) -> bp.BuiltinPlugin:
    """基于真实清单构造一个 BuiltinPlugin，可覆盖任意字段。"""
    base = next(p for p in bp.list_builtins() if p.name == "pi-manager-vision")
    fields = dict(
        name=base.name,
        type=base.type,
        description=base.description,
        source=base.source,
        target_dir=base.target_dir,
        templated=base.templated,
        template_vars=base.template_vars,
        min_version=base.min_version,
        needs_npm_install=base.needs_npm_install,
        enabled_by_default=base.enabled_by_default,
    )
    fields.update(overrides)
    return bp.BuiltinPlugin(**fields)


def _tamper_manifest_target_dir(monkeypatch, tmp_path, target_dir: str) -> None:
    """把真实 manifest 的 target_dir 篡改后写入 tmp，并让 _load_manifest 读取它。"""
    real = Path(__file__).resolve().parent.parent / "assets" / "builtin" / "manifest.json"
    data = json.loads(real.read_text(encoding="utf-8"))
    data["plugins"][0]["target_dir"] = target_dir
    fake = tmp_path / "manifest.json"
    fake.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(
        bp.resources,
        "asset_path",
        lambda *parts: fake if parts == bp._MANIFEST_REL else None,
    )


# ---- 清单与校验 ----


def test_list_builtins_returns_manifest_entries(isolated_home):
    plugins = bp.list_builtins()
    names = {p.name for p in plugins}
    assert "pi-manager-vision" in names
    assert "pi-manager-mcp-bridge" in names
    # v1.8.2 新增内置插件
    for expected in (
        "commit-message",
        "document-processing",
        "pi-sensitive-guard",
        "pi-git-checkpoint",
        "pi-manager-state",
    ):
        assert expected in names, f"缺少内置插件 {expected}"
    for p in plugins:
        assert p.type in {"skill", "extension"}
        assert p.source
        assert p.target_dir


def test_new_extensions_do_not_need_npm(isolated_home):
    """3 个新 extension 仅依赖 node 内置模块，落盘即用，无需 npm install。"""
    for name in ("pi-sensitive-guard", "pi-git-checkpoint", "pi-manager-state"):
        plugin = next(p for p in bp.list_builtins() if p.name == name)
        assert plugin.needs_npm_install is False
        assert plugin.enabled_by_default is True
        src = (
            Path(__file__).resolve().parent.parent
            / "assets"
            / "builtin"
            / plugin.source
        )
        assert (src / "index.ts").exists(), f"{name} 缺少 index.ts"


def test_builtin_plugin_target_path_under_agent_dir(isolated_home):
    agent_dir = core.pi_agent_dir().resolve()
    for p in bp.list_builtins():
        rel = p.target_path.resolve().relative_to(agent_dir)
        # 用正斜杠归一化后比较，避免平台分隔符差异
        assert str(rel).replace("\\", "/") == p.target_dir


def test_self_check_passes_for_valid_manifest(isolated_home):
    assert bp.self_check() == []


def test_self_check_rejects_unknown_type(isolated_home, monkeypatch):
    original = bp._load_manifest

    def fake_manifest():
        plugins = original()
        # 篡改第一个插件的类型为非法值
        first = plugins[0]
        return [
            bp.BuiltinPlugin(
                name=first.name,
                type="bogus",
                description=first.description,
                source=first.source,
                target_dir=first.target_dir,
                templated=first.templated,
                template_vars=first.template_vars,
                min_version=first.min_version,
                needs_npm_install=first.needs_npm_install,
                enabled_by_default=first.enabled_by_default,
            )
        ]

    monkeypatch.setattr(bp, "_load_manifest", fake_manifest)
    errors = bp.self_check()
    assert any("未知类型" in e for e in errors)


def test_self_check_rejects_target_outside_agent_dir(isolated_home, monkeypatch):
    original = bp._load_manifest

    def fake_manifest():
        plugins = original()
        first = plugins[0]
        return [
            bp.BuiltinPlugin(
                name=first.name,
                type=first.type,
                description=first.description,
                source=first.source,
                target_dir="../../escaped",
                templated=first.templated,
                template_vars=first.template_vars,
                min_version=first.min_version,
                needs_npm_install=first.needs_npm_install,
                enabled_by_default=first.enabled_by_default,
            )
        ]

    monkeypatch.setattr(bp, "_load_manifest", fake_manifest)
    errors = bp.self_check()
    assert any("越界" in e for e in errors)


def test_load_manifest_rejects_windows_absolute_target_dir(isolated_home, monkeypatch, tmp_path):
    _tamper_manifest_target_dir(monkeypatch, tmp_path, "C:/Windows/evil")
    with pytest.raises(bp.BuiltinPluginError, match="绝对路径"):
        bp._load_manifest()


def test_load_manifest_rejects_posix_absolute_target_dir(isolated_home, monkeypatch, tmp_path):
    _tamper_manifest_target_dir(monkeypatch, tmp_path, "/etc/evil")
    with pytest.raises(bp.BuiltinPluginError, match="绝对路径"):
        bp._load_manifest()


def test_load_manifest_rejects_drive_relative_target_dir(isolated_home, monkeypatch, tmp_path):
    # C:foo 在 Windows 是「当前盘相对路径」，POSIX 上 Path.is_absolute() 不识别，必须显式拒绝
    _tamper_manifest_target_dir(monkeypatch, tmp_path, "C:evil")
    with pytest.raises(bp.BuiltinPluginError, match="绝对路径"):
        bp._load_manifest()


def test_load_manifest_rejects_dotdot_target_dir(isolated_home, monkeypatch, tmp_path):
    _tamper_manifest_target_dir(monkeypatch, tmp_path, "skills/../../escaped")
    with pytest.raises(bp.BuiltinPluginError, match=r"\.\."):
        bp._load_manifest()


def test_load_manifest_rejects_backslash_dotdot_target_dir(isolated_home, monkeypatch, tmp_path):
    # 反斜杠形式的 .. 段（Windows 风格）也必须被拒绝
    _tamper_manifest_target_dir(monkeypatch, tmp_path, "skills\\..\\..\\escaped")
    with pytest.raises(bp.BuiltinPluginError, match=r"\.\."):
        bp._load_manifest()


def test_install_one_rejects_outside_target(isolated_home, monkeypatch, tmp_path):
    """纵深防御：即使绕过 _load_manifest 的解析期校验，_install_one 落盘前也拒绝越界 target。"""
    plugin = _make_plugin(target_dir="../../escaped")
    src = tmp_path / "src"
    src.mkdir()
    monkeypatch.setattr(bp, "_load_manifest", lambda: [plugin])
    monkeypatch.setattr(bp, "_builtin_assets_dir", lambda: tmp_path)
    with pytest.raises(bp.BuiltinPluginError, match="目标路径"):
        bp.install_builtin(plugin.name)


def test_install_builtin_force_never_rmtrees_outside_agent_dir(isolated_home, monkeypatch):
    """force 分支的 rmtree 前必须再次校验 target：越界时不删除任何目录。"""
    victim = core.pi_agent_dir().parent.parent / "pwned"
    victim.mkdir()
    plugin = _make_plugin(target_dir="../../pwned")
    monkeypatch.setattr(bp, "_load_manifest", lambda: [plugin])
    with pytest.raises(bp.BuiltinPluginError, match="目标路径"):
        bp.install_builtin(plugin.name, force=True)
    assert victim.is_dir()


def test_install_all_builtins_force_skips_outside_target(isolated_home, monkeypatch):
    victim = core.pi_agent_dir().parent.parent / "pwned"
    victim.mkdir()
    plugin = _make_plugin(target_dir="../../pwned")
    monkeypatch.setattr(bp, "_load_manifest", lambda: [plugin])
    result = bp.install_all_builtins(force=True)
    assert result["ok"] is False
    entry = next(r for r in result["installed"] if r["name"] == plugin.name)
    assert entry["ok"] is False
    assert "目标路径" in entry["error"]
    assert victim.is_dir()


def test_force_rmtree_is_guarded_by_target_check(isolated_home, monkeypatch):
    """force 安装时 _assert_safe_target_dir 至少在落盘入口与 rmtree 前各被调用一次。"""
    bp.install_builtin("pi-manager-vision")
    calls = []
    real = bp._assert_safe_target_dir

    def spy(plugin):
        calls.append(plugin.name)
        real(plugin)

    monkeypatch.setattr(bp, "_assert_safe_target_dir", spy)
    bp.install_builtin("pi-manager-vision", force=True)
    assert calls.count("pi-manager-vision") >= 2


# ---- 落盘与幂等 ----


def test_install_vision_skill_renders_template_and_lands_correctly(isolated_home):
    result = bp.install_builtin("pi-manager-vision")
    assert result["ok"] is True
    assert result["updated"] is True
    skill_file = core.pi_agent_dir() / "skills" / "pi-manager-vision" / "SKILL.md"
    assert skill_file.exists()
    content = skill_file.read_text(encoding="utf-8")
    # 模板变量应已渲染，不留裸占位
    assert "{{vision_command}}" not in content
    assert "--vision-describe" in content


def test_install_is_idempotent_when_content_unchanged(isolated_home):
    first = bp.install_builtin("pi-manager-vision")
    second = bp.install_builtin("pi-manager-vision")
    assert first["updated"] is True
    assert second["updated"] is False
    assert second["skipped"] == ["SKILL.md"]


def test_install_all_builtins_skips_disabled_by_default(isolated_home):
    # 默认只装 enabled_by_default=True 的，MCP 桥不应落盘
    result = bp.install_all_builtins()
    names_installed = {r["name"] for r in result["installed"]}
    assert "pi-manager-vision" in names_installed
    assert "pi-manager-mcp-bridge" not in names_installed


def test_install_all_builtins_includes_disabled_when_requested(isolated_home):
    result = bp.install_all_builtins(include_disabled=True)
    names_installed = {r["name"] for r in result["installed"]}
    assert "pi-manager-mcp-bridge" in names_installed


def test_install_all_builtins_lands_new_plugins(isolated_home):
    """默认安装应落盘全部新插件（含脚本与 SKILL.md）。"""
    result = bp.install_all_builtins()
    names_installed = {r["name"] for r in result["installed"]}
    for name in (
        "commit-message",
        "document-processing",
        "pi-sensitive-guard",
        "pi-git-checkpoint",
        "pi-manager-state",
    ):
        assert name in names_installed
    agent = core.pi_agent_dir()
    assert (agent / "skills" / "commit-message" / "SKILL.md").exists()
    doc_dir = agent / "skills" / "document-processing"
    for script in (
        "extract_docx.py",
        "extract_xlsx.py",
        "extract_pptx.py",
        "extract_pdf.py",
    ):
        assert (doc_dir / "scripts" / script).exists()
    for ext in ("pi-sensitive-guard", "pi-git-checkpoint", "pi-manager-state"):
        assert (agent / "extensions" / ext / "index.ts").exists()


def test_sensitive_guard_contains_guard_logic(isolated_home):
    """敏感防泄漏扩展必须包含拦截与抹除逻辑的关键结构。"""
    plugin = next(p for p in bp.list_builtins() if p.name == "pi-sensitive-guard")
    src_root = Path(__file__).resolve().parent.parent / "assets" / "builtin" / plugin.source
    content = (src_root / "index.ts").read_text(encoding="utf-8")
    assert 'pi.on("tool_call"' in content
    assert 'pi.on("tool_result"' in content
    assert "secrets.vault" in content
    assert "auth.json" in content
    assert "REDACTED" in content


def test_git_checkpoint_registers_commands(isolated_home):
    plugin = next(p for p in bp.list_builtins() if p.name == "pi-git-checkpoint")
    src_root = Path(__file__).resolve().parent.parent / "assets" / "builtin" / plugin.source
    content = (src_root / "index.ts").read_text(encoding="utf-8")
    assert "git-checkpoints" in content
    assert "git-checkpoint-restore" in content
    assert "stash" in content


def test_manager_state_injection_is_readonly(isolated_home):
    plugin = next(p for p in bp.list_builtins() if p.name == "pi-manager-state")
    src_root = Path(__file__).resolve().parent.parent / "assets" / "builtin" / plugin.source
    content = (src_root / "index.ts").read_text(encoding="utf-8")
    assert "before_agent_start" in content
    assert "pi-manager-health.json" in content
    assert "readFileSync" in content
    # 只读：不得出现写文件 API
    assert "writeFileSync" not in content
    assert "appendFileSync" not in content


def test_skill_frontmatter_is_valid(isolated_home):
    """新增 skill 必须有合法 frontmatter（name + description）。"""
    for name in ("commit-message", "document-processing"):
        plugin = next(p for p in bp.list_builtins() if p.name == name)
        src_root = Path(__file__).resolve().parent.parent / "assets" / "builtin" / plugin.source
        text = (src_root / "SKILL.md").read_text(encoding="utf-8")
        assert text.startswith("---")
        assert "name:" in text.split("---")[1]
        assert "description:" in text.split("---")[1]


def test_install_all_builtins_force_rewrites(isolated_home):
    bp.install_builtin("pi-manager-vision")
    result = bp.install_all_builtins(force=True)
    vision = next(r for r in result["installed"] if r["name"] == "pi-manager-vision")
    assert vision["updated"] is True


def test_install_unknown_plugin_raises(isolated_home):
    with pytest.raises(bp.BuiltinPluginError, match="未知"):
        bp.install_builtin("does-not-exist")


def test_install_one_wraps_template_decode_error(isolated_home, monkeypatch, tmp_path):
    """模板文件不是合法 UTF-8 时，应包装为 BuiltinPluginError（含文件路径）而非冒泡。"""
    plugin = _make_plugin(
        name="bad-skill",
        type="skill",
        source="skills/bad-skill",
        target_dir="skills/bad-skill",
        templated=True,
    )
    src = tmp_path / "skills" / "bad-skill"
    src.mkdir(parents=True)
    (src / "SKILL.md").write_bytes(b"\xff\xfe not valid utf8")
    monkeypatch.setattr(bp, "_load_manifest", lambda: [plugin])
    monkeypatch.setattr(bp, "_builtin_assets_dir", lambda: tmp_path)
    with pytest.raises(bp.BuiltinPluginError) as excinfo:
        bp.install_builtin("bad-skill")
    assert "SKILL.md" in str(excinfo.value)


def test_install_all_builtins_hint_uses_ignore_scripts(isolated_home):
    """需 npm 的插件落盘后，提示命令必须带 --ignore-scripts（不执行依赖包脚本）。"""
    result = bp.install_all_builtins(include_disabled=True)
    mcp = next(r for r in result["installed"] if r["name"] == "pi-manager-mcp-bridge")
    assert mcp.get("npm_install_required") is True
    assert "--ignore-scripts" in mcp["npm_install_hint"]
    assert mcp["npm_install_args"]


# ---- 已下架内置插件清理 ----


def test_retired_builtins_includes_geonode_rotator():
    """回归守卫：geonode-ip-rotator 必须留在下架清单里。

    它曾在 v1.8.5 随发布静默落盘（按 HTTP 402/429 轮换住宅代理出口 IP 以绕过
    提供商额度/限流强制）。manifest 条目已移除，但用户磁盘上的残留只能靠这份
    下架清单清掉——一旦这里被删，老用户会永远带着该 skill。
    """
    assert "skills/geonode-ip-rotator" in bp._RETIRED_BUILTINS


def test_geonode_rotator_is_not_a_builtin_anymore(isolated_home):
    """回归守卫：该插件不得重新出现在内置清单或内置资产里。"""
    assert all("geonode" not in p.name for p in bp.list_builtins())
    assets = (
        Path(__file__).resolve().parent.parent
        / "assets" / "builtin" / "skills" / "geonode-ip-rotator"
    )
    assert not assets.exists(), f"下架插件资产又出现了: {assets}"


def test_cleanup_retired_builtins_removes_stale_dir(isolated_home):
    stale = core.pi_agent_dir() / "skills" / "geonode-ip-rotator"
    (stale / "scripts").mkdir(parents=True)
    (stale / "SKILL.md").write_text("stale", encoding="utf-8")
    result = bp.cleanup_retired_builtins()
    assert not stale.exists()
    entry = next(r for r in result if r["target_dir"] == "skills/geonode-ip-rotator")
    assert entry["removed"] is True


def test_cleanup_retired_builtins_is_idempotent(isolated_home):
    """残留目录不存在时静默跳过：不报错、不返回条目。"""
    assert bp.cleanup_retired_builtins() == []
    assert bp.cleanup_retired_builtins() == []


def test_install_all_builtins_cleans_retired_dirs(isolated_home):
    """安装入口必须顺带清理下架残留——这是升级路径的实际触发点。"""
    stale = core.pi_agent_dir() / "skills" / "geonode-ip-rotator"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("stale", encoding="utf-8")
    result = bp.install_all_builtins()
    assert result["ok"] is True
    assert not stale.exists()
    assert any(
        r["target_dir"] == "skills/geonode-ip-rotator" and r["removed"] is True
        for r in result["retired_removed"]
    )


def test_cleanup_retired_builtins_refuses_escaping_entry(isolated_home, monkeypatch):
    """下架清单被篡改成越界路径时只记日志跳过，绝不删 agent 目录之外的东西。"""
    victim = core.pi_agent_dir().parent.parent / "pwned"
    victim.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(bp, "_RETIRED_BUILTINS", ("../../pwned",))
    assert bp.cleanup_retired_builtins() == []
    assert victim.is_dir()


def test_cleanup_retired_builtins_reports_rmtree_failure(isolated_home, monkeypatch):
    """清理失败必须非致命：返回 removed=False + error，且不拖垮后续安装。"""
    stale = core.pi_agent_dir() / "skills" / "geonode-ip-rotator"
    stale.mkdir(parents=True)

    def boom(*args, **kwargs):
        raise OSError("device busy")

    monkeypatch.setattr(bp.shutil, "rmtree", boom)
    result = bp.install_all_builtins()
    assert result["ok"] is True
    entry = next(
        r for r in result["retired_removed"]
        if r["target_dir"] == "skills/geonode-ip-rotator"
    )
    assert entry["removed"] is False
    assert "device busy" in entry["error"]


# ---- 状态查询 ----


def test_plugin_status_reports_on_disk_and_ready(isolated_home):
    bp.install_builtin("pi-manager-vision")
    status = bp.plugin_status("pi-manager-vision")
    assert status["on_disk"] is True
    assert status["needs_npm_install"] is False
    assert status["ready"] is True


def test_plugin_status_reports_not_installed(isolated_home):
    status = bp.plugin_status("pi-manager-vision")
    assert status["on_disk"] is False
    assert status["ready"] is False


def test_plugin_status_unknown_raises(isolated_home):
    with pytest.raises(bp.BuiltinPluginError, match="未知"):
        bp.plugin_status("nope")


def test_all_statuses_returns_all(isolated_home):
    statuses = bp.all_statuses()
    assert len(statuses) == len(bp.list_builtins())
    for s in statuses:
        assert "name" in s
        assert "on_disk" in s


# ---- 一键安装 ----


def test_install_one_click_vision_success(isolated_home):
    result = bp.install_one_click("pi-manager-vision")
    assert result["ok"] is True
    assert result["status"]["ready"] is True


def test_install_one_click_unknown_returns_error(isolated_home):
    result = bp.install_one_click("nope")
    assert result["ok"] is False
    assert "error" in result


def test_install_one_click_extension_without_npm_simulated(isolated_home, monkeypatch):
    """MCP 桥需要 npm install；用假 npm 让 install_one_click 走失败路径，
    验证返回的 command 字段供用户手动执行，且带 --ignore-scripts 提示。"""
    bp.install_builtin("pi-manager-mcp-bridge")  # 先落盘

    def fake_npm_install(name):
        return {
            "ok": False,
            "returncode": 127,
            "stdout": "",
            "stderr": "npm not found",
            "command": 'cd "FAKE" && npm install --omit=dev --ignore-scripts --no-audit --no-fund',
            "cwd": "FAKE",
            "args": ["install", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
            "path": "FAKE",
        }

    monkeypatch.setattr(bp, "npm_install", fake_npm_install)
    result = bp.install_one_click("pi-manager-mcp-bridge")
    assert result["ok"] is False
    assert result["command"] == (
        'cd "FAKE" && npm install --omit=dev --ignore-scripts --no-audit --no-fund'
    )
    assert "npm not found" in result["error"]
    # 明确告知用户：npm 使用 --ignore-scripts（不执行依赖包脚本）
    assert "--ignore-scripts" in result["hint"]


# ---- npm_install ----


def test_npm_install_for_plugin_without_npm_returns_skipped(isolated_home):
    result = bp.npm_install("pi-manager-vision")
    assert result["ok"] is True
    assert result.get("skipped") is True


def test_npm_install_unknown_plugin_raises(isolated_home):
    with pytest.raises(bp.BuiltinPluginError, match="未知"):
        bp.npm_install("nope")


def test_npm_install_when_not_on_disk_returns_command(isolated_home):
    # MCP 桥未落盘时，npm_install 应返回带 command 的失败结果
    result = bp.npm_install("pi-manager-mcp-bridge")
    assert result["ok"] is False
    assert "尚未落盘" in result["stderr"]
    assert "npm install --omit=dev" in result["command"]
    assert "--ignore-scripts" in result["command"]
    # 结构化字段：cwd 与 args
    assert result["cwd"]
    assert "install" in result["args"]


def _capture_npm_run(monkeypatch, calls: dict):
    """把 npm 执行替换为假命令并捕获参数。"""

    def fake_run(cmd, **kwargs):
        calls["cmd"] = list(cmd)
        calls["cwd"] = kwargs.get("cwd")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(bp.core, "_npm_command", lambda *args: ["fake-npm", *args])
    monkeypatch.setattr(bp.subprocess, "run", fake_run)


def test_npm_install_uses_ignore_scripts_without_lockfile(isolated_home, monkeypatch):
    """无 package-lock.json 时退回 npm install，且必须带 --ignore-scripts。"""
    bp.install_builtin("pi-manager-mcp-bridge")
    target = core.pi_agent_dir() / "extensions" / "pi-manager-mcp-bridge"
    lockfile = target / "package-lock.json"
    if lockfile.exists():
        lockfile.unlink()  # 模拟旧版本无 lockfile 的场景
    calls: dict = {}
    _capture_npm_run(monkeypatch, calls)
    result = bp.npm_install("pi-manager-mcp-bridge")
    assert result["ok"] is True
    assert calls["cmd"] == [
        "fake-npm",
        "install",
        "--omit=dev",
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
    ]
    assert calls["cwd"] == str(target)
    assert result["args"] == calls["cmd"][1:]
    assert result["cwd"] == str(target)
    assert "npm install --omit=dev --ignore-scripts" in result["command"]


def test_npm_install_uses_npm_ci_with_lockfile(isolated_home, monkeypatch):
    """存在 package-lock.json 时优先 npm ci 固定版本。"""
    bp.install_builtin("pi-manager-mcp-bridge")
    target = core.pi_agent_dir() / "extensions" / "pi-manager-mcp-bridge"
    assert (target / "package-lock.json").is_file()  # 内置资产自带 lockfile，落盘后应存在
    calls: dict = {}
    _capture_npm_run(monkeypatch, calls)
    result = bp.npm_install("pi-manager-mcp-bridge")
    assert result["ok"] is True
    assert calls["cmd"][0] == "fake-npm"
    assert calls["cmd"][1] == "ci"
    assert "--ignore-scripts" in calls["cmd"]
    assert "npm ci --omit=dev" in result["command"]


def test_npm_install_registry_env_restriction(isolated_home, monkeypatch):
    """设置 PI_MANAGER_NPM_REGISTRY 时，参数与提示命令都应带 --registry。"""
    monkeypatch.setenv("PI_MANAGER_NPM_REGISTRY", "https://registry.example.test")
    bp.install_builtin("pi-manager-mcp-bridge")
    calls: dict = {}
    _capture_npm_run(monkeypatch, calls)
    result = bp.npm_install("pi-manager-mcp-bridge")
    assert result["ok"] is True
    assert calls["cmd"][-2:] == ["--registry", "https://registry.example.test"]
    assert "--registry" in result["command"]


def test_npm_install_rejects_outside_target(isolated_home, monkeypatch):
    """纵深防御：npm 在越界 target 上执行前必须拒绝。"""
    plugin = _make_plugin(
        name="evil-ext",
        type="extension",
        needs_npm_install=True,
        target_dir="../../pwned",
    )
    monkeypatch.setattr(bp, "_load_manifest", lambda: [plugin])
    with pytest.raises(bp.BuiltinPluginError, match="目标路径"):
        bp.npm_install(plugin.name)


def test_npm_install_handles_missing_npm(isolated_home, monkeypatch):
    """npm 可执行文件不存在（FileNotFoundError）时返回结构化失败结果。"""
    bp.install_builtin("pi-manager-mcp-bridge")

    def boom(*args, **kwargs):
        raise FileNotFoundError("npm not found")

    monkeypatch.setattr(bp.core, "_npm_command", lambda *args: ["npm", *args])
    monkeypatch.setattr(bp.subprocess, "run", boom)
    result = bp.npm_install("pi-manager-mcp-bridge")
    assert result["ok"] is False
    assert "未找到 npm" in result["stderr"]
    assert result["args"]
    assert result["cwd"]


def test_npm_install_handles_timeout(isolated_home, monkeypatch):
    """npm install 超时（TimeoutExpired）时返回结构化失败结果。"""
    bp.install_builtin("pi-manager-mcp-bridge")

    def boom(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=["npm"], timeout=300)

    monkeypatch.setattr(bp.core, "_npm_command", lambda *args: ["npm", *args])
    monkeypatch.setattr(bp.subprocess, "run", boom)
    result = bp.npm_install("pi-manager-mcp-bridge")
    assert result["ok"] is False
    assert "超时" in result["stderr"]
    assert result["args"]
    assert result["cwd"]
