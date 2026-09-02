"""Modern session browser page."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ... import core
from ... import extras
from ..components import EmptyState, SurfaceCard


def build_sessions_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    filter_card = SurfaceCard(margins=(14, 12, 14, 12), spacing=8)
    filters = QHBoxLayout()
    filters.setSpacing(8)
    window.session_filter_wd = QLineEdit()
    window.session_filter_wd.setPlaceholderText("筛选项目或工作目录…")
    window.session_filter_name = QLineEdit()
    window.session_filter_name.setPlaceholderText("筛选模型、预览或文件名…")
    # 筛选防抖：sessions_apply_filter 只对 refresh_sessions 缓存做内存过滤，
    # 但仍要重建整表；按每次击键触发在数百会话时可感知。
    window._session_filter_debounce = QTimer(window)
    window._session_filter_debounce.setSingleShot(True)
    window._session_filter_debounce.setInterval(180)
    window._session_filter_debounce.timeout.connect(window.sessions_apply_filter)
    window.session_filter_wd.textChanged.connect(
        lambda _text: window._session_filter_debounce.start()
    )
    window.session_filter_name.textChanged.connect(
        lambda _text: window._session_filter_debounce.start()
    )
    filters.addWidget(window.session_filter_wd, 1)
    filters.addWidget(window.session_filter_name, 1)
    filters.addWidget(window._btn("刷新", window.refresh_sessions, secondary=True))
    filter_card.content.addLayout(filters)
    filter_tip = QLabel("会话记录仅用于恢复上下文；项目目录和模型信息从本地会话文件解析。")
    filter_tip.setObjectName("subtitle")
    filter_card.content.addWidget(filter_tip)
    layout.addWidget(filter_card)

    table_card = SurfaceCard(margins=(0, 0, 0, 12), spacing=10)
    window.sessions_table = QTableWidget(0, 5)
    window.sessions_table.setHorizontalHeaderLabels(["项目", "工作目录", "模型", "时间", "首条预览"])
    header = window.sessions_table.horizontalHeader()
    header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(1, QHeaderView.Stretch)
    header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(3, QHeaderView.ResizeToContents)
    header.setSectionResizeMode(4, QHeaderView.Stretch)
    window._polish_table(window.sessions_table)
    window.sessions_empty = EmptyState(
        "还没有会话记录",
        "启动完整 Pi 或在快速提问中对话后，会话会出现在这里。",
    )
    window.sessions_empty.add_action(
        window._btn("去概览启动", lambda: window._goto_page("simple"), success=True)
    )
    window.sessions_empty.add_action(
        window._btn("打开快速提问", lambda: window._goto_page("chat"), secondary=True)
    )
    window.sessions_empty.setVisible(False)
    table_card.content.addWidget(window.sessions_table, 1)
    table_card.content.addWidget(window.sessions_empty, 1)
    actions = QHBoxLayout()
    actions.setContentsMargins(12, 0, 12, 0)
    actions.setSpacing(8)
    actions.addWidget(window._btn("继续会话", window.session_continue, success=True))
    actions.addWidget(window._btn("打开项目目录", window.session_open_project, secondary=True))
    actions.addWidget(window._btn("在资源管理器显示", window.session_reveal, secondary=True))
    actions.addWidget(window._btn("重命名", window.session_rename, ghost=True))
    actions.addStretch(1)
    actions.addWidget(window._btn("删除选中", window.session_delete, danger=True))
    actions.addWidget(window._btn("批量删除", window.session_delete_batch, danger=True))
    table_card.content.addLayout(actions)
    layout.addWidget(table_card, 1)
    return page


class SessionsPageMixin:
    """会话页行为：列表、筛选、继续与删除。从 ``ui.py`` / ``ui_features.py`` 下沉。"""

    def refresh_sessions(self):
        # 唯一的磁盘遍历入口：会话目录遍历 + 会话文件解析只在这里发生，
        # 结果缓存供 sessions_apply_filter 做内存过滤（见其 docstring）。
        self._sessions_cache = list(core.list_sessions(limit=200))
        if hasattr(self, "session_filter_wd"):
            self.sessions_apply_filter()
            return
        self._fill_sessions_table(self._sessions_cache)

    def _filter_sessions_cache(
        self, workdir_substr: str = "", name_substr: str = "", *, limit: int = 100
    ) -> list[dict[str, str]]:
        """对 refresh_sessions() 的缓存做内存过滤（语义同 extras.list_sessions_filtered）。"""
        rows = getattr(self, "_sessions_cache", None)
        if rows is None:
            rows = self._sessions_cache = list(core.list_sessions(limit=200))
        wd = (workdir_substr or "").lower().strip()
        nm = (name_substr or "").lower().strip()
        out: list[dict[str, str]] = []
        for r in rows:
            if wd or nm:
                blob = " ".join(
                    str(r.get(k) or "")
                    for k in ("path", "folder", "name", "cwd", "project", "model", "preview")
                ).lower()
                if wd and wd not in blob:
                    continue
                if nm and nm not in blob:
                    continue
            out.append(r)
            if len(out) >= limit:
                break
        return out

    def _session_path_at(self, row: int) -> str | None:
        item = self.sessions_table.item(row, 0)
        if not item:
            # 兼容旧 3 列布局：路径在第 2 列
            legacy = self.sessions_table.item(row, 2)
            return legacy.text() if legacy else None
        data = item.data(Qt.UserRole)
        if data:
            return str(data)
        legacy = self.sessions_table.item(row, 2)
        return legacy.text() if legacy else None

    def _session_cwd_at(self, row: int) -> str | None:
        item = self.sessions_table.item(row, 0)
        if item:
            cwd = item.data(Qt.UserRole + 1)
            if cwd:
                return str(cwd)
        # 工作目录列
        wd = self.sessions_table.item(row, 1)
        return wd.text() if wd and wd.text() else None

    def _fill_sessions_table(self, rows: list[dict[str, str]]) -> None:
        self.sessions_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            project = r.get("project") or core.project_name_from_path(r.get("cwd") or r.get("folder") or "")
            cwd = r.get("cwd") or r.get("folder") or ""
            model = r.get("model") or "—"
            when = r.get("started") or r.get("mtime_text") or ""
            preview = r.get("preview") or ""
            path = r.get("path") or ""

            proj_item = QTableWidgetItem(project)
            proj_item.setData(Qt.UserRole, path)
            proj_item.setData(Qt.UserRole + 1, cwd)
            tip = f"项目: {project}\n目录: {cwd}\n文件: {path}"
            if r.get("session_id"):
                tip += f"\nID: {r.get('session_id')}"
            proj_item.setToolTip(tip)
            self.sessions_table.setItem(i, 0, proj_item)

            cwd_item = QTableWidgetItem(cwd)
            cwd_item.setToolTip(cwd)
            self.sessions_table.setItem(i, 1, cwd_item)

            model_item = QTableWidgetItem(model)
            model_item.setToolTip(model)
            self.sessions_table.setItem(i, 2, model_item)

            time_item = QTableWidgetItem(when)
            time_item.setToolTip(when)
            self.sessions_table.setItem(i, 3, time_item)

            prev_item = QTableWidgetItem(preview or r.get("name") or "")
            prev_item.setToolTip(preview or r.get("name") or path)
            self.sessions_table.setItem(i, 4, prev_item)
        empty = getattr(self, "sessions_empty", None)
        if empty is not None:
            has_rows = bool(rows)
            self.sessions_table.setVisible(has_rows)
            empty.setVisible(not has_rows)
            if not has_rows:
                filtered = bool(
                    (getattr(self, "session_filter_wd", None) and self.session_filter_wd.text().strip())
                    or (
                        getattr(self, "session_filter_name", None)
                        and self.session_filter_name.text().strip()
                    )
                )
                if filtered:
                    empty.set_copy(
                        "没有匹配的会话",
                        "当前筛选条件下没有会话。可以清空筛选后再试。",
                    )
                else:
                    empty.set_copy(
                        "还没有会话记录",
                        "启动完整 Pi 或在快速提问中对话后，会话会出现在这里。",
                    )

    def sessions_apply_filter(self):
        """按筛选词重建会话表，只对缓存做内存过滤。

        ``extras.list_sessions_filtered`` 的 ``limit`` 只裁剪结果，内部的
        ``core.list_sessions(limit=200)`` 目录遍历 + 会话文件解析与筛选词无关，
        因此原来每次击键都完整重跑一次磁盘 IO（数百会话时每字符数十~数百 ms
        阻塞主线程）。现在磁盘遍历只发生在 ``refresh_sessions()``。
        """
        wd = self.session_filter_wd.text().strip() if hasattr(self, "session_filter_wd") else ""
        nm = self.session_filter_name.text().strip() if hasattr(self, "session_filter_name") else ""
        rows = self._filter_sessions_cache(wd, nm, limit=100)
        if hasattr(self, "_fill_sessions_table"):
            self._fill_sessions_table(rows)
            return
        self.sessions_table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            self.sessions_table.setItem(i, 0, QTableWidgetItem(r.get("project") or r.get("name") or ""))
            self.sessions_table.setItem(i, 1, QTableWidgetItem(r.get("cwd") or r.get("folder") or ""))
            self.sessions_table.setItem(i, 2, QTableWidgetItem(r.get("model") or r.get("path") or ""))

    def session_selected_path(self) -> str | None:
        sm = self.sessions_table.selectionModel()
        if not sm:
            return None
        rows = sm.selectedRows()
        if not rows:
            return None
        r = rows[0].row()
        if hasattr(self, "_session_path_at"):
            return self._session_path_at(r)
        item = self.sessions_table.item(r, 0)
        if item and item.data(Qt.UserRole):
            return str(item.data(Qt.UserRole))
        legacy = self.sessions_table.item(r, 2)
        return legacy.text() if legacy else None

    def session_reveal(self):
        sm = self.sessions_table.selectionModel()
        if not sm:
            return
        rows = sm.selectedRows()
        if not rows:
            return
        path = self._session_path_at(rows[0].row())
        if path:
            core.open_in_explorer(path)

    def session_open_project(self):
        sm = self.sessions_table.selectionModel()
        if not sm:
            return
        rows = sm.selectedRows()
        if not rows:
            self.notify_warning("请先选择会话")
            return
        cwd = self._session_cwd_at(rows[0].row())
        if not cwd:
            self.notify_warning("无法解析该会话的项目目录")
            return
        p = Path(cwd)
        if not p.exists():
            QMessageBox.warning(self, "目录不存在", f"项目目录不存在：\n{cwd}")
            return
        core.open_path(str(p))

    def session_continue(self):
        sm = self.sessions_table.selectionModel()
        if not sm:
            return
        rows = sm.selectedRows()
        if not rows:
            return
        path = self._session_path_at(rows[0].row())
        if not path:
            return
        cwd = self._session_cwd_at(rows[0].row()) or self.workdir_edit.text().strip() or str(core.user_home())
        self.persist_mgr()
        try:
            cmd = core.launch_pi_interactive(
                cwd,
                terminal=str(self.terminal_combo.currentData() or self.terminal_combo.currentText() or "auto"),
                extra=["--session", path],
            )
            self.status.showMessage(f"继续会话: {cmd}")
        except Exception as e:
            QMessageBox.critical(self, "启动失败", str(e))

    def session_rename(self):
        path = self.session_selected_path()
        if not path:
            self.notify_warning("请先选择会话")
            return
        name, ok = QInputDialog.getText(self, "重命名", "新文件名：", text=Path(path).name)
        if not ok or not name.strip():
            return
        try:
            newp = extras.session_rename(path, name.strip())
            self.refresh_sessions()
            self.status.showMessage(f"已重命名为 {newp}")
        except Exception as e:
            QMessageBox.warning(self, "重命名失败", str(e))

    def session_delete(self):
        path = self.session_selected_path()
        if not path:
            self.notify_warning("请先选择会话")
            return
        if QMessageBox.question(
            self,
            "删除会话",
            f"将永久删除这个本地会话文件，无法从 Pi Manager 恢复：\n\n{path}\n\n"
            "不会影响 Provider、模型或密钥配置。确定删除？",
        ) != QMessageBox.Yes:
            return
        if extras.session_delete(path):
            self.refresh_sessions()
            self.status.showMessage("会话已删除")
        else:
            QMessageBox.warning(self, "失败", "无法删除")

    def session_delete_batch(self):
        sm = self.sessions_table.selectionModel()
        if not sm:
            return
        paths = []
        for idx in sm.selectedRows():
            path = self._session_path_at(idx.row())
            if path:
                paths.append(path)
        if not paths:
            self.notify_warning("请多选要删除的会话")
            return
        if QMessageBox.question(
            self,
            "批量删除会话",
            f"将永久删除选中的 {len(paths)} 个本地会话文件，无法从 Pi Manager 恢复。\n\n"
            "不会影响 Provider、模型或密钥配置。确定删除？",
        ) != QMessageBox.Yes:
            return
        ok = 0
        for p in paths:
            if extras.session_delete(p):
                ok += 1
        self.refresh_sessions()
        self.status.showMessage(f"已删除 {ok}/{len(paths)} 个会话")
