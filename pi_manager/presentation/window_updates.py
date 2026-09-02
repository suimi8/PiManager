"""主窗口更新可见性：状态栏角标、仪表盘横幅与忽略提示。"""
from __future__ import annotations

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices

from .. import core, extras
from .design import tokens_for
from .design.icons import icon
from .workers import Worker


class UpdateChromeMixin:
    """Pi CLI / Pi Manager 更新角标与横幅。"""


    # ---- 更新可见性（Pi CLI / Pi Manager 共用） ---------------------------------
    def _update_colors(self):
        colors = tokens_for(*self._theme_pair())
        return colors

    def _set_update_indicator(self, state: str) -> None:
        self._update_indicator_state = state
        colors = self._update_colors()
        if state == "ok":
            color, name = colors.success, "check"
        elif state == "update":
            color, name = colors.warning, "ellipsis"
        elif state == "failed":
            color, name = colors.danger, "activity"
        elif state == "checking":
            color, name = colors.text_muted, "refresh"
        else:
            color, name = colors.text_muted, "help"
        self.update_indicator.setIcon(icon(name, color, 16))

    def check_all_updates(self) -> None:
        self.status.showMessage("正在检查 Pi 与 Pi Manager 更新…")
        self._set_update_indicator("checking")
        w = self._track(Worker(core.check_pi_status))
        w.done.connect(self._on_update_status)
        w.failed.connect(lambda e: self.status.showMessage(f"检查 Pi 更新失败: {e}"))
        w.start()
        try:
            self.check_manager_update(silent=True)
        except Exception:
            pass

    def _refresh_update_indicators(self) -> None:
        """Refresh nav badges, dashboard banner and status bar from last statuses."""
        self._update_colors()
        pi = getattr(self, "_pi_update_status", None) or {}
        pi_state = str(pi.get("state") or "")
        pi_needs_action = pi_state in {"outdated", "missing", "repair_required"}
        pi_dismissed = bool(
            pi_needs_action and core.is_update_dismissed("pi", str(pi.get("latest") or ""))
        )
        if pi_needs_action and not pi_dismissed:
            self.nav.set_badge("simple")
        else:
            self.nav.clear_badge("simple")

        mgr = getattr(self, "_last_manager_update", None) or {}
        mgr_has_update = bool(mgr.get("has_update"))
        mgr_dismissed = bool(
            mgr_has_update and core.is_update_dismissed("mgr", str(mgr.get("remote") or ""))
        )
        if mgr_has_update and not mgr_dismissed:
            self.nav.set_badge("settings")
        else:
            self.nav.clear_badge("settings")

        if pi_state == "ok":
            installed = pi.get("installed") or "未知"
            self.version_pill.setText(f"✓ {installed}")
            self.version_pill.setToolTip(str(pi.get("message") or ""))
            self.nav.set_version(f"pi: {installed}")
        elif pi_state == "outdated":
            self.version_pill.setText(f"↑ {pi.get('installed')} → {pi.get('latest')}")
            self.version_pill.setToolTip(str(pi.get("message") or ""))
        elif pi_state == "missing":
            self.version_pill.setText("✗ 未安装 Pi")
            self.version_pill.setToolTip(str(pi.get("message") or "未检测到 Pi"))
        elif pi_state in {"check_failed", "blocked"}:
            self.version_pill.setToolTip(str(pi.get("message") or ""))

        indicator = "ok"
        if mgr_has_update and not mgr_dismissed:
            indicator = "update"
        elif pi_needs_action and not pi_dismissed:
            indicator = "update"
        elif pi_state == "check_failed":
            indicator = "failed"
        elif pi_state == "blocked":
            indicator = "update"
        elif not pi and not mgr:
            indicator = ""
        self._set_update_indicator(indicator)

        self._update_banner(pi, pi_dismissed, mgr, mgr_dismissed)
        self._update_version_card(pi, pi_dismissed, mgr, mgr_dismissed)

    def _update_version_card(
        self, pi: dict, pi_dismissed: bool, mgr: dict, mgr_dismissed: bool
    ) -> None:
        """Show a direct update hint inside the PI CLI version metric card.

        The dashboard's version card is the user's fixed reference point for
        the installed Pi CLI version; keep update/install/repair actions right
        there instead of relying only on the top banner.
        """
        if not hasattr(self, "version_update_label") or not hasattr(self, "version_update_btn"):
            return
        pi_state = str(pi.get("state") or "")
        label = self.version_update_label
        button = self.version_update_btn
        if pi_state in {"outdated", "missing", "repair_required"} and not pi_dismissed:
            installed = pi.get("installed")
            latest = pi.get("latest")
            if pi_state == "outdated":
                label.setText(f"有新版本：{installed} → {latest}")
                button.setText("立即更新")
            elif pi_state == "missing":
                label.setText("未检测到 Pi CLI")
                button.setText("安装")
            else:
                label.setText("Pi CLI 运行异常")
                button.setText("修复")
            label.setVisible(True)
            button.setVisible(True)
        else:
            label.setVisible(False)
            button.setVisible(False)

    def _update_banner(self, pi: dict, pi_dismissed: bool, mgr: dict, mgr_dismissed: bool) -> None:
        if not hasattr(self, "update_banner"):
            return
        pi_state = str(pi.get("state") or "")
        show_any = False
        if pi_state in {"outdated", "missing", "repair_required"} and not pi_dismissed:
            show_any = True
            installed = pi.get("installed")
            latest = pi.get("latest")
            if pi_state == "outdated":
                text = f"Pi CLI 有新版本：{installed} → {latest}"
            elif pi_state == "missing":
                text = "未检测到 Pi CLI，点击按钮即可安装"
            else:
                text = "Pi CLI 运行异常，建议修复"
            self.pi_banner_label.setText(text)
            self.pi_banner_btn.setText("安装 Pi" if pi_state == "missing" else "立即更新")
            self.pi_banner_btn.setVisible(True)
            self.pi_banner_close.setVisible(True)
        else:
            self.pi_banner_label.setText("")
            self.pi_banner_btn.setVisible(False)
            self.pi_banner_close.setVisible(False)
        if mgr.get("has_update") and not mgr_dismissed:
            show_any = True
            remote = str(mgr.get("remote") or "")
            self.mgr_banner_label.setText(f"Pi Manager 有新版本：v{extras.APP_VERSION} → v{remote}")
            self.mgr_banner_btn.setVisible(True)
            self.mgr_banner_close.setVisible(True)
        else:
            self.mgr_banner_label.setText("")
            self.mgr_banner_btn.setVisible(False)
            self.mgr_banner_close.setVisible(False)
        self.update_banner.setVisible(show_any)

    def on_pi_banner_action(self) -> None:
        status = getattr(self, "_pi_update_status", None)
        if isinstance(status, dict):
            self.open_install_dialog(status)
        else:
            self.open_install_dialog(None)

    def on_mgr_banner_action(self) -> None:
        res = getattr(self, "_last_manager_update", None) or {}
        page = str(res.get("url") or extras.GITHUB_RELEASES_PAGE)
        try:
            QDesktopServices.openUrl(QUrl(page))
        except Exception:
            core.open_path(page)

    def dismiss_pi_update(self) -> None:
        pi = getattr(self, "_pi_update_status", None) or {}
        core.dismiss_update("pi", str(pi.get("latest") or ""))
        self.status.showMessage("已忽略本次 Pi 更新提示", 5000)
        self._refresh_update_indicators()

    def dismiss_manager_update(self) -> None:
        mgr = getattr(self, "_last_manager_update", None) or {}
        core.dismiss_update("mgr", str(mgr.get("remote") or ""))
        self.status.showMessage("已忽略本次 Pi Manager 更新提示", 5000)
        self._refresh_update_indicators()
