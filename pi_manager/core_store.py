"""配置读写：settings / models / auth / manager（原子写、缓存、损坏恢复）。"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import storage
from .core_paths import (
    auth_path,
    manager_config_path,
    models_path,
    settings_path,
    user_home,
)

logger = logging.getLogger(__name__)


def _core():
    from . import core

    return core



def load_json(path: Path, default: Any) -> Any:
    return storage.load_json(path, default)



def save_json(path: Path, data: Any, *, private: bool = False) -> None:
    _core().ensure_agent_dir()
    storage.save_json(path, data, private=private)
    _invalidate_config_cache(path)



def mask_secret(value: str | None, keep: int = 4) -> str:
    """把可能是密钥的值转成可安全展示的形式。

    此前这里有一份 `^[A-Z][A-Z0-9_]{2,}$` 的「裸大写串 = 环境变量名」启发式，
    命中就**原样返回不打码**。但 AWS Access Key ID（`AKIAIOSFODNN7EXAMPLE`）这类
    真实凭据恰好就是全大写字母数字，于是真实密钥会完整出现在 UI 与日志里
    （审查 P1-2）。`secrets.referenced_env_name` 已删除同一条启发式，这里必须同步，
    否则 P1-2 只修了一半。

    现在只有**显式**的引用/命令前缀才放行：`$NAME` / `${NAME}` / `!command`。
    代价是历史上真填过裸变量名的用户会看到打码后的变量名——打码过度是安全方向，
    打码不足才是泄漏。
    """
    if not value:
        return ""
    s = str(value)
    if s.startswith(("!", "$")):
        # 显式的环境变量引用或 shell 命令，不是密钥本身
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



_CONFIG_CACHE: dict[str, tuple[int, int, Any, float]] = {}

_CONFIG_CACHE_LOCK = threading.Lock()

# ==== 配置读写：settings / models / auth / manager（带进程内缓存） ====

_CONFIG_CACHE_TTL = 5.0  # seconds



def _invalidate_config_cache(path: Path | None = None) -> None:
    """Drop cached config entries after an in-process write."""
    with _CONFIG_CACHE_LOCK:
        if path is None:
            _CONFIG_CACHE.clear()
        else:
            _CONFIG_CACHE.pop(str(path), None)



def _load_json_cached(path: Path, default: Any) -> Any:
    """load_json with an (mtime_ns, size)-keyed cache for hot-path configs.

    A quick-ask reads pi-manager.json many times per prompt; one os.stat is
    far cheaper than the full file-lock + parse round trip. Writers (this
    process, the pi CLI, the extension's broker) all replace the file, so a
    changed signature naturally invalidates the entry.

    A monotonic TTL guards against file systems whose mtime granularity is
    too coarse to detect a rapid in-place rewrite by another process.
    """
    key = str(path)
    try:
        stat_before = os.stat(path)
        signature = (stat_before.st_mtime_ns, stat_before.st_size)
    except OSError:
        signature = None
    if signature is not None:
        with _CONFIG_CACHE_LOCK:
            cached = _CONFIG_CACHE.get(key)
        if (
            cached is not None
            and (cached[0], cached[1]) == signature
            and (time.monotonic() - cached[3]) < _CONFIG_CACHE_TTL
        ):
            return copy.deepcopy(cached[2])
    data = load_json(path, default)
    # Only cache when the file did not change while we were reading it.
    try:
        stat_after = os.stat(path)
        after = (stat_after.st_mtime_ns, stat_after.st_size)
    except OSError:
        after = None
    if after is not None and after == signature:
        with _CONFIG_CACHE_LOCK:
            _CONFIG_CACHE[key] = (after[0], after[1], copy.deepcopy(data), time.monotonic())
    return data



def _update_config(
    path: Path,
    default: Any,
    updater: Callable[[Any], Any],
    *,
    private: bool = False,
) -> Any:
    """在同一把跨进程锁内完成一次配置「读 → 改 → 写」。

    与「``load_xxx()`` → 改 → ``save_xxx()``」的关键区别：后者只在写的那一瞬间
    持锁，读与写之间的窗口毫无保护，而写入是**整份文档覆盖** —— 窗口期内其它
    写入者（另一个线程、模型测试线程池、健康检查、甚至 ``--config-mutate`` 的
    helper 进程）对**任何字段**的修改都会被静默回退。这是审查里 25 处「丢失
    更新」的共同根因，所有新代码都应走这里而不是 load/save 对。
    """
    _core().ensure_agent_dir()
    try:
        # 整体再包一把锁：``storage.locked`` 已改为进程内可重入，所以内层的
        # update_json / save_json 各自再加锁是安全的。这让「发现损坏 → 修复 →
        # 重试」成为一个原子步骤，中间没有别的进程再写坏文件的窗口。
        with storage.locked(path):
            try:
                return storage.update_json(path, default, updater, private=private)
            except storage.CorruptJsonError as exc:
                # 读路径有 _load_config_with_recovery 兜底，写路径必须同样韧性。
                # 否则会出现一个反直觉的倒退：25 处「load → 改 → save」迁到
                # update_json 之后，load_xxx() 那次「顺手把损坏文件修好」的副作用
                # 没了，配置损坏时改主题/改语言/写失败计数会直接抛 CorruptJsonError
                # —— 比迁移前更差。helper 进程（``--config-mutate``）更是从不先读。
                logger.warning(
                    "%s 损坏无法读取，写入前先尝试恢复: %s", path.name, exc
                )
                _repair_corrupt_config(path, default, path.name, private=private)
                # 只重试一次：修不好（例如路径根本不是普通文件）就让异常传上去，
                # 不进入「修复 → 失败 → 再修复」的循环。
                return storage.update_json(path, default, updater, private=private)
    finally:
        # 写盘后缓存必然过期；updater 抛错时缓存也未必还可信（另一进程可能刚写过），
        # 所以无论成功失败都失效，宁可多读一次盘。
        _invalidate_config_cache(path)



def _repair_corrupt_config(
    path: Path, default: Any, label: str, *, private: bool = False
) -> Any:
    """损坏 → 回退备份 → 再回退默认值，并**把结果写回磁盘**修复该文件。

    只在内存里兜底是不够的：损坏文件仍留在原地，之后每一次写入都会被
    ``storage._write_unlocked`` 的「拒绝覆盖无法读取的配置文件」守卫挡住 ——
    应用进入永久只读的死角，而应用内没有「删除损坏文件」的入口。所以这里必须
    真的把文件修好，并且走 ``allow_corrupt_overwrite`` 这条唯一的恢复出口
    （损坏内容会被隔离成 ``<name>.corrupt.<ts>``，备份链不参与轮转）。
    """
    restored = _restore_latest_config_backup(path)
    if restored is not None:
        data: Any = restored
        source = "最近备份"
    else:
        data = copy.deepcopy(default)
        source = "默认值"
    try:
        storage.save_json(path, data, private=private, allow_corrupt_overwrite=True)
        logger.warning("%s 损坏，已用%s重建（损坏内容另存为 .corrupt.*）", label, source)
    except Exception as exc:
        # 修不好也要让调用方拿到可用数据：UI 能继续启动，用户还能走「备份恢复」。
        logger.warning("%s 损坏且自动重建失败（%s），本次使用%s兜底", label, exc, source)
    _invalidate_config_cache(path)
    return data



def _load_config_with_recovery(
    path: Path, default: Any, label: str, *, private: bool = False
) -> Any:
    """带韧性的配置读取：损坏时回退备份/默认值，而不是把异常抛给启动路径。

    ``get_language()`` / ``get_ui_theme()`` / ``is_setup_done()`` /
    ``get_default_model()`` / ``get_theme()`` 这些启动路径函数都没有 try/except，
    任何一份配置损坏就会让应用起不来。三份配置统一走这里，韧性等级才一致 ——
    比逐点打 try/except 补丁（必然漏）更可靠。
    """
    try:
        return _load_json_cached(path, default)
    except storage.CorruptJsonError as exc:
        logger.warning("%s 损坏无法读取，尝试恢复备份: %s", label, exc)
        return _repair_corrupt_config(path, default, label, private=private)



def load_settings() -> dict[str, Any]:
    return _load_config_with_recovery(settings_path(), {}, "settings.json")



def save_settings(data: dict[str, Any]) -> None:
    save_json(settings_path(), data)



def update_settings(
    updater: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """全程持锁地「读 → 改 → 写」settings.json（见 ``_update_config``）。"""

    def _apply(current: Any) -> dict[str, Any]:
        return updater(current if isinstance(current, dict) else {})

    return _core()._update_config(settings_path(), {}, _apply)



DEFAULT_OPENAI_COMPAT_USER_AGENT = "PiManager/1.0 (+PiCLI)"

_OPENAI_COMPAT_APIS = frozenset(
    {"openai", "openai-completions", "openai-responses"}
)



def _openai_compat_headers(
    api: str, headers: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Add a WAF-friendly UA without overriding a non-empty custom value."""
    result = dict(headers or {})
    if str(api or "").strip().lower() not in _OPENAI_COMPAT_APIS:
        return result
    user_agent_key = next(
        (key for key in result if str(key).strip().lower() == "user-agent"),
        None,
    )
    if user_agent_key is None:
        result["User-Agent"] = DEFAULT_OPENAI_COMPAT_USER_AGENT
    elif not str(result.get(user_agent_key) or "").strip():
        result[user_agent_key] = DEFAULT_OPENAI_COMPAT_USER_AGENT
    return result



def _restore_latest_config_backup(target_path: Path) -> dict[str, Any] | None:
    """Return the newest parseable ``<name>.bak.*`` backup for *target_path*.

    Used as a last resort when the live config file is corrupt. Backups are
    tried newest-first; the first one that parses to a dict wins. Returns
    ``None`` if no usable backup exists.
    """
    name = target_path.name
    root = target_path.parent
    try:
        candidates = [
            p
            for p in root.glob(f"{name}.bak.*")
            if p.is_file()
        ]
    except OSError:
        return None
    # Newest by mtime first.
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for bak in candidates:
        try:
            data = load_json(bak, None)
        except Exception:
            continue
        if isinstance(data, dict):
            return data
    return None



_MODELS_MIGRATION_LOCK = threading.Lock()

# 最近一次跑完格式迁移时 models.json 的 (mtime_ns, size) 签名。
# 迁移逻辑是幂等的，但每次 load_models_config() 都重跑全量扫描
# （密钥迁移 / User-Agent 头 / 模型缺省字段 / thinkingLevelMap）纯属浪费 —— 而它是热路径
# （get_provider_config → provider_runtime_credential / test_model 每次都调）。
# 用文件签名而不是「一次性布尔」做门槛：另一个进程（pi CLI、helper、配置导入）
# 写过盘后签名会变，迁移仍会重新执行，不会漏掉外部写入的旧格式配置。
# 签名里带上路径：测试的 isolated_home 会让 models_path() 在用例之间变化，
# 只比 (mtime_ns, size) 有极小概率在两份不同文件上撞上（NTFS 的 FILETIME 是
# 100ns 刻度），撞上就会错误地跳过迁移。
_MODELS_MIGRATED_SIGNATURE: tuple[str, int, int] | None = None



def _models_file_signature() -> tuple[str, int, int] | None:
    path = models_path()
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size)



def _migrate_models_keys(cfg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """明文 / 遗留 ``__DPAPI__`` 密钥迁移成环境变量引用。

    Pi understands environment references but not Pi Manager's legacy
    ``__DPAPI__`` marker.
    """
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return cfg, False
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
        if not needs_migration:
            return cfg, False
        migrated = secretstore.migrate_plaintext_keys(providers)
        if migrated == providers:
            return cfg, False
        result = dict(cfg)
        result["providers"] = migrated
        return result, True
    except Exception as exc:
        # Keep configuration readable even if the platform keyring is broken,
        # but leave a trace: a failed migration means plaintext keys may still
        # sit in models.json and must not disappear silently.
        logger.warning("models.json 密钥迁移失败，明文引用可能仍保留在配置中: %s", exc)
        return cfg, False



def _migrate_models_headers(cfg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """给 OpenAI 兼容 Provider 补上 WAF 友好的默认 User-Agent。

    OpenAI's Node SDK UA may be blocked by some compatible-provider WAFs;
    persist the safe default so upgraded, existing providers behave like new ones.
    """
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return cfg, False
    updated_providers = dict(providers)
    changed = False
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        current_headers = entry.get("headers")
        if current_headers is not None and not isinstance(current_headers, dict):
            continue
        effective_headers = _openai_compat_headers(
            str(entry.get("api") or "openai-completions"), current_headers
        )
        if effective_headers == (current_headers or {}):
            continue
        updated_entry = dict(entry)
        updated_entry["headers"] = effective_headers
        updated_providers[name] = updated_entry
        changed = True
    if not changed:
        return cfg, False
    result = dict(cfg)
    result["providers"] = updated_providers
    return result, True



def _migrate_models_defaults(cfg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """给手填的不完整模型补上与「添加模型」相同的缺省字段。

    只写 ``{"id": "grok-4.5", "name": "grok-4.5"}`` 时，官方 Pi 缺少
    ``reasoning`` / ``contextWindow`` / ``thinkingLevelMap``，表现为能保存但无法对话。
    """
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return cfg, False
    updated_providers = dict(providers)
    changed = False
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models")
        if not isinstance(models, list):
            continue
        new_models: list[Any] = []
        any_changed = False
        for m in models:
            if not isinstance(m, dict):
                new_models.append(m)
                continue
            migrated = _core().fill_model_defaults(m)
            if migrated is not m:
                any_changed = True
            new_models.append(migrated)
        if not any_changed:
            continue
        updated_entry = dict(entry)
        updated_entry["models"] = new_models
        updated_providers[name] = updated_entry
        changed = True
    if not changed:
        return cfg, False
    result = dict(cfg)
    result["providers"] = updated_providers
    return result, True



def _migrate_models_thinking(cfg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """给缺少 thinkingLevelMap 的推理模型补上默认映射。

    Without it, Pi silently clamps "max" down to "high" (and drops xhigh/max
    from the supported levels list).
    """
    providers = cfg.get("providers")
    if not isinstance(providers, dict):
        return cfg, False
    updated_providers = dict(providers)
    changed = False
    for name, entry in providers.items():
        if not isinstance(entry, dict):
            continue
        models = entry.get("models")
        if not isinstance(models, list):
            continue
        new_models: list[Any] = []
        any_changed = False
        for m in models:
            if not isinstance(m, dict):
                new_models.append(m)
                continue
            migrated = _core().ensure_thinking_level_map(m)
            if migrated is not m:
                any_changed = True
            new_models.append(migrated)
        if not any_changed:
            continue
        updated_entry = dict(entry)
        updated_entry["models"] = new_models
        updated_providers[name] = updated_entry
        changed = True
    if not changed:
        return cfg, False
    result = dict(cfg)
    result["providers"] = updated_providers
    return result, True



_MODELS_MIGRATIONS = (
    _migrate_models_keys,
    _migrate_models_headers,
    _migrate_models_defaults,
    _migrate_models_thinking,
)



def _migrate_models_config(cfg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """格式迁移的纯变换组合（幂等：``m(m(x)) == m(x)``）。"""
    changed = False
    for step in _core()._MODELS_MIGRATIONS:
        cfg, step_changed = step(cfg)
        changed = changed or step_changed
    return cfg, changed



def load_models_config() -> dict[str, Any]:
    cfg = _load_config_with_recovery(models_path(), {"providers": {}}, "models.json")
    if not isinstance(cfg, dict):
        cfg = {"providers": {}}
    if not isinstance(cfg.get("providers"), dict):
        cfg["providers"] = {}
        return cfg

    signature = _models_file_signature()
    with _MODELS_MIGRATION_LOCK:
        if signature is not None and signature == _core()._MODELS_MIGRATED_SIGNATURE:
            return cfg

    # 先在内存里（不加锁）判断是否真的需要迁移：绝大多数调用都不需要，
    # 这样热路径不会因为「迁移检测」而付出一次跨进程加锁的代价。
    migrated_cfg, changed = _migrate_models_config(cfg)
    if changed:
        try:
            # 落盘时必须在锁内基于**磁盘最新内容**重算一遍：从加载到写入之间的
            # 并发写入（另一个线程加 Provider、helper 进程改配置）本来会被这次
            # 整份覆盖回退。迁移是幂等纯变换，重算的代价可以忽略。
            cfg = update_models_config(lambda current: _migrate_models_config(current)[0])
        except Exception as exc:
            logger.warning("models.json 格式迁移落盘失败，本次仅在内存生效: %s", exc)
            cfg = migrated_cfg
    else:
        cfg = migrated_cfg

    if changed:
        # 迁移刚把明文原文轮转进 models.json.bak.1：不擦除的话「把明文安全迁移成
        # 引用」反而等于把明文永久留在同目录（R2 审计 P1-3，已实证）。purge 只读
        # 字节判断、只擦确实含明文的副本，且不回调 load_models_config，无递归风险。
        # 这是配置加载热路径，任何失败都不能影响正常读配置，所以整体兜住。
        try:
            from .extras import purge_plaintext_key_backups

            purged = purge_plaintext_key_backups()
            if purged:
                logger.info("已擦除含明文密钥的 models.json 旧备份：%d 个", len(purged))
        except Exception as exc:
            logger.warning("擦除明文密钥备份失败（不影响本次配置加载）: %s", exc)

    # 记录「这个磁盘版本已经迁移过」。必须重新取签名：上面可能写过盘。
    with _MODELS_MIGRATION_LOCK:
        _core()._MODELS_MIGRATED_SIGNATURE = _models_file_signature()
    return cfg



def save_models_config(data: dict[str, Any]) -> None:
    # private=True：与 pi-manager.json 一致收紧 POSIX 权限位 / Windows DACL。
    # models.json 虽无密钥本体，但含 provider 清单与 baseUrl，备份轮转副本
    # 同路径加固（此前默认 private=False，继承父目录宽松 ACL）。
    save_json(models_path(), _sanitize_models_config(data), private=True)



def _sanitize_models_config(data: Any) -> Any:
    """丢弃顶层的下划线私有键，避免内部返回通道被误持久化。

    ``delete_custom_provider`` / ``remove_model_from_provider`` 会把操作结果塞进
    返回的配置字典（``_purge`` / ``_purged_enabled``，UI 依赖这个契约）。只要有人
    写出 ``save_models_config(delete_custom_provider(x))``，这些键就会落进
    models.json，Pi CLI 会看到不认识的顶层字段。models.json 的正式 schema 里没有
    任何下划线顶层键，所以在写入口统一挡掉最省心。
    """
    if not isinstance(data, dict):
        return data
    if not any(str(key).startswith("_") for key in data):
        return data
    return {key: value for key, value in data.items() if not str(key).startswith("_")}



def update_models_config(
    updater: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """全程持锁地「读 → 改 → 写」models.json（见 ``_update_config``）。

    updater 拿到的是**磁盘上的最新内容**（结构已规范化为含 ``providers`` 字典），
    不是调用方几行之前 ``load_models_config()`` 读到的快照 —— 这正是它能避免
    丢失更新的原因。三轮格式迁移不在锁内重跑：它们由 ``load_models_config()``
    负责且幂等，重复执行只会白费 IO。
    """

    def _apply(current: Any) -> dict[str, Any]:
        cfg = current if isinstance(current, dict) else {"providers": {}}
        if not isinstance(cfg.get("providers"), dict):
            cfg["providers"] = {}
        return _sanitize_models_config(updater(cfg))

    return _core()._update_config(models_path(), {"providers": {}}, _apply)



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



def delete_provider_auth(provider: str) -> dict[str, Any] | None:
    """Remove one provider's Pi credentials from auth.json (Pi-only logout).

    Other local tools (OpenAI CLI, Claude Code, Gemini CLI, …) keep their own
    credential stores and are never touched by this operation.
    """
    provider = (provider or "").strip()
    if not provider:
        return None
    removed: dict[str, Any] | None = None

    def remove(current: Any) -> dict[str, Any]:
        nonlocal removed
        if not isinstance(current, dict):
            raise ValueError("auth.json 顶层必须是对象")
        entry = current.get(provider)
        if not isinstance(entry, dict):
            raise ValueError(f"Provider「{provider}」没有已保存的认证")
        removed = entry
        result = dict(current)
        del result[provider]
        return result

    try:
        storage.update_json(auth_path(), {}, remove)
    except Exception as exc:
        raise ValueError(str(exc)) from exc
    return removed



# 升级兼容：这些键在**已存在**的 pi-manager.json 上也会被补齐。
# 只列「新增过的键」，不含 favorites / last_workdir / terminal / quick_models /
# setup_done / last_update_check —— 那几个从第一版就有，缺失说明用户手工删过，
# 代码里都按 `cfg.get(...) or 默认` 处理，不应在读路径上又写回去。
_MANAGER_UPGRADE_DEFAULTS: dict[str, Any] = {
    "proxy_enabled": False,
    "proxy_url": "",
    "test_concurrency": 3,
    "secure_keys": True,
    "minimize_to_tray": True,
    "start_minimized": False,
    "health_interval_min": 0,
    "update_manifest_url": "",
    "last_manager_update_check": "",
    "pi_update_status": {},
    "manager_update_status": {},
    "dismissed_updates": [],
    "drop_auto_launch": True,
    "language": "zh-CN",
    "ui_mode": "night",
    "ui_accent": "blue",
    "auto_check_update": True,
    "failover_enabled": True,
    "failover_fail_threshold": 3,
    "failover_fail_counts": {},
    "failover_silent": True,
    "chat_persistent_session": True,
    "chat_session_idle_min": 10,
}



def _manager_config_defaults() -> dict[str, Any]:
    """pi-manager.json 缺失时的完整初值。

    必须是函数而不是模块常量：``last_workdir`` 依赖 ``user_home()``，
    模块导入期求值会把测试的 ``isolated_home``（monkeypatch HOME）钉死成
    真实用户目录 —— 项目出过测试污染真实 ``~/.pi/agent/`` 的事故。
    """
    return {
        "favorites": [],
        "last_workdir": str(user_home()),
        "terminal": "auto",
        "quick_models": [],
        "setup_done": False,
        "last_update_check": "",
        **copy.deepcopy(_MANAGER_UPGRADE_DEFAULTS),
    }



def _normalize_manager_config(data: Any) -> dict[str, Any]:
    """保证顶层是 dict 并补齐升级新增键。

    ``load_manager_config()`` 与 ``update_manager_config()`` 的 updater 共用它，
    保证「读到的结构」在读路径和读-改-写路径上完全一致 —— 否则 updater 会看到
    比 load 更贫瘠的字典，逐个 `.get()` 兜底的老代码就会踩空。
    """
    if not isinstance(data, dict):
        data = {}
    for key, value in _MANAGER_UPGRADE_DEFAULTS.items():
        data.setdefault(key, copy.deepcopy(value))
    return data



def load_manager_config() -> dict[str, Any]:
    data = _load_config_with_recovery(
        manager_config_path(),
        _manager_config_defaults(),
        "pi-manager.json",
        # pi-manager.json may hold a proxy URL with embedded credentials.
        private=True,
    )
    return _normalize_manager_config(data)



def save_manager_config(data: dict[str, Any]) -> None:
    # pi-manager.json may hold a proxy URL with embedded credentials.
    save_json(manager_config_path(), data, private=True)



def update_manager_config(
    updater: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """全程持锁地「读 → 改 → 写」pi-manager.json（见 ``_update_config``）。

    这是本项目里争用最激烈的一份配置：GUI 主线程改设置、模型测试线程池写历史、
    健康检查、失败计数、更新检查快照、以及 ``--config-mutate`` helper 进程都在写它。
    """

    def _apply(current: Any) -> dict[str, Any]:
        return updater(_normalize_manager_config(current))

    return _core()._update_config(
        manager_config_path(),
        _manager_config_defaults(),
        _apply,
        private=True,
    )
