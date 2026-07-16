"""Small non-GUI helper used by the Cursor extension to obtain provider env."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import secrets as secretstore
from .core import (
    ProviderKeyError,
    classify_provider_key_failure,
    provider_runtime_credential,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a Pi Manager provider environment as JSON")
    parser.add_argument("--json", action="store_true", help="emit JSON (kept for explicit callers)")
    parser.add_argument("--output", help="write JSON to an existing private file")
    parser.add_argument("--mark-failed", action="store_true", help="mark one managed key invalid")
    parser.add_argument("--key-id", default="", help="non-sensitive managed key identifier")
    parser.add_argument("--reason", default="", help="short non-sensitive failure reason")
    parser.add_argument("provider")
    args = parser.parse_args(argv)
    if args.mark_failed:
        if not args.key_id:
            _emit({"ok": False, "error": "--key-id is required"}, args.output)
            return 2
        classification = classify_provider_key_failure(1, "", args.reason)
        if not classification.get("status"):
            _emit(
                {
                    "ok": True,
                    "marked": False,
                    "status": "",
                    "has_available": True,
                },
                args.output,
            )
            return 0
        changed = secretstore.mark_provider_key_failed(
            args.provider, args.key_id, args.reason
        )
        if not changed:
            _emit({"ok": False, "error": "API Key 不存在或已被删除"}, args.output)
            return 2
        next_credential = secretstore.get_active_provider_credential(args.provider)
        _emit(
            {
                "ok": True,
                "marked": True,
                "status": classification["status"],
                "failure_kind": classification["failure_kind"],
                "retry_at": classification["retry_at"],
                "has_available": bool(next_credential),
            },
            args.output,
        )
        return 0
    try:
        credential = provider_runtime_credential(args.provider)
    except ProviderKeyError as exc:
        _emit({"ok": False, "error": str(exc)}, args.output)
        return 2
    _emit(
        {
            "ok": True,
            "env": credential["env"],
            "key_id": credential.get("key_id", ""),
        },
        args.output,
    )
    return 0


def _emit(payload: dict[str, object], output: str | None) -> None:
    text = json.dumps(payload, ensure_ascii=False)
    if not output:
        print(text)
        return
    path = Path(output)
    if not path.exists() or not path.is_file():
        raise ValueError("helper output file must already exist")
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
