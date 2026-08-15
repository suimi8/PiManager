# -*- coding: utf-8 -*-
"""Pi 插件管理页。

内置插件仍然使用 ``builtin_plugins`` 的原有安装流程；自定义插件通过
``plugin_manager`` 的公共 API 管理。所有可能触碰文件系统的操作都通过
``Worker`` 执行，UI 只负责选择路径、展示后端返回的元数据和确认操作。
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ... import builtin_plugins
from ...ui import Worker
from ..components import SectionHeading, StatusBadge, SurfaceCard


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


def build_plugins_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    body = QWidget()
    layout = QVBoxLayout(body)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    header = SurfaceCard(margins=(17, 15, 17, 15), spacing=10)
    header_row = QHBoxLayout()
    header_row.addWidget(
        SectionHeading(
            "插件管理",
            "统一管理 PiManager 内置插件与用户自定义插件；自定义插件导入前会先做静态预览和风险确认。",
        ),
        1,
    )
    window.plugins_add_btn = window._btn(
        "添加插件", lambda checked=False: _add_plugin(window), success=True
    )
    window.plugins_refresh_btn = window._btn(
        "刷新状态", lambda checked=False: _refresh(window), secondary=True
    )
    window.plugins_install_all_btn = window._btn(
        "全部安装", lambda checked=False: _install_all(window), success=True
    )
    header_row.addWidget(window.plugins_add_btn, 0, Qt.AlignTop)
    header_row.addWidget(window.plugins_refresh_btn, 0, Qt.AlignTop)
    header_row.addWidget(window.plugins_install_all_btn, 0, Qt.AlignTop)
    header.content.addLayout(header_row)

    window.plugins_global_status = QLabel("加载中…")
    window.plugins_global_status.setObjectName("subtitle")
    window.plugins_global_status.setWordWrap(True)
    header.content.addWidget(window.plugins_global_status)
    window.plugins_backend_status = QLabel("")
    window.plugins_backend_status.setObjectName("subtitle")
    window.plugins_backend_status.setWordWrap(True)
    window.plugins_backend_status.setVisible(False)
    header.content.addWidget(window.plugins_backend_status)
    layout.addWidget(header)

    window.plugins_list_container = QVBoxLayout()
    window.plugins_list_container.setSpacing(10)
    layout.addLayout(window.plugins_list_container)
    layout.addStretch(1)

    scroll.setWidget(body)
    outer.addWidget(scroll)

    window._plugin_cards = {}
    window._plugin_refreshing = False
    window._plugin_operation_keys = set()
    window._plugin_pending_after = []
    _bind_page_title(window)
    _refresh(window)
    return page


def _bind_page_title(window) -> None:
    """在不改动导航配置的前提下，把当前页的壳层标题显示为“插件管理”。"""
    nav = getattr(window, "nav", None)
    header = getattr(window, "page_header", None)
    if nav is None or header is None:
        return

    def update_title(key: str) -> None:
        if key == "plugins":
            header.set_page(
                "插件管理",
                "内置 skills / extensions 与用户自定义插件的统一管理。",
            )

    nav.pageChanged.connect(update_title)
    if getattr(nav, "current_key", lambda: "")() == "plugins":
        update_title("plugins")


def _clear_list(window) -> None:
    while window.plugins_list_container.count():
        item = window.plugins_list_container.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


def _collect_plugin_rows() -> dict[str, Any]:
    """在 Worker 中读取内置和自定义插件的状态。"""
    builtin_error = ""
    builtin_statuses: list[dict[str, Any]] = []
    builtin_meta: dict[str, dict[str, Any]] = {}
    try:
        builtin_statuses = [dict(item) for item in builtin_plugins.all_statuses()]
        for item in builtin_plugins.list_builtins():
            data = _as_mapping(item)
            name = str(_first(data, "name", default=""))
            if name:
                builtin_meta[name] = data
    except Exception as exc:
        builtin_error = f"读取内置插件失败：{exc}"

    builtin_rows: list[dict[str, Any]] = []
    builtin_ids: set[str] = set()
    for status in builtin_statuses:
        name = str(status.get("name") or "未命名内置插件")
        meta = builtin_meta.get(name, {})
        row = dict(meta)
        row.update(status)
        row["_origin"] = "builtin"
        row["origin"] = "builtin"
        row["id"] = name
        row["version"] = _first(row, "version", "min_version", default="—")
        row["resources"] = _first(row, "resources", "type", default="")
        row["install_path"] = _first(row, "install_path", "target", default="")
        builtin_rows.append(row)
        builtin_ids.add(name)

    backend_error = ""
    custom_rows: list[dict[str, Any]] = []
    manager, manager_error = _plugin_manager()
    if manager is None:
        backend_error = manager_error
    else:
        try:
            raw = manager.list_plugins()
            error = _result_error(raw)
            if error:
                backend_error = f"读取自定义插件失败：{error}"
            else:
                records = list(_plugin_items(raw))
                if not records:
                    # 注册表可能损坏（schemaVersion 过高 / plugins 非对象）导致
                    # list_plugins 静默返回空列表；用 self_check 兑底区分
                    # “确实没有插件”与“注册表损坏”。
                    checker = getattr(manager, "self_check", None)
                    check_errors: list[str] = []
                    if callable(checker):
                        try:
                            check_errors = [str(item) for item in (checker() or [])]
                        except Exception as exc:
                            check_errors = [f"自检失败：{exc}"]
                    if check_errors:
                        preview = "；".join(check_errors[:5])
                        if len(check_errors) > 5:
                            preview += f"；…等共 {len(check_errors)} 个问题"
                        backend_error = (
                            "自定义插件注册表可能损坏：读取到 0 个插件，"
                            f"自检发现 {len(check_errors)} 个问题：{preview}"
                        )
                else:
                    for record in records:
                        record = dict(record)
                        if _is_builtin(record) or _plugin_id(record) in builtin_ids:
                            continue
                        record["_origin"] = "custom"
                        record.setdefault("origin", "custom")
                        custom_rows.append(record)
        except Exception as exc:
            backend_error = f"读取自定义插件失败：{exc}"

    return {
        "plugins": builtin_rows + custom_rows,
        "builtin_error": builtin_error,
        "backend_error": backend_error,
    }


def _run_pending_after(window, immediate=None) -> None:
    """执行本次刷新后的回调，以及此前因刷新进行中而被排队的回调。"""
    callbacks: list = [immediate] if immediate is not None else []
    pending = getattr(window, "_plugin_pending_after", None)
    if pending:
        callbacks.extend(pending)
        pending.clear()
    for callback in callbacks:
        try:
            callback()
        except Exception:
            import logging

            logging.getLogger(__name__).exception("插件页回调执行失败")


def _refresh(window, after=None) -> None:
    """后台读取插件状态并在主线程重建列表。

    ``after`` 回调在刷新完成后执行；若刷新正在进行中，回调会排队等待
    本次刷新结束，而不是被静默丢弃。
    """
    if getattr(window, "_plugin_refreshing", False):
        if after is not None:
            pending = getattr(window, "_plugin_pending_after", None)
            if pending is None:
                pending = []
                window._plugin_pending_after = pending
            pending.append(after)
        return
    window._plugin_refreshing = True
    window.plugins_refresh_btn.setEnabled(False)
    window.plugins_global_status.setText("正在扫描插件状态…")
    window.plugins_backend_status.setVisible(False)

    worker = Worker(_collect_plugin_rows)
    _track_worker(window, worker)

    def on_done(result: dict[str, Any]) -> None:
        window._plugin_refreshing = False
        window.plugins_refresh_btn.setEnabled(True)
        _render_plugin_rows(window, result or {})
        _run_pending_after(window, after)

    def on_failed(error: str) -> None:
        window._plugin_refreshing = False
        window.plugins_refresh_btn.setEnabled(True)
        _clear_list(window)
        window._plugin_cards = {}
        _set_label_error(window.plugins_global_status, f"扫描插件失败：{error}")
        _set_label_error(window.plugins_backend_status, "插件状态未更新；请修复后端或稍后重试。")
        window.plugins_backend_status.setVisible(True)
        _re_enable_btns(window)
        _run_pending_after(window, after)

    worker.done.connect(on_done)
    worker.failed.connect(on_failed)
    worker.start()


def _render_plugin_rows(window, result: Mapping[str, Any]) -> None:
    _clear_list(window)
    window._plugin_cards = {}
    rows = list(result.get("plugins") or [])
    for plugin in rows:
        row = dict(plugin)
        card = _build_plugin_card(window, row)
        window.plugins_list_container.addWidget(card)
        key = _plugin_id(row) or _plugin_name(row)
        window._plugin_cards[key] = card

    if not rows:
        empty = QLabel("暂无可显示的插件。可点击“添加插件”导入本地目录或 ZIP。")
        empty.setObjectName("subtitle")
        empty.setWordWrap(True)
        window.plugins_list_container.addWidget(empty)

    builtin_count = sum(1 for row in rows if row.get("_origin") == "builtin")
    custom_count = len(rows) - builtin_count
    ready_count = sum(
        1
        for row in rows
        if _status_text(row) in {"已启用", "已就绪"} or row.get("ready")
    )
    window.plugins_global_status.setText(
        f"共 {len(rows)} 个插件：内置 {builtin_count} 个 · 自定义 {custom_count} 个 · "
        f"{ready_count} 个已启用/就绪 · 安装目标：~/.pi/agent/"
    )

    messages = []
    if result.get("builtin_error"):
        messages.append(str(result["builtin_error"]))
    if result.get("backend_error"):
        messages.append(
            f"{result['backend_error']}。内置插件仍可使用；自定义插件导入、启停和卸载暂不可用。"
        )
    window._plugin_backend_error = str(result.get("backend_error") or "")
    if messages:
        _set_label_error(window.plugins_backend_status, "\n".join(messages))
        window.plugins_backend_status.setVisible(True)
    else:
        window.plugins_backend_status.clear()
        window.plugins_backend_status.setVisible(False)
    _re_enable_btns(window)


def _build_plugin_card(window, plugin: dict[str, Any]) -> QWidget:
    if plugin.get("_origin") == "builtin":
        return _build_builtin_card(window, plugin)
    return _build_custom_card(window, plugin)


def _build_builtin_card(window, status: dict[str, Any]) -> QWidget:
    name = str(status.get("name") or "")
    ptype = str(status.get("type") or "")
    desc = str(status.get("description") or "")
    target = str(status.get("target") or "")
    version = _plugin_version(status)

    card = SurfaceCard(margins=(15, 13, 15, 13), spacing=8)
    title_row = QHBoxLayout()
    title_row.setSpacing(8)
    title = QLabel(name)
    title.setObjectName("cardTitle")
    title_row.addWidget(title)
    title_row.addWidget(StatusBadge("内置", "info"))
    title_row.addWidget(StatusBadge(_type_label(ptype), "info"))
    title_row.addWidget(_badge_for_status(status))
    title_row.addStretch(1)
    card.content.addLayout(title_row)

    desc_lbl = QLabel(desc)
    desc_lbl.setObjectName("subtitle")
    desc_lbl.setWordWrap(True)
    card.content.addWidget(desc_lbl)

    meta_lbl = QLabel(
        f"来源：内置 · 类型/资源：{_resource_label(status)} · 版本：{version} · 信任：官方内置"
    )
    meta_lbl.setObjectName("subtitle")
    meta_lbl.setWordWrap(True)
    card.content.addWidget(meta_lbl)

    path_lbl = QLabel(f"安装路径：{target or '—'}")
    path_lbl.setObjectName("statusBadge")
    path_lbl.setWordWrap(True)
    card.content.addWidget(path_lbl)

    if status.get("needs_npm_install"):
        if status.get("on_disk"):
            npm_text = "依赖已安装 ✓" if status.get("npm_installed") else "依赖未安装（需 npm install）"
        else:
            npm_text = "未落盘"
        npm_lbl = QLabel(f"npm 依赖：{npm_text}")
        npm_lbl.setObjectName("subtitle")
        card.content.addWidget(npm_lbl)

    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)
    btn_row.addStretch(1)
    install_btn = window._btn(
        "一键安装",
        lambda checked=False, n=name: _install_one(window, n),
        success=True,
    )
    reinstall_btn = window._btn(
        "重装",
        lambda checked=False, n=name: _install_one(window, n, force=True),
        secondary=True,
    )
    btn_row.addWidget(reinstall_btn)
    btn_row.addWidget(install_btn)
    card.content.addLayout(btn_row)

    result_lbl = QLabel("")
    result_lbl.setObjectName("subtitle")
    result_lbl.setWordWrap(True)
    card.content.addWidget(result_lbl)
    card._result_label = result_lbl
    card._install_btn = install_btn
    card._reinstall_btn = reinstall_btn
    card._plugin_action_buttons = [install_btn, reinstall_btn]
    return card


def _build_custom_card(window, plugin: dict[str, Any]) -> QWidget:
    plugin_id = _plugin_id(plugin)
    name = _plugin_name(plugin)
    resources = _resource_label(plugin)
    version = _plugin_version(plugin)
    path = _plugin_path(plugin)
    trusted = _is_trusted(plugin)
    enabled = _is_enabled(plugin)
    active_version = _plugin_version(plugin)
    is_missing = _status_text(plugin) == "目录缺失"
    # 若后端返回带 installed 标志的版本记录，排除目录已缺失的版本，
    # 避免回滚对话框列出必然失败的选项；否则退回原始版本号列表。
    versions_map = _first(
        plugin, "versions", "version_records", "records", default=None
    )
    if isinstance(versions_map, Mapping):
        available_versions = [
            str(ver)
            for ver, record in versions_map.items()
            if str(ver) != active_version
            and not (isinstance(record, Mapping) and record.get("installed") is False)
        ]
    else:
        available_versions = [
            str(item)
            for item in (_first(plugin, "available_versions", default=[]) or [])
            if str(item) != active_version
        ]

    card = SurfaceCard(margins=(15, 13, 15, 13), spacing=8)
    title_row = QHBoxLayout()
    title_row.setSpacing(8)
    title = QLabel(name)
    title.setObjectName("cardTitle")
    title_row.addWidget(title)
    title_row.addWidget(StatusBadge("自定义", "warning"))
    title_row.addWidget(
        StatusBadge(_type_label(_first(plugin, "type", "kind", default="")), "info")
    )
    title_row.addWidget(_status_badge(plugin))
    title_row.addWidget(StatusBadge(_trust_text(plugin), "success" if trusted else "warning"))
    title_row.addStretch(1)
    card.content.addLayout(title_row)

    if is_missing:
        missing_lbl = QLabel("安装目录缺失，可卸载后重新导入。")
        missing_lbl.setObjectName("statusBadge")
        missing_lbl.setProperty("status", "danger")
        missing_lbl.setWordWrap(True)
        card.content.addWidget(missing_lbl)

    desc = QLabel(str(_first(plugin, "description", "summary", default="")))
    desc.setObjectName("subtitle")
    desc.setWordWrap(True)
    card.content.addWidget(desc)

    meta = QLabel(
        f"来源：自定义（{_first(plugin, 'source_type', 'sourceType', default='未知')}） · "
        f"类型/资源：{resources} · 版本：{version} · ID：{plugin_id or '—'}"
    )
    meta.setObjectName("subtitle")
    meta.setWordWrap(True)
    card.content.addWidget(meta)

    permissions = QLabel(f"权限/能力：{_permissions_label(plugin)}")
    permissions.setObjectName("subtitle")
    permissions.setWordWrap(True)
    card.content.addWidget(permissions)

    warnings = _first(plugin, "warnings", "warning", default=None)
    if warnings:
        warning_lbl = QLabel(f"警告：{_display(warnings)}")
        warning_lbl.setObjectName("subtitle")
        warning_lbl.setWordWrap(True)
        card.content.addWidget(warning_lbl)

    path_lbl = QLabel(f"安装路径：{path}")
    path_lbl.setObjectName("statusBadge")
    path_lbl.setWordWrap(True)
    card.content.addWidget(path_lbl)

    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)
    btn_row.addStretch(1)
    rescan_btn = window._btn(
        "重新扫描",
        lambda checked=False, pid=plugin_id: _rescan_plugin(window, pid),
        secondary=True,
    )
    toggle_text = "禁用" if enabled else "启用"
    toggle_btn = window._btn(
        toggle_text,
        lambda checked=False, pid=plugin_id, target=not enabled: _set_plugin_enabled(window, pid, target),
        success=not enabled,
        secondary=enabled,
    )
    action_buttons = [rescan_btn, toggle_btn]
    rollback_btn = None
    if available_versions:
        rollback_btn = window._btn(
            "回滚",
            lambda checked=False, pid=plugin_id, label=name, versions=available_versions: _rollback_plugin(
                window, pid, label, versions
            ),
            secondary=True,
        )
        rollback_btn.setToolTip("在已校验并保留的历史版本之间切换。")
    if not trusted:
        if is_missing:
            toggle_btn.setEnabled(False)
            toggle_btn.setProperty("lockedByMissing", True)
            toggle_btn.setToolTip("安装目录缺失，请先卸载后重新导入。")
        else:
            toggle_btn.setEnabled(False)
            toggle_btn.setProperty("lockedByTrust", True)
            toggle_btn.setToolTip("插件尚未信任；请先点击“信任并启用”并确认权限后再启用。")
        trust_btn = window._btn(
            "信任并启用",
            lambda checked=False, pid=plugin_id, label=name: _trust_plugin(window, pid, label),
            success=True,
        )
        trust_btn.setToolTip("确认插件权限后，允许其在 Pi 进程中加载声明的 Extension。")
        if is_missing:
            trust_btn.setEnabled(False)
            trust_btn.setProperty("lockedByMissing", True)
            trust_btn.setToolTip("安装目录缺失，无法信任；请先卸载后重新导入。")
    elif is_missing:
        # 已信任但目录缺失：启停操作必然失败，直接禁用并提示。
        toggle_btn.setEnabled(False)
        toggle_btn.setProperty("lockedByMissing", True)
        toggle_btn.setToolTip("安装目录缺失，请先卸载后重新导入。")
    if rollback_btn is not None and is_missing:
        rollback_btn.setEnabled(False)
        rollback_btn.setProperty("lockedByMissing", True)
        rollback_btn.setToolTip("历史版本目录缺失，无法回滚。")
    remove_btn = window._btn(
        "卸载",
        lambda checked=False, pid=plugin_id, label=name: _remove_plugin(window, pid, label),
        danger=True,
        secondary=True,
    )
    btn_row.addWidget(rescan_btn)
    if rollback_btn is not None:
        btn_row.addWidget(rollback_btn)
    if not trusted:
        btn_row.addWidget(trust_btn)
    btn_row.addWidget(toggle_btn)
    btn_row.addWidget(remove_btn)
    card.content.addLayout(btn_row)

    result_lbl = QLabel("")
    result_lbl.setObjectName("subtitle")
    result_lbl.setWordWrap(True)
    card.content.addWidget(result_lbl)
    card._result_label = result_lbl
    card._plugin_id = plugin_id
    card._plugin_missing = is_missing
    action_buttons.extend([remove_btn])
    if rollback_btn is not None:
        action_buttons.append(rollback_btn)
    if not trusted:
        action_buttons.append(trust_btn)
    card._plugin_action_buttons = action_buttons
    return card


def _set_card_result(window, plugin_key: str, text: str, *, ok: bool) -> None:
    card = getattr(window, "_plugin_cards", {}).get(plugin_key)
    if card is None:
        return
    label: QLabel = getattr(card, "_result_label", None)
    if label is None:
        return
    _set_label_error(label, text, error=not ok)


def _set_card_busy(window, plugin_key: str, text: str) -> None:
    card = getattr(window, "_plugin_cards", {}).get(plugin_key)
    keys = getattr(window, "_plugin_operation_keys", None)
    if keys is None:
        keys = window._plugin_operation_keys = set()
    if card is None:
        # 卡片已不存在（如刷新后 key 变化），不残留 busy 标记。
        keys.discard(plugin_key)
        return
    keys.add(plugin_key)
    _set_card_result(window, plugin_key, text, ok=True)
    for button in getattr(card, "_plugin_action_buttons", []):
        button.setEnabled(False)


def _track_worker(window, worker) -> None:
    """登记 Worker，避免 QThread 运行中被垃圾回收。"""
    window._active_workers = getattr(window, "_active_workers", [])
    window._active_workers.append(worker)
    worker.finished.connect(worker.deleteLater)
    worker.finished.connect(lambda w=worker: _untrack_worker(window, w))


def _untrack_worker(window, worker) -> None:
    workers = getattr(window, "_active_workers", [])
    try:
        workers.remove(worker)
    except ValueError:
        pass


def _install_one(window, name: str, *, force: bool = False) -> None:
    """后台一键安装单个内置插件；保留原有安装行为。"""
    keys = getattr(window, "_plugin_operation_keys", None)
    if keys is None:
        keys = window._plugin_operation_keys = set()
    if name in keys:
        _set_card_result(window, name, "该插件有操作进行中，请稍候。", ok=False)
        return
    _set_card_busy(window, name, "安装中…")

    def task():
        if force:
            builtin_plugins.install_builtin(name, force=True)
        return builtin_plugins.install_one_click(name)

    worker = Worker(task)
    _track_worker(window, worker)

    def on_done(result):
        def finish():
            keys.discard(name)
            if result.get("ok"):
                _set_card_result(window, name, "安装成功，pi 下次启动时自动加载。", ok=True)
            else:
                cmd = result.get("command") or ""
                err = result.get("error") or "安装失败"
                message = f"失败：{err}"
                if cmd:
                    message += f"\n请手动执行：\n{cmd}"
                _set_card_result(window, name, message, ok=False)
            _re_enable_btns(window)

        _refresh(window, after=finish)

    def on_failed(error):
        def finish():
            keys.discard(name)
            _set_card_result(window, name, f"出错：{error}", ok=False)
            _re_enable_btns(window)

        _refresh(window, after=finish)

    worker.done.connect(on_done)
    worker.failed.connect(on_failed)
    worker.start()


def _install_all(window) -> None:
    """后台一键安装所有内置插件（含需 npm install 的）。"""
    window.plugins_install_all_btn.setEnabled(False)
    window.plugins_global_status.setText("正在安装全部内置插件…")

    def task():
        builtin_plugins.install_all_builtins(include_disabled=True)
        results = []
        for status in builtin_plugins.all_statuses():
            if status.get("needs_npm_install") and status.get("on_disk") and not status.get("npm_installed"):
                results.append(builtin_plugins.npm_install(status["name"]))
        return results

    worker = Worker(task)
    _track_worker(window, worker)

    def on_done(results):
        def finish():
            failed = [item for item in results if not item.get("ok") and not item.get("skipped")]
            if failed:
                commands = "\n".join(
                    {item.get("command", "") for item in failed if item.get("command")}
                )
                window.plugins_global_status.setText(
                    f"部分插件 npm install 失败（{len(failed)}/{len(results)}）。请手动执行：\n{commands}"
                )
            else:
                window.plugins_global_status.setText("全部内置插件安装完成，pi 下次启动时自动加载。")
            _re_enable_btns(window)

        _refresh(window, after=finish)

    def on_failed(error):
        def finish():
            window.plugins_global_status.setText(f"安装出错：{error}")
            _re_enable_btns(window)

        _refresh(window, after=finish)

    worker.done.connect(on_done)
    worker.failed.connect(on_failed)
    worker.start()


def _choose_plugin_source(window) -> str:
    chooser = QMessageBox(window)
    chooser.setWindowTitle("添加插件")
    chooser.setText("选择要导入的自定义插件来源")
    chooser.setInformativeText("可以导入本地插件目录，也可以导入包含标准清单的 ZIP 文件。")
    directory_btn = chooser.addButton("本地目录", QMessageBox.AcceptRole)
    zip_btn = chooser.addButton("ZIP 文件", QMessageBox.AcceptRole)
    chooser.addButton("取消", QMessageBox.RejectRole)
    chooser.exec()
    clicked = chooser.clickedButton()
    if clicked is directory_btn:
        return QFileDialog.getExistingDirectory(window, "选择插件目录", "") or ""
    if clicked is zip_btn:
        path, _ = QFileDialog.getOpenFileName(
            window,
            "选择插件 ZIP",
            "",
            "ZIP 文件 (*.zip);;所有文件 (*)",
        )
        return path or ""
    return ""


def _inspect_task(source: str) -> Any:
    manager, error = _plugin_manager()
    if manager is None:
        raise RuntimeError(error)
    return manager.inspect_plugin(source)


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


def _confirm_plugin_import(window, source: str, inspection: Any) -> bool | None:
    dialog = QDialog(window)
    dialog.setWindowTitle("确认导入插件")
    dialog.resize(760, 560)
    layout = QVBoxLayout(dialog)
    heading = QLabel("请确认插件信息与风险")
    heading.setObjectName("sectionTitle")
    layout.addWidget(heading)

    details = QPlainTextEdit()
    details.setReadOnly(True)
    details.setObjectName("mono")
    details.setPlainText(_preview_lines(source, inspection))
    layout.addWidget(details, 1)

    trust_hint = QLabel("仅导入会保持插件未信任/禁用；选择“信任并导入”才允许之后启用。")
    trust_hint.setObjectName("subtitle")
    trust_hint.setWordWrap(True)
    layout.addWidget(trust_hint)
    trust_check = QCheckBox("我已阅读权限和警告，并信任该插件来源")
    layout.addWidget(trust_check)

    decision: dict[str, bool | None] = {"trust": None}

    def accept_import(trust: bool) -> None:
        if trust and not trust_check.isChecked():
            QMessageBox.warning(
                dialog,
                "需要确认信任",
                "请先勾选“我已阅读权限和警告，并信任该插件来源”。",
            )
            return
        decision["trust"] = trust
        dialog.accept()

    buttons = QHBoxLayout()
    buttons.addStretch(1)
    cancel_btn = window._btn("取消", lambda checked=False: dialog.reject(), ghost=True)
    import_btn = window._btn(
        "仅导入（不信任）",
        lambda checked=False: accept_import(False),
        secondary=True,
    )
    trust_btn = window._btn(
        "信任并导入",
        lambda checked=False: accept_import(True),
        success=True,
    )
    buttons.addWidget(cancel_btn)
    buttons.addWidget(import_btn)
    buttons.addWidget(trust_btn)
    layout.addLayout(buttons)

    if dialog.exec() != QDialog.Accepted:
        return None
    return decision["trust"]


def _add_plugin(window) -> None:
    backend_error = str(getattr(window, "_plugin_backend_error", "") or "")
    if backend_error:
        QMessageBox.warning(window, "自定义插件不可用", backend_error)
        return
    source = _choose_plugin_source(window)
    if not source:
        return

    window._plugin_importing = True
    window.plugins_add_btn.setEnabled(False)
    window.plugins_global_status.setText("正在检查插件清单和风险…")
    worker = Worker(_inspect_task, source)
    _track_worker(window, worker)

    def on_done(inspection):
        error_text = _result_error(inspection)
        if error_text:
            QMessageBox.warning(window, "插件检查失败", error_text)
            window.plugins_global_status.setText(f"插件检查失败：{error_text}")
            window._plugin_importing = False
            window.plugins_add_btn.setEnabled(True)
            return
        preview = _preview_record(inspection)
        if not preview:
            QMessageBox.warning(window, "插件检查失败", "后端没有返回可预览的插件元数据，已停止导入。")
            window._plugin_importing = False
            window.plugins_add_btn.setEnabled(True)
            return
        trust = _confirm_plugin_import(window, source, inspection)
        if trust is None:
            window.plugins_global_status.setText("已取消插件导入。")
            window._plugin_importing = False
            window.plugins_add_btn.setEnabled(True)
            return
        _import_plugin(window, source, trust=bool(trust))

    def on_failed(error_text):
        window._plugin_importing = False
        window.plugins_add_btn.setEnabled(True)
        window.plugins_global_status.setText(f"插件检查出错：{error_text}")
        QMessageBox.warning(window, "插件检查出错", error_text)

    worker.done.connect(on_done)
    worker.failed.connect(on_failed)
    worker.start()


def _import_plugin(window, source: str, *, trust: bool) -> None:
    window.plugins_global_status.setText("正在导入插件…")

    def task():
        manager, error = _plugin_manager()
        if manager is None:
            raise RuntimeError(error)
        # 显式传入 enable=False，导入不会直接启用插件。
        return manager.import_plugin(source, enable=False, trust=trust)

    worker = Worker(task)
    _track_worker(window, worker)

    def on_done(result):
        error_text = _result_error(result)
        if error_text:
            window.plugins_global_status.setText(f"插件导入失败：{error_text}")
            QMessageBox.warning(window, "插件导入失败", error_text)
        else:
            window.plugins_global_status.setText("插件导入完成，正在刷新列表…")
        window._plugin_importing = False
        window.plugins_add_btn.setEnabled(True)
        _refresh(window)

    def on_failed(error_text):
        window._plugin_importing = False
        window.plugins_add_btn.setEnabled(True)
        window.plugins_global_status.setText(f"插件导入出错：{error_text}")
        QMessageBox.warning(window, "插件导入出错", error_text)
        _refresh(window)

    worker.done.connect(on_done)
    worker.failed.connect(on_failed)
    worker.start()


def _manager_operation(window, plugin_id: str, operation: str, task_factory, success_text: str) -> None:
    if not plugin_id:
        QMessageBox.warning(window, "插件操作失败", "插件 ID 为空，无法执行操作。")
        return
    keys = getattr(window, "_plugin_operation_keys", None)
    if keys is None:
        keys = window._plugin_operation_keys = set()
    if plugin_id in keys:
        window.plugins_global_status.setText(
            f"该插件有操作进行中，已忽略新的“{operation}”请求。"
        )
        _set_card_result(
            window, plugin_id, "该插件有操作进行中，请等待当前操作完成。", ok=False
        )
        return
    keys.add(plugin_id)
    _set_card_busy(window, plugin_id, f"正在{operation}…")

    worker = Worker(task_factory)
    _track_worker(window, worker)

    def on_done(result):
        error_text = _result_error(result)

        def finish():
            keys.discard(plugin_id)
            data = _as_mapping(result)
            warning = str(data.get("warning") or "").strip()
            cleanup_pending = bool(data.get("cleanup_pending"))
            if error_text:
                window.plugins_global_status.setText(f"插件{operation}失败：{error_text}")
                _set_card_result(window, plugin_id, f"失败：{error_text}", ok=False)
            elif warning:
                # 成功但带警告（如卸载后 trash 清理失败）：以警告样式展示，
                # 不再静默丢弃 remove_plugin 的 cleanup_pending/warning。
                summary = success_text
                if cleanup_pending:
                    summary = f"插件已{operation}（部分清理未完成）"
                window.plugins_global_status.setText(f"{summary}；{warning}")
                _set_card_result(
                    window, plugin_id, f"{summary}\n警告：{warning}", ok=False
                )
            else:
                window.plugins_global_status.setText(success_text)
                _set_card_result(window, plugin_id, success_text, ok=True)
            _re_enable_btns(window)

        _refresh(window, after=finish)

    def on_failed(error_text):
        def finish():
            keys.discard(plugin_id)
            window.plugins_global_status.setText(f"插件{operation}出错：{error_text}")
            _set_card_result(window, plugin_id, f"出错：{error_text}", ok=False)
            _re_enable_btns(window)

        _refresh(window, after=finish)

    worker.done.connect(on_done)
    worker.failed.connect(on_failed)
    worker.start()


def _set_plugin_enabled(window, plugin_id: str, enabled: bool) -> None:
    def task():
        manager, error = _plugin_manager()
        if manager is None:
            raise RuntimeError(error)
        return manager.set_plugin_enabled(plugin_id, enabled)

    _manager_operation(
        window,
        plugin_id,
        "启用" if enabled else "禁用",
        task,
        f"插件已{'启用' if enabled else '禁用'}，Pi 下次启动时生效。",
    )


def _trust_plugin(window, plugin_id: str, name: str) -> None:
    reply = QMessageBox.question(
        window,
        "确认信任插件",
        (
            f"确定信任并启用“{name}”吗？\n\n"
            "Extension 可能在 Pi 进程中访问文件、网络、环境变量或启动子进程；"
            "PiManager 的 trust 标记不提供沙箱。"
        ),
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    if reply != QMessageBox.Yes:
        return

    def task():
        manager, error = _plugin_manager()
        if manager is None:
            raise RuntimeError(error)
        return manager.set_plugin_trust(plugin_id, True, enable=True)

    _manager_operation(
        window,
        plugin_id,
        "信任并启用",
        task,
        "插件已标记为信任并启用，Pi 下次启动时生效。",
    )


def _rollback_plugin(
    window,
    plugin_id: str,
    name: str,
    versions: list[str],
) -> None:
    target, accepted = QInputDialog.getItem(
        window,
        "选择回滚版本",
        f"选择“{name}”要切换到的历史版本：",
        versions,
        0,
        False,
    )
    if not accepted or not target:
        return

    def task():
        manager, error = _plugin_manager()
        if manager is None:
            raise RuntimeError(error)
        return manager.rollback_plugin(plugin_id, str(target))

    _manager_operation(
        window,
        plugin_id,
        "回滚",
        task,
        f"插件已回滚到 {target}，Pi 下次启动时生效。",
    )


def _remove_plugin(window, plugin_id: str, name: str) -> None:
    reply = QMessageBox.question(
        window,
        "确认卸载插件",
        f"确定卸载自定义插件“{name}”吗？此操作不会执行插件清理代码。",
        QMessageBox.Yes | QMessageBox.No,
        QMessageBox.No,
    )
    if reply != QMessageBox.Yes:
        return

    def task():
        manager, error = _plugin_manager()
        if manager is None:
            raise RuntimeError(error)
        return manager.remove_plugin(plugin_id)

    _manager_operation(window, plugin_id, "卸载", task, "插件已卸载，正在刷新列表…")


def _rescan_plugin(window, _plugin_id: str = "") -> None:
    """后端暂无独立 rescan API，重新调用 list_plugins 即完成重新扫描。"""
    _refresh(window)


def _re_enable_btns(window) -> None:
    if hasattr(window, "plugins_install_all_btn"):
        window.plugins_install_all_btn.setEnabled(True)
    if hasattr(window, "plugins_add_btn") and not getattr(window, "_plugin_importing", False):
        window.plugins_add_btn.setEnabled(True)
    busy = getattr(window, "_plugin_operation_keys", None) or set()
    for key, card in getattr(window, "_plugin_cards", {}).items():
        if key in busy:
            # 该插件仍有操作进行中：保持按钮禁用，避免形成并发操作。
            continue
        for button in getattr(card, "_plugin_action_buttons", []):
            if button.property("lockedByTrust") or button.property("lockedByMissing"):
                continue
            button.setEnabled(True)
