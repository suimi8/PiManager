# -*- coding: utf-8 -*-
"""Extra features backend for Pi Manager."""
from __future__ import annotations

import concurrent.futures
import base64
import json
import os
import stat
import threading
import time
import zipfile
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from . import core
from . import secrets as secretstore
from . import storage

APP_VERSION = "1.8.7"
APP_NAME = "Pi Manager"
# Optional remote version manifest (JSON: {"version":"x.y.z","notes":"...","url":"..."})
# 未配置时自动回退 GitHub Releases API
UPDATE_MANIFEST_URL = ""  # user can set in manager config
GITHUB_REPO = "suimi8/PiManager"
GITHUB_RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
GITHUB_RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"


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


def _validate_proxy_url(url: str) -> str:
    """Validate a proxy URL; return an error message, or "" when acceptable."""
    url = (url or "").strip()
    if not url:
        return ""
    return core.validate_proxy_url(url)


def set_proxy_settings(enabled: bool, url: str) -> dict[str, Any]:
    url = (url or "").strip()
    if url:
        proxy_error = _validate_proxy_url(url)
        if proxy_error:
            raise ValueError(proxy_error)
    cfg = core.load_manager_config()
    cfg["proxy_enabled"] = bool(enabled)
    cfg["proxy_url"] = url
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
    is_cancelled: Callable[[], bool] | None = None,
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
    indexed = iter(enumerate(pairs))
    in_flight: set[concurrent.futures.Future[tuple[int, dict[str, Any]]]] = set()
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        def submit_until_budget() -> None:
            while len(in_flight) < workers * 2 and not (is_cancelled and is_cancelled()):
                try:
                    item = next(indexed)
                except StopIteration:
                    return
                in_flight.add(pool.submit(one, item))

        submit_until_budget()
        while in_flight:
            done, in_flight = concurrent.futures.wait(
                in_flight,
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for fut in done:
                idx, res = fut.result()
                results[idx] = res
            if is_cancelled and is_cancelled():
                for fut in in_flight:
                    fut.cancel()
                break
            submit_until_budget()
    out = [r if r is not None else {"ok": False, "available": False, "error": "missing"} for r in results]
    if not append_history_each:
        try:
            append_test_history(out)
        except Exception:
            pass
    return out


def _shred_file(path: Path) -> bool:
    """先覆盖写零再删除：备份文件里的明文密钥只 unlink 仍可被恢复。"""
    try:
        size = path.stat().st_size
    except OSError:
        return False
    try:
        with open(path, "r+b") as handle:
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        # 覆盖失败（占用 / 权限）也要尽力删除，删不掉再报告失败。
        pass
    try:
        path.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _models_json_holds_plaintext_secret(raw: bytes) -> bool:
    """True 当这份 models.json 快照里还有明文 apiKey / 敏感 Header。

    引用（`$`）、命令（`!`）与 `__DPAPI__:` 历史标记都不是密钥本体。
    """
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except Exception:
        return False
    providers = data.get("providers") if isinstance(data, dict) else None
    if not isinstance(providers, dict):
        return False
    for entry in providers.values():
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("apiKey") or "").strip()
        if key and not key.startswith(("$", "!", "__DPAPI__:")):
            return True
        headers = entry.get("headers")
        if not isinstance(headers, dict):
            continue
        for name, value in headers.items():
            raw_value = str(value or "").strip()
            if (
                raw_value
                and not raw_value.startswith(("$", "!"))
                and secretstore.is_sensitive_header_name(str(name))
            ):
                return True
    return False


def purge_plaintext_key_backups() -> list[str]:
    """擦除 `models.json` 的备份 / 残留临时文件中仍含明文密钥的副本。

    `storage._write_unlocked` 每次写 JSON 都把旧内容轮转进 `<name>.bak.1`
    （并把上一份挤到 `.bak.2`），因此「把明文 Key 安全迁移成引用」这一步反而
    会把迁移前的明文完整复制进备份并永久保留（R2 审计 P1-3，已实证）；
    `os.replace` 失败时还会留下 `.models.json.<pid>...tmp` 全量副本（P3-8）。
    这里在迁移 / 导入这两个「配置刚变更」的时点做自愈：只擦除**确实含明文
    密钥**的副本，正常备份保留，不影响回滚能力。返回被擦除的文件名列表。
    """
    purged: list[str] = []
    try:
        models = core.models_path()
        agent_dir = models.parent
        candidates = list(agent_dir.glob(f"{models.name}.bak.*")) + list(
            agent_dir.glob(f".{models.name}.*.tmp")
        )
    except OSError:
        return purged
    for candidate in candidates:
        try:
            if not candidate.is_file():
                continue
            raw = candidate.read_bytes()
        except OSError:
            continue
        if not _models_json_holds_plaintext_secret(raw):
            continue
        if _shred_file(candidate):
            purged.append(candidate.name)
    return purged


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
    # 迁移刚刚把明文原文轮转进 models.json.bak.1：不擦除的话「安全迁移」等于
    # 把明文永久留在同目录下（P1-3）。
    purged = purge_plaintext_key_backups()
    return {
        "ok": True,
        "count": len(new_providers),
        "secrets": secretstore.list_secret_names(),
        "purged_backups": purged,
    }


def resolve_api_key_for_provider(provider: str, api_key_field: str = "") -> str:
    raw = api_key_field
    if not raw:
        entry = core.get_provider_config(provider) or {}
        raw = str(entry.get("apiKey") or "")
    resolved = secretstore.resolve_provider_api_key(raw, provider)
    return core.resolve_api_key_value(resolved)


_BUNDLE_AAD = b"PiManagerConfigSecrets:v1"
# 复用 secrets 的 KDF 迭代次数，保证 vault 与配置包加密强度一致。
_BUNDLE_KDF_ITERATIONS = secretstore.KDF_ITERATIONS
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


def _strip_plaintext_api_keys(models: dict[str, Any]) -> list[str]:
    """把 providers 中仍为明文的 apiKey 引用化（存入安全存储）。

    迁移失败（vault/keyring 不可用）时置空该字段并返回警告，保证未加密
    导出包永远不携带明文 apiKey。已是环境变量引用或命令引用（`!` 前缀）
    的值原样保留，不触发任何存储写入。返回值为 export-meta.json 的
    warnings 列表内容。
    """
    warnings: list[str] = []
    providers = models.get("providers")
    if not isinstance(providers, dict):
        return warnings
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        raw = entry.get("apiKey")
        key = str(raw or "").strip() if raw is not None else ""
        if not key or key.startswith("!"):
            continue
        env_name = secretstore.referenced_env_name(key)
        if env_name:
            entry["apiKey"] = f"${{{env_name}}}"
            continue
        try:
            entry["apiKey"] = secretstore.store_provider_api_key(str(name), key)
        except Exception:
            # 密钥存储不可用：置空并警告，绝不把明文写进导出包。
            entry["apiKey"] = ""
            warnings.append(
                f"provider {name} 的 apiKey 无法安全引用化（密钥存储不可用），"
                "已从导出中移除"
            )
    return warnings


# settings.json 是官方 Pi 自己的配置文件，PiManager 只读写 defaultProvider /
# defaultModel / defaultThinkingLevel / enabledModels / theme 这类展示型键，但
# 配置包导入历史上对它零校验（只断言「顶层是 dict」）：任何具备可执行语义的键
# （hook / mcpServers / command / apiKeyHelper / env）都会被无校验落盘，用户下次
# 运行 Pi（一个有 shell 权限的编码 agent）即代码执行（R2 审计 P1-4）。
# 这里刻意不做「白名单保留、其余丢弃」——那会静默吃掉用户自己的 Pi 设置，让
# 导出→导入变成有损操作。改为：导出侧剥离这些键（自己导的包仍可原样导回），
# 导入侧命中即整包拒绝并指出键名。
_EXECUTABLE_SETTINGS_MARKERS = (
    "hook",
    "mcpserver",
    "command",
    "shell",
    "exec",
    "helper",
    "interpreter",
)
_EXECUTABLE_SETTINGS_KEYS = ("env", "permissions")


def _executable_settings_keys(settings: dict[str, Any]) -> list[str]:
    """返回 settings.json 中具备可执行 / 授权语义的键名。"""
    hits: list[str] = []
    for key in settings:
        normalized = "".join(ch for ch in str(key).lower() if ch.isalnum())
        if normalized in _EXECUTABLE_SETTINGS_KEYS or any(
            marker in normalized for marker in _EXECUTABLE_SETTINGS_MARKERS
        ):
            hits.append(str(key))
    return sorted(hits)


def _export_safe_settings() -> tuple[dict[str, Any], list[str]]:
    """导出用的 settings.json 副本：剥离可执行语义键，返回 (副本, 警告)。"""
    settings = json.loads(json.dumps(core.load_settings()))
    if not isinstance(settings, dict):
        return {}, []
    dropped = _executable_settings_keys(settings)
    for key in dropped:
        settings.pop(key, None)
    warnings = [
        f"settings.json 的 {key} 键具备可执行语义，已从导出中移除（导入侧一律拒绝）"
        for key in dropped
    ]
    return settings, warnings


def _known_secret_values() -> list[str]:
    """安全存储里当前全部密钥值（用于未加密导出的最后一道闸）。"""
    values: list[str] = []
    for name in secretstore.list_secret_names():
        try:
            value = secretstore.get_secret(name)
        except Exception:
            continue
        # 过短的值参与比对会造成误伤（与 core.redact_secret_values 同口径）。
        if value and len(value) >= 8:
            values.append(value)
    return values


def _assert_no_known_secret_in_entries(entries: dict[str, bytes]) -> None:
    """最后一道闸：导出包的明文成员里不得出现任何已知密钥值。

    承诺 P5（未加密导出不含任何密钥）此前完全依赖 `referenced_env_name` 的判断，
    一旦该判断出错（P1-2 就是实例）明文就直接进了 ZIP。这里在写盘前用安全存储
    里的真实值做一次精确比对，命中即拒绝导出（fail closed），不打印密钥本身。
    """
    values = _known_secret_values()
    if not values:
        return
    for name, content in entries.items():
        if name == "secrets.enc.json":
            continue  # 已是 AES-GCM 密文
        for value in values:
            if value.encode("utf-8") in content:
                raise ValueError(
                    f"导出被中止：{name} 中检测到安全存储里的密钥明文。"
                    "请先在 Provider 编辑页把该字段改为环境变量引用，再重新导出"
                )


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
    safe_models = _export_safe_models()
    export_warnings = _strip_plaintext_api_keys(safe_models)
    safe_settings, settings_warnings = _export_safe_settings()
    export_warnings.extend(settings_warnings)
    entries: dict[str, bytes] = {
        "settings.json": _json_bytes(safe_settings),
        "models.json": _json_bytes(safe_models),
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
    if export_warnings:
        # 新增可选字段：明文 apiKey 被移除时的警告列表（导入侧不读取，
        # 向后兼容）。
        meta["warnings"] = export_warnings
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

    _assert_no_known_secret_in_entries(entries)
    dest.parent.mkdir(parents=True, exist_ok=True)
    temp = dest.with_name(f".{dest.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, content in entries.items():
                zf.writestr(name, content)
        os.replace(temp, dest)
    finally:
        temp.unlink(missing_ok=True)
    # 导出包可能含配置快照，POSIX 下收紧为仅当前用户可读写。
    try:
        os.chmod(dest, 0o600)
    except OSError:
        pass
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
        value = json.loads(files[name].decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"{name} 不是有效 UTF-8 JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{name} 顶层必须是 JSON 对象")
    return value


def _atomic_replace_bytes(path: Path, content: bytes, *, private: bool = False) -> None:
    """以任意 bytes 内容原子替换文件。

    刻意独立于 ``storage._write_unlocked``：storage 面向 JSON——写前会读取
    校验、拒绝覆盖损坏 JSON、并做 ``.bak.1``/``.bak.2`` 备份轮转。本函数写任意
    bytes（导入/恢复配置包时可能是 JSON、也可能是 ``AGENTS.md`` 等纯文本），
    且调用方（``import_config_bundle`` 及回滚路径）已自行管理备份快照，因此不
    做 JSON 解析与备份轮转，以免引入多余耦合或改变现有行为。原子写机制与
    ``storage._write_unlocked`` 的原子写部分一致：``O_EXCL`` 创建临时文件、
    ``0o600`` 初值、保留已有文件的 ``previous_mode``、``flush`` + ``fsync`` +
    ``os.replace``。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mode: int | None = None
    if not private:
        try:
            previous_mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            previous_mode = None
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        # 与 storage.py 的语义一致：新文件一律 0600 初值；已有文件保留
        # previous_mode，绝不因一次重写而放宽权限。
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        handle = os.fdopen(fd, "wb")
        with handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if previous_mode is not None:
            try:
                os.chmod(temp, previous_mode)
            except OSError:
                pass
        os.replace(temp, path)
        if private:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
    finally:
        temp.unlink(missing_ok=True)


def _is_private_or_link_local_host(host: str) -> bool:
    import ipaddress

    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith((".localhost", ".local")):
        return True
    if host.startswith("::ffff:"):
        host = host[7:]
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    # 复用 core 的 IP 分类（private/loopback/link_local/reserved/multicast），
    # 额外补充 core 未覆盖的 is_unspecified（0.0.0.0 / ::）。
    return core._is_private_host(host) or ip.is_unspecified


def _validate_model_base_url(url: str) -> str:
    """Return an error message for an imported provider baseUrl, or "" when OK."""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return "URL 格式不合法"
    if parsed.scheme not in ("http", "https"):
        return "仅支持 http/https 协议"
    if not parsed.hostname:
        return "缺少有效主机名"
    if _is_private_or_link_local_host(str(parsed.hostname)):
        return "不允许指向本地或内网地址"
    return ""


def _is_dpapi_marker(value: Any) -> bool:
    """True 当值是 `__DPAPI__:<provider>` 历史标记（含引号 / 空白包装）。"""
    return core.normalize_config_string(value).startswith("__DPAPI__:")


def _validate_settings(settings: dict[str, Any]) -> None:
    """拒绝携带可执行 / 授权语义键的 settings.json（R2 审计 P1-4）。"""
    hits = _executable_settings_keys(settings)
    if hits:
        raise ValueError(
            "settings.json 含具备可执行或授权语义的键，配置包一律拒绝："
            + "、".join(hits)
            + "。请手动核对后在本机 settings.json 中自行配置"
        )


def _validate_models(models: dict[str, Any]) -> None:
    providers = models.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("models.json.providers 必须是对象")
    for name, entry in providers.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            raise ValueError("models.json Provider 条目无效")
        if "models" in entry and not isinstance(entry["models"], list):
            raise ValueError(f"Provider {name} 的 models 必须是数组")
        if "apiKey" in entry and not isinstance(entry["apiKey"], str):
            raise ValueError(f"Provider {name} 的 apiKey 必须是字符串")
        if core.is_executable_config_value(entry.get("apiKey")):
            raise ValueError(f"Provider {name} 包含已禁用的 !command 凭据")
        if _is_dpapi_marker(entry.get("apiKey")):
            raise ValueError(
                f"Provider {name} 的 apiKey 使用了 __DPAPI__: 历史标记："
                "该标记声明「我的 Key 存在另一个 Provider 名下」，导入时会把你已有"
                "Provider 的真实密钥复制给这个新 Provider（并随后发往它的 baseUrl），"
                "因此配置包一律不接受"
            )
        base_url = str(entry.get("baseUrl") or "")
        if base_url:
            base_error = _validate_model_base_url(base_url)
            if base_error:
                raise ValueError(
                    f"Provider {name} 的 baseUrl 无效：{base_error}；"
                    "如需本地模型服务，请直接在设置中手动添加该模型地址"
                )
        headers = entry.get("headers", {})
        if headers and not isinstance(headers, dict):
            raise ValueError(f"Provider {name} 的 headers 必须是对象")
        if isinstance(headers, dict):
            for header_name, value in headers.items():
                if not isinstance(header_name, str) or not isinstance(value, str):
                    raise ValueError(f"Provider {name} 的 Header 必须是字符串键值")
                if core.is_executable_config_value(value):
                    raise ValueError(
                        f"Provider {name} 的 Header {header_name} 包含已禁用的 !command 凭据"
                    )
                if _is_dpapi_marker(value):
                    raise ValueError(
                        f"Provider {name} 的 Header {header_name} 使用了"
                        " __DPAPI__: 历史标记，配置包一律不接受"
                    )


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


# ---- R1：导入前的高风险变更清单（写盘前逐条确认） ----
#
# `${NAME}` 是官方 Pi 支持的合法 apiKey / Header 写法，`_validate_models` 不能像
# 对待 `__DPAPI__:` 那样一律拒绝——那会打死所有正常使用环境变量的用户。但
# `secrets.resolve_provider_api_key` 与 `secrets.resolve_provider_header_value` 对
# 「引用名 != 本 Provider 自管名」的情况会直接 `os.environ.get(env_name)`，于是一个
# 配置包只要写
#     apiKey: "${OPENAI_API_KEY}"  +  baseUrl: "https://attacker.example/v1"
# 导入后应用就会把**用户环境里的真实 Key** 以 Bearer 发往攻击者。这与已修的
# `__DPAPI__:` 跨 Provider 窃取（P0-2）同属一类「让导入的配置引用本机已有凭据 +
# 把流量指向别处」，但无法靠拒绝某个标记解决，只能在写盘前把差异摆给用户逐条确认。
RISK_NEW_PROVIDER = "new_provider"
RISK_BASE_URL_CHANGE = "base_url_change"
RISK_API_KEY_ENV_REF = "api_key_env_ref"
RISK_HEADER_ENV_REF = "header_env_ref"

_NO_BASE_URL_HINT = "（未指定，沿用 Pi 默认端点）"
_RISK_UNCONFIRMED_ERROR = (
    "配置包含需要逐条确认的高风险变更（新增 Provider / Base URL 变更 / 凭据引用"
    "本机环境变量），但调用方没有提供确认入口，已整包拒绝，本机配置未做任何修改"
)
_RISK_DECLINED_ERROR = "已取消导入：高风险变更未获确认，本机配置未做任何修改"


def _external_env_reference(value: Any, managed_env: str) -> str:
    """返回该字段引用的「非本 Provider 自管」环境变量名，否则空串。

    自管引用（`PI_MANAGER_PROVIDER_<SLUG>_<HASH>_API_KEY` 与 Header 的对应名）指向
    本应用安全存储里该 Provider 自己的条目，是「导出→导入」往返的正常形态，不算
    风险；其余任何 `$NAME` / `${NAME}` 在请求时都会从**用户进程环境**取值，正是本
    节要拦的对象——包括「引用另一个 Provider 的自管名」这种变形。

    裸大写变量名不在此列：外部输入面已在 P1-2 关闭（`migrate_plaintext_keys(
    trusted=False)`），导入时它会被当成字面密钥保管，不会解析环境变量。
    """
    name = secretstore.referenced_env_name(core.normalize_config_string(value))
    if not name or name == managed_env:
        return ""
    return name


def _providers_on_disk() -> dict[str, Any]:
    """本机 models.json 当前的 providers；刻意不触发任何迁移 / 清理副作用。

    不用 `core.load_models_config()`：它会顺带把明文 Key 迁进安全存储并重写文件。
    差异计算发生在「要不要写盘」的判断阶段，本身不应该产生任何写入；而且这里要的
    正是**磁盘上的原样现状**（明文 apiKey 就该被看作「无环境变量引用」）。
    """
    data = core.load_json(core.models_path(), {})
    providers = data.get("providers") if isinstance(data, dict) else None
    return providers if isinstance(providers, dict) else {}


def _env_state_hint(env_name: str) -> tuple[bool, str]:
    """该环境变量此刻是否真有值——决定这条风险是「理论上的」还是「立刻生效的」。"""
    present = bool(os.environ.get(env_name, "").strip())
    return present, "当前已设置" if present else "当前未设置"


def _api_key_env_risk(
    name: str, entry: dict[str, Any], existing: dict[str, Any] | None, base_url: str
) -> dict[str, Any] | None:
    managed = secretstore.provider_env_name(name)
    env_name = _external_env_reference(entry.get("apiKey"), managed)
    if not env_name:
        return None
    if existing is not None and env_name == _external_env_reference(
        existing.get("apiKey"), managed
    ):
        return None  # 本机已经是同一个引用，导入没有改变凭据来源
    present, hint = _env_state_hint(env_name)
    return {
        "kind": RISK_API_KEY_ENV_REF,
        "provider": name,
        "base_url": base_url,
        "env_name": env_name,
        "env_present": present,
        "detail": (
            f"Provider「{name}」的 API Key 将引用本机环境变量 ${{{env_name}}}"
            f"（{hint}）；请求时该变量的真实值会被发往 {base_url}"
        ),
    }


def _header_env_risks(
    name: str, entry: dict[str, Any], existing: dict[str, Any] | None, base_url: str
) -> list[dict[str, Any]]:
    """Header 里的外部环境变量引用，与 apiKey 完全同源的一条泄漏通路。

    刻意**不**按 `is_sensitive_header_name` 过滤：`core_remote` 的两处发送路径
    （`:198`、`:655`）对**所有** Header 都调 `resolve_provider_header_value`，所以
    `X-Whatever: ${OPENAI_API_KEY}` 一样会把真实值发出去。
    """
    headers = entry.get("headers")
    if not isinstance(headers, dict):
        return []
    current = (existing or {}).get("headers")
    current = current if isinstance(current, dict) else {}
    risks: list[dict[str, Any]] = []
    for header, value in headers.items():
        if not isinstance(header, str):
            continue
        managed = secretstore.provider_header_env_name(name, header)
        env_name = _external_env_reference(value, managed)
        if not env_name:
            continue
        if existing is not None and env_name == _external_env_reference(
            current.get(header), managed
        ):
            continue
        present, hint = _env_state_hint(env_name)
        risks.append(
            {
                "kind": RISK_HEADER_ENV_REF,
                "provider": name,
                "base_url": base_url,
                "header": header,
                "env_name": env_name,
                "env_present": present,
                "detail": (
                    f"Provider「{name}」的 Header {header} 将引用本机环境变量 "
                    f"${{{env_name}}}（{hint}）；请求时该变量的真实值会被发往 "
                    f"{base_url}"
                ),
            }
        )
    return risks


def collect_import_risks(providers: Any) -> list[dict[str, Any]]:
    """算出配置包 providers 相对本机现状的高风险变更清单（写盘前用于逐条确认）。

    每一项形如 `{"kind", "provider", "base_url", "detail", ...}`，`detail` 是可直接
    展示给用户的中文单行说明。只报真正会让「本机凭据流向新地址」的四类变更：

    * `new_provider`：本机没有的 Provider —— 它的 baseUrl 与凭据来源全是新的；
    * `base_url_change`：已有 Provider 改指新地址 —— 原有 Key 会发往新地址；
    * `api_key_env_ref`：apiKey 新引用一个外部环境变量；
    * `header_env_ref`：某个 Header 新引用一个外部环境变量。

    其余变更（模型列表、显示型字段、Provider 删除、baseUrl 被清空回默认端点）一律
    不报。这条边界是刻意的：如果连纯粹的模型列表更新都弹确认，用户很快就会学会
    无脑点「确定」，那和没有确认没有区别。
    """
    if not isinstance(providers, dict):
        return []
    current = _providers_on_disk()
    risks: list[dict[str, Any]] = []
    for name, entry in providers.items():
        if not isinstance(name, str) or not isinstance(entry, dict):
            continue
        existing = current.get(name)
        existing = existing if isinstance(existing, dict) else None
        incoming_base = core.normalize_config_string(entry.get("baseUrl"))
        current_base = core.normalize_config_string((existing or {}).get("baseUrl"))
        base_url = incoming_base or current_base or _NO_BASE_URL_HINT
        if existing is None:
            risks.append(
                {
                    "kind": RISK_NEW_PROVIDER,
                    "provider": name,
                    "base_url": base_url,
                    "detail": f"新增 Provider「{name}」，请求将发往 {base_url}",
                }
            )
        elif incoming_base and incoming_base != current_base:
            risks.append(
                {
                    "kind": RISK_BASE_URL_CHANGE,
                    "provider": name,
                    "base_url": incoming_base,
                    "old_base_url": current_base or _NO_BASE_URL_HINT,
                    "detail": (
                        f"Provider「{name}」的 Base URL 变更："
                        f"{current_base or _NO_BASE_URL_HINT} → {incoming_base}；"
                        "该 Provider 现有的 API Key 将改为发往新地址"
                    ),
                }
            )
        api_risk = _api_key_env_risk(name, entry, existing, base_url)
        if api_risk is not None:
            risks.append(api_risk)
        risks.extend(_header_env_risks(name, entry, existing, base_url))
    return risks


def _gate_import_risks(
    models: dict[str, Any] | None,
    confirm_risks: Callable[[list[dict[str, Any]]], bool] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """返回 (风险清单, 拒绝结果)。拒绝结果非 None 时调用方必须原样返回它。

    fail closed：没有确认入口就等于没人确认，整包拒绝。宁可让一个新调用方立刻
    报错（可见、可修），也不要让它静默继承一条凭据外流通路。
    """
    risks = collect_import_risks(
        models.get("providers") if isinstance(models, dict) else None
    )
    if not risks:
        return risks, None
    if confirm_risks is None:
        return risks, {
            "ok": False,
            "needs_confirmation": True,
            "risks": risks,
            "error": _RISK_UNCONFIRMED_ERROR,
        }
    # 传副本：回调（UI）不该能改写我们即将据以提交的清单。
    if not confirm_risks([dict(item) for item in risks]):
        return risks, {
            "ok": False,
            "cancelled": True,
            "risks": risks,
            "error": _RISK_DECLINED_ERROR,
        }
    return risks, None


def import_config_bundle(
    zip_path: str,
    *,
    restore_secrets: bool = False,
    password: str = "",
    allow_commands: bool = False,
    import_agents_md: bool = False,
    confirm_risks: Callable[[list[dict[str, Any]]], bool] | None = None,
) -> dict[str, Any]:
    """Validate an entire bundle before applying it, then commit transactionally.

    ``confirm_risks`` 是 R1 的确认入口：全量校验通过、**尚未写任何东西**时，本函数
    算出「将新增 / 变更的 Provider + 其 baseUrl + 凭据引用形式」的高风险清单
    （见 :func:`collect_import_risks`），非空则回调它；回调返回假值即整包不写
    （沿用既有事务语义：磁盘与安全存储都保持原状）。清单为空时**不回调**——无害的
    导入（纯模型列表更新、原样导回自己导的包）不打扰用户。

    没有传 ``confirm_risks`` 而清单非空时一律拒绝（fail closed），并在结果里带
    ``needs_confirmation`` 与 ``risks``，让调用方知道该接确认入口。

    ``import_agents_md`` 默认 False：`AGENTS.md` 是全局 agent 指令文件，覆盖它
    等于让下一次运行的 Pi（有 shell 权限、`run_pi_print` 还会自动加 `--approve`）
    遵循配置包作者的指令，是一条间接提示注入 → 代码执行的通路（R2 审计 P1-4）。
    调用方必须在向用户展示全文 diff 并取得确认后才显式传 True。
    """
    src = Path(zip_path)
    if not src.exists() or not src.is_file():
        return {"ok": False, "error": "文件不存在"}
    try:
        files = _read_bundle(src)
        settings = _parse_bundle_json(files, "settings.json")
        models = _parse_bundle_json(files, "models.json")
        manager = _parse_bundle_json(files, "pi-manager.json")
        if settings is not None:
            _validate_settings(settings)
        if models is not None:
            _validate_models(models)
        if manager is not None:
            proxy_error = _validate_proxy_url(str(manager.get("proxy_url") or ""))
            if proxy_error:
                raise ValueError(f"pi-manager.json 的代理地址无效：{proxy_error}")
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

        # 全量校验已通过、此刻还没有写任何东西：在这里算差异并请求确认。
        # 位置刻意选在提交之前、且与提交共用同一份已解析的 ``models`` —— 若改成
        # 「先预览一次、用户确认后再重新打开 ZIP 提交」，攻击者可以在两次读取之间
        # 换掉文件（TOCTOU），用户确认的就不是最终落盘的内容。
        risks, refusal = _gate_import_risks(models, confirm_risks)
        if refusal is not None:
            return refusal

        core.ensure_agent_dir()
        writes: dict[Path, bytes] = {}
        if settings is not None:
            writes[core.settings_path()] = _json_bytes(settings)
        if models is not None:
            writes[core.models_path()] = _json_bytes(models)
        if manager is not None:
            writes[core.manager_config_path()] = _json_bytes(manager)
        skipped: list[str] = []
        if "AGENTS.md" in files:
            files["AGENTS.md"].decode("utf-8")
            if import_agents_md:
                writes[core.agents_md_path()] = files["AGENTS.md"]
            else:
                skipped.append("AGENTS.md")
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
                    # trusted=False：这些 provider 条目直接来自外部配置包，
                    # 不得触发 __DPAPI__ 跨 provider 凭据复制（P0-2），也不做
                    # 裸大写变量名的兼容解析（P1-2 的外部输入面）。
                    models["providers"] = secretstore.migrate_plaintext_keys(
                        models.get("providers") or {}, trusted=False
                    )
                    writes[core.models_path()] = _json_bytes(models)
                for path, content in writes.items():
                    _atomic_replace_bytes(
                        path,
                        content,
                        private=(path == core.manager_config_path()),
                    )
                restored = [
                    name
                    for name in ("settings.json", "models.json", "pi-manager.json")
                    if name in files
                ]
                if "AGENTS.md" in files and import_agents_md:
                    restored.append("AGENTS.md")
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
        # 导入刚刚重写了 models.json：如果历史备份里还留着迁移前的明文密钥，
        # 一并擦除（P1-3）。
        purged = purge_plaintext_key_backups()
        result: dict[str, Any] = {"ok": True, "restored": restored}
        if risks:
            # 已确认并落盘的高风险项：调用方可据此在成功提示里复述一遍，用户过后
            # 也还能看到自己刚刚同意了什么。
            result["risks"] = risks
        if skipped:
            result["skipped"] = skipped
        if purged:
            result["purged_backups"] = purged
        return result
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
        needs_attention = any(
            info.get(key)
            for key in ("missing", "outdated", "repair_required", "blocked", "check_failed")
        )
        if needs_attention:
            add("Pi \u66f4\u65b0", False, info.get("message") or "\u9700\u8981\u5904\u7406", "warn")
        else:
            add("Pi \u66f4\u65b0", True, info.get("message") or "\u5df2\u662f\u517c\u5bb9\u901a\u9053\u6700\u65b0\u7248")
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

    # orphaned provider key pools (provider config no longer in models.json)
    try:
        orphans = core.list_orphaned_provider_keys()
        if orphans:
            names_text = "、".join(o["provider"] for o in orphans[:5])
            more = f" 等 {len(orphans)} 个" if len(orphans) > 5 else ""
            add(
                "孤儿密钥",
                False,
                f"检测到 {len(orphans)} 个已无配置的 Provider 密钥池：{names_text}{more}（可在 Provider 页一键清理）",
                "warn",
            )
        else:
            add("孤儿密钥", True, "无")
    except Exception as e:
        add("孤儿密钥", True, f"跳过：{e}")

    # stale settings.enabledModels references (removed providers)
    try:
        builtin: set[str] = set()
        if core.find_pi_command():
            for m in core.list_models():
                builtin.add(m.provider)
        stale = core.list_stale_enabled_models(builtin_providers=builtin)
        if stale:
            stale_text = "、".join(stale[:5])
            more = f" 等 {len(stale)} 条" if len(stale) > 5 else ""
            add(
                "启用模型残留",
                False,
                f"settings.enabledModels 引用了 {len(stale)} 个已不存在的模式：{stale_text}{more}；"
                "Pi 每次启动都会输出 No models match pattern 警告，建议清理",
                "warn",
            )
        else:
            add("启用模型残留", True, "无残留模式")
    except Exception as e:
        add("启用模型残留", True, f"跳过：{e}")

    # third-party config sources (e.g. pi-ui writes models-store.json)
    try:
        store_path = core.pi_agent_dir() / "models-store.json"
        if store_path.exists():
            count = ""
            try:
                store_data = json.loads(
                    store_path.read_text(encoding="utf-8-sig")
                )
                if isinstance(store_data, dict):
                    count = f"（{len(store_data.get('providers') or {})} 个 provider）"
            except Exception:
                pass
            add(
                "第三方配置源",
                True,
                f"检测到 models-store.json{count}——由其他工具（如 pi-ui）维护，"
                "PiManager 不读写该文件；如两处配置不同步，请以 models.json 为准",
                "info",
            )
        else:
            add("第三方配置源", True, "无")
    except Exception as e:
        add("第三方配置源", True, f"跳过：{e}")

    # Pi project trust file (managed by the pi CLI itself)
    try:
        trust_path = core.pi_agent_dir() / "trust.json"
        if trust_path.exists():
            trust_data = json.loads(trust_path.read_text(encoding="utf-8-sig"))
            entries = "；".join(f"{k} → {v}" for k, v in trust_data.items()) or "空"
            add("项目信任", True, f"Pi 信任列表：{entries[:120]}")
        else:
            add("项目信任", True, "未配置")
    except Exception:
        add("项目信任", True, "存在但无法解析")

    # language
    add("语言偏好", True, core.get_language())

    # workdir last
    mgr = core.load_manager_config()
    wd = mgr.get("last_workdir") or ""
    add("最近工作目录", bool(wd), str(wd) or "—")

    # network quick (optional lightweight); several endpoints so users outside
    # mainland China are not misreported as offline. Consistent HTTP policy:
    # no redirects, status only, body never read.
    import urllib.parse
    import urllib.request

    from . import http_client

    probe_urls = (
        "https://www.baidu.com",
        "https://www.gstatic.com/generate_204",
        "https://api.github.com",
    )
    probe_error = ""
    for probe_url in probe_urls:
        try:
            t0 = time.perf_counter()
            req = urllib.request.Request(
                probe_url, method="GET", headers={"User-Agent": "PiManager"}
            )
            opener = urllib.request.build_opener(http_client.DenyRedirectHandler())
            with opener.open(req, timeout=5) as resp:
                _ = resp.status
            ms = round((time.perf_counter() - t0) * 1000)
            host = urllib.parse.urlsplit(probe_url).netloc
            add("基础网络", True, f"连通（{host}），延迟约 {ms} ms")
            break
        except Exception as e:
            probe_error = str(e)
    else:
        add("基础网络", False, f"异常：{probe_error}", "warn")

    add("Pi Manager 版本", True, APP_VERSION)
    return checks


def _http_get_json(url: str, *, timeout: int = 15) -> dict[str, Any]:
    import urllib.request

    from . import http_client

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"PiManager/{APP_VERSION}",
            "Accept": "application/vnd.github+json",
        },
    )
    opener = urllib.request.build_opener(http_client.DenyRedirectHandler())
    with opener.open(req, timeout=timeout) as resp:
        body = http_client.read_limited(
            resp, http_client.MANIFEST_MAX_BYTES
        ).decode("utf-8", errors="replace")
    data = json.loads(body)
    return data if isinstance(data, dict) else {}


def _pick_release_asset(assets: list[dict[str, Any]]) -> dict[str, str]:
    """按当前平台挑选推荐下载资源。"""
    import sys

    names = [(str(a.get("name") or ""), str(a.get("browser_download_url") or "")) for a in assets]
    names = [(n, u) for n, u in names if n and u]
    prefer: list[str] = []
    if sys.platform == "win32":
        prefer = ["windows-x64-dir.zip", "windows-x64-onefile.zip", "windows"]
    elif sys.platform == "darwin":
        # Apple Silicon 优先 arm64，否则任意 macos
        prefer = ["macos-arm64.zip", "macos-x64.zip", "macos"]
    else:
        prefer = ["linux-x64.tar.gz", "linux"]
    for key in prefer:
        for n, u in names:
            if key in n.lower():
                return {"name": n, "url": u}
    if names:
        return {"name": names[0][0], "url": names[0][1]}
    return {"name": "", "url": ""}


def check_manager_update() -> dict[str, Any]:
    """Check the official release feed without trusting it for installation."""
    cfg = core.load_manager_config()
    settings = core.load_settings()
    url = ""
    manifest_url = str(cfg.get("update_manifest_url") or "").strip()
    if not manifest_url:
        manifest_url = str(settings.get("update_manifest_url") or "").strip()
    try:
        parsed = urlsplit(manifest_url)
        if parsed.scheme == "https" and parsed.hostname:
            url = manifest_url
    except ValueError:
        url = ""
    if not url:
        url = UPDATE_MANIFEST_URL
    local = APP_VERSION
    result: dict[str, Any] = {
        "ok": True,
        "local": local,
        "remote": None,
        "has_update": False,
        "notes": "",
        "url": url or GITHUB_RELEASES_PAGE,
        "download": "",
        "asset_name": "",
        "source": "",
        "message": f"当前版本 {local}",
    }
    cfg["last_manager_update_check"] = datetime.now().isoformat(timespec="seconds")
    core.save_manager_config(cfg)

    try:
        if url:
            data = _http_get_json(url)
            tag = str(
                data.get("version")
                or data.get("tag_name")
                or data.get("name")
                or ""
            ).strip()
            remote = tag.lstrip("vV")
            result["source"] = "manifest"
            result["remote"] = remote
            result["notes"] = str(data.get("notes") or data.get("body") or "")[:2000]
            result["url"] = str(data.get("url") or GITHUB_RELEASES_PAGE)
            result["download"] = ""
            result["asset_name"] = ""
        else:
            data = _http_get_json(GITHUB_RELEASES_API)
            tag = str(data.get("tag_name") or data.get("name") or "").strip()
            remote = tag.lstrip("vV")
            result["source"] = "github-notification-only"
            result["remote"] = remote
            result["notes"] = str(data.get("body") or "")[:2000]
            result["url"] = str(data.get("html_url") or GITHUB_RELEASES_PAGE)
            assets = data.get("assets") if isinstance(data.get("assets"), list) else []
            picked = _pick_release_asset([a for a in assets if isinstance(a, dict)])
            result["asset_name"] = picked.get("name") or ""
            result["download"] = ""

        remote = str(result.get("remote") or "")
        if remote and core.parse_semver(remote) > core.parse_semver(local):
            result["has_update"] = True
            asset = result.get("asset_name") or ""
            extra = f" · 推荐包 {asset}" if asset else ""
            result["message"] = f"发现新版本 v{remote}（当前 v{local}）{extra}"
        elif remote:
            result["message"] = f"已是最新（本地 v{local}，远程 v{remote}）"
        else:
            result["message"] = f"当前版本 v{local}（未能解析远程版本号）"
    except Exception as e:
        result["ok"] = False
        result["message"] = f"检查失败：{e}"
    cfg = core.load_manager_config()
    cfg["manager_update_status"] = {
        "state": "ok" if result.get("ok") else "failed",
        "local": result.get("local"),
        "remote": result.get("remote"),
        "has_update": bool(result.get("has_update")),
        "notes": str(result.get("notes") or "")[:2000],
        "url": str(result.get("url") or ""),
        "message": str(result.get("message") or ""),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    core.save_manager_config(cfg)
    return result


def _install_root() -> Path:
    """当前安装根目录（frozen）或源码根。"""
    import sys

    if getattr(sys, "frozen", False):
        exe = Path(sys.executable).resolve()
        # macOS: .../PiManager.app/Contents/MacOS/PiManager
        if exe.parent.name == "MacOS" and exe.parent.parent.name == "Contents":
            return exe.parents[2]  # *.app
        return exe.parent
    return Path(__file__).resolve().parent.parent


def apply_manager_update_inplace(archive_path: str | Path) -> dict[str, Any]:
    """Reject in-place installation until signed package verification exists."""
    return {
        "ok": False,
        "need_exit": False,
        "message": "签名更新链尚未启用，已禁止原地安装。请从官方 Release 页面手动更新。",
    }


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
            parsed = core.parse_favorite_key(str(key))
            if parsed:
                add(parsed[0], parsed[1])
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
    is_cancelled: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """批量健康检查。

    ``is_cancelled`` 是与 ``ui.Worker`` 约定的协作式取消入口：Worker 检测到本函数
    声明了这个形参就会自动注入 ``isInterruptionRequested``。健康检查是最长的后台
    任务之一（每个模型最多 90s），此前不接这个契约，导致 ``requestInterruption()``
    对它完全是空操作、关闭时的 2.5s 预算形同虚设（R2 UI 审计 P1）。
    取消时**保留已完成的部分结果**并照常写入 health —— 已经花掉的探测不该白费。
    """
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
        timeout=90 if (mode or "auto").lower().strip() == "pi" else 45,
        max_workers=get_test_concurrency(),
        on_one=_on_one,
        append_history_each=False,
        is_cancelled=is_cancelled,
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
    return {
        "ok": True,
        "results": results,
        "health": health,
        "scope": scope,
        "count": len(pairs),
        # 被取消时 results 会短于 pairs：调用方据此区分「全部跑完」与「中途停下」，
        # 否则界面会把部分结果当成完整结论展示。
        "cancelled": bool(is_cancelled and is_cancelled()),
    }


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


def chat_once(
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    workdir: str | None = None,
    timeout: float = 180,
    thinking: str | None = "off",
) -> dict[str, Any]:
    apply_proxy_env()
    t0 = time.perf_counter()
    try:
        code, out, err = core.run_pi_print(
            prompt,
            workdir=workdir or str(core.user_home()),
            provider=provider,
            model=model,
            thinking=thinking or "off",
            timeout=timeout,
        )
    except Exception as exc:
        code, out, err = -1, "", str(exc)
    ms = round((time.perf_counter() - t0) * 1000, 1)
    text = (out or "").strip()
    err_text = (err or "").strip()
    ok = code == 0 and bool(text)
    if code == 0 and not text and err_text and "error" not in err_text.lower():
        text = err_text
        ok = True
    return {
        "ok": ok,
        "returncode": code,
        "stdout": out,
        "stderr": err,
        "latency_ms": ms,
        "provider": provider,
        "model": model,
        "error": "" if ok else (err_text or text or f"退出码 {code}"),
    }


def failover_chain(start_provider: str | None = None, start_model: str | None = None) -> list[tuple[str, str]]:
    """故障切换候选链：当前模型 → 收藏 → enabledModels → 默认，去重保序。"""
    chain: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(p: str | None, m: str | None):
        p = (p or "").strip()
        m = (m or "").strip()
        if not p or not m:
            return
        k = f"{p}/{m}"
        if k in seen:
            return
        seen.add(k)
        chain.append((p, m))

    add(start_provider, start_model)
    mgr = core.load_manager_config()
    for key in mgr.get("favorites") or []:
        parsed = core.parse_favorite_key(str(key))
        if parsed:
            add(parsed[0], parsed[1])
    try:
        settings = core.load_settings()
        for key in settings.get("enabledModels") or []:
            parsed = core.parse_favorite_key(str(key))
            if parsed:
                add(parsed[0], parsed[1])
        dp = str(settings.get("defaultProvider") or "")
        dm = str(settings.get("defaultModel") or "")
        add(dp, dm)
    except Exception:
        pass
    return chain


def _model_pair_key(provider: str | None, model: str | None) -> str:
    try:
        pair = core.normalize_model_pair(provider, model)
    except ValueError:
        return ""
    return f"{pair[0]}/{pair[1]}" if pair is not None else ""


def _fail_counts() -> dict[str, int]:
    mgr = core.load_manager_config()
    raw = mgr.get("failover_fail_counts") or {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for k, v in raw.items():
        try:
            out[str(k)] = int(v)
        except Exception:
            out[str(k)] = 0
    return out


def _save_fail_counts(counts: dict[str, int]) -> None:
    mgr = core.load_manager_config()
    mgr["failover_fail_counts"] = counts
    core.save_manager_config(mgr)


# 串行化 fail_count 的读-改-写，避免跨线程并发丢失更新。
# 注意：不能用 storage.locked(manager_config_path())，因为 _fail_counts/
# _save_fail_counts 内部走 core.load/save_manager_config → storage.load/save_json
# → storage.locked(path)，跨进程锁 msvcrt/fcntl 不可重入，会触发死锁。
_fail_counts_lock = threading.Lock()


def record_model_success(provider: str, model: str) -> None:
    key = _model_pair_key(provider, model)
    if not key:
        return
    with _fail_counts_lock:
        counts = _fail_counts()
        if key in counts:
            counts[key] = 0
            _save_fail_counts(counts)


def record_model_failure(provider: str, model: str) -> int:
    """累计失败次数并返回当前计数。"""
    key = _model_pair_key(provider, model)
    if not key:
        return 0
    with _fail_counts_lock:
        counts = _fail_counts()
        counts[key] = int(counts.get(key) or 0) + 1
        _save_fail_counts(counts)
        return counts[key]


def should_failover(provider: str, model: str) -> bool:
    mgr = core.load_manager_config()
    if not bool(mgr.get("failover_enabled", True)):
        return False
    thr = int(mgr.get("failover_fail_threshold") or 3)
    thr = max(1, thr)
    key = _model_pair_key(provider, model)
    return bool(key) and int(_fail_counts().get(key) or 0) >= thr


def _chat_attempt(
    prompt: str,
    *,
    provider: str | None,
    model: str | None,
    workdir: str | None,
    timeout: float,
    thinking: str | None,
) -> dict[str, Any]:
    """One chat attempt: persistent RPC session when available, else one-shot.

    The RPC session keeps conversation context in-process and lets failover
    hot-switch models via set_model; when `pi --mode rpc` is unusable the
    session layer disables itself for the rest of the run and every attempt
    falls back to the classic one-shot `pi -p` path.

    上游 5xx / ``upstream_overloaded`` 会在同一模型上短暂重试，避免 Grokified
    这类中转把瞬时过载误报成「无法对话」。
    """
    from . import rpc_session
    from .core_http import is_transient_upstream_error, transient_retry_delay

    def _once() -> dict[str, Any]:
        apply_proxy_env()
        if rpc_session.rpc_chat_enabled():
            result = rpc_session.rpc_chat_once(
                prompt,
                provider=provider,
                model=model,
                workdir=workdir,
                timeout=timeout,
                thinking=thinking,
            )
            if result.get("ok") or rpc_session.rpc_chat_enabled():
                return result
            # rpc became unavailable during this attempt — retry one-shot
        return chat_once(
            prompt,
            provider=provider,
            model=model,
            workdir=workdir,
            timeout=timeout,
            thinking=thinking,
        )

    last = _once()
    max_attempts = core.TRANSIENT_HTTP_MAX_ATTEMPTS
    for attempt in range(max_attempts - 1):
        if last.get("ok"):
            return last
        blob = "\n".join(
            str(last.get(key) or "") for key in ("error", "stderr", "stdout")
        )
        if not is_transient_upstream_error(blob):
            return last
        core.sleep_transient_retry(transient_retry_delay(attempt))
        last = _once()
    return last


def chat_with_failover(
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    workdir: str | None = None,
    timeout: float = 180,
    thinking: str | None = "off",
    set_as_default_on_switch: bool = True,
) -> dict[str, Any]:
    """快速提问 + 连续失败自动切换下一个模型。

    规则：同一模型累计失败达到 failover_fail_threshold（默认 3）后，
    自动跳到候选链下一个模型重试同一 prompt，尽量无感继续对话。
    """
    mgr = core.load_manager_config()
    enabled = bool(mgr.get("failover_enabled", True))
    thr = max(1, int(mgr.get("failover_fail_threshold") or 3))
    silent = bool(mgr.get("failover_silent", True))

    try:
        requested_pair = core.normalize_model_pair(provider, model)
        if requested_pair is not None:
            provider, model = requested_pair
        else:
            dp, dm, _ = core.get_default_model()
            default_pair = core.normalize_model_pair(dp, dm)
            if default_pair is not None:
                provider, model = default_pair
    except ValueError as exc:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
            "latency_ms": 0,
            "provider": provider,
            "model": model,
            "switched": False,
            "attempts": [],
            "error": str(exc),
        }

    chain = failover_chain(provider, model)
    if not chain:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": "无可用模型（请配置默认或收藏）",
            "latency_ms": 0,
            "provider": provider,
            "model": model,
            "switched": False,
            "attempts": [],
            "error": "无可用模型",
        }

    # 从当前模型在链中的位置开始；若已达失败阈值，则直接从下一个开始
    start_idx = 0
    for i, (p, m) in enumerate(chain):
        if p == (provider or "") and m == (model or ""):
            start_idx = i
            break
    if enabled and should_failover(chain[start_idx][0], chain[start_idx][1]):
        start_idx = min(start_idx + 1, len(chain) - 1)

    attempts: list[dict[str, Any]] = []
    last: dict[str, Any] = {}
    switched_from: str | None = None

    for idx in range(start_idx, len(chain)):
        p, m = chain[idx]
        # 若该模型已达阈值且不是链尾唯一选择，跳过
        if enabled and idx > start_idx and should_failover(p, m) and idx < len(chain) - 1:
            attempts.append({"provider": p, "model": m, "skipped": True, "reason": f"已连续失败≥{thr}"})
            continue

        result = _chat_attempt(
            prompt,
            provider=p,
            model=m,
            workdir=workdir,
            timeout=timeout,
            thinking=thinking,
        )
        result = dict(result)
        result["attempt_index"] = idx
        attempts.append(
            {
                "provider": p,
                "model": m,
                "ok": result.get("ok"),
                "returncode": result.get("returncode"),
                "latency_ms": result.get("latency_ms"),
                "error": result.get("error") or "",
            }
        )
        last = result

        if result.get("ok"):
            record_model_success(p, m)
            switched = bool(switched_from) or (p != (provider or "") or m != (model or ""))
            if switched and set_as_default_on_switch:
                try:
                    core.set_default_model(p, m)
                except Exception:
                    pass
            last["switched"] = switched
            last["switched_from"] = switched_from
            last["attempts"] = attempts
            last["silent"] = silent
            last["failover_enabled"] = enabled
            if switched and not silent:
                last["notice"] = f"已自动切换：{switched_from or f'{provider}/{model}'} → {p}/{m}"
            elif switched and silent:
                last["notice"] = ""  # 无感：不在正文强调
            else:
                last["notice"] = ""
            return last

        # 失败：累计
        count = record_model_failure(p, m)
        attempts[-1]["fail_count"] = count
        if not enabled:
            break
        if count < thr:
            # 未达阈值：本轮返回失败，下次继续累计
            break
        # 达阈值：本轮内立刻切下一个模型重试同一问题（无感继续）
        if switched_from is None:
            switched_from = f"{p}/{m}"
        continue

    if last:
        last["switched"] = bool(switched_from)
        last["switched_from"] = switched_from
        last["attempts"] = attempts
        last["silent"] = silent
        last["failover_enabled"] = enabled
        last["notice"] = "" if silent else (f"尝试切换失败，已用尽候选（自 {switched_from}）" if switched_from else "")
        return last
    return {
        "ok": False,
        "returncode": -1,
        "stdout": "",
        "stderr": "全部候选模型失败",
        "latency_ms": 0,
        "provider": provider,
        "model": model,
        "switched": False,
        "attempts": attempts,
        "error": "全部候选模型失败",
    }
