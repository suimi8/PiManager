# -*- coding: utf-8 -*-
"""Extra features backend for Pi Manager."""
from __future__ import annotations

import concurrent.futures
import base64
import json
import os
import time
import zipfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from . import core
from . import secrets as secretstore
from . import storage

APP_VERSION = "1.6.0"
APP_NAME = "Pi Manager"
# Optional remote version manifest (JSON: {"version":"x.y.z","notes":"...","url":"..."})
UPDATE_MANIFEST_URL = ""  # user can set in manager config


def history_path() -> Path:
    return core.pi_agent_dir() / "pi-manager-test-history.json"


def health_path() -> Path:
    return core.pi_agent_dir() / "pi-manager-health.json"


def load_history() -> list[dict[str, Any]]:
    data = core.load_json(history_path(), [])
    return data if isinstance(data, list) else []


def save_history(items: list[dict[str, Any]]) -> None:
    # keep last 500
    core.save_json(history_path(), items[-500:])


def append_test_history(results: list[dict[str, Any]]) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    additions = []
    for r in results:
        additions.append(
            {
                "time": ts,
                "provider": r.get("provider"),
                "model": r.get("model"),
                "available": bool(r.get("available")),
                "latency_ms": r.get("latency_ms"),
                "mode": r.get("mode"),
                "error": (str(r.get("error") or "").splitlines()[0][:200] if not r.get("available") else ""),
                "preview": (r.get("preview") or "")[:120],
            }
        )

    def update(current: Any) -> list[dict[str, Any]]:
        hist = current if isinstance(current, list) else []
        return [*hist, *additions][-500:]

    storage.update_json(history_path(), [], update)


def history_for_model(provider: str, model: str, limit: int = 30) -> list[dict[str, Any]]:
    key_p, key_m = provider, model
    rows = [h for h in load_history() if h.get("provider") == key_p and h.get("model") == key_m]
    return rows[-limit:]


def get_proxy_settings() -> dict[str, Any]:
    cfg = core.load_manager_config()
    enabled = bool(cfg.get("proxy_enabled"))
    url = str(cfg.get("proxy_url") or "").strip()
    # also surface env
    env = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    ).strip()
    effective = url if enabled and url else env
    return {
        "enabled": enabled,
        "url": url,
        "env": env,
        "effective": effective,
    }


def set_proxy_settings(enabled: bool, url: str) -> dict[str, Any]:
    cfg = core.load_manager_config()
    cfg["proxy_enabled"] = bool(enabled)
    cfg["proxy_url"] = (url or "").strip()
    core.save_manager_config(cfg)
    # apply to process env for child pi processes when enabled
    apply_proxy_env()
    return get_proxy_settings()


def apply_proxy_env() -> None:
    ps = get_proxy_settings()
    eff = ps.get("effective") or ""
    if eff:
        os.environ["HTTPS_PROXY"] = eff
        os.environ["HTTP_PROXY"] = eff
        os.environ["https_proxy"] = eff
        os.environ["http_proxy"] = eff
    # do not delete user env if manager proxy disabled — leave system env alone


def effective_proxy(explicit: str = "") -> str:
    if (explicit or "").strip():
        return explicit.strip()
    return str(get_proxy_settings().get("effective") or "")


def get_test_concurrency() -> int:
    cfg = core.load_manager_config()
    try:
        n = int(cfg.get("test_concurrency") or 3)
    except Exception:
        n = 3
    return max(1, min(n, 8))


def set_test_concurrency(n: int) -> None:
    cfg = core.load_manager_config()
    cfg["test_concurrency"] = max(1, min(int(n), 8))
    core.save_manager_config(cfg)


def test_models_batch_concurrent(
    pairs: list[tuple[str, str]],
    *,
    mode: str = "auto",
    timeout: float = 60,
    insecure_ssl: bool = False,
    proxy: str = "",
    workdir: str | None = None,
    max_workers: int | None = None,
    on_one: Callable[[dict[str, Any]], None] | None = None,
    append_history_each: bool = False,
) -> list[dict[str, Any]]:
    """Concurrent model tests with ordered result list matching input pairs.

    on_one: called as each model finishes (from worker threads).
    append_history_each: write history per result; otherwise batch-append at end.
    """
    if not pairs:
        return []
    apply_proxy_env()
    workers = max_workers or get_test_concurrency()
    proxy = effective_proxy(proxy)

    def one(idx_pair: tuple[int, tuple[str, str]]) -> tuple[int, dict[str, Any]]:
        idx, (provider, model) = idx_pair
        try:
            res = core.test_model(
                provider,
                model,
                mode=mode,
                timeout=timeout,
                insecure_ssl=insecure_ssl,
                proxy=proxy,
                workdir=workdir,
            )
        except Exception as e:
            res = {
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
        if append_history_each:
            try:
                append_test_history([res])
            except Exception:
                pass
        if on_one:
            try:
                on_one(res)
            except Exception:
                pass
        return idx, res

    results: list[dict[str, Any] | None] = [None] * len(pairs)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(one, (i, p)) for i, p in enumerate(pairs)]
        for fut in concurrent.futures.as_completed(futs):
            idx, res = fut.result()
            results[idx] = res
    out = [r if r is not None else {"ok": False, "available": False, "error": "missing"} for r in results]
    if not append_history_each:
        try:
            append_test_history(out)
        except Exception:
            pass
    return out


def secure_existing_keys() -> dict[str, Any]:
    """Migrate plaintext provider keys into the platform secret store."""
    cfg = core.load_models_config()
    providers = cfg.get("providers") or {}
    if not isinstance(providers, dict):
        return {"ok": False, "count": 0}
    new_providers = secretstore.migrate_plaintext_keys(providers)
    cfg["providers"] = new_providers
    core.save_models_config(cfg)
    mgr = core.load_manager_config()
    mgr["secure_keys"] = True
    core.save_manager_config(mgr)
    return {"ok": True, "count": len(new_providers), "secrets": secretstore.list_secret_names()}


def resolve_api_key_for_provider(provider: str, api_key_field: str = "") -> str:
    raw = api_key_field
    if not raw:
        entry = core.get_provider_config(provider) or {}
        raw = str(entry.get("apiKey") or "")
    resolved = secretstore.resolve_provider_api_key(raw, provider)
    return core.resolve_api_key_value(resolved)


_BUNDLE_AAD = b"PiManagerConfigSecrets:v1"
_BUNDLE_KDF_ITERATIONS = 600_000
_MAX_ZIP_MEMBERS = 128
_MAX_ZIP_MEMBER_BYTES = 5 * 1024 * 1024
_MAX_ZIP_TOTAL_BYTES = 20 * 1024 * 1024


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _bundle_key(password: str, salt: bytes, iterations: int) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    return PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    ).derive(password.encode("utf-8"))


def _encrypt_bundle_secrets(secrets: dict[str, str], password: str) -> dict[str, Any]:
    if len(password) < 10:
        raise ValueError("密钥包密码至少需要 10 个字符")
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt, nonce = os.urandom(16), os.urandom(12)
    plaintext = _json_bytes({"secrets": secrets})
    ciphertext = AESGCM(_bundle_key(password, salt, _BUNDLE_KDF_ITERATIONS)).encrypt(
        nonce, plaintext, _BUNDLE_AAD
    )
    return {
        "version": 1,
        "cipher": "AES-256-GCM",
        "kdf": "PBKDF2-HMAC-SHA256",
        "iterations": _BUNDLE_KDF_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def _decrypt_bundle_secrets(payload: dict[str, Any], password: str) -> dict[str, str]:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    if payload.get("version") != 1 or payload.get("cipher") != "AES-256-GCM":
        raise ValueError("不支持的密钥包加密格式")
    try:
        iterations = int(payload["iterations"])
        if not 100_000 <= iterations <= 2_000_000:
            raise ValueError("invalid KDF iterations")
        salt = base64.b64decode(str(payload["salt"]), validate=True)
        nonce = base64.b64decode(str(payload["nonce"]), validate=True)
        ciphertext = base64.b64decode(str(payload["ciphertext"]), validate=True)
        plaintext = AESGCM(_bundle_key(password, salt, iterations)).decrypt(
            nonce, ciphertext, _BUNDLE_AAD
        )
        decoded = json.loads(plaintext.decode("utf-8"))
        secrets = decoded.get("secrets") if isinstance(decoded, dict) else None
        if not isinstance(secrets, dict):
            raise ValueError("invalid secrets payload")
        return {str(name): str(value) for name, value in secrets.items()}
    except Exception as exc:
        raise ValueError("密钥包密码错误或文件已被篡改") from exc


def _export_safe_models() -> dict[str, Any]:
    models = json.loads(json.dumps(core.load_models_config()))
    for entry in (models.get("providers") or {}).values():
        if not isinstance(entry, dict):
            continue
        headers = entry.get("headers")
        if not isinstance(headers, dict):
            continue
        for key, value in list(headers.items()):
            field = str(key).lower()
            raw = str(value or "")
            is_reference = raw.startswith(("$", "!"))
            if any(x in field for x in ("authorization", "api-key", "apikey", "token", "secret", "cookie")) and not is_reference:
                headers[key] = ""
    return models


def _export_safe_manager() -> dict[str, Any]:
    manager = json.loads(json.dumps(core.load_manager_config()))
    proxy = str(manager.get("proxy_url") or "")
    try:
        parsed = urlsplit(proxy)
        if parsed.username is not None or parsed.password is not None:
            host = parsed.hostname or ""
            if parsed.port:
                host += f":{parsed.port}"
            manager["proxy_url"] = urlunsplit(
                (parsed.scheme, host, parsed.path, parsed.query, parsed.fragment)
            )
    except ValueError:
        manager["proxy_url"] = ""
    return manager


def export_config_bundle(
    dest_path: str,
    *,
    include_secrets: bool = False,
    password: str = "",
) -> str:
    """Export a validated config ZIP; secret values are always authenticated-encrypted."""
    dest = Path(dest_path)
    if dest.suffix.lower() != ".zip":
        dest = dest.with_suffix(".zip")
    core.ensure_agent_dir()
    entries: dict[str, bytes] = {
        "settings.json": _json_bytes(core.load_settings()),
        "models.json": _json_bytes(_export_safe_models()),
        "pi-manager.json": _json_bytes(_export_safe_manager()),
    }
    agents = core.agents_md_path()
    if agents.exists() and agents.is_file():
        entries["AGENTS.md"] = agents.read_bytes()
    themes = core.pi_agent_dir() / "themes"
    if themes.exists():
        for theme in themes.glob("*.json"):
            if theme.is_file() and theme.stat().st_size <= _MAX_ZIP_MEMBER_BYTES:
                entries[f"themes/{theme.name}"] = theme.read_bytes()

    meta = {
        "app": APP_NAME,
        "version": APP_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "include_secrets": include_secrets,
        "secrets_encrypted": include_secrets,
    }
    entries["export-meta.json"] = _json_bytes(meta)
    if include_secrets:
        values = {}
        for name in secretstore.list_secret_names():
            value = secretstore.get_secret(name)
            if value:
                values[name] = value
        entries["secrets.enc.json"] = _json_bytes(
            _encrypt_bundle_secrets(values, password)
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(f".{dest.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)
    return str(dest)


def _read_bundle(src: Path) -> dict[str, bytes]:
    allowed_roots = {
        "settings.json",
        "models.json",
        "pi-manager.json",
        "AGENTS.md",
        "export-meta.json",
        "secrets.enc.json",
    }
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(src, "r") as zf:
        infos = zf.infolist()
        if len(infos) > _MAX_ZIP_MEMBERS:
            raise ValueError("ZIP 文件成员过多")
        total = 0
        for info in infos:
            name = info.filename.replace("\\", "/")
            path = PurePosixPath(name)
            if info.is_dir():
                continue
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise ValueError(f"ZIP 包含非法路径: {info.filename}")
            allowed = name in allowed_roots or (
                len(path.parts) == 2
                and path.parts[0] == "themes"
                and path.suffix.lower() == ".json"
            )
            if not allowed:
                raise ValueError(f"ZIP 包含不允许的文件: {info.filename}")
            if info.file_size > _MAX_ZIP_MEMBER_BYTES:
                raise ValueError(f"ZIP 成员过大: {info.filename}")
            total += info.file_size
            if total > _MAX_ZIP_TOTAL_BYTES:
                raise ValueError("ZIP 解压后总大小超过限制")
            if name in files:
                raise ValueError(f"ZIP 包含重复文件: {name}")
            content = zf.read(info)
            if len(content) != info.file_size:
                raise ValueError(f"ZIP 成员长度异常: {name}")
            files[name] = content
    return files


def bundle_contains_secrets(zip_path: str) -> bool:
    src = Path(zip_path)
    if not src.exists():
        return False
    try:
        return "secrets.enc.json" in _read_bundle(src)
    except Exception:
        return False


def _parse_bundle_json(files: dict[str, bytes], name: str) -> dict[str, Any] | None:
    if name not in files:
        return None
    try:
        value = json.loads(files[name].decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"{name} 不是有效 UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} 顶层必须是 JSON 对象")
    return value


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _validate_models(models: dict[str, Any]) -> list[str]:
    providers = models.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("models.json.providers 必须是对象")
    commands: list[str] = []
    for name, entry in providers.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ValueError("models.json Provider 条目无效")
        if "models" in entry and not isinstance(entry["models"], list):
            raise ValueError(f"Provider {name} 的 models 必须是数组")
        if "apiKey" in entry and not isinstance(entry["apiKey"], str):
            raise ValueError(f"Provider {name} 的 apiKey 必须是字符串")
        if str(entry.get("apiKey") or "").strip().startswith("!"):
            commands.append(name)
        headers = entry.get("headers", {})
        if headers and not isinstance(headers, dict):
            raise ValueError(f"Provider {name} 的 headers 必须是对象")
        if isinstance(headers, dict) and any(
            isinstance(value, str) and value.strip().startswith("!")
            for value in headers.values()
        ):
            commands.append(name)
    return sorted(set(commands))


def _secret_snapshot() -> dict[str, str]:
    return {
        name: secretstore.get_secret(name)
        for name in secretstore.list_secret_names()
    }


def _restore_secret_snapshot(snapshot: dict[str, str]) -> None:
    for name in set(secretstore.list_secret_names()) - set(snapshot):
        secretstore.delete_secret(name)
    for name, value in snapshot.items():
        secretstore.set_secret(name, value)


def import_config_bundle(
    zip_path: str,
    *,
    restore_secrets: bool = False,
    password: str = "",
    allow_commands: bool = False,
) -> dict[str, Any]:
    """Validate an entire bundle before applying it, then commit transactionally."""
    src = Path(zip_path)
    if not src.exists() or not src.is_file():
        return {"ok": False, "error": "文件不存在"}
    try:
        files = _read_bundle(src)
        settings = _parse_bundle_json(files, "settings.json")
        models = _parse_bundle_json(files, "models.json")
        manager = _parse_bundle_json(files, "pi-manager.json")
        commands = _validate_models(models) if models is not None else []
        if commands and not allow_commands:
            return {
                "ok": False,
                "error": "导入包包含可执行的 !command 密钥配置",
                "requires_command_confirmation": True,
                "command_providers": commands,
            }
        theme_data: dict[str, dict[str, Any]] = {}
        for name in files:
            if name.startswith("themes/"):
                parsed = _parse_bundle_json(files, name)
                if parsed is not None:
                    theme_data[name] = parsed
        imported_secrets: dict[str, str] = {}
        if restore_secrets and "secrets.enc.json" in files:
            encrypted = _parse_bundle_json(files, "secrets.enc.json") or {}
            imported_secrets = _decrypt_bundle_secrets(encrypted, password)
        elif restore_secrets and "secrets.enc.json" not in files:
            raise ValueError("配置包不包含加密密钥")

        core.ensure_agent_dir()
        writes: dict[Path, bytes] = {}
        if settings is not None:
            writes[core.settings_path()] = _json_bytes(settings)
        if models is not None:
            writes[core.models_path()] = _json_bytes(models)
        if manager is not None:
            writes[core.manager_config_path()] = _json_bytes(manager)
        if "AGENTS.md" in files:
            files["AGENTS.md"].decode("utf-8")
            writes[core.agents_md_path()] = files["AGENTS.md"]
        for name, parsed in theme_data.items():
            writes[core.pi_agent_dir() / name] = _json_bytes(parsed)

        restored: list[str] = []
        with ExitStack() as file_locks:
            for path in sorted(writes, key=lambda item: str(item.resolve())):
                file_locks.enter_context(storage.locked(path))
            backups = {
                path: path.read_bytes() if path.exists() else None
                for path in writes
            }
            secret_backup = _secret_snapshot()
            try:
                for name, value in imported_secrets.items():
                    secretstore.set_secret(name, value)
                if models is not None:
                    models["providers"] = secretstore.migrate_plaintext_keys(
                        models.get("providers") or {}
                    )
                    writes[core.models_path()] = _json_bytes(models)
                for path, content in writes.items():
                    _atomic_replace_bytes(path, content)
                restored = [
                    name
                    for name in ("settings.json", "models.json", "pi-manager.json", "AGENTS.md")
                    if name in files
                ]
                if theme_data:
                    restored.append("themes/")
                if imported_secrets:
                    restored.append("secrets")
            except Exception:
                for path, original in backups.items():
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        _atomic_replace_bytes(path, original)
                _restore_secret_snapshot(secret_backup)
                raise
        return {"ok": True, "restored": restored}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def run_self_check() -> list[dict[str, Any]]:
    """Return list of {name, ok, detail, level}."""
    checks: list[dict[str, Any]] = []

    def add(name: str, ok: bool, detail: str, level: str = "info"):
        checks.append({"name": name, "ok": ok, "detail": detail, "level": level if ok else "warn"})

    # Pi installed
    pi = core.find_pi_command()
    ver = core.get_installed_pi_version() or core.get_pi_version()
    add("Pi CLI", bool(pi), f"{pi or '未找到'} | 版本 {ver or '?'}", "error" if not pi else "info")

    # update available?
    try:
        info = core.needs_pi_install_or_update()
        if info.get("missing") or info.get("outdated"):
            add("Pi 更新", False, info.get("message") or "建议更新", "warn")
        else:
            add("Pi 更新", True, "已是较新版本或无法检查线上版本")
    except Exception as e:
        add("Pi 更新", True, f"跳过：{e}")

    # default model
    p, m, t = core.get_default_model()
    add("默认模型", bool(p and m), f"{p}/{m} thinking={t}" if p else "未设置", "warn" if not (p and m) else "info")

    # config dir
    d = core.pi_agent_dir()
    add("配置目录", d.exists(), str(d))

    # models.json
    models = core.load_models_config()
    provs = models.get("providers") or {}
    add("自定义 Provider", True, f"{len(provs)} 个")

    # proxy
    ps = get_proxy_settings()
    add(
        "代理",
        True,
        f"启用={ps['enabled']} url={ps['url'] or '—'} 环境={ps['env'] or '—'} 生效={ps['effective'] or '无'}",
    )

    # secrets
    names = secretstore.list_secret_names()
    add("安全密钥库", True, f"{len(names)} 条（{secretstore.backend_description()}）")

    # language
    add("语言偏好", True, core.get_language())

    # workdir last
    mgr = core.load_manager_config()
    wd = mgr.get("last_workdir") or ""
    add("最近工作目录", bool(wd), str(wd) or "—")

    # network quick (optional lightweight)
    try:
        import urllib.request

        t0 = time.perf_counter()
        req = urllib.request.Request("https://www.baidu.com", method="GET", headers={"User-Agent": "PiManager"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            _ = resp.status
        ms = round((time.perf_counter() - t0) * 1000)
        add("基础网络", True, f"连通，延迟约 {ms} ms")
    except Exception as e:
        add("基础网络", False, f"异常：{e}", "warn")

    add("Pi Manager 版本", True, APP_VERSION)
    return checks


def check_manager_update() -> dict[str, Any]:
    """Check remote manifest if configured; always returns local version."""
    cfg = core.load_manager_config()
    url = str(cfg.get("update_manifest_url") or UPDATE_MANIFEST_URL or "").strip()
    local = APP_VERSION
    result = {
        "ok": True,
        "local": local,
        "remote": None,
        "has_update": False,
        "notes": "",
        "url": url,
        "message": f"当前版本 {local}",
    }
    cfg["last_manager_update_check"] = datetime.now().isoformat(timespec="seconds")
    core.save_manager_config(cfg)
    if not url:
        result["message"] = f"当前版本 {local}（未配置 update_manifest_url，跳过远程检查）"
        return result
    try:
        import urllib.request

        req = urllib.request.Request(url, headers={"User-Agent": f"PiManager/{local}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        data = json.loads(body)
        remote = str(data.get("version") or "")
        result["remote"] = remote
        result["notes"] = str(data.get("notes") or "")
        result["download"] = data.get("url") or data.get("download") or ""
        if remote and core.parse_semver(remote) > core.parse_semver(local):
            result["has_update"] = True
            result["message"] = f"发现新版本 {remote}（当前 {local}）"
        else:
            result["message"] = f"已是最新（本地 {local}，远程 {remote or '—'}）"
    except Exception as e:
        result["ok"] = False
        result["message"] = f"检查失败：{e}"
    return result


def load_health() -> dict[str, Any]:
    return core.load_json(health_path(), {"models": {}, "updated_at": ""})


def save_health(data: dict[str, Any]) -> None:
    core.save_json(health_path(), data)


def collect_model_pairs(scope: str = "favorites", selected: list[tuple[str, str]] | None = None) -> list[tuple[str, str]]:
    """scope: favorites|default|custom|all_listed|selected"""
    scope = (scope or "favorites").lower().strip()
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(p: str, m: str):
        p, m = (p or "").strip(), (m or "").strip()
        if not p or not m:
            return
        key = f"{p}/{m}"
        if key in seen:
            return
        seen.add(key)
        pairs.append((p, m))

    if scope == "selected":
        for p, m in selected or []:
            add(p, m)
        return pairs

    if scope == "default":
        p, m, _ = core.get_default_model()
        add(p, m)
        return pairs

    if scope == "favorites":
        mgr = core.load_manager_config()
        for key in mgr.get("favorites") or []:
            if "/" in str(key):
                p, m = str(key).split("/", 1)
                add(p, m)
        if not pairs:
            p, m, _ = core.get_default_model()
            add(p, m)
        return pairs

    if scope == "custom":
        cfg = core.load_models_config()
        for name, entry in (cfg.get("providers") or {}).items():
            if not isinstance(entry, dict):
                continue
            models = entry.get("models") or []
            if not models:
                continue
            # test up to first 5 models per provider for batch health
            for item in models[:8]:
                mid = item.get("id") if isinstance(item, dict) else str(item)
                add(str(name), str(mid))
        return pairs

    if scope == "all_listed":
        try:
            for mi in core.list_models():
                add(mi.provider, mi.model)
        except Exception:
            pass
        return pairs

    # fallback favorites
    return collect_model_pairs("favorites")


def _health_entry_from_result(r: dict[str, Any], *, scope: str, ts: str) -> dict[str, Any]:
    return {
        "available": bool(r.get("available")),
        "latency_ms": r.get("latency_ms"),
        "mode": r.get("mode"),
        "error": (
            str(r.get("error") or "").splitlines()[0][:200]
            if not r.get("available")
            else (r.get("preview") or "")[:120]
        ),
        "checked_at": ts,
        "scope": scope,
    }


def run_health_check(
    pairs: list[tuple[str, str]] | None = None,
    *,
    mode: str = "auto",
    scope: str = "favorites",
    selected: list[tuple[str, str]] | None = None,
    on_one: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if pairs is None:
        pairs = collect_model_pairs(scope, selected=selected)
    if not pairs:
        return {"ok": False, "results": [], "health": load_health(), "error": "没有可检查的模型（请先收藏、设默认或选择范围）"}

    def _on_one(res: dict[str, Any]):
        if on_one:
            try:
                on_one(res)
            except Exception:
                pass

    results = test_models_batch_concurrent(
        pairs,
        mode=mode,
        timeout=45,
        max_workers=get_test_concurrency(),
        on_one=_on_one,
        append_history_each=False,
    )
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def update_health(current: Any) -> dict[str, Any]:
        health = current if isinstance(current, dict) else {}
        models = health.get("models")
        if not isinstance(models, dict):
            models = {}
        for res in results:
            key = f"{res.get('provider')}/{res.get('model')}"
            models[key] = _health_entry_from_result(res, scope=scope, ts=ts)
        health["models"] = models
        health["updated_at"] = ts
        health["last_scope"] = scope
        return health

    health = storage.update_json(
        health_path(),
        {"models": {}, "updated_at": ""},
        update_health,
    )
    return {"ok": True, "results": results, "health": health, "scope": scope, "count": len(pairs)}


def session_delete(path: str) -> bool:
    p = Path(path)
    if p.exists() and p.is_file():
        p.unlink()
        return True
    return False


def session_rename(path: str, new_name: str) -> str:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("名称为空")
    if not Path(new_name).suffix:
        new_name = new_name + p.suffix
    dest = p.with_name(new_name)
    if dest.exists():
        raise FileExistsError(str(dest))
    p.rename(dest)
    return str(dest)


def list_sessions_filtered(limit: int = 100, workdir_substr: str = "", name_substr: str = "") -> list[dict[str, str]]:
    rows = core.list_sessions(limit=max(limit, 200))
    wd = (workdir_substr or "").lower().strip()
    nm = (name_substr or "").lower().strip()
    out = []
    for r in rows:
        blob = f"{r.get('path','')} {r.get('folder','')} {r.get('name','')}".lower()
        if wd and wd not in blob:
            continue
        if nm and nm not in (r.get("name") or "").lower():
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def chat_once(
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    workdir: str | None = None,
    timeout: float = 180,
) -> dict[str, Any]:
    apply_proxy_env()
    t0 = time.perf_counter()
    code, out, err = core.run_pi_print(
        prompt,
        workdir=workdir or str(core.user_home()),
        provider=provider,
        model=model,
        thinking="off",
        timeout=timeout,
    )
    ms = round((time.perf_counter() - t0) * 1000, 1)
    return {
        "ok": code == 0,
        "returncode": code,
        "stdout": out,
        "stderr": err,
        "latency_ms": ms,
        "provider": provider,
        "model": model,
    }
