"""Window and dialog size helpers for small screens and scaled displays."""
from __future__ import annotations

from PySide6.QtWidgets import QApplication, QWidget

MIN_WINDOW_WIDTH = 800
MIN_WINDOW_HEIGHT = 600
COMPACT_WINDOW_WIDTH = 1024


def clamp_dialog_to_screen(
    widget: QWidget,
    width: int,
    height: int,
    *,
    margin: int = 48,
) -> None:
    """Resize a dialog so it stays inside the available screen area.

    800×600 @125% 时向导/Provider 对话框的 preferred 尺寸会超出可用区域，
    底部按钮会被推到屏幕外。
    """
    screen = widget.screen() if hasattr(widget, "screen") else None
    if screen is None:
        app = QApplication.instance()
        screen = app.primaryScreen() if app is not None else None
    if screen is None:
        widget.resize(width, height)
        return
    avail = screen.availableGeometry()
    max_w = max(360, avail.width() - margin)
    max_h = max(320, avail.height() - margin)
    widget.resize(min(int(width), max_w), min(int(height), max_h))
