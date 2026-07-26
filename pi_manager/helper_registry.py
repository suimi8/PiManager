"""Publish the local Pi Manager helper command for editor integrations."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import core, storage


REGISTRY_NAME = "pi-manager-helper.json"


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
    return payload


def register_current_helper_best_effort() -> None:
    try:
        register_current_helper()
    except (OSError, ValueError):
        pass
