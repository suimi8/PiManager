"""Publish the local Pi Manager helper command for editor integrations."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import core, platform_util, storage


REGISTRY_NAME = "pi-manager-helper.json"

# ~/.pi/agent/ 下必须 owner-only 的敏感文件。
# 为什么需要显式清单：`storage.save_json(private=True)` 只做 chmod 0600，在 Windows
# 上不产生任何访问控制效果，这些文件的权限 100% 来自父目录的继承 ACE。目录加固带
# (OI)(CI) 只对**此后新建**的文件生效，已存在的文件必须逐个补。
_SENSITIVE_NAMES = (
    REGISTRY_NAME,          # command 字段是编辑器扩展将要执行的可执行文件路径
    ".broker-token",        # 配置变更授权凭据
    "secrets.vault",        # AES-GCM 密钥库（keyring 不可用时的回退存储）
    ".vault_master_key",    # vault 主密钥派生盐
    "secrets.index.json",   # 密钥条目索引
    "secrets.dpapi",        # 历史 DPAPI 密钥库（迁移期仍可能存在）
    "auth.json",            # Pi CLI 认证信息
)


def registry_path() -> Path:
    return core.pi_agent_dir() / REGISTRY_NAME


def current_helper_command() -> list[str]:
    executable = str(Path(sys.executable).resolve())
    if bool(getattr(sys, "frozen", False)):
        return [executable]
    return [executable, str(Path(__file__).resolve().parents[1] / "main.py")]


def register_current_helper() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": 1,
        "command": current_helper_command(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }
    # The registry names an executable the editor extension will run; keep it
    # owner-only so other local accounts cannot repoint it.
    storage.save_json(registry_path(), payload, private=True)
    # save_json(private=True) 只做 chmod 0600 —— Windows 上是空操作。不补 ACL 的话
    # 同机其他账户可以改写 command 字段，让编辑器扩展以受害者身份执行攻击者的程序。
    harden_agent_dir_best_effort()
    return payload


def harden_agent_dir_best_effort() -> None:
    """把 ~/.pi/agent 目录与其中的敏感文件收紧为「仅当前用户」。

    放在这里而不是各写入点：Windows ACL 加固需要在文件已存在之后执行，而本模块的
    register_current_helper() 是启动路径上唯一由应用自己拥有、必然执行一次的钩子
    （main.py 启动 GUI 前调用）。加固是幂等的，重复调用无副作用。
    POSIX 上退化为 chmod 0700/0600，语义与原有 private=True 一致。
    """
    agent_dir = core.pi_agent_dir()
    try:
        agent_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    # 目录先加固：Windows 上带 (OI)(CI)，此后新建的文件自动继承 owner-only。
    platform_util.harden_private_path(agent_dir)
    for name in _SENSITIVE_NAMES:
        target = agent_dir / name
        # 只处理已存在的普通文件；重解析点一律跳过（不替攻击者去改别处的 ACL）。
        if not target.exists() or platform_util.is_reparse_point(target):
            continue
        platform_util.harden_private_path(target)


def register_current_helper_best_effort() -> None:
    try:
        register_current_helper()
    except (OSError, ValueError):
        pass
