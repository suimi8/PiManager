"""Reusable native Qt components for the modern Pi Manager UI."""
from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
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
            self.clicked.connect(callback)

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
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        self.value_label = QLabel(value)
        self.value_label.setObjectName("metricValue")
        self.label_label = QLabel(label)
        self.label_label.setObjectName("metricLabel")
        layout.addWidget(self.value_label)
        layout.addWidget(self.label_label)


class PageHeader(QFrame):
    """Shared page title and action host used by the main window shell."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("topBar")
        root = QHBoxLayout(self)
        root.setContentsMargins(26, 18, 26, 16)
        root.setSpacing(18)
        title_box = QVBoxLayout()
        title_box.setSpacing(3)
        self.eyebrow = QLabel("PI MANAGER")
        self.eyebrow.setObjectName("pageEyebrow")
        self.title = QLabel()
        self.title.setObjectName("pageTitle")
        self.description = QLabel()
        self.description.setObjectName("pageDescription")
        self.description.setWordWrap(True)
        title_box.addWidget(self.eyebrow)
        title_box.addWidget(self.title)
        title_box.addWidget(self.description)
        root.addLayout(title_box, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        root.addLayout(self.actions)

    def set_page(self, title: str, description: str) -> None:
        self.title.setText(title)
        self.description.setText(description)


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
