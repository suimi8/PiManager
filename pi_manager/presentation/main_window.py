"""现代主窗口：导航壳 + 页面组装。

命令与配置行为留在 ``MainWindow`` 及其 page mixin；本模块只组装展示层 mixin。
"""
from __future__ import annotations

from .shell import MainWindow
from .window_chrome import WindowChromeMixin
from .window_overlays import ViewOverlayMixin
from .window_updates import UpdateChromeMixin


class ModernMainWindow(ViewOverlayMixin, UpdateChromeMixin, WindowChromeMixin, MainWindow):
    """导航壳与页面组装，覆盖 MainWindow 的展示适配。"""
