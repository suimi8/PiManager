# -*- coding: utf-8 -*-
"""开发规范一致性测试：把 docs/DEVELOPMENT_STANDARDS.md 的可脚本化红线
变成 pytest 断言（对应规范第 1 节 R1-R9 与第 8 节自动化审查项）。

设计原则：只读源码与文档做静态断言，不启动 GUI、不触碰真实配置目录。
"""
from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "pi_manager"


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _module_names() -> list[str]:
    return sorted(
        p.relative_to(REPO_ROOT).as_posix().replace("\\", "/")
        for p in PACKAGE_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    )


# ---- R2: 用户配置目录固定 ~/.pi/agent ----


def test_config_dir_is_fixed_agent_path() -> None:
    """core 的配置目录解析必须指向 ~/.pi/agent（Windows 为 %USERPROFILE%\\.pi\\agent）。"""
    from pi_manager import core

    path = core.pi_agent_dir()
    assert path.name == "agent"
    assert path.parent.name == ".pi"
    # 必须是用户主目录下，而不是项目目录或父目录
    home = Path.home().resolve()
    assert path.resolve().is_relative_to(home), f"配置目录越界: {path}"


# ---- R4: 轻量 CLI 入口不得导入 PySide6 ----


_LIGHT_CLI_ENTRIES = (
    "--print-provider-env",
    "--vision-describe",
    "--config-mutate",
)


def _assert_no_pyside6_import(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    # 只检查模块顶层 import（函数体内的条件 import 不在模块加载路径上）。
    for node in tree.body:
        if isinstance(node, ast.Import):
            if any(alias.name == "PySide6" or alias.name.startswith("PySide6.")
                   for alias in node.names):
                pytest.fail(f"{path.relative_to(REPO_ROOT)} 顶层导入 PySide6")
        elif isinstance(node, ast.ImportFrom):
            if node.module and (node.module == "PySide6" or node.module.startswith("PySide6.")):
                pytest.fail(f"{path.relative_to(REPO_ROOT)} 顶层导入 {node.module}")


def test_light_cli_entries_do_not_import_pyside6() -> None:
    """轻量 CLI 入口（Cursor 扩展热路径）不得 import PySide6。"""
    for flag in _LIGHT_CLI_ENTRIES:
        # main.py 中每个轻量入口的 handler 所在模块不应导入 PySide6
        assert flag in _read("main.py"), f"main.py 缺少轻量 CLI 入口 {flag}"
    _assert_no_pyside6_import(REPO_ROOT / "main.py")
    # provider_env 是 --print-provider-env 的实现模块
    provider_env = PACKAGE_ROOT / "provider_env.py"
    if provider_env.exists():
        _assert_no_pyside6_import(provider_env)


def test_core_modules_do_not_import_pyside6() -> None:
    """core 层（非 presentation）不应 import PySide6，保证无 GUI 依赖。"""
    for rel in _module_names():
        if "presentation" in rel or rel.endswith("ui.py") or rel.endswith("ui_features.py"):
            continue
        path = PACKAGE_ROOT / Path(*rel.split("/")[1:])
        _assert_no_pyside6_import(path)


# ---- R5: 版本单一来源 ----


def _app_version() -> str:
    match = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', _read("pi_manager/extras.py"))
    assert match, "extras.py 缺少 APP_VERSION"
    return match.group(1)


def test_docs_top_version_matches_app_version() -> None:
    """发布说明 / 使用教程顶部版本必须与 extras.py 一致（R5）。"""
    app = _app_version()
    for rel in ("docs/发布说明.md", "docs/使用教程.md"):
        head = "\n".join(_read(rel).splitlines()[:12])
        match = re.search(r"v?(\d+\.\d+\.\d+)", head)
        assert match, f"{rel} 前 12 行未找到版本号"
        assert match.group(1) == app, f"{rel} 顶部版本 {match.group(1)} != APP_VERSION {app}"


def test_check_versions_script_passes() -> None:
    """版本一致性脚本自身可执行且通过（R5 的 CI 强制者）。"""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_versions.py")],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- R7: 发布产物不入库 ----


def test_release_artifacts_not_tracked() -> None:
    """release-assets/ 与 dist/ 不得出现在 git 跟踪文件中（R7）。"""
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("非 git 仓库")
    tracked = proc.stdout.splitlines()
    for rel in tracked:
        assert not rel.startswith("release-assets/"), f"release-assets 被跟踪: {rel}"
        assert not rel.startswith("dist/"), f"dist 被跟踪: {rel}"


def test_secrets_vault_not_tracked() -> None:
    """secrets.vault / auth.json 不得被 git 跟踪（R1）。"""
    try:
        proc = subprocess.run(
            ["git", "ls-files"],
            capture_output=True,
            text=True,
            cwd=str(REPO_ROOT),
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("非 git 仓库")
    tracked = proc.stdout.splitlines()
    for rel in tracked:
        name = Path(rel).name.lower()
        assert name not in {"secrets.vault", "auth.json"}, f"敏感文件被跟踪: {rel}"


# ---- R1: 密钥扫描脚本可用 ----


def test_check_secrets_script_passes() -> None:
    """密钥扫描脚本（默认范围）必须通过（R1 的 CI 强制者）。"""
    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "check_secrets.py")],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---- AGENTS.md 不变量守卫：被引用的测试/脚本必须存在 ----


def test_agents_invariants_have_corresponding_tests() -> None:
    """AGENTS.md 列出的检测不变量必须有对应测试或脚本存在（防规范空转）。"""
    required = {
        "tests": "tests/test_plugin_security_matrix.py",
        "self_check": "main.py",
        "smoke": "scripts/smoke_test_dist.py",
        "keyring": "tests/test_keyring_priority.py",
    }
    for label, rel in required.items():
        assert (REPO_ROOT / rel).exists(), f"AGENTS.md 不变量 {label} 缺少对应文件 {rel}"
    # 密钥扫描与版本检查脚本存在且可执行
    assert (REPO_ROOT / "scripts" / "check_secrets.py").exists()
    assert (REPO_ROOT / "scripts" / "check_versions.py").exists()


def test_standards_doc_references_are_present() -> None:
    """统一规范文档存在，且被 CONTRIBUTING.md / AGENTS.md 引用（G5 闭环）。"""
    assert (REPO_ROOT / "docs" / "DEVELOPMENT_STANDARDS.md").is_file()
    contributing = _read("CONTRIBUTING.md")
    assert "DEVELOPMENT_STANDARDS.md" in contributing
    agents = _read("AGENTS.md")
    assert "DEVELOPMENT_STANDARDS.md" in agents
