---
name: document-processing
description: "提取 PDF、Word(.docx)、Excel(.xlsx)、PowerPoint(.pptx) 文档中的文字与表格内容，供模型阅读、总结、对比或转换为其他格式。当用户要求读取/解析/总结文档、提取表格或文本时使用。"
---

# 文档处理

## 概述

用本目录下的 Python 脚本把二进制文档转成纯文本，供阅读与总结。脚本位于本 skill 目录的 `scripts/` 下：

```
scripts/
├── extract_docx.py   # Word
├── extract_xlsx.py   # Excel（含表格）
├── extract_pptx.py   # PowerPoint
└── extract_pdf.py    # PDF（需一次性安装依赖）
```

## 依赖（一次性）

docx / xlsx / pptx 使用 Python 标准库，**无需安装**。PDF 需要 pdfplumber：

```bash
python -m pip install pdfplumber
```

## 使用

按文件类型调用对应脚本，输出到 stdout（可重定向到文件再读取）：

```bash
python scripts/extract_docx.py "path/to/file.docx"
python scripts/extract_xlsx.py "path/to/file.xlsx"          # 全部 sheet
python scripts/extract_xlsx.py "path/to/file.xlsx" --sheet 1 # 指定 sheet 索引
python scripts/extract_pptx.py "path/to/file.pptx"
python scripts/extract_pdf.py "path/to/file.pdf"            # 默认全部页
python scripts/extract_pdf.py "path/to/file.pdf" --pages 1-3
```

## 工作流

1. **识别类型**：按扩展名选择脚本（`.docx`/`.docx`→docx；`.xlsx`→xlsx；`.pptx`→pptx；`.pdf`→pdf）。
2. **提取**：运行对应脚本，捕获输出。文档较大时先提取到临时文件再读取：
   ```bash
   python scripts/extract_docx.py "report.docx" > /tmp/report.txt
   ```
3. **给模型阅读**：把提取的文本作为上下文，回答总结/对比/问答类问题。
4. **失败处理**：
   - 脚本报 `ModuleNotFoundError: pdfplumber` → 先执行 pip install。
   - 文件损坏/加密 → 提示用户文件无法解析（加密 PDF 需先解密）。
   - 输出乱码 → 检查文件是否为 UTF-8；Windows 下脚本已强制 UTF-8 输出，如仍乱码可将输出重定向到文件后用 read 工具读取。

## 注意事项

- 脚本只做文本提取，不修改原文件、不执行文档内宏/脚本。
- 含敏感信息（密钥、凭据）的文档内容属于用户数据，仅在对话中按要求摘要使用，不写入项目文件或提交信息。
- 大文档（>100 页 PDF / >50MB）建议用 `--pages` 分段提取，避免上下文超限。
