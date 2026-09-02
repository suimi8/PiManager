"""Reusable native Qt components for the modern Pi Manager UI."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QTimer, Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..design.icons import icon
from ..design.tokens import tokens_for


class SurfaceCard(QFrame):
    """A design-system surface with a ready-to-use vertical layout."""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        elevated: bool = False,
        object_name: str = "surfaceCard",
        margins: tuple[int, int, int, int] = (16, 16, 16, 16),
        spacing: int = 10,
    ) -> None:
        super().__init__(parent)
        self.setObjectName(object_name)
        self.setProperty("elevated", elevated)
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(*margins)
        self.content.setSpacing(spacing)


class SectionHeading(QWidget):
    def __init__(
        self,
        title: str,
        description: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.title = QLabel(title)
        self.title.setObjectName("sectionTitle")
        layout.addWidget(self.title)
        self.description = QLabel(description)
        self.description.setObjectName("subtitle")
        self.description.setWordWrap(True)
        self.description.setVisible(bool(description))
        layout.addWidget(self.description)


class StatusBadge(QLabel):
    def __init__(
        self,
        text: str = "",
        status: str = "neutral",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(text, parent)
        self.setObjectName("statusBadge")
        self.setAlignment(Qt.AlignCenter)
        self.set_status(status)

    def set_status(self, status: str, text: str | None = None) -> None:
        self.setProperty("status", status)
        if text is not None:
            self.setText(text)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class AppButton(QPushButton):
    def __init__(
        self,
        text: str,
        callback: Callable[[], None] | None = None,
        parent: QWidget | None = None,
        *,
        icon_name: str | None = None,
        icon_color: str = "#FFFFFF",
        secondary: bool = False,
        danger: bool = False,
        success: bool = False,
        ghost: bool = False,
    ) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setProperty("secondary", secondary)
        self.setProperty("danger", danger)
        self.setProperty("success", success)
        self.setProperty("ghost", ghost)
        self.setProperty("iconName", icon_name or "")
        if icon_name:
            self.setIcon(icon(icon_name, icon_color, 17))
        if callback is not None:
            # 吞掉 QPushButton.clicked 携带的 bool checked：否则 PySide6 会把它
            # 填给接受参数的槽（open_install_dialog(status)/check_manager_update(
            # silent)/open_setup_wizard(force) 三处今天只是"碰巧对"），点按钮与
            # 直接调用的行为不一致，改默认值就会引入极难定位的缺陷。
            self.clicked.connect(lambda *_ignored: callback())

    def refresh_theme(self, mode: str, accent: str) -> None:
        icon_name = str(self.property("iconName") or "")
        if not icon_name:
            return
        colors = tokens_for(mode, accent)
        if self.property("danger") and not self.property("secondary"):
            color = colors.danger
        elif self.property("secondary") or self.property("ghost"):
            color = colors.text_muted
        else:
            color = "#FFFFFF"
        self.setIcon(icon(icon_name, color, 17))


class MetricCard(QFrame):
    def __init__(self, label: str, value: str = "—", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("metricCard")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.content = QVBoxLayout(self)
        self.content.setContentsMargins(14, 12, 14, 12)
        self.content.setSpacing(4)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        self.value_label.setWordWrap(True)
        self.label_label = QLabel(label)
        self.label_label.setObjectName("metricLabel")
        self.label_label.setWordWrap(True)
        self.content.addWidget(self.value_label)
        self.content.addWidget(self.label_label)


class PageHeader(QFrame):
    """Shared page title and action host used by the main window shell."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("topBar")
        self._root = QHBoxLayout(self)
        self._root.setContentsMargins(26, 18, 26, 16)
        self._root.setSpacing(18)
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        self.eyebrow = QLabel("PI MANAGER")
        self.eyebrow.setObjectName("pageEyebrow")
        self.title = QLabel()
        self.title.setObjectName("pageTitle")
        self.title.setWordWrap(True)
        self.description = QLabel()
        self.description.setObjectName("pageDescription")
        self.description.setWordWrap(True)
        title_box.addWidget(self.eyebrow)
        title_box.addWidget(self.title)
        title_box.addWidget(self.description)
        self._root.addLayout(title_box, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self._root.addLayout(self.actions)

    def set_page(self, title: str, description: str) -> None:
        self.title.setText(title)
        self.description.setText(description)

    def set_compact(self, compact: bool) -> None:
        compact = bool(compact)
        if compact:
            self._root.setContentsMargins(16, 10, 16, 8)
            self._root.setSpacing(10)
        else:
            self._root.setContentsMargins(26, 18, 26, 16)
            self._root.setSpacing(18)
        self.eyebrow.setVisible(not compact)


class CollapsibleSection(QFrame):
    """Card-like section whose advanced content can be folded away."""

    def __init__(
        self,
        title: str,
        description: str = "",
        parent: QWidget | None = None,
        *,
        expanded: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("collapsibleSection")
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = QToolButton()
        self.header.setObjectName("collapsibleHeader")
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.header.setToolTip(description)
        root.addWidget(self.header)
        self.description = QLabel(description)
        self.description.setObjectName("collapsibleDescription")
        self.description.setWordWrap(True)
        self.description.setContentsMargins(16, 0, 16, 10)
        root.addWidget(self.description)
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(16, 4, 16, 16)
        self.body_layout.setSpacing(10)
        root.addWidget(self.body)
        self.header.toggled.connect(self.set_expanded)
        self.set_expanded(expanded)

    def set_expanded(self, expanded: bool) -> None:
        self.header.setChecked(bool(expanded))
        self.body.setVisible(bool(expanded))
        self.description.setVisible(bool(self.description.text()))
        self.refresh_theme()

    def refresh_theme(self, mode: str | None = None, accent: str | None = None) -> None:
        if mode is None:
            color = self.palette().color(QPalette.PlaceholderText).name()
        else:
            color = tokens_for(mode, accent).text_muted
        name = "chevron-down" if self.header.isChecked() else "chevron-right"
        self.header.setIcon(icon(name, color, 17))


class PropertyTable(QWidget):
    """结构化「属性 / 值」面板，替代面向开发者的 JSON 预览。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("propertyTable")
        self._rows: list[tuple[QLabel, QLabel]] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)

    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows = []
        for key, value in rows:
            line = QHBoxLayout()
            line.setContentsMargins(0, 0, 0, 0)
            line.setSpacing(12)
            key_label = QLabel(key)
            key_label.setObjectName("propKey")
            key_label.setMinimumWidth(88)
            value_label = QLabel(value or "—")
            value_label.setObjectName("propValue")
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            line.addWidget(key_label, 0, Qt.AlignTop)
            line.addWidget(value_label, 1)
            host = QWidget()
            host.setLayout(line)
            self._layout.addWidget(host)
            self._rows.append((key_label, value_label))
        self._layout.addStretch(1)

    def value_text(self) -> str:
        return "\n".join(f"{key.text()}\t{value.text()}" for key, value in self._rows)


class EmptyState(QFrame):
    """空状态：当前情况、为什么为空、下一步操作。"""

    def __init__(
        self,
        title: str,
        reason: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("emptyState")
        root = QVBoxLayout(self)
        root.setContentsMargins(24, 28, 24, 28)
        root.setSpacing(8)
        self.title = QLabel(title)
        self.title.setObjectName("emptyTitle")
        self.title.setAlignment(Qt.AlignCenter)
        self.title.setWordWrap(True)
        self.reason = QLabel(reason)
        self.reason.setObjectName("emptyReason")
        self.reason.setAlignment(Qt.AlignCenter)
        self.reason.setWordWrap(True)
        self.reason.setVisible(bool(reason))
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        self.actions.addStretch(1)
        root.addStretch(1)
        root.addWidget(self.title)
        root.addWidget(self.reason)
        root.addLayout(self.actions)
        root.addStretch(1)

    def set_copy(self, title: str, reason: str = "") -> None:
        self.title.setText(title)
        self.reason.setText(reason)
        self.reason.setVisible(bool(reason))

    def add_action(self, button: QWidget) -> None:
        self.actions.insertWidget(self.actions.count() - 1, button)


class ErrorActionPanel(QFrame):
    """失败状态：原因摘要 + 下一步动作，而不是一枚红色 Badge。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("errorPanel")
        root = QVBoxLayout(self)
        root.setContentsMargins(12, 10, 12, 10)
        root.setSpacing(8)
        self.title = QLabel("连接失败")
        self.title.setObjectName("errorTitle")
        self.title.setWordWrap(True)
        self.reason = QLabel("")
        self.reason.setObjectName("errorReason")
        self.reason.setWordWrap(True)
        self.detail = QLabel("")
        self.detail.setObjectName("errorDetail")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setVisible(False)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        root.addWidget(self.title)
        root.addWidget(self.reason)
        root.addWidget(self.detail)
        root.addLayout(self.actions)
        self._detail_text = ""

    def set_error(self, title: str, reason: str, detail: str = "") -> None:
        self.title.setText(title)
        self.reason.setText(reason)
        self.reason.setVisible(bool(reason))
        self._detail_text = str(detail or "")
        self.detail.setText(self._detail_text)
        self.detail.setVisible(False)

    def toggle_detail(self) -> None:
        if not self._detail_text:
            return
        self.detail.setVisible(self.detail.isHidden())

    def detail_text(self) -> str:
        return self._detail_text

    def add_action(self, button: QWidget) -> None:
        self.actions.addWidget(button)

    def clear_actions(self) -> None:
        while self.actions.count():
            item = self.actions.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)


class InlineBanner(QFrame):
    """页面内警告 / 错误 / 信息条，带可选动作按钮。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("updateBanner")
        self.setAttribute(Qt.WA_StyledBackground, True)
        root = QHBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(8)
        self.message = QLabel("")
        self.message.setObjectName("bannerText")
        self.message.setWordWrap(True)
        self.action_btn = QPushButton("")
        self.action_btn.setProperty("success", True)
        self.action_btn.setCursor(Qt.PointingHandCursor)
        self.action_btn.setVisible(False)
        root.addWidget(self.message, 1)
        root.addWidget(self.action_btn)
        self._action: Callable[[], None] | None = None
        self.action_btn.clicked.connect(self._run_action)
        self.hide()

    def _run_action(self) -> None:
        if self._action is not None:
            self._action()

    def set_message(
        self,
        text: str,
        *,
        tone: str = "warning",
        action_text: str = "",
        action: Callable[[], None] | None = None,
    ) -> None:
        self.setProperty("status", tone)
        self.message.setText(text)
        self.message.setProperty("state", "danger" if tone == "danger" else "")
        self.style().unpolish(self)
        self.style().polish(self)
        self.message.style().unpolish(self.message)
        self.message.style().polish(self.message)
        self._action = action
        self.action_btn.setText(action_text)
        self.action_btn.setVisible(bool(action_text and action is not None))
        self.setVisible(bool(text))


class FeedbackToast(QFrame):
    """短暂成功 / 警告 / 错误提示，不打断当前操作。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("feedbackToast")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setMaximumWidth(380)
        root = QHBoxLayout(self)
        root.setContentsMargins(14, 10, 14, 10)
        self.message = QLabel("")
        self.message.setObjectName("toastText")
        self.message.setWordWrap(True)
        root.addWidget(self.message)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self.hide()
        if parent is not None:
            parent.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if obj is self.parent() and event.type() == QEvent.Type.Resize:
            self._reposition()
        return super().eventFilter(obj, event)

    def show_message(self, text: str, tone: str = "success", ms: int = 3500) -> None:
        self.setProperty("status", tone)
        self.message.setText(text)
        self.style().unpolish(self)
        self.style().polish(self)
        self.adjustSize()
        self.show()
        self.raise_()
        self._reposition()
        self._timer.start(max(800, int(ms)))

    def _reposition(self) -> None:
        parent = self.parentWidget()
        if parent is None or self.isHidden():
            return
        self.adjustSize()
        x = max(16, parent.width() - self.width() - 24)
        self.move(x, 16)


class ResultSheet(QFrame):
    """页内持久结果面板：多行导入摘要、测试说明等，可关闭、可滚动。"""

    _MAX_BODY = 8000

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("resultSheet")
        self.setAttribute(Qt.WA_StyledBackground, True)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 10, 16, 12)
        root.setSpacing(6)
        header = QHBoxLayout()
        header.setSpacing(8)
        self.title = QLabel("")
        self.title.setObjectName("resultSheetTitle")
        self.title.setWordWrap(True)
        header.addWidget(self.title, 1)
        self.close_btn = QPushButton("关闭")
        self.close_btn.setProperty("ghost", True)
        self.close_btn.setCursor(Qt.PointingHandCursor)
        self.close_btn.setAccessibleName("关闭结果面板")
        self.close_btn.clicked.connect(self.dismiss)
        header.addWidget(self.close_btn)
        root.addLayout(header)
        self.body = QPlainTextEdit()
        self.body.setObjectName("resultSheetBody")
        self.body.setReadOnly(True)
        self.body.setMaximumHeight(160)
        self.body.setMinimumHeight(48)
        self.body.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        root.addWidget(self.body)
        self.hide()

    def show_result(self, title: str, body: str, tone: str = "success") -> None:
        allowed = {"success", "warning", "danger", "info"}
        self.setProperty("status", tone if tone in allowed else "success")
        self.title.setText(title)
        text = body or title
        if len(text) > self._MAX_BODY:
            text = text[: self._MAX_BODY] + "\n…"
        self.body.setPlainText(text)
        self.style().unpolish(self)
        self.style().polish(self)
        self.setVisible(True)

    def dismiss(self) -> None:
        self.hide()
        self.body.clear()
