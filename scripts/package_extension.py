"""Package the Cursor extension into the repository release-assets directory.

Usage:
  python scripts/package_extension.py
  python scripts/package_extension.py --out release-assets --vsce vsce

The default output is always relative to the repository root, regardless of
the current working directory.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = REPO_ROOT / "extensions" / "pi-cursor"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "release-assets"


def resolve_repo_path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def find_command(name: str, candidates: tuple[str, ...]) -> str:
    for candidate in (name, *candidates):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    raise FileNotFoundError(name)


def vsce_command(name: str) -> tuple[list[str], None]:
    if name != "vsce":
        return [find_command(name, (f"{name}.cmd",))], None
    local = EXTENSION_DIR / "node_modules" / ".bin" / (
        "vsce.cmd" if sys.platform == "win32" else "vsce"
    )
    if not local.is_file():
        raise FileNotFoundError(
            f"Pinned VSCE is missing: {local}. Run npm ci in {EXTENSION_DIR}."
        )
    return [str(local)], None


def extension_version() -> str:
    package_path = EXTENSION_DIR / "package.json"
    try:
        package = json.loads(package_path.read_text(encoding="utf-8"))
        version = str(package["version"]).strip()
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Unable to read extension version from {package_path}: {exc}") from exc
    if not version:
        raise RuntimeError(f"Extension version is empty in {package_path}")
    return version


def run_tests(npm_command: str) -> None:
    subprocess.run([npm_command, "test"], cwd=EXTENSION_DIR, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUTPUT_DIR),
        help="VSIX output directory; relative paths are resolved from the repository root",
    )
    parser.add_argument(
        "--vsce",
        default="vsce",
        help="vsce executable or command name (default: pinned local node_modules version)",
    )
    parser.add_argument(
        "--skip-tests",
        action="store_true",
        help="skip npm test before packaging",
    )
    args = parser.parse_args()

    if not EXTENSION_DIR.is_dir():
        print(f"Extension directory not found: {EXTENSION_DIR}", file=sys.stderr)
        return 1

    try:
        vsce, _unused = vsce_command(args.vsce)
        npm = find_command("npm", ("npm.cmd",))
        version = extension_version()
    except (FileNotFoundError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        if isinstance(exc, FileNotFoundError):
            print("Install dependencies with npm ci before packaging.", file=sys.stderr)
        return 1

    if not args.skip_tests:
        run_tests(npm)

    output_dir = resolve_repo_path(args.out)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"pi-manager-pi-cursor-{version}.vsix"
    staged_target = output_dir / f".{target.stem}.staging.vsix"
    if staged_target.exists():
        staged_target.unlink()

    try:
        subprocess.run(
            [*vsce, "package", "--out", str(staged_target)],
            cwd=EXTENSION_DIR,
            check=True,
        )

        if not staged_target.is_file():
            print(f"VSCE completed without creating expected file: {staged_target}", file=sys.stderr)
            return 1
        staged_target.replace(target)
    finally:
        if staged_target.exists():
            staged_target.unlink()

    print(f"Packaged Cursor extension: {target} ({target.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
