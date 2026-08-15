# -*- coding: utf-8 -*-
"""pi-sensitive-guard 内置扩展的静态一致性测试。

守卫是 TypeScript（无 node 测试框架），本文件对源码做静态断言，
确保防护名单与 pi_manager/secrets.py 中的真实敏感文件名保持一致，
并锁定路径判断的大小写不敏感与 ~\\ 展开修复。

风格参照 tests/test_spec_integrity.py：只读源码，不执行插件。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GUARD_SRC = (
    REPO_ROOT / "assets" / "builtin" / "extensions" / "pi-sensitive-guard" / "index.ts"
)


def _guard_text() -> str:
    return GUARD_SRC.read_text(encoding="utf-8")


def _agent_sensitive_files_block() -> str:
    text = _guard_text()
    match = re.search(r"const AGENT_SENSITIVE_FILES = \[(.*?)\];", text, flags=re.S)
    assert match, "未找到 AGENT_SENSITIVE_FILES 声明"
    return match.group(1)


def test_guard_covers_real_secret_store_filenames() -> None:
    """黑名单必须覆盖 secrets.py 中实际落盘的敏感文件名。"""
    block = _agent_sensitive_files_block()
    for name in (
        ".vault_master_key",  # secrets.py:_master_key_path()
        ".broker-token",  # secrets.py:broker_token_path()
        "secrets.index.json",  # secrets.py:_index_path()
        "secrets.dpapi",  # secrets.py:_legacy_vault_path()
        "secrets.vault",  # secrets.py:_vault_path()
        "auth.json",
        "mcp-servers.json",
    ):
        assert f'"{name}"' in block, f"守卫黑名单缺少真实敏感文件: {name}"


def test_guard_drops_stale_placeholder_names() -> None:
    """误写的 .master_key / keyring*.json 占位名不得残留在黑名单里。"""
    block = _agent_sensitive_files_block()
    assert '".master_key"' not in block
    assert '"keyring"' not in block
    assert '"keyring.json"' not in block


def test_guard_path_checks_are_case_insensitive() -> None:
    """Windows 大小写绕过：路径判断处必须统一小写后比较。"""
    text = _guard_text()
    assert ".toLowerCase()" in text, "守卫缺少大小写归一化"
    assert "AGENT_DIR.toLowerCase()" in text
    # 三个路径判断函数都应基于小写比较
    for func in ("isAgentSensitive", "isAgentConfig", "isProjectSensitive"):
        match = re.search(
            rf"function {func}\(path: string\): boolean \{{(.*?)\n\}}", text, flags=re.S
        )
        assert match, f"未找到函数 {func}"
        assert ".toLowerCase()" in match.group(0), f"{func} 未做小写归一化"


def test_guard_expands_windows_style_home() -> None:
    r"""normalizePath 必须同时展开 ~\ 与 ~/。"""
    text = _guard_text()
    assert r'path.startsWith("~\\")' in text
    assert 'path.startsWith("~/")' in text


def test_guard_redaction_covers_github_pat_and_huggingface() -> None:
    """输出侧抹除须覆盖 fine-grained PAT 与 HuggingFace token。"""
    text = _guard_text()
    assert r"github_pat_[A-Za-z0-9_]{22,}" in text
    assert r"hf_[A-Za-z0-9]{20,}" in text


def test_guard_remains_zero_dependency() -> None:
    """保持纯防御零依赖约束：只允许 node: 内置与 ExtensionAPI 类型导入。"""
    text = _guard_text()
    for match in re.finditer(r'^import .*? from "(.+?)";', text, flags=re.M):
        source = match.group(1)
        assert source.startswith("node:") or source.startswith(
            "@earendil-works/"
        ), f"守卫引入了未授权依赖: {source}"
