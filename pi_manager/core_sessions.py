# -*- coding: utf-8 -*-
"""会话管理：会话目录解析 / 历史会话列表 / 打开路径。

从 ``core.py`` 抽出。对 core 路径函数（sessions_dir）用函数内延迟 import。
core.py 顶部重新导出这些符号以保持 ``core.xxx`` 调用兼容。
"""
from __future__ import annotations

import heapq
import json
import re
import sys
from pathlib import Path
from typing import Any


def _decode_session_folder_slug(slug: str) -> str:
    """Pi 把 cwd 编码为会话目录名，如 --C--Users-suimi-Desktop-app-- → C:\\Users\\suimi\\Desktop\\app。

    编码规则（Pi 0.84.1 及旧版一致）：去掉首分隔符后把 \\ / : 替换为 "-"
    （旧版 Pi 把盘符冒号编码为 "--"），再包一层 "--"。编码有损（连字符与
    分隔符同形），解码为兜底展示，jsonl 中的 cwd 才是权威值。
    """
    s = (slug or "").strip()
    if not s or s in {".", ""}:
        return s
    if not s.startswith("--") or not s.endswith("--") or len(s) <= 4:
        return s
    body = s[2:-2]
    if not body:
        return s
    # Windows 会话目录保留盘符：--C--Users-...--（旧版）或 --C-Users-...--（新版）；
    # 盘符后的 "--" 是路径中的字面连字符（旧版编码的盘符冒号已在上一步消费），
    # 只把单个 "-" 分隔符还原为反斜杠。
    if sys.platform == "win32":
        m = re.match(r"^([A-Za-z])--?(.+)$", body)
        if m:
            drive = m.group(1).upper()
            rest = re.sub(r"(?<!-)-(?!-)", "\\\\", m.group(2))
            return f"{drive}:\\{rest}"
    # 其余平台按实际编码规则还原：去掉两侧 -- 后把单个 "-" 分隔符还原为 "/"
    return "/" + re.sub(r"(?<!-)-(?!-)", "/", body)


def project_name_from_path(path_str: str) -> str:
    p = Path(path_str or "")
    name = p.name.strip() if str(p) else ""
    if name:
        return name
    # Windows 根目录 C:\
    s = str(path_str or "").rstrip("\\/")
    return s or "（未知项目）"


def _extract_user_message_text(obj: dict) -> str:
    """从 message 类型 jsonl 行提取首条用户消息文本（含原样转录）。"""
    # Pi jsonl: {"type":"message","message":{"role":"user","content":[...]}}
    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
    role = str(msg.get("role") or obj.get("role") or "").lower()
    if role and role not in {"user", "human"}:
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        bits: list[str] = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in {"text", "input_text", None} and part.get("text"):
                    bits.append(str(part.get("text")))
            elif isinstance(part, str):
                bits.append(part)
        text = " ".join(bits)
    elif isinstance(msg.get("text"), str):
        text = str(msg.get("text"))
    else:
        text = ""
    return re.sub(r"\s+", " ", text).strip()


def _apply_session_line(obj: dict, meta: dict[str, str]) -> None:
    """把单行 jsonl 解析结果合并进 meta（按 type 分派）。"""
    t = str(obj.get("type") or "")
    if t == "session":
        cwd = str(obj.get("cwd") or "").strip()
        if cwd:
            meta["cwd"] = cwd
            meta["project"] = project_name_from_path(cwd)
        sid = str(obj.get("id") or "").strip()
        if sid:
            meta["session_id"] = sid
        ts = str(obj.get("timestamp") or "").strip()
        if ts:
            meta["started"] = ts
    elif t == "model_change":
        provider = str(obj.get("provider") or "").strip()
        model_id = str(obj.get("modelId") or obj.get("model") or "").strip()
        if provider:
            meta["provider"] = provider
        if provider and model_id:
            meta["model"] = f"{provider}/{model_id}"
        elif model_id:
            meta["model"] = model_id
    elif t == "message" and not meta["preview"]:
        text = _extract_user_message_text(obj)
        if text:
            meta["preview"] = text[:80] + ("…" if len(text) > 80 else "")


def _parse_session_meta(path: Path) -> dict[str, str]:
    """从 session jsonl 头部提取 cwd / model / 首条用户消息摘要。"""
    meta: dict[str, str] = {
        "cwd": "",
        "project": "",
        "model": "",
        "provider": "",
        "preview": "",
        "session_id": "",
        "started": "",
    }
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for i, line in enumerate(fh):
                if i > 120:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if not isinstance(obj, dict):
                    continue
                _apply_session_line(obj, meta)
                if meta["cwd"] and meta["model"] and meta["preview"]:
                    break
    except OSError:
        pass
    return meta


def list_sessions(limit: int = 50) -> list[dict[str, str]]:
    from . import core

    root = core.sessions_dir()
    if not root.exists() or limit <= 0:
        return []

    def candidates(preferred_only: bool):
        for path in root.rglob("*"):
            try:
                if not path.is_file():
                    continue
                preferred = path.suffix.lower() in {".jsonl", ".json", ".pi"} or "session" in path.name.lower()
                if preferred_only and not preferred:
                    continue
                stat_result = path.stat()
                yield stat_result.st_mtime, str(path), path, stat_result
            except OSError:
                continue

    selected = heapq.nlargest(limit, candidates(True), key=lambda item: item[0])
    if not selected:
        selected = heapq.nlargest(limit, candidates(False), key=lambda item: item[0])
    rows = []
    for _mtime, _path_key, p, st in selected:
        try:
            folder_slug = str(p.parent.relative_to(root)) if p.parent != root else "."
            meta = _parse_session_meta(p)
            cwd = meta.get("cwd") or _decode_session_folder_slug(folder_slug)
            project = meta.get("project") or project_name_from_path(cwd)
            # 时间展示
            from datetime import datetime

            try:
                mtime_s = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
            except Exception:
                mtime_s = ""
            started = meta.get("started") or ""
            if started.endswith("Z") and "T" in started:
                try:
                    started = started.replace("T", " ")[:16]
                except Exception:
                    pass
            rows.append(
                {
                    "path": str(p),
                    "name": p.name,
                    "folder": folder_slug,
                    "mtime": str(st.st_mtime),
                    "mtime_text": mtime_s,
                    "started": started or mtime_s,
                    "size": str(st.st_size),
                    "cwd": cwd,
                    "project": project,
                    "model": meta.get("model") or "",
                    "provider": meta.get("provider") or "",
                    "preview": meta.get("preview") or "",
                    "session_id": meta.get("session_id") or "",
                }
            )
        except OSError:
            continue
    return rows


def open_in_explorer(path: str) -> None:
    from . import platform_util as pu

    pu.open_path(path, select_if_file=True)


def open_path(path: str) -> None:
    from . import platform_util as pu

    pu.open_path(path, select_if_file=False)
