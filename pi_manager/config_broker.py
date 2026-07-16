"""Whitelisted configuration mutations for desktop and Cursor clients."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import core, storage

_ALLOWED_MANAGER_FIELDS = frozenset(
    {
        "failover_fail_counts",
        "favorites",
        "failover_enabled",
        "failover_fail_threshold",
        "failover_silent",
    }
)


def _revision_path() -> Path:
    return core.pi_agent_dir() / ".config-revisions.json"


def _broker_lock_path() -> Path:
    return core.pi_agent_dir() / ".config-broker.mutation"


def _sha256(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _record_revision(path: Path) -> int:
    def update(current: Any) -> dict[str, Any]:
        state = current if isinstance(current, dict) else {}
        entry = state.get(path.name) if isinstance(state.get(path.name), dict) else {}
        revision = int(entry.get("revision") or 0) + 1
        state[path.name] = {"revision": revision, "sha256": _sha256(path)}
        return state

    state = storage.update_json(_revision_path(), {}, update)
    return int(state[path.name]["revision"])


def mutate(request: dict[str, Any]) -> dict[str, Any]:
    request_id = str(request.get("request_id") or "")
    if int(request.get("schema_version") or 0) != 1:
        return {"ok": False, "request_id": request_id, "error": "unsupported_schema"}
    operation = str(request.get("operation") or "")
    arguments = request.get("arguments")
    if not isinstance(arguments, dict):
        return {"ok": False, "request_id": request_id, "error": "invalid_arguments"}

    try:
        if operation == "set_default_model":
            provider = str(arguments.get("provider") or "").strip()
            model = str(arguments.get("model") or "").strip()
            if not provider or not model:
                raise ValueError("provider and model are required")
            thinking = str(arguments.get("thinking") or "").strip()
            sync_enabled = bool(arguments.get("sync_enabled", True))
            favorites = [str(item) for item in arguments.get("favorites", []) if isinstance(item, str)]

            def update_settings(current: Any) -> dict[str, Any]:
                if not isinstance(current, dict):
                    raise ValueError("settings.json 顶层必须是对象")
                result = dict(current)
                result["defaultProvider"] = provider
                result["defaultModel"] = model
                if thinking:
                    result["defaultThinkingLevel"] = thinking
                if sync_enabled:
                    enabled = [str(item) for item in result.get("enabledModels", []) if isinstance(item, str)]
                    result["enabledModels"] = list(dict.fromkeys([*enabled, *favorites, f"{provider}/{model}"]))
                return result

            with storage.locked(_broker_lock_path()):
                storage.update_json(core.settings_path(), {}, update_settings)
                revision = _record_revision(core.settings_path())
            return {
                "ok": True,
                "request_id": request_id,
                "revision": revision,
                "result": {"provider": provider, "model": model},
            }

        if operation == "set_manager_fields":
            fields = arguments.get("fields")
            if not isinstance(fields, dict) or any(key not in _ALLOWED_MANAGER_FIELDS for key in fields):
                raise ValueError("manager mutation contains non-whitelisted fields")

            def update_manager(current: Any) -> dict[str, Any]:
                if not isinstance(current, dict):
                    raise ValueError("pi-manager.json 顶层必须是对象")
                return {**current, **fields}

            with storage.locked(_broker_lock_path()):
                storage.update_json(core.manager_config_path(), {}, update_manager)
                revision = _record_revision(core.manager_config_path())
            return {"ok": True, "request_id": request_id, "revision": revision, "result": {}}

        return {"ok": False, "request_id": request_id, "error": "operation_not_allowed"}
    except Exception as exc:
        return {"ok": False, "request_id": request_id, "error": str(exc)}


def mutate_file(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    if not source.exists() or not source.is_file() or source.stat().st_size > 64 * 1024:
        return {"ok": False, "error": "invalid_request_file"}
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"invalid_request: {exc}"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "request_must_be_object"}
    return mutate(payload)
