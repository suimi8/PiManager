"""Design tokens for the modern Pi Manager presentation layer."""
from __future__ import annotations

from dataclasses import dataclass


ACCENTS: dict[str, tuple[str, str, str]] = {
    "blue": ("#4F8CFF", "#6EA3FF", "#326FE0"),
    "green": ("#22C55E", "#4ADE80", "#16A34A"),
    "purple": ("#A855F7", "#C084FC", "#9333EA"),
    "orange": ("#F59E0B", "#FBBF24", "#D97706"),
    "cyan": ("#06B6D4", "#22D3EE", "#0891B2"),
}


@dataclass(frozen=True, slots=True)
class DesignTokens:
    mode: str
    accent_name: str
    window: str
    sidebar: str
    surface: str
    surface_raised: str
    surface_hover: str
    input: str
    border: str
    border_strong: str
    text: str
    text_secondary: str
    text_muted: str
    accent: str
    accent_hover: str
    accent_pressed: str
    accent_soft: str
    accent_text: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    info: str
    info_soft: str
    selection_text: str
    shadow: str


def normalize_mode(mode: str | None) -> str:
    value = str(mode or "night").strip().lower()
    return "day" if value in {"day", "light", "白天"} else "night"


def normalize_accent(accent: str | None) -> str:
    value = str(accent or "blue").strip().lower()
    return value if value in ACCENTS else "blue"


def tokens_for(mode: str | None = None, accent: str | None = None) -> DesignTokens:
    mode_name = normalize_mode(mode)
    accent_name = normalize_accent(accent)
    primary, hover, pressed = ACCENTS[accent_name]
    if mode_name == "day":
        return DesignTokens(
            mode=mode_name,
            accent_name=accent_name,
            window="#F4F6F8",
            sidebar="#FFFFFF",
            surface="#FFFFFF",
            surface_raised="#FFFFFF",
            surface_hover="#F7F9FC",
            input="#FFFFFF",
            border="#E2E7EE",
            border_strong="#CBD3DE",
            text="#151A23",
            text_secondary="#536071",
            text_muted="#7C8797",
            accent=primary,
            accent_hover=hover,
            accent_pressed=pressed,
            accent_soft="#EAF1FF" if accent_name == "blue" else "#F1F5F9",
            accent_text=pressed,
            success="#16A34A",
            success_soft="#EAF8EF",
            warning="#D97706",
            warning_soft="#FFF6E5",
            danger="#DC2626",
            danger_soft="#FFF0F0",
            info="#2563EB",
            info_soft="#EAF1FF",
            selection_text="#FFFFFF",
            shadow="#160F172A",
        )
    return DesignTokens(
        mode=mode_name,
        accent_name=accent_name,
        window="#090C10",
        sidebar="#0D1117",
        surface="#11161D",
        surface_raised="#151B24",
        surface_hover="#1A222D",
        input="#0C1117",
        border="#242C38",
        border_strong="#344052",
        text="#F2F5F8",
        text_secondary="#B3BECC",
        text_muted="#7F8B9D",
        accent=primary,
        accent_hover=hover,
        accent_pressed=pressed,
        accent_soft="#17243B" if accent_name == "blue" else "#192128",
        accent_text="#9FC0FF" if accent_name == "blue" else hover,
        success="#35C56F",
        success_soft="#102A1B",
        warning="#F5B942",
        warning_soft="#2D2310",
        danger="#FF6673",
        danger_soft="#32171B",
        info="#67A0FF",
        info_soft="#17243B",
        selection_text="#FFFFFF",
        shadow="#80000000",
    )
