"""可搜索、可勾选的上游模型列表。勾选状态在过滤后仍保留。"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ... import core
from ...remote_models import filter_remote_models, model_id


class ModelCapabilityBar(QFrame):
    """一键配置上下文与能力：默认 1M、只开思考、不含图片。"""

    applied = Signal(int)

    def __init__(self, parent=None, *, apply_label: str = "一键应用到已选") -> None:
        super().__init__(parent)
        self.setObjectName("modelCapabilityBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        label = QLabel("能力")
        label.setObjectName("muted")
        row.addWidget(label)

        self.context_combo = QComboBox()
        self.context_combo.setMinimumWidth(88)
        self.context_combo.setToolTip("写入 models.json 的 contextWindow；上游目录通常不带此字段。")
        default_index = 0
        for index, (label_text, value) in enumerate(core.CONTEXT_WINDOW_PRESETS):
            self.context_combo.addItem(label_text, value)
            if value == core.DEFAULT_CONTEXT_WINDOW:
                default_index = index
        self.context_combo.setCurrentIndex(default_index)
        row.addWidget(self.context_combo)

        ctx_hint = QLabel("上下文")
        ctx_hint.setObjectName("muted")
        row.addWidget(ctx_hint)

        self.think_check = QCheckBox("思考")
        self.think_check.setChecked(True)
        self.think_check.setToolTip("写入 reasoning，并补全 thinkingLevelMap。默认只开思考。")
        row.addWidget(self.think_check)

        self.image_check = QCheckBox("图片")
        self.image_check.setChecked(False)
        self.image_check.setToolTip("写入 input 是否含 image。默认不含图片。")
        row.addWidget(self.image_check)

        self.apply_btn = QPushButton(apply_label)
        self.apply_btn.setProperty("secondary", True)
        self.apply_btn.setToolTip("立刻按当前选项覆盖已选模型的上下文与能力。")
        row.addWidget(self.apply_btn)
        row.addStretch(1)

    def capability_spec(self) -> dict[str, Any]:
        value = self.context_combo.currentData()
        context_window = (
            int(value) if value is not None else core.DEFAULT_CONTEXT_WINDOW
        )
        return {
            "context_window": context_window,
            "reasoning": self.think_check.isChecked(),
            "images": self.image_check.isChecked(),
        }

    def summary_text(self) -> str:
        spec = self.capability_spec()
        ctx = self.context_combo.currentText() or "1M"
        parts = [f"{ctx} 上下文"]
        if spec["reasoning"]:
            parts.append("思考")
        if spec["images"]:
            parts.append("图片")
        if not spec["reasoning"] and not spec["images"]:
            parts.append("仅文本")
        return "、".join(parts)


class ModelCapabilityDialog(QDialog):
    """模型页批量改已保存模型的能力。"""

    def __init__(self, parent=None, count: int = 0) -> None:
        super().__init__(parent)
        self.setWindowTitle("配置模型能力")
        layout = QVBoxLayout(self)
        hint = QLabel(
            f"将覆盖选中的 {count} 个自定义 Provider 模型。"
            "默认 1M 上下文、只开思考、不含图片。内置模型不会改写。"
        )
        hint.setObjectName("subtitle")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.bar = ModelCapabilityBar(apply_label="预览选项")
        self.bar.apply_btn.setVisible(False)
        layout.addWidget(self.bar)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("应用")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def capability_spec(self) -> dict[str, Any]:
        return self.bar.capability_spec()


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

        self.capability = ModelCapabilityBar()
        self.capability.apply_btn.clicked.connect(self.apply_capabilities)
        root.addWidget(self.capability)
        cap_hint = QLabel("保存时按上面的能力写入。默认 1M 上下文、只开思考、不含图片。")
        cap_hint.setObjectName("subtitle")
        cap_hint.setWordWrap(True)
        self.capability_hint = cap_hint
        root.addWidget(cap_hint)

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

    def capability_spec(self) -> dict[str, Any]:
        return self.capability.capability_spec()

    def checked_models(self) -> list[Any]:
        spec = self.capability_spec()
        wanted = self._checked
        out: list[Any] = []
        for entry in self._models:
            ident = model_id(entry)
            if ident not in wanted:
                continue
            out.append(core.apply_model_capabilities(entry, **spec))
        return out

    def apply_capabilities(self) -> int:
        spec = self.capability_spec()
        if not self._checked:
            self.capability_hint.setText("请先勾选要接入的模型，再一键应用能力。")
            self.capability.applied.emit(0)
            self.checkedChanged.emit(0)
            return 0
        updated: list[Any] = []
        count = 0
        for entry in self._models:
            ident = model_id(entry)
            if ident in self._checked:
                updated.append(core.apply_model_capabilities(entry, **spec))
                count += 1
            else:
                updated.append(entry)
        self._models = updated
        summary = self.capability.summary_text()
        self.capability_hint.setText(f"已将 {summary} 应用到 {count} 个已选模型。")
        self.capability.applied.emit(count)
        self.checkedChanged.emit(len(self._checked))
        return count

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
