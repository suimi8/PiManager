# -*- coding: utf-8 -*-
"""MCP 桥扩展环境白名单的静态断言（只读源码，不执行 TS）。

安全边界：pi-manager-mcp-bridge spawn 第三方 MCP server 时不得直传
`process.env`，否则 PiManager 注入 pi 的 provider API Key 会随子进程
环境扩散到任意 MCP server（凭据越权）。本测试以正则/文本级断言锁定：
- spawn 的 env 不存在 `{ ...process.env, ... }` 直传；
- 存在显式白名单集合，且只叠加 `cfg.env` 用户显式声明；
- 白名单不含任何疑似密钥变量名；
- 文件头注释与配套文档（manifest / help_docs）已声明该安全边界。
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
BRIDGE_TS = (
    REPO_ROOT
    / "assets"
    / "builtin"
    / "extensions"
    / "pi-manager-mcp-bridge"
    / "index.ts"
)
MANIFEST = REPO_ROOT / "assets" / "builtin" / "manifest.json"
HELP_DOCS = REPO_ROOT / "pi_manager" / "help_docs.py"


def _bridge_source() -> str:
    return BRIDGE_TS.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """粗略剔除块注释与行注释，避免注释里的示例干扰代码断言。"""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    text = re.sub(r"(?m)^(\s*)//.*$", r"\1", text)
    return text


def test_spawn_does_not_spread_process_env() -> None:
    code = _strip_comments(_bridge_source())
    assert "...process.env" not in code, (
        "MCP 桥 spawn 子进程时不得直传 process.env（凭据扩散到第三方 server）"
    )


def test_spawn_env_uses_whitelist_plus_cfg_env() -> None:
    code = _strip_comments(_bridge_source())
    assert re.search(
        r"env:\s*\{\s*\.\.\.buildSafeEnv\(\),\s*\.\.\.\(cfg\.env\s*\?\?\s*\{\}\)\s*\}",
        code,
    ), "spawn env 应为白名单基础环境 + cfg.env 显式声明的叠加"


def test_whitelist_constant_exists_and_lists_safe_vars() -> None:
    src = _bridge_source()
    match = re.search(
        r"SAFE_ENV_WHITELIST\s*=\s*\[(.*?)\]\s*as const", src, flags=re.S
    )
    assert match, "index.ts 中应定义 SAFE_ENV_WHITELIST 白名单集合"
    entries = set(re.findall(r'"([A-Za-z_][A-Za-z0-9_]*)"', match.group(1)))
    # 进程启动所必需的最小集合必须在白名单中
    for required in ("PATH", "HOME", "USERPROFILE", "TEMP", "SystemRoot"):
        assert required in entries, f"白名单缺少必需变量 {required}"
    # 白名单不得包含任何疑似密钥 / provider 凭据变量
    suspicious = [
        name
        for name in entries
        if re.search(r"KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PI_MANAGER", name, re.I)
    ]
    assert not suspicious, f"白名单混入疑似凭据变量: {suspicious}"


def test_header_comment_documents_credential_boundary() -> None:
    src = _bridge_source()
    header = src[: src.index("*/")] if "*/" in src else src[:2000]
    assert "不应继承宿主" in header and "凭据扩散" in header, (
        "文件头注释应声明：MCP server 不应继承宿主全部环境，避免凭据扩散"
    )


def test_manifest_and_help_docs_disclose_explicit_env_rule() -> None:
    manifest = MANIFEST.read_text(encoding="utf-8")
    help_docs = HELP_DOCS.read_text(encoding="utf-8")
    for label, text in (("manifest.json", manifest), ("help_docs.py", help_docs)):
        assert "env" in text and "显式" in text, (
            f"{label} 应告知用户：给 MCP server 传密钥须在该 server 的 env 中显式声明"
        )
        assert "不" in text and ("继承" in text or "自动" in text), (
            f"{label} 应告知用户：provider 密钥不再自动继承给 MCP server"
        )
