"""桌面进程入口：导航表、单实例保护与 ``run_app``。"""
from __future__ import annotations

import logging
import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

from .. import core
from .design import apply_app_font
from .lifecycle import app_icon

logger = logging.getLogger(__name__)

SINGLE_INSTANCE_SERVER_NAME = "PiManager"


def drain_pending_connections(server) -> int:
    """取走并关闭 QLocalServer 的全部挂起连接，返回处理数量。

    ``QLocalServer`` 在 ``newConnection`` 发出后把已建立的 ``QLocalSocket`` 放进
    内部 pending 队列，**必须**由使用者 ``nextPendingConnection()`` 取走并负责
    销毁。以前的唤醒槽从不取走：每次双击图标唤醒都泄漏一个 socket / 命名管道
    句柄，默认 ``maxPendingConnections()=30``，队列满后 ``newConnection`` 不再
    发出 —— 双击图标彻底不再唤醒窗口，且第二实例连不上后静默退出。
    """
    drained = 0
    while True:
        conn = server.nextPendingConnection()
        if conn is None:
            break
        conn.disconnected.connect(conn.deleteLater)
        conn.close()
        drained += 1
    return drained


NAV_PAGES = [
    ("simple", "简化配置", "默认模型 / 快速接入 / 启动"),
    ("models", "模型列表", "切换、收藏、批量测试"),
    ("providers", "Provider", "自定义与密钥管理"),
    ("chat", "快速提问", "轻量多轮问答"),
    ("sessions", "会话", "继续历史会话"),
    ("health", "健康监控", "可用性巡检"),
    ("history", "测试历史", "延迟记录"),
    ("tools", "工具", "导入导出 / 自检"),
    ("plugins", "插件", "内置 skills / extensions 一键安装"),
    ("settings", "设置", "语言 / 主题 / 代理"),
    ("help", "使用教程", "教程与常见问题"),
]


def run_app():
    import sys

    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass
    try:
        # 关闭原生文件对话框：Windows 上的 IFileDialog 在部分带 shell 扩展
        # （云盘/杀软右键菜单注入）的机器上会在 QFileDialog 返回后崩溃整个
        # 进程，且崩在 Qt 之外无法捕获。代价是失去「最近位置」「快速访问」侧栏
        # 与系统搜索；若将来能确认目标平台无此问题，可按平台放开此开关。
        QApplication.setAttribute(Qt.AA_DontUseNativeDialogs, True)
    except Exception as e:
        logger.warning("disable native dialogs failed: %s", e)
    app = QApplication(sys.argv)
    app.setApplicationName("Pi Manager")
    app.setOrganizationName("PiManager")
    app.setQuitOnLastWindowClosed(False)
    # 单实例保护：只允许一个桌面实例；后启动的实例向已运行实例发送唤醒消息
    # 后退出，避免多实例并发写回 models.json / settings.json 造成数据冲突。
    # PI_MANAGER_DISABLE_SINGLE_INSTANCE=1 可跳过（供测试与嵌入场景使用）。
    server = None
    if os.environ.get("PI_MANAGER_DISABLE_SINGLE_INSTANCE") != "1":
        from PySide6.QtNetwork import QLocalServer, QLocalSocket

        core.ensure_agent_dir()
        server = QLocalServer(app)
        server.setSocketOptions(QLocalServer.UserAccessOption)
        if not server.listen(SINGLE_INSTANCE_SERVER_NAME):
            socket = QLocalSocket()
            socket.connectToServer(SINGLE_INSTANCE_SERVER_NAME)
            if socket.waitForConnected(500):
                socket.write(b"wake")
                socket.flush()
                socket.waitForBytesWritten(500)
                socket.close()
                return 0
            socket.close()
            # listen() 失败且无法连接到已有实例：上一个实例崩溃后遗留了
            # 残旧的 Unix-domain socket 文件（Linux/macOS）。删除它后再重试，
            # 否则用户必须手动删除该 socket 才能再次启动。removeServer 在
            # Windows 的命名管道上是 no-op，故跨平台安全。
            QLocalServer.removeServer(SINGLE_INSTANCE_SERVER_NAME)
            if not server.listen(SINGLE_INSTANCE_SERVER_NAME):
                # 既连不上已有实例，也拿不到监听名：不能静默 return 0（用户双击
                # 图标毫无反应且无提示）。放弃单实例保护继续启动，并留下日志。
                logger.warning(
                    "single-instance server '%s' unavailable (%s); "
                    "starting without single-instance protection",
                    SINGLE_INSTANCE_SERVER_NAME,
                    server.errorString(),
                )
                server = None
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    apply_app_font(app)
    try:
        app.setWindowIcon(app_icon())
    except Exception:
        pass
    theme = core.get_ui_theme()
    core.sync_cli_theme_with_ui(theme.get("mode"))
    from .design import apply_application_theme

    apply_application_theme(app, theme.get("mode"), theme.get("accent"))
    from .main_window import ModernMainWindow

    win = ModernMainWindow()
    win.show()
    if server is not None:

        def _wake_primary():
            drain_pending_connections(server)
            win.showNormal()
            win.raise_()
            win.activateWindow()

        server.newConnection.connect(_wake_primary)
    return app.exec()
