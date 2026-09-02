"""语言、CLI 主题、UI 主题与首次引导。"""
from __future__ import annotations

import sys
import re
from pathlib import Path
from typing import Any

from . import storage


def _core():
    from . import core

    return core




# Language / install / theme helpers
# ---------------------------------------------------------------------------

LANG_ZH_PROMPT = """## 语言偏好（必须遵守）
- 请尽可能使用简体中文与用户交流、解释、写说明与文档。
- 仅当中文无法准确表达时才保留英文（如 API 名、协议字段、库名、错误码、固定术语），并尽量附简短中文说明。
- 代码标识符、命令、路径、配置键名保持原样，不要翻译。
- 回答优先中文，结构清晰，避免无必要的英文整段输出。
"""


LANG_EN_PROMPT = """## Language preference
- Prefer clear English for explanations and documentation.
- Keep code identifiers, commands, paths, and config keys unchanged.
"""


LANG_PROMPTS = {
    "zh-CN": LANG_ZH_PROMPT,
    "en": LANG_EN_PROMPT,
}



def get_language() -> str:
    cfg = _core().load_manager_config()
    lang = str(cfg.get("language") or "zh-CN")
    return lang if lang in LANG_PROMPTS or lang == "auto" else "zh-CN"



def set_language(lang: str) -> None:
    def _apply(cfg: dict[str, Any]) -> Any:
        cfg["language"] = lang
        return cfg

    _core().update_manager_config(_apply)
    apply_language_preference(lang)



def language_prompt_text(lang: str | None = None) -> str:
    lang = lang or get_language()
    if lang == "auto":
        return ""
    return LANG_PROMPTS.get(lang, LANG_ZH_PROMPT)



def agents_md_path() -> Path:
    return _core().pi_agent_dir() / "AGENTS.md"



_LANG_BLOCK_RE = re.compile(
    r"<!-- PI-MANAGER-LANG-START -->.*?<!-- PI-MANAGER-LANG-END -->\n?",
    re.DOTALL,
)



def apply_language_preference(lang: str | None = None) -> Path:
    """Write global AGENTS.md language block so Pi sessions use the preference.

    ``~/.pi/agent/AGENTS.md`` 是**用户的全局 Pi 指令文件**，可能有大量手写内容，
    所以读-改-写整体走 ``storage.locked`` + ``storage.save_text``：
    - 原子替换：``Path.write_text`` 是先截断再写，中途崩溃/磁盘满会把文件截成半截；
    - 持锁：GUI 与 ``--config-mutate`` helper 进程可能同时改写，后写者整份覆盖；
    - 备份轮转：出错后还有 ``AGENTS.md.bak.1/.bak.2`` 可退。
    """
    lang = lang or get_language()
    _core().ensure_agent_dir()
    path = agents_md_path()
    with storage.locked(path):
        # Tolerate non-UTF-8 bytes (e.g. a user edited AGENTS.md as GBK/ANSI on
        # Windows): replacing bad bytes lets the save proceed and rewrites the
        # file as clean UTF-8, instead of crashing the language preference action.
        existing = (
            path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
        )
        existing = _LANG_BLOCK_RE.sub("", existing).rstrip() + "\n"
        body = language_prompt_text(lang).strip()
        if body:
            block = (
                "\n<!-- PI-MANAGER-LANG-START -->\n"
                f"{body}\n"
                "<!-- PI-MANAGER-LANG-END -->\n"
            )
            existing = existing.rstrip() + "\n" + block
        storage.save_text(
            path,
            existing.lstrip() + ("\n" if not existing.endswith("\n") else ""),
        )
    return path



def append_language_args(args: list[str], lang: str | None = None) -> list[str]:
    """Add --append-system-prompt for launch-time language enforcement."""
    text = language_prompt_text(lang)
    if text.strip():
        args = list(args) + ["--append-system-prompt", text.strip()]
    return args



def _helper_command_text() -> str:
    """Shell-quoted helper command for prompts/skills (single source of truth)."""
    import shlex

    try:
        from .helper_registry import current_helper_command

        return " ".join(shlex.quote(str(part)) for part in current_helper_command())
    except Exception:
        if getattr(sys, "frozen", False):
            return shlex.quote(sys.executable)
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        return f"{shlex.quote(sys.executable)} {shlex.quote(str(main_path))}"



def _vision_rule_prompt() -> str:
    """System-prompt rule appended when launching Pi: images are always
    routed through the free Zhipu vision model first — never sent straight to
    a (possibly text-only) provider model."""
    command = _helper_command_text()
    return (
        "## 图片处理规则（必须遵守）\n"
        "- 用户发送图片、粘贴截图或要求查看图片时：绝不要尝试把图片直接发送给当前对话模型"
        "（纯文本模型会报 image_url 错误）。\n"
        "- 先获取图片文件路径：用户提供，或从系统临时目录（%TEMP%）查找最新的 "
        "pi-clipboard-*.png 文件。\n"
        f"- 运行识图命令：{command} --vision-describe \"<图片路径>\" \"<用户问题，可空>\"\n"
        "- 将命令输出的文字描述视为图片内容，结合用户问题回答。\n"
        "- 若提示未配置智谱 API Key，请告知用户在 Pi Manager「设置 → 识图模型」中配置。"
    )



def append_vision_args(args: list[str]) -> list[str]:
    """Add the image-routing system-prompt rule at launch time."""
    try:
        text = _vision_rule_prompt()
    except Exception:
        return list(args)
    return list(args) + ["--append-system-prompt", text]



def apply_theme(theme_name: str) -> dict[str, Any]:
    from .builtin_themes import ensure_builtin_themes

    ensure_builtin_themes()

    def _apply(settings: dict[str, Any]) -> Any:
        settings["theme"] = theme_name
        return settings

    return _core().update_settings(_apply)



def get_theme() -> str:
    return str(_core().load_settings().get("theme") or "dark")



def list_themes() -> list[tuple[str, str]]:
    from .builtin_themes import list_theme_choices

    return list_theme_choices()


def is_setup_done() -> bool:
    return bool(_core().load_manager_config().get("setup_done"))



def mark_setup_done(done: bool = True) -> None:
    def _apply(cfg: dict[str, Any]) -> Any:
        cfg["setup_done"] = bool(done)
        return cfg

    _core().update_manager_config(_apply)



def run_first_time_bootstrap() -> None:
    """Ensure language block + themes exist."""
    from .builtin_themes import ensure_builtin_themes

    ensure_builtin_themes()
    apply_language_preference(get_language())


def normalize_ui_mode(mode: str | None) -> str:
    value = str(mode or "night").strip().lower()
    return "day" if value in {"day", "light", "\u767d\u5929"} else "night"



def cli_theme_for_ui_mode(mode: str | None) -> str:
    """Map the global UI mode to Pi CLI's matching built-in theme."""
    return "light" if normalize_ui_mode(mode) == "day" else "dark"



def sync_cli_theme_with_ui(mode: str | None = None) -> str:
    """Persist Pi CLI's theme so it always follows the manager's global mode."""
    normalized = normalize_ui_mode(mode or get_ui_theme().get("mode"))
    theme = cli_theme_for_ui_mode(normalized)

    def _apply(settings: dict[str, Any]) -> Any:
        # 「是否需要改」的判定必须在锁内做，否则锁外预检可能被并发写入推翻。
        if settings.get("theme") == theme:
            return storage.UNCHANGED
        settings["theme"] = theme
        return settings

    _core().update_settings(_apply)
    return theme



_UI_ACCENTS = frozenset({"blue", "green", "purple", "orange", "cyan"})



def _normalize_ui_accent(accent: Any) -> str:
    value = str(accent or "blue").strip().lower()
    return value if value in _UI_ACCENTS else "blue"



def get_ui_theme() -> dict[str, str]:
    cfg = _core().load_manager_config()
    return {
        "mode": normalize_ui_mode(str(cfg.get("ui_mode") or "night")),
        "accent": _normalize_ui_accent(cfg.get("ui_accent")),
    }



def set_ui_theme(mode: str | None = None, accent: str | None = None) -> dict[str, str]:
    result: dict[str, str] = {}

    def _apply(cfg: dict[str, Any]) -> Any:
        nonlocal result
        # 「未指定则保留当前值」的当前值从锁内的同一份快照里读，而不是另做一次
        # _core().load_manager_config() —— 否则两次读之间的并发写入会被这次整份覆盖回退。
        mode_name = normalize_ui_mode(
            mode if mode is not None else str(cfg.get("ui_mode") or "night")
        )
        accent_name = _normalize_ui_accent(
            accent if accent is not None else cfg.get("ui_accent")
        )
        cfg["ui_mode"] = mode_name
        cfg["ui_accent"] = accent_name
        result = {"mode": mode_name, "accent": accent_name}
        return cfg

    _core().update_manager_config(_apply)
    # 锁已释放后再同步 CLI 主题：settings.json 与 pi-manager.json 的锁不嵌套，
    # 避免与其它「先 settings 后 manager」的调用方构成跨进程 ABBA 死锁。
    sync_cli_theme_with_ui(result["mode"])
    return result
