"""插件页操作：扫描、安装、导入与 manager 变更。"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QVBoxLayout,
)

from .... import builtin_plugins
from ...workers import Worker
from . import cards
from .format import (
    _as_mapping,
    _first,
    _is_builtin,
    _plugin_id,
    _plugin_items,
    _plugin_manager,
    _plugin_name,
    _preview_lines,
    _preview_record,
    _result_error,
    _set_label_error,
    _status_text,
)


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
        card = cards._build_plugin_card(window, row)
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
    """登记 Worker：统一并入 WorkerTrackerMixin 的唯一登记表。

    历史上这里维护过独立的 ``window._active_workers`` 列表，而
    ``_shutdown_background_tasks`` 只遍历 ``window._workers``，导致插件安装 /
    导入 / 卸载 / npm install 的 QThread 退出时从不被中断或 join —— 运行态析构
    触发 qFatal，npm install 与插件注册表写入可能被截断。现在只登记一次。
    """
    tracker = getattr(window, "_track", None)
    if callable(tracker):
        tracker(worker)
        return
    # 兜底：window 未使用 WorkerTrackerMixin（嵌入/测试桩）时保持旧行为，
    # 但仍复用同名列表，便于外部统一收割。
    workers = getattr(window, "_workers", None)
    if workers is None:
        workers = window._workers = []
    workers.append(worker)
    worker.finished.connect(lambda w=worker: _untrack_worker(window, w))
    worker.finished.connect(worker.deleteLater)

def _untrack_worker(window, worker) -> None:
    workers = getattr(window, "_workers", None) or []
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

    def _finish_import(inspection) -> None:
        """确认流程：在 Worker 槽之外的下一轮事件循环里执行。

        ``_confirm_plugin_import`` 内部是 ``dialog.exec()``，会启动嵌套事件
        循环；直接在 ``done`` 槽里调用，期间其他插件 Worker 的 done 槽会被
        投递并执行 —— 包括 ``_refresh`` 的 ``_render_plugin_rows`` 把全部卡片
        ``deleteLater()`` 并替换 ``window._plugin_cards``。用户看到的预览与随后
        写入的状态可能已不是同一份数据。
        """
        trust = _confirm_plugin_import(window, source, inspection)
        if trust is None:
            window.plugins_global_status.setText("已取消插件导入。")
            window._plugin_importing = False
            window.plugins_add_btn.setEnabled(True)
            _re_enable_btns(window)
            return
        _import_plugin(window, source, trust=bool(trust))

    def on_done(inspection):
        error_text = _result_error(inspection)
        if error_text:
            window.plugins_global_status.setText(f"插件检查失败：{error_text}")
            window._plugin_importing = False
            window.plugins_add_btn.setEnabled(True)
            QTimer.singleShot(
                0, lambda: QMessageBox.warning(window, "插件检查失败", error_text)
            )
            return
        preview = _preview_record(inspection)
        if not preview:
            window._plugin_importing = False
            window.plugins_add_btn.setEnabled(True)
            QTimer.singleShot(
                0,
                lambda: QMessageBox.warning(
                    window, "插件检查失败", "后端没有返回可预览的插件元数据，已停止导入。"
                ),
            )
            return
        # _plugin_importing 保持 True，_re_enable_btns 才能正确识别忙碌态，
        # 避免确认框还开着时按钮被其他回调提前放开。
        QTimer.singleShot(0, lambda: _finish_import(inspection))

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
