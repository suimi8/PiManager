# -*- coding: utf-8 -*-
"""提取 PowerPoint(.pptx) 各页文本，zip + xml 标准库实现。"""
import io
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
P_NS = "{http://schemas.openxmlformats.org/presentationml/2006/main}"


def _slide_files(z: zipfile.ZipFile) -> list[str]:
    names = z.namelist()
    slides = sorted(
        n for n in names
        if re.match(r"ppt/slides/slide\d+\.xml$", n)
        or re.match(r"ppt/slides/slide\d+\.xml$", n)
    )
    # 按数字排序
    def key(n: str) -> int:
        m = re.search(r"(\d+)", n)
        return int(m.group(1)) if m else 0

    return sorted(slides, key=key)


def extract(pptx_path: str) -> str:
    with zipfile.ZipFile(pptx_path) as z:
        slides = _slide_files(z)
        if not slides:
            return "[错误] 不是有效的 .pptx 文件（未找到 slide）"
        out: list[str] = []
        for idx, sf in enumerate(slides, 1):
            root = ET.fromstring(z.read(sf))
            texts: list[str] = []
            for t in root.iter(f"{A_NS}t"):
                if t.text and t.text.strip():
                    texts.append(t.text)
            out.append(f"== 第 {idx} 页 ==")
            if texts:
                out.append("\n".join(texts))
            else:
                out.append("（无文本）")
        return "\n\n".join(out)


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python extract_pptx.py <file.pptx>", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]
    try:
        out = extract(path)
    except zipfile.BadZipFile:
        print("[错误] 文件不是有效的 zip/pptx", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"[错误] 读取失败: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(out)


if __name__ == "__main__":
    main()
