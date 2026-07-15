"""Pi Manager window themes (night / day + accent colors).

Modern cross-platform stylesheet for Windows / macOS / Linux.
"""
from __future__ import annotations

import sys
from typing import Any

# mode: night | day
# accent: blue | green | purple | orange | cyan

ACCENTS = {
    "blue": {
        "primary": "#3b82f6",
        "primary_hover": "#60a5fa",
        "primary_pressed": "#2563eb",
        "pill_border": "#3b82f680",
        "accent_text": "#93c5fd",
        "accent_soft": "#3b82f61a",
        "accent_soft_day": "#3b82f614",
        "glow": "#3b82f633",
    },
    "green": {
        "primary": "#22c55e",
        "primary_hover": "#4ade80",
        "primary_pressed": "#16a34a",
        "pill_border": "#22c55e80",
        "accent_text": "#86efac",
        "accent_soft": "#22c55e1a",
        "accent_soft_day": "#22c55e14",
        "glow": "#22c55e33",
    },
    "purple": {
        "primary": "#a855f7",
        "primary_hover": "#c084fc",
        "primary_pressed": "#9333ea",
        "pill_border": "#a855f780",
        "accent_text": "#d8b4fe",
        "accent_soft": "#a855f71a",
        "accent_soft_day": "#a855f714",
        "glow": "#a855f733",
    },
    "orange": {
        "primary": "#f59e0b",
        "primary_hover": "#fbbf24",
        "primary_pressed": "#d97706",
        "pill_border": "#f59e0b80",
        "accent_text": "#fcd34d",
        "accent_soft": "#f59e0b1a",
        "accent_soft_day": "#f59e0b14",
        "glow": "#f59e0b33",
    },
    "cyan": {
        "primary": "#06b6d4",
        "primary_hover": "#22d3ee",
        "primary_pressed": "#0891b2",
        "pill_border": "#06b6d480",
        "accent_text": "#67e8f9",
        "accent_soft": "#06b6d41a",
        "accent_soft_day": "#06b6d414",
        "glow": "#06b6d433",
    },
}

MODES = {
    "night": {
        "window": "#0b0f14",
        "text": "#e8eef7",
        "subtitle": "#8b9bb4",
        "muted": "#6b7a90",
        "pane": "#121820",
        "tab": "#1a222d",
        "tab_text": "#8b9bb4",
        "tab_selected": "#243041",
        "tab_selected_text": "#ffffff",
        "group_border": "#243041",
        "group_title": None,
        "input_bg": "#0f141b",
        "input_border": "#2a3545",
        "input_focus": None,
        "header_bg": "#161d27",
        "header_text": "#c5d0e0",
        "btn_disabled_bg": "#243041",
        "btn_disabled_text": "#6b7a90",
        "btn_secondary_bg": "#161d27",
        "btn_secondary_border": "#2a3545",
        "btn_secondary_hover": "#1e2836",
        "danger": "#ef4444",
        "danger_hover": "#f87171",
        "success": "#16a34a",
        "success_hover": "#22c55e",
        "status_bg": "#070a0e",
        "status_text": "#8b9bb4",
        "toolbar_bg": "#070a0e",
        "toolbar_border": "#1a222d",
        "title": "#f4f7fb",
        "card_bg": "#121820",
        "card_border": "#243041",
        "card_hover_border": "#334155",
        "drop_bg": "#0f1520",
        "drop_border": "#3b4d66",
        "drop_active_bg": "#132033",
        "selection": "#3b82f6",
        "sidebar_bg": "#080b10",
        "sidebar_border": "#1a222d",
        "nav_item": "transparent",
        "nav_item_hover": "#161d27",
        "nav_item_text": "#9aa8bd",
        "nav_item_selected_text": "#ffffff",
        "content_bg": "#0b0f14",
        "scroll": "#2a3545",
        "scroll_hover": "#3b4d66",
        "table_alt": "#0f141b",
        "separator": "#1e2836",
        "menu_bg": "#121820",
        "tooltip_bg": "#1a222d",
        "tooltip_text": "#e8eef7",
        "palette_window": "#0b0f14",
        "palette_text": "#e8eef7",
        "palette_base": "#0f141b",
        "palette_button": None,
        "palette_button_text": "#ffffff",
        "palette_highlight": None,
    },
    "day": {
        "window": "#f3f5f8",
        "text": "#1a2332",
        "subtitle": "#5b6b80",
        "muted": "#8090a3",
        "pane": "#ffffff",
        "tab": "#e8edf3",
        "tab_text": "#5b6b80",
        "tab_selected": "#ffffff",
        "tab_selected_text": "#1a2332",
        "group_border": "#d7dee8",
        "group_title": None,
        "input_bg": "#ffffff",
        "input_border": "#d0d8e3",
        "input_focus": None,
        "header_bg": "#eef2f7",
        "header_text": "#1a2332",
        "btn_disabled_bg": "#d7dee8",
        "btn_disabled_text": "#93a0b0",
        "btn_secondary_bg": "#ffffff",
        "btn_secondary_border": "#d0d8e3",
        "btn_secondary_hover": "#eef2f7",
        "danger": "#dc2626",
        "danger_hover": "#b91c1c",
        "success": "#15803d",
        "success_hover": "#166534",
        "status_bg": "#ffffff",
        "status_text": "#5b6b80",
        "toolbar_bg": "#ffffff",
        "toolbar_border": "#d7dee8",
        "title": "#0f172a",
        "card_bg": "#ffffff",
        "card_border": "#d7dee8",
        "card_hover_border": "#b8c4d4",
        "drop_bg": "#f7f9fc",
        "drop_border": "#b8c4d4",
        "drop_active_bg": "#eff6ff",
        "selection": "#2563eb",
        "sidebar_bg": "#ffffff",
        "sidebar_border": "#e2e8f0",
        "nav_item": "transparent",
        "nav_item_hover": "#eef2f7",
        "nav_item_text": "#5b6b80",
        "nav_item_selected_text": "#ffffff",
        "content_bg": "#f3f5f8",
        "scroll": "#c5d0de",
        "scroll_hover": "#a8b6c8",
        "table_alt": "#f7f9fc",
        "separator": "#e2e8f0",
        "menu_bg": "#ffffff",
        "tooltip_bg": "#1a2332",
        "tooltip_text": "#ffffff",
        "palette_window": "#f3f5f8",
        "palette_text": "#1a2332",
        "palette_base": "#ffffff",
        "palette_button": None,
        "palette_button_text": "#ffffff",
        "palette_highlight": None,
    },
}

MODE_LABELS = {"night": "夜间模式", "day": "白天模式"}
ACCENT_LABELS = {
    "blue": "蓝色",
    "green": "绿色",
    "purple": "紫色",
    "orange": "橙色",
    "cyan": "青色",
}

# Nav page key -> short glyph (cross-platform unicode, no emoji font deps)
NAV_ICONS = {
    "simple": "◆",
    "models": "☰",
    "providers": "◎",
    "chat": "✎",
    "sessions": "◷",
    "health": "♥",
    "history": "◷",
    "tools": "⚙",
    "settings": "✦",
    "help": "?",
}


def normalize_mode(mode: str | None) -> str:
    m = (mode or "night").lower()
    if m in {"light", "day", "白天"}:
        return "day"
    return "night"


def normalize_accent(accent: str | None) -> str:
    a = (accent or "blue").lower()
    return a if a in ACCENTS else "blue"


def ui_font_family() -> str:
    """Prefer modern system UI fonts per platform."""
    if sys.platform == "darwin":
        return '"SF Pro Text", "SF Pro Display", "Helvetica Neue", "PingFang SC", sans-serif'
    if sys.platform == "win32":
        return '"Segoe UI Variable", "Segoe UI", "Microsoft YaHei UI", "Microsoft YaHei", sans-serif'
    return '"Inter", "Noto Sans CJK SC", "Noto Sans", "Source Han Sans SC", "WenQuanYi Micro Hei", sans-serif'


def ui_mono_family() -> str:
    if sys.platform == "darwin":
        return '"SF Mono", Menlo, Monaco, monospace'
    if sys.platform == "win32":
        return '"Cascadia Mono", "Consolas", "Courier New", monospace'
    return '"JetBrains Mono", "Fira Code", "DejaVu Sans Mono", monospace'


def build_stylesheet(mode: str = "night", accent: str = "blue") -> str:
    mode = normalize_mode(mode)
    accent = normalize_accent(accent)
    c = dict(MODES[mode])
    a = ACCENTS[accent]
    c["primary"] = a["primary"]
    c["primary_hover"] = a["primary_hover"]
    c["primary_pressed"] = a["primary_pressed"]
    c["pill_border"] = a["pill_border"]
    c["accent_text"] = a["accent_text"] if mode == "night" else a["primary"]
    c["group_title"] = c["accent_text"]
    c["selection"] = a["primary"]
    c["accent_soft"] = a["accent_soft"] if mode == "night" else a["accent_soft_day"]
    c["glow"] = a["glow"]
    drop_active_border = a["primary"]
    font = ui_font_family()
    mono = ui_mono_family()
    radius = "12px"
    radius_sm = "10px"
    radius_xs = "8px"

    return f"""
/* ========== Base ========== */
* {{
  font-family: {font};
}}
QMainWindow, QWidget {{
  background: {c['window']};
  color: {c['text']};
  font-size: 13px;
}}
QToolTip {{
  background: {c['tooltip_bg']};
  color: {c['tooltip_text']};
  border: 1px solid {c['card_border']};
  border-radius: 8px;
  padding: 6px 10px;
  font-size: 12px;
}}

/* ========== Sidebar ========== */
QFrame#sidebar {{
  background: {c['sidebar_bg']};
  border-right: 1px solid {c['sidebar_border']};
}}
QFrame#sidebar QPushButton {{
  padding: 8px 10px;
  font-size: 12px;
  border-radius: {radius_xs};
}}
QLabel#brandIcon {{
  background: transparent;
  border-radius: 12px;
}}
QLabel#navBrand {{
  font-size: 17px;
  font-weight: 800;
  color: {c['title']};
  letter-spacing: 0.2px;
  padding: 2px 2px 0 2px;
}}
QLabel#navTag {{
  color: {c['subtitle']};
  font-size: 11px;
  padding: 0 2px 10px 2px;
}}
QListWidget#sideNav {{
  background: transparent;
  border: none;
  outline: none;
  padding: 2px 6px 6px 6px;
}}
QListWidget#sideNav::item {{
  background: {c['nav_item']};
  color: {c['nav_item_text']};
  border-radius: {radius_sm};
  padding: 11px 14px;
  margin: 2px 0;
  border: 1px solid transparent;
}}
QListWidget#sideNav::item:hover {{
  background: {c['nav_item_hover']};
  color: {c['text']};
  border: 1px solid {c['card_border']};
}}
QListWidget#sideNav::item:selected {{
  background: {c['primary']};
  color: {c['nav_item_selected_text']};
  font-weight: 600;
  border: 1px solid {c['primary_hover']};
}}

/* ========== Content shell ========== */
QFrame#contentShell {{
  background: {c['content_bg']};
  border: none;
}}
QFrame#pageHeader {{
  background: transparent;
  border: none;
}}
QFrame#headerRule {{
  background: {c['separator']};
  border: none;
  max-height: 1px;
  min-height: 1px;
}}
QLabel#pageTitle {{
  font-size: 22px;
  font-weight: 800;
  color: {c['title']};
  letter-spacing: -0.2px;
}}
QLabel#sectionTitle {{
  font-size: 13px;
  font-weight: 700;
  color: {c['accent_text']};
  letter-spacing: 0.2px;
}}
QLabel#heroValue {{
  font-size: 18px;
  font-weight: 800;
  color: {c['accent_text']};
  font-family: {mono};
}}
QLabel#title {{
  font-size: 20px;
  font-weight: 700;
  color: {c['title']};
}}
QLabel#subtitle {{
  color: {c['subtitle']};
  font-size: 12.5px;
  line-height: 1.4;
}}
QLabel#muted {{
  color: {c['muted']};
  font-size: 12px;
}}
QLabel#pill {{
  background: {c['accent_soft']};
  border: 1px solid {c['pill_border']};
  border-radius: 999px;
  padding: 5px 12px;
  color: {c['accent_text']};
  font-size: 11.5px;
  font-weight: 600;
}}
QStackedWidget#pages {{
  background: transparent;
  border: none;
}}

/* ========== Cards / Groups ========== */
QFrame#card {{
  background: {c['card_bg']};
  border: 1px solid {c['card_border']};
  border-radius: {radius};
}}
QFrame#card[elevated="true"] {{
  border: 1px solid {c['card_hover_border']};
}}
QFrame#dropZone {{
  background: {c['drop_bg']};
  border: 1.5px dashed {c['drop_border']};
  border-radius: {radius};
}}
QFrame#dropZone[active="true"] {{
  border: 1.5px dashed {drop_active_border};
  background: {c['drop_active_bg']};
}}
QGroupBox {{
  border: 1px solid {c['group_border']};
  border-radius: {radius};
  margin-top: 14px;
  padding-top: 14px;
  background: {c['card_bg']};
  font-weight: 600;
}}
QGroupBox::title {{
  subcontrol-origin: margin;
  left: 14px;
  padding: 0 8px;
  color: {c['group_title']};
  font-weight: 700;
  font-size: 12.5px;
}}

/* ========== Tabs ========== */
QTabWidget::pane {{
  border: 1px solid {c['card_border']};
  border-radius: {radius_sm};
  top: -1px;
  background: {c['pane']};
  padding: 4px;
}}
QTabBar::tab {{
  background: {c['tab']};
  color: {c['tab_text']};
  padding: 9px 16px;
  margin-right: 4px;
  border-top-left-radius: {radius_xs};
  border-top-right-radius: {radius_xs};
  border: 1px solid transparent;
  font-weight: 500;
}}
QTabBar::tab:hover {{
  color: {c['text']};
  background: {c['nav_item_hover']};
}}
QTabBar::tab:selected {{
  background: {c['tab_selected']};
  color: {c['tab_selected_text']};
  border: 1px solid {c['card_border']};
  border-bottom-color: {c['tab_selected']};
  font-weight: 600;
}}

/* ========== Inputs ========== */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QListWidget, QTableWidget, QTextBrowser, QTreeWidget {{
  background: {c['input_bg']};
  border: 1px solid {c['input_border']};
  border-radius: {radius_xs};
  padding: 7px 10px;
  color: {c['text']};
  selection-background-color: {c['selection']};
  selection-color: #ffffff;
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QComboBox:focus {{
  border: 1px solid {c['primary']};
  background: {c['input_bg']};
}}
QLineEdit:hover, QComboBox:hover, QSpinBox:hover {{
  border: 1px solid {c['card_hover_border']};
}}
QComboBox::drop-down {{
  border: none;
  width: 28px;
}}
QComboBox::down-arrow {{
  width: 0;
  height: 0;
  border-left: 4px solid transparent;
  border-right: 4px solid transparent;
  border-top: 5px solid {c['subtitle']};
  margin-right: 10px;
}}
QComboBox QAbstractItemView {{
  background: {c['menu_bg']};
  border: 1px solid {c['card_border']};
  border-radius: 8px;
  selection-background-color: {c['primary']};
  selection-color: #ffffff;
  outline: none;
  padding: 4px;
}}
QSpinBox::up-button, QSpinBox::down-button {{
  background: {c['btn_secondary_bg']};
  border: none;
  width: 18px;
}}
QHeaderView::section {{
  background: {c['header_bg']};
  color: {c['header_text']};
  padding: 8px 10px;
  border: none;
  border-right: 1px solid {c['input_border']};
  border-bottom: 1px solid {c['input_border']};
  font-weight: 600;
  font-size: 12px;
}}
QTableWidget {{
  gridline-color: {c['separator']};
  alternate-background-color: {c['table_alt']};
  outline: none;
}}
QTableWidget::item {{
  padding: 6px 8px;
}}
QTableWidget::item:selected, QListWidget::item:selected {{
  background: {c['primary']};
  color: #ffffff;
}}
QListWidget::item {{
  border-radius: 6px;
  padding: 6px 8px;
  margin: 1px 0;
}}
QListWidget::item:hover {{
  background: {c['nav_item_hover']};
}}
QPlainTextEdit, QTextEdit, QTextBrowser {{
  font-family: {mono};
  font-size: 12.5px;
  line-height: 1.45;
}}

/* ========== Buttons ========== */
QToolButton {{
  background: {c['btn_secondary_bg']};
  border: 1px solid {c['btn_secondary_border']};
  border-radius: {radius_xs};
  padding: 8px 12px;
  color: {c['text']};
  font-weight: 600;
}}
QToolButton:hover {{
  background: {c['btn_secondary_hover']};
  border-color: {c['card_hover_border']};
}}
QToolButton:pressed {{
  background: {c['tab']};
}}
QToolButton::menu-indicator {{
  image: none;
  width: 0;
}}
QPushButton {{
  background: {c['primary']};
  color: white;
  border: none;
  border-radius: {radius_xs};
  padding: 8px 16px;
  font-weight: 600;
  min-height: 18px;
}}
QPushButton:hover {{
  background: {c['primary_hover']};
}}
QPushButton:pressed {{
  background: {c['primary_pressed']};
}}
QPushButton:disabled {{
  background: {c['btn_disabled_bg']};
  color: {c['btn_disabled_text']};
}}
QPushButton[secondary="true"] {{
  background: {c['btn_secondary_bg']};
  border: 1px solid {c['btn_secondary_border']};
  color: {c['text']};
}}
QPushButton[secondary="true"]:hover {{
  background: {c['btn_secondary_hover']};
  border-color: {c['card_hover_border']};
}}
QPushButton[secondary="true"]:pressed {{
  background: {c['tab']};
}}
QPushButton[danger="true"] {{
  background: {c['danger']};
  color: white;
  border: none;
}}
QPushButton[danger="true"]:hover {{
  background: {c['danger_hover']};
}}
QPushButton[success="true"] {{
  background: {c['success']};
  color: white;
  border: none;
}}
QPushButton[success="true"]:hover {{
  background: {c['success_hover']};
}}
QPushButton[large="true"] {{
  padding: 12px 20px;
  font-size: 14px;
  border-radius: {radius_sm};
}}
QPushButton[ghost="true"] {{
  background: transparent;
  border: 1px solid transparent;
  color: {c['subtitle']};
}}
QPushButton[ghost="true"]:hover {{
  background: {c['nav_item_hover']};
  color: {c['text']};
}}

/* ========== Chrome ========== */
QStatusBar {{
  background: {c['status_bg']};
  color: {c['status_text']};
  border-top: 1px solid {c['sidebar_border']};
  font-size: 12px;
  padding: 2px 8px;
}}
QStatusBar::item {{
  border: none;
}}
QToolBar {{
  background: {c['toolbar_bg']};
  border-bottom: 1px solid {c['toolbar_border']};
  spacing: 8px;
  padding: 8px;
}}
QCheckBox, QRadioButton {{
  color: {c['text']};
  spacing: 8px;
}}
QCheckBox::indicator, QRadioButton::indicator {{
  width: 16px;
  height: 16px;
  border-radius: 4px;
  border: 1px solid {c['input_border']};
  background: {c['input_bg']};
}}
QCheckBox::indicator:checked {{
  background: {c['primary']};
  border: 1px solid {c['primary']};
}}
QRadioButton::indicator {{
  border-radius: 8px;
}}
QRadioButton::indicator:checked {{
  background: {c['primary']};
  border: 1px solid {c['primary']};
}}
QMenu {{
  background: {c['menu_bg']};
  color: {c['text']};
  border: 1px solid {c['card_border']};
  border-radius: 10px;
  padding: 6px;
}}
QMenu::item {{
  padding: 8px 28px 8px 14px;
  border-radius: 6px;
  margin: 1px 2px;
}}
QMenu::item:selected {{
  background: {c['primary']};
  color: white;
}}
QMenu::separator {{
  height: 1px;
  background: {c['separator']};
  margin: 5px 8px;
}}
QDialog {{
  background: {c['window']};
  color: {c['text']};
}}
QProgressBar {{
  border: 1px solid {c['input_border']};
  border-radius: 8px;
  background: {c['input_bg']};
  text-align: center;
  color: {c['text']};
  min-height: 14px;
  max-height: 18px;
}}
QProgressBar::chunk {{
  background: {c['primary']};
  border-radius: 7px;
}}
QScrollArea {{
  border: none;
  background: transparent;
}}
QScrollBar:vertical {{
  background: transparent;
  width: 10px;
  margin: 2px;
}}
QScrollBar::handle:vertical {{
  background: {c['scroll']};
  border-radius: 5px;
  min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{
  background: {c['scroll_hover']};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical,
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
  background: none;
  height: 0;
}}
QScrollBar:horizontal {{
  background: transparent;
  height: 10px;
  margin: 2px;
}}
QScrollBar::handle:horizontal {{
  background: {c['scroll']};
  border-radius: 5px;
  min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{
  background: {c['scroll_hover']};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal,
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
  background: none;
  width: 0;
}}
QSplitter::handle {{
  background: {c['separator']};
}}
QAbstractItemView {{
  outline: none;
}}
"""


def palette_colors(mode: str = "night", accent: str = "blue") -> dict[str, str]:
    mode = normalize_mode(mode)
    accent = normalize_accent(accent)
    c = MODES[mode]
    a = ACCENTS[accent]
    return {
        "window": c["palette_window"],
        "text": c["palette_text"],
        "base": c["palette_base"],
        "button": a["primary"],
        "button_text": c["palette_button_text"],
        "highlight": a["primary"],
    }


def apply_app_font(app: Any) -> None:
    """Set application-wide QFont with platform-friendly defaults."""
    try:
        from PySide6.QtGui import QFont

        if sys.platform == "darwin":
            family = ".AppleSystemUIFont"
            size = 13
        elif sys.platform == "win32":
            family = "Segoe UI"
            size = 10  # Windows point size ~ visual 13px
        else:
            family = "Noto Sans"
            size = 10
        font = QFont(family, size)
        font.setStyleHint(QFont.SansSerif)
        font.setHintingPreference(QFont.PreferDefaultHinting)
        app.setFont(font)
    except Exception:
        pass
