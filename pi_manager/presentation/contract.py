# -*- coding: utf-8 -*-
"""表现层向行为基类注入的 widget 契约（显式化，供类型检查器兜底）。

**为什么需要这个文件**

当前分层的依赖方向是倒置的：``pi_manager/ui.py`` 的 ``MainWindow`` 与
``pi_manager/ui_features.py`` 的 ``FeatureMixin`` 不是被表现层调用的底层，而是
``presentation.main_window.ModernMainWindow`` 的**基类**；而这些基类的方法直接
读取 ``self.models_table`` / ``self.workdir_edit`` / ``self.chat_output`` 等由
子类的页面构建器（``presentation/pages/*.py``）注入到 ``window`` 上的属性。

这个契约此前完全隐式：没有 Protocol、没有类型标注、无静态可验证性。删掉
``dashboard.py`` 里一行 ``window.lbl_thinking = ...``，要等运行到
``refresh_dashboard`` 才会炸。基类里那批 ``hasattr`` 防御正是这个架构的症状
（而非病因）——它们必要，是因为基类无法静态知道子类到底建了哪些 widget。

**这个文件做什么 / 不做什么**

* 做：把「基类读取、表现层注入」的公共 widget 属性写成 ``Protocol``，
  ``mypy``/``pyright`` 从此可以在删除或改名一个 widget 时报错。
* 不做：不改任何运行时行为。``Protocol`` 只在类型检查期生效；运行期不会被
  实例化、不会被继承、不参与 MRO。

**如何使用**

```python
if TYPE_CHECKING:
    from .presentation.contract import WindowWidgets

class MainWindow(WorkerTrackerMixin, FeatureMixin, QMainWindow):
    if TYPE_CHECKING:
        # 断言：本类的方法会读取这些由子类注入的 widget
        def __getattr__(self, name: str) -> object: ...
```

或在类型检查配置里把 ``MainWindow`` 的 ``self`` 视作 ``WindowWidgets``。

**维护约定**：新增一个「基类读取 + 表现层注入」的 widget 时，同步在这里加一行；
只在表现层内部使用（基类不读）的 widget **不要**加进来，避免契约膨胀。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QSpinBox,
    QStatusBar,
    QTableWidget,
    QTreeWidget,
    QWidget,
)


@runtime_checkable
class WindowWidgets(Protocol):
    """``ui.py`` / ``ui_features.py`` 的方法会读取的 widget 属性全集。

    ``runtime_checkable`` 只支持 ``isinstance`` 的存在性检查（不校验类型），
    因此可用于测试里的「契约完整性」断言；日常价值在静态检查。
    """

    # ---- 壳层 ----------------------------------------------------------------
    status: QStatusBar

    # ---- 简化配置 / 仪表盘 ---------------------------------------------------
    lbl_current: QLabel
    lbl_thinking: QLabel
    version_pill: QLabel
    auth_table: QTableWidget
    fav_list: QListWidget
    workdir_edit: QLineEdit
    terminal_combo: QComboBox
    drop_zone: QFrame
    drop_hint: QLabel
    chk_drop_launch: QCheckBox
    quick_name: QLineEdit
    quick_base: QLineEdit
    quick_key: QLineEdit
    quick_api: QComboBox
    quick_status: QLabel

    # ---- 模型列表 ------------------------------------------------------------
    models_table: QTreeWidget
    models_count_lbl: QLabel
    model_filter: QLineEdit
    model_provider_filter: QComboBox
    model_only_favorites: QCheckBox
    thinking_combo: QComboBox
    test_mode_combo: QComboBox
    test_status: QLabel

    # ---- Provider ------------------------------------------------------------
    provider_list: QListWidget
    provider_detail: QPlainTextEdit

    # ---- 快速提问 ------------------------------------------------------------
    chat_provider: QComboBox
    chat_model: QComboBox
    chat_output: QPlainTextEdit
    chat_input: QWidget  # ImageAttachEdit（QPlainTextEdit 子类 + attachments()）
    chat_context_badge: QWidget  # StatusBadge

    # ---- 会话 ----------------------------------------------------------------
    sessions_table: QTableWidget
    session_filter_wd: QLineEdit
    session_filter_name: QLineEdit

    # ---- 健康监控 / 测试历史 / 诊断 ------------------------------------------
    health_table: QTableWidget
    health_status: QLabel
    health_interval: QSpinBox
    history_table: QTableWidget
    history_filter: QLineEdit
    selfcheck_table: QTableWidget
    backup_status: QLabel
    update_status: QLabel
    update_url_edit: QLineEdit
    mgr_version_lbl: QLabel

    # ---- 设置 ----------------------------------------------------------------
    set_provider: QLineEdit
    set_model: QLineEdit
    set_thinking: QComboBox
    set_enabled: QPlainTextEdit
    set_language: QComboBox
    set_ui_mode: QComboBox
    set_ui_accent: QComboBox
    settings_raw: QPlainTextEdit
    secure_keys_chk: QCheckBox
    minimize_to_tray: QCheckBox
    start_minimized: QCheckBox
    proxy_enabled: QCheckBox
    proxy_url: QLineEdit
    failover_enabled: QCheckBox
    failover_silent: QCheckBox
    failover_threshold: QSpinBox
    test_concurrency: QSpinBox
    chat_persistent_session: QCheckBox
    vision_model_combo: QComboBox
    vision_test_status: QLabel
    zhipu_key_edit: QLineEdit

    # ---- 帮助 ----------------------------------------------------------------
    help_browsers: list


#: 契约中所有属性名（供测试断言表现层确实注入了它们）。
#: ``Protocol.__annotations__`` 只含本类直接声明的注解，正是我们要的集合。
WINDOW_WIDGET_NAMES: tuple[str, ...] = tuple(WindowWidgets.__annotations__)
