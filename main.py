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
    except Exception as exc:
        import logging
        logging.getLogger("pi_manager").debug("stdio redirect failed: %s", exc, exc_info=True)


def main():
    _ensure_windows_cli_stdio()
    if len(sys.argv) >= 2 and sys.argv[1] in {"--print-provider-env", "--provider-env"}:
        from pi_manager.provider_env import main as provider_env_main

        return provider_env_main(sys.argv[2:])
    if len(sys.argv) >= 2 and sys.argv[1] == "--vision-describe":
        # Lightweight image understanding entry for Pi skills: no GUI import.
        import json

        from pi_manager.core import describe_image

        path = sys.argv[2] if len(sys.argv) > 2 else ""
        if not path:
            print(json.dumps({"ok": False, "error": "usage: --vision-describe <image-path> [prompt]"}))
            return 2
        allowed_exts = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"}
        p = os.path.normpath(os.path.abspath(os.path.expanduser(path)))
        if os.path.splitext(p)[1].lower() not in allowed_exts:
            print(json.dumps({"ok": False, "error": "仅支持图片文件（png/jpg/jpeg/gif/bmp/webp/tiff）"}))
            return 2
        max_image_size = 20 * 1024 * 1024
        try:
            if os.path.getsize(p) > max_image_size:
                print(json.dumps({"ok": False, "error": "图片文件过大（上限 20MB）"}))
                return 2
        except OSError as exc:
            print(json.dumps({"ok": False, "error": f"无法读取图片：{exc}"}))
            return 2
        prompt = " ".join(sys.argv[3:]) or ""
        try:
            with open(p, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            print(json.dumps({"ok": False, "error": f"无法读取图片：{exc}"}))
            return 2
        result = describe_image(data, prompt=prompt or None)
        if result.get("ok"):
            print(result.get("description") or "")
            return 0
        print(json.dumps({"ok": False, "error": result.get("error") or "识图失败"}))
        return 1
    if len(sys.argv) >= 2 and sys.argv[1] == "--config-mutate":
        import json

        from pi_manager.config_broker import mutate_file
        from pi_manager.provider_env import _emit

        output_path = ""
        if len(sys.argv) == 5 and sys.argv[3] == "--output":
            output_path = sys.argv[4]
        elif len(sys.argv) != 3:
            result = {"ok": False, "error": "request file is required"}
            print(json.dumps(result))
            return 2
        result = mutate_file(sys.argv[2])
        encoded = json.dumps(result, ensure_ascii=False)
        if output_path:
            try:
                # Same hardened write as provider-env responses (pre-created
                # file only, no symlink following); stdout below remains the
                # fallback channel the extension already reads.
                _emit(result, output_path)
            except (ValueError, OSError):
                pass
        if not output_path:
            print(encoded)
        return 0 if result.get("ok") else 2
    # Helper subcommands above are the extension's hot path and must not
    # rewrite the registry on every call; publish it when the app itself runs.
    from pi_manager.helper_registry import register_current_helper_best_effort

    register_current_helper_best_effort()
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
