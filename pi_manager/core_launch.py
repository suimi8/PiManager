"""启动 Pi：构造参数、外部终端、非交互 -p。"""
from __future__ import annotations

from collections.abc import Callable


def _core():
    from . import core

    return core




def build_pi_launch_args(
    *,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    args: list[str] = []
    pair = _core().normalize_model_pair(provider, model)
    if pair is not None:
        pair_provider, pair_model = pair
        args += ["--provider", pair_provider, "--model", pair_model]
    if thinking:
        args += ["--thinking", thinking]
    if extra:
        args += extra
    # 构造期就校验，让非法 provider/model 名在离用户操作最近的地方报错。
    # _core().escape_cmd_shim_args 这个统一出口也会拦（安全性已由它保证），但那时错误
    # 信息已经离触发点很远了（审查 P0-1 的「三处同时拦截」要求）。
    _core().validate_launch_tokens(args)
    return args



def launch_pi_interactive(
    workdir: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    terminal: str = "auto",
    extra: list[str] | None = None,
) -> str:
    """Launch full interactive Pi in an external terminal (cross-platform)."""
    from . import platform_util as pu

    pi_args = build_pi_launch_args(
        provider=provider, model=model, thinking=thinking, extra=extra
    )
    pi_args = _core().append_language_args(pi_args)
    pi_args = _core().append_vision_args(pi_args)
    base = _core().pi_base_cmd()
    # Mirror _core().run_pi: when the pi launcher is a cmd.exe batch shim, cmd.exe
    # re-expands %VAR% in the command line (e.g. %TEMP% in the vision rule)
    # before the script runs. Escape percents so args stay literal.
    pi_args = _core().escape_cmd_shim_args(pi_args, base)
    full_cmd_list = base + pi_args
    workdir = workdir or str(_core().user_home())
    if provider:
        child_env = _core().provider_runtime_env(provider)
    else:
        child_env = {}
    return pu.launch_in_terminal(
        full_cmd_list,
        workdir,
        terminal=terminal,
        env=child_env,
    )



def run_pi_print(
    prompt: str,
    *,
    workdir: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    timeout: float = 300,
    is_cancelled: Callable[[], bool] | None = None,
) -> tuple[int, str, str]:
    args = build_pi_launch_args(provider=provider, model=model, thinking=thinking)
    args = _core().append_language_args(args)
    args += ["-p", "--no-session", prompt]
    # project trust for non-interactive
    args += ["--approve"]
    attempted_key_ids: set[str] = set()
    while True:
        if is_cancelled and is_cancelled():
            return -1, "", "已停止生成"
        credential = _core().provider_runtime_credential(provider)
        p = _core().run_pi(
            args,
            cwd=workdir,
            timeout=timeout,
            env=credential["env"],
            is_cancelled=is_cancelled,
        )
        stdout = p.stdout or ""
        stderr = p.stderr or ""
        if (is_cancelled and is_cancelled()) or "已停止生成" in stderr:
            return -1, stdout, "已停止生成"
        key_id = str(credential.get("key_id") or "")
        if p.returncode == 0 or not key_id or not _core().is_provider_key_error(
            p.returncode, stdout, stderr
        ):
            return p.returncode, stdout, stderr
        if key_id in attempted_key_ids:
            return p.returncode, stdout, stderr
        attempted_key_ids.add(key_id)

        from . import secrets as secretstore

        reason = _core().provider_key_failure_reason(p.returncode, stdout, stderr)
        changed = secretstore.mark_provider_key_failed(
            str(provider or ""), key_id, reason
        )
        next_credential = secretstore.get_active_provider_credential(str(provider or ""))
        if (
            not changed
            or not next_credential
            or next_credential["key_id"] in attempted_key_ids
        ):
            return p.returncode, stdout, stderr
