# -*- coding: utf-8 -*-
"""明文密钥备份擦除与存量密钥收口。

从 ``extras.py`` 下沉。``pi_manager.extras`` 继续 re-export，保持现有导入与
monkeypatch 点（``extras.xxx``）稳定。对会被测试 patch 的符号走 ``_extras().xxx``。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from . import core
from . import secrets as secretstore


def _extras():
    from . import extras

    return extras


def _shred_file(path: Path) -> bool:
    """先覆盖写零再删除：备份文件里的明文密钥只 unlink 仍可被恢复。"""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    try:
        with open(path, "r+b") as handle:
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        # 覆盖失败（占用 / 权限）也要尽力删除，删不掉再报告失败。
        pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _models_json_holds_plaintext_secret(raw: bytes) -> bool:
    """True 当这份 models.json 快照里还有明文 apiKey / 敏感 Header。

    引用（`$`）、命令（`!`）与 `__DPAPI__:` 历史标记都不是密钥本体。
    """
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return False
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return False
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("apiKey") or "").strip()
        if key and not key.startswith(("$", "!", "__DPAPI__:")):
            return True
        headers = entry.get("headers")
        if not isinstance(headers, dict):
            continue
        for name, value in headers.items():
            raw_value = str(value or "").strip()
            if (
                raw_value
                and not raw_value.startswith(("$", "!"))
                and secretstore.is_sensitive_header_name(str(name))
            ):
                return True
    return False


def purge_plaintext_key_backups() -> list[str]:
    """擦除 `models.json` 的备份 / 残留临时文件中仍含明文密钥的副本。

    `storage._write_unlocked` 每次写 JSON 都把旧内容轮转进 `<name>.bak.1`
    （并把上一份挤到 `.bak.2`），因此「把明文 Key 安全迁移成引用」这一步反而
    会把迁移前的明文完整复制进备份并永久保留（R2 审计 P1-3，已实证）；
    `os.replace` 失败时还会留下 `.models.json.<pid>...tmp` 全量副本（P3-8）。
    这里在迁移 / 导入这两个「配置刚变更」的时点做自愈：只擦除**确实含明文
    密钥**的副本，正常备份保留，不影响回滚能力。返回被擦除的文件名列表。
    """
    purged: list[str] = []
    try:
        models = core.models_path()
        agent_dir = models.parent
        candidates = list(agent_dir.glob(f"{models.name}.bak.*")) + list(
            agent_dir.glob(f".{models.name}.*.tmp")
        )
    except OSError:
        return purged
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            raw = candidate.read_bytes()
        except OSError:
            continue
        if not _models_json_holds_plaintext_secret(raw):
            continue
        if _shred_file(candidate):
            purged.append(candidate.name)
    return purged


def secure_existing_keys() -> dict[str, Any]:
    """Migrate plaintext provider keys into the platform secret store."""
    cfg = core.load_models_config()
    providers = cfg.get("providers") or {}
    if not isinstance(providers, dict):
        return {"ok": False, "count": 0}
    count = 0

    def _apply_models(current: dict[str, Any]) -> dict[str, Any]:
        nonlocal count
        raw = current.get("providers") or {}
        if not isinstance(raw, dict):
            return current
        new_providers = secretstore.migrate_plaintext_keys(raw)
        current["providers"] = new_providers
        count = len(new_providers)
        return current

    core.update_models_config(_apply_models)

    def _apply_mgr(mgr: dict[str, Any]) -> dict[str, Any]:
        mgr["secure_keys"] = True
        return mgr

    core.update_manager_config(_apply_mgr)
    # 迁移刚刚把明文原文轮转进 models.json.bak.1：不擦除的话「安全迁移」等于
    # 把明文永久留在同目录下（P1-3）。
    purged = _extras().purge_plaintext_key_backups()
    return {
        "ok": True,
        "count": count,
        "secrets": secretstore.list_secret_names(),
        "purged_backups": purged,
    }


def resolve_api_key_for_provider(provider: str, api_key_field: str = "") -> str:
    raw = api_key_field
    if not raw:
        entry = core.get_provider_config(provider) or {}
        raw = str(entry.get("apiKey") or "")
    resolved = secretstore.resolve_provider_api_key(raw, provider)
    return core.resolve_api_key_value(resolved)
