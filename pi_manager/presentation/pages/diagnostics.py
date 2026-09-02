"""健康 / 历史 / 工具三页的兼容组合层。

实现已按导航页拆到 ``health`` / ``history`` / ``tools``。
``MainWindow`` 继续挂本组合 mixin，避免一次改三处 MRO。
"""
from __future__ import annotations

from .health import HealthPageMixin as HealthPageMixin
from .health import build_health_page as build_health_page
from .history import HistoryPageMixin as HistoryPageMixin
from .history import build_history_page as build_history_page
from .tools import ToolsPageMixin as ToolsPageMixin
from .tools import build_tools_page as build_tools_page


class DiagnosticsPageMixin(HealthPageMixin, HistoryPageMixin, ToolsPageMixin):
    """三页 mixin 组合，保持历史导入面。"""
