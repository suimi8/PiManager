"""可搜索、可勾选的上游模型列表。勾选状态在过滤后仍保留。"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ...remote_models import filter_remote_models, model_id


class RemoteModelPicker(QFrame):
    """拉取结果的搜索 + 勾选器。保存时应只取 ``checked_models()``。"""

    checkedChanged = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("remoteModelPicker")
        self._models: list[Any] = []
        self._checked: set[str] = set()
        self._rebuilding = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(8)

        search_row = QHBoxLayout()
        search_row.setSpacing(8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("搜索模型 ID 或名称，例如 qwen、gpt、:free")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._rebuild)
        self.count_label = QLabel("尚未拉取")
        self.count_label.setObjectName("subtitle")
        search_row.addWidget(self.search, 1)
        search_row.addWidget(self.count_label)
        root.addLayout(search_row)

        self.empty = QLabel("拉取上游模型后，可在这里搜索并勾选要接入的模型。")
        self.empty.setObjectName("subtitle")
        self.empty.setWordWrap(True)
        root.addWidget(self.empty)

        self.list = QListWidget()
        self.list.setObjectName("remoteModelList")
        self.list.setSelectionMode(QAbstractItemView.NoSelection)
        self.list.setMinimumHeight(160)
        self.list.itemChanged.connect(self._on_item_changed)
        root.addWidget(self.list, 1)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        self.btn_check_visible = QPushButton("勾选当前结果")
        self.btn_check_visible.setProperty("secondary", True)
        self.btn_check_visible.clicked.connect(self.check_visible)
        self.btn_uncheck_visible = QPushButton("取消当前结果")
        self.btn_uncheck_visible.setProperty("ghost", True)
        self.btn_uncheck_visible.clicked.connect(self.uncheck_visible)
        self.btn_clear = QPushButton("清空已选")
        self.btn_clear.setProperty("ghost", True)
        self.btn_clear.clicked.connect(self.clear_checked)
        actions.addWidget(self.btn_check_visible)
        actions.addWidget(self.btn_uncheck_visible)
        actions.addWidget(self.btn_clear)
        actions.addStretch(1)
        root.addLayout(actions)
        self._rebuild()

    def set_models(
        self,
        models: list[Any],
        *,
        checked_ids: set[str] | None = None,
    ) -> None:
        self._models = list(models or [])
        if checked_ids is None:
            self._checked = set()
        else:
            valid = {model_id(entry) for entry in self._models}
            self._checked = {str(item).strip() for item in checked_ids if str(item).strip()}
            self._checked &= valid
        self._rebuild()
        self.checkedChanged.emit(len(self._checked))

    def model_count(self) -> int:
        return len(self._models)

    def checked_ids(self) -> set[str]:
        return set(self._checked)

    def checked_models(self) -> list[Any]:
        wanted = self._checked
        return [entry for entry in self._models if model_id(entry) in wanted]

    def visible_models(self) -> list[Any]:
        return filter_remote_models(self._models, self.search.text())

    def check_visible(self) -> None:
        for entry in self.visible_models():
            ident = model_id(entry)
            if ident:
                self._checked.add(ident)
        self._rebuild()
        self.checkedChanged.emit(len(self._checked))

    def uncheck_visible(self) -> None:
        for entry in self.visible_models():
            self._checked.discard(model_id(entry))
        self._rebuild()
        self.checkedChanged.emit(len(self._checked))

    def clear_checked(self) -> None:
        self._checked.clear()
        self._rebuild()
        self.checkedChanged.emit(0)

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._rebuilding:
            return
        ident = str(item.data(Qt.UserRole) or item.text() or "").strip()
        if not ident:
            return
        if item.checkState() == Qt.Checked:
            self._checked.add(ident)
        else:
            self._checked.discard(ident)
        self._refresh_count()
        self.checkedChanged.emit(len(self._checked))

    def _rebuild(self) -> None:
        visible = self.visible_models()
        self._rebuilding = True
        self.list.blockSignals(True)
        self.list.clear()
        for entry in visible:
            ident = model_id(entry)
            if not ident:
                continue
            item = QListWidgetItem(ident)
            item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsUserCheckable | Qt.ItemIsSelectable
            )
            item.setData(Qt.UserRole, ident)
            item.setCheckState(Qt.Checked if ident in self._checked else Qt.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._rebuilding = False
        has_rows = self.list.count() > 0
        self.list.setVisible(bool(self._models) and has_rows)
        self.empty.setVisible(not self._models or not has_rows)
        if self._models and not has_rows:
            self.empty.setText("没有匹配的模型，试试更短的关键字。")
        elif not self._models:
            self.empty.setText("拉取上游模型后，可在这里搜索并勾选要接入的模型。")
        self._refresh_count()

    def _refresh_count(self) -> None:
        total = len(self._models)
        if total == 0:
            self.count_label.setText("尚未拉取")
            return
        self.count_label.setText(
            f"显示 {self.list.count()} / 共 {total} · 已选 {len(self._checked)}"
        )
