# -*- coding: utf-8 -*-
"""会话文件删除/重命名/过滤（路径限制在 sessions 目录内）。

从 ``extras.py`` 下沉。``pi_manager.extras`` 继续 re-export，保持现有导入与
monkeypatch 点（``extras.xxx``）稳定。对会被测试 patch 的符号走 ``_extras().xxx``。
"""
from __future__ import annotations

import os
from pathlib import Path

from . import core


def _confined_session_path(path: str) -> Path | None:
    root = Path(os.path.realpath(str(core.sessions_dir())))
    real = Path(os.path.realpath(str(path)))
    try:
        real.relative_to(root)
    except ValueError:
        return None
    return real


def session_delete(path: str) -> bool:
    real = _confined_session_path(path)
    if real is None or not real.exists() or not real.is_file():
        return False
    real.unlink()
    return True


def session_rename(path: str, new_name: str) -> str:
    real = _confined_session_path(path)
    if real is None or not real.exists():
        raise FileNotFoundError(path)
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("名称为空")
    if (
        ".." in new_name
        or os.sep in new_name
        or (os.altsep and os.altsep in new_name)
        or os.path.isabs(new_name)
    ):
        raise ValueError("非法的会话名称")
    if not Path(new_name).suffix:
        new_name = new_name + real.suffix
    dest = real.with_name(new_name)
    if dest.exists():
        raise FileExistsError(str(dest))
    real.rename(dest)
    return str(dest)


def list_sessions_filtered(limit: int = 100, workdir_substr: str = "", name_substr: str = "") -> list[dict[str, str]]:
    rows = core.list_sessions(limit=max(limit, 200))
    wd = (workdir_substr or "").lower().strip()
    nm = (name_substr or "").lower().strip()
    out = []
    for r in rows:
        blob = " ".join(
            str(r.get(k) or "")
            for k in ("path", "folder", "name", "cwd", "project", "model", "preview")
        ).lower()
        if wd and wd not in blob:
            continue
        if nm and nm not in blob:
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out
