"""插件记录的展示与后端探测，不改窗口状态。"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from PySide6.QtWidgets import QLabel

from ...components import StatusBadge


_PLUGIN_MANAGER_API = (
    "list_plugins",
    "inspect_plugin",
    "import_plugin",
    "set_plugin_enabled",
    "set_plugin_trust",
    "remove_plugin",
    "rollback_plugin",
)

def _plugin_manager() -> tuple[Any | None, str]:
    """加载可选后端；后端不可用时由页面展示可操作的错误。"""
    try:
        from ... import plugin_manager
    except Exception as exc:
        return None, f"自定义插件管理后端不可用：{exc}"
    missing = [
        name for name in _PLUGIN_MANAGER_API
        if not callable(getattr(plugin_manager, name, None))
    ]
    if missing:
        return None, f"自定义插件管理后端 API 不完整：缺少 {', '.join(missing)}"
    return plugin_manager, ""

def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        values = vars(value)
    except TypeError:
        return {}
    return dict(values) if isinstance(values, Mapping) else {}

def _first(data: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return default

def _display(value: Any, default: str = "—", limit: int = 900) -> str:
    if value is None or value == "":
        return default
    if isinstance(value, (Mapping, list, tuple, set)):
        try:
            text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
        except (TypeError, ValueError):
            text = str(value)
    else:
        text = str(value)
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text

def _plugin_items(value: Any) -> list[dict[str, Any]]:
    """把后端可能返回的列表/包装字典统一成插件记录列表。"""
    if value is None:
        return []
    if isinstance(value, Mapping):
        for key in ("plugins", "items", "results"):
            if key in value:
                return _plugin_items(value.get(key))
        if any(key in value for key in ("id", "plugin_id", "name", "version")):
            return [_as_mapping(value)]
        result: list[dict[str, Any]] = []
        for key, item in value.items():
            record = _as_mapping(item)
            if record:
                record.setdefault("id", str(key))
                result.append(record)
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for item in value:
            record = _as_mapping(item)
            if record:
                result.append(record)
        return result
    record = _as_mapping(value)
    return [record] if record else []

def _result_error(value: Any) -> str:
    data = _as_mapping(value)
    if isinstance(value, bool) and not value:
        return "后端操作返回失败"
    if data.get("ok") is False or data.get("success") is False:
        return str(_first(data, "error", "message", "reason", default="后端操作返回失败"))
    error = _first(data, "error", "exception", default="")
    return str(error) if error else ""

def _type_label(value: Any) -> str:
    text = str(value or "").strip().lower()
    return {
        "skill": "Skill",
        "skills": "Skills",
        "extension": "Extension",
        "extensions": "Extensions",
        "package": "Package",
    }.get(text, str(value or "未知"))

def _resource_label(plugin: Mapping[str, Any]) -> str:
    resources = _first(plugin, "resources", "resource", default=None)
    if isinstance(resources, Mapping):
        names = [str(key) for key, value in resources.items() if value]
        if names:
            return ", ".join(names)
    if isinstance(resources, (list, tuple, set)):
        names = [str(item) for item in resources if item]
        if names:
            return ", ".join(names)
    if resources:
        return str(resources)
    ptype = _first(plugin, "type", "kind", default="")
    return _type_label(ptype) if ptype else "—"

def _permissions_label(plugin: Mapping[str, Any]) -> str:
    permissions = _first(plugin, "permissions", "capabilities", default=None)
    if permissions is None:
        manifest = _as_mapping(plugin.get("manifest"))
        manager = _as_mapping(manifest.get("piManager"))
        permissions = _first(manager, "permissions", "capabilities", default=None)
    if permissions is None:
        return "未声明"
    if isinstance(permissions, Mapping):
        parts = []
        for key, value in permissions.items():
            parts.append(f"{key}: {_display(value, limit=280)}")
        return "；".join(parts) if parts else "未声明"
    if isinstance(permissions, (list, tuple, set)):
        return "、".join(str(item) for item in permissions) or "未声明"
    return str(permissions)

def _plugin_id(plugin: Mapping[str, Any]) -> str:
    return str(_first(plugin, "id", "plugin_id", "pluginId", "name", default=""))

def _plugin_name(plugin: Mapping[str, Any]) -> str:
    return str(
        _first(
            plugin,
            "display_name",
            "displayName",
            "name",
            "id",
            "plugin_id",
            default="未命名插件",
        )
    )

def _plugin_version(plugin: Mapping[str, Any]) -> str:
    return str(_first(plugin, "version", "plugin_version", "min_version", default="—"))

def _plugin_path(plugin: Mapping[str, Any]) -> str:
    value = _first(
        plugin,
        "install_path",
        "installPath",
        "installRoot",
        "target",
        "path",
        "location",
        default="",
    )
    return _display(value)

def _origin_label(plugin: Mapping[str, Any]) -> str:
    origin = str(_first(plugin, "origin", "source_type", "sourceType", default="custom")).lower()
    if plugin.get("builtin") is True or origin in {"builtin", "built-in", "内置"}:
        return "内置"
    return "自定义"

def _is_builtin(plugin: Mapping[str, Any]) -> bool:
    return _origin_label(plugin) == "内置"

def _status_text(plugin: Mapping[str, Any]) -> str:
    value = _first(plugin, "status", "state", default="")
    if isinstance(value, Mapping):
        value = _first(value, "status", "state", "label", default="")
    text = str(value or "").strip().lower()
    if text in {"missing", "not-installed", "dir-missing", "目录缺失", "已安装但目录缺失", "安装目录缺失"}:
        return "目录缺失"
    if text in {"enabled", "active", "ready", "running", "已启用", "已就绪"}:
        return "已启用" if text not in {"ready", "已就绪"} else "已就绪"
    if text in {"disabled", "inactive", "installed-disabled", "已禁用"}:
        return "已禁用"
    if text in {"pending-trust", "pending", "untrusted", "待信任"}:
        return "待信任"
    if text in {"enabled-partial", "partial", "部分启用"}:
        return "部分启用"
    if text in {"broken", "error", "failed", "异常", "失败"}:
        return "异常"
    if text in {"removed", "uninstalled", "未安装"}:
        return "未安装"
    enabled = _first(plugin, "enabled", "is_enabled", default=None)
    if isinstance(enabled, bool):
        return "已启用" if enabled else "已禁用"
    installed = _first(plugin, "installed", "on_disk", default=None)
    if installed is False:
        return "未安装"
    if installed is True:
        return "已安装"
    return str(value) if value else "未知"

def _trust_text(plugin: Mapping[str, Any]) -> str:
    if _is_builtin(plugin):
        return "官方内置"
    value = _first(plugin, "trust", "trust_status", "trustStatus", "trusted", default=None)
    if isinstance(value, bool):
        return "已信任" if value else "待信任"
    text = str(value or "").strip().lower()
    if text in {"trusted", "verified", "official", "已信任", "信任"}:
        return "已信任"
    if text in {"pending", "pending-trust", "untrusted", "待信任", "未信任"}:
        return "待信任"
    return str(value) if value else "未知"

def _is_trusted(plugin: Mapping[str, Any]) -> bool:
    if _is_builtin(plugin):
        return True
    value = _first(plugin, "trusted", "trust", "trust_status", "trustStatus", default=False)
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {
        "trusted",
        "verified",
        "official",
        "已信任",
        "信任",
    }

def _is_enabled(plugin: Mapping[str, Any]) -> bool:
    status_value = _first(plugin, "status", "state", default="")
    if isinstance(status_value, Mapping):
        status_value = _first(status_value, "status", "state", "label", default="")
    if str(status_value or "").strip().lower() in {
        "missing",
        "not-installed",
        "dir-missing",
        "目录缺失",
        "已安装但目录缺失",
        "安装目录缺失",
    }:
        return False
    value = _first(plugin, "enabled", "is_enabled", default=None)
    if isinstance(value, bool):
        return value
    return _status_text(plugin) in {"已启用", "已就绪"}

def _status_badge(plugin: Mapping[str, Any]) -> StatusBadge:
    text = _status_text(plugin)
    color = {
        "已启用": "success",
        "已就绪": "success",
        "已安装": "info",
        "已禁用": "neutral",
        "待信任": "warning",
        "部分启用": "warning",
        "目录缺失": "danger",
        "未安装": "neutral",
        "异常": "danger",
    }.get(text, "neutral")
    return StatusBadge(text, color)

def _badge_for_status(status: dict) -> StatusBadge:
    if status.get("ready"):
        return StatusBadge("已就绪", "success")
    if status.get("on_disk") and status.get("needs_npm_install") and not status.get("npm_installed"):
        return StatusBadge("待 npm install", "warning")
    if status.get("on_disk"):
        return StatusBadge("已落盘", "info")
    return StatusBadge("未安装", "neutral")

def _set_label_error(label: QLabel, text: str, *, error: bool = True) -> None:
    label.setText(text)
    label.setProperty("error", error)
    label.style().unpolish(label)
    label.style().polish(label)

def _preview_record(value: Any) -> dict[str, Any]:
    data = _as_mapping(value)
    for key in ("plugin", "manifest", "metadata", "preview"):
        nested = _as_mapping(data.get(key))
        if nested:
            merged = dict(nested)
            merged.update({k: v for k, v in data.items() if k != key})
            data = merged
            break
    return data

def _files_preview(files: Any) -> str:
    """把 inspect 结果中的文件清单渲染成预览文本（前 30 条）。"""
    if files is None:
        return "未提供"
    if isinstance(files, (list, tuple, set)):
        items = [str(item) for item in files]
        total = len(items)
        shown = items[:30]
        lines = [f"  - {item}" for item in shown]
        if total > 30:
            lines.append(f"  …共 {total} 个文件（仅显示前 30 条）")
        return "\n".join(lines) if lines else "（空清单）"
    if isinstance(files, Mapping):
        # 兼容 {path: size} 形式的文件清单。
        items = [f"  - {key}（{value}）" for key, value in files.items()]
        total = len(items)
        shown = items[:30]
        lines = list(shown)
        if total > 30:
            lines.append(f"  …共 {total} 个文件（仅显示前 30 条）")
        return "\n".join(lines) if lines else "（空清单）"
    return _display(files, "未提供", limit=900)

def _preview_lines(source: str, value: Any) -> str:
    data = _preview_record(value)
    resources = _resource_label(data)
    files = _first(data, "files", "entries", "file_list", "fileList", default=None)
    warnings = _first(data, "warnings", "warning", default=None)
    permissions = _permissions_label(data)
    lines = [
        f"来源路径：{source}",
        f"插件 ID：{_plugin_id(data) or '—'}",
        f"名称：{_plugin_name(data)}",
        f"版本：{_plugin_version(data)}",
        f"类型/资源：{resources}",
        f"权限/能力：{permissions}",
        f"兼容性：{_display(_first(data, 'compatibility', 'compat', default='未声明'))}",
        "文件清单：",
        _files_preview(files),
        "",
        "警告：",
        _display(warnings, "无"),
        "",
        "Extension 可能在 Pi 进程中拥有较高权限；此页面只做元数据检查，不执行插件代码或 npm 命令。",
    ]
    return "\n".join(lines)
