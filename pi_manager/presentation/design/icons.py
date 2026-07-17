"""Local SVG icon loader with theme-aware recoloring."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

from ... import resources

_ICON_PLACEHOLDER = "#7C8799"


@lru_cache(maxsize=256)
def icon(name: str, color: str = "#7C8799", size: int = 18) -> QIcon:
    path = resources.asset_path("icons", f"{name}.svg")
    if path is None:
        return QIcon()
    try:
        svg = path.read_text(encoding="utf-8").replace(_ICON_PLACEHOLDER, color)
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        if not renderer.isValid():
            return QIcon()
        pixmap = QPixmap(QSize(size, size))
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()
        return QIcon(pixmap)
    except Exception:
        return QIcon()


def clear_icon_cache() -> None:
    icon.cache_clear()
