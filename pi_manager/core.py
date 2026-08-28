"""
Pi Manager - Cross-platform GUI for managing and launching Pi Coding Agent.
All agent capability comes from the official `pi` CLI; this app manages
providers/models/settings and launches full Pi sessions.
"""
from __future__ import annotations

import copy
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import secrets as secretstore
from . import storage

# ── 以下 6 组是 core_* 子模块的**重新导出**（约 50 个名字，其中 11 个下划线私有名）。
# 它们在 core.py 内部一个都没被使用，纯转发，但**不能删**：
#   1. 下游（ui / extras / rpc_session / provider_env …）按 `core.xxx` 调用；
#   2. `core_process.py` / `core_remote.py` 里明确写着「走 core.xxx 动态查找，
#      使测试 monkeypatch 生效」—— 测试策略与这里的动态查找绑死，
#      `core.pi_base_cmd` / `core.run_pi` / `core._http_json_request` 都是 patch 点。
# 审查 P2-2 建议补 `__all__` 并把 `_http_json_request` / `_terminate_process_tree`
# 之类的私有名改成公开名。改名要同批修改 core_remote.py / core_process.py 与相应
# 测试（不在本次改动的文件集内），因此这里只把契约写清楚，留给 P2-1 的分层重构
# （新建 config_paths.py / config_store.py、把 core.py 退化为门面）一并处理。
# ruff 的 F401 已通过 pyproject.toml 的 per-file-ignores 为本文件豁免。
# HTTP 工具函数已抽到 core_http，此处重新导出以保持 core.xxx 调用兼容。
from .core_http import (
    _friendly_fetch_error,
    _ssl_context,
    normalize_openai_base_url,
    redact_endpoint_url,
    redact_secret_values,
)
# 视觉识图管道已抽到 core_vision，此处重新导出以保持 core.xxx 调用兼容。
from .core_vision import (
    DEFAULT_VISION_PROMPT,
    ZHIPU_API_KEY_SECRET,
    ZHIPU_BASE_URL,
    ZHIPU_VISION_MODELS,
    build_vision_prompt,
    describe_image,
    ensure_zhipu_provider,
    install_vision_skill,
    load_image_for_describe,
    set_vision_model_choice,
    set_zhipu_api_key,
    test_vision,
    vision_model_choice,
    zhipu_api_key,
)
# 进程管理工具已抽到 core_process，此处重新导出以保持 core.xxx 调用兼容。
from .core_process import (
    _check_request_scheme,
    _is_private_host,
    _terminate_process_tree,
    escape_cmd_shim_args,
    find_pi_command,
    list_terminal_options,
    pi_base_cmd,
    proxy_reachable,
    redact_proxy_url,
    run_pi,
    sanitize_proxy_env,
    validate_launch_tokens,
    validate_proxy_url,
)
# 远程模型获取与 HTTP 连通性测试已抽到 core_remote，此处重新导出。
from .core_remote import (
    _extract_reply_preview,
    _http_json_request,
    _resolve_provider_runtime_key,
    fetch_remote_models,
    format_test_summary,
    test_model,
    test_model_http,
    test_model_via_pi,
)
# 会话管理已抽到 core_sessions，此处重新导出。
from .core_sessions import (
    _decode_session_folder_slug,
    _parse_session_meta,
    list_sessions,
    open_in_explorer,
    open_path,
    project_name_from_path,
)
# 凭据与 Provider 密钥已抽到 core_credentials，此处重新导出。
from .core_credentials import (
    ProviderKeyError,
    _is_provider_request_block_error,
    all_provider_runtime_env,
    classify_provider_key_failure,
    is_executable_config_value,
    is_provider_key_error,
    normalize_config_string,
    provider_key_failure_reason,
    provider_runtime_credential,
    provider_runtime_env,
    resolve_api_key_value,
)

logger = logging.getLogger(__name__)


# ==== 基础工具：路径定位 / JSON 读写 / 敏感数据脱敏 ====


def user_home() -> Path:
    return Path(os.path.expanduser("~"))


def pi_agent_dir() -> Path:
    return secretstore.config_dir()


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


def save_json(path: Path, data: Any, *, private: bool = False) -> None:
    ensure_agent_dir()
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


# ==== 模型列表与 Pi 版本 ====


def get_pi_version() -> str:
    """Return Pi's version only when the CLI exits successfully.

    Runtime failures can mention a Node.js version in stderr. Those failures
    must never be parsed as Pi's installed version.
    """
    try:
        process = run_pi(["-v"], timeout=20)
        output = (process.stdout or process.stderr or "").strip()
        first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
        if process.returncode != 0:
            detail = first_line or "\u672a\u8fd4\u56de\u9519\u8bef\u8be6\u60c5"
            return f"error: Pi \u542f\u52a8\u5931\u8d25\uff08\u9000\u51fa\u7801 {process.returncode}\uff09\uff1a{detail}"
        return first_line or "unknown"
    except Exception as exc:
        return f"error: {exc}"


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
        # Provider names may contain spaces or non-ASCII characters (e.g.
        # "opencode go", "中转站"), so only reject obvious junk: a trailing
        # colon identifies warning/error lines ("Warning: ...").
        if not provider or not model or provider.endswith(":"):
            continue
        # Real table rows carry a numeric-ish capability column right after
        # the model id (e.g. "128K", "1M", "32.8K"); free-text lines such as
        # "No models matching ..." never do. When the row is too short to
        # check, keep it.
        if len(parts) >= 3 and not re.match(r"^[0-9.,]+[KM]?$", parts[2]):
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
    ensure_agent_dir()
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

    return _update_config(settings_path(), {}, _apply)


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
# 最近一次跑完三轮迁移时 models.json 的 (mtime_ns, size) 签名。
# 迁移逻辑是幂等的，但每次 load_models_config() 都重跑三轮全量扫描
# （密钥迁移 / User-Agent 头 / thinkingLevelMap）纯属浪费 —— 而它是热路径
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
            migrated = ensure_thinking_level_map(m)
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
    _migrate_models_thinking,
)


def _migrate_models_config(cfg: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """三轮格式迁移的纯变换组合（幂等：``m(m(x)) == m(x)``）。"""
    changed = False
    for step in _MODELS_MIGRATIONS:
        cfg, step_changed = step(cfg)
        changed = changed or step_changed
    return cfg, changed


def load_models_config() -> dict[str, Any]:
    global _MODELS_MIGRATED_SIGNATURE

    cfg = _load_config_with_recovery(models_path(), {"providers": {}}, "models.json")
    if not isinstance(cfg, dict):
        cfg = {"providers": {}}
    if not isinstance(cfg.get("providers"), dict):
        cfg["providers"] = {}
        return cfg

    signature = _models_file_signature()
    with _MODELS_MIGRATION_LOCK:
        if signature is not None and signature == _MODELS_MIGRATED_SIGNATURE:
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
        _MODELS_MIGRATED_SIGNATURE = _models_file_signature()
    return cfg


def save_models_config(data: dict[str, Any]) -> None:
    save_json(models_path(), _sanitize_models_config(data))


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

    return _update_config(models_path(), {"providers": {}}, _apply)


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

    return _update_config(
        manager_config_path(),
        _manager_config_defaults(),
        _apply,
        private=True,
    )


# ==== 默认模型 / 收藏 / 自定义 provider / 模型管理 ====


def normalize_model_pair(
    provider: str | None,
    model: str | None,
    *,
    allow_empty: bool = True,
) -> tuple[str, str] | None:
    """Normalize an atomic Provider/Model pair without mixing partial defaults."""
    p = str(provider or "").strip()
    m = str(model or "").strip()
    if not p and not m and allow_empty:
        return None
    if not p or not m:
        raise ValueError("Provider 和 Model 必须成对指定，不能跨模型混用")
    return p, m


DEFAULT_THINKING_LEVEL = "medium"


def set_default_model(provider: str, model: str, thinking: str | None = None) -> dict[str, Any]:
    pair = normalize_model_pair(provider, model, allow_empty=False)
    assert pair is not None
    provider, model = pair

    def _apply(settings: dict[str, Any]) -> Any:
        settings["defaultProvider"] = provider
        settings["defaultModel"] = model
        if thinking:
            settings["defaultThinkingLevel"] = thinking
        return settings

    return update_settings(_apply)


def get_default_model() -> tuple[str, str, str]:
    s = load_settings()
    return (
        str(s.get("defaultProvider") or ""),
        str(s.get("defaultModel") or ""),
        str(s.get("defaultThinkingLevel") or DEFAULT_THINKING_LEVEL),
    )


def set_enabled_models(patterns: list[str]) -> dict[str, Any]:
    def _apply(settings: dict[str, Any]) -> Any:
        if patterns:
            settings["enabledModels"] = list(patterns)
        else:
            settings.pop("enabledModels", None)
        return settings

    return update_settings(_apply)


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
    from . import secrets as secretstore

    # 密钥入库放在锁外：它写的是密钥库（另一份存储），且不依赖 models.json 的
    # 当前内容。只有「沿用已存在的 apiKey」这一支必须在锁内读，见 _apply。
    stored_key: str | None = None
    if api_key is not None:
        stored_key = secretstore.store_provider_api_key(name, str(api_key).strip())

    def _apply(cfg: dict[str, Any]) -> Any:
        providers = cfg.setdefault("providers", {})
        existing = providers.get(name) if isinstance(providers.get(name), dict) else {}
        existing = existing or {}
        raw_key = (
            stored_key
            if stored_key is not None
            else str(existing.get("apiKey") or "")
        )
        saved_models = [
            ensure_thinking_level_map(m)
            for m in (models if models is not None else existing.get("models", []))
        ]
        entry: dict[str, Any] = {
            "baseUrl": base_url,
            "api": api,
            "apiKey": raw_key,
            "models": saved_models,
        }
        if compat is not None:
            entry["compat"] = compat
        elif "compat" in existing:
            entry["compat"] = existing["compat"]
        header_source = headers if headers is not None else existing.get("headers")
        if isinstance(header_source, dict) or header_source is None:
            effective_headers = _openai_compat_headers(api, header_source)
            if effective_headers or headers is not None or "headers" in existing:
                entry["headers"] = secretstore.store_provider_headers(
                    name, effective_headers
                )
        elif "headers" in existing:
            entry["headers"] = existing["headers"]
        providers[name] = entry
        return cfg

    cfg = update_models_config(_apply)
    # 保存 provider 后用户即将使用 Pi：确保所有内置插件（含 vision skill）
    # 已落盘，让图片处理等开箱即用。委托给 builtin_plugins 统一机制。
    try:
        from . import builtin_plugins
        builtin_plugins.install_all_builtins()
    except Exception as exc:
        logger.warning("安装内置插件失败: %s", exc)
    return cfg


def parse_favorite_key(key: str) -> tuple[str, str] | None:
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
    kept: list[str] = []
    removed: list[str] = []

    def _apply(mgr: dict[str, Any]) -> Any:
        nonlocal kept, removed
        kept, removed = [], []
        favs = list(mgr.get("favorites") or [])
        for key in favs:
            parsed = parse_favorite_key(str(key))
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
        # favs != kept 也算改动：磁盘上可能存着非字符串收藏项，顺手规范化。
        if not removed and favs == kept:
            return storage.UNCHANGED
        mgr["favorites"] = kept
        return mgr

    update_manager_config(_apply)

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
            parsed = parse_favorite_key(str(key))
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
            def _clear_default(settings: dict[str, Any]) -> Any:
                settings["defaultProvider"] = ""
                settings["defaultModel"] = ""
                return settings

            update_settings(_clear_default)
            result["default_changed"] = True
            result["default_provider"] = ""
            result["default_model"] = ""
    else:
        result["default_provider"] = cur_p
        result["default_model"] = cur_m

    return result


def purge_enabled_models(
    *,
    provider: str | None = None,
    model: str | None = None,
) -> list[str]:
    """从 settings.enabledModels 中移除指向已删除 Provider/模型 的残留模式。

    - 仅 provider：移除该 Provider 下全部模式（如 ``name/model``）
    - provider + model：只移除精确匹配 ``provider/model`` 的模式
    - 纯模型名（不含 ``/``）不参与匹配，原样保留
    """
    provider = (provider or "").strip()
    model = (model or "").strip()
    removed: list[str] = []

    def _apply(settings: dict[str, Any]) -> Any:
        nonlocal removed
        removed = []
        patterns = settings.get("enabledModels")
        if not isinstance(patterns, list):
            return storage.UNCHANGED
        kept: list[str] = []
        for pattern in patterns:
            key = str(pattern)
            parsed = parse_favorite_key(key)
            if not parsed:
                kept.append(key)
                continue
            p, m = parsed
            drop = False
            if provider and model:
                drop = p == provider and m == model
            elif provider:
                drop = p == provider
            if drop:
                removed.append(key)
            else:
                kept.append(key)
        if not removed:
            return storage.UNCHANGED
        if kept:
            settings["enabledModels"] = kept
        else:
            settings.pop("enabledModels", None)
        return settings

    update_settings(_apply)
    return removed


def list_stale_enabled_models(builtin_providers: set[str] | None = None) -> list[str]:
    """返回 settings.enabledModels 中引用已不存在 Provider 的残留模式。

    ``builtin_providers`` 传入 Pi 内置 Provider 名集合时可避免误报；
    不传时仅与 models.json 中的自定义 Provider 比对。
    """
    settings = load_settings()
    patterns = settings.get("enabledModels")
    if not isinstance(patterns, list):
        return []
    cfg = load_models_config()
    custom = set(cfg.get("providers") or {})
    stale: list[str] = []
    for pattern in patterns:
        parsed = parse_favorite_key(str(pattern))
        if not parsed:
            continue
        p, _m = parsed
        if p in custom:
            continue
        if builtin_providers and p in builtin_providers:
            continue
        stale.append(str(pattern))
    return stale


def delete_custom_provider(name: str) -> dict[str, Any]:
    """删除自定义 Provider 及其收藏 / enabledModels / 密钥池残留。

    跨 4 份持久状态（pi-manager.json、settings.json、密钥库、models.json）无法做
    真正的事务，所以顺序上「先删依赖、后删主体」：中途失败留下的是「Provider 还在
    但引用已清」—— 用户重试即可收敛；反过来（Provider 没了、引用还在）会留下悬挂
    状态，只能靠 `run_self_check` 事后发现。失败原因聚合进 ``_partial_failures``，
    让 UI 能提示用户而不是静默吞掉。
    """
    failures: list[str] = []

    # 1. 收藏（含默认模型改指）
    purge: dict[str, Any] = {
        "removed_favorites": [],
        "favorites": [],
        "default_changed": False,
    }
    try:
        purge = purge_favorites(provider=name, redefault=True)
    except Exception as exc:
        logger.warning("删除 provider「%s」后清理收藏失败: %s", name, exc)
        failures.append(f"清理收藏失败：{exc}")

    # 2. settings.enabledModels 残留模式，避免 Pi 每次启动输出
    #    "No models match pattern" 警告并污染测试结果
    purged_enabled: list[str] = []
    try:
        purged_enabled = purge_enabled_models(provider=name)
    except Exception as exc:
        logger.warning("删除 provider「%s」后清理 enabledModels 失败: %s", name, exc)
        failures.append(f"清理 enabledModels 失败：{exc}")

    # 3. 密钥池 / 头部密钥（要在删掉 provider 条目之前读出 headers）
    existing_entry = get_provider_config(name)
    try:
        from . import secrets as secretstore

        if isinstance(existing_entry, dict) and isinstance(
            existing_entry.get("headers"), dict
        ):
            secretstore.delete_provider_header_secrets(name, existing_entry["headers"])
        secretstore.delete_provider_api_keys(name)
    except Exception as exc:
        logger.warning("删除 provider「%s」的密钥/头清理失败: %s", name, exc)
        failures.append(f"清理密钥失败：{exc}")

    # 4. 主体：models.json 里的 provider 条目
    def _apply(cfg: dict[str, Any]) -> Any:
        providers = cfg.setdefault("providers", {})
        if name not in providers:
            return storage.UNCHANGED
        del providers[name]
        return cfg

    cfg = update_models_config(_apply)

    # _purge / _purged_enabled 是给 UI 的返回通道，不属于 models.json schema；
    # 挂在返回值的副本上，并由 save_models_config 兜底拦截（见 _sanitize_models_config）。
    result = dict(cfg) if isinstance(cfg, dict) else {"providers": {}}
    result["_purge"] = purge
    result["_purged_enabled"] = purged_enabled
    if failures:
        result["_partial_failures"] = failures
    return result


# Default mapping from Pi thinking levels to OpenAI-style reasoning_effort
# values. Pi treats xhigh/max as unsupported when a reasoning model has no
# thinkingLevelMap, silently clamping max down to high — so fill it in.
DEFAULT_THINKING_LEVEL_MAP: dict[str, str] = {
    "off": "none",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "xhigh",
    "max": "max",
}


def ensure_thinking_level_map(model: dict[str, Any]) -> dict[str, Any]:
    """Fill a default thinkingLevelMap for reasoning models missing one.

    Without a thinkingLevelMap, Pi's getSupportedThinkingLevels() drops
    xhigh/max, and clampThinkingLevel() demotes max to high. Only touch
    models that support reasoning and have no explicit map, so user-provided
    custom mappings are preserved.
    """
    if not isinstance(model, dict):
        return model
    if not model.get("reasoning") or model.get("thinkingLevelMap"):
        return model
    result = dict(model)
    result["thinkingLevelMap"] = dict(DEFAULT_THINKING_LEVEL_MAP)
    return result


def add_model_to_provider(provider: str, model_id: str, **kwargs: Any) -> dict[str, Any]:
    def _apply(cfg: dict[str, Any]) -> Any:
        providers = cfg.setdefault("providers", {})
        entry = providers.get(provider)
        if not isinstance(entry, dict):
            raise KeyError(f"provider not found: {provider}")
        models = entry.get("models")
        # replace if exists（非 dict 条目原样保留，别让脏数据把整个操作打崩）
        kept = [
            m
            for m in (models if isinstance(models, list) else [])
            if not (isinstance(m, dict) and m.get("id") == model_id)
        ]
        item = ensure_thinking_level_map({"id": model_id, **kwargs})
        if "cost" not in item:
            item["cost"] = {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}
        kept.append(item)
        entry["models"] = kept
        return cfg

    return update_models_config(_apply)


def remove_model_from_provider(provider: str, model_id: str) -> dict[str, Any]:
    def _apply(cfg: dict[str, Any]) -> Any:
        providers = cfg.setdefault("providers", {})
        entry = providers.get(provider)
        if not isinstance(entry, dict):
            return storage.UNCHANGED
        models = entry.get("models")
        if not isinstance(models, list):
            return storage.UNCHANGED
        kept = [
            m for m in models if not (isinstance(m, dict) and m.get("id") == model_id)
        ]
        if kept == models:
            return storage.UNCHANGED
        entry["models"] = kept
        return cfg

    cfg = update_models_config(_apply)

    result = dict(cfg) if isinstance(cfg, dict) else {"providers": {}}
    try:
        result["_purge"] = purge_favorites(
            provider=provider, model=model_id, redefault=True
        )
    except Exception as exc:
        logger.warning("移除模型后清理收藏失败: %s", exc)
        result["_purge"] = {
            "removed_favorites": [],
            "favorites": [],
            "default_changed": False,
        }
    try:
        result["_purged_enabled"] = purge_enabled_models(
            provider=provider, model=model_id
        )
    except Exception as exc:
        logger.warning("移除模型后清理 enabledModels 失败: %s", exc)
        result["_purged_enabled"] = []
    return result



def build_pi_launch_args(
    *,
    provider: str | None = None,
    model: str | None = None,
    thinking: str | None = None,
    extra: list[str] | None = None,
) -> list[str]:
    args: list[str] = []
    pair = normalize_model_pair(provider, model)
    if pair is not None:
        pair_provider, pair_model = pair
        args += ["--provider", pair_provider, "--model", pair_model]
    if thinking:
        args += ["--thinking", thinking]
    if extra:
        args += extra
    # 构造期就校验，让非法 provider/model 名在离用户操作最近的地方报错。
    # escape_cmd_shim_args 这个统一出口也会拦（安全性已由它保证），但那时错误
    # 信息已经离触发点很远了（审查 P0-1 的「三处同时拦截」要求）。
    validate_launch_tokens(args)
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
    pi_args = append_vision_args(pi_args)
    base = pi_base_cmd()
    # Mirror run_pi: when the pi launcher is a cmd.exe batch shim, cmd.exe
    # re-expands %VAR% in the command line (e.g. %TEMP% in the vision rule)
    # before the script runs. Escape percents so args stay literal.
    pi_args = escape_cmd_shim_args(pi_args, base)
    full_cmd_list = base + pi_args
    workdir = workdir or str(user_home())
    if provider:
        child_env = provider_runtime_env(provider)
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
) -> tuple[int, str, str]:
    args = build_pi_launch_args(provider=provider, model=model, thinking=thinking)
    args = append_language_args(args)
    args += ["-p", "--no-session", prompt]
    # project trust for non-interactive
    args += ["--approve"]
    attempted_key_ids: set[str] = set()
    while True:
        credential = provider_runtime_credential(provider)
        p = run_pi(
            args,
            cwd=workdir,
            timeout=timeout,
            env=credential["env"],
        )
        stdout = p.stdout or ""
        stderr = p.stderr or ""
        key_id = str(credential.get("key_id") or "")
        if p.returncode == 0 or not key_id or not is_provider_key_error(
            p.returncode, stdout, stderr
        ):
            return p.returncode, stdout, stderr
        if key_id in attempted_key_ids:
            return p.returncode, stdout, stderr
        attempted_key_ids.add(key_id)

        from . import secrets as secretstore

        reason = provider_key_failure_reason(p.returncode, stdout, stderr)
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
    def _apply(cfg: dict[str, Any]) -> Any:
        cfg["language"] = lang
        return cfg

    update_manager_config(_apply)
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
    """Write global AGENTS.md language block so Pi sessions use the preference.

    ``~/.pi/agent/AGENTS.md`` 是**用户的全局 Pi 指令文件**，可能有大量手写内容，
    所以读-改-写整体走 ``storage.locked`` + ``storage.save_text``：
    - 原子替换：``Path.write_text`` 是先截断再写，中途崩溃/磁盘满会把文件截成半截；
    - 持锁：GUI 与 ``--config-mutate`` helper 进程可能同时改写，后写者整份覆盖；
    - 备份轮转：出错后还有 ``AGENTS.md.bak.1/.bak.2`` 可退。
    """
    lang = lang or get_language()
    ensure_agent_dir()
    path = agents_md_path()
    with storage.locked(path):
        # Tolerate non-UTF-8 bytes (e.g. a user edited AGENTS.md as GBK/ANSI on
        # Windows): replacing bad bytes lets the save proceed and rewrites the
        # file as clean UTF-8, instead of crashing the language preference action.
        existing = (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )
        existing = _LANG_BLOCK_RE.sub("", existing).rstrip() + "\n"
        body = language_prompt_text(lang).strip()
        if body:
            block = (
                "\n<!-- PI-MANAGER-LANG-START -->\n"
                f"{body}\n"
                "<!-- PI-MANAGER-LANG-END -->\n"
            )
            existing = existing.rstrip() + "\n" + block
        storage.save_text(
            path,
            existing.lstrip() + ("\n" if not existing.endswith("\n") else ""),
        )
    return path


def append_language_args(args: list[str], lang: str | None = None) -> list[str]:
    """Add --append-system-prompt for launch-time language enforcement."""
    text = language_prompt_text(lang)
    if text.strip():
        args = list(args) + ["--append-system-prompt", text.strip()]
    return args


def _helper_command_text() -> str:
    """Shell-quoted helper command for prompts/skills (single source of truth)."""
    import shlex

    try:
        from .helper_registry import current_helper_command

        return " ".join(shlex.quote(str(part)) for part in current_helper_command())
    except Exception:
        if getattr(sys, "frozen", False):
            return shlex.quote(sys.executable)
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(main_path))}"


def _vision_rule_prompt() -> str:
    """System-prompt rule appended when launching Pi: images are always
    routed through the free Zhipu vision model first — never sent straight to
    a (possibly text-only) provider model."""
    command = _helper_command_text()
    return (
        "## 图片处理规则（必须遵守）\n"
        "- 用户发送图片、粘贴截图或要求查看图片时：绝不要尝试把图片直接发送给当前对话模型"
        "（纯文本模型会报 image_url 错误）。\n"
        "- 先获取图片文件路径：用户提供，或从系统临时目录（%TEMP%）查找最新的 "
        "pi-clipboard-*.png 文件。\n"
        f"- 运行识图命令：{command} --vision-describe \"<图片路径>\" \"<用户问题，可空>\"\n"
        "- 将命令输出的文字描述视为图片内容，结合用户问题回答。\n"
        "- 若提示未配置智谱 API Key，请告知用户在 Pi Manager「设置 → 识图模型」中配置。"
    )


def append_vision_args(args: list[str]) -> list[str]:
    """Add the image-routing system-prompt rule at launch time."""
    try:
        text = _vision_rule_prompt()
    except Exception:
        return list(args)
    return list(args) + ["--append-system-prompt", text]


def apply_theme(theme_name: str) -> dict[str, Any]:
    from .builtin_themes import ensure_builtin_themes

    ensure_builtin_themes()

    def _apply(settings: dict[str, Any]) -> Any:
        settings["theme"] = theme_name
        return settings

    return update_settings(_apply)


def get_theme() -> str:
    return str(load_settings().get("theme") or "dark")


def list_themes() -> list[tuple[str, str]]:
    from .builtin_themes import list_theme_choices

    return list_theme_choices()


PI_NPM_PACKAGE = "@earendil-works/pi-coding-agent"
PI_LATEST_TAG = "latest"
PI_LEGACY_NODE20_TAG = "legacy-node20"
PI_LATEST_MIN_NODE = (22, 19, 0)
PI_LEGACY_MIN_NODE = (20, 6, 0)


def _npm_command(*args: str) -> list[str]:
    """Resolve npm's Windows command shim without invoking a shell."""
    names = ("npm.cmd", "npm") if sys.platform == "win32" else ("npm",)
    executable = next((path for name in names if (path := shutil.which(name))), names[0])
    return [executable, *args]


def _node_command(*args: str) -> list[str]:
    names = ("node.exe", "node") if sys.platform == "win32" else ("node",)
    executable = next((path for name in names if (path := shutil.which(name))), names[0])
    return [executable, *args]


def _run_version_command(command: list[str], timeout: float = 20) -> str | None:
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        return None
    if process.returncode != 0:
        return None
    output = (process.stdout or process.stderr or "").strip()
    match = re.search(r"(?:^|\D)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", output)
    return match.group(1) if match else None


def get_node_version(timeout: float = 20) -> str | None:
    """Return the active Node.js semantic version without a leading v."""
    return _run_version_command(_node_command("--version"), timeout=timeout)


def get_npm_version(timeout: float = 20) -> str | None:
    """Return the active npm semantic version."""
    return _run_version_command(_npm_command("--version"), timeout=timeout)


def select_pi_install_channel(node_version: str | None = None) -> str | None:
    """Select the npm dist-tag compatible with the active Node.js runtime."""
    version = node_version if node_version is not None else get_node_version()
    if not version:
        return None
    parsed = parse_semver(version)
    if parsed >= PI_LATEST_MIN_NODE:
        return PI_LATEST_TAG
    if parsed >= PI_LEGACY_MIN_NODE:
        return PI_LEGACY_NODE20_TAG
    return None


def pi_package_spec(channel: str | None) -> str | None:
    return f"{PI_NPM_PACKAGE}@{channel}" if channel else None


def get_latest_pi_version(timeout: float = 20, tag: str | None = None) -> str | None:
    """Return the newest Pi version for an npm compatibility channel."""
    channel = tag or select_pi_install_channel() or PI_LATEST_TAG
    try:
        process = subprocess.run(
            _npm_command("view", f"{PI_NPM_PACKAGE}@{channel}", "version"),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception:
        return None
    if process.returncode != 0:
        return None
    output = (process.stdout or "").strip()
    match = re.search(r"(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", output)
    return match.group(1) if match else None


def get_installed_pi_version() -> str | None:
    value = get_pi_version()
    if not value or value.startswith("error:") or value == "unknown":
        return None
    match = re.search(r"(?:^|\D)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", value)
    return match.group(1) if match else None


def parse_semver(v: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", str(v or ""))
    values = tuple(int(x) for x in parts[:3]) if parts else (0,)
    return values + (0,) * (3 - len(values))


def get_pi_runtime_status() -> dict[str, Any]:
    """Inspect whether the Pi command exists and can actually start."""
    command = find_pi_command()
    if not command:
        return {
            "command": None,
            "installed": None,
            "raw_version": None,
            "missing": True,
            "runtime_broken": False,
            "ok": False,
            "error": "\u672a\u627e\u5230 Pi \u547d\u4ee4\u3002",
        }
    raw_version = get_pi_version()
    if raw_version.startswith("error:") or raw_version == "unknown":
        error = raw_version.removeprefix("error:").strip() or "Pi \u65e0\u6cd5\u542f\u52a8\u3002"
        return {
            "command": command,
            "installed": None,
            "raw_version": raw_version,
            "missing": False,
            "runtime_broken": True,
            "ok": False,
            "error": error,
        }
    match = re.search(r"(?:^|\D)(\d+\.\d+\.\d+(?:[-+][\w.-]+)?)", raw_version)
    installed = match.group(1) if match else None
    if not installed:
        return {
            "command": command,
            "installed": None,
            "raw_version": raw_version,
            "missing": False,
            "runtime_broken": True,
            "ok": False,
            "error": f"\u65e0\u6cd5\u89e3\u6790 Pi \u7248\u672c\u8f93\u51fa\uff1a{raw_version}",
        }
    return {
        "command": command,
        "installed": installed,
        "raw_version": raw_version,
        "missing": False,
        "runtime_broken": False,
        "ok": True,
        "error": "",
    }


def needs_pi_install_or_update() -> dict[str, Any]:
    """Return actionable Pi runtime, registry, and compatibility status."""
    node_version = get_node_version()
    npm_version = get_npm_version()
    channel = select_pi_install_channel(node_version)
    package_spec = pi_package_spec(channel)
    runtime = get_pi_runtime_status()

    blocked_reason = ""
    if not node_version:
        blocked_reason = "\u672a\u68c0\u6d4b\u5230 Node.js\u3002\u8bf7\u5148\u5b89\u88c5 Node.js 20.6 \u6216\u66f4\u9ad8\u7248\u672c\u3002"
    elif parse_semver(node_version) < PI_LEGACY_MIN_NODE:
        blocked_reason = (
            f"\u5f53\u524d Node.js {node_version} \u8fc7\u4f4e\uff1bPi \u81f3\u5c11\u9700\u8981 Node.js 20.6\uff0c"
            "\u63a8\u8350\u5347\u7ea7\u5230 22.19 \u6216\u66f4\u9ad8\u7248\u672c\u3002"
        )
    elif not npm_version:
        blocked_reason = "\u672a\u68c0\u6d4b\u5230\u53ef\u7528\u7684 npm\u3002\u8bf7\u4fee\u590d Node.js/npm \u5b89\u88c5\u540e\u91cd\u8bd5\u3002"

    installable = bool(channel and npm_version and not blocked_reason)
    latest = get_latest_pi_version(tag=channel) if installable else None
    registry_ok = bool(latest)
    check_failed = bool(installable and not registry_ok)
    installed = runtime.get("installed")
    missing = bool(runtime.get("missing"))
    runtime_broken = bool(runtime.get("runtime_broken"))
    repair_required = runtime_broken
    outdated = bool(
        installed and latest and parse_semver(str(installed)) < parse_semver(str(latest))
    )

    result: dict[str, Any] = {
        "installed": installed,
        "latest": latest,
        "missing": missing,
        "outdated": outdated,
        "ok": False,
        "message": "",
        "registry_ok": registry_ok,
        "check_failed": check_failed,
        "runtime_broken": runtime_broken,
        "repair_required": repair_required,
        "installable": installable,
        "blocked": bool(blocked_reason),
        "node_version": node_version,
        "npm_version": npm_version,
        "channel": channel,
        "package_spec": package_spec,
        "error": "",
        "command": runtime.get("command"),
    }

    channel_label = "\u6700\u65b0\u7248\u901a\u9053" if channel == PI_LATEST_TAG else "Node 20 \u517c\u5bb9\u901a\u9053"
    channel_detail = f"{channel_label}\uff08{channel}\uff09" if channel else "\u65e0\u517c\u5bb9\u901a\u9053"
    if blocked_reason:
        runtime_detail = f" \u5f53\u524d Pi\uff1a{installed}\u3002" if installed else ""
        result["message"] = blocked_reason + runtime_detail
        result["error"] = blocked_reason
        return result
    if runtime_broken:
        detail = str(runtime.get("error") or "Pi \u65e0\u6cd5\u542f\u52a8")
        result["message"] = (
            f"\u68c0\u6d4b\u5230 Pi \u547d\u4ee4\uff0c\u4f46\u8fd0\u884c\u5931\u8d25\uff1a{detail}\n"
            f"\u53ef\u901a\u8fc7 {package_spec} \u6267\u884c\u4fee\u590d\u5b89\u88c5\u3002"
        )
        result["error"] = detail
        return result
    if check_failed:
        installed_detail = f"\u5f53\u524d\u5df2\u5b89\u88c5 {installed}\uff0c" if installed else ""
        result["message"] = (
            f"{installed_detail}\u4f46\u65e0\u6cd5\u4ece npm registry \u83b7\u53d6 {channel_detail} \u7684\u7248\u672c\u4fe1\u606f\u3002"
            "\u8bf7\u68c0\u67e5\u7f51\u7edc\u3001\u4ee3\u7406\u6216 npm registry \u914d\u7f6e\u540e\u91cd\u8bd5\u3002"
        )
        result["error"] = "npm registry \u7248\u672c\u67e5\u8be2\u5931\u8d25"
        return result
    if missing:
        result["message"] = (
            f"\u672a\u68c0\u6d4b\u5230 Pi\u3002\u5f53\u524d Node.js {node_version}\uff0c\u5c06\u5b89\u88c5 {channel_detail}"
            f"\uff08\u76ee\u6807 {latest}\uff09\u3002"
        )
        return result
    if outdated:
        result["message"] = (
            f"\u5df2\u5b89\u88c5 Pi {installed}\uff0c{channel_detail} \u6700\u65b0\u4e3a {latest}\uff0c\u5efa\u8bae\u5347\u7ea7\u3002"
        )
        return result

    result["ok"] = True
    result["message"] = (
        f"Pi \u5df2\u5c31\u7eea\uff08{installed}\uff0c{channel_detail} \u6700\u65b0 {latest}\uff1b"
        f"Node.js {node_version}\uff0cnpm {npm_version}\uff09"
    )
    return result


def pi_update_state(status: dict[str, Any]) -> str:
    """Classify a needs_pi_install_or_update() result into a UI state."""
    if status.get("check_failed"):
        return "check_failed"
    if status.get("blocked"):
        return "blocked"
    if status.get("missing"):
        return "missing"
    if status.get("runtime_broken") or status.get("repair_required"):
        return "repair_required"
    if status.get("outdated"):
        return "outdated"
    if status.get("ok"):
        return "ok"
    return "unknown"


def check_pi_status() -> dict[str, Any]:
    """Run the Pi update check and persist a compact snapshot for the UI."""
    status = needs_pi_install_or_update()
    from datetime import datetime

    snapshot = {
        "state": pi_update_state(status),
        "installed": status.get("installed"),
        "latest": status.get("latest"),
        "channel": status.get("channel"),
        "message": str(status.get("message") or ""),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
    }
    def _apply(cfg: dict[str, Any]) -> Any:
        cfg["pi_update_status"] = snapshot
        cfg["last_update_check"] = snapshot["checked_at"]
        return cfg

    update_manager_config(_apply)
    return status


def load_pi_update_status() -> dict[str, Any]:
    return dict(load_manager_config().get("pi_update_status") or {})


def is_update_dismissed(kind: str, version: str) -> bool:
    """True when the user dismissed this update before (same version)."""
    if not version:
        return False
    key = f"{kind}@{str(version).strip()}"
    return key in {str(x) for x in (load_manager_config().get("dismissed_updates") or [])}


def dismiss_update(kind: str, version: str) -> None:
    """Remember the user dismissed this kind@version so it stops nagging."""
    if not version:
        return
    key = f"{kind}@{str(version).strip()}"

    def _apply(cfg: dict[str, Any]) -> Any:
        entries = [str(x) for x in (cfg.get("dismissed_updates") or [])]
        if key in entries:
            return storage.UNCHANGED
        entries.append(key)
        cfg["dismissed_updates"] = entries
        return cfg

    update_manager_config(_apply)


def install_or_update_pi(timeout: float = 300) -> tuple[int, str, str]:
    """Install the Node-compatible Pi channel and verify the resulting CLI."""
    node_version = get_node_version()
    npm_version = get_npm_version()
    channel = select_pi_install_channel(node_version)
    if not node_version:
        return 2, "", "\u672a\u68c0\u6d4b\u5230 Node.js\uff1b\u8bf7\u5148\u5b89\u88c5 Node.js 20.6 \u6216\u66f4\u9ad8\u7248\u672c\u3002"
    if parse_semver(node_version) < PI_LEGACY_MIN_NODE or not channel:
        return (
            2,
            "",
            f"\u5f53\u524d Node.js {node_version} \u8fc7\u4f4e\uff1b\u8bf7\u5347\u7ea7\u5230 20.6 \u6216\u66f4\u9ad8\u7248\u672c\uff08\u63a8\u8350 22.19+\uff09\u3002",
        )
    if not npm_version:
        return 2, "", "\u672a\u68c0\u6d4b\u5230\u53ef\u7528\u7684 npm\uff1b\u8bf7\u4fee\u590d Node.js/npm \u5b89\u88c5\u3002"

    package_spec = pi_package_spec(channel)
    target_version = get_latest_pi_version(timeout=min(timeout, 30), tag=channel)
    if not target_version:
        return (
            3,
            "",
            f"\u65e0\u6cd5\u4ece npm registry \u83b7\u53d6 {package_spec} \u7684\u7248\u672c\u4fe1\u606f\uff1b\u672a\u6267\u884c\u5b89\u88c5\u3002",
        )

    command = _npm_command("install", "-g", str(package_spec))
    try:
        process = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
    except Exception as exc:
        return 1, "", str(exc)
    stdout = process.stdout or ""
    stderr = process.stderr or ""
    if process.returncode != 0:
        return process.returncode, stdout, stderr

    runtime = get_pi_runtime_status()
    installed = runtime.get("installed")
    if not runtime.get("ok") or not installed:
        detail = str(runtime.get("error") or "npm \u5b89\u88c5\u5b8c\u6210\uff0c\u4f46 Pi \u4ecd\u65e0\u6cd5\u542f\u52a8\u3002")
        return 4, stdout, (stderr + "\n" + detail).strip()
    if parse_semver(str(installed)) < parse_semver(target_version):
        detail = (
            f"npm \u5df2\u5b89\u88c5 {package_spec} {target_version}\uff0c\u4f46 PATH \u4e2d\u5b9e\u9645\u8fd0\u884c\u7684 Pi \u4ecd\u4e3a "
            f"{installed}\u3002\u8bf7\u68c0\u67e5\u65e7\u7684 pi \u547d\u4ee4\u6216 npm \u5168\u5c40 bin \u8def\u5f84\u3002"
        )
        return 5, stdout, (stderr + "\n" + detail).strip()

    verified = (
        f"\u5df2\u9a8c\u8bc1 Pi {installed}\uff08{channel} \u901a\u9053\uff0c\u76ee\u6807 {target_version}\uff1b"
        f"Node.js {node_version}\uff0cnpm {npm_version}\uff09"
    )
    return 0, (stdout.rstrip() + ("\n" if stdout.strip() else "") + verified + "\n"), stderr

def is_setup_done() -> bool:
    return bool(load_manager_config().get("setup_done"))


def mark_setup_done(done: bool = True) -> None:
    def _apply(cfg: dict[str, Any]) -> Any:
        cfg["setup_done"] = bool(done)
        return cfg

    update_manager_config(_apply)


def run_first_time_bootstrap() -> None:
    """Ensure language block + themes exist."""
    from .builtin_themes import ensure_builtin_themes

    ensure_builtin_themes()
    apply_language_preference(get_language())

def normalize_ui_mode(mode: str | None) -> str:
    value = str(mode or "night").strip().lower()
    return "day" if value in {"day", "light", "\u767d\u5929"} else "night"


def cli_theme_for_ui_mode(mode: str | None) -> str:
    """Map the global UI mode to Pi CLI's matching built-in theme."""
    return "light" if normalize_ui_mode(mode) == "day" else "dark"


def sync_cli_theme_with_ui(mode: str | None = None) -> str:
    """Persist Pi CLI's theme so it always follows the manager's global mode."""
    normalized = normalize_ui_mode(mode or get_ui_theme().get("mode"))
    theme = cli_theme_for_ui_mode(normalized)

    def _apply(settings: dict[str, Any]) -> Any:
        # 「是否需要改」的判定必须在锁内做，否则锁外预检可能被并发写入推翻。
        if settings.get("theme") == theme:
            return storage.UNCHANGED
        settings["theme"] = theme
        return settings

    update_settings(_apply)
    return theme


_UI_ACCENTS = frozenset({"blue", "green", "purple", "orange", "cyan"})


def _normalize_ui_accent(accent: Any) -> str:
    value = str(accent or "blue").strip().lower()
    return value if value in _UI_ACCENTS else "blue"


def get_ui_theme() -> dict[str, str]:
    cfg = load_manager_config()
    return {
        "mode": normalize_ui_mode(str(cfg.get("ui_mode") or "night")),
        "accent": _normalize_ui_accent(cfg.get("ui_accent")),
    }


def set_ui_theme(mode: str | None = None, accent: str | None = None) -> dict[str, str]:
    result: dict[str, str] = {}

    def _apply(cfg: dict[str, Any]) -> Any:
        nonlocal result
        # 「未指定则保留当前值」的当前值从锁内的同一份快照里读，而不是另做一次
        # load_manager_config() —— 否则两次读之间的并发写入会被这次整份覆盖回退。
        mode_name = normalize_ui_mode(
            mode if mode is not None else str(cfg.get("ui_mode") or "night")
        )
        accent_name = _normalize_ui_accent(
            accent if accent is not None else cfg.get("ui_accent")
        )
        cfg["ui_mode"] = mode_name
        cfg["ui_accent"] = accent_name
        result = {"mode": mode_name, "accent": accent_name}
        return cfg

    update_manager_config(_apply)
    # 锁已释放后再同步 CLI 主题：settings.json 与 pi-manager.json 的锁不嵌套，
    # 避免与其它「先 settings 后 manager」的调用方构成跨进程 ABBA 死锁。
    sync_cli_theme_with_ui(result["mode"])
    return result


# ==== HTTP 工具：URL 规范化 / SSL 上下文 / 端点脱敏 / 友好错误 ====
# 已抽到 pi_manager/core_http.py，此处通过顶部 import 重新导出，保持 core.xxx 兼容。


# vision 子系统（智谱识图管道）已抽到 pi_manager/core_vision.py，
# 顶部重新导出保持 core.xxx 兼容。_effective_proxy_url 留在 core（被
# fetch_remote_models / _http_json_request 共用）。


def _effective_proxy_url(explicit: str = "") -> str:
    """Resolve the proxy for an outgoing request (explicit > config > env).

    Invalid (non-http(s) scheme or missing host) values are dropped with a
    warning instead of being handed to urllib.
    """
    candidates: list[str] = []
    explicit = (explicit or "").strip()
    if explicit:
        candidates.append(explicit)
    try:
        cfg = load_manager_config()
        if not explicit and cfg.get("proxy_enabled") and cfg.get("proxy_url"):
            candidates.append(str(cfg.get("proxy_url") or "").strip())
    except Exception:
        pass
    if not candidates:
        for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            value = (os.environ.get(var) or "").strip()
            if value:
                candidates.append(value)
                break
    for value in candidates:
        error = validate_proxy_url(value)
        if error:
            # 代理 URL 可含 user:pass@，不能整串进日志（审查 P2-6）
            logger.warning("忽略无效代理地址「%s」: %s", redact_proxy_url(value), error)
            continue
        return value
    return ""



# ==== Provider 配置查询 / 密钥池管理 / 配置备份 ====


def get_provider_config(provider: str) -> dict[str, Any] | None:
    """Return custom provider entry from models.json, if any."""
    if not provider:
        return None
    cfg = load_models_config()
    providers = cfg.get("providers") or {}
    entry = providers.get(provider)
    return entry if isinstance(entry, dict) else None


def list_orphaned_provider_keys() -> list[dict[str, Any]]:
    """Return key pools stored in the secret store with no matching provider config.

    A provider deleted outside this app (or by an older version) leaves its
    key pool behind; this surfaces those leftovers so they can be cleaned.
    """
    from . import secrets as secretstore

    cfg = load_models_config()
    providers = cfg.get("providers") or {}
    orphaned: list[dict[str, Any]] = []
    for provider, _pool_name, _single_name in secretstore.provider_pool_names():
        if provider in providers:
            continue
        keys = secretstore.list_provider_keys(provider)
        orphaned.append(
            {
                "provider": provider,
                "key_count": len(keys),
                "statuses": sorted({str(k.get("status") or "") for k in keys}),
                "masked": [str(k.get("masked") or "") for k in keys][:3],
            }
        )
    return orphaned


def delete_orphaned_provider_keys() -> int:
    """Delete key pools whose provider no longer exists in models.json."""
    from . import secrets as secretstore

    cfg = load_models_config()
    providers = cfg.get("providers") or {}
    deleted = 0
    for provider, _pool_name, _single_name in secretstore.provider_pool_names():
        if provider in providers:
            continue
        try:
            secretstore.delete_provider_api_keys(provider)
            deleted += 1
        except Exception:
            pass
    return deleted


_BACKUP_TARGETS = frozenset(
    {
        "settings.json",
        "models.json",
        "pi-manager.json",
        "pi-manager-test-history.json",
        "pi-manager-health.json",
        "auth.json",
    }
)


def list_config_backups() -> list[dict[str, str]]:
    """List recoverable ``.bak.*`` config backups inside the agent directory."""
    from datetime import datetime

    rows: list[dict[str, str]] = []
    root = pi_agent_dir()
    if not root.exists():
        return rows
    for path in sorted(root.glob("*.bak.*")):
        if not path.is_file():
            continue
        name = path.name
        target_name = ""
        for target in _BACKUP_TARGETS:
            if name.startswith(target + ".bak."):
                target_name = target
                break
        if not target_name:
            continue
        try:
            st = path.stat()
            mtime_s = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")
        except OSError:
            st = None
            mtime_s = ""
        rows.append(
            {
                "path": str(path),
                "name": name,
                "target": target_name,
                "mtime": mtime_s,
                "size": str(st.st_size) if st is not None else "",
            }
        )
    return rows


def restore_config_backup(backup_path: str | Path) -> dict[str, Any]:
    """Restore a ``.bak.*`` backup back to its target config file (atomic).

    The backup must live in the agent directory and map to a known JSON config
    target, so no path traversal or arbitrary overwrite is possible.
    """
    src = Path(backup_path).resolve()
    root = pi_agent_dir().resolve()
    if src.parent != root:
        return {"ok": False, "error": "备份文件必须在配置目录内"}
    name = src.name
    target_name = ""
    for target in _BACKUP_TARGETS:
        if name.startswith(target + ".bak."):
            target_name = target
            break
    if not target_name:
        return {"ok": False, "error": "不是可恢复的配置备份"}
    try:
        data = load_json(src, None)
    except Exception as exc:
        return {"ok": False, "error": f"备份内容无法解析：{exc}"}
    target_path = root / target_name
    try:
        ensure_agent_dir()
        # allow_corrupt_overwrite 是这条恢复路径存在的**唯一**理由：
        # storage 的「拒绝覆盖无法读取的配置文件」守卫本意是防误覆盖（对的，
        # 别删），但它同时把唯一的修复入口也堵死了 —— 目标文件损坏时恢复必然
        # 失败，而应用内没有「删除损坏文件」的入口，用户只能离开应用手工删文件。
        # 绕过时 storage 会把损坏内容隔离成 <name>.corrupt.<ts> 并跳过备份轮转
        # （否则连续两次恢复会把仅存的可用备份挤掉）。
        storage.save_json(
            target_path,
            data,
            private=target_path == manager_config_path(),
            allow_corrupt_overwrite=True,
        )
    except Exception as exc:
        return {"ok": False, "error": f"恢复失败：{exc}"}
    finally:
        _invalidate_config_cache(target_path)
    return {"ok": True, "target": target_name, "backup": name}


def list_provider_api_keys(provider: str, *, reveal: bool = False) -> list[dict[str, Any]]:
    """列出 provider 密钥池；``reveal=True`` 时附带明文（仅限 GUI 显示请求）。"""
    from . import secrets as secretstore

    return secretstore.list_provider_keys(provider, reveal=reveal)


def add_provider_api_key(provider: str, value: str) -> dict[str, Any]:
    from . import secrets as secretstore

    result = secretstore.add_provider_api_key(provider, value)
    reference = secretstore.provider_api_key_reference(provider)

    def _apply(cfg: dict[str, Any]) -> Any:
        entry = (cfg.get("providers") or {}).get(provider)
        if not isinstance(entry, dict) or entry.get("apiKey") == reference:
            return storage.UNCHANGED
        entry["apiKey"] = reference
        return cfg

    update_models_config(_apply)
    return result


def remove_provider_api_key(provider: str, key_id: str) -> bool:
    from . import secrets as secretstore

    return secretstore.remove_provider_api_key(provider, key_id)


def restore_provider_api_key(provider: str, key_id: str) -> bool:
    from . import secrets as secretstore

    return secretstore.restore_provider_key(provider, key_id)


def restore_all_provider_api_keys(provider: str) -> int:
    from . import secrets as secretstore

    return secretstore.restore_all_provider_keys(provider)


