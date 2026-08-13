# -*- coding: utf-8 -*-
"""内置插件管理页：列出随 PiManager 分发的 skills / extensions，一键安装或查看状态。"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ... import builtin_plugins
from ...ui import Worker
from ..components import SectionHeading, StatusBadge, SurfaceCard


def _badge_for_status(status: dict) -> StatusBadge:
    if status.get("ready"):
        return StatusBadge("已就绪", "success")
    if status.get("on_disk") and status.get("needs_npm_install") and not status.get("npm_installed"):
        return StatusBadge("待 npm install", "warning")
    if status.get("on_disk"):
        return StatusBadge("已落盘", "info")
    return StatusBadge("未安装", "neutral")


def _type_label(t: str) -> str:
    return {"skill": "Skill", "extension": "Extension"}.get(t, t)


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

    # 顶部说明 + 全部安装按钮
    header = SurfaceCard(margins=(17, 15, 17, 15), spacing=10)
    header_row = QHBoxLayout()
    header_row.addWidget(
        SectionHeading(
            "内置插件",
            "随 PiManager 分发的 skills / extensions，一键安装到 ~/.pi/agent/，pi 启动时自动加载。",
        ),
        1,
    )
    window.plugins_install_all_btn = window._btn("全部安装", lambda: _install_all(window), success=True)
    window.plugins_refresh_btn = window._btn("刷新状态", lambda: _refresh(window), secondary=True)
    header_row.addWidget(window.plugins_refresh_btn, 0, Qt.AlignTop)
    header_row.addWidget(window.plugins_install_all_btn, 0, Qt.AlignTop)
    header.content.addLayout(header_row)
    window.plugins_global_status = QLabel("加载中…")
    window.plugins_global_status.setObjectName("subtitle")
    window.plugins_global_status.setWordWrap(True)
    header.content.addWidget(window.plugins_global_status)
    layout.addWidget(header)

    # 插件列表容器（刷新时动态重建）
    window.plugins_list_container = QVBoxLayout()
    window.plugins_list_container.setSpacing(10)
    layout.addLayout(window.plugins_list_container)
    layout.addStretch(1)

    scroll.setWidget(body)
    outer.addWidget(scroll)

    # 初始加载
    _refresh(window)
    return page


def _clear_list(window) -> None:
    while window.plugins_list_container.count():
        item = window.plugins_list_container.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()


def _refresh(window) -> None:
    """刷新插件列表状态（UI 线程内同步，数据量小）。"""
    if getattr(window, "_refreshing", False):
        return
    window._refreshing = True
    try:
        try:
            statuses = builtin_plugins.all_statuses()
        except Exception as exc:
            window.plugins_global_status.setText(f"读取清单失败：{exc}")
            return
        _clear_list(window)
        window._plugin_cards = {}
        installed = 0
        total = len(statuses)
        for status in statuses:
            card = _build_plugin_card(window, status)
            window.plugins_list_container.addWidget(card)
            window._plugin_cards[status.get("name", "")] = card
            if status.get("ready"):
                installed += 1
        window.plugins_global_status.setText(
            f"共 {total} 个内置插件，{installed} 个已就绪 · 安装目标：~/.pi/agent/"
        )
    finally:
        window._refreshing = False


def _build_plugin_card(window, status: dict) -> QWidget:
    name = status.get("name", "")
    ptype = status.get("type", "")
    desc = status.get("description", "")
    target = status.get("target", "")

    card = SurfaceCard(margins=(15, 13, 15, 13), spacing=8)
    # 标题行
    title_row = QHBoxLayout()
    title_row.setSpacing(8)
    title = QLabel(f"{name}")
    title.setObjectName("cardTitle")
    title_row.addWidget(title)
    type_badge = StatusBadge(_type_label(ptype), "info")
    title_row.addWidget(type_badge)
    title_row.addWidget(_badge_for_status(status))
    title_row.addStretch(1)
    card.content.addLayout(title_row)

    # 描述
    desc_lbl = QLabel(desc)
    desc_lbl.setObjectName("subtitle")
    desc_lbl.setWordWrap(True)
    card.content.addWidget(desc_lbl)

    # 目标路径
    path_lbl = QLabel(f"目标：{target}")
    path_lbl.setObjectName("statusBadge")
    path_lbl.setWordWrap(True)
    card.content.addWidget(path_lbl)

    # 状态详情（仅 npm 插件显示）
    if status.get("needs_npm_install"):
        if status.get("on_disk"):
            npm_text = "依赖已安装 ✓" if status.get("npm_installed") else "依赖未安装（需 npm install）"
        else:
            npm_text = "未落盘"
        npm_lbl = QLabel(f"npm 依赖：{npm_text}")
        npm_lbl.setObjectName("subtitle")
        card.content.addWidget(npm_lbl)

    # 操作按钮行
    btn_row = QHBoxLayout()
    btn_row.setSpacing(8)
    btn_row.addStretch(1)

    install_btn = window._btn("一键安装", lambda checked=False, n=name: _install_one(window, n), success=True)
    reinstall_btn = window._btn("重装", lambda checked=False, n=name: _install_one(window, n, force=True), secondary=True)
    btn_row.addWidget(reinstall_btn)
    btn_row.addWidget(install_btn)
    card.content.addLayout(btn_row)

    # 结果反馈标签
    result_lbl = QLabel("")
    result_lbl.setObjectName("subtitle")
    result_lbl.setWordWrap(True)
    card.content.addWidget(result_lbl)
    card._result_label = result_lbl
    card._install_btn = install_btn
    return card


def _set_card_result(window, name: str, text: str, *, ok: bool) -> None:
    card = getattr(window, "_plugin_cards", {}).get(name)
    if card is None:
        return
    lbl: QLabel = getattr(card, "_result_label", None)
    if lbl is None:
        return
    lbl.setText(text)
    lbl.setProperty("error", not ok)
    lbl.style().unpolish(lbl)
    lbl.style().polish(lbl)


def _track_worker(window, worker) -> None:
    """登记 Worker 并在完成时自动清理，避免 QThread 运行中被 GC 回收。"""
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
    """后台一键安装单个插件。"""
    card = getattr(window, "_plugin_cards", {}).get(name)
    if card is not None:
        getattr(card, "_result_label", QLabel("")).setText("安装中…")
        getattr(card, "_install_btn", None).setEnabled(False) if card is not None else None

    def task():
        if force:
            builtin_plugins.install_builtin(name, force=True)
        return builtin_plugins.install_one_click(name)

    worker = Worker(task)
    _track_worker(window, worker)

    def on_done(result):
        _refresh(window)
        status = result.get("status") or {}
        if result.get("ok"):
            _set_card_result(window, name, "安装成功，pi 下次启动时自动加载。", ok=True)
        else:
            cmd = result.get("command") or ""
            err = result.get("error") or "安装失败"
            msg = f"失败：{err}"
            if cmd:
                msg += f"\n请手动执行：\n{cmd}"
            _set_card_result(window, name, msg, ok=False)
        _re_enable_btns(window)

    def on_failed(err):
        _refresh(window)
        _set_card_result(window, name, f"出错：{err}", ok=False)
        _re_enable_btns(window)

    worker.done.connect(on_done)
    worker.failed.connect(on_failed)
    worker.start()


def _install_all(window) -> None:
    """后台一键安装所有内置插件（含需 npm install 的）。"""
    window.plugins_install_all_btn.setEnabled(False)
    window.plugins_global_status.setText("正在安装全部插件…")

    def task():
        # 先落盘所有插件（含 disabled，MCP 桥也要装上）
        builtin_plugins.install_all_builtins(include_disabled=True)
        # 再对每个需要 npm install 的插件执行安装
        results = []
        for status in builtin_plugins.all_statuses():
            if status.get("needs_npm_install") and status.get("on_disk") and not status.get("npm_installed"):
                results.append(builtin_plugins.npm_install(status["name"]))
        return results

    worker = Worker(task)
    _track_worker(window, worker)

    def on_done(results):
        _refresh(window)
        failed = [r for r in results if not r.get("ok") and not r.get("skipped")]
        if failed:
            cmds = "\n".join({r.get("command", "") for r in failed if r.get("command")})
            window.plugins_global_status.setText(
                f"部分插件 npm install 失败（{len(failed)}/{len(results)}）。请手动执行：\n{cmds}"
            )
        else:
            window.plugins_global_status.setText("全部安装完成，pi 下次启动时自动加载。")
        _re_enable_btns(window)

    def on_failed(err):
        _refresh(window)
        window.plugins_global_status.setText(f"安装出错：{err}")
        _re_enable_btns(window)

    worker.done.connect(on_done)
    worker.failed.connect(on_failed)
    worker.start()


def _re_enable_btns(window) -> None:
    window.plugins_install_all_btn.setEnabled(True)
    for card in getattr(window, "_plugin_cards", {}).values():
        btn = getattr(card, "_install_btn", None)
        if btn is not None:
            btn.setEnabled(True)
