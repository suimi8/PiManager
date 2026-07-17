"""Single application-wide Qt theme entry point."""
from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QWidget

from .stylesheet import build_stylesheet
from .tokens import normalize_accent, normalize_mode, tokens_for


def build_palette(mode: str = "night", accent: str = "blue") -> QPalette:
    """Build a complete active/inactive/disabled palette from design tokens."""
    colors = tokens_for(mode, accent)
    palette = QPalette()
    roles = {
        QPalette.Window: colors.window,
        QPalette.WindowText: colors.text,
        QPalette.Base: colors.input,
        QPalette.AlternateBase: colors.surface_raised,
        QPalette.ToolTipBase: colors.surface_raised,
        QPalette.ToolTipText: colors.text,
        QPalette.Text: colors.text,
        QPalette.Button: colors.surface,
        QPalette.ButtonText: colors.text,
        QPalette.BrightText: colors.danger,
        QPalette.Light: colors.surface_raised,
        QPalette.Midlight: colors.surface_hover,
        QPalette.Dark: colors.border_strong,
        QPalette.Mid: colors.border,
        QPalette.Shadow: colors.shadow,
        QPalette.Highlight: colors.accent,
        QPalette.HighlightedText: colors.selection_text,
        QPalette.Link: colors.info,
        QPalette.LinkVisited: colors.accent_hover,
        QPalette.PlaceholderText: colors.text_muted,
    }
    for role, value in roles.items():
        palette.setColor(QPalette.Active, role, QColor(value))
        palette.setColor(QPalette.Inactive, role, QColor(value))

    disabled_roles = {
        QPalette.Window: colors.window,
        QPalette.WindowText: colors.text_muted,
        QPalette.Base: colors.surface,
        QPalette.AlternateBase: colors.surface_raised,
        QPalette.ToolTipBase: colors.surface_raised,
        QPalette.ToolTipText: colors.text_muted,
        QPalette.Text: colors.text_muted,
        QPalette.Button: colors.surface,
        QPalette.ButtonText: colors.text_muted,
        QPalette.BrightText: colors.danger,
        QPalette.Light: colors.surface_raised,
        QPalette.Midlight: colors.surface_hover,
        QPalette.Dark: colors.border,
        QPalette.Mid: colors.border,
        QPalette.Shadow: colors.shadow,
        QPalette.Highlight: colors.border_strong,
        QPalette.HighlightedText: colors.text_muted,
        QPalette.Link: colors.text_muted,
        QPalette.LinkVisited: colors.text_muted,
        QPalette.PlaceholderText: colors.text_muted,
    }
    for role, value in disabled_roles.items():
        palette.setColor(QPalette.Disabled, role, QColor(value))
    return palette


def repolish_application(app: QApplication) -> None:
    """Refresh every existing widget, including already-open dialogs and menus."""
    widgets = list(app.allWidgets())
    for widget in widgets:
        try:
            style = widget.style()
            style.unpolish(widget)
            style.polish(widget)
        except RuntimeError:
            continue
    # A later parent/child polish can re-resolve a palette that was assigned
    # earlier in the loop. Apply the global palette in a separate final pass.
    for widget in widgets:
        try:
            widget.setPalette(app.palette())
            QWidget.update(widget)
        except RuntimeError:
            continue
    for window in app.topLevelWidgets():
        if isinstance(window, QWidget):
            QWidget.update(window)


def apply_application_theme(
    app: QApplication,
    mode: str = "night",
    accent: str = "blue",
) -> tuple[str, str]:
    """Apply one palette and stylesheet to the whole application."""
    mode_name = normalize_mode(mode)
    accent_name = normalize_accent(accent)
    app.setProperty("uiMode", mode_name)
    app.setProperty("uiAccent", accent_name)
    app.setPalette(build_palette(mode_name, accent_name))
    app.setStyleSheet(build_stylesheet(mode_name, accent_name))
    repolish_application(app)
    return mode_name, accent_name
