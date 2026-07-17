import os
import sys


def _ensure_windows_cli_stdio() -> None:
    """GUI-subsystem binaries need usable stdio for CLI flags on Windows."""
    if sys.platform != "win32":
        return
    if len(sys.argv) < 2 or not str(sys.argv[1]).startswith("--"):
        return
    # If parent already redirected pipes (CI / Cursor helper), keep them.
    if sys.stdout is not None and sys.stderr is not None:
        try:
            sys.stdout.fileno()
            sys.stderr.fileno()
            return
        except Exception:
            pass
    try:
        import ctypes
        import io

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        if kernel32.GetConsoleWindow() == 0:
            kernel32.AttachConsole(0xFFFFFFFF)  # ATTACH_PARENT_PROCESS
        try:
            sys.stdout = io.TextIOWrapper(
                open("CONOUT$", "wb", buffering=0), encoding="utf-8", errors="replace"
            )
            sys.stderr = io.TextIOWrapper(
                open("CONOUT$", "wb", buffering=0), encoding="utf-8", errors="replace"
            )
        except OSError:
            # Fallback for fully headless launches.
            if sys.stdout is None:
                sys.stdout = open(os.devnull, "w", encoding="utf-8")
            if sys.stderr is None:
                sys.stderr = open(os.devnull, "w", encoding="utf-8")
    except Exception:
        pass


def main():
    _ensure_windows_cli_stdio()
    if len(sys.argv) >= 2 and sys.argv[1] in {"--print-provider-env", "--provider-env"}:
        from pi_manager.provider_env import main as provider_env_main

        return provider_env_main(sys.argv[2:])
    if len(sys.argv) >= 2 and sys.argv[1] == "--config-mutate":
        import json

        from pi_manager.config_broker import mutate_file

        if len(sys.argv) != 3:
            print(json.dumps({"ok": False, "error": "request file is required"}))
            return 2
        result = mutate_file(sys.argv[2])
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result.get("ok") else 2
    if len(sys.argv) >= 2 and sys.argv[1] in {"--self-check", "--smoke-test"}:
        from pi_manager.extras import APP_VERSION
        from pi_manager.resources import self_check

        errors = self_check()
        if errors:
            for line in errors:
                print(f"FAIL: {line}", file=sys.stderr)
            print("self-check: FAILED", file=sys.stderr)
            return 1
        print("self-check: OK")
        print(f"version={APP_VERSION}")
        print(f"frozen={bool(getattr(sys, 'frozen', False))}")
        print(f"executable={sys.executable}")
        print(f"platform={sys.platform}")
        return 0
    from pi_manager.ui import run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
