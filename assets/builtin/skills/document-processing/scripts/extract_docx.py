# -*- coding: utf-8 -*-
"""提取 Word(.docx) 文本：zip + xml 标准库实现，无需额外依赖。"""
import io
import re
import sys
import zipfile
import xml.etree.ElementTree as ET

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def extract(docx_path: str) -> str:
    parts: list[str] = []
    with zipfile.ZipFile(docx_path) as z:
        if "word/document.xml" not in z.namelist():
            return "[错误] 不是有效的 .docx 文件（缺少 word/document.xml）"
        xml_bytes = z.read("word/document.xml")
    root = ET.fromstring(xml_bytes)
    body = root.find(f"{W_NS}body")
    if body is None:
        return ""
    for node in body.iter():
        if node.tag == f"{W_NS}p":
            # 段落：拼接其中的文本节点
            texts = "".join(
                t.text or ""
                for t in node.iter(f"{W_NS}t")
            )
            # 段落属性 pPr/jc 决定是否居中/标题，这里统一输出
            parts.append(texts)
    text = "\n".join(parts)
    # 清理多余空白行
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python extract_docx.py <file.docx>", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]
    try:
        out = extract(path)
    except zipfile.BadZipFile:
        print("[错误] 文件不是有效的 zip/docx", file=sys.stderr)
        sys.exit(1)
    except ET.ParseError as exc:
        print(f"[错误] document.xml 解析失败: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"[错误] 读取失败: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(out)


if __name__ == "__main__":
    main()
