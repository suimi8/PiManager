"""Small non-GUI helper used by the Cursor extension to obtain provider env."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .core import ProviderKeyError, provider_runtime_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print a Pi Manager provider environment as JSON")
    parser.add_argument("--json", action="store_true", help="emit JSON (kept for explicit callers)")
    parser.add_argument("--output", help="write JSON to an existing private file")
    parser.add_argument("provider")
    args = parser.parse_args(argv)
    try:
        env = provider_runtime_env(args.provider)
    except ProviderKeyError as exc:
        _emit({"ok": False, "error": str(exc)}, args.output)
        return 2
    _emit({"ok": True, "env": env}, args.output)
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
