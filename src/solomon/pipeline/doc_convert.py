#!/usr/bin/env python3
"""doc_convert.py — 多格式文档 / 网页 → Markdown 的轻量转换（零 torch、零 Docker）。

给 ingest.py 复用：
  _to_markdown(filepath)   → markitdown 转 PDF/DOCX/XLSX/HTML/EPUB 等为 Markdown
  _web_to_markdown(url)    → trafilatura 提取网页正文为 Markdown

两个库都是纯 Python（pdfminer/mammoth/openpyxl / lxml），失败返回 None 由调用方降级。
"""
from __future__ import annotations

import os
import sys

# markitdown/trafilatura 装在 python3.14 site-packages；当本文件被其它解释器 import 时
# 保持可导入（失败由调用方捕获）。
try:
    from markitdown import MarkItDown  # type: ignore
except Exception:  # pragma: no cover - 仅环境缺包时
    MarkItDown = None

try:
    import trafilatura  # type: ignore
except Exception:  # pragma: no cover
    trafilatura = None


def _to_markdown(filepath: str) -> str | None:
    """任意支持格式 → Markdown 文本；失败返回 None（调用方回退/报错）。"""
    if MarkItDown is None:
        return None
    try:
        md = MarkItDown()
        result = md.convert(filepath)
        text = (result.text_content or "").strip()
        return text or None
    except Exception as exc:
        print(f"[doc_convert] markitdown 转换失败: {exc}", file=sys.stderr)
        return None


def _web_to_markdown(url: str, timeout: int = 60) -> str | None:
    """网页 URL → 正文 Markdown；失败返回 None（调用方降级为按 URL 入库）。"""
    if trafilatura is None:
        return None
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=True,
            favor_precision=True,
        )
        text = (text or "").strip()
        return text or None
    except Exception as exc:
        print(f"[doc_convert] trafilatura 提取失败: {exc}", file=sys.stderr)
        return None


if __name__ == "__main__":
    # 冒烟：python3.14 doc_convert.py <file|url>
    target = sys.argv[1] if len(sys.argv) > 1 else ""
    if target.startswith(("http://", "https://")):
        out = _web_to_markdown(target)
        print("--- 网页正文 ---" if out else "--- 提取失败 ---")
    else:
        out = _to_markdown(target)
        print("--- markdown ---" if out else "--- 转换失败 ---")
    if out:
        print(out[:2000])
