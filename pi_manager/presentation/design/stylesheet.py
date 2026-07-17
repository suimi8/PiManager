"""Application stylesheet generated from presentation design tokens."""
from __future__ import annotations

from .tokens import tokens_for
from .typography import mono_font_family, ui_font_family


def build_stylesheet(mode: str = "night", accent: str = "blue") -> str:
    c = tokens_for(mode, accent)
    font = ui_font_family()
    mono = mono_font_family()
    return f"""
/* Pi Manager modern presentation layer */
* {{
    font-family: {font};
}}
QMainWindow, QDialog {{
    background: {c.window};
    color: {c.text};
}}
QWidget {{
    background: transparent;
    color: {c.text};
    font-size: 13px;
}}
QWidget#appRoot, QFrame#contentShell, QStackedWidget#pages {{
    background: {c.window};
}}
QToolTip {{
    background: {c.surface_raised};
    color: {c.text};
    border: 1px solid {c.border_strong};
    border-radius: 7px;
    padding: 6px 9px;
}}

/* Navigation */
QFrame#navRail {{
    background: {c.sidebar};
    border-right: 1px solid {c.border};
}}
QFrame#brandPanel {{
    background: transparent;
    border: none;
}}
QLabel#brandMark {{
    background: {c.accent};
    color: #FFFFFF;
    border-radius: 10px;
    font-size: 16px;
    font-weight: 800;
}}
QLabel#navBrand {{
    color: {c.text};
    font-size: 16px;
    font-weight: 700;
}}
QLabel#navTag, QLabel#navSection {{
    color: {c.text_muted};
}}
QLabel#navSection {{
    padding: 10px 10px 4px 10px;
    font-size: 10px;
    font-weight: 700;
}}
QToolButton#navButton {{
    background: transparent;
    color: {c.text_secondary};
    border: none;
    border-radius: 8px;
    padding: 8px 10px;
    text-align: left;
    font-size: 13px;
    font-weight: 500;
}}
QToolButton#navButton:hover {{
    background: {c.surface_hover};
    color: {c.text};
}}
QToolButton#navButton:checked {{
    background: {c.accent_soft};
    color: {c.accent_text};
    font-weight: 650;
}}
QToolButton#navToggle, QToolButton#iconButton {{
    background: transparent;
    color: {c.text_secondary};
    border: 1px solid transparent;
    border-radius: 8px;
    padding: 7px;
}}
QToolButton#navToggle:hover, QToolButton#iconButton:hover {{
    background: {c.surface_hover};
    border-color: {c.border};
    color: {c.text};
}}
QFrame#sidebarFooter {{
    background: {c.surface};
    border: 1px solid {c.border};
    border-radius: 10px;
}}
QLabel#versionPill {{
    color: {c.text_secondary};
    font-size: 11px;
}}

/* Page header */
QFrame#topBar {{
    background: {c.window};
    border-bottom: 1px solid {c.border};
}}
QLabel#pageEyebrow {{
    color: {c.accent_text};
    font-size: 10px;
    font-weight: 700;
}}
QLabel#pageTitle {{
    color: {c.text};
    font-size: 23px;
    font-weight: 720;
}}
QLabel#pageDescription, QLabel#subtitle, QLabel#muted {{
    color: {c.text_muted};
}}
QLabel#pageDescription {{
    font-size: 12px;
}}
QFrame#pageBody {{
    background: transparent;
}}

/* Surfaces */
QFrame#surfaceCard, QFrame#card {{
    background: {c.surface};
    border: 1px solid {c.border};
    border-radius: 12px;
}}
QFrame#surfaceCard[elevated="true"], QFrame#card[elevated="true"] {{
    background: {c.surface_raised};
    border-color: {c.border_strong};
}}
QFrame#heroCard {{
    background: {c.surface_raised};
    border: 1px solid {c.border_strong};
    border-radius: 14px;
}}
QFrame#metricCard {{
    background: {c.surface};
    border: 1px solid {c.border};
    border-radius: 10px;
}}
QLabel#sectionTitle {{
    color: {c.text};
    font-size: 14px;
    font-weight: 650;
}}
QLabel#sectionKicker {{
    color: {c.text_muted};
    font-size: 10px;
    font-weight: 700;
}}
QLabel#heroValue {{
    color: {c.text};
    font-size: 25px;
    font-weight: 740;
}}
QLabel#heroProvider {{
    color: {c.accent_text};
    font-size: 11px;
    font-weight: 700;
}}
QLabel#metricValue {{
    color: {c.text};
    font-size: 19px;
    font-weight: 700;
}}
QLabel#metricLabel {{
    color: {c.text_muted};
    font-size: 11px;
}}
QFrame#divider {{
    background: {c.border};
    border: none;
}}
QFrame#collapsibleSection {{
    background: {c.surface};
    border: 1px solid {c.border};
    border-radius: 12px;
}}
QToolButton#collapsibleHeader {{
    background: transparent;
    color: {c.text};
    border: none;
    border-radius: 11px;
    padding: 13px 15px;
    text-align: left;
    font-size: 14px;
    font-weight: 650;
}}
QToolButton#collapsibleHeader:hover {{
    background: {c.surface_hover};
}}
QLabel#collapsibleDescription {{
    color: {c.text_muted};
    font-size: 11px;
}}
QGroupBox {{
    background: {c.surface};
    border: 1px solid {c.border};
    border-radius: 12px;
    margin-top: 13px;
    padding: 13px 13px 11px 13px;
    font-weight: 650;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 13px;
    padding: 0 6px;
    color: {c.text};
    background: {c.window};
}}

/* Badges */
QLabel#statusBadge {{
    border: 1px solid {c.border};
    border-radius: 9px;
    padding: 3px 8px;
    color: {c.text_secondary};
    background: {c.surface_hover};
    font-size: 11px;
    font-weight: 650;
}}
QLabel#statusBadge[status="success"] {{
    color: {c.success};
    background: {c.success_soft};
    border-color: {c.success};
}}
QLabel#statusBadge[status="warning"] {{
    color: {c.warning};
    background: {c.warning_soft};
    border-color: {c.warning};
}}
QLabel#statusBadge[status="danger"] {{
    color: {c.danger};
    background: {c.danger_soft};
    border-color: {c.danger};
}}
QLabel#statusBadge[status="info"] {{
    color: {c.info};
    background: {c.info_soft};
    border-color: {c.info};
}}

/* Buttons */
QPushButton, QToolButton {{
    min-height: 32px;
}}
QPushButton {{
    background: {c.accent};
    color: #FFFFFF;
    border: 1px solid {c.accent};
    border-radius: 8px;
    padding: 0 13px;
    font-weight: 620;
}}
QPushButton:hover {{
    background: {c.accent_hover};
    border-color: {c.accent_hover};
}}
QPushButton:pressed {{
    background: {c.accent_pressed};
    border-color: {c.accent_pressed};
}}
QPushButton:disabled {{
    background: {c.surface_hover};
    color: {c.text_muted};
    border-color: {c.border};
}}
QPushButton[secondary="true"], QToolButton[secondary="true"] {{
    background: {c.surface};
    color: {c.text_secondary};
    border: 1px solid {c.border_strong};
}}
QPushButton[secondary="true"]:hover, QToolButton[secondary="true"]:hover {{
    background: {c.surface_hover};
    color: {c.text};
    border-color: {c.border_strong};
}}
QPushButton[ghost="true"] {{
    background: transparent;
    color: {c.text_secondary};
    border: 1px solid transparent;
}}
QPushButton[ghost="true"]:hover {{
    background: {c.surface_hover};
    color: {c.text};
}}
QPushButton[success="true"] {{
    background: {c.success};
    border-color: {c.success};
    color: #FFFFFF;
}}
QPushButton[success="true"]:hover {{
    background: #2FB565;
    border-color: #2FB565;
}}
QPushButton[danger="true"] {{
    background: {c.danger_soft};
    color: {c.danger};
    border-color: {c.danger};
}}
QPushButton[danger="true"]:hover {{
    background: {c.danger};
    color: #FFFFFF;
}}
QPushButton[large="true"] {{
    min-height: 38px;
    padding: 0 17px;
}}
QToolButton[secondary="true"] {{
    border-radius: 8px;
    padding: 0 11px;
}}
QToolButton::menu-indicator {{
    image: none;
}}

/* Inputs */
QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox {{
    background: {c.input};
    color: {c.text};
    border: 1px solid {c.border};
    border-radius: 8px;
    selection-background-color: {c.accent};
    selection-color: {c.selection_text};
}}
QLineEdit, QComboBox, QSpinBox {{
    min-height: 32px;
    padding: 0 10px;
}}
QPlainTextEdit, QTextEdit {{
    padding: 9px;
}}
QLineEdit:hover, QPlainTextEdit:hover, QTextEdit:hover, QComboBox:hover, QSpinBox:hover {{
    border-color: {c.border_strong};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
    border: 1px solid {c.accent};
}}
QComboBox::drop-down {{
    width: 26px;
    border: none;
}}
QComboBox QAbstractItemView {{
    background: {c.surface_raised};
    color: {c.text};
    border: 1px solid {c.border_strong};
    selection-background-color: {c.accent_soft};
    selection-color: {c.accent_text};
    outline: none;
}}
QCheckBox, QRadioButton {{
    color: {c.text_secondary};
    spacing: 7px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px;
    height: 15px;
}}
QCheckBox::indicator {{
    background: {c.input};
    border: 1px solid {c.border_strong};
    border-radius: 4px;
}}
QCheckBox::indicator:checked {{
    background: {c.accent};
    border-color: {c.accent};
}}
QRadioButton::indicator {{
    background: {c.input};
    border: 1px solid {c.border_strong};
    border-radius: 8px;
}}
QRadioButton::indicator:checked {{
    background: {c.accent};
    border: 4px solid {c.input};
}}

/* Lists and tables */
QListWidget, QTreeWidget, QTableWidget {{
    background: {c.surface};
    color: {c.text_secondary};
    border: 1px solid {c.border};
    border-radius: 10px;
    outline: none;
    alternate-background-color: {c.surface_raised};
    gridline-color: transparent;
}}
QListWidget::item {{
    min-height: 34px;
    padding: 4px 9px;
    border-radius: 6px;
    margin: 2px 4px;
}}
QListWidget::item:hover {{
    background: {c.surface_hover};
    color: {c.text};
}}
QListWidget::item:selected {{
    background: {c.accent_soft};
    color: {c.accent_text};
}}
QTableWidget::item {{
    border-bottom: 1px solid {c.border};
    padding: 6px 9px;
}}
QTableWidget::item:hover {{
    background: {c.surface_hover};
}}
QTableWidget::item:selected {{
    background: {c.accent_soft};
    color: {c.text};
}}
QHeaderView::section {{
    background: {c.surface_raised};
    color: {c.text_muted};
    border: none;
    border-bottom: 1px solid {c.border_strong};
    padding: 8px 9px;
    font-size: 11px;
    font-weight: 700;
}}
QTableCornerButton::section {{
    background: {c.surface_raised};
    border: none;
}}

/* Menus, tabs, scrollbars */
QMenu {{
    background: {c.surface_raised};
    color: {c.text};
    border: 1px solid {c.border_strong};
    border-radius: 8px;
    padding: 5px;
}}
QMenu::item {{
    padding: 7px 24px 7px 10px;
    border-radius: 5px;
}}
QMenu::item:selected {{
    background: {c.accent_soft};
    color: {c.accent_text};
}}
QMenu::separator {{
    height: 1px;
    background: {c.border};
    margin: 5px 8px;
}}
QTabWidget::pane {{
    background: {c.surface};
    border: 1px solid {c.border};
    border-radius: 10px;
}}
QTabBar::tab {{
    background: transparent;
    color: {c.text_muted};
    padding: 8px 13px;
    border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{
    color: {c.accent_text};
    border-bottom-color: {c.accent};
}}
QScrollArea {{
    background: transparent;
    border: none;
}}
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {c.border_strong};
    min-height: 28px;
    border-radius: 4px;
}}
QScrollBar::handle:vertical:hover {{
    background: {c.text_muted};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
    border: none;
    height: 0;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {c.border_strong};
    min-width: 28px;
    border-radius: 4px;
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
    border: none;
    width: 0;
}}

/* Specialized legacy-compatible widgets */
QFrame#dropZone {{
    background: {c.input};
    border: 1px dashed {c.border_strong};
    border-radius: 10px;
}}
QFrame#dropZone[active="true"] {{
    background: {c.accent_soft};
    border: 1px solid {c.accent};
}}
QLabel#pill {{
    color: {c.text_secondary};
    background: {c.surface_hover};
    border: 1px solid {c.border};
    border-radius: 9px;
    padding: 4px 8px;
}}
QProgressBar {{
    background: {c.input};
    border: 1px solid {c.border};
    border-radius: 6px;
    min-height: 10px;
    text-align: center;
}}
QProgressBar::chunk {{
    background: {c.accent};
    border-radius: 5px;
}}
QStatusBar {{
    background: {c.sidebar};
    color: {c.text_muted};
    border-top: 1px solid {c.border};
    min-height: 25px;
}}
QStatusBar::item {{
    border: none;
}}
QLabel#mono, QPlainTextEdit#mono {{
    font-family: {mono};
}}
"""


def palette_colors(mode: str = "night", accent: str = "blue") -> dict[str, str]:
    c = tokens_for(mode, accent)
    return {
        "window": c.window,
        "text": c.text,
        "base": c.input,
        "button": c.surface,
        "button_text": c.text,
        "highlight": c.accent,
        "alternate_base": c.surface_raised,
        "tooltip_base": c.surface_raised,
        "tooltip_text": c.text,
    }
