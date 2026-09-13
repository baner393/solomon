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
    """网页 URL → 正文 Markdown；失败返回 None（调用方降级为按 URL 入库）。

    开 include_images/include_links：图文网页的图片会以 `![...](http://...)` 保留、
    参考链接保留。调用方（ingest_document）负责把图片下载进 vault 并改写引用。
    """
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
            include_images=True,
            include_links=True,
        )
        text = (text or "").strip()
        return text or None
    except Exception as exc:
        print(f"[doc_convert] trafilatura 提取失败: {exc}", file=sys.stderr)
        return None


def _download_web_images(markdown_text: str, page_url: str, dest_dir: str) -> str:
    """把 Markdown 里的 `![alt](http://...)` 网页图片下载到 dest_dir，引用改写为
    `![[文件名]]`（Obsidian embed，verify 按附件存在性命中）。

    - 相对协议 URL（//host/...）补 https:
    - 相对路径（/img/x.png）基于 page_url 的 host 补全
    - 下载失败/非图片不阻塞：保留原引用或删掉该图引用（避免断链）
    返回改写后的 Markdown。
    """
    import re as _re
    import urllib.request
    from urllib.parse import urlparse, urljoin
    import os as _os

    if not markdown_text:
        return markdown_text

    _os.makedirs(dest_dir, exist_ok=True)
    base_host = (urlparse(page_url).netloc or "")

    def _to_abs(u: str) -> str:
        if u.startswith("//"):
            return "https:" + u
        if u.startswith("/"):
            return f"https://{base_host}{u}"
        if u.startswith(("http://", "https://")):
            return u
        return urljoin(page_url, u)

    def _dl(img_url: str) -> str | None:
        """下载图片到 dest_dir，返回落盘文件名；失败返回 None。"""
        try:
            req = urllib.request.Request(_to_abs(img_url), headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                data = resp.read()
            if len(data) < 100 or not data[:8].startswith(b"\x89PNG") and not data[:4] in (b"\xff\xd8\xff", b"GIF8", b"RIFF"):
                return None  # 非图片内容
            # 用图片 URL 的 basename，清洗非法字符（冒号→横杠，与视频图片一致）
            name = _re.sub(r"[^\w.\-]", "_", _os.path.basename(urlparse(img_url).path) or "img.png")
            name = name.replace(":", "-")
            if not name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg")):
                name += ".png"
            out = _os.path.join(dest_dir, name)
            with open(out, "wb") as f:
                f.write(data)
            return name
        except Exception as exc:
            print(f"[doc_convert] 图片下载失败 {img_url[:60]}: {exc}", file=sys.stderr)
            return None

    def _replace(m: _re.Match) -> str:
        alt, img_url = m.group(1), m.group(2)
        if not img_url.startswith(("http://", "https://", "//", "/")):
            return m.group(0)  # 本地/相对引用不动
        name = _dl(img_url)
        if name:
            # 改写为 Obsidian wiki embed（verify 按附件存在性命中，与视频笔记一致）
            return f"![[{name}]]"
        return ""  # 下载失败删掉引用，避免断链

    return _re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _replace, markdown_text)


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
