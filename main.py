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


def _shred_request_file(path: str) -> None:
    """覆盖擦除并删除一次性请求文件（P2-11）。

    ``--config-mutate`` 的请求文件里带着 broker token，调用后留在临时目录等于
    把凭据落盘（与 ``provider_env._emit`` 的严格加固标准明显不一致）。这里做
    best-effort 清理：先零覆盖再删除，任何失败都不影响主流程返回值。

    只处理「普通文件且非重解析点」：否则同机攻击者可以用 junction 把请求路径
    指向别处，让本函数去零覆盖一个不该动的文件。
    """
    import stat

    try:
        from pi_manager import platform_util

        info = os.stat(path, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or platform_util.is_reparse_point(path):
            return
        size = info.st_size
    except (OSError, ImportError):
        return
    try:
        with open(path, "r+b") as fh:
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def _cli_json_error(error: str) -> int:
    """Print a JSON error payload (extension contract) and return exit code 2."""
    import json

    print(json.dumps({"ok": False, "error": error}))
    return 2


def main():
    _ensure_windows_cli_stdio()
    if len(sys.argv) >= 2 and sys.argv[1] in {"--print-provider-env", "--provider-env"}:
        from pi_manager.provider_env import main as provider_env_main

        return provider_env_main(sys.argv[2:])
    if len(sys.argv) >= 2 and sys.argv[1] == "--vision-describe":
        # Lightweight image understanding entry for Pi skills: no GUI import.
        import argparse as _argparse
        import json

        from pi_manager.core import (
            build_vision_prompt,
            describe_image,
            load_image_for_describe,
        )

        parser = _argparse.ArgumentParser(
            prog="PiManager --vision-describe",
            add_help=False,
            allow_abbrev=False,
        )
        parser.add_argument("path", nargs="?", default="")
        # prompt is everything after the path, joined with spaces — the
        # calling convention used by the Cursor extension / vision skill
        # (<path> [prompt...]) must stay stable.
        parser.add_argument("prompt", nargs=_argparse.REMAINDER)
        try:
            args, _extra = parser.parse_known_args(sys.argv[2:])
        except SystemExit:
            return _cli_json_error("usage: --vision-describe <image-path> [prompt]")
        path = args.path or ""
        if not path:
            return _cli_json_error("usage: --vision-describe <image-path> [prompt]")
        loaded = load_image_for_describe(path)
        if not loaded.get("ok"):
            return _cli_json_error(str(loaded.get("error") or "无法读取图片"))
        # CLI 路径必须与 GUI 路径（ui_features 的识图入口）行为一致：
        # 1. 用 build_vision_prompt 构造「逐字转录」指令，绝不把 None 传下去
        #    （prompt=None 会覆盖签名默认值，请求体里出现 "text": null）；
        # 2. 传 load_image_for_describe 探测到的真实 MIME，别把 JPEG/WebP
        #    一律标成 image/png。
        prompt = " ".join(args.prompt).strip()
        result = describe_image(
            loaded["data"],
            loaded.get("mime") or "image/png",
            prompt=build_vision_prompt(prompt),
        )
        if result.get("ok"):
            desc = result.get("description") or ""
            try:
                print(desc)
            except UnicodeEncodeError:
                sys.stdout.buffer.write(desc.encode("utf-8", errors="replace"))
                sys.stdout.buffer.write(b"\n")
                sys.stdout.buffer.flush()
            return 0
        print(json.dumps({"ok": False, "error": result.get("error") or "识图失败"}))
        return 1
    if len(sys.argv) >= 2 and sys.argv[1] == "--config-mutate":
        import argparse as _argparse
        import json

        from pi_manager.config_broker import mutate_file
        from pi_manager.provider_env import _emit

        parser = _argparse.ArgumentParser(
            prog="PiManager --config-mutate",
            add_help=False,
            allow_abbrev=False,
        )
        parser.add_argument("request_file")
        parser.add_argument("--output", default="")
        rest = sys.argv[2:]
        # Pre-checks keep the JSON-only error contract (no argparse usage
        # noise on stderr for the common bad invocations).
        if not rest or rest[0].startswith("-"):
            return _cli_json_error("request file is required")
        try:
            args, extra = parser.parse_known_args(rest)
        except SystemExit:
            return _cli_json_error("request file is required")
        if extra:
            return _cli_json_error("request file is required")
        result = mutate_file(args.request_file)
        # 请求文件是一次性凭据载体，用完即焚（P2-11）。
        _shred_request_file(args.request_file)
        encoded = json.dumps(result, ensure_ascii=False)
        # 结果可能含中文（错误信息等）；Windows CI 等控制台默认代码页非 UTF-8，
        # print(encoded) 会抛 UnicodeEncodeError 让 --config-mutate 契约崩掉
        # （Cursor 扩展热路径）。stdout 统一 UTF-8 + 替换容错。
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass  # 流不支持 reconfigure（如已被包装）时保持原样
        if args.output:
            try:
                # Same hardened write as provider-env responses (pre-created
                # file only, no symlink following); stdout below remains the
                # fallback channel the extension already reads.
                _emit(result, args.output)
            except (ValueError, OSError):
                pass
        if not args.output:
            print(encoded)
        return 0 if result.get("ok") else 2
    # Helper subcommands above are the extension's hot path and must not
    # rewrite the registry on every call; publish it only when the GUI runs.
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
    from pi_manager.helper_registry import register_current_helper_best_effort

    register_current_helper_best_effort()
    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
