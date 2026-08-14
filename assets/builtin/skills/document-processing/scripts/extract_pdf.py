# -*- coding: utf-8 -*-
"""提取 PDF 文本。优先 pdfplumber（推荐），回退 pypdf，均无则提示安装。"""
import io
import sys


def _extract_with_pdfplumber(path: str, pages: tuple[int, int] | None) -> str:
    import pdfplumber

    out: list[str] = []
    with pdfplumber.open(path) as pdf:
        total = len(pdf.pages)
        if pages is None:
            indices = range(total)
        else:
            start, end = pages
            indices = range(max(0, start - 1), min(total, end))
        for i in indices:
            page = pdf.pages[i]
            text = page.extract_text() or ""
            tables = page.extract_tables() or []
            out.append(f"== 第 {i + 1} 页 ==")
            if text.strip():
                out.append(text)
            for t in tables:
                if t:
                    rows = [" | ".join("" if c is None else str(c) for c in row) for row in t]
                    out.append("[表格]")
                    out.extend(rows)
    return "\n".join(out)


def _extract_with_pypdf(path: str, pages: tuple[int, int] | None) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    total = len(reader.pages)
    if pages is None:
        indices = range(total)
    else:
        start, end = pages
        indices = range(max(0, start - 1), min(total, end))
    out: list[str] = []
    for i in indices:
        text = reader.pages[i].extract_text() or ""
        out.append(f"== 第 {i + 1} 页 ==")
        out.append(text)
    return "\n".join(out)


def parse_pages_arg(arg: str) -> tuple[int, int] | None:
    """解析 --pages 1-3 / --pages 2 / --pages 1- 形式。"""
    arg = arg.strip()
    if not arg:
        return None
    if "-" in arg:
        left, right = arg.split("-", 1)
        start = int(left) if left.strip() else 1
        end = int(right) if right.strip() else 10**9
        return (start, end)
    n = int(arg)
    return (n, n)


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python extract_pdf.py <file.pdf> [--pages 1-3]", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]
    pages = None
    if "--pages" in sys.argv:
        try:
            pages = parse_pages_arg(sys.argv[sys.argv.index("--pages") + 1])
        except (ValueError, IndexError):
            print("--pages 需要形如 1-3 / 2 / 1- 的参数", file=sys.stderr)
            sys.exit(2)
    try:
        try:
            out = _extract_with_pdfplumber(path, pages)
        except ImportError:
            try:
                out = _extract_with_pypdf(path, pages)
            except ImportError:
                print(
                    "[错误] 需要 pdf 提取库，请先执行: python -m pip install pdfplumber",
                    file=sys.stderr,
                )
                sys.exit(3)
    except Exception as exc:
        print(f"[错误] PDF 解析失败: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(out)


if __name__ == "__main__":
    main()
