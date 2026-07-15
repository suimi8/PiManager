"""Pi Manager window themes (night / day + accent colors)."""
from __future__ import annotations

from typing import Any

# mode: night | day
# accent: blue | green | purple | orange | cyan

ACCENTS = {
    "blue": {"primary": "#1f6feb", "primary_hover": "#388bfd", "pill_border": "#2f6fed", "accent_text": "#8ec8ff"},
    "green": {"primary": "#238636", "primary_hover": "#2ea043", "pill_border": "#3fb950", "accent_text": "#7ee787"},
    "purple": {"primary": "#8957e5", "primary_hover": "#a371f7", "pill_border": "#a371f7", "accent_text": "#d2a8ff"},
    "orange": {"primary": "#d29922", "primary_hover": "#e3b341", "pill_border": "#e3b341", "accent_text": "#f2cc60"},
    "cyan": {"primary": "#1b7c83", "primary_hover": "#2cb5c0", "pill_border": "#39c5cf", "accent_text": "#56d4dd"},
}

MODES = {
    "night": {
        "window": "#0d1117",
        "text": "#e6edf3",
        "subtitle": "#8b9cb3",
        "pane": "#161b22",
        "tab": "#21262d",
        "tab_text": "#8b9cb3",
        "tab_selected": "#30363d",
        "tab_selected_text": "#ffffff",
        "group_border": "#30363d",
        "group_title": None,
        "input_bg": "#0d1117",
        "input_border": "#30363d",
        "header_bg": "#21262d",
        "header_text": "#c9d1d9",
        "btn_disabled_bg": "#30363d",
        "btn_disabled_text": "#8b949e",
        "btn_secondary_bg": "#21262d",
        "btn_secondary_border": "#30363d",
        "btn_secondary_hover": "#30363d",
        "danger": "#da3633",
        "danger_hover": "#f85149",
        "success": "#238636",
        "success_hover": "#2ea043",
        "status_bg": "#010409",
        "status_text": "#8b9cb3",
        "toolbar_bg": "#010409",
        "toolbar_border": "#21262d",
        "title": "#ffffff",
        "card_bg": "#161b22",
        "card_border": "#30363d",
        "drop_bg": "#121820",
        "drop_border": "#3d4f66",
        "drop_active_bg": "#152238",
        "selection": "#2f6fed",
        "sidebar_bg": "#010409",
        "sidebar_border": "#21262d",
        "nav_item": "transparent",
        "nav_item_hover": "#161b22",
        "nav_item_text": "#8b9cb3",
        "nav_item_selected_text": "#ffffff",
        "content_bg": "#0d1117",
        "palette_window": "#0d1117",
        "palette_text": "#e6edf3",
        "palette_base": "#0d1117",
        "palette_button": None,
        "palette_button_text": "#ffffff",
        "palette_highlight": None,
    },
    "day": {
        "window": "#f6f8fa",
        "text": "#1f2328",
        "subtitle": "#656d76",
        "pane": "#ffffff",
        "tab": "#eaeef2",
        "tab_text": "#656d76",
        "tab_selected": "#ffffff",
        "tab_selected_text": "#1f2328",
        "group_border": "#d0d7de",
        "group_title": None,
        "input_bg": "#ffffff",
        "input_border": "#d0d7de",
        "header_bg": "#eaeef2",
        "header_text": "#1f2328",
        "btn_disabled_bg": "#d0d7de",
        "btn_disabled_text": "#8c959f",
        "btn_secondary_bg": "#f6f8fa",
        "btn_secondary_border": "#d0d7de",
        "btn_secondary_hover": "#eaeef2",
        "danger": "#cf222e",
        "danger_hover": "#a40e26",
        "success": "#1a7f37",
        "success_hover": "#116329",
        "status_bg": "#ffffff",
        "status_text": "#656d76",
        "toolbar_bg": "#ffffff",
        "toolbar_border": "#d0d7de",
        "title": "#1f2328",
        "card_bg": "#ffffff",
        "card_border": "#d0d7de",
        "drop_bg": "#f6f8fa",
        "drop_border": "#afb8c1",
        "drop_active_bg": "#ddf4ff",
        "selection": "#0969da",
        "sidebar_bg": "#ffffff",
        "sidebar_border": "#d0d7de",
        "nav_item": "transparent",
        "nav_item_hover": "#eaeef2",
        "nav_item_text": "#656d76",
        "nav_item_selected_text": "#ffffff",
        "content_bg": "#f6f8fa",
        "palette_window": "#f6f8fa",
        "palette_text": "#1f2328",
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


def normalize_mode(mode: str | None) -> str:
    m = (mode or "night").lower()
    if m in {"light", "day", "白天"}:
        return "day"
    return "night"


def normalize_accent(accent: str | None) -> str:
    a = (accent or "blue").lower()
    return a if a in ACCENTS else "blue"


def build_stylesheet(mode: str = "night", accent: str = "blue") -> str:
    mode = normalize_mode(mode)
    accent = normalize_accent(accent)
    c = dict(MODES[mode])
    a = ACCENTS[accent]
    c["primary"] = a["primary"]
    c["primary_hover"] = a["primary_hover"]
    c["pill_border"] = a["pill_border"]
    c["accent_text"] = a["accent_text"]
    c["group_title"] = a["accent_text"]
    c["selection"] = a["primary"]
    drop_active_border = a["primary"]

    return f"""
QMainWindow, QWidget {{ background: {c['window']}; color: {c['text']}; font-size: 13px; }}
QFrame#sidebar QPushButton {{
  padding: 8px 10px;
  font-size: 12px;
}}
QFrame#sidebar {{
  background: {c['sidebar_bg']};
  border-right: 1px solid {c['sidebar_border']};
}}
QLabel#brandIcon {{
  background: transparent;
  border-radius: 10px;
}}
QFrame#pageHeader {{
  background: transparent;
  border: none;
}}
QFrame#headerRule {{
  background: {c['card_border']};
  border: none;
  max-height: 1px;
}}
QLabel#navBrand {{
  font-size: 18px;
  font-weight: 800;
  color: {c['title']};
  padding: 4px 2px 0 2px;
}}
QLabel#navTag {{
  color: {c['subtitle']};
  font-size: 11px;
  padding: 0 2px 8px 2px;
}}
QListWidget#sideNav {{
  background: transparent;
  border: none;
  outline: none;
  padding: 4px 8px;
}}
QListWidget#sideNav::item {{
  background: {c['nav_item']};
  color: {c['nav_item_text']};
  border-radius: 10px;
  padding: 10px 12px;
  margin: 2px 0;
}}
QListWidget#sideNav::item:hover {{
  background: {c['nav_item_hover']};
  color: {c['text']};
}}
QListWidget#sideNav::item:selected {{
  background: {c['primary']};
  color: {c['nav_item_selected_text']};
  font-weight: 600;
}}
QFrame#contentShell {{
  background: {c['content_bg']};
  border: none;
}}
QLabel#pageTitle {{
  font-size: 20px;
  font-weight: 800;
  color: {c['title']};
}}
QLabel#sectionTitle {{
  font-size: 14px;
  font-weight: 700;
  color: {c['accent_text']};
}}
QLabel#heroValue {{
  font-size: 20px;
  font-weight: 800;
  color: {c['accent_text']};
}}
QStackedWidget#pages {{
  background: transparent;
  border: none;
}}
QTabWidget::pane {{ border: 1px solid {c['card_border']}; border-radius: 8px; top: -1px; background: {c['pane']}; }}
QTabBar::tab {{ background: {c['tab']}; color: {c['tab_text']}; padding: 8px 14px; margin-right: 4px; border-top-left-radius: 8px; border-top-right-radius: 8px; }}
QTabBar::tab:selected {{ background: {c['tab_selected']}; color: {c['tab_selected_text']}; }}
QGroupBox {{ border: 1px solid {c['group_border']}; border-radius: 10px; margin-top: 12px; padding-top: 10px; background: {c['card_bg']}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {c['group_title']}; font-weight: 700; }}
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QComboBox, QListWidget, QTableWidget, QTextBrowser {{
  background: {c['input_bg']}; border: 1px solid {c['input_border']}; border-radius: 8px; padding: 6px 8px; color: {c['text']}; selection-background-color: {c['selection']};
}}
QHeaderView::section {{ background: {c['header_bg']}; color: {c['header_text']}; padding: 6px; border: none; border-right: 1px solid {c['input_border']}; }}
QTableWidget {{ gridline-color: {c['input_border']}; alternate-background-color: {c['tab']}; }}
QToolButton {{ background: {c['btn_secondary_bg']}; border: 1px solid {c['btn_secondary_border']}; border-radius: 8px; padding: 8px 12px; color: {c['text']}; font-weight: 600; }}
QToolButton:hover {{ background: {c['btn_secondary_hover']}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}
QPushButton {{
  background: {c['primary']}; color: white; border: none; border-radius: 8px; padding: 8px 14px; font-weight: 600;
}}
QPushButton:hover {{ background: {c['primary_hover']}; }}
QPushButton:disabled {{ background: {c['btn_disabled_bg']}; color: {c['btn_disabled_text']}; }}
QPushButton[secondary="true"] {{ background: {c['btn_secondary_bg']}; border: 1px solid {c['btn_secondary_border']}; color: {c['text']}; }}
QPushButton[secondary="true"]:hover {{ background: {c['btn_secondary_hover']}; }}
QPushButton[danger="true"] {{ background: {c['danger']}; color: white; }}
QPushButton[danger="true"]:hover {{ background: {c['danger_hover']}; }}
QPushButton[success="true"] {{ background: {c['success']}; color: white; }}
QPushButton[success="true"]:hover {{ background: {c['success_hover']}; }}
QPushButton[large="true"] {{ padding: 12px 18px; font-size: 14px; }}
QStatusBar {{ background: {c['status_bg']}; color: {c['status_text']}; }}
QToolBar {{ background: {c['toolbar_bg']}; border-bottom: 1px solid {c['toolbar_border']}; spacing: 8px; padding: 6px; }}
QLabel#title {{ font-size: 20px; font-weight: 700; color: {c['title']}; }}
QLabel#subtitle {{ color: {c['subtitle']}; }}
QLabel#pill {{ background: {c['tab']}; border: 1px solid {c['pill_border']}; border-radius: 999px; padding: 4px 10px; color: {c['accent_text']}; }}
QFrame#card {{ background: {c['card_bg']}; border: 1px solid {c['card_border']}; border-radius: 12px; }}
QFrame#dropZone {{ background: {c['drop_bg']}; border: 2px dashed {c['drop_border']}; border-radius: 12px; }}
QFrame#dropZone[active="true"] {{ border: 2px dashed {drop_active_border}; background: {c['drop_active_bg']}; }}
QCheckBox {{ color: {c['text']}; spacing: 8px; }}
QMenu {{ background: {c['pane']}; color: {c['text']}; border: 1px solid {c['card_border']}; }}
QMenu::item:selected {{ background: {c['selection']}; color: white; }}
QDialog {{ background: {c['window']}; color: {c['text']}; }}
QProgressBar {{ border: 1px solid {c['input_border']}; border-radius: 6px; background: {c['input_bg']}; text-align: center; color: {c['text']}; }}
QProgressBar::chunk {{ background: {c['primary']}; border-radius: 6px; }}
QScrollArea {{ border: none; background: transparent; }}
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
