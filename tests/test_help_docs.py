# -*- coding: utf-8 -*-
"""help_docs.markdown_to_html 手写解析器（标题/列表/表格/代码块/内联格式/转义）测试。

用代表性样例断言输出 HTML 结构，不追求像素级一致，保证手写解析器
的每个分支都有行为契约。
"""
from __future__ import annotations

import pytest

from pi_manager import help_docs


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
