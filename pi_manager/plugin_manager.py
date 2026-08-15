"""安全管理 Pi 用户插件的本地安装后端。

本模块只处理 Pi 的本地 package 目录和 ZIP 包，不导入或执行插件代码。
插件源码安装在 ``~/.pi/agent/pimanager/plugins``，再通过 Pi 官方支持的
``settings.json`` ``packages`` 投影加载资源。内置插件仍由
``builtin_plugins`` 独立管理，本模块不会覆盖 ``skills`` / ``extensions``
目录，也不会修改内置清单。

安全边界
--------
* 只接受本地目录和 ZIP；ZIP 成员逐个校验，不使用 ``extractall``。
* 拒绝路径穿越、绝对路径、符号链接/Windows 重解析点和特殊文件。
* 安装前检查 package.json、SemVer、资源路径、Skill frontmatter 以及
  文件数量/大小；安装后重新校验暂存目录。
* registry/settings 的 JSON 写入统一使用 :mod:`pi_manager.storage` 的锁和
  原子替换。失败时只删除本次暂存/新版本，保留旧版本和旧配置。
* ``trust`` 只是记录用户是否确认过第三方源码，绝不构成 TypeScript/JS
  沙箱。未信任的 extension 即便请求启用，也会保持 extension 资源关闭。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

import yaml

from . import core, platform_util, storage


# 这些上限既保护 ZIP 解压，也保护本地目录导入。对普通文档型插件足够宽松，
# 但能阻止误选大型仓库或压缩炸弹拖垮桌面进程。
MAX_FILE_COUNT = 10_000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_DESCRIPTION_BYTES = 16 * 1024
MAX_SKILL_FRONTMATTER_BYTES = 128 * 1024
MAX_COMPRESSION_RATIO = 1_000
# ZIP 目录成员数量与路径深度上限：防止恶意包用海量目录/超深路径触发 mkdir 风暴。
MAX_ZIP_DIR_MEMBERS = 20_000
MAX_ZIP_PATH_DEPTH = 64

_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*)(?:\.(?:0|[1-9]\d*))*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
# 常见密钥形态的脱敏正则：注册表、UI 预览与日志中不允许出现任何明文密钥。
# 除原有 sk-/ghp_/AIza/bearer 外，覆盖 AWS AKIA、Slack xox*、PEM 私钥块、
# password/token/secret/api_key 等键值对形态。
_SECRET_RE = re.compile(
    r"(?i)(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{12,}|"
    r"AIza[A-Za-z0-9_-]{20,}|bearer\s+[A-Za-z0-9._-]{12,}|"
    r"AKIA[0-9A-Z]{16}|xox[baprs]-[0-9A-Za-z-]{10,}|"
    r"-----BEGIN[^-]*PRIVATE KEY-----[\s\S]*?-----END[^-]*PRIVATE KEY-----|"
    r"-----BEGIN[^-]*PRIVATE KEY-----|"
    r"(?:password|passwd|pwd|token|secret|apikey|api_key|access[-_]?key|client[-_]?secret|auth[-_]?token)"
    r"\s*[:=]\s*[^\s,;'\"`]+)"
)
# Windows 保留设备名（含带扩展名变体，不区分大小写）。
_WIN32_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)
# 权限声明中的 network 主机名：只允许 hostname（可含通配子域），
# 拒绝路径字符、空白与任何凭据形态。
_NETWORK_HOST_RE = re.compile(
    r"^(?:\*\.)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)
_RESOURCE_TYPES = ("skills", "extensions", "prompts", "themes")
_REGISTRY_SCHEMA_VERSION = 1


class PluginManagerError(RuntimeError):
    """插件导入或生命周期操作失败。"""


class PluginValidationError(PluginManagerError):
    """插件包不符合 PiManager V1 规范。"""

    def __init__(self, message: str, errors: list[str] | None = None) -> None:
        super().__init__(message)
        self.errors = list(errors or [message])


@dataclass(frozen=True)
class _PackageInfo:
    root: Path
    manifest: dict[str, Any]
    plugin_id: str
    name: str
    version: str
    description: str
    resources: dict[str, list[str]]
    sha256: str
    file_count: int
    total_bytes: int
    source_type: str
    files: list[str]

    @property
    def has_extensions(self) -> bool:
        return bool(self.resources.get("extensions"))


def _agent_dir() -> Path:
    """Return the single supported Pi global configuration directory."""

    return core.pi_agent_dir()


def _plugins_root() -> Path:
    return _agent_dir() / "pimanager" / "plugins"


def _staging_root() -> Path:
    return _plugins_root() / ".staging"


def _trash_root() -> Path:
    return _plugins_root() / ".trash"


def _operation_lock_path() -> Path:
    return _agent_dir() / "pimanager" / ".plugin-manager.operations.lock"


def plugin_registry_path() -> Path:
    """Return the user plugin registry path without creating it."""

    return _agent_dir() / "pi-plugins.json"


def _settings_path() -> Path:
    return core.settings_path()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _empty_registry() -> dict[str, Any]:
    return {"schemaVersion": _REGISTRY_SCHEMA_VERSION, "plugins": {}}


def _load_registry() -> dict[str, Any]:
    try:
        data = storage.load_json(plugin_registry_path(), _empty_registry())
    except Exception as exc:
        raise PluginManagerError(f"插件注册表无法读取：{exc}") from exc
    if not isinstance(data, dict):
        raise PluginManagerError("插件注册表顶层必须是对象")
    schema = data.get("schemaVersion", _REGISTRY_SCHEMA_VERSION)
    # 严格校验：必须是整数且不能是布尔，拒绝 int() 截断/字符串/True 等变形。
    if not isinstance(schema, int) or isinstance(schema, bool):
        raise PluginManagerError("插件注册表 schemaVersion 必须是整数")
    if schema > _REGISTRY_SCHEMA_VERSION:
        raise PluginManagerError(f"不支持的插件注册表版本：{schema}")
    plugins = data.get("plugins", {})
    if not isinstance(plugins, dict):
        raise PluginManagerError("插件注册表 plugins 必须是对象")
    # 消费前对每条版本记录的 install_root 做规范化校验：必须是合法相对路径
    # 且与规范安装路径严格一致，防止注册表被篡改后把 settings 投影到任意目录。
    for plugin_id, entry in plugins.items():
        if not isinstance(plugin_id, str) or not isinstance(entry, dict):
            continue  # 具体条目的格式问题由 self_check 报告，这里只拦截路径投影
        for version, record in _registry_versions(entry).items():
            if not isinstance(record, dict):
                continue
            install_root = record.get("install_root")
            if install_root is None:
                continue  # 旧格式记录缺 install_root，读取端按规范值回退
            _validate_install_root_record(plugin_id, version, install_root)
    result = copy.deepcopy(data)
    result["schemaVersion"] = _REGISTRY_SCHEMA_VERSION
    result["plugins"] = plugins
    return result


def _write_registry(data: dict[str, Any]) -> None:
    """Atomically write only manager-owned registry data."""

    storage.save_json(plugin_registry_path(), data)


def _load_settings() -> dict[str, Any]:
    try:
        data = storage.load_json(_settings_path(), {})
    except Exception as exc:
        raise PluginManagerError(f"settings.json 无法读取：{exc}") from exc
    if not isinstance(data, dict):
        raise PluginManagerError("settings.json 顶层必须是对象")
    return copy.deepcopy(data)


def _write_settings(data: dict[str, Any]) -> None:
    storage.save_json(_settings_path(), data)
    # core.load_settings 有短期缓存；插件管理器绕过它直接读取，写完后主动失效。
    invalidate = getattr(core, "_invalidate_config_cache", None)
    if callable(invalidate):
        invalidate(_settings_path())


@contextmanager
def _operation_lock() -> Iterator[None]:
    """Serialize multi-file plugin transactions within PiManager."""

    with storage.locked(_operation_lock_path()):
        yield


def _is_reparse_or_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise PluginManagerError(f"无法读取插件路径：{path}: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        return True
    attrs = platform_util.windows_file_attributes(path)
    return attrs is not None and bool(attrs & platform_util.FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_safe_existing_path(path: Path, stop: Path) -> None:
    """Reject a reparse point in an existing path between ``stop`` and path."""

    try:
        stop_resolved = stop.resolve()
        current = path
        while True:
            if current.exists() or current.is_symlink():
                if _is_reparse_or_symlink(current):
                    raise PluginValidationError(f"路径包含符号链接或重解析点：{current}")
            if current == stop or current.resolve() == stop_resolved:
                break
            parent = current.parent
            if parent == current:
                break
            current = parent
    except PluginValidationError:
        raise
    except OSError as exc:
        raise PluginValidationError(f"无法安全解析路径：{path}: {exc}") from exc


def _is_reserved_win32_name(part: str) -> bool:
    """拒绝 Windows 保留设备名（CON/NUL/COM1-9/LPT1-9/AUX/PRN 及带扩展名变体）。"""

    return part.split(".")[0].upper() in _WIN32_RESERVED_NAMES


def _safe_relative_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PluginValidationError(f"{field} 必须是非空相对路径")
    raw = value.strip().replace("\\", "/")
    if "\x00" in raw or raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        raise PluginValidationError(f"{field} 必须是包根目录内的相对路径：{value!r}")
    while raw.startswith("./"):
        raw = raw[2:]
    parts = PurePosixPath(raw).parts
    if not parts or any(part in {"", ".", ".."} for part in parts):
        raise PluginValidationError(f"{field} 含非法路径段：{value!r}")
    if any(":" in part for part in parts):
        raise PluginValidationError(f"{field} 不允许 Windows ADS 或盘符路径：{value!r}")
    if any(char in raw for char in "*?[]"):
        raise PluginValidationError(f"{field} 不允许使用 glob：{value!r}")
    if any(_is_reserved_win32_name(part) for part in parts):
        raise PluginValidationError(f"{field} 不允许使用 Windows 保留设备名：{value!r}")
    return "/".join(parts)


def _under(root: Path, candidate: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _read_limited(path: Path, limit: int) -> bytes:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as exc:
        raise PluginValidationError(f"无法读取插件文件 {path}: {exc}") from exc
    if len(data) > limit:
        raise PluginValidationError(f"文件过大（>{limit} 字节）：{path}")
    return data


def _semver_key(version: str) -> tuple[Any, ...]:
    match = _SEMVER_RE.fullmatch(version)
    if not match:
        return (0, 0, 0, (1, ""))
    major, minor, patch, prerelease, _build = match.groups()
    if prerelease is None:
        pre: tuple[Any, ...] = (1,)
    else:
        parts: list[Any] = [0]
        for item in prerelease.split("."):
            parts.append((0, int(item)) if item.isdigit() else (1, item))
        pre = tuple(parts)
    return (int(major), int(minor), int(patch), pre)


def _validate_semver(value: Any) -> str:
    if not isinstance(value, str) or not _SEMVER_RE.fullmatch(value):
        raise PluginValidationError(f"version 不是合法 SemVer：{value!r}")
    return value


def _validate_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise PluginValidationError(
            "piManager.id 必须是小写字母开头、仅含小写字母/数字/._- 的安全 ID"
        )
    return value


def _validate_string_list(value: Any, *, field: str) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise PluginValidationError(f"{field} 必须是非空字符串或字符串数组")
    return [item.strip() for item in value]


def _validate_manager_metadata(manager: dict[str, Any]) -> None:
    display_name = manager.get("displayName")
    if display_name is not None:
        if not isinstance(display_name, str) or not display_name.strip():
            raise PluginValidationError("piManager.displayName 必须是非空字符串")
        if len(display_name.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
            raise PluginValidationError("piManager.displayName 过长")

    permissions = manager.get("permissions")
    if permissions is not None:
        if not isinstance(permissions, dict):
            raise PluginValidationError("piManager.permissions 必须是对象")
        allowed = {"network", "filesystem", "process", "secrets"}
        unknown = sorted(set(permissions) - allowed)
        if unknown:
            raise PluginValidationError(
                f"piManager.permissions 含未知类别：{', '.join(map(str, unknown))}"
            )
        for kind, raw in permissions.items():
            values = _validate_string_list(raw, field=f"piManager.permissions.{kind}")
            if kind == "secrets":
                for value in values:
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                        raise PluginValidationError(
                            f"piManager.permissions.secrets 必须是环境变量名：{value!r}"
                        )
            elif kind == "network":
                for value in values:
                    # 只允许 hostname（可含通配子域），拒绝路径字符、空白与凭据。
                    if not _NETWORK_HOST_RE.fullmatch(value):
                        raise PluginValidationError(
                            f"piManager.permissions.network 只能填写主机名，"
                            f"不得包含路径、空白或凭据：{value!r}"
                        )

    compatibility = manager.get("compatibility")
    if compatibility is not None:
        if not isinstance(compatibility, dict):
            raise PluginValidationError("piManager.compatibility 必须是对象")
        platforms = compatibility.get("platforms")
        if platforms is not None:
            values = _validate_string_list(
                platforms, field="piManager.compatibility.platforms"
            )
            allowed_platforms = {"win32", "darwin", "linux"}
            unknown_platforms = sorted(set(values) - allowed_platforms)
            if unknown_platforms:
                raise PluginValidationError(
                    "piManager.compatibility.platforms 含未知平台："
                    + ", ".join(unknown_platforms)
                )


def _resource_values(manifest: dict[str, Any]) -> dict[str, list[str]]:
    pi = manifest.get("pi")
    manager = manifest.get("piManager")
    if not isinstance(pi, dict):
        raise PluginValidationError("pi 必须是对象")
    if not isinstance(manager, dict):
        raise PluginValidationError("piManager 必须是对象")
    unknown_pi = sorted(set(pi) - set(_RESOURCE_TYPES))
    if unknown_pi:
        raise PluginValidationError(f"pi 含未知资源类别：{', '.join(map(str, unknown_pi))}")
    manager_resources = manager.get("resources")
    if isinstance(manager_resources, dict):
        unknown_manager = sorted(set(manager_resources) - set(_RESOURCE_TYPES))
        if unknown_manager:
            raise PluginValidationError(
                "piManager.resources 含未知资源类别："
                + ", ".join(map(str, unknown_manager))
            )

    resources: dict[str, list[str]] = {}
    declared: set[str] = set()
    if manager_resources is not None and not isinstance(manager_resources, dict):
        raise PluginValidationError("piManager.resources 必须是对象")
    for kind in _RESOURCE_TYPES:
        raw_values: Any = None
        if kind in pi:
            declared.add(kind)
            raw_values = pi[kind]
        if isinstance(manager_resources, dict) and kind in manager_resources:
            declared.add(kind)
            raw_values = manager_resources[kind]
        elif kind in manager:
            declared.add(kind)
            raw_values = manager[kind]

        if raw_values is None:
            continue
        if isinstance(raw_values, str):
            raw_values = [raw_values]
        if not isinstance(raw_values, list) or any(not isinstance(item, str) for item in raw_values):
            raise PluginValidationError(f"{kind} 资源路径必须是字符串或字符串数组")
        resources[kind] = [
            _safe_relative_path(item, field=f"{kind} 资源路径") for item in raw_values
        ]

    # Pi 也支持约定目录。只有 manifest 没有显式声明某类型时才自动使用，
    # 显式 [] 仍然表示用户不允许该类型。
    for kind in _RESOURCE_TYPES:
        if kind in declared:
            resources.setdefault(kind, [])
            continue
        conventional = manifest.get("__root_for_validation__")
        if isinstance(conventional, Path) and (conventional / kind).exists():
            resources[kind] = [kind]
    if not any(resources.get(kind) for kind in _RESOURCE_TYPES):
        raise PluginValidationError(
            "pi 至少要声明一个实际资源入口：skills、extensions、prompts 或 themes"
        )
    return resources


def _validate_frontmatter(path: Path) -> None:
    raw = _read_limited(path, MAX_SKILL_FRONTMATTER_BYTES)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PluginValidationError(f"SKILL.md 不是 UTF-8：{path}") from exc
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise PluginValidationError(f"SKILL.md 缺少 YAML frontmatter 起始标记：{path}")
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        raise PluginValidationError(f"SKILL.md 缺少 YAML frontmatter 结束标记：{path}")
    frontmatter_text = "\n".join(lines[1:end])
    # 语法校验：与 pi 加载 SKILL.md 时使用的 YAML 解析器保持一致口径，
    # 避免「Pi Manager 自检通过、pi 实际加载报错」（如未加引号的描述里
    # 出现 ``@chore: 前缀`` 这类冒号+空格被解析为嵌套映射）。
    try:
        data = yaml.safe_load(frontmatter_text)
    except yaml.YAMLError as exc:
        raise PluginValidationError(
            f"SKILL.md frontmatter 不是合法 YAML（pi 将无法加载）：{path}：{exc}"
        ) from exc
    if not isinstance(data, dict):
        raise PluginValidationError(
            f"SKILL.md frontmatter 必须是 YAML 映射：{path}"
        )
    # 重复键检测：PyYAML 会静默覆盖，而 pi 的 yaml 解析器默认拒绝，
    # 这里显式拦截以保持与 pi 行为一致。
    try:
        node = yaml.compose(frontmatter_text)
    except yaml.YAMLError:
        node = None
    if isinstance(node, yaml.MappingNode):
        seen: set[str] = set()
        for key_node, _ in node.value:
            if isinstance(key_node, yaml.ScalarNode):
                key = key_node.value
                if key in seen:
                    raise PluginValidationError(
                        f"SKILL.md frontmatter 含重复字段 {key!r}：{path}"
                    )
                seen.add(key)
    # 必填字段检查以 YAML 解析结果的顶层键为准，禁止逐行正则扫描：
    # 嵌套字段（如 ``nested: {name: x}``）不得冒充顶层 name/description，
    # 与 pi 实际解析 SKILL.md 时的语义保持一致。
    for required_field in ("name", "description"):
        value = data.get(required_field)
        if not isinstance(value, str) or not value.strip():
            raise PluginValidationError(
                f"SKILL.md frontmatter 顶层必须包含非空字符串 {required_field}：{path}"
            )


def _walk_regular_files(root: Path) -> list[tuple[str, Path, int]]:
    if _is_reparse_or_symlink(root):
        raise PluginValidationError(f"插件根目录不能是符号链接或重解析点：{root}")
    if not root.is_dir():
        raise PluginValidationError(f"插件源不是目录：{root}")
    files: list[tuple[str, Path, int]] = []
    seen_casefold: set[str] = set()
    total = 0
    try:
        walker = os.walk(root, topdown=True, followlinks=False)
        for current, dirnames, filenames in walker:
            current_path = Path(current)
            for dirname in list(dirnames):
                child = current_path / dirname
                if _is_reparse_or_symlink(child):
                    raise PluginValidationError(f"插件包含符号链接或重解析目录：{child}")
                if not child.is_dir():
                    raise PluginValidationError(f"插件目录项不是目录：{child}")
            for filename in filenames:
                child = current_path / filename
                if _is_reparse_or_symlink(child):
                    raise PluginValidationError(f"插件包含符号链接或重解析文件：{child}")
                try:
                    info = child.stat()
                except OSError as exc:
                    raise PluginValidationError(f"无法读取插件文件：{child}: {exc}") from exc
                if not stat.S_ISREG(info.st_mode):
                    raise PluginValidationError(f"插件包含特殊文件：{child}")
                if info.st_size > MAX_FILE_BYTES:
                    raise PluginValidationError(
                        f"单文件超过上限 {MAX_FILE_BYTES} 字节：{child}"
                    )
                total += int(info.st_size)
                if getattr(info, "st_nlink", 1) > 1:
                    raise PluginValidationError(f"插件禁止硬链接文件：{child}")
                if total > MAX_TOTAL_BYTES:
                    raise PluginValidationError(
                        f"插件总大小超过上限 {MAX_TOTAL_BYTES} 字节"
                    )
                rel = child.relative_to(root).as_posix()
                if not _safe_relative_path(rel, field="插件文件路径"):
                    raise PluginValidationError(f"插件文件路径非法：{rel}")
                folded = rel.casefold()
                if folded in seen_casefold:
                    raise PluginValidationError(f"插件存在大小写冲突的重复路径：{rel}")
                seen_casefold.add(folded)
                if any(part.casefold() == "node_modules" for part in PurePosixPath(rel).parts):
                    raise PluginValidationError("插件包不得包含 node_modules，请声明依赖而不是打包依赖")
                files.append((rel, child, int(info.st_size)))
    except PluginValidationError:
        raise
    except OSError as exc:
        raise PluginValidationError(f"遍历插件目录失败：{root}: {exc}") from exc
    if len(files) > MAX_FILE_COUNT:
        raise PluginValidationError(f"插件文件数量超过上限 {MAX_FILE_COUNT}")
    files.sort(key=lambda item: item[0])
    return files


def _tree_sha256(files: list[tuple[str, Path, int]]) -> str:
    digest = hashlib.sha256()
    for rel, path, size in files:
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")
        try:
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
        except OSError as exc:
            raise PluginValidationError(f"无法计算插件哈希：{path}: {exc}") from exc
        digest.update(b"\0")
    return digest.hexdigest()


def _manifest_from_root(root: Path) -> dict[str, Any]:
    manifest_path = root / "package.json"
    if _is_reparse_or_symlink(manifest_path) or not manifest_path.is_file():
        raise PluginValidationError("插件根目录必须包含普通文件 package.json")
    raw = _read_limited(manifest_path, MAX_MANIFEST_BYTES)
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginValidationError(f"package.json 解析失败：{exc}") from exc
    if not isinstance(manifest, dict):
        raise PluginValidationError("package.json 顶层必须是对象")
    required = ("name", "version", "description", "pi", "piManager")
    missing = [key for key in required if key not in manifest]
    if missing:
        raise PluginValidationError(f"package.json 缺少必填字段：{', '.join(missing)}")
    if not isinstance(manifest.get("name"), str) or not manifest["name"].strip():
        raise PluginValidationError("name 必须是非空字符串")
    if not isinstance(manifest.get("description"), str) or not manifest["description"].strip():
        raise PluginValidationError("description 必须是非空字符串")
    if len(manifest["description"].encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        raise PluginValidationError(f"description 超过 {MAX_DESCRIPTION_BYTES} 字节")
    _validate_semver(manifest.get("version"))
    manager = manifest.get("piManager")
    if not isinstance(manager, dict):
        raise PluginValidationError("piManager 必须是对象")
    if "schemaVersion" not in manager:
        raise PluginValidationError("piManager.schemaVersion 是必填字段")
    manager_version = manager.get("schemaVersion")
    if (
        not isinstance(manager_version, int)
        or isinstance(manager_version, bool)
        or manager_version != _REGISTRY_SCHEMA_VERSION
    ):
        raise PluginValidationError(f"不支持的 piManager.schemaVersion：{manager_version!r}")
    _validate_id(manager.get("id"))
    _validate_manager_metadata(manager)
    scripts = manifest.get("scripts")
    if scripts is not None and not isinstance(scripts, dict):
        raise PluginValidationError("package.json 的 scripts 必须是对象")
    if isinstance(scripts, dict):
        blocked = ("preinstall", "install", "postinstall", "prepare")
        found = [name for name in blocked if name in scripts]
        if found:
            raise PluginValidationError(
                f"禁止 package.json 生命周期脚本：{', '.join(found)}"
            )
    return manifest


def _validate_resource_paths(root: Path, resources: dict[str, list[str]]) -> None:
    for kind, paths in resources.items():
        for rel in paths:
            target = root / Path(*PurePosixPath(rel).parts)
            if not _under(root, target):
                raise PluginValidationError(f"{kind} 资源路径越界：{rel}")
            if _is_reparse_or_symlink(target):
                raise PluginValidationError(f"{kind} 资源路径不能是符号链接：{rel}")
            if not target.exists():
                raise PluginValidationError(f"{kind} 资源路径不存在：{rel}")
            if not target.is_dir() and not target.is_file():
                raise PluginValidationError(f"{kind} 资源路径不是文件或目录：{rel}")

            if kind == "extensions":
                if target.is_file():
                    if target.suffix.lower() not in {".ts", ".js"}:
                        raise PluginValidationError(
                            f"extension 文件必须使用 .ts 或 .js：{rel}"
                        )
                else:
                    extension_files = []
                    for candidate in target.rglob("*"):
                        if _is_reparse_or_symlink(candidate):
                            raise PluginValidationError(
                                f"extension 目录包含符号链接或重解析点：{candidate}"
                            )
                        if candidate.is_file():
                            extension_files.append(candidate)
                    if not any(
                        candidate.suffix.lower() in {".ts", ".js"}
                        for candidate in extension_files
                    ):
                        raise PluginValidationError(
                            f"extension 目录 {rel} 至少要包含一个 .ts 或 .js 入口"
                        )

    skill_paths = resources.get("skills", [])
    for rel in skill_paths:
        target = root / Path(*PurePosixPath(rel).parts)
        skill_files: list[Path] = []
        if target.is_file():
            if target.name == "SKILL.md":
                skill_files = [target]
        else:
            for candidate in target.rglob("SKILL.md"):
                if _is_reparse_or_symlink(candidate):
                    raise PluginValidationError(f"skill 包含符号链接：{candidate}")
                skill_files.append(candidate)
        if not skill_files:
            raise PluginValidationError(f"skill 资源 {rel} 缺少 SKILL.md")
        for skill_file in skill_files:
            _validate_frontmatter(skill_file)


def _scan_package_root(root: Path, *, source_type: str) -> _PackageInfo:
    files = _walk_regular_files(root)
    manifest = _manifest_from_root(root)
    # _resource_values uses this private in-memory marker only; it is removed
    # before anything is stored or returned.
    manifest_for_resources = copy.deepcopy(manifest)
    manifest_for_resources["__root_for_validation__"] = root
    resources = _resource_values(manifest_for_resources)
    _validate_resource_paths(root, resources)
    clean_manifest = copy.deepcopy(manifest)
    return _PackageInfo(
        root=root,
        manifest=clean_manifest,
        plugin_id=str(manifest["piManager"]["id"]),
        name=str(manifest["name"]),
        version=str(manifest["version"]),
        description=str(manifest["description"]),
        resources=resources,
        sha256=_tree_sha256(files),
        file_count=len(files),
        total_bytes=sum(item[2] for item in files),
        source_type=source_type,
        files=[rel for rel, _path, _size in files],
    )


def _zip_member_is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = (info.external_attr >> 16) & 0xFFFF
    return stat.S_IFMT(mode) == stat.S_IFLNK


def _zip_member_path(name: str) -> str:
    if not name or "\x00" in name:
        raise PluginValidationError("ZIP 成员名称为空或包含 NUL")
    if "\\" in name or ":" in name or name.startswith("/") or re.match(r"^[A-Za-z]:", name):
        raise PluginValidationError(f"ZIP 成员路径非法：{name!r}")
    parts = PurePosixPath(name).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise PluginValidationError(f"ZIP 成员路径含越界段：{name!r}")
    return "/".join(parts)


def _validate_zip_infos(archive: Path, infos: list[zipfile.ZipInfo]) -> tuple[list[zipfile.ZipInfo], int]:
    if _is_reparse_or_symlink(archive):
        raise PluginValidationError(f"ZIP 文件不能是符号链接：{archive}")
    try:
        archive_size = archive.stat().st_size
    except OSError as exc:
        raise PluginValidationError(f"无法读取 ZIP 文件：{archive}: {exc}") from exc
    if archive_size > MAX_ARCHIVE_BYTES:
        raise PluginValidationError(f"ZIP 文件超过上限 {MAX_ARCHIVE_BYTES} 字节")
    seen: set[str] = set()
    seen_casefold: set[str] = set()
    files: list[zipfile.ZipInfo] = []
    total = 0
    dir_count = 0
    for info in infos:
        rel = _zip_member_path(info.filename)
        parts = PurePosixPath(rel).parts
        # 路径深度上限：与目录成员数一起防止海量深层 mkdir 风暴。
        if len(parts) > MAX_ZIP_PATH_DEPTH:
            raise PluginValidationError(f"ZIP 成员路径过深（>{MAX_ZIP_PATH_DEPTH} 段）：{rel}")
        folded = rel.casefold()
        if rel in seen or folded in seen_casefold:
            raise PluginValidationError(f"ZIP 存在重复成员：{rel}")
        seen.add(rel)
        seen_casefold.add(folded)
        if any(part.casefold() == "node_modules" for part in parts):
            raise PluginValidationError("ZIP 插件包不得包含 node_modules")
        if _zip_member_is_symlink(info):
            raise PluginValidationError(f"ZIP 禁止符号链接成员：{rel}")
        if info.is_dir():
            dir_count += 1
            if dir_count > MAX_ZIP_DIR_MEMBERS:
                raise PluginValidationError(
                    f"ZIP 目录成员超过上限 {MAX_ZIP_DIR_MEMBERS}"
                )
            continue
        if info.file_size > MAX_FILE_BYTES:
            raise PluginValidationError(f"ZIP 单文件超过上限：{rel}")
        if info.file_size and (
            info.compress_size == 0
            or info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO
        ):
            raise PluginValidationError(f"ZIP 成员压缩比过高，疑似压缩炸弹：{rel}")
        total += int(info.file_size)
        if total > MAX_TOTAL_BYTES:
            raise PluginValidationError(f"ZIP 解压总大小超过上限 {MAX_TOTAL_BYTES} 字节")
        files.append(info)
    if len(files) > MAX_FILE_COUNT:
        raise PluginValidationError(f"ZIP 文件数量超过上限 {MAX_FILE_COUNT}")
    if "package.json" not in seen or not any(
        not info.is_dir() and _zip_member_path(info.filename) == "package.json" for info in files
    ):
        raise PluginValidationError("ZIP 根目录必须包含 package.json")
    return files, total


def _extract_zip_safely(archive: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=False)
    try:
        with zipfile.ZipFile(archive, "r") as handle:
            files, _total = _validate_zip_infos(archive, handle.infolist())
            for info in files:
                rel = _zip_member_path(info.filename)
                target = destination / Path(*PurePosixPath(rel).parts)
                if not _under(destination, target):
                    raise PluginValidationError(f"ZIP 成员路径越界：{rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists() or target.is_symlink():
                    raise PluginValidationError(f"ZIP 成员目标重复：{rel}")
                with handle.open(info, "r") as source, target.open("xb") as output:
                    remaining = MAX_FILE_BYTES + 1
                    while remaining > 0:
                        chunk = source.read(min(1024 * 1024, remaining))
                        if not chunk:
                            break
                        output.write(chunk)
                        remaining -= len(chunk)
                    if remaining <= 0:
                        raise PluginValidationError(f"ZIP 成员解压后超过单文件上限：{rel}")
    except PluginValidationError:
        raise
    except (OSError, zipfile.BadZipFile, RuntimeError, ValueError) as exc:
        raise PluginValidationError(f"ZIP 读取或解压失败：{exc}") from exc


def _copy_file_safely(source: Path, destination: Path) -> None:
    flags = os.O_RDONLY
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if nofollow:
        flags |= nofollow
    try:
        fd = os.open(source, flags)
    except OSError as exc:
        raise PluginValidationError(f"无法安全读取插件文件：{source}: {exc}") from exc
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with os.fdopen(fd, "rb", closefd=True) as input_handle, destination.open("xb") as output:
            shutil.copyfileobj(input_handle, output, length=1024 * 1024)
    except OSError as exc:
        raise PluginValidationError(f"复制插件文件失败：{source}: {exc}") from exc


def _copy_directory_safely(source: Path, destination: Path) -> None:
    if _is_reparse_or_symlink(source) or not source.is_dir():
        raise PluginValidationError(f"插件源目录非法：{source}")
    # 在复制前先检查数量、大小、硬链接和重解析点，避免把明显超限的目录
    # 先完整复制到暂存区；复制后还会再次扫描并校验内容哈希，防止竞态修改。
    _walk_regular_files(source)
    destination.mkdir(parents=True, exist_ok=False)
    for current, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        rel_current = current_path.relative_to(source)
        dest_current = destination / rel_current
        for dirname in list(dirnames):
            src_dir = current_path / dirname
            if _is_reparse_or_symlink(src_dir):
                raise PluginValidationError(f"插件包含符号链接或重解析目录：{src_dir}")
            (dest_current / dirname).mkdir(parents=True, exist_ok=True)
        for filename in filenames:
            src_file = current_path / filename
            if _is_reparse_or_symlink(src_file):
                raise PluginValidationError(f"插件包含符号链接或重解析文件：{src_file}")
            if not src_file.is_file():
                raise PluginValidationError(f"插件包含非普通文件：{src_file}")
            _copy_file_safely(src_file, dest_current / filename)


def _prepare_package(source: str, destination: Path) -> _PackageInfo:
    path = Path(source).expanduser()
    if not path.exists() and not path.is_symlink():
        raise PluginValidationError(f"插件源不存在：{source}")
    path = path.absolute()
    if path.is_dir():
        _copy_directory_safely(path, destination)
        return _scan_package_root(destination, source_type="directory")
    if path.is_file():
        _extract_zip_safely(path, destination)
        return _scan_package_root(destination, source_type="zip")
    raise PluginValidationError(f"插件源必须是目录或 ZIP 文件：{source}")


def _inspect_directory(source: Path) -> _PackageInfo:
    return _scan_package_root(source, source_type="directory")


def _inspect_zip(source: Path) -> _PackageInfo:
    with tempfile.TemporaryDirectory(prefix="pimanager-plugin-inspect-") as temp:
        root = Path(temp) / "package"
        _extract_zip_safely(source, root)
        return _scan_package_root(root, source_type="zip")


def _inspect_source(source: str) -> _PackageInfo:
    path = Path(source).expanduser()
    if not path.exists() and not path.is_symlink():
        raise PluginValidationError(f"插件源不存在：{source}")
    if path.is_dir():
        return _inspect_directory(path.absolute())
    if path.is_file():
        return _inspect_zip(path.absolute())
    raise PluginValidationError(f"插件源必须是目录或 ZIP 文件：{source}")


def _safe_registry_description(value: str) -> str:
    return _SECRET_RE.sub("[REDACTED]", value[:MAX_DESCRIPTION_BYTES])


def _redact_for_display(value: Any, *, field: str = "") -> Any:
    """Redact likely secrets before metadata reaches UI or the registry."""

    sensitive_fields = {
        "apikey",
        "accesstoken",
        "authtoken",
        "clientsecret",
        "password",
        "cookie",
        "authorization",
        "privatekey",
        "secret",
        "credential",
    }
    if field.replace("_", "").replace("-", "").lower() in sensitive_fields:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(key): _redact_for_display(item, field=str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_for_display(item, field=field) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_for_display(item, field=field) for item in value)
    if isinstance(value, str):
        return _SECRET_RE.sub("[REDACTED]", value)
    return copy.deepcopy(value)


def _relative_install_root(plugin_id: str, version: str) -> str:
    return (Path("pimanager") / "plugins" / plugin_id / version).as_posix()


def _validate_install_root_record(plugin_id: str, version: str, install_root: Any) -> None:
    """校验注册表版本记录的 install_root：必须与规范安装路径严格一致。

    任何绝对路径、``..`` 段、盘符、glob、保留设备名等非法形态都会被拒绝，
    防止被篡改的注册表把 settings 投影或列表路径指向包目录之外。
    """

    try:
        safe = _safe_relative_path(install_root, field="install_root")
    except PluginValidationError as exc:
        raise PluginManagerError(
            f"插件注册表 {plugin_id}@{version} 的 install_root 非法：{exc}"
        ) from exc
    if safe != _relative_install_root(plugin_id, version):
        raise PluginManagerError(
            f"插件注册表 {plugin_id}@{version} 的 install_root 非规范路径，已拒绝读取"
        )


def _absolute_install_root(plugin_id: str, version: str) -> Path:
    root = _plugins_root().resolve()
    candidate = root / plugin_id / version
    if not _under(root, candidate):
        raise PluginManagerError("插件安装路径越界")
    return candidate


def _record_from_info(
    info: _PackageInfo,
    *,
    source: str,
    trust: bool,
    enabled: bool,
    installed_at: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    manager = info.manifest.get("piManager")
    manager = manager if isinstance(manager, dict) else {}
    return {
        "id": info.plugin_id,
        "name": info.name,
        "version": info.version,
        "description": _safe_registry_description(info.description),
        "display_name": _safe_registry_description(
            str(manager.get("displayName") or info.name)
        ),
        "permissions": _redact_for_display(manager.get("permissions", {})),
        "compatibility": _redact_for_display(manager.get("compatibility", {})),
        "source": _SECRET_RE.sub("[REDACTED]", str(source)),
        "source_type": info.source_type,
        "sha256": info.sha256,
        "status": status or ("enabled" if enabled else "disabled"),
        "trust": bool(trust),
        "enabled": bool(enabled),
        "install_root": _relative_install_root(info.plugin_id, info.version),
        "resources": copy.deepcopy(info.resources),
        "file_count": info.file_count,
        "total_bytes": info.total_bytes,
        "installed_at": installed_at or _now(),
    }


def _registry_versions(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    versions = entry.get("versions")
    if not isinstance(versions, dict):
        versions = {}
    result: dict[str, dict[str, Any]] = {}
    for version, value in versions.items():
        if isinstance(version, str) and isinstance(value, dict):
            result[version] = copy.deepcopy(value)
    active_version = entry.get("active_version") or entry.get("version")
    if isinstance(active_version, str) and isinstance(entry.get("version"), str):
        result.setdefault(active_version, _version_record_from_entry(entry))
    return result


def _version_record_from_entry(entry: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id", "name", "version", "display_name", "description", "permissions",
        "compatibility", "source", "source_type", "sha256",
        "trust", "enabled", "install_root", "resources", "file_count", "total_bytes",
        "installed_at", "status", "warning",
    )
    return {key: copy.deepcopy(entry[key]) for key in keys if key in entry}


def _active_entry(entry: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(entry)
    result.pop("versions", None)
    result.pop("active_version", None)
    return result


def _set_active_entry(
    registry_entry: dict[str, Any],
    version_record: dict[str, Any],
    *,
    active_version: str,
) -> dict[str, Any]:
    result = copy.deepcopy(version_record)
    result["active_version"] = active_version
    versions = _registry_versions(registry_entry)
    versions.update(_registry_versions(result))
    versions[active_version] = _version_record_from_entry(result)
    result["versions"] = versions
    return result


def _known_install_sources(registry: dict[str, Any]) -> set[str]:
    known: set[str] = set()
    plugins = registry.get("plugins", {})
    if not isinstance(plugins, dict):
        return known
    for entry in plugins.values():
        if not isinstance(entry, dict):
            continue
        records = _registry_versions(entry)
        for record in records.values():
            if isinstance(record, dict) and isinstance(record.get("install_root"), str):
                known.add(_normalise_source_token(record["install_root"]))
    return known


def _normalise_source_token(value: str) -> str:
    raw = str(value).replace("\\", "/")
    while raw.startswith("./"):
        raw = raw[2:]
    return raw.rstrip("/").lower()


def _settings_source_for(record: dict[str, Any]) -> str:
    install_root = record.get("install_root")
    plugin_id = str(record.get("id") or "")
    version = str(record.get("version") or "")
    if isinstance(install_root, str) and install_root:
        # 消费前必须校验：install_root 必须与规范安装路径严格一致，
        # 杜绝注册表篡改导致的任意路径投影。
        _validate_install_root_record(plugin_id, version, install_root)
        return install_root.replace("\\", "/")
    return _relative_install_root(plugin_id, version)


def _package_entry(record: dict[str, Any]) -> dict[str, Any]:
    source = _settings_source_for(record)
    enabled = bool(record.get("enabled"))
    trusted = bool(record.get("trust"))
    entry: dict[str, Any] = {"source": source}
    if not enabled or not trusted:
        for kind in _RESOURCE_TYPES:
            entry[kind] = []
        return entry
    return entry


def _source_matches_managed(value: Any, managed_sources: set[str]) -> bool:
    if isinstance(value, str):
        return _normalise_source_token(value) in managed_sources
    if isinstance(value, dict):
        source = value.get("source")
        return isinstance(source, str) and _normalise_source_token(source) in managed_sources
    return False


def _settings_for_registry(
    settings: dict[str, Any],
    registry: dict[str, Any],
    *,
    previous_registry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = copy.deepcopy(settings)
    packages = result.get("packages", [])
    if not isinstance(packages, list):
        raise PluginManagerError("settings.json 的 packages 必须是数组，已拒绝覆盖")
    managed_sources = _known_install_sources(registry)
    if previous_registry is not None:
        managed_sources.update(_known_install_sources(previous_registry))
    kept = [item for item in packages if not _source_matches_managed(item, managed_sources)]
    plugins = registry.get("plugins", {})
    if isinstance(plugins, dict):
        for plugin_id in sorted(plugins):
            entry = plugins[plugin_id]
            if not isinstance(entry, dict):
                continue
            active_version = entry.get("active_version") or entry.get("version")
            if not isinstance(active_version, str):
                continue
            record = _registry_versions(entry).get(active_version)
            if not isinstance(record, dict):
                record = _active_entry(entry)
            kept.append(_package_entry(record))
    result["packages"] = kept
    return result


def _rollback_install_failure(
    *,
    final: Path,
    plugins_root: Path,
    old_settings: dict[str, Any],
    original: Exception,
) -> bool:
    """回滚失败的安装事务：先删新目录，再恢复 settings。

    返回 True 表示新目录仍存在（清理失败），False 表示已删除。
    清理/恢复失败只并入错误消息并保留原始异常语义，绝不掩盖原异常。
    """

    notes: list[str] = []
    still_present = True
    try:
        _remove_owned_tree(final, plugins_root)
        still_present = False
    except Exception as exc:
        notes.append(f"清理新版本目录失败：{exc}")
    try:
        _write_settings(old_settings)
    except Exception as exc:
        notes.append(f"恢复 settings.json 失败：{exc}")
    if notes:
        raise PluginManagerError(f"{original}；" + "；".join(notes)) from original
    return still_present


def _restore_settings_safely(old_settings: dict[str, Any], original: Exception) -> None:
    """恢复 settings 快照；恢复失败时并入错误消息，保留原始异常语义。"""

    try:
        _write_settings(old_settings)
    except Exception as exc:
        raise PluginManagerError(
            f"{original}；恢复 settings.json 失败：{exc}"
        ) from original


def _error_result(exc: Exception, *, errors: list[str] | None = None, plugin_id: str = "") -> dict[str, Any]:
    result: dict[str, Any] = {"ok": False, "error": str(exc)}
    if errors:
        result["errors"] = list(errors)
    if plugin_id:
        result["id"] = plugin_id
        result["plugin_id"] = plugin_id
    return result


def inspect_plugin(source: str) -> dict[str, Any]:
    """Inspect a local directory/ZIP without importing or executing code."""

    try:
        info = _inspect_source(str(source))
    except PluginValidationError as exc:
        return _error_result(exc, errors=exc.errors)
    except Exception as exc:  # UI-facing inspection should never execute/propagate plugin code.
        return _error_result(exc)
    return {
        "ok": True,
        "id": info.plugin_id,
        "plugin_id": info.plugin_id,
        "name": info.name,
        "version": info.version,
        "description": _safe_registry_description(info.description),
        "display_name": _safe_registry_description(
            str((info.manifest.get("piManager") or {}).get("displayName") or info.name)
        ),
        "source": _SECRET_RE.sub("[REDACTED]", str(source)),
        "source_type": info.source_type,
        "sha256": info.sha256,
        "file_count": info.file_count,
        "total_bytes": info.total_bytes,
        "resources": copy.deepcopy(info.resources),
        "has_extensions": info.has_extensions,
        # 前 50 个脱敏后的相对路径，供 UI 预览包内内容。
        "files": [_redact_for_display(item) for item in info.files[:50]],
        "permissions": _redact_for_display(
            (info.manifest.get("piManager") or {}).get("permissions", {})
        ),
        "compatibility": _redact_for_display(
            (info.manifest.get("piManager") or {}).get("compatibility", {})
        ),
        "manifest": _redact_for_display(info.manifest),
    }


def _install_info(
    info: _PackageInfo,
    *,
    source: str,
    enable: bool,
    trust: bool,
) -> dict[str, Any]:
    plugin_id = info.plugin_id
    final = _absolute_install_root(plugin_id, info.version)
    plugins_root = _plugins_root().resolve()
    _assert_safe_existing_path(final.parent, plugins_root)
    if final.exists() or final.is_symlink():
        raise PluginManagerError(f"安装目标已存在：{final}")
    if not _under(plugins_root, final):
        raise PluginManagerError("插件安装目标越界")

    with _operation_lock():
        registry = _load_registry()
        plugins = registry.setdefault("plugins", {})
        if not isinstance(plugins, dict):
            raise PluginManagerError("插件注册表 plugins 必须是对象")
        existing = plugins.get(plugin_id)
        if isinstance(existing, dict):
            versions = _registry_versions(existing)
            if info.version in versions:
                raise PluginManagerError(f"插件 ID/版本已存在：{plugin_id}@{info.version}")

        old_registry = copy.deepcopy(registry)
        old_settings = _load_settings()
        effective_enabled = bool(enable and trust)
        new_record = _record_from_info(
            info,
            source=source,
            trust=trust,
            enabled=effective_enabled,
        )
        new_record["status"] = "enabled" if effective_enabled else "disabled"
        if enable and not trust:
            new_record["status"] = "pending-trust"
            new_record["warning"] = "插件尚未获得用户信任，已安装但保持禁用；trust 不提供沙箱"

        if existing is not None and not isinstance(existing, dict):
            raise PluginManagerError(f"插件注册表中的 {plugin_id} 条目非法")
        if isinstance(existing, dict):
            new_entry = _set_active_entry(existing, new_record, active_version=info.version)
            # 保留旧版本记录，但标记其不再是 active；物理目录也保留供回滚。
            for old_version, old_record in _registry_versions(existing).items():
                if old_version != info.version and isinstance(old_record, dict):
                    old_copy = copy.deepcopy(old_record)
                    old_copy["enabled"] = False
                    old_copy["status"] = "superseded"
                    new_entry["versions"][old_version] = old_copy
        else:
            new_entry = _set_active_entry({}, new_record, active_version=info.version)
        candidate_registry = copy.deepcopy(old_registry)
        candidate_registry["plugins"][plugin_id] = new_entry

        # 事务开始前校验 .staging 根目录安全（位于 agent 目录之下），
        # 防止本地重解析点把暂存写往包目录之外。
        staging_root = _staging_root()
        _assert_safe_existing_path(staging_root, _agent_dir().resolve())
        staging_root.mkdir(parents=True, exist_ok=True)
        stage_parent = staging_root / uuid.uuid4().hex
        moved = False
        try:
            # ``info`` was obtained from the source only for duplicate checking;
            # stage is copied/extracted and validated again before installation.
            staged_root = stage_parent / "package"
            staged_info = _prepare_package(source, staged_root)
            if staged_info.plugin_id != plugin_id or staged_info.version != info.version:
                raise PluginManagerError("源文件在检查后发生变化，已拒绝安装")
            if staged_info.sha256 != info.sha256:
                raise PluginManagerError("源文件在检查后发生变化，哈希不一致")
            final.parent.mkdir(parents=True, exist_ok=True)
            _assert_safe_existing_path(final.parent, plugins_root)
            if final.exists() or final.is_symlink():
                raise PluginManagerError(f"安装目标已存在：{final}")
            os.replace(staged_root, final)
            moved = True

            # 先更新 settings 投影，再写 registry；写 registry 失败时按
            # 「先删新目录、再恢复 settings」的顺序回滚，避免 settings 引用
            # registry 中已不存在的目录，清理异常不掩盖原始异常。
            _write_settings(_settings_for_registry(old_settings, candidate_registry))
            try:
                _write_registry(candidate_registry)
            except Exception as registry_exc:
                moved = _rollback_install_failure(
                    final=final,
                    plugins_root=plugins_root,
                    old_settings=old_settings,
                    original=registry_exc,
                )
                raise
        except Exception as exc:
            if moved:
                try:
                    _remove_owned_tree(final, plugins_root)
                except Exception as cleanup_exc:
                    if isinstance(exc, PluginManagerError):
                        raise PluginManagerError(
                            f"{exc}；清理新版本目录失败：{cleanup_exc}"
                        ) from exc
                    raise
            raise
        finally:
            shutil.rmtree(stage_parent, ignore_errors=True)

        result = copy.deepcopy(new_record)
        result.update({"ok": True, "id": plugin_id, "plugin_id": plugin_id})
        result["active_version"] = info.version
        result["enabled"] = effective_enabled
        result["install_root"] = str(final)
        result["installRoot"] = str(final)
        return result


def import_plugin(source: str, *, enable: bool = False, trust: bool = False) -> dict[str, Any]:
    """Validate and install a local directory or ZIP package transactionally."""

    try:
        info = _inspect_source(str(source))
        return _install_info(info, source=str(source), enable=bool(enable), trust=bool(trust))
    except PluginValidationError as exc:
        return _error_result(exc, errors=exc.errors)
    except Exception as exc:
        return _error_result(exc)


def list_plugins() -> list[dict[str, Any]]:
    """List active managed plugins without loading their TypeScript/JavaScript."""

    try:
        registry = _load_registry()
    except PluginManagerError:
        return []
    plugins = registry.get("plugins", {})
    if not isinstance(plugins, dict):
        return []
    result: list[dict[str, Any]] = []
    for plugin_id in sorted(plugins):
        entry = plugins[plugin_id]
        if not isinstance(entry, dict):
            continue
        active_version = entry.get("active_version") or entry.get("version")
        record = _active_entry(entry)
        if isinstance(active_version, str):
            record = copy.deepcopy(_registry_versions(entry).get(active_version, record))
            record["active_version"] = active_version
        versions = _registry_versions(entry)
        record["available_versions"] = sorted(versions, key=_semver_key)
        # 绝对路径统一经 _absolute_install_root 构造（含 _under 校验），
        # 不信任注册表字符串直接拼接，杜绝 .. 越界。
        absolute: Path | None = None
        if isinstance(active_version, str):
            try:
                absolute = _absolute_install_root(plugin_id, active_version)
            except PluginManagerError:
                absolute = None
        if absolute is not None:
            record["installed"] = absolute.is_dir() and not _is_reparse_or_symlink(absolute)
            record["install_root"] = str(absolute)
            record["installRoot"] = str(absolute)
            if not record["installed"] and record.get("status") not in {"broken", "missing"}:
                record["status"] = "missing"
        result.append(record)
    return result


def _find_plugin(registry: dict[str, Any], plugin_id: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    plugin_id = _validate_id(plugin_id)
    plugins = registry.get("plugins", {})
    entry = plugins.get(plugin_id) if isinstance(plugins, dict) else None
    if not isinstance(entry, dict):
        raise PluginManagerError(f"未知插件：{plugin_id}")
    active_version = entry.get("active_version") or entry.get("version")
    if not isinstance(active_version, str):
        raise PluginManagerError(f"插件 {plugin_id} 缺少 active_version")
    record = _registry_versions(entry).get(active_version)
    if not isinstance(record, dict):
        record = _active_entry(entry)
    return entry, record, active_version


def _remove_owned_tree(target: Path, root: Path) -> None:
    if not _under(root, target) or target.resolve() == root.resolve():
        raise PluginManagerError(f"拒绝删除越界插件目录：{target}")
    if target.exists() or target.is_symlink():
        _assert_safe_existing_path(target, root)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            raise PluginManagerError(f"插件安装根不是目录：{target}")


def _validate_installed_record(
    plugin_id: str,
    version: str,
    record: dict[str, Any],
) -> Path:
    """Verify an installed version before changing its runtime projection."""

    target = _absolute_install_root(plugin_id, version)
    if not target.is_dir() or _is_reparse_or_symlink(target):
        raise PluginManagerError(f"插件 {plugin_id}@{version} 安装目录缺失或不安全：{target}")
    info = _scan_package_root(target, source_type="installed")
    if info.plugin_id != plugin_id or info.version != version:
        raise PluginManagerError(f"插件 {plugin_id}@{version} package.json 身份不匹配")
    expected_hash = record.get("sha256")
    if isinstance(expected_hash, str) and expected_hash != info.sha256:
        raise PluginManagerError(f"插件 {plugin_id}@{version} 内容哈希不匹配")
    return target


def set_plugin_enabled(plugin_id: str, enabled: bool) -> dict[str, Any]:
    """Enable/disable the active version by updating Pi package filters."""

    try:
        with _operation_lock():
            registry = _load_registry()
            entry, record, active_version = _find_plugin(registry, plugin_id)
            _validate_installed_record(plugin_id, active_version, record)
            if enabled and not record.get("trust"):
                raise PluginManagerError("启用前必须先确认并记录插件信任")
            old_registry = copy.deepcopy(registry)
            old_settings = _load_settings()
            updated_record = copy.deepcopy(record)
            updated_record["enabled"] = bool(enabled)
            updated_record["status"] = "enabled" if enabled else "disabled"
            # 只在真正启用时清除 warning；pending-trust → disabled 保留提示。
            if enabled:
                updated_record.pop("warning", None)
            new_entry = _set_active_entry(entry, updated_record, active_version=active_version)
            # Keep historical versions unchanged except for the active copy.
            candidate = copy.deepcopy(registry)
            candidate["plugins"][plugin_id] = new_entry
            try:
                _write_settings(
                    _settings_for_registry(
                        old_settings, candidate, previous_registry=old_registry
                    )
                )
                _write_registry(candidate)
            except Exception as exc:
                _restore_settings_safely(old_settings, exc)
                raise
            result = copy.deepcopy(updated_record)
            result.update({"ok": True, "id": plugin_id, "plugin_id": plugin_id})
            return result
    except Exception as exc:
        return _error_result(exc, plugin_id=str(plugin_id))


def set_plugin_trust(
    plugin_id: str,
    trusted: bool = True,
    *,
    enable: bool | None = None,
) -> dict[str, Any]:
    """Record an explicit trust decision and optionally enable the plugin.

    Trust is only a user-consent flag. It does not sandbox JavaScript/TypeScript
    and never grants additional credentials by itself.
    """

    try:
        with _operation_lock():
            registry = _load_registry()
            entry, record, active_version = _find_plugin(registry, plugin_id)
            _validate_installed_record(plugin_id, active_version, record)
            old_registry = copy.deepcopy(registry)
            old_settings = _load_settings()
            updated_record = copy.deepcopy(record)
            updated_record["trust"] = bool(trusted)
            enabled_now = bool(enable) if enable is not None else bool(record.get("enabled"))
            if not trusted:
                enabled_now = False
            updated_record["enabled"] = enabled_now
            updated_record["status"] = "enabled" if enabled_now else "disabled"
            # 与 set_plugin_enabled 口径一致：仅在真正启用时清除 warning。
            if enabled_now:
                updated_record.pop("warning", None)
            new_entry = _set_active_entry(entry, updated_record, active_version=active_version)
            candidate = copy.deepcopy(registry)
            candidate["plugins"][plugin_id] = new_entry
            try:
                _write_settings(
                    _settings_for_registry(
                        old_settings, candidate, previous_registry=old_registry
                    )
                )
                _write_registry(candidate)
            except Exception as exc:
                _restore_settings_safely(old_settings, exc)
                raise
            result = copy.deepcopy(updated_record)
            result.update({"ok": True, "id": plugin_id, "plugin_id": plugin_id})
            return result
    except Exception as exc:
        return _error_result(exc, plugin_id=str(plugin_id))


def remove_plugin(plugin_id: str) -> dict[str, Any]:
    """Remove all installed versions of a managed plugin and its projection."""

    try:
        with _operation_lock():
            registry = _load_registry()
            entry, _record, _active_version = _find_plugin(registry, plugin_id)
            old_registry = copy.deepcopy(registry)
            old_settings = _load_settings()
            plugin_dir = _plugins_root() / plugin_id
            if plugin_dir.exists():
                _assert_safe_existing_path(plugin_dir, _plugins_root().resolve())
            # 事务开始前校验 .trash 根目录安全（位于 agent 目录之下），
            # 防止本地重解析点把待删除目录移往包目录之外。
            trash_root = _trash_root()
            _assert_safe_existing_path(trash_root, _agent_dir().resolve())
            trash_root.mkdir(parents=True, exist_ok=True)
            trash_parent = trash_root / uuid.uuid4().hex
            moved = False
            if plugin_dir.exists():
                os.replace(plugin_dir, trash_parent)
                moved = True
            candidate = copy.deepcopy(registry)
            del candidate["plugins"][plugin_id]
            try:
                _write_settings(
                    _settings_for_registry(
                        old_settings, candidate, previous_registry=old_registry
                    )
                )
                _write_registry(candidate)
            except Exception as exc:
                # 恢复顺序：先还原目录，再恢复 settings；任一失败并入错误消息。
                restore_notes: list[str] = []
                if moved and not plugin_dir.exists():
                    try:
                        plugin_dir.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(trash_parent, plugin_dir)
                        moved = False
                    except Exception as move_exc:
                        restore_notes.append(f"还原插件目录失败：{move_exc}")
                try:
                    _write_settings(old_settings)
                except Exception as restore_exc:
                    restore_notes.append(f"恢复 settings.json 失败：{restore_exc}")
                if restore_notes:
                    raise PluginManagerError(
                        f"{exc}；" + "；".join(restore_notes)
                    ) from exc
                raise
            cleanup_error = ""
            if moved:
                try:
                    shutil.rmtree(trash_parent, ignore_errors=False)
                except OSError as exc:
                    # The managed package is already absent from both registry
                    # and settings. Keep a recoverable trash copy and report it
                    # instead of pretending the transaction was rolled back.
                    cleanup_error = str(exc)
            result = {
                "ok": True,
                "id": plugin_id,
                "plugin_id": plugin_id,
                "removed": True,
                "versions": sorted(_registry_versions(entry), key=_semver_key),
            }
            if cleanup_error:
                result["cleanup_pending"] = True
                result["warning"] = f"插件已卸载，但临时清理失败：{cleanup_error}"
            return result
    except Exception as exc:
        return _error_result(exc, plugin_id=str(plugin_id))


def rollback_plugin(plugin_id: str, version: str | None = None) -> dict[str, Any]:
    """Switch an installed plugin back to a retained version.

    This extra helper is intentionally small; the required public lifecycle API
    remains list/inspect/import/enable/remove. It never executes plugin code.
    """

    try:
        with _operation_lock():
            registry = _load_registry()
            entry, active, active_version = _find_plugin(registry, plugin_id)
            versions = _registry_versions(entry)
            candidates = [item for item in versions if item != active_version]
            target_version = version or (sorted(candidates, key=_semver_key, reverse=True)[0] if candidates else "")
            if not target_version or target_version not in versions:
                raise PluginManagerError(f"插件 {plugin_id} 没有可回滚的版本")
            target = versions[target_version]
            if not isinstance(target, dict):
                raise PluginManagerError(f"插件版本记录非法：{plugin_id}@{target_version}")
            target_path = _absolute_install_root(plugin_id, target_version)
            if not target_path.is_dir() or _is_reparse_or_symlink(target_path):
                raise PluginManagerError(f"回滚目标目录不存在或不安全：{target_path}")
            _validate_installed_record(plugin_id, target_version, target)
            old_registry = copy.deepcopy(registry)
            old_settings = _load_settings()
            target_copy = copy.deepcopy(target)
            target_copy["enabled"] = bool(active.get("enabled"))
            target_copy["trust"] = bool(active.get("trust"))
            if not target_copy["trust"]:
                target_copy["enabled"] = False
            target_copy["status"] = "enabled" if target_copy["enabled"] else "disabled"
            if target_copy["enabled"]:
                target_copy.pop("warning", None)
            new_entry = _set_active_entry(entry, target_copy, active_version=target_version)
            # 与导入路径对齐：除目标 active 版本外的所有历史版本统一标记
            # enabled=false / status=superseded，保持 versions 数据诚实。
            for version, record in _registry_versions(entry).items():
                if version != target_version and isinstance(record, dict):
                    old_copy = copy.deepcopy(record)
                    old_copy["enabled"] = False
                    old_copy["status"] = "superseded"
                    new_entry["versions"][version] = old_copy
            candidate = copy.deepcopy(registry)
            candidate["plugins"][plugin_id] = new_entry
            try:
                _write_settings(
                    _settings_for_registry(
                        old_settings, candidate, previous_registry=old_registry
                    )
                )
                _write_registry(candidate)
            except Exception as exc:
                _restore_settings_safely(old_settings, exc)
                raise
            result = copy.deepcopy(target_copy)
            result.update(
                {
                    "ok": True,
                    "id": plugin_id,
                    "plugin_id": plugin_id,
                    "active_version": target_version,
                    "rolled_back_from": active_version,
                }
            )
            return result
    except Exception as exc:
        return _error_result(exc, plugin_id=str(plugin_id))


def validate(source: str) -> dict[str, Any]:
    """Compatibility alias for the read-only inspection operation."""

    return inspect_plugin(source)


def install(
    source: str,
    *,
    enable: bool = False,
    trust: bool = False,
) -> dict[str, Any]:
    """Compatibility alias for :func:`import_plugin`."""

    return import_plugin(source, enable=enable, trust=trust)


def status(plugin_id: str) -> dict[str, Any]:
    """Return one managed plugin record in the common result shape."""

    try:
        for record in list_plugins():
            if record.get("id") == plugin_id or record.get("plugin_id") == plugin_id:
                result = copy.deepcopy(record)
                result.update({"ok": True, "plugin_id": plugin_id})
                return result
        raise PluginManagerError(f"未知插件：{plugin_id}")
    except Exception as exc:
        return _error_result(exc, plugin_id=str(plugin_id))


def enable(plugin_id: str) -> dict[str, Any]:
    return set_plugin_enabled(plugin_id, True)


def disable(plugin_id: str) -> dict[str, Any]:
    return set_plugin_enabled(plugin_id, False)


def uninstall(plugin_id: str) -> dict[str, Any]:
    return remove_plugin(plugin_id)


def rollback(plugin_id: str, version: str | None = None) -> dict[str, Any]:
    return rollback_plugin(plugin_id, version)


def self_check() -> list[str]:
    """Return manager/plugin integrity errors; an empty list means OK."""

    errors: list[str] = []
    try:
        registry = _load_registry()
    except Exception as exc:
        return [str(exc)]
    plugins = registry.get("plugins", {})
    if not isinstance(plugins, dict):
        return ["插件注册表 plugins 必须是对象"]
    for plugin_id, entry in plugins.items():
        if not isinstance(plugin_id, str) or not _ID_RE.fullmatch(plugin_id):
            errors.append(f"注册表包含非法插件 ID：{plugin_id!r}")
            continue
        if not isinstance(entry, dict):
            errors.append(f"插件 {plugin_id} 注册表条目不是对象")
            continue
        active_version = entry.get("active_version") or entry.get("version")
        if not isinstance(active_version, str) or not _SEMVER_RE.fullmatch(active_version):
            errors.append(f"插件 {plugin_id} active_version 非法")
            continue
        records = _registry_versions(entry)
        for version, record in records.items():
            if not isinstance(record, dict):
                errors.append(f"插件 {plugin_id}@{version} 版本记录不是对象")
                continue
            try:
                _validate_semver(version)
                # 回滚/导入后，非 active 版本必须统一为 enabled=false + superseded。
                if version != active_version and (
                    record.get("enabled") or record.get("status") != "superseded"
                ):
                    errors.append(
                        f"插件 {plugin_id} 版本 {version} 状态不诚实："
                        "非 active 版本必须为 enabled=false/status=superseded"
                    )
                install_root = record.get("install_root")
                if not isinstance(install_root, str):
                    raise PluginManagerError("缺少 install_root")
                expected = _absolute_install_root(plugin_id, version)
                if install_root.replace("\\", "/") != _relative_install_root(plugin_id, version):
                    errors.append(f"插件 {plugin_id}@{version} install_root 非规范路径")
                if not expected.is_dir() or _is_reparse_or_symlink(expected):
                    errors.append(f"插件 {plugin_id}@{version} 安装目录缺失或不安全：{expected}")
                    continue
                info = _scan_package_root(expected, source_type="installed")
                if info.plugin_id != plugin_id or info.version != version:
                    errors.append(f"插件 {plugin_id}@{version} package.json 身份不匹配")
                expected_hash = record.get("sha256")
                if isinstance(expected_hash, str) and expected_hash != info.sha256:
                    errors.append(f"插件 {plugin_id}@{version} sha256 不匹配")
            except Exception as exc:
                errors.append(f"插件 {plugin_id}@{version} 自检失败：{exc}")
    try:
        settings = _load_settings()
        _settings_for_registry(settings, registry)
    except Exception as exc:
        errors.append(f"settings.json 插件投影检查失败：{exc}")
    return errors


__all__ = [
    "PluginManagerError",
    "PluginValidationError",
    "disable",
    "enable",
    "install",
    "inspect_plugin",
    "import_plugin",
    "list_plugins",
    "plugin_registry_path",
    "remove_plugin",
    "rollback",
    "rollback_plugin",
    "self_check",
    "status",
    "set_plugin_enabled",
    "set_plugin_trust",
    "uninstall",
    "validate",
]
