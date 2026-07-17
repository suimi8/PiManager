"""Modern categorized help page."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ... import core, help_docs
from ..components import SectionHeading, StatusBadge, SurfaceCard


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
