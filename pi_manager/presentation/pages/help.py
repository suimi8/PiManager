"""Modern categorized help page."""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ... import core, help_docs
from ..components import SectionHeading, StatusBadge, SurfaceCard

logger = logging.getLogger(__name__)


def build_help_page(window) -> QWidget:
    page = QWidget()
    page.setObjectName("pageBody")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(26, 22, 26, 24)
    layout.setSpacing(12)

    toolbar = SurfaceCard(margins=(14, 12, 14, 12), spacing=8)
    row = QHBoxLayout()
    row.addWidget(SectionHeading("内置使用手册", "按主题组织的本地文档，无需联网即可查看。"), 1)
    row.addWidget(StatusBadge("离线可用", "success"), 0, Qt.AlignTop)
    row.addWidget(window._btn("复制全部 Markdown", window.help_copy_md, secondary=True), 0, Qt.AlignTop)
    row.addWidget(window._btn("导出 .md", window.help_export_md, secondary=True), 0, Qt.AlignTop)
    toolbar.content.addLayout(row)
    layout.addWidget(toolbar)

    tabs_card = SurfaceCard(margins=(10, 8, 10, 10), spacing=0)
    window.help_tabs = QTabWidget()
    window.help_browsers = []
    window._help_section_mds = []
    sections = help_docs.help_sections()
    mode = "night"
    try:
        mode = str(core.get_ui_theme().get("mode") or "night")
    except Exception:
        pass
    for title, markdown in sections:
        tab = QWidget()
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(6, 10, 6, 6)
        browser = QTextBrowser()
        browser.setOpenExternalLinks(True)
        browser.setHtml(help_docs.help_section_html(markdown, mode=mode))
        tab_layout.addWidget(browser, 1)
        window.help_browsers.append(browser)
        window._help_section_mds.append(markdown)
        window.help_tabs.addTab(tab, title)
    window.help_browser = window.help_browsers[0] if window.help_browsers else QTextBrowser()
    tabs_card.content.addWidget(window.help_tabs, 1)
    layout.addWidget(tabs_card, 1)
    return page


class HelpPageMixin:
    """使用教程页：主题重渲、复制与导出。从 ``ui_features.py`` 下沉。"""

    def refresh_help_theme(self, mode: str | None = None) -> None:
        """昼夜切换后重渲帮助 HTML，避免白天模式浅底深色字看不清。"""
        if not getattr(self, "help_browsers", None):
            return
        if mode is None:
            try:
                mode = str(core.get_ui_theme().get("mode") or "night")
            except Exception:
                mode = "night"
        mds = getattr(self, "_help_section_mds", None) or []
        if not mds:
            mds = [md for _, md in help_docs.help_sections()]
            self._help_section_mds = mds
        for browser, md in zip(self.help_browsers, mds):
            try:
                browser.setHtml(help_docs.help_section_html(md, mode=mode))
            except Exception as e:
                logger.warning("render help section failed: %s", e)

    def help_copy_md(self):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(help_docs.HELP_MARKDOWN)
        self.status.showMessage("已复制教程 Markdown 到剪贴板")

    def help_export_md(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出使用教程", str(Path.home() / "PiManager-使用教程.md"), "Markdown (*.md)"
        )
        if not path:
            return
        Path(path).write_text(help_docs.HELP_MARKDOWN, encoding="utf-8")
        self.notify_success(f"已导出：{path}")

