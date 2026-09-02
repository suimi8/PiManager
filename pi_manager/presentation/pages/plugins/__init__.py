"""Pi 插件管理页。

内置插件仍然使用 ``builtin_plugins`` 的原有安装流程；自定义插件通过
``plugin_manager`` 的公共 API 管理。所有可能触碰文件系统的操作都通过
``Worker`` 执行，UI 只负责选择路径、展示后端返回的元数据和确认操作。

实现拆到 ``format`` / ``cards`` / ``ops`` / ``page``；本模块只暴露页面入口，
供 ``main_window`` 惰性导入。
"""
from __future__ import annotations

from .ops import _track_worker as _track_worker
from .page import build_plugins_page as build_plugins_page
from .page import refresh_plugins_page as refresh_plugins_page
