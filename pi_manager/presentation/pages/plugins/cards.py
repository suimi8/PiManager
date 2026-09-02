"""插件卡片：内置 / 自定义两套布局。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from ...components import StatusBadge, SurfaceCard
from .format import (
    _badge_for_status,
    _display,
    _first,
    _is_enabled,
    _is_trusted,
    _permissions_label,
    _plugin_id,
    _plugin_name,
    _plugin_path,
    _plugin_version,
    _resource_label,
    _status_badge,
    _status_text,
    _trust_text,
    _type_label,
)


def _ops():
    from . import ops

    return ops


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
        lambda checked=False, n=name: _ops()._install_one(window, n),
        success=True,
    )
    reinstall_btn = window._btn(
        "重装",
        lambda checked=False, n=name: _ops()._install_one(window, n, force=True),
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

    if not trusted:
        trust_note = QLabel("此插件尚未被信任，启用前请确认其来源。")
        trust_note.setObjectName("subtitle")
        trust_note.setWordWrap(True)
        card.content.addWidget(trust_note)

    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)
    btn_row.addStretch(1)
    rescan_btn = window._btn(
        "重新扫描",
        lambda checked=False, pid=plugin_id: _ops()._rescan_plugin(window, pid),
        secondary=True,
    )
    toggle_text = "禁用" if enabled else "启用"
    toggle_btn = window._btn(
        toggle_text,
        lambda checked=False, pid=plugin_id, target=not enabled: _ops()._set_plugin_enabled(window, pid, target),
        success=not enabled,
        secondary=enabled,
    )
    action_buttons = [rescan_btn, toggle_btn]
    rollback_btn = None
    if available_versions:
        rollback_btn = window._btn(
            "回滚",
            lambda checked=False, pid=plugin_id, label=name, versions=available_versions: _ops()._rollback_plugin(
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
            lambda checked=False, pid=plugin_id, label=name: _ops()._trust_plugin(window, pid, label),
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
        lambda checked=False, pid=plugin_id, label=name: _ops()._remove_plugin(window, pid, label),
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
