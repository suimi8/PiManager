"""Whitelisted configuration mutations for desktop and Cursor clients."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import stat
import time
from pathlib import Path
from typing import Any

from . import core, platform_util, secrets, storage

_ALLOWED_MANAGER_FIELDS = frozenset(
    {
        "failover_fail_counts",
        "favorites",
        "failover_enabled",
        "failover_fail_threshold",
        "failover_silent",
    }
)

# broker token 最长有效期：180 天。校验通过后按此期限轮换。
_BROKER_TOKEN_MAX_AGE_SECONDS = 180 * 24 * 3600
# token 恒为 32 字节的 hex（64 字符）；留出换行/BOM 余量后设上限，防止读取超大文件。
_BROKER_TOKEN_MAX_BYTES = 256
# 请求文件上限（原有 64KiB 语义保持不变，提为常量供校验函数复用）。
_REQUEST_FILE_MAX_BYTES = 64 * 1024


def broker_token_path() -> Path:
    return secrets.broker_token_path()


def _create_broker_token() -> str:
    token = os.urandom(32).hex()
    path = broker_token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先收紧目录：Windows 上新建文件继承父目录 ACE，先加固目录能让 token 从
    # 诞生那一刻就是 owner-only，避免「创建 -> 加固」之间的窗口。
    if os.name == "nt":
        platform_util.restrict_windows_acl(path.parent)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(token.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        # On Windows, chmod 0o600 is insufficient; restrict ACL to current user.
        if os.name == "nt":
            _restrict_windows_acl(path)
        return token
    except Exception:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _restrict_windows_acl(path: Path) -> bool:
    """把敏感文件的 DACL 收紧为「仅当前用户」（Windows）。

    实现已抽到 platform_util.restrict_windows_acl —— 同一套加固还要覆盖 vault /
    salt / index / helper registry，留在本模块会变成复制粘贴。这里保留函数名
    是为了不破坏既有调用点与测试。

    与旧实现的关键区别：**失败不再静默**。旧版整个函数体被
    `except Exception: pass` 包住，而函数第一段就引用了不存在的
    `ctypes.wintypes.PVOID`，于是在任何 Windows 机器上都是彻底的 no-op，
    却对外表现得像加固成功了。
    """
    ok = platform_util.restrict_windows_acl(path)
    if not ok:
        logging.getLogger(__name__).warning(
            "broker token 的 Windows ACL 加固未生效，%s 可能被同机其他账户读取或改写", path
        )
    return ok


def _rotate_broker_token_if_expired() -> None:
    """校验通过后按有效期轮换 broker token（默认 180 天，基于文件 mtime）。

    轮换发生在 token 校验成功之后，不影响当前请求；写入使用 os.replace
    保证原子性。扩展每次调用前重新读取 token 文件，因此下一次调用自然
    拿到新 token。轮换失败（目录只读等）时非致命：沿用旧 token，仅记日志。
    """
    path = broker_token_path()
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return
    if age < _BROKER_TOKEN_MAX_AGE_SECONDS:
        return
    new_token = os.urandom(32).hex()
    temp = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(new_token.encode("ascii"))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        if os.name == "nt":
            _restrict_windows_acl(temp)
        os.replace(temp, path)
        if os.name == "nt":
            _restrict_windows_acl(path)
    except OSError:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        logging.getLogger(__name__).warning(
            "broker token 已过期但轮换失败，沿用旧 token：%s", path
        )
        return
    logging.getLogger(__name__).info("broker token 已按有效期轮换")


def _verify_broker_token(provided: str) -> bool:
    path = broker_token_path()
    if not path.exists():
        try:
            token = _create_broker_token()
            # Even on first creation, require the caller to present the token
            # we just created. An empty provided value is never accepted.
            return bool(provided) and hmac.compare_digest((provided or "").strip(), token)
        except FileExistsError:
            pass
    # P2-9：与 secrets._ensure_regular_file 对齐 —— 读取前必须确认它是普通文件而非
    # 符号链接/junction（否则同机攻击者可把读取重定向到自己可控的文件，从而让自己
    # 提供的 token 校验通过），并限制大小（token 恒为 64 个 hex 字符）。
    try:
        info = path.stat(follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(info.st_mode) or platform_util.is_reparse_point(path):
        logging.getLogger(__name__).warning(
            "broker token 不是普通文件（疑似符号链接劫持），拒绝校验：%s", path
        )
        return False
    if info.st_size > _BROKER_TOKEN_MAX_BYTES:
        logging.getLogger(__name__).warning(
            "broker token 文件异常大（%s 字节），拒绝校验：%s", info.st_size, path
        )
        return False
    try:
        stored = path.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError):
        return False
    return bool(provided) and hmac.compare_digest((provided or "").strip(), stored)


def _revision_path() -> Path:
    return core.pi_agent_dir() / ".config-revisions.json"


def _broker_lock_path() -> Path:
    """broker 变更的串行化锁路径。

    **为什么它与被改的配置文件是两条不同的路径**：``storage.locked`` 的跨进程锁
    按「路径」互斥，而本模块的每个操作都是
    ``locked(_broker_lock_path())`` → ``update_json(<配置文件>)``
    → ``_record_revision(.config-revisions.json)`` 三层调用。三者是三条互不相同
    的路径（``.config-broker.mutation`` / ``pi-manager.json`` 或 ``settings.json``
    / ``.config-revisions.json``），因此拿到的是三把不同的边车锁，**不存在对同一
    路径的嵌套加锁**。

    这件事必须显式论证：``msvcrt.locking`` 对同一句柄区间的重复加锁在 Windows 上
    会阻塞约 9 秒后抛 ``OSError: Resource deadlock avoided``；``extras.py`` 的
    ``_fail_counts_lock`` 之所以退而求其次用 ``threading.Lock``，正是因为它的
    ``_fail_counts``/``_save_fail_counts`` 内部会再次 ``locked(manager_config_path())``。
    本模块没有这个形状：外层锁的是 broker 专用的哨兵路径，从不是配置文件本身。

    ``storage.locked`` 现已实现进程内重入计数，但本模块**不依赖**它 —— 上面的
    「三条不同路径」论证在不可重入语义下同样成立。
    """
    return core.pi_agent_dir() / ".config-broker.mutation"


def _sha256(path: Path) -> str:
    if not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_recorded_revision(path: Path) -> bool:
    """校验 path 的内容是否仍与上次 broker 变更记录的 sha256 一致。

    P2-10：原本 .config-revisions.json 只是**写入** sha256 而从不校验，看着像
    完整性控制、实际只是个计数器，给人虚假的安全感。这里把它变成真正的检测：
    发现不一致说明文件在两次 broker 变更之间被 broker 之外的写入者改过
    （桌面端 UI 自身的写入也算，因此只告警、不阻断）。
    无记录时返回 True（首次变更没有基线可比）。
    """
    state = storage.load_json(_revision_path(), {})
    entry = state.get(path.name) if isinstance(state, dict) else None
    if not isinstance(entry, dict):
        return True
    recorded = str(entry.get("sha256") or "")
    if not recorded:
        return True
    return hmac.compare_digest(recorded, _sha256(path))


def _recorded_revision(path: Path) -> int:
    """读回 *path* 上次被 broker 变更时记录的 revision（无记录时 0）。

    「无需改动」的请求（例如清零一个从未记录过的失败计数）不该白白推高
    revision —— 那会让 ``.config-revisions.json`` 从「变更序号」退化成
    「调用次数」，也会让 sha256 基线在没有写入的情况下被重新盖章。
    """
    state = storage.load_json(_revision_path(), {})
    entry = state.get(path.name) if isinstance(state, dict) else None
    if not isinstance(entry, dict):
        return 0
    try:
        return int(entry.get("revision") or 0)
    except (TypeError, ValueError):
        return 0


def _record_revision(path: Path) -> int:
    # 先比对基线再记录新值：不一致只记 WARNING，不阻断（UI 与 broker 都是合法写入者）。
    if not verify_recorded_revision(path):
        logging.getLogger(__name__).warning(
            "%s 自上次 broker 变更后被其他写入者修改（sha256 与记录不一致）", path.name
        )

    def update(current: Any) -> dict[str, Any]:
        state = current if isinstance(current, dict) else {}
        entry = state.get(path.name) if isinstance(state.get(path.name), dict) else {}
        revision = int(entry.get("revision") or 0) + 1
        state[path.name] = {"revision": revision, "sha256": _sha256(path)}
        return state

    state = storage.update_json(_revision_path(), {}, update)
    return int(state[path.name]["revision"])


def _normalized_fail_counts(current: Any) -> dict[str, int]:
    """把 ``failover_fail_counts`` 归一成 ``{str: int}``。

    与扩展侧 ``failover.js:failureCounts`` 逐条同构：非对象 → 空表，无法转成
    整数的值 → 0。两端必须用同一套归一规则，否则同一份 pi-manager.json 会
    在桌面端与扩展端得出不同的「是否已达阈值」。
    """
    raw = current.get("failover_fail_counts") if isinstance(current, dict) else None
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for key, value in raw.items():
        try:
            counts[str(key)] = int(value)
        except (TypeError, ValueError):
            counts[str(key)] = 0
    return counts


def mutate(request: dict[str, Any]) -> dict[str, Any]:
    request_id = str(request.get("request_id") or "")
    if not _verify_broker_token(str(request.get("token") or "")):
        return {
            "ok": False,
            "request_id": request_id,
            "error": "broker token 校验失败，请求已被拒绝",
        }
    # 校验通过后按有效期轮换 token；轮换失败不影响当前请求。
    _rotate_broker_token_if_expired()
    if int(request.get("schema_version") or 0) != 1:
        return {"ok": False, "request_id": request_id, "error": "unsupported_schema"}
    operation = str(request.get("operation") or "")
    arguments = request.get("arguments")
    if not isinstance(arguments, dict):
        return {"ok": False, "request_id": request_id, "error": "invalid_arguments"}

    try:
        if operation == "set_default_model":
            provider = str(arguments.get("provider") or "").strip()
            model = str(arguments.get("model") or "").strip()
            if not provider or not model:
                raise ValueError("provider and model are required")
            thinking = str(arguments.get("thinking") or "").strip()
            # 与 core_process.validate_launch_tokens 同规则校验：defaultThinkingLevel
            # 会作为 --thinking 进入 pi 命令行（见 rpc_session._ensure），恶意/损坏
            # 扩展可经此注入 argv 参数，必须与启动白名单保持一致。
            try:
                from . import core_process

                tokens = ["--provider", provider, "--model", model]
                if thinking:
                    tokens += ["--thinking", thinking]
                core_process.validate_launch_tokens(tokens)
            except ValueError as exc:
                raise ValueError(f"illegal launch tokens: {exc}") from exc
            sync_enabled = bool(arguments.get("sync_enabled", True))
            favorites = [str(item) for item in arguments.get("favorites", []) if isinstance(item, str)]

            def update_settings(current: Any) -> dict[str, Any]:
                if not isinstance(current, dict):
                    raise ValueError("settings.json 顶层必须是对象")
                result = dict(current)
                result["defaultProvider"] = provider
                result["defaultModel"] = model
                if thinking:
                    result["defaultThinkingLevel"] = thinking
                if sync_enabled:
                    enabled = [str(item) for item in result.get("enabledModels", []) if isinstance(item, str)]
                    result["enabledModels"] = list(dict.fromkeys([*enabled, *favorites, f"{provider}/{model}"]))
                return result

            with storage.locked(_broker_lock_path()):
                storage.update_json(core.settings_path(), {}, update_settings)
                core._invalidate_config_cache(core.settings_path())
                revision = _record_revision(core.settings_path())
            return {
                "ok": True,
                "request_id": request_id,
                "revision": revision,
                "result": {"provider": provider, "model": model},
            }

        if operation == "set_manager_fields":
            fields = arguments.get("fields")
            if not isinstance(fields, dict) or any(key not in _ALLOWED_MANAGER_FIELDS for key in fields):
                raise ValueError("manager mutation contains non-whitelisted fields")

            def update_manager(current: Any) -> dict[str, Any]:
                if not isinstance(current, dict):
                    raise ValueError("pi-manager.json 顶层必须是对象")
                return {**current, **fields}

            with storage.locked(_broker_lock_path()):
                # private=True 必须与 core.update_manager_config 一致：pi-manager.json
                # 可能带含凭据的代理 URL，而 storage 只在 private=True 时收紧
                # POSIX 权限位并调用 _harden_private（Windows DACL）。broker 这条
                # 写路径此前是 private=False，等于每次变更都把桌面端刚加固过的
                # DACL 换成从父目录继承的宽松 ACE。
                storage.update_json(
                    core.manager_config_path(), {}, update_manager, private=True
                )
                core._invalidate_config_cache(core.manager_config_path())
                revision = _record_revision(core.manager_config_path())
            return {"ok": True, "request_id": request_id, "revision": revision, "result": {}}

        if operation == "increment_failure_count":
            # G3：模型失败计数的「读 → 改 → 写」下推到 broker，从而落进
            # storage.locked 的**跨进程**锁里。扩展侧原先在 JS 里算好增量再让
            # broker 整表覆盖（set_manager_fields），桌面端与扩展端同时运行时
            # 会丢失更新；它靠「写后回读、发现自己的增量被吞就重试」缓解，
            # 而这只是收窄窗口，不是原子性（R2 扩展审计 C-1 / D2）。
            provider = str(arguments.get("provider") or "").strip()
            model = str(arguments.get("model") or "").strip()
            if not provider or not model:
                raise ValueError("provider and model are required")
            succeeded = bool(arguments.get("succeeded", False))
            key = f"{provider}/{model}"
            state: dict[str, Any] = {"count": 0, "changed": False}

            def update_counts(current: Any) -> Any:
                if not isinstance(current, dict):
                    raise ValueError("pi-manager.json 顶层必须是对象")
                counts = _normalized_fail_counts(current)
                if succeeded:
                    # 与 failover.js 一致：从未记录过的键无需清零，直接不写盘。
                    if key not in counts:
                        state["count"] = 0
                        return storage.UNCHANGED
                    counts[key] = 0
                else:
                    counts[key] = int(counts.get(key) or 0) + 1
                state["count"] = counts[key]
                state["changed"] = True
                return {**current, "failover_fail_counts": counts}

            with storage.locked(_broker_lock_path()):
                storage.update_json(
                    core.manager_config_path(), {}, update_counts, private=True
                )
                core._invalidate_config_cache(core.manager_config_path())
                revision = (
                    _record_revision(core.manager_config_path())
                    if state["changed"]
                    else _recorded_revision(core.manager_config_path())
                )
            return {
                "ok": True,
                "request_id": request_id,
                "revision": revision,
                "result": {
                    "key": key,
                    "count": int(state["count"]),
                    "changed": bool(state["changed"]),
                },
            }

        return {"ok": False, "request_id": request_id, "error": "operation_not_allowed"}
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "config broker mutation failed: %s", exc, exc_info=True
        )
        return {"ok": False, "request_id": request_id, "error": "操作失败"}


def mutate_file(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    # P2-11：请求文件里带着 broker token，读它之前必须确认它是**普通文件**且不是
    # 重解析点 —— 否则同机攻击者可以用 junction 把「我给你的请求」指向别处，
    # 或诱使 broker 去读它本不该读的路径。属主校验在 POSIX 上顺带做掉。
    try:
        info = source.stat(follow_symlinks=False)
    except OSError:
        return {"ok": False, "error": "invalid_request_file"}
    if not stat.S_ISREG(info.st_mode) or platform_util.is_reparse_point(source):
        return {"ok": False, "error": "request_file_must_be_regular_file"}
    if info.st_size > _REQUEST_FILE_MAX_BYTES:
        return {"ok": False, "error": "invalid_request_file"}
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        return {"ok": False, "error": "request_file_must_be_owned_by_current_user"}
    try:
        # utf-8-sig：请求文件可能由外部工具生成（PowerShell Out-File 默认带 BOM）
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": f"invalid_request: {exc}"}
    if not isinstance(payload, dict):
        return {"ok": False, "error": "request_must_be_object"}
    return mutate(payload)
