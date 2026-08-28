# -*- coding: utf-8 -*-
"""help_docs.markdown_to_html 手写解析器（标题/列表/表格/代码块/内联格式/转义）测试。

用代表性样例断言输出 HTML 结构，不追求像素级一致，保证手写解析器
的每个分支都有行为契约。
"""
from __future__ import annotations

import re
from pathlib import Path

from pi_manager import help_docs

REPO_ROOT = Path(__file__).resolve().parents[1]
TUTORIAL_DOC = REPO_ROOT / "docs" / "使用教程.md"


def test_h1_h2_h3_headings_rendered():
    html = help_docs.markdown_to_html("# 标题一\n\n## 标题二\n\n### 标题三\n")
    assert "<h1" in html and "标题一" in html
    assert "<h2" in html and "标题二" in html
    assert "<h3" in html and "标题三" in html
    # 标题必须经内联渲染（避免 raw markdown 泄漏）
    assert html.count("标题一") == 1


def test_unordered_list_rendered():
    html = help_docs.markdown_to_html("- 甲\n- 乙\n- 丙\n")
    assert "<ul" in html
    assert html.count("<li") == 3
    for item in ("甲", "乙", "丙"):
        assert item in html
    assert "</ul>" in html


def test_unordered_list_closes_before_paragraph():
    html = help_docs.markdown_to_html("- 甲\n\n普通段落\n")
    assert html.index("</ul>") < html.index("普通段落")


def test_table_with_separator_row_rendered_as_th_td():
    md = (
        "| 操作 | 说明 |\n"
        "|------|------|\n"
        "| 设为默认 | 双击模型行 |\n"
        "| 收藏 | 批量加入 |\n"
    )
    html = help_docs.markdown_to_html(md)
    assert "<table" in html
    assert "<tr>" in html
    assert "<th" in html and "操作" in html and "说明" in html
    assert "<td" in html and "设为默认" in html and "收藏" in html
    # 分隔行不渲染为单元格内容
    assert "------" not in html


def test_code_block_rendered_as_pre_and_escaped():
    md = "```text\n添加 Provider → 刷新\n<tag> & not-html\n```\n"
    html = help_docs.markdown_to_html(md)
    assert "<pre" in html and "</pre>" in html
    # 代码块内容 HTML 转义，不会变成真实标签
    assert "<tag>" not in html
    assert "&lt;tag&gt;" in html
    assert "&amp;" in html


def test_unclosed_code_block_still_closes():
    html = help_docs.markdown_to_html("```\ncode\n")
    assert "<pre" in html and "</pre>" in html


def test_inline_code_bold_italic_link_rendered():
    md = "用 `code` 和 **加粗** 与 *斜体*，参考 [链接](https://example.com/x)。\n"
    html = help_docs.markdown_to_html(md)
    assert "<code" in html and "code" in html and "</code>" in html
    assert "<b>加粗</b>" in html
    assert "<i>斜体</i>" in html
    assert '<a href="https://example.com/x"' in html
    assert ">链接</a>" in html
    assert "**" not in html.replace("<b>", "").replace("</b>", "")


def test_inline_escaping_prevents_html_injection():
    md = "攻击 <script>alert(1)</script> & <img src=x onerror=alert(1)>\n"
    html = help_docs.markdown_to_html(md)
    assert "<script>" not in html
    assert "<img" not in html
    assert "&lt;script&gt;" in html


def test_blockquote_rendered():
    html = help_docs.markdown_to_html("> 版本 1.0 · 说明\n")
    assert "<blockquote" in html and "版本 1.0" in html and "</blockquote>" in html


def test_horizontal_rule_rendered():
    html = help_docs.markdown_to_html("---\n")
    assert "<hr" in html


def test_blank_lines_become_breaks():
    html = help_docs.markdown_to_html("段落甲\n\n段落乙\n")
    assert html.count("<br/>") >= 1
    assert "段落甲" in html and "段落乙" in html


def test_ordered_list_items_render_as_paragraphs():
    # 该解析器把有序列表渲染为带缩进的 <p>，保持行为契约
    html = help_docs.markdown_to_html("1. 第一\n2. 第二\n")
    assert "第一" in html and "第二" in html
    assert "<li" not in html


def test_day_and_night_modes_differ_in_theme_colors():
    day = help_docs.markdown_to_html("# 标题\n", mode="day")
    night = help_docs.markdown_to_html("# 标题\n", mode="night")
    assert day != night
    # body 文本色：day 深字浅底、night 浅字深底（主题 token 体现在输出中）
    assert "color:#1f2937" in day.lower()
    assert "color:#e8eef7" in night.lower()
    # 代码块背景色随模式切换
    code_md = "`x` 和 ```\npre\n```\n"
    day_code = help_docs.markdown_to_html(code_md, mode="day").lower()
    night_code = help_docs.markdown_to_html(code_md, mode="night").lower()
    assert "background:#f3f4f6" in day_code
    assert "background:#1a222d" in night_code


def test_light_alias_uses_day_theme():
    assert help_docs.markdown_to_html("# t\n", mode="light") == help_docs.markdown_to_html(
        "# t\n", mode="day"
    )


def test_output_has_full_document_wrapper():
    html = help_docs.markdown_to_html("# 标题\n")
    assert html.startswith("<html>")
    assert "<body" in html and "</body>" in html and "</html>" in html
    assert "charset='utf-8'" in html


def test_full_help_markdown_renders_without_error_and_contains_sections():
    html = help_docs.help_html()
    assert "<h1" in html
    for marker in ("快速上手", "常见问题", "路径速查"):
        assert marker in html
    # 表格与代码块都出现在完整文档中
    assert "<table" in html
    assert "<pre" in html


def test_help_sections_split_by_h2_and_have_short_titles():
    sections = help_docs.help_sections()
    assert isinstance(sections, list) and len(sections) >= 7
    titles = [title for title, _ in sections]
    for expected in ("快速上手", "功能说明", "日常流程", "常见问题", "路径速查", "故障排查"):
        assert any(expected in title for title in titles)
    for _, md in sections:
        assert md.strip()


def test_help_section_html_wraps_markdown():
    section = help_docs.help_section_html("### 小节\n\n- 条目\n")
    assert section.startswith("<html>")
    assert "<h3" in section and "<li" in section


# ---- 文档单一来源：docs/使用教程.md 由 HELP_MARKDOWN 生成 ----


def test_tutorial_doc_is_generated_from_help_markdown():
    """`docs/使用教程.md` 必须与 `HELP_MARKDOWN` 逐字一致（消除重复源）。

    此前二者是同一份教程的两个手工副本、已双向漂移（审查 G2）：应用内帮助页有
    「插件」「识图」「Provider 一键模板」整节而文档站没有，FAQ 编号还错位到同号
    不同题。现在文档是生成产物，任何一次只改一边都会让本用例与 CI 的
    `consistency` job 直接红。

    改教程内容请改 `pi_manager/help_docs.py`，再运行
    `python scripts/check_versions.py --write`。
    """
    assert TUTORIAL_DOC.is_file(), f"缺少 {TUTORIAL_DOC}"
    doc = TUTORIAL_DOC.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert doc.startswith("<!-- 本文件由 pi_manager/help_docs.py"), (
        "使用教程缺少「自动生成、请勿手工编辑」头注释"
    )
    body = doc.split("-->", 2)[-1].lstrip("\n")
    assert body == help_docs.HELP_MARKDOWN.lstrip("\n"), (
        "docs/使用教程.md 与 help_docs.HELP_MARKDOWN 不一致；"
        "请运行 python scripts/check_versions.py --write 重新生成"
    )


def test_tutorial_covers_every_navigation_page():
    """11 个侧边栏页面每一个都必须在教程「功能分类说明」里有对应小节。

    「插件」页（v1.8.4 主特性）此前在文档站完全缺失，用户读到的是上一代产品。
    这条断言让"加了新页面但没写文档"变成可检测项。
    """
    from pi_manager.ui import NAV_PAGES

    md = help_docs.HELP_MARKDOWN
    headings = set(re.findall(r"^### (.+?)$", md, flags=re.MULTILINE))
    missing = [
        label
        for _key, label, _desc in NAV_PAGES
        if label != "使用教程"  # 教程页本身不需要自我介绍
        and not any(label in heading for heading in headings)
    ]
    assert not missing, f"教程「功能分类说明」缺少这些导航页的小节: {missing}"


def test_faq_numbering_is_contiguous_and_unique():
    """FAQ 编号必须连续且不重复（漂移期两份副本出现过同号不同题）。"""
    numbers = [int(n) for n in re.findall(r"^\*\*Q(\d+)[：:]", help_docs.HELP_MARKDOWN,
                                          flags=re.MULTILINE)]
    assert numbers, "未解析到任何 FAQ 条目"
    assert numbers == sorted(numbers), f"FAQ 编号非递增: {numbers}"
    assert len(numbers) == len(set(numbers)), f"FAQ 编号重复: {numbers}"
    assert numbers == list(range(1, len(numbers) + 1)), f"FAQ 编号不连续: {numbers}"


def test_tutorial_does_not_advertise_removed_windows_directory_build():
    """Windows 目录版已在 v1.8.5/1.8.6 移除，教程不得再引导用户去用它。"""
    assert "目录版或新单文件" not in help_docs.HELP_MARKDOWN
