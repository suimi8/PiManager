"""pi_manager.builtin_themes 的单元测试。

覆盖 themes_dir / ensure_builtin_themes / list_theme_choices 三个公开入口，
以及 BUILTIN_THEMES / THEME_LABELS 两个常量的基本不变量。
"""
from __future__ import annotations

import json

import pytest

from pi_manager import builtin_themes
from pi_manager.builtin_themes import (
    BUILTIN_THEMES,
    THEME_LABELS,
    ensure_builtin_themes,
    list_theme_choices,
    themes_dir,
)


# ---------------------------------------------------------------------------
# 1. themes_dir() 落点
# ---------------------------------------------------------------------------
def test_themes_dir_under_agent_dir(isolated_home):
    """themes_dir() 必须落在 pi_agent_dir() 内的 themes 子目录。"""
    from pi_manager import core

    expected = core.pi_agent_dir() / "themes"
    assert themes_dir() == expected
    # 路径必须位于 isolated_home 之下（isolated_home 被 HOME/USERPROFILE 重定向）
    assert str(themes_dir()).startswith(str(isolated_home))


# ---------------------------------------------------------------------------
# 2. ensure_builtin_themes() 落盘
# ---------------------------------------------------------------------------
def test_ensure_builtin_themes_creates_files(isolated_home):
    """首次调用应把每个内置主题写到 themes_dir 下 {name}.json。"""
    written = ensure_builtin_themes()
    d = themes_dir()
    assert d.is_dir()
    # 返回值应列出所有已写入主题（首次调用即全部内置主题）
    assert set(written) == set(BUILTIN_THEMES)
    for name in BUILTIN_THEMES:
        assert (d / f"{name}.json").is_file()


# ---------------------------------------------------------------------------
# 3. 幂等性
# ---------------------------------------------------------------------------
def test_ensure_builtin_themes_idempotent(isolated_home):
    """调用两次不应报错，第二次不应重复写文件（返回空列表），且文件内容完整。"""
    first = ensure_builtin_themes()
    assert set(first) == set(BUILTIN_THEMES)

    second = ensure_builtin_themes()
    assert second == [], "已存在的主题文件不应再次写入"

    d = themes_dir()
    for name in BUILTIN_THEMES:
        path = d / f"{name}.json"
        # 文件仍是合法 JSON 且内容与常量一致
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data == BUILTIN_THEMES[name]


# ---------------------------------------------------------------------------
# 4. list_theme_choices() 返回内置主题
# ---------------------------------------------------------------------------
def test_list_theme_choices_returns_builtin(isolated_home):
    """list_theme_choices 返回的列表必须包含全部内置主题名（value 标签非空）。"""
    choices = list_theme_choices()
    names = [n for n, _ in choices]
    # dark / light 永远在前
    assert names[0] == "dark"
    assert names[1] == "light"
    # 所有 THEME_LABELS 中的 key（含 dark/light）都应出现
    for key in THEME_LABELS:
        assert key in names
    # 额外内置主题（ocean/forest/...）也都在
    for key in BUILTIN_THEMES:
        assert key in names
    # 标签非空
    for _, label in choices:
        assert isinstance(label, str) and label


# ---------------------------------------------------------------------------
# 5. 损坏文件的容错
# ---------------------------------------------------------------------------
def test_list_theme_choices_skips_corrupt(isolated_home):
    """一个损坏的主题文件不应让 list_theme_choices 崩溃。

    list_theme_choices 只 glob 文件名 stem（不解析内容），所以损坏文件
    会被当作额外用户主题列入；关键是函数不得抛异常。
    """
    ensure_builtin_themes()
    d = themes_dir()
    bad = d / "broken-theme.json"
    bad.write_text("{ not valid json ", encoding="utf-8")

    # 不应抛异常
    choices = list_theme_choices()
    names = [n for n, _ in choices]
    assert "broken-theme" in names
    # 内置主题仍完整返回
    for key in BUILTIN_THEMES:
        assert key in names


# ---------------------------------------------------------------------------
# 6. 落盘文件是合法 JSON
# ---------------------------------------------------------------------------
def test_theme_file_content_valid_json(isolated_home):
    """落盘的每个主题文件都应是合法 JSON，且解析后与 BUILTIN_THEMES 一致。"""
    ensure_builtin_themes()
    d = themes_dir()
    for name, expected in BUILTIN_THEMES.items():
        path = d / f"{name}.json"
        raw = path.read_text(encoding="utf-8")
        # 必须能被 json.loads 解析
        parsed = json.loads(raw)
        assert parsed == expected, f"主题 {name} 落盘内容与常量不一致"


# ---------------------------------------------------------------------------
# 7. 原子写：已存在文件不被截断
# ---------------------------------------------------------------------------
def test_ensure_builtin_themes_atomic(isolated_home):
    """ensure_builtin_themes 对已存在文件应跳过（原子写保护）。

    预置一个完整且合法的主题文件，再调用 ensure_builtin_themes，验证：
    - 返回列表不含该主题（未覆盖）
    - 文件内容与预置值完全一致（未被截断或改写）
    """
    ensure_builtin_themes()
    d = themes_dir()
    target = d / "ocean.json"
    assert target.exists()
    # 记录原始内容
    original = target.read_text(encoding="utf-8")
    original_stat = target.stat().st_mtime_ns

    # 再次调用：已存在则跳过，不应触发写
    written = ensure_builtin_themes()
    assert "ocean" not in written

    assert target.read_text(encoding="utf-8") == original
    # mtime 不应变化（未重写文件）
    assert target.stat().st_mtime_ns == original_stat


def test_ensure_builtin_themes_does_not_overwrite_user_customization(isolated_home):
    """如果用户手动修改了某主题文件，ensure_builtin_themes 不应覆盖它。"""
    ensure_builtin_themes()
    d = themes_dir()
    target = d / "forest.json"
    # 模拟用户自定义内容（合法 JSON 但与内置不同）
    custom = json.loads(target.read_text(encoding="utf-8"))
    custom["name"] = "forest-customized"
    custom["vars"]["accent"] = "#000000"
    target.write_text(json.dumps(custom, ensure_ascii=False, indent=2), encoding="utf-8")

    written = ensure_builtin_themes()
    assert "forest" not in written
    after = json.loads(target.read_text(encoding="utf-8"))
    assert after["name"] == "forest-customized"
    assert after["vars"]["accent"] == "#000000"


# ---------------------------------------------------------------------------
# 常量不变量
# ---------------------------------------------------------------------------
def test_builtin_themes_constants_consistency():
    """BUILTIN_THEMES 的每个条目都应自带 name 字段且与 key 一致；
    THEME_LABELS 应覆盖所有内置主题。"""
    for key, data in BUILTIN_THEMES.items():
        assert data.get("name") == key, f"BUILTIN_THEMES[{key!r}].name 不匹配"
        assert "vars" in data and isinstance(data["vars"], dict)
        assert "colors" in data and isinstance(data["colors"], dict)
        assert "export" in data and isinstance(data["export"], dict)
    # 标签覆盖
    for key in BUILTIN_THEMES:
        assert key in THEME_LABELS
    # dark/light 只有标签、没有内置文件
    assert "dark" in THEME_LABELS and "dark" not in BUILTIN_THEMES
    assert "light" in THEME_LABELS and "light" not in BUILTIN_THEMES


def test_expected_builtin_theme_names_present():
    """期望的内置主题名都在（ocean/forest/rose/nord/mono/dracula）。"""
    expected = {"ocean", "forest", "rose", "nord", "mono", "dracula"}
    assert expected.issubset(BUILTIN_THEMES.keys())
