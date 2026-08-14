# -*- coding: utf-8 -*-
"""提取 Excel(.xlsx) 单元格文本（含 sharedStrings），zip + xml 标准库实现。"""
import io
import sys
import zipfile
import xml.etree.ElementTree as ET

S_NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R_NS = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    out: list[str] = []
    if "xl/sharedStrings.xml" not in z.namelist():
        return out
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    for si in root.iter(f"{S_NS}si"):
        out.append("".join(t.text or "" for t in si.iter(f"{S_NS}t")))
    return out


def _sheet_names(z: zipfile.ZipFile) -> list[str]:
    if "xl/workbook.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/workbook.xml"))
    names: list[str] = []
    for sh in root.iter(f"{S_NS}sheet"):
        names.append(str(sh.attrib.get("name") or ""))
    return names


def _rels(z: zipfile.ZipFile) -> dict[str, str]:
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" not in z.namelist():
        return rels
    root = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    # rels 文件自身用 package 命名空间；用 tag 后缀匹配避免命名空间常量差异
    for rel in root.iter():
        if not rel.tag.endswith("}Relationship") and rel.tag != "Relationship":
            continue
        rels[str(rel.attrib.get("Id") or "")] = str(rel.attrib.get("Target") or "")
    return rels


def _col_name(ref: str) -> str:
    """从单元格引用（如 C5）提取列字母。"""
    return "".join(ch for ch in ref if ch.isalpha())


def extract(xlsx_path: str, sheet_index: int | None = None) -> str:
    with zipfile.ZipFile(xlsx_path) as z:
        names = _sheet_names(z)
        rels = _rels(z)
        shared = _shared_strings(z)
        # 找到要输出的 sheet 文件
        sheet_files: list[str] = []
        if "xl/workbook.xml" in z.namelist():
            wb_root = ET.fromstring(z.read("xl/workbook.xml"))
            for sh in wb_root.iter(f"{S_NS}sheet"):
                rid = str(sh.attrib.get(f"{R_NS}id") or "")
                target = rels.get(rid, "")
                if target.startswith("/"):
                    target = target.lstrip("/")
                elif not target.startswith("xl/"):
                    target = "xl/" + target.lstrip("/")
                sheet_files.append(target)
        if not sheet_files:
            sheet_files = [n for n in z.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]

        target_indices = (
            [sheet_index]
            if sheet_index is not None
            else list(range(len(sheet_files)))
        )
        out: list[str] = []
        for idx in target_indices:
            if idx < 0 or idx >= len(sheet_files):
                out.append(f"[sheet {idx} 不存在]")
                continue
            title = names[idx] if idx < len(names) else f"Sheet{idx + 1}"
            sf = sheet_files[idx]
            root = ET.fromstring(z.read(sf))
            rows_out: list[str] = []
            for row in root.iter(f"{S_NS}row"):
                cells: list[str] = []
                for c in row.iter(f"{S_NS}c"):
                    t = c.attrib.get("t", "")
                    ref = c.attrib.get("r", "")
                    v = None
                    for node in c:
                        if node.tag == f"{S_NS}v":
                            v = node.text or ""
                        elif node.tag == f"{S_NS}is" and t == "inlineStr":
                            v = "".join(x.text or "" for x in node.iter(f"{S_NS}t"))
                    if v is None:
                        continue
                    if t == "s" and v.isdigit():
                        idx_s = int(v)
                        v = shared[idx_s] if idx_s < len(shared) else ""
                    cells.append(v)
                if any(x.strip() for x in cells):
                    rows_out.append(" | ".join(cells))
            out.append(f"== {title} ==")
            out.extend(rows_out)
        return "\n".join(out)


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python extract_xlsx.py <file.xlsx> [--sheet N]", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[1]
    sheet_index = None
    if "--sheet" in sys.argv:
        try:
            sheet_index = int(sys.argv[sys.argv.index("--sheet") + 1])
        except (ValueError, IndexError):
            print("--sheet 需要整数索引", file=sys.stderr)
            sys.exit(2)
    try:
        out = extract(path, sheet_index)
    except zipfile.BadZipFile:
        print("[错误] 文件不是有效的 zip/xlsx", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"[错误] 读取失败: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    print(out)


if __name__ == "__main__":
    main()
