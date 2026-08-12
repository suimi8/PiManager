# -*- coding: utf-8 -*-
"""Built-in usage tutorial and FAQ (Markdown)."""
from __future__ import annotations

import html
import re

from .extras import APP_VERSION

_RE_CODE = re.compile(r"`([^`]+)`")
_RE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_RE_ITALIC = re.compile(r"\*([^*]+)\*")
_RE_LINK = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_RE_SEP = re.compile(r":?-{3,}:?")
_RE_UL = re.compile(r"^[-*] ")
_RE_OL = re.compile(r"^\d+\. ")

_HELP_MARKDOWN = r'''# Pi Manager 使用教程与常见问题

> 版本 __APP_VERSION__ · 跨平台 GUI 管理官方 Pi Coding Agent（Windows / macOS / Linux）
> 完整 agent 能力始终由官方 `pi` 提供，本工具负责配置、切换、测试与启动。

---

## 一、快速上手（3 分钟）

### 界面布局（侧边栏）

左侧边栏（参考 CC Switch 风格）：

1. **简化配置**（首页）：当前默认、快速接入 Provider、工作目录拖拽启动、收藏一键切换  
2. **模型列表**：切换默认、收藏、批量测试可用性/延迟  
3. **Provider**：自定义 Provider 增删改  
4. **快速提问 / 会话 / 健康监控 / 测试历史 / 工具 / 设置 / 使用教程**

高级功能都在侧边栏二级页面，日常优先用「简化配置」。



### 1. 安装 / 确认 Pi
1. 打开侧边栏 **工具** → **运行自检**，确认「Pi CLI」通过。  
2. 若未安装：设置页点 **安装/升级 Pi**，或工具栏按提示操作。  
3. 版本显示在右上角绿色标签（如 `pi: 0.80.6`）。

### 2. 添加自定义 Provider（推荐流程）
**方式 A：一键模板（最快）**
1. 进入 **Provider** → **新建 Provider**。  
2. 顶部 **常用模板** 下拉选择目标大模型（如 DeepSeek / 智谱 GLM / Kimi / 通义千问 / OpenAI / Claude / Gemini …国内外主流 26 家）。  
3. Base URL、API 类型、常用模型列表、兼容选项自动填充，**只需粘贴自己的 API Key** → 保存。  
4. 回到 **模型列表** → **刷新模型列表**。

**方式 B：手动填写**
1. 在首页 **简化配置** 填写 Base URL + API Key 点「拉取并保存」，或进入 **Provider** → **从 API 拉取模型**。  
2. 填写：
   - Provider 名称（如 `免费grok`）
   - Base URL（如 `https://xxx/v1`）
   - API 类型（多为 `openai-completions`）
   - API Key（真实 `sk-...` 或环境变量名）
3. 点 **拉取可用模型** → 多选模型 → **保存到 models.json**。  
4. 回到 **模型列表** → **刷新模型列表**。

### 3. 设为默认并启动
1. 模型列表已按 **Provider 分组为树状**：点击分组前的箭头可展开 / 收起，双击分组同样可切换。  
2. 选中某个模型 → **设为默认**（或双击模型行）。  
3. 在 **简化配置** 确认默认模型正确。  
4. 拖入工作文件夹，或点 **启动完整 Pi 会话** / **启动 Pi**。

### 4. 一键切换
- **收藏**：模型页选中 → **加入收藏**（可批量多选后加入）。  
- **托盘**：关闭窗口进托盘 → 右键 **切换默认模型**。  
- **enabledModels**：把收藏写入循环列表，Pi 内 `Ctrl+P` 切换。

---

## 二、功能分类说明

### 仪表盘
- 查看当前默认模型、Thinking、认证状态。  
- 拖拽文件夹：设工作目录并可用默认模型启动 Pi。  
- 收藏列表：双击设默认；支持批量测试收藏。

### 模型切换
| 操作 | 说明 |
|------|------|
| 树状分组 | 模型按 Provider 分组；点击箭头或双击分组展开/收起，状态自动记忆 |
| 过滤 | 按 provider/model 关键字筛选（匹配 Provider 时保留整组） |
| 多选 | Ctrl/Shift 多选模型子项 |
| 设为默认 | 当前选中第一项（双击模型行） |
| 批量加入收藏 | 所有选中子项 |
| 测试选中 | 批量测可用性与延迟 |
| 测试当前过滤结果 | 对筛选后全部模型批量测 |
| 批量测试收藏 / 全部模型 | 一键巡检 |
| 启动 Pi | 用选中模型开官方会话 |

**测试方式**
- **自动(HTTP优先)**：自定义 Provider 先 HTTP，失败再 Pi  
- **HTTP 直连**：只打 BaseURL（适合中转）  
- **Pi 实测**：官方 `pi -p`（适合 OAuth / 内置）

### Provider 管理
- 增删改自定义 Provider。  
- **常用模板**：新建 Provider 时从国内外主流大模型模板选择（OpenAI / Claude / Gemini / DeepSeek / GLM / Kimi / Qwen / ERNIE / 混元 / 星火 / 豆包 / SiliconFlow 等），自动填充 Base URL、API 类型与模型列表，仅需粘贴 API Key。  
- **从 API 拉取模型**：BaseURL + API Key 获取模型列表。  
- API Key 默认保存在 OS keyring；不可用时回退到当前用户专属的加密文件库。  
- `models.json` 仅保存官方 Pi 可识别的 `${PI_MANAGER_PROVIDER_..._API_KEY}` 引用，启动 Pi 时才把真实密钥注入子进程环境。

### 快速提问
- 默认使用常驻 `pi --mode rpc` 会话：多轮上下文保留在会话内，故障切换模型时通过 `set_model` 在同一会话内热切、不丢上下文。
- 空闲一段时间后会自动回收 pi 进程（固定会话 ID 保证下次提问自动恢复上下文）；可在「设置 → 可靠性」关闭常驻会话回退到一次性模式。
- 输入框 `Ctrl+Enter` 直接发送。复杂改代码请用「启动完整 Pi」。
- **识图（默认启用）**：粘贴/拖入图片时，Pi skill 自动调用内置免费识图模型（智谱 GLM-4.6V-Flash 优先，限流自动切 GLM-4.1V-Thinking-Flash）把图片转为文字，再交给当前默认对话模型回答。
  - 识图模型在「设置 → 识图模型」中配置（Key + 模型选择），**只用于识图管道，不会自动出现在模型列表中**；如需在列表中使用智谱模型，请在 Provider 管理中手动添加。
  - 未配置智谱 Key 时，附加的图片会被跳过并给出提示。

### 会话
- 按路径/名称过滤。  
- 批量删除选中会话；重命名、继续、资源管理器打开。

### 健康监控
- 范围可选：**收藏 / 默认 / 自定义 Provider / 全部已加载模型 / 模型页选中**。  
- 显示状态、延迟、方式、错误摘要。  
- 可设定时巡检（分钟，0=关闭）。

### 测试历史
- 自动记录每次测试；可过滤、清空。

### 工具/自检
- 自检清单、配置导入导出、密钥加密、版本检查。

### 设置
- 默认模型、语言（中文优先）、CLI/界面主题。  
- 全局代理、批量测试并发、托盘行为、密钥加密。

---

## 三、推荐日常流程

```text
添加/更新 Provider → 刷新模型列表 → 批量测试关键模型
→ 收藏常用 → 设默认 → 拖入项目目录启动 Pi
→ （可选）健康监控定时巡检
```

### 批量操作一览
- 模型：多选 → 批量收藏 / 批量测试 / 测过滤结果 / 测全部  
- 健康：按范围批量巡检  
- 会话：多选批量删除  
- 历史：一键清空  
- 配置：导出/导入整包 ZIP  
- 密钥：一键加密现有明文 Key  

---

## 四、常见问题（FAQ）

### A. 安装与启动

**Q1：单文件 EXE 报 `QSpinBox is not defined`？**  
A：旧包缺陷，请用最新打包版本（目录版或新单文件）。

**Q2：PowerShell 启动 pi 报「意外的标记」？**  
A：路径含 `@scope` 时需 `&` 调用。请用 Pi Manager「启动 Pi」，或 Windows Terminal / cmd。

**Q3：关闭窗口后程序还在？**  
A：默认最小化到托盘。托盘右键 → 退出。可在设置关闭「关闭窗口时最小化到托盘」。

### B. Provider / API Key / 拉取模型

**Q4：切换 Provider 后为什么仍报 401 Invalid API key / Missing bearer？**  
A：旧版会把 `__DPAPI__:名称` 写进 `models.json`，官方 Pi 不认识该私有占位符，会把它当成真实 API Key 发送。当前版本会自动迁移为 `${PI_MANAGER_PROVIDER_..._API_KEY}` 并在启动 Pi 时注入真实密钥。升级后请打开一次 Provider 页面并重新保存；若提示安全密钥丢失，请重新填写真实 Key。外部环境变量必须显式写为 `$OPENAI_API_KEY` 或 `${OPENAI_API_KEY}`。

**Q5：SSL UNEXPECTED_EOF？**  
A：网络/防火墙/直连不稳定。可：
1. 设置全局代理（如 `http://127.0.0.1:7890`）  
2. 改用可访问的中转 Base URL  
3. 拉取对话框勾选忽略 SSL（仅排查）

**Q6：Key 存在 models.json 安全吗？**  
A：真实 Key 不写入 `models.json`。它优先存入系统 keyring，回退时写入 `secrets.vault` 加密库；`models.json` 中只有 `${PI_MANAGER_PROVIDER_..._API_KEY}` 引用。导出密钥时必须设置至少 10 位密码，密钥包使用 PBKDF2-HMAC-SHA256 + AES-256-GCM。

### C. 测试与健康检查

**Q7：手动测试 免费grok/grok-4.5 可用，健康检查 0/3 全挂？**  
A：健康检查默认测的是 **收藏列表**。若收藏是 `openai-codex/...`（需登录/OAuth），会不可用。  
解决：  
1. 把可用的 `免费grok/grok-4.5` 加入收藏  
2. 健康范围选「默认模型」或「自定义 Provider」  
3. 查看健康表错误列 / 测试历史

**Q8：延迟多少算正常？**  
A：中转常见 1–5 秒；>10 秒检查网络/代理/服务商。

**Q9：测试会花钱吗？**  
A：会发极短 prompt，通常费用极低；免费额度以服务商为准。

### D. 启动 Pi 与工作目录

**Q10：如何用默认模型在某项目打开 Pi？**  
A：把文件夹拖到仪表盘；或填工作目录后点启动。

**Q11：Pi 里如何尽量用中文？**  
A：设置「默认语言」= 简体中文。会写入 `AGENTS.md` 并附加 system prompt。

### E. 配置迁移

**Q12：换电脑怎么迁移？**  
A：工具页 **导出配置包**（需要 Key 再勾选含密钥）→ 新机器 **导入配置包**。

**Q13：配置文件在哪？**  
A：`%USERPROFILE%\.pi\agent\`  
含 `settings.json`、`models.json`、`pi-manager.json`、`secrets.vault` 等。系统 keyring 可用时，真实密钥由操作系统管理；旧 `secrets.dpapi` 只用于兼容迁移。

### F. Cursor / 其它

**Q14：Cursor 里怎么用 Pi？**  
A：若已装本地扩展 `pi-manager.pi-cursor`，命令面板搜 Pi；否则用本 GUI 启动终端 Pi。

**Q15：昼夜模式会同步到 Pi CLI 吗？**
A：会。全局白天模式会让管理器、弹窗、帮助页与 Pi CLI 同步使用浅色；全局夜间模式会同步使用深色。Pi CLI 不再单独配置主题。

**Q16：Provider 里的「兼容选项」是什么？**  
A：写进 `models.json` 的 `compat` 字段，告诉官方 Pi 这个接口支持哪些能力：  
- **支持 Developer 角色**：能否用 `developer` 角色消息（部分 OpenAI 兼容中转支持；不确定就关掉）。  
- **支持推理强度（Reasoning Effort）**：能否调节 thinking/reasoning 强度（支持就勾选，不支持关掉以免请求报错）。


---

## 五、路径速查

| 项目 | 路径 |
|------|------|
| 配置根 | `%USERPROFILE%\.pi\agent\` |
| 默认模型等 | `settings.json` |
| 自定义 Provider | `models.json` |
| GUI 偏好 | `pi-manager.json` |
| 加密密钥回退库 | `secrets.vault`（旧 `secrets.dpapi` 仅用于迁移） |
| 测试历史 | `pi-manager-test-history.json` |
| 健康状态 | `pi-manager-health.json` |

---

## 六、故障排查清单

1. 自检是否全绿？  
2. 默认模型是否为已验证可用模型？  
3. 自定义 Provider 的 BaseURL / Key 是否正确？  
4. 需要代理时是否启用全局代理？  
5. 健康检查范围是否包含「会失败的 OAuth 模型」？  
6. 测试历史里的错误摘要是什么？  
7. 是否用最新 EXE？先退出托盘再启动。

---

*完整编码/agent 能力请始终通过「启动 Pi」使用官方交互会话。*
'''

# 版本号单一来源：pi_manager/extras.py 的 APP_VERSION。
HELP_MARKDOWN = _HELP_MARKDOWN.replace("__APP_VERSION__", APP_VERSION)


def _help_theme_colors(mode: str = "night") -> dict[str, str]:
    """HTML 内联色：随昼夜模式切换，保证 QTextBrowser 可读。"""
    m = (mode or "night").lower().strip()
    if m in {"day", "light", "白天"}:
        return {
            "text": "#1f2937",
            "muted": "#4b5563",
            "title": "#111827",
            "heading": "#1d4ed8",
            "border": "#d1d5db",
            "code_bg": "#f3f4f6",
            "code_fg": "#1d4ed8",
            "pre_bg": "#f8fafc",
            "pre_fg": "#1f2937",
            "pre_border": "#e5e7eb",
            "quote_bg": "#eff6ff",
            "quote_fg": "#1e3a5f",
            "quote_border": "#3b82f6",
            "th_bg": "#eef2ff",
            "th_fg": "#1e40af",
            "td_fg": "#1f2937",
            "link": "#2563eb",
            "hr": "#d1d5db",
        }
    return {
        "text": "#e8eef7",
        "muted": "#c5d0e0",
        "title": "#f4f7fb",
        "heading": "#93c5fd",
        "border": "#2a3545",
        "code_bg": "#1a222d",
        "code_fg": "#93c5fd",
        "pre_bg": "#0f141b",
        "pre_fg": "#d4d4d4",
        "pre_border": "#243041",
        "quote_bg": "#132033",
        "quote_fg": "#c5d0e0",
        "quote_border": "#3b82f6",
        "th_bg": "#161d27",
        "th_fg": "#93c5fd",
        "td_fg": "#e8eef7",
        "link": "#60a5fa",
        "hr": "#243041",
    }


def markdown_to_html(md: str, mode: str = "night") -> str:
    """Lightweight Markdown -> HTML for QTextBrowser (no external deps)."""
    c = _help_theme_colors(mode)
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_code = False
    in_ul = False
    in_table = False
    table_rows: list[list[str]] = []

    def close_ul():
        nonlocal in_ul
        if in_ul:
            out.append("</ul>")
            in_ul = False

    def flush_table():
        nonlocal in_table, table_rows
        if not table_rows:
            return
        out.append(
            '<table border="0" cellspacing="0" cellpadding="6" '
            'style="border-collapse:collapse;margin:10px 0;width:100%;">'
        )
        for i, row in enumerate(table_rows):
            # skip separator row
            if all(_RE_SEP.fullmatch(c_cell.strip() or "") for c_cell in row):
                continue
            tag = "th" if i == 0 else "td"
            style = f"border:1px solid {c['border']};padding:8px 12px;color:{c['td_fg']};"
            if i == 0:
                style += f"background:{c['th_bg']};font-weight:700;color:{c['th_fg']};"
            else:
                style += "background:transparent;"
            cells = "".join(f'<{tag} style="{style}">{_inline(cell)}</{tag}>' for cell in row)
            out.append(f"<tr>{cells}</tr>")
        out.append("</table>")
        table_rows = []
        in_table = False

    def _inline(text: str) -> str:
        t = html.escape(text)
        code_open = (
            f"<code style='background:{c['code_bg']};color:{c['code_fg']};padding:2px 6px;"
            f"border-radius:6px;font-family:Consolas,monospace;'>"
        )
        t = _RE_CODE.sub(lambda m: f"{code_open}{m.group(1)}</code>", t)
        t = _RE_BOLD.sub(r"<b>\1</b>", t)
        t = _RE_ITALIC.sub(r"<i>\1</i>", t)
        link_style = f'color:{c["link"]};text-decoration:none;'
        t = _RE_LINK.sub(
            lambda m: f'<a href="{m.group(2)}" style="{link_style}">{m.group(1)}</a>',
            t,
        )
        return t

    for raw in lines:
        line = raw.rstrip()
        if line.strip().startswith("```"):
            close_ul()
            flush_table()
            if not in_code:
                in_code = True
                out.append(
                    f"<pre style=\"background:{c['pre_bg']};color:{c['pre_fg']};padding:12px 14px;"
                    f"border-radius:10px;overflow:auto;border:1px solid {c['pre_border']};"
                    f"font-family:Consolas,monospace;font-size:12.5px;line-height:1.45;\">"
                )
            else:
                in_code = False
                out.append("</pre>")
            continue
        if in_code:
            out.append(html.escape(raw))
            continue

        if "|" in line and line.strip().startswith("|"):
            close_ul()
            parts = [p.strip() for p in line.strip().strip("|").split("|")]
            table_rows.append(parts)
            in_table = True
            continue
        else:
            if in_table:
                flush_table()

        if not line.strip():
            close_ul()
            out.append("<br/>")
            continue
        if line.startswith("### "):
            close_ul()
            out.append(
                f"<h3 style='margin:16px 0 8px;color:{c['heading']};font-size:14px;'>{_inline(line[4:])}</h3>"
            )
        elif line.startswith("## "):
            close_ul()
            out.append(
                f"<h2 style='margin:20px 0 10px;border-bottom:1px solid {c['border']};"
                f"padding-bottom:6px;color:{c['title']};font-size:16px;'>{_inline(line[3:])}</h2>"
            )
        elif line.startswith("# "):
            close_ul()
            out.append(
                f"<h1 style='margin:8px 0 14px;color:{c['title']};font-size:20px;'>{_inline(line[2:])}</h1>"
            )
        elif line.startswith("> "):
            close_ul()
            out.append(
                f"<blockquote style='margin:10px 0;padding:10px 14px;border-left:4px solid {c['quote_border']};"
                f"background:{c['quote_bg']};color:{c['quote_fg']};border-radius:0 8px 8px 0;'>{_inline(line[2:])}</blockquote>"
            )
        elif line.startswith("---"):
            close_ul()
            out.append(f"<hr style='border:none;border-top:1px solid {c['hr']};margin:18px 0;'/>")
        elif _RE_UL.match(line):
            if not in_ul:
                out.append(f"<ul style='margin:8px 0 8px 1.2em;color:{c['text']};'>")
                in_ul = True
            out.append(f"<li style='margin:4px 0;line-height:1.5;'>{_inline(line[2:])}</li>")
        elif _RE_OL.match(line):
            close_ul()
            out.append(f"<p style='margin:4px 0 4px 0.5em;color:{c['text']};'>{_inline(line)}</p>")
        else:
            close_ul()
            out.append(f"<p style='margin:8px 0;line-height:1.6;color:{c['text']};'>{_inline(line)}</p>")

    close_ul()
    flush_table()
    if in_code:
        out.append("</pre>")

    body = "\n".join(out)
    return (
        "<html><head><meta charset='utf-8'></head>"
        "<body style=\"font-family:'Segoe UI','Microsoft YaHei UI','PingFang SC',sans-serif;"
        f"font-size:13px;color:{c['text']};background:transparent;padding:10px 14px;line-height:1.55;\">"
        f"{body}</body></html>"
    )


def help_sections() -> list[tuple[str, str]]:
    """Split HELP_MARKDOWN into tab-friendly sections by top-level ## headers."""
    lines = HELP_MARKDOWN.strip().splitlines()
    intro: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    current_title: str | None = None
    current: list[str] = []

    def short_title(raw: str) -> str:
        mapping = [
            ("快速上手", "快速上手"),
            ("界面布局", "界面布局"),
            ("功能分类", "功能说明"),
            ("推荐日常", "日常流程"),
            ("常见问题", "常见问题"),
            ("路径速查", "路径速查"),
            ("故障排查", "故障排查"),
        ]
        for key, short in mapping:
            if key in raw:
                return short
        return raw

    for line in lines:
        if line.startswith("# ") and not line.startswith("## "):
            # skip document H1
            continue
        if line.startswith("## ") and not line.startswith("### "):
            if current_title is None:
                # flush intro as 概览 if has content
                intro_text = "\n".join(intro).strip()
                if intro_text:
                    sections.append(("概览", intro[:]))
                intro = []
            else:
                sections.append((current_title, current))
            current_title = short_title(line[3:].strip())
            current = [line]
        else:
            if current_title is None:
                intro.append(line)
            else:
                current.append(line)
    if current_title is not None:
        sections.append((current_title, current))
    elif intro:
        sections.append(("概览", intro))

    out: list[tuple[str, str]] = []
    for title, body_lines in sections:
        text = "\n".join(body_lines).strip()
        if text:
            out.append((title, text + "\n"))
    if not out:
        out = [("全部", HELP_MARKDOWN)]
    return out


def help_section_html(section_md: str, mode: str = "night") -> str:
    return markdown_to_html(section_md, mode=mode)


def help_html(mode: str = "night") -> str:
    return markdown_to_html(HELP_MARKDOWN, mode=mode)
