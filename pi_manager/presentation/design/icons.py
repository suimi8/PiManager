"""Local SVG icon loader with theme-aware recoloring."""
from __future__ import annotations

from functools import lru_cache

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

from ... import resources

_ICON_PLACEHOLDER = "#7C8799"


def _device_pixel_ratio() -> float:
    """当前设备像素比，用于按物理像素渲染 SVG。

    ``QApplication.setHighDpiScaleFactorRoundingPolicy(PassThrough)``（ui.py）
    下 DPR 可能是 1.25/1.5/1.75 等非整数：以 18×18 物理像素渲染再由 Qt 放大，
    图标就会发虚 —— 源文件本来就是矢量 SVG，没有理由损失清晰度。
    """
    app = QApplication.instance()
    if app is None:
        return 1.0
    try:
        dpr = float(app.devicePixelRatio())
    except Exception:
        return 1.0
    # 量化到 0.25 步长：避免多显示器间浮点抖动把缓存打散
    dpr = round(max(dpr, 1.0) * 4) / 4
    return min(dpr, 4.0)


@lru_cache(maxsize=512)
def _render_icon(name: str, color: str, size: int, dpr: float) -> QIcon:
    path = resources.asset_path("icons", f"{name}.svg")
    if path is None:
        return QIcon()
    try:
        svg = path.read_text(encoding="utf-8").replace(_ICON_PLACEHOLDER, color)
        renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        if not renderer.isValid():
            return QIcon()
        physical = max(1, round(size * dpr))
        pixmap = QPixmap(QSize(physical, physical))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        # 必须显式指定逻辑边界：render() 不带 bounds 时按画家 viewport 的
        # **设备像素**（physical×physical）铺满，而 pixmap 设了 DPR 后逻辑
        # 画布只有 size×size —— 图标会被画成 dpr 倍大，右侧/底部被裁掉
        # （DPR>1 时侧栏图标显示不完整）。显式 bounds 让 SVG 精确缩放到
        # size×size 逻辑像素（= 整张物理画布），矢量渲染保持清晰。
        renderer.render(painter, QRectF(0, 0, size, size))
        painter.end()
        return QIcon(pixmap)
    except Exception:
        return QIcon()


def icon(name: str, color: str = "#7C8799", size: int = 18) -> QIcon:
    # DPR 进入缓存 key：多显示器不同缩放时必须分别缓存。
    return _render_icon(name, color, size, _device_pixel_ratio())


def clear_icon_cache() -> None:
    _render_icon.cache_clear()
