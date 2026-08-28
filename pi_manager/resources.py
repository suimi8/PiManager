# -*- coding: utf-8 -*-
"""Resolve bundled asset paths (dev + PyInstaller, all platforms)."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


def _frozen_roots() -> list[Path]:
    """冻结产物的资源根目录，按信任度排序（``sys._MEIPASS`` 最高优先）。

    安全约束（docs/review/r2-build.md B-1，严重）：便携版的 **exe 所在目录是
    攻击者可写的**（README / RUN-windows.txt 明示「可放到任意目录 / U 盘」，
    ``Downloads/``、U 盘、共享目录都是典型投放点）。旧实现把 ``exe_dir`` 与
    ``exe_dir/assets`` 排在 ``sys._MEIPASS`` **之前**，于是同目录投放的
    ``assets/builtin/manifest.json`` 会完全遮蔽打包进产物的真实清单，
    ``builtin_plugins`` 随后把清单里指定的任意 ``.ts`` 写入
    ``~/.pi/agent/extensions/``（扩展拥有完整系统权限）→ 任意代码执行。

    因此这里**只信任 PyInstaller 自己创建的确定性布局**，不再接受任何
    「exe 同级目录」通配根：

    - onefile：``sys._MEIPASS`` 是随机命名、进程退出即删的解包目录；
    - onedir ：``sys._MEIPASS == <exe_dir>/_internal``（PyInstaller 6.x
      的 ``contents_directory`` 默认值）；
    - macOS ``.app``：``sys._MEIPASS == <app>/Contents/Frameworks``，数据文件
      实体位于 ``Contents/Resources``，两棵树互为符号链接。
    """
    roots: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        mp = Path(meipass)
        roots.extend([mp / "assets", mp])
        # macOS .app：Frameworks↔Resources 交叉符号链接若缺失（归档被破坏等），
        # 显式补上 Resources 分支。两者都在 .app 内部，信任级别与主二进制相同。
        contents = mp.parent
        if mp.name == "Frameworks" and contents.name == "Contents":
            roots.extend([contents / "Resources" / "assets", contents / "Resources"])
        return roots

    # 走到这里说明 sys.frozen 为真但没有 _MEIPASS（非 PyInstaller 冻结器 /
    # 极旧布局）。仍然只接受确定性子目录，绝不把 exe_dir 本身当资源根。
    exe_dir = Path(sys.executable).resolve().parent
    roots.extend([exe_dir / "_internal" / "assets", exe_dir / "_internal"])
    if exe_dir.name == "MacOS":
        contents = exe_dir.parent
        roots.extend(
            [
                contents / "Resources" / "assets",
                contents / "Resources",
                contents / "Frameworks" / "assets",
                contents / "Frameworks",
            ]
        )
    return roots


def _candidates_roots() -> list[Path]:
    roots: list[Path] = []
    if getattr(sys, "frozen", False):
        # 冻结态不再混入源码树候选：冻结时 __file__ 指向 _MEIPASS 内部，混入
        # 只会产生重复项，却给未来的布局变化留下旁路空间。
        roots.extend(_frozen_roots())
    else:
        # source tree: pi_manager/resources.py -> ../assets
        here = Path(__file__).resolve().parent
        roots.extend([here.parent / "assets", here.parent, here / "assets"])
    out: list[Path] = []
    seen: set[str] = set()
    for r in roots:
        s = str(r)
        if s not in seen:
            seen.add(s)
            out.append(r)
    return out


def assets_dir() -> Path:
    for root in _candidates_roots():
        if root.name == "assets" and root.is_dir():
            return root
        nested = root / "assets"
        if nested.is_dir():
            return nested
        # 探针文件兜底（源码树被重排等情况）。放在 ``root/assets`` 之后，且所有
        # 候选根现已收敛为可信目录，因此不会再被 exe 同级目录的 icon.png 误导
        # （B-21，与 B-1 同源）。
        if (root / "icon.png").exists() or (root / "pi-manager.ico").exists() or (root / "logo-256.png").exists():
            return root
    return Path(__file__).resolve().parent.parent / "assets"


def asset_path(*parts: str) -> Path | None:
    for root in _candidates_roots():
        p = root.joinpath(*parts)
        if p.exists():
            return p
        p2 = root / "assets"
        p = p2.joinpath(*parts)
        if p.exists():
            return p
    return None


def icon_candidates() -> list[Path]:
    names = [
        ("pi-manager.ico",),
        ("icon.png",),
        ("logo-256.png",),
        ("logo-512.png",),
        ("logo-1024.png",),
    ]
    out: list[Path] = []
    seen: set[str] = set()
    for n in names:
        p = asset_path(*n)
        if p is not None and str(p) not in seen:
            seen.add(str(p))
            out.append(p)
    return out


def _check_image_pipeline() -> list[str]:
    """真正渲染一次 SVG 图标与 PNG 位图。

    只检查「文件存在」无法发现 ``Qt6Svg`` / ``qsvg`` / ``qsvgicon`` / ``qpng``
    在 Qt 裁剪中被误删：``icons.icon()`` 的三条错误路径全部静默返回空
    ``QIcon()``，那样的产物会以完全无图标的 UI 启动，而旧版 ``--self-check``
    依旧打印 OK（docs/review/r2-build.md B-8）。需要 QApplication 已就绪。
    """
    problems: list[str] = []
    try:
        from PySide6.QtGui import QPixmap
    except Exception as exc:  # pragma: no cover - environment specific
        return [f"QtGui import failed: {exc}"]

    try:
        from pi_manager.presentation.design import icons

        qicon = icons.icon("home")
        if qicon.isNull():
            problems.append(
                "SVG icon rendering failed (Qt6Svg / qsvg plugin missing?): icons/home.svg"
            )
        else:
            pixmap = qicon.pixmap(18, 18)
            if pixmap.isNull() or pixmap.width() <= 0:
                problems.append("SVG icon rendered an empty pixmap: icons/home.svg")
    except Exception as exc:  # pragma: no cover - environment specific
        problems.append(f"SVG icon rendering raised: {exc}")

    png = asset_path("logo-256.png") or asset_path("icon.png")
    if png is None:
        problems.append("bundled PNG logo missing (logo-256.png / icon.png)")
    else:
        try:
            if QPixmap(str(png)).isNull():
                problems.append(
                    f"PNG decoding failed (qpng imageformat plugin missing?): {png.name}"
                )
        except Exception as exc:  # pragma: no cover - environment specific
            problems.append(f"PNG decoding raised: {exc}")
    return problems


def _check_deep_ui() -> list[str]:
    """深度自检：真正实例化一次主窗口（页面 / 样式表 / 主题全部构造）。

    仅当 ``PIMANAGER_SELFCHECK_DEEP`` 为真值时执行 —— 主窗口会读写用户配置
    目录，默认关闭以免污染开发者本机的 ``~/.pi/agent/``；由
    ``scripts/smoke_test_dist.py --deep`` 在一次性 CI runner 上开启。
    """
    problems: list[str] = []
    try:
        from PySide6.QtWidgets import QApplication, QWidget

        app = QApplication.instance()
        if app is None:
            return ["deep self-check requires an existing QApplication"]
        from pi_manager.presentation import ModernMainWindow

        window = ModernMainWindow()
        try:
            if len(window.findChildren(QWidget)) < 5:
                problems.append("main window built almost no child widgets")
        finally:
            window.close()
            window.deleteLater()
            app.processEvents()
    except Exception as exc:  # pragma: no cover - environment specific
        problems.append(f"main window construction failed: {exc}")
    return problems


def _deep_self_check_enabled() -> bool:
    raw = os.environ.get("PIMANAGER_SELFCHECK_DEEP", "").strip().lower()
    return raw not in {"", "0", "false", "no", "off"}


def self_check() -> list[str]:
    """Return human-readable diagnostics. Empty list means OK for packaging smoke tests."""
    errors: list[str] = []
    # Critical imports for a frozen GUI build
    try:
        import PySide6  # noqa: F401
        from PySide6.QtCore import Qt  # noqa: F401
        from PySide6.QtWidgets import QApplication  # noqa: F401
    except Exception as exc:  # pragma: no cover - environment specific
        errors.append(f"PySide6 import failed: {exc}")

    try:
        import certifi  # noqa: F401
    except Exception as exc:
        errors.append(f"certifi import failed: {exc}")

    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
    except Exception as exc:
        errors.append(f"cryptography import failed: {exc}")

    try:
        import keyring  # noqa: F401
    except Exception as exc:
        errors.append(f"keyring import failed: {exc}")

    try:
        from pi_manager import core, extras, secrets, platform_util, provider_env  # noqa: F401
        from pi_manager.presentation import ModernMainWindow  # noqa: F401
    except Exception as exc:
        errors.append(f"pi_manager package import failed: {exc}")

    icon = asset_path("icon.png") or asset_path("logo-256.png") or asset_path("pi-manager.ico")
    if icon is None:
        errors.append("bundled assets missing (icon.png / logo-256.png / pi-manager.ico)")
    required_ui_icons = ("home.svg", "models.svg", "providers.svg", "settings.svg")
    missing_ui_icons = [name for name in required_ui_icons if asset_path("icons", name) is None]
    if missing_ui_icons:
        errors.append(f"modern UI icons missing: {', '.join(missing_ui_icons)}")

    # Offscreen Qt app creation proves plugins are loadable without a display server
    # when QT_QPA_PLATFORM=offscreen (set by CI smoke tests).
    if not errors:
        try:
            from PySide6.QtWidgets import QApplication

            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
            app = QApplication.instance() or QApplication(["PiManagerSelfCheck"])
            _ = app.applicationName()
        except Exception as exc:
            errors.append(f"Qt QApplication init failed: {exc}")
    # 图像插件渲染验证（依赖上面的 QApplication 已创建）。
    if not errors:
        errors.extend(_check_image_pipeline())
    if not errors and _deep_self_check_enabled():
        errors.extend(_check_deep_ui())
    try:
        from pi_manager import secrets as _secretstore
        _vault_path = _secretstore._vault_path()
        if _vault_path.exists():
            _secretstore.load_vault()
        _master_key_path = _secretstore._master_key_path()
        if _master_key_path.exists() and os.name != "nt":
            _master_mode = stat.S_IMODE(_master_key_path.stat().st_mode)
            if _master_mode & 0o077:
                errors.append(f"vault master key permissions too open: {oct(_master_mode)}")
    except Exception as exc:
        errors.append(f"vault security check failed: {exc}")
    # 内置插件（skills / extensions）资源完整性
    try:
        from pi_manager import builtin_plugins, plugin_manager
        errors.extend(builtin_plugins.self_check())
        errors.extend(plugin_manager.self_check())
    except Exception as exc:
        errors.append(f"plugin resources check failed: {exc}")
    return errors
