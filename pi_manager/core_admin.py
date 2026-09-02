"""Provider 查询、孤儿密钥、配置备份与密钥池包装。"""
from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any

from . import storage

logger = logging.getLogger(__name__)


def _core():
    from . import core

    return core



# ==== HTTP 工具：URL 规范化 / SSL 上下文 / 端点脱敏 / 友好错误 ====
# 已抽到 pi_manager/core_http.py，此处通过顶部 import 重新导出，保持 core.xxx 兼容。


# vision 子系统（智谱识图管道）已抽到 pi_manager/core_vision.py，
# 顶部重新导出保持 core.xxx 兼容。_effective_proxy_url 留在 core（被
# fetch_remote_models / _http_json_request 共用）。


def _effective_proxy_url(explicit: str = "") -> str:
    """Resolve the proxy for an outgoing request (explicit > config > env).

    Invalid (non-http(s) scheme or missing host) values are dropped with a
    warning instead of being handed to urllib.
    """
    candidates: list[str] = []
    explicit = (explicit or "").strip()
    if explicit:
        candidates.append(explicit)
    try:
        cfg = _core().load_manager_config()
        if not explicit and cfg.get("proxy_enabled") and cfg.get("proxy_url"):
            candidates.append(str(cfg.get("proxy_url") or "").strip())
    except Exception:
        pass
    if not candidates:
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = (os.environ.get(var) or "").strip()
            if value:
                candidates.append(value)
                break
    for value in candidates:
        error = _core().validate_proxy_url(value)
        if error:
            # 代理 URL 可含 user:pass@，不能整串进日志（审查 P2-6）
            logger.warning("忽略无效代理地址「%s」: %s", _core().redact_proxy_url(value), error)
            continue
        return value
    return ""




# ==== Provider 配置查询 / 密钥池管理 / 配置备份 ====


def get_provider_config(provider: str) -> dict[str, Any] | None:
    """Return custom provider entry from models.json, if any."""
    if not provider:
        return None
    cfg = _core().load_models_config()
    providers = cfg.get("providers") or {}
    entry = providers.get(provider)
    return entry if isinstance(entry, dict) else None



def list_orphaned_provider_keys() -> list[dict[str, Any]]:
    """Return key pools stored in the secret store with no matching provider config.

    A provider deleted outside this app (or by an older version) leaves its
    key pool behind; this surfaces those leftovers so they can be cleaned.
    """
    from . import secrets as secretstore

    cfg = _core().load_models_config()
    providers = cfg.get("providers") or {}
    orphaned: list[dict[str, Any]] = []
    for provider, _pool_name, _single_name in secretstore.provider_pool_names():
        if provider in providers:
            continue
        keys = secretstore.list_provider_keys(provider)
        orphaned.append(
            {
                "provider": provider,
                "key_count": len(keys),
                "statuses": sorted({str(k.get("status") or "") for k in keys}),
                "masked": [str(k.get("masked") or "") for k in keys][:3],
            }
        )
    return orphaned



def delete_orphaned_provider_keys() -> int:
    """Delete key pools whose provider no longer exists in models.json."""
    from . import secrets as secretstore

    cfg = _core().load_models_config()
    providers = cfg.get("providers") or {}
    deleted = 0
    for provider, _pool_name, _single_name in secretstore.provider_pool_names():
        if provider in providers:
            continue
        try:
            secretstore.delete_provider_api_keys(provider)
            deleted += 1
        except Exception:
            pass
    return deleted



_BACKUP_TARGETS = frozenset(
    {
        "settings.json",
        "models.json",
        "pi-manager.json",
        "pi-manager-test-history.json",
        "pi-manager-health.json",
        "auth.json",
    }
)



def list_config_backups() -> list[dict[str, str]]:
    """List recoverable ``.bak.*`` config backups inside the agent directory."""
    from datetime import datetime

    rows: list[dict[str, str]] = []
    root = _core().pi_agent_dir()
    if not root.exists():
        return rows
    for path in sorted(root.glob("*.bak.*")):
        if not path.is_file():
            continue
        name = path.name
        target_name = ""
        for target in _BACKUP_TARGETS:
            if name.startswith(target + ".bak."):
                target_name = target
                break
        if not target_name:
            continue
        try:
            st = path.stat()
            mtime_s = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            st = None
            mtime_s = ""
        rows.append(
            {
                "path": str(path),
                "name": name,
                "target": target_name,
                "mtime": mtime_s,
                "size": str(st.st_size) if st is not None else "",
            }
        )
    return rows



def restore_config_backup(backup_path: str | Path) -> dict[str, Any]:
    """Restore a ``.bak.*`` backup back to its target config file (atomic).

    The backup must live in the agent directory and map to a known JSON config
    target, so no path traversal or arbitrary overwrite is possible.
    """
    src = Path(backup_path).resolve()
    root = _core().pi_agent_dir().resolve()
    if src.parent != root:
        return {"ok": False, "error": "备份文件必须在配置目录内"}
    name = src.name
    target_name = ""
    for target in _BACKUP_TARGETS:
        if name.startswith(target + ".bak."):
            target_name = target
            break
    if not target_name:
        return {"ok": False, "error": "不是可恢复的配置备份"}
    try:
        data = _core().load_json(src, None)
    except Exception as exc:
        return {"ok": False, "error": f"备份内容无法解析：{exc}"}
    target_path = root / target_name
    try:
        _core().ensure_agent_dir()
        # allow_corrupt_overwrite 是这条恢复路径存在的**唯一**理由：
        # storage 的「拒绝覆盖无法读取的配置文件」守卫本意是防误覆盖（对的，
        # 别删），但它同时把唯一的修复入口也堵死了 —— 目标文件损坏时恢复必然
        # 失败，而应用内没有「删除损坏文件」的入口，用户只能离开应用手工删文件。
        # 绕过时 storage 会把损坏内容隔离成 <name>.corrupt.<ts> 并跳过备份轮转
        # （否则连续两次恢复会把仅存的可用备份挤掉）。
        storage.save_json(
            target_path,
            data,
            private=target_path == _core().manager_config_path(),
            allow_corrupt_overwrite=True,
        )
    except Exception as exc:
        return {"ok": False, "error": f"恢复失败：{exc}"}
    finally:
        _core()._invalidate_config_cache(target_path)
    return {"ok": True, "target": target_name, "backup": name}



def list_provider_api_keys(provider: str, *, reveal: bool = False) -> list[dict[str, Any]]:
    """列出 provider 密钥池；``reveal=True`` 时附带明文（仅限 GUI 显示请求）。"""
    from . import secrets as secretstore

    return secretstore.list_provider_keys(provider, reveal=reveal)



def add_provider_api_key(provider: str, value: str) -> dict[str, Any]:
    from . import secrets as secretstore

    result = secretstore.add_provider_api_key(provider, value)
    reference = secretstore.provider_api_key_reference(provider)

    def _apply(cfg: dict[str, Any]) -> Any:
        entry = (cfg.get("providers") or {}).get(provider)
        if not isinstance(entry, dict) or entry.get("apiKey") == reference:
            return storage.UNCHANGED
        entry["apiKey"] = reference
        return cfg

    _core().update_models_config(_apply)
    return result



def remove_provider_api_key(provider: str, key_id: str) -> bool:
    from . import secrets as secretstore

    return secretstore.remove_provider_api_key(provider, key_id)



def restore_provider_api_key(provider: str, key_id: str) -> bool:
    from . import secrets as secretstore

    return secretstore.restore_provider_key(provider, key_id)



def restore_all_provider_api_keys(provider: str) -> int:
    from . import secrets as secretstore

    return secretstore.restore_all_provider_keys(provider)
