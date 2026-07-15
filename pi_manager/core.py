"""
Pi Manager - Cross-platform GUI for managing and launching Pi Coding Agent.
All agent capability comes from the official `pi` CLI; this app manages
providers/models/settings and launches full Pi sessions.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import storage


def user_home() -> Path:
    return Path(os.path.expanduser("~"))


def pi_agent_dir() -> Path:
    return user_home() / ".pi" / "agent"


def models_path() -> Path:
    return pi_agent_dir() / "models.json"


def settings_path() -> Path:
    return pi_agent_dir() / "settings.json"


def auth_path() -> Path:
    return pi_agent_dir() / "auth.json"


def manager_config_path() -> Path:
    return pi_agent_dir() / "pi-manager.json"


def sessions_dir() -> Path:
    return pi_agent_dir() / "sessions"


def ensure_agent_dir() -> None:
    pi_agent_dir().mkdir(parents=True, exist_ok=True)


def load_json(path: Path, default: Any) -> Any:
    return storage.load_json(path, default)


def save_json(path: Path, data: Any) -> None:
    ensure_agent_dir()
    storage.save_json(path, data)


def mask_secret(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    s = str(value)
    if s.startswith(("!", "$")) or s.isupper() and "_" in s and not s.startswith("sk") and not s.startswith("tp-"):
        # env var name or shell command
        return s
    if len(s) <= keep * 2:
        return "*" * len(s)
    return s[:keep] + "*" * max(4, len(s) - keep * 2) + s[-keep:]


def redact_sensitive_config(value: Any, field_name: str = "") -> Any:
    """Return a display-safe deep copy of provider configuration."""
    sensitive = any(
        marker in field_name.lower().replace("_", "-")
        for marker in ("apikey", "api-key", "authorization", "token", "secret", "cookie")
    )
    if sensitive and isinstance(value, (str, int, float)):
        return mask_secret(str(value))
    if isinstance(value, dict):
        return {
            str(key): redact_sensitive_config(item, str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive_config(item, field_name) for item in value]
    return value


def find_pi_command() -> str | None:
    """Return absolute path to pi launcher if possible (Windows / macOS / Linux)."""
    from . import platform_util as pu

    return pu.find_pi_command()


def pi_base_cmd() -> list[str]:
    raw = find_pi_command()
    if not raw:
        raise FileNotFoundError(
            "未找到 pi 命令。请先安装: npm install -g @earendil-works/pi-coding-agent"
        )
    if raw.startswith("NODECLI::"):
        parts = raw.split("::", 2)
        if len(parts) == 3:
            return [parts[1], parts[2]]
    if raw.startswith('"') and '" "' in raw:
        parts = re.findall(r'"([^"]+)"', raw)
        if len(parts) >= 2:
            return parts[:2]
    low = raw.lower()
    if low.endswith(".cmd") or low.endswith(".bat"):
        return ["cmd.exe", "/c", raw]
    if low.endswith(".ps1"):
        return [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            raw,
        ]
    return [raw]


def list_terminal_options() -> list[tuple[str, str]]:
    from . import platform_util as pu

    return pu.list_terminal_options()


def run_pi(
    args: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = 60,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = pi_base_cmd() + args
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    # Prefer UTF-8 output
    full_env.setdefault("PYTHONIOENCODING", "utf-8")
    return subprocess.run(
        cmd,
        cwd=cwd or os.getcwd(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        env=full_env,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )


def get_pi_version() -> str:
    try:
        p = run_pi(["-v"], timeout=20)
        out = (p.stdout or p.stderr or "").strip()
        return out.splitlines()[0] if out else "unknown"
    except Exception as e:
        return f"error: {e}"


@dataclass
class ModelInfo:
    provider: str
    model: str
    context: str = ""
    max_out: str = ""
    thinking: str = ""
    images: str = ""

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    def display(self) -> str:
        extra = []
        if self.context:
            extra.append(f"ctx {self.context}")
        if self.thinking and self.thinking.lower() in {"yes", "true", "y"}:
            extra.append("thinking")
        if self.images and self.images.lower() in {"yes", "true", "y"}:
            extra.append("images")
        suffix = f"  ({', '.join(extra)})" if extra else ""
        return f"{self.key}{suffix}"


def list_models(search: str | None = None) -> list[ModelInfo]:
    args = ["--list-models"]
    if search:
        args.append(search)
    try:
        p = run_pi(args, timeout=45, env=all_provider_runtime_env(strict=False))
    except Exception:
        return []
    text = (p.stdout or "") + "\n" + (p.stderr or "")
    models: list[ModelInfo] = []
    # lines like: provider  model  context  max-out  thinking  images
    for line in text.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("provider"):
            continue
        # collapse multiple spaces
        parts = re.split(r"\s{2,}|\t+", line)
        if len(parts) < 2:
            parts = line.split()
        if len(parts) < 2:
            continue
        provider, model = parts[0], parts[1]
        if provider in {"provider", "─", "-", "="}:
            continue
        # skip junk
        if not re.match(r"^[\w.-]+$", provider):
            continue
        models.append(
            ModelInfo(
                provider=provider,
                model=model,
                context=parts[2] if len(parts) > 2 else "",
                max_out=parts[3] if len(parts) > 3 else "",
                thinking=parts[4] if len(parts) > 4 else "",
                images=parts[5] if len(parts) > 5 else "",
            )
        )
    # de-dupe
    seen: set[str] = set()
    uniq: list[ModelInfo] = []
    for m in models:
        if m.key in seen:
            continue
        seen.add(m.key)
        uniq.append(m)
    return uniq


def load_settings() -> dict[str, Any]:
    return load_json(settings_path(), {})


def save_settings(data: dict[str, Any]) -> None:
    save_json(settings_path(), data)


def load_models_config() -> dict[str, Any]:
    cfg = load_json(models_path(), {"providers": {}})
    if not isinstance(cfg, dict):
        cfg = {"providers": {}}
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        cfg["providers"] = {}
        return cfg

    # Pi understands environment references but not Pi Manager's legacy
    # __DPAPI__ marker. Migrate legacy markers and plaintext keys on first read.
    try:
        from . import secrets as secretstore

        needs_migration = any(
            isinstance(entry, dict)
            and bool(str(entry.get("apiKey") or ""))
            and not str(entry.get("apiKey") or "").startswith("!")
            and (
                str(entry.get("apiKey") or "").startswith("__DPAPI__:")
                or not secretstore.referenced_env_name(str(entry.get("apiKey") or ""))
            )
            for entry in providers.values()
        )
        if needs_migration:
            migrated = secretstore.migrate_plaintext_keys(providers)
            if migrated != providers:
                migrated_cfg = dict(cfg)
                migrated_cfg["providers"] = migrated
                save_models_config(migrated_cfg)
                cfg = migrated_cfg
    except Exception:
        # Keep configuration readable even if the platform keyring is broken.
        pass
    return cfg


def save_models_config(data: dict[str, Any]) -> None:
    save_json(models_path(), data)


def load_auth() -> dict[str, Any]:
    return load_json(auth_path(), {})


def auth_summary() -> list[dict[str, str]]:
    auth = load_auth()
    rows = []
    for name, val in auth.items():
        if not isinstance(val, dict):
            continue
        t = val.get("type", "unknown")
        if t == "oauth" or "access" in val or "refresh" in val:
            status = "OAuth 已登录"
        elif t == "api_key" or "key" in val:
            key = val.get("key", "")
            status = f"API Key ({mask_secret(str(key))})"
        else:
            status = str(t)
        rows.append({"provider": name, "status": status})
    return rows


def load_manager_config() -> dict[str, Any]:
    data = load_json(
        manager_config_path(),
        {
            "favorites": [],
            "last_workdir": str(user_home()),
            "terminal": "auto",
            "quick_models": [],
            "drop_auto_launch": True,
            "language": "zh-CN",
            "setup_done": False,
            "auto_check_update": True,
            "last_update_check": "",
            "ui_mode": "night",
            "ui_accent": "blue",
            "proxy_enabled": False,
            "proxy_url": "",
            "test_concurrency": 3,
            "secure_keys": True,
            "minimize_to_tray": True,
            "start_minimized": False,
            "health_interval_min": 0,
            "update_manifest_url": "",
            "last_manager_update_check": "",
            # 快速提问：模型连续失败后自动切换下一个收藏模型
            "failover_enabled": True,
            "failover_fail_threshold": 3,
            "failover_fail_counts": {},
            "failover_silent": True,
        },
    )
    # merge missing keys for upgrades
    defaults = {
        "proxy_enabled": False,
        "proxy_url": "",
        "test_concurrency": 3,
        "secure_keys": True,
        "minimize_to_tray": True,
        "start_minimized": False,
        "health_interval_min": 0,
        "update_manifest_url": "",
        "last_manager_update_check": "",
        "drop_auto_launch": True,
        "language": "zh-CN",
        "ui_mode": "night",
        "ui_accent": "blue",
        "auto_check_update": True,
        "failover_enabled": True,
        "failover_fail_threshold": 3,
        "failover_fail_counts": {},
        "failover_silent": True,
    }
    if not isinstance(data, dict):
        data = {}
    for k, v in defaults.items():
        data.setdefault(k, v)
    return data


def save_manager_config(data: dict[str, Any]) -> None:
    save_json(manager_config_path(), data)


def set_default_model(provider: str, model: str, thinking: str | None = None) -> dict[str, Any]:
    settings = load_settings()
    settings["defaultProvider"] = provider
    settings["defaultModel"] = model
    if thinking:
        settings["defaultThinkingLevel"] = thinking
    save_settings(settings)
    return settings


def get_default_model() -> tuple[str, str, str]:
    s = load_settings()
    return (
        str(s.get("defaultProvider") or ""),
        str(s.get("defaultModel") or ""),
        str(s.get("defaultThinkingLevel") or "medium"),
    )


def set_enabled_models(patterns: list[str]) -> dict[str, Any]:
    settings = load_settings()
    if patterns:
        settings["enabledModels"] = patterns
    elif "enabledModels" in settings:
        del settings["enabledModels"]
    save_settings(settings)
    return settings


def upsert_custom_provider(
    name: str,
    *,
    base_url: str,
    api: str = "openai-completions",
    api_key: str | None = None,
    models: list[dict[str, Any]] | None = None,
    compat: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    cfg = load_models_config()
    providers = cfg.setdefault("providers", {})
    existing = providers.get(name, {}) if isinstance(providers.get(name), dict) else {}
    from . import secrets as secretstore

    if api_key is None:
        raw_key = str(existing.get("apiKey") or "")
    else:
        raw_key = secretstore.store_provider_api_key(name, str(api_key).strip())
    entry: dict[str, Any] = {
        "baseUrl": base_url,
        "api": api,
        "apiKey": raw_key,
        "models": models if models is not None else existing.get("models", []),
    }
    if compat is not None:
        entry["compat"] = compat
    elif "compat" in existing:
        entry["compat"] = existing["compat"]
    if headers is not None:
        entry["headers"] = headers
    elif "headers" in existing:
        entry["headers"] = existing["headers"]
    providers[name] = entry
    save_models_config(cfg)
    return cfg


def _parse_favorite_key(key: str) -> tuple[str, str] | None:
    key = (key or "").strip()
    if "/" not in key:
        return None
    provider, model = key.split("/", 1)
    provider, model = provider.strip(), model.strip()
    if not provider or not model:
        return None
    return provider, model


def purge_favorites(
    *,
    provider: str | None = None,
    model: str | None = None,
    redefault: bool = True,
) -> dict[str, Any]:
    """从收藏中移除匹配项；若默认模型被移除，则自动切换到下一个收藏。

    - 仅 provider：删除该 Provider 下全部收藏
    - provider + model：只删除该模型收藏
    - redefault=True：默认模型落在被删集合时，切到剩余收藏第一项；无剩余则清空默认
    """
    provider = (provider or "").strip()
    model = (model or "").strip()
    mgr = load_manager_config()
    favs = list(mgr.get("favorites") or [])
    kept: list[str] = []
    removed: list[str] = []
    for key in favs:
        parsed = _parse_favorite_key(str(key))
        if not parsed:
            kept.append(str(key))
            continue
        p, m = parsed
        drop = False
        if provider and model:
            drop = p == provider and m == model
        elif provider:
            drop = p == provider
        if drop:
            removed.append(str(key))
        else:
            kept.append(str(key))

    changed = removed or favs != kept
    if changed:
        mgr["favorites"] = kept
        save_manager_config(mgr)

    result: dict[str, Any] = {
        "removed_favorites": removed,
        "favorites": kept,
        "default_changed": False,
        "default_provider": "",
        "default_model": "",
    }

    if not redefault:
        return result

    cur_p, cur_m, thinking = get_default_model()
    need_redefault = False
    if provider and model:
        need_redefault = cur_p == provider and cur_m == model
    elif provider:
        need_redefault = cur_p == provider
    # 默认模型对应收藏已被删，或默认本身指向已删 provider
    if not need_redefault and removed:
        cur_key = f"{cur_p}/{cur_m}" if cur_p and cur_m else ""
        if cur_key and cur_key in removed:
            need_redefault = True

    if need_redefault:
        next_p, next_m = "", ""
        for key in kept:
            parsed = _parse_favorite_key(str(key))
            if parsed:
                next_p, next_m = parsed
                break
        if next_p and next_m:
            set_default_model(next_p, next_m, thinking or None)
            result["default_changed"] = True
            result["default_provider"] = next_p
            result["default_model"] = next_m
        else:
            # 无可用收藏：清空默认，避免指向已删除 provider
            settings = load_settings()
            settings["defaultProvider"] = ""
            settings["defaultModel"] = ""
            save_settings(settings)
            result["default_changed"] = True
            result["default_provider"] = ""
            result["default_model"] = ""
    else:
        result["default_provider"] = cur_p
        result["default_model"] = cur_m

    return result


def delete_custom_provider(name: str) -> dict[str, Any]:
    cfg = load_models_config()
    providers = cfg.get("providers", {})
    if name in providers:
        del providers[name]
        cfg["providers"] = providers
        save_models_config(cfg)
    try:
        from . import secrets as secretstore

        secretstore.delete_secret(secretstore.provider_key_name(name))
    except Exception:
        pass
    # 同步清理收藏，并在默认属于该 Provider 时切换到下一个收藏
    try:
        cfg["_purge"] = purge_favorites(provider=name, redefault=True)
    except Exception:
        cfg["_purge"] = {"removed_favorites": [], "favorites": [], "default_changed": False}
    return cfg


def add_model_to_provider(provider: str, model_id: str, **kwargs: Any) -> dict[str, Any]:
    cfg = load_models_config()
    providers = cfg.setdefault("providers", {})
    if provider not in providers:
        raise KeyError(f"provider not found: {provider}")
    models = providers[provider].setdefault("models", [])
    # replace if exists
    models = [m for m in models if m.get("id") != model_id]
    item = {"id": model_id, **kwargs}
    if "cost" not in item:
        item["cost"] = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
    models.append(item)
    providers[provider]["models"] = models
    save_models_config(cfg)
    return cfg


def remove_model_from_provider(provider: str, model_id: str) -> dict[str, Any]:
    cfg = load_models_config()
    providers = cfg.get("providers", {})
    if provider in providers:
        models = providers[provider].get("models", [])
        providers[provider]["models"] = [m for m in models if m.get("id") != model_id]
        save_models_config(cfg)
    try:
        cfg["_purge"] = purge_favorites(provider=provider, model=model_id, redefault=True)
    except Exception:
        cfg["_purge"] = {"removed_favorites": [], "favorites": [], "default_changed": False}
    return cfg



def build_pi_launch_args(
    *,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    args: list[str] = []
    if provider:
        args += ["--provider", provider]
    if model:
        args += ["--model", model]
    if thinking:
        args += ["--thinking", thinking]
    if extra:
        args += extra
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
    pi_args = append_language_args(pi_args)
    base = pi_base_cmd()
    full_cmd_list = base + pi_args
    workdir = workdir or str(user_home())
    return pu.launch_in_terminal(
        full_cmd_list,
        workdir,
        terminal=terminal,
        env=provider_runtime_env(provider),
    )


def run_pi_print(
    prompt: str,
    *,
    workdir: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    timeout: float = 300,
) -> tuple[int, str, str]:
    args = build_pi_launch_args(provider=provider, model=model, thinking=thinking)
    args = append_language_args(args)
    args += ["-p", "--no-session", prompt]
    # project trust for non-interactive
    args += ["--approve"]
    p = run_pi(
        args,
        cwd=workdir,
        timeout=timeout,
        env=provider_runtime_env(provider),
    )
    return p.returncode, p.stdout or "", p.stderr or ""


def _decode_session_folder_slug(slug: str) -> str:
    """Pi 将 cwd 编码为目录名，如 --C--Users-suimi-Desktop-app-- → C:\\Users\\suimi\\Desktop\\app。"""
    s = (slug or "").strip()
    if not s or s in {".", ""}:
        return s
    # Windows: --C--Users-suimi-Desktop-app--
    m = re.match(r"^--([A-Za-z])--(.+)--$", s)
    if m:
        drive = m.group(1).upper()
        rest = m.group(2).replace("-", "\\")
        return f"{drive}:\\{rest}"
    # 退化：去掉两侧 --
    if s.startswith("--") and s.endswith("--") and len(s) > 4:
        body = s[2:-2]
        return "/" + body.replace("-", "/")
    return s


def _project_name_from_path(path_str: str) -> str:
    p = Path(path_str or "")
    name = p.name.strip() if str(p) else ""
    if name:
        return name
    # Windows 根目录 C:\
    s = str(path_str or "").rstrip("\\/")
    return s or "（未知项目）"


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
                t = str(obj.get("type") or "")
                if t == "session":
                    cwd = str(obj.get("cwd") or "").strip()
                    if cwd:
                        meta["cwd"] = cwd
                        meta["project"] = _project_name_from_path(cwd)
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
                    # Pi jsonl: {"type":"message","message":{"role":"user","content":[...]}}
                    msg = obj.get("message") if isinstance(obj.get("message"), dict) else obj
                    role = str(msg.get("role") or obj.get("role") or "").lower()
                    if role and role not in {"user", "human"}:
                        continue
                    text = ""
                    content = msg.get("content")
                    if isinstance(content, str):
                        text = content
                    elif isinstance(content, list):
                        bits = []
                        for part in content:
                            if isinstance(part, dict):
                                if part.get("type") in {"text", "input_text", None} and part.get("text"):
                                    bits.append(str(part.get("text")))
                            elif isinstance(part, str):
                                bits.append(part)
                        text = " ".join(bits)
                    elif isinstance(msg.get("text"), str):
                        text = str(msg.get("text"))
                    text = re.sub(r"\s+", " ", text).strip()
                    if text:
                        meta["preview"] = text[:80] + ("…" if len(text) > 80 else "")
                if meta["cwd"] and meta["model"] and meta["preview"]:
                    break
    except OSError:
        pass
    return meta


def list_sessions(limit: int = 50) -> list[dict[str, str]]:
    root = sessions_dir()
    if not root.exists():
        return []
    files: list[Path] = []
    for p in root.rglob("*"):
        if p.is_file() and p.suffix.lower() in {".jsonl", ".json", ".pi"}:
            files.append(p)
        elif p.is_file() and "session" in p.name.lower():
            files.append(p)
    if not files:
        files = [p for p in root.rglob("*") if p.is_file()]
    files.sort(key=lambda x: x.stat().st_mtime, reverse=True)
    rows = []
    for p in files[:limit]:
        try:
            st = p.stat()
            folder_slug = str(p.parent.relative_to(root)) if p.parent != root else "."
            meta = _parse_session_meta(p)
            cwd = meta.get("cwd") or _decode_session_folder_slug(folder_slug)
            project = meta.get("project") or _project_name_from_path(cwd)
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


def default_model_template(model_id: str) -> dict[str, Any]:
    return {
        "id": model_id,
        "reasoning": True,
        "input": ["text"],
        "contextWindow": 128000,
        "maxTokens": 32768,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }



# Language / install / theme helpers
# ---------------------------------------------------------------------------

LANG_ZH_PROMPT = """## 语言偏好（必须遵守）
- 请尽可能使用简体中文与用户交流、解释、写说明与文档。
- 仅当中文无法准确表达时才保留英文（如 API 名、协议字段、库名、错误码、固定术语），并尽量附简短中文说明。
- 代码标识符、命令、路径、配置键名保持原样，不要翻译。
- 回答优先中文，结构清晰，避免无必要的英文整段输出。
"""

LANG_EN_PROMPT = """## Language preference
- Prefer clear English for explanations and documentation.
- Keep code identifiers, commands, paths, and config keys unchanged.
"""

LANG_PROMPTS = {
    "zh-CN": LANG_ZH_PROMPT,
    "en": LANG_EN_PROMPT,
}


def get_language() -> str:
    cfg = load_manager_config()
    lang = str(cfg.get("language") or "zh-CN")
    return lang if lang in LANG_PROMPTS or lang == "auto" else "zh-CN"


def set_language(lang: str) -> None:
    cfg = load_manager_config()
    cfg["language"] = lang
    save_manager_config(cfg)
    apply_language_preference(lang)


def language_prompt_text(lang: str | None = None) -> str:
    lang = lang or get_language()
    if lang == "auto":
        return ""
    return LANG_PROMPTS.get(lang, LANG_ZH_PROMPT)


def agents_md_path() -> Path:
    return pi_agent_dir() / "AGENTS.md"


_LANG_BLOCK_RE = re.compile(
    r"<!-- PI-MANAGER-LANG-START -->.*?<!-- PI-MANAGER-LANG-END -->\n?",
    re.DOTALL,
)


def apply_language_preference(lang: str | None = None) -> Path:
    """Write global AGENTS.md language block so Pi sessions use the preference."""
    lang = lang or get_language()
    ensure_agent_dir()
    path = agents_md_path()
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    existing = _LANG_BLOCK_RE.sub("", existing).rstrip() + "\n"
    body = language_prompt_text(lang).strip()
    if body:
        block = (
            "\n<!-- PI-MANAGER-LANG-START -->\n"
            f"{body}\n"
            "<!-- PI-MANAGER-LANG-END -->\n"
        )
        existing = existing.rstrip() + "\n" + block
    path.write_text(existing.lstrip() + ("\n" if not existing.endswith("\n") else ""), encoding="utf-8")
    return path


def append_language_args(args: list[str], lang: str | None = None) -> list[str]:
    """Add --append-system-prompt for launch-time language enforcement."""
    text = language_prompt_text(lang)
    if text.strip():
        args = list(args) + ["--append-system-prompt", text.strip()]
    return args


def apply_theme(theme_name: str) -> dict[str, Any]:
    from .builtin_themes import ensure_builtin_themes

    ensure_builtin_themes()
    settings = load_settings()
    settings["theme"] = theme_name
    save_settings(settings)
    return settings


def get_theme() -> str:
    return str(load_settings().get("theme") or "dark")


def list_themes() -> list[tuple[str, str]]:
    from .builtin_themes import list_theme_choices

    return list_theme_choices()


def _npm_command(*args: str) -> list[str]:
    """Resolve npm's Windows command shim without invoking a shell."""
    names = ("npm.cmd", "npm") if sys.platform == "win32" else ("npm",)
    executable = next((path for name in names if (path := shutil.which(name))), names[0])
    return [executable, *args]


def get_latest_pi_version(timeout: float = 20) -> str | None:
    try:
        p = subprocess.run(
            _npm_command("view", "@earendil-works/pi-coding-agent", "version"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        ver = (p.stdout or "").strip()
        return ver or None
    except Exception:
        return None


def get_installed_pi_version() -> str | None:
    v = get_pi_version()
    if not v or v.startswith("error"):
        return None
    # version line may be just 0.80.6
    m = re.search(r"(\d+\.\d+\.\d+(?:[-+][\w.]+)?)", v)
    return m.group(1) if m else v.strip() or None


def parse_semver(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", v)
    return tuple(int(x) for x in parts[:4]) if parts else (0,)


def needs_pi_install_or_update() -> dict[str, Any]:
    installed = get_installed_pi_version()
    latest = get_latest_pi_version()
    result = {
        "installed": installed,
        "latest": latest,
        "missing": installed is None,
        "outdated": False,
        "ok": False,
        "message": "",
    }
    if installed is None:
        result["message"] = "未检测到 Pi，需要安装。"
        return result
    if latest and parse_semver(installed) < parse_semver(latest):
        result["outdated"] = True
        result["message"] = f"已安装 {installed}，最新 {latest}，建议升级。"
        return result
    result["ok"] = True
    result["message"] = f"Pi 已就绪（{installed}" + (f"，最新 {latest}" if latest else "") + "）"
    return result


def install_or_update_pi(timeout: float = 300) -> tuple[int, str, str]:
    """Install/update latest pi via npm. Returns (code, stdout, stderr)."""
    cmd = _npm_command("install", "-g", "@earendil-works/pi-coding-agent@latest")
    try:
        p = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:
        return 1, "", str(e)


def is_setup_done() -> bool:
    return bool(load_manager_config().get("setup_done"))


def mark_setup_done(done: bool = True) -> None:
    cfg = load_manager_config()
    cfg["setup_done"] = bool(done)
    save_manager_config(cfg)


def run_first_time_bootstrap() -> None:
    """Ensure language block + themes exist."""
    from .builtin_themes import ensure_builtin_themes

    ensure_builtin_themes()
    apply_language_preference(get_language())

def get_ui_theme() -> dict[str, str]:
    cfg = load_manager_config()
    mode = str(cfg.get("ui_mode") or "night")
    accent = str(cfg.get("ui_accent") or "blue")
    return {"mode": mode, "accent": accent}


def set_ui_theme(mode: str | None = None, accent: str | None = None) -> dict[str, str]:
    cfg = load_manager_config()
    if mode is not None:
        cfg["ui_mode"] = mode
    if accent is not None:
        cfg["ui_accent"] = accent
    save_manager_config(cfg)
    return get_ui_theme()


class ProviderKeyError(RuntimeError):
    """Raised when a selected custom provider has no usable credential."""


def _run_api_key_command(command: str) -> str:
    try:
        argv = shlex.split(command, posix=os.name != "nt")
        if not argv:
            return ""
        p = subprocess.run(
            argv,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        if p.returncode != 0:
            return ""
        return (p.stdout or "").strip()[:16384]
    except Exception:
        return ""


def resolve_api_key_value(
    api_key: str,
    provider: str = "",
    *,
    allow_command: bool = True,
) -> str:
    """Resolve models.json apiKey field: secure reference / env / literal / command."""
    if not api_key:
        return ""
    key = str(api_key).strip().strip('"').strip("'")
    if not key:
        return ""
    if key.startswith("__DPAPI__:") or provider:
        try:
            from . import secrets as secretstore

            secured = secretstore.resolve_provider_api_key(key, provider)
            if secured and secured != key:
                key = secured
            elif key.startswith("__DPAPI__:"):
                key = secretstore.resolve_provider_api_key(key, provider)
        except Exception:
            pass
    if key.startswith("!"):
        return _run_api_key_command(key[1:].strip()) if allow_command else ""
    try:
        from . import secrets as secretstore

        env_name = secretstore.referenced_env_name(key)
    except Exception:
        env_name = ""
    if env_name:
        # A syntactically valid environment reference is never a literal key.
        # Returning empty lets callers surface an actionable missing-key error.
        return os.environ.get(env_name, "").strip()
    return key


def provider_runtime_env(provider: str | None) -> dict[str, str]:
    """Resolve one custom provider credential for a Pi child process.

    The real key stays out of models.json and command-line arguments. Built-in
    providers are left to Pi's normal auth and environment resolution.
    """
    provider = (provider or "").strip()
    if not provider:
        return {}
    entry = get_provider_config(provider)
    if not entry:
        return {}

    from . import secrets as secretstore

    raw = str(entry.get("apiKey") or "").strip()
    if not raw or raw.startswith("!"):
        # Pi natively supports !command. Keeping it in models.json avoids
        # putting command output in a process argument or persistent file.
        return {}

    env_name = secretstore.referenced_env_name(raw)
    if not env_name:
        # A legacy/plaintext configuration may not have reached the migration
        # path yet. Pi must see the new reference in models.json; injecting an
        # environment variable alone would leave Pi sending the old marker.
        reference = secretstore.store_provider_api_key(provider, raw)
        env_name = secretstore.referenced_env_name(reference)
        if env_name:
            changed_concurrently = False

            def persist_reference(config: Any) -> dict[str, Any]:
                nonlocal changed_concurrently
                if not isinstance(config, dict):
                    raise ValueError("models.json 顶层必须是对象")
                providers = config.get("providers")
                if not isinstance(providers, dict):
                    raise ValueError("models.json.providers 必须是对象")
                current = providers.get(provider)
                if not isinstance(current, dict):
                    raise ValueError(f"Provider「{provider}」已不存在")
                current_raw = str(current.get("apiKey") or "").strip()
                if current_raw == raw:
                    updated = dict(current)
                    updated["apiKey"] = reference
                    providers = dict(providers)
                    providers[provider] = updated
                    config = dict(config)
                    config["providers"] = providers
                elif current_raw != reference:
                    changed_concurrently = True
                return config

            try:
                storage.update_json(
                    models_path(), {"providers": {}}, persist_reference
                )
            except Exception as exc:
                raise ProviderKeyError(
                    f"Provider「{provider}」的旧 API Key 配置无法迁移到安全引用：{exc}。"
                    "请确认 models.json 可写，然后在 Provider 编辑页重新保存。"
                ) from exc
            if changed_concurrently:
                raise ProviderKeyError(
                    f"Provider「{provider}」在启动时被其他进程修改。请重试启动。"
                )

    if not env_name:
        return {}
    if env_name == secretstore.provider_env_name(provider):
        value = secretstore.get_secret(secretstore.provider_key_name(provider))
        if not value:
            value = os.environ.get(env_name, "")
    else:
        value = os.environ.get(env_name, "")
    if not value:
        raise ProviderKeyError(
            f"Provider「{provider}」引用的环境变量 {env_name} 未设置或安全密钥已丢失。"
            "请在 Provider 编辑页重新填写 API Key 后保存。"
        )
    return {env_name: value}


def all_provider_runtime_env(*, strict: bool = False) -> dict[str, str]:
    """Resolve credentials needed while Pi enumerates all custom models."""
    cfg = load_models_config()
    providers = cfg.get("providers") or {}
    result: dict[str, str] = {}
    for provider in providers if isinstance(providers, dict) else {}:
        try:
            result.update(provider_runtime_env(str(provider)))
        except ProviderKeyError:
            if strict:
                raise
    return result


def normalize_openai_base_url(base_url: str) -> str:
    u = (base_url or "").strip().rstrip("/")
    if not u:
        return ""
    # If user passed full chat completions path, strip to /v1 root-ish
    for suffix in ("/chat/completions", "/completions", "/responses", "/messages"):
        if u.lower().endswith(suffix):
            u = u[: -len(suffix)]
            break
    return u.rstrip("/")


def _ssl_context(insecure: bool = False):
    import ssl

    if insecure:
        ctx = ssl._create_unverified_context()
        return ctx
    cafile = None
    try:
        import certifi

        cafile = certifi.where()
    except Exception:
        cafile = None
    if cafile and os.path.isfile(cafile):
        ctx = ssl.create_default_context(cafile=cafile)
    else:
        ctx = ssl.create_default_context()
    try:
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    except Exception:
        pass
    return ctx


def _friendly_fetch_error(exc: BaseException, endpoint: str = "") -> str:
    msg = str(exc)
    low = msg.lower()
    tips: list[str] = []
    if "missing bearer" in low or "unauthorized" in low or "401" in low:
        tips.append("未带上有效 API Key，或 Key 无效/已过期。")
        tips.append("请在「API Key」填入 sk-... 真实密钥，或已存在的环境变量名（如 OPENAI_API_KEY）。")
    if "ssl" in low or "eof occurred" in low or "certificate" in low or "wrong version number" in low:
        tips.append("TLS/SSL 握手失败：常见于网络拦截、公司代理、或直连 api.openai.com 不稳定。")
        tips.append("可尝试：1) 设置系统/用户环境变量 HTTPS_PROXY；2) 改用可访问的中转 Base URL；3) 勾选「忽略 SSL 校验」仅作排查。")
    if "timed out" in low or "timeout" in low:
        tips.append("请求超时：检查网络、代理或 Base URL 是否可达。")
    if "name or service not known" in low or "getaddrinfo failed" in low or "nodename nor servname" in low:
        tips.append("域名解析失败：检查 DNS / 是否需要代理。")
    if "10061" in low or "connection refused" in low:
        tips.append("连接被拒绝：代理地址错误或目标服务未开放。")
    if "proxy" in low:
        tips.append("代理相关错误：检查 HTTP_PROXY / HTTPS_PROXY。")

    header = f"{type(exc).__name__}: {msg}"
    if tips:
        return header + "\n\n排查建议：\n- " + "\n- ".join(tips)
    if endpoint:
        return header + f"\nendpoint: {endpoint}"
    return header


def fetch_remote_models(
    base_url: str,
    api_key: str = "",
    *,
    api: str = "openai-completions",
    timeout: float = 30,
    headers: dict[str, str] | None = None,
    insecure_ssl: bool = False,
    proxy: str = "",
    provider: str = "",
) -> dict[str, Any]:
    """Fetch available models from provider endpoint using baseUrl + apiKey.

    Returns: { ok, models: [{id, name, ...}], endpoint, error, raw_count }
    """
    import urllib.error
    import urllib.request

    base = normalize_openai_base_url(base_url)
    if not base:
        return {"ok": False, "models": [], "endpoint": "", "error": "Base URL 为空", "raw_count": 0}

    raw_key = (api_key or "").strip()
    key = resolve_api_key_value(api_key, provider=provider)
    api = (api or "openai-completions").lower()

    # OpenAI / Anthropic always need a key for /models
    if api in {"openai-completions", "openai-responses", "openai", "anthropic-messages", "anthropic"}:
        if not raw_key:
            return {
                "ok": False,
                "models": [],
                "endpoint": "",
                "error": (
                    "未填写 API Key。\n"
                    "请粘贴真实密钥（如 sk-...），或填写已配置的环境变量名（如 OPENAI_API_KEY）。\n"
                    "空 Key 会返回 HTTP 401：Missing bearer authentication。"
                ),
                "raw_count": 0,
            }
        if not key:
            return {
                "ok": False,
                "models": [],
                "endpoint": "",
                "error": (
                    f"环境变量「{raw_key}」未设置或为空。\n"
                    "请先在系统/用户环境变量中配置，或直接粘贴 API Key 本身。"
                ),
                "raw_count": 0,
            }

    # Build endpoint
    if api in {"openai-completions", "openai-responses", "openai"}:
        endpoint = base + ("/models" if base.endswith("/v1") or base.endswith("/v1beta") else "/v1/models")
        if base.endswith("/models"):
            endpoint = base
        req_headers = {
            "Accept": "application/json",
            "User-Agent": "PiManager/1.0 (+Windows)",
        }
        if key:
            req_headers["Authorization"] = f"Bearer {key}"
    elif api in {"anthropic-messages", "anthropic"}:
        if base.endswith("/v1"):
            endpoint = base + "/models"
        elif base.endswith("/models"):
            endpoint = base
        else:
            endpoint = base.rstrip("/") + "/v1/models"
        req_headers = {
            "Accept": "application/json",
            "User-Agent": "PiManager/1.0 (+Windows)",
            "anthropic-version": "2023-06-01",
        }
        if key:
            req_headers["x-api-key"] = key
    elif api in {"google-generative-ai", "google"}:
        if "key=" in base:
            endpoint = base
        else:
            root = base.rstrip("/")
            if not root.endswith("/models"):
                endpoint = root + "/models"
            else:
                endpoint = root
            if key:
                sep = "&" if "?" in endpoint else "?"
                from urllib.parse import quote
                endpoint = f"{endpoint}{sep}key={quote(key, safe='')}"
        req_headers = {"Accept": "application/json", "User-Agent": "PiManager/1.0 (+Windows)"}
        if not key and "key=" not in endpoint:
            return {
                "ok": False,
                "models": [],
                "endpoint": endpoint,
                "error": "Google 接口需要 API Key（查询参数 key=...）。",
                "raw_count": 0,
            }
    else:
        endpoint = base + ("/models" if base.endswith("/v1") else "/v1/models")
        req_headers = {"Accept": "application/json", "User-Agent": "PiManager/1.0 (+Windows)"}
        if key:
            req_headers["Authorization"] = f"Bearer {key}"

    if headers:
        for k, v in headers.items():
            req_headers[k] = resolve_api_key_value(v) if isinstance(v, str) else str(v)

    proxy = (proxy or "").strip()
    if not proxy:
        try:
            cfg = load_manager_config()
            if cfg.get("proxy_enabled") and cfg.get("proxy_url"):
                proxy = str(cfg.get("proxy_url") or "").strip()
        except Exception:
            proxy = ""
    if not proxy:
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or ""
        ).strip()

    handlers: list[Any] = []
    if proxy:
        handlers.append(
            urllib.request.ProxyHandler(
                {
                    "http": proxy,
                    "https": proxy,
                }
            )
        )
    # Always use our SSL context via HTTPSHandler
    handlers.append(urllib.request.HTTPSHandler(context=_ssl_context(insecure_ssl)))
    opener = urllib.request.build_opener(*handlers)

    try:
        req = urllib.request.Request(endpoint, headers=req_headers, method="GET")
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            status = getattr(resp, "status", 200)
    except urllib.error.HTTPError as e:
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        detail = f"HTTP {e.code}: {e.reason}"
        if err_body:
            detail += f"\n{err_body}"
        friendly = _friendly_fetch_error(Exception(detail), endpoint)
        return {
            "ok": False,
            "models": [],
            "endpoint": endpoint,
            "error": friendly,
            "raw_count": 0,
            "http_status": e.code,
            "proxy": proxy or "",
        }
    except Exception as e:
        return {
            "ok": False,
            "models": [],
            "endpoint": endpoint,
            "error": _friendly_fetch_error(e, endpoint),
            "raw_count": 0,
            "proxy": proxy or "",
        }

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return {
            "ok": False,
            "models": [],
            "endpoint": endpoint,
            "error": f"响应不是 JSON: {body[:200]}",
            "raw_count": 0,
        }

    models: list[dict[str, Any]] = []

    def add_model(mid: str, name: str | None = None, extra: dict | None = None):
        if not mid:
            return
        item = {
            "id": mid,
            "name": name or mid,
            "reasoning": True,
            "input": ["text"],
            "contextWindow": 128000,
            "maxTokens": 32768,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        }
        if extra:
            # common optional fields
            if "context_window" in extra:
                item["contextWindow"] = int(extra["context_window"])
            if "contextWindow" in extra:
                item["contextWindow"] = int(extra["contextWindow"])
            if "max_tokens" in extra:
                item["maxTokens"] = int(extra["max_tokens"])
            if "maxTokens" in extra:
                item["maxTokens"] = int(extra["maxTokens"])
        models.append(item)

    # OpenAI style: { data: [ {id} ] }
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        for m in data["data"]:
            if isinstance(m, dict):
                add_model(str(m.get("id") or m.get("name") or ""), str(m.get("id") or m.get("name") or ""), m)
            elif isinstance(m, str):
                add_model(m)
    # Anthropic style sometimes { data: [...] } same
    # Google: { models: [ { name: "models/xxx", displayName } ] }
    elif isinstance(data, dict) and isinstance(data.get("models"), list):
        for m in data["models"]:
            if not isinstance(m, dict):
                continue
            name = str(m.get("name") or "")
            mid = name.split("/")[-1] if name else str(m.get("displayName") or "")
            display = str(m.get("displayName") or mid)
            add_model(mid, display, m)
    # plain list
    elif isinstance(data, list):
        for m in data:
            if isinstance(m, dict):
                add_model(str(m.get("id") or m.get("name") or ""), None, m)
            elif isinstance(m, str):
                add_model(m)
    else:
        return {
            "ok": False,
            "models": [],
            "endpoint": endpoint,
            "error": f"无法识别模型列表结构，keys={list(data.keys()) if isinstance(data, dict) else type(data)}",
            "raw_count": 0,
        }

    # de-dupe by id
    seen = set()
    uniq = []
    for m in models:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        uniq.append(m)

    return {
        "ok": True,
        "models": uniq,
        "endpoint": endpoint,
        "error": "",
        "raw_count": len(uniq),
        "http_status": status,
    }


def upsert_provider_with_fetched_models(
    name: str,
    *,
    base_url: str,
    api_key: str,
    api: str = "openai-completions",
    models: list[dict[str, Any]] | None = None,
    fetch: bool = True,
    compat: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create/update provider; optionally fetch models first."""
    fetched = None
    if fetch and models is None:
        fetched = fetch_remote_models(base_url, api_key, api=api)
        if not fetched.get("ok"):
            return {"ok": False, "error": fetched.get("error"), "fetched": fetched}
        models = fetched["models"]
    if models is None:
        models = []
    upsert_custom_provider(
        name,
        base_url=base_url,
        api=api,
        api_key=api_key,
        models=models,
        compat=compat
        or {
            "supportsDeveloperRole": False,
            "supportsReasoningEffort": True,
        },
    )
    return {"ok": True, "count": len(models), "fetched": fetched, "name": name}



def get_provider_config(provider: str) -> dict[str, Any] | None:
    """Return custom provider entry from models.json, if any."""
    if not provider:
        return None
    cfg = load_models_config()
    providers = cfg.get("providers") or {}
    entry = providers.get(provider)
    return entry if isinstance(entry, dict) else None


def _http_json_request(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
    timeout: float = 45,
    insecure_ssl: bool = False,
    proxy: str = "",
) -> dict[str, Any]:
    """Low-level HTTP helper with latency measurement."""
    import time
    import urllib.error
    import urllib.request

    req_headers = dict(headers or {})
    proxy = (proxy or "").strip()
    if not proxy:
        try:
            cfg = load_manager_config()
            if cfg.get("proxy_enabled") and cfg.get("proxy_url"):
                proxy = str(cfg.get("proxy_url") or "").strip()
        except Exception:
            proxy = ""
    if not proxy:
        proxy = (
            os.environ.get("HTTPS_PROXY")
            or os.environ.get("https_proxy")
            or os.environ.get("HTTP_PROXY")
            or os.environ.get("http_proxy")
            or ""
        ).strip()

    handlers: list[Any] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    handlers.append(urllib.request.HTTPSHandler(context=_ssl_context(insecure_ssl)))
    opener = urllib.request.build_opener(*handlers)

    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    t0 = time.perf_counter()
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            t1 = time.perf_counter()
            status = getattr(resp, "status", 200)
            text = raw.decode("utf-8", errors="replace")
            return {
                "ok": 200 <= int(status) < 300,
                "status": int(status),
                "body": text,
                "latency_ms": round((t1 - t0) * 1000, 1),
                "bytes": len(raw),
                "proxy": proxy,
                "error": "",
            }
    except urllib.error.HTTPError as e:
        t1 = time.perf_counter()
        err_body = ""
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:800]
        except Exception:
            pass
        return {
            "ok": False,
            "status": int(getattr(e, "code", 0) or 0),
            "body": err_body,
            "latency_ms": round((t1 - t0) * 1000, 1),
            "bytes": len(err_body.encode("utf-8", errors="ignore")),
            "proxy": proxy,
            "error": f"HTTP {e.code}: {e.reason}",
        }
    except Exception as e:
        t1 = time.perf_counter()
        return {
            "ok": False,
            "status": 0,
            "body": "",
            "latency_ms": round((t1 - t0) * 1000, 1),
            "bytes": 0,
            "proxy": proxy,
            "error": _friendly_fetch_error(e, url),
        }


def _extract_reply_preview(api: str, body_text: str) -> str:
    try:
        data = json.loads(body_text or "{}")
    except Exception:
        return (body_text or "")[:120]

    api = (api or "").lower()
    try:
        if api in {"openai-completions", "openai", "openai-responses"} or "choices" in data:
            # chat completions
            choices = data.get("choices") or []
            if choices:
                msg = choices[0].get("message") or {}
                content = msg.get("content")
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("text"):
                            parts.append(str(c["text"]))
                        elif isinstance(c, str):
                            parts.append(c)
                    content = "".join(parts)
                if content:
                    return str(content).strip()[:120]
                if choices[0].get("text"):
                    return str(choices[0]["text"]).strip()[:120]
            # responses API
            if data.get("output_text"):
                return str(data["output_text"]).strip()[:120]
            output = data.get("output") or []
            for item in output:
                if not isinstance(item, dict):
                    continue
                for c in item.get("content") or []:
                    if isinstance(c, dict) and c.get("text"):
                        return str(c["text"]).strip()[:120]
        if api in {"anthropic-messages", "anthropic"}:
            content = data.get("content") or []
            for c in content:
                if isinstance(c, dict) and c.get("type") == "text":
                    return str(c.get("text") or "").strip()[:120]
                if isinstance(c, dict) and c.get("text"):
                    return str(c.get("text") or "").strip()[:120]
        if api in {"google-generative-ai", "google"}:
            cands = data.get("candidates") or []
            if cands:
                parts = cands[0].get("content", {}).get("parts") or []
                texts = [str(p.get("text") or "") for p in parts if isinstance(p, dict)]
                joined = "".join(texts).strip()
                if joined:
                    return joined[:120]
    except Exception:
        pass
    return (body_text or "")[:120]


def test_model_http(
    provider: str,
    model: str,
    *,
    timeout: float = 45,
    insecure_ssl: bool = False,
    proxy: str = "",
    prompt: str = "Reply with exactly: OK",
) -> dict[str, Any]:
    """Test model via provider BaseURL HTTP (custom providers in models.json)."""
    entry = get_provider_config(provider)
    if not entry:
        return {
            "ok": False,
            "available": False,
            "mode": "http",
            "provider": provider,
            "model": model,
            "latency_ms": None,
            "error": f"models.json 中没有自定义 provider「{provider}」，无法走 HTTP 直连测试。可改用 Pi 实测。",
            "preview": "",
            "endpoint": "",
            "http_status": 0,
        }

    base = normalize_openai_base_url(str(entry.get("baseUrl") or ""))
    api = str(entry.get("api") or "openai-completions").lower()
    key = resolve_api_key_value(str(entry.get("apiKey") or ""), provider=provider)
    extra_headers = entry.get("headers") if isinstance(entry.get("headers"), dict) else {}

    if not base:
        return {
            "ok": False,
            "available": False,
            "mode": "http",
            "provider": provider,
            "model": model,
            "latency_ms": None,
            "error": "provider 缺少 baseUrl",
            "preview": "",
            "endpoint": "",
            "http_status": 0,
        }

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "PiManager/1.0 (+model-test)",
    }
    for k, v in (extra_headers or {}).items():
        headers[str(k)] = resolve_api_key_value(str(v)) if isinstance(v, str) else str(v)

    body_obj: dict[str, Any]
    if api in {"openai-completions", "openai"}:
        endpoint = base + ("/chat/completions" if not base.endswith("/chat/completions") else "")
        if base.endswith("/v1") or "/v1/" in base or base.endswith("/v1beta"):
            endpoint = base.rstrip("/") + "/chat/completions"
        headers["Authorization"] = f"Bearer {key}" if key else headers.get("Authorization", "")
        if not key and "Authorization" not in (extra_headers or {}):
            return {
                "ok": False,
                "available": False,
                "mode": "http",
                "provider": provider,
                "model": model,
                "latency_ms": None,
                "error": "缺少 API Key，无法 HTTP 测试",
                "preview": "",
                "endpoint": endpoint,
                "http_status": 0,
            }
        body_obj = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "temperature": 0,
        }
    elif api in {"openai-responses"}:
        endpoint = base.rstrip("/") + "/responses"
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body_obj = {
            "model": model,
            "input": prompt,
            "max_output_tokens": 16,
        }
    elif api in {"anthropic-messages", "anthropic"}:
        endpoint = base.rstrip("/") + "/messages"
        if base.endswith("/messages"):
            endpoint = base
        if key:
            headers["x-api-key"] = key
        headers["anthropic-version"] = headers.get("anthropic-version") or "2023-06-01"
        body_obj = {
            "model": model,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": prompt}],
        }
    elif api in {"google-generative-ai", "google"}:
        # generateContent
        root = base.rstrip("/")
        if root.endswith("/models"):
            endpoint = f"{root}/{model}:generateContent"
        else:
            endpoint = f"{root}/models/{model}:generateContent"
        if key:
            sep = "&" if "?" in endpoint else "?"
            endpoint = f"{endpoint}{sep}key={key}"
        body_obj = {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": 16, "temperature": 0},
        }
    else:
        endpoint = base.rstrip("/") + "/chat/completions"
        if key:
            headers["Authorization"] = f"Bearer {key}"
        body_obj = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "temperature": 0,
        }

    payload = json.dumps(body_obj, ensure_ascii=False).encode("utf-8")
    result = _http_json_request(
        endpoint,
        method="POST",
        headers=headers,
        body=payload,
        timeout=timeout,
        insecure_ssl=insecure_ssl,
        proxy=proxy,
    )
    preview = _extract_reply_preview(api, result.get("body") or "") if result.get("ok") else (result.get("body") or "")[:160]
    available = bool(result.get("ok"))
    err = result.get("error") or ""
    if not available and result.get("body"):
        err = (err + "\n" + str(result.get("body"))[:400]).strip()

    return {
        "ok": available,
        "available": available,
        "mode": "http",
        "provider": provider,
        "model": model,
        "latency_ms": result.get("latency_ms"),
        "error": err if not available else "",
        "preview": preview,
        "endpoint": endpoint,
        "http_status": result.get("status") or 0,
        "proxy": result.get("proxy") or "",
        "api": api,
    }


def test_model_via_pi(
    provider: str,
    model: str,
    *,
    timeout: float = 90,
    prompt: str = "只回复两个字符：OK",
    workdir: str | None = None,
) -> dict[str, Any]:
    """Test model availability via official pi -p (covers OAuth/built-in providers)."""
    import time

    t0 = time.perf_counter()
    try:
        code, out, err = run_pi_print(
            prompt,
            workdir=workdir or str(user_home()),
            provider=provider,
            model=model,
            thinking="off",
            timeout=timeout,
        )
        t1 = time.perf_counter()
    except Exception as e:
        t1 = time.perf_counter()
        return {
            "ok": False,
            "available": False,
            "mode": "pi",
            "provider": provider,
            "model": model,
            "latency_ms": round((t1 - t0) * 1000, 1),
            "error": str(e),
            "preview": "",
            "endpoint": "pi -p",
            "http_status": 0,
            "returncode": -1,
        }

    text = (out or "").strip()
    err_text = (err or "").strip()
    # Consider available if exit 0 and some non-empty model output
    available = code == 0 and bool(text)
    # Some pi versions write assistant text only to stdout
    if code == 0 and not text and err_text and "error" not in err_text.lower():
        text = err_text
        available = True

    combined_err = ""
    if not available:
        combined_err = err_text or text or f"pi 退出码 {code}"
        low = combined_err.lower()
        if "auth" in low or "login" in low or "api key" in low or "unauthorized" in low:
            combined_err += "\n提示：该 provider 可能未登录/未配置 API Key。"

    return {
        "ok": available,
        "available": available,
        "mode": "pi",
        "provider": provider,
        "model": model,
        "latency_ms": round((t1 - t0) * 1000, 1),
        "error": combined_err if not available else "",
        "preview": (text[:160] if text else ""),
        "endpoint": "pi -p --no-session --approve",
        "http_status": 0,
        "returncode": code,
        "stderr": err_text[:300],
    }


def test_model(
    provider: str,
    model: str,
    *,
    mode: str = "auto",
    timeout: float = 60,
    insecure_ssl: bool = False,
    proxy: str = "",
    workdir: str | None = None,
) -> dict[str, Any]:
    """Test one model. mode: auto|http|pi

    auto: custom provider with baseUrl -> HTTP first; on failure also try pi if installed.
          otherwise pi only.
    """
    mode = (mode or "auto").lower().strip()
    entry = get_provider_config(provider)

    if mode == "http":
        return test_model_http(
            provider, model, timeout=timeout, insecure_ssl=insecure_ssl, proxy=proxy
        )
    if mode == "pi":
        return test_model_via_pi(provider, model, timeout=timeout, workdir=workdir)

    # auto
    if entry and entry.get("baseUrl"):
        http_res = test_model_http(
            provider, model, timeout=min(timeout, 45), insecure_ssl=insecure_ssl, proxy=proxy
        )
        if http_res.get("available"):
            http_res["note"] = "HTTP 直连成功"
            return http_res
        # fallback to pi for better diagnosis / oauth hybrids
        if find_pi_command():
            pi_res = test_model_via_pi(
                provider, model, timeout=timeout, workdir=workdir
            )
            pi_res["http_fallback"] = http_res
            if pi_res.get("available"):
                pi_res["note"] = "HTTP 失败但 Pi 实测成功"
            else:
                pi_res["note"] = "HTTP 与 Pi 均失败"
                # prefer richer error
                if http_res.get("error") and not pi_res.get("error"):
                    pi_res["error"] = http_res.get("error")
                elif http_res.get("error"):
                    pi_res["error"] = (
                        f"[HTTP] {http_res.get('error')}\n[Pi] {pi_res.get('error')}"
                    )
            return pi_res
        return http_res

    return test_model_via_pi(provider, model, timeout=timeout, workdir=workdir)


def test_models_batch(
    pairs: list[tuple[str, str]],
    *,
    mode: str = "auto",
    timeout: float = 60,
    insecure_ssl: bool = False,
    proxy: str = "",
    workdir: str | None = None,
) -> list[dict[str, Any]]:
    """Test multiple provider/model pairs sequentially."""
    results: list[dict[str, Any]] = []
    for provider, model in pairs:
        try:
            results.append(
                test_model(
                    provider,
                    model,
                    mode=mode,
                    timeout=timeout,
                    insecure_ssl=insecure_ssl,
                    proxy=proxy,
                    workdir=workdir,
                )
            )
        except Exception as e:
            results.append(
                {
                    "ok": False,
                    "available": False,
                    "mode": mode,
                    "provider": provider,
                    "model": model,
                    "latency_ms": None,
                    "error": str(e),
                    "preview": "",
                    "endpoint": "",
                    "http_status": 0,
                }
            )
    return results


def format_test_summary(result: dict[str, Any]) -> str:
    """Human-readable one-line summary for table/status."""
    key = f"{result.get('provider')}/{result.get('model')}"
    if result.get("available"):
        lat = result.get("latency_ms")
        lat_s = f"{lat:.0f} ms" if isinstance(lat, (int, float)) else "?"
        mode = result.get("mode") or ""
        return f"可用 · {lat_s} · {mode}"
    err = (result.get("error") or "失败").splitlines()[0][:80]
    return f"不可用 · {err}"

