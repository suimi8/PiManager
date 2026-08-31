"""Shared subprocess helpers for launching Pi child processes.

Both ``core.run_pi`` (one-shot capture) and ``rpc_session.PiRpcSession``
(long-lived RPC) need the same Windows no-console flag and proxy-sanitized
child environment. Keeping that boilerplate here stops the two launch paths
from drifting; each caller still owns its Popen options (text mode, streams,
process-group handling, output limits), so per-caller behavior is unchanged.
"""
from __future__ import annotations

import os
import subprocess
import sys

from .core import sanitize_proxy_env, strip_pyinstaller_runtime_env


def create_no_window_flag() -> int:
    """Return ``CREATE_NO_WINDOW`` (Windows GUI subsystem) or 0 on POSIX.

    Both launch paths use this so a GUI-subsystem binary never flashes a
    console window when spawning a child.
    """
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def spawn_env(
    extra: dict[str, str] | None = None,
    *,
    sanitize_after_merge: bool = True,
) -> dict[str, str]:
    """Build the child process environment from the ambient environment.

    ``sanitize_proxy_env`` drops proxy env vars that point at unreachable
    endpoints (a configured-but-stopped proxy would otherwise break every
    child request with "Connection error"). Frozen onefile bookkeeping
    (``_PYI_*``) is always stripped so a later ``PiManager.exe`` helper is
    not treated as a worker of this GUI process.

    - ``sanitize_after_merge=True`` mirrors ``core.run_pi``: merge extras
      first, then drop unreachable proxy vars (a proxy var inside extras is
      filtered too).
    - ``sanitize_after_merge=False`` mirrors ``rpc_session._ensure``:
      sanitize the ambient environment first, then overlay the provider env
      untouched.
    """
    if sanitize_after_merge:
        env = os.environ.copy()
        if extra:
            env.update(extra)
        env = sanitize_proxy_env(env)
    else:
        env = sanitize_proxy_env(os.environ.copy())
        if extra:
            env.update(extra)
    return strip_pyinstaller_runtime_env(env)
