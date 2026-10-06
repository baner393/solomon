#!/usr/bin/env python3
"""
feishu_doc.py — 飞书文档读取（wiki/docx → Markdown），供 ingest 入库

凭据：读 FEISHU_APP_ID / FEISHU_APP_SECRET env（profile .env 已有）。
权限要求（飞书开放平台给应用开）：
  - wiki:wiki（解析 wiki 链接）
  - docx:document（读取 docx 文档块）
  - drive:drive（可选，图片下载用，二期）

用法：
    from feishu_doc import fetch_doc
    title, markdown = fetch_doc("https://xxx.feishu.cn/wiki/AbCd...")
    # 或直链 https://xxx.feishu.cn/docx/AbCd...

实测（2026-10-06）：
  - blocks 列表接口返回的块**自带内容键**（text/heading1/ordered/image…），
    block_type 是数字枚举（1=page 根, 2=text, 3-7=heading1-5, 13=ordered, 27=image）
  - 渲染按「内容键驱动」：块里出现哪个内容键就渲染哪个，不依赖枚举数值
"""

from __future__ import annotations

import json
import os
import re
import urllib.request

_API = "https://open.feishu.cn/open-apis"
_TIMEOUT = 30


def _app_credentials() -> tuple[str, str]:
    app_id = os.environ.get("FEISHU_APP_ID", "").strip()
    app_secret = os.environ.get("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise RuntimeError("缺 FEISHU_APP_ID/FEISHU_APP_SECRET（profile .env 未配置）")
    return app_id, app_secret


def tenant_token() -> str:
    """tenant_access_token（应用身份访问凭据）。"""
    app_id, app_secret = _app_credentials()
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        f"{_API}/auth/v3/tenant_access_token/internal", data=body,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        data = json.loads(r.read().decode())
    if data.get("code") != 0:
        raise RuntimeError(f"飞书取 token 失败: {data.get('code')} {data.get('msg')}")
    return data["tenant_access_token"]


def _get(path: str, tok: str) -> dict:
    req = urllib.request.Request(f"{_API}{path}", headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
        data = json.loads(r.read().decode())
    if data.get("code") != 0:
        raise RuntimeError(f"飞书 API 失败 {path}: code={data.get('code')} msg={data.get('msg')}")
    return data.get("data", {})


def resolve_url(url: str, tok: str) -> tuple[str, str, str]:
    """返回 (obj_token, obj_type, title)。支持 wiki 链接与 docx 直链。"""
    url = url.strip()
    if "/wiki/" in url:
        wiki_token = url.rstrip("/").split("/")[-1]
        node = _get(f"/wiki/v2/spaces/get_node?token={wiki_token}", tok).get("node", {})
        return node.get("obj_token", ""), node.get("obj_type", ""), node.get("title", "")
    m = re.search(r"/docx/([A-Za-z0-9]+)", url)
    if m:
        info = _get(f"/docx/v1/documents/{m.group(1)}", tok).get("document", {})
        return m.group(1), "docx", info.get("title", "")
    raise RuntimeError(f"无法识别的飞书链接: {url}")


def fetch_docx_blocks(doc_token: str, tok: str) -> list[dict]:
    """分页拉取 docx 全部块（骨架接口，块自带内容键）。"""
    blocks, page_token = [], ""
    while True:
        path = f"/docx/v1/documents/{doc_token}/blocks?page_size=500"
        if page_token:
            path += f"&page_token={page_token}"
        data = _get(path, tok)
        blocks.extend(data.get("items", []))
        page_token = data.get("page_token", "")
        if not page_token or data.get("has_more") is False:
            break
        if len(blocks) > 20000:  # 防呆
            break
    return blocks


# ── 块 → Markdown（内容键驱动）───────────────────────────────
def _elements_text(node: dict) -> str:
    """块内 text.elements[] → 纯文本（text_run / mention / equation 等）。"""
    out = []
    for el in (node.get("elements") or []):
        if "text_run" in el:
            out.append(el["text_run"].get("content", ""))
        elif "mention" in el:
            out.append(el["mention"].get("text", "") or "")
        elif "equation" in el:
            out.append(f"${el['equation'].get('content', '')}$")
        elif "inline_block" in el:
            out.append("")
    return "".join(out)


def _block_md(b: dict) -> tuple[str, str] | None:
    """按内容键渲染单块 → (prefix, text)；无内容返回 None。"""
    if "image" in b and isinstance(b["image"], dict):
        tok = b["image"].get("token", "")
        return ("", f"> [!NOTE] 🖼️ 图片（token: {tok}，图片下载为二期）") if tok else None
    if "text" in b and isinstance(b["text"], dict):
        t = _elements_text(b["text"]).strip()
        return ("", t) if t else None
    for key, prefix in {
        "heading1": "# ", "heading2": "## ", "heading3": "### ", "heading4": "#### ",
        "heading5": "##### ", "heading6": "###### ", "heading7": "####### ", "heading8": "######## ",
        "bullet": "- ", "quote": "> ", "callout": "> [!NOTE] ",
    }.items():
        node = b.get(key)
        if isinstance(node, dict):
            t = _elements_text(node).strip()
            if t:
                return (prefix, t)
    # 有序列表（ordered）
    node = b.get("ordered")
    if isinstance(node, dict):
        t = _elements_text(node).strip()
        if t:
            return ("", t)  # 前缀由渲染侧按同级编号
    # 待办 todo
    node = b.get("todo")
    if isinstance(node, dict):
        t = _elements_text(node).strip()
        if t:
            return ("- [x] " if node.get("done") else "- [ ] ", t)
    # 代码块
    node = b.get("code")
    if isinstance(node, dict):
        lang = node.get("language", "") or ""
        inner = node.get("text")
        elements = inner.get("elements") if isinstance(inner, dict) else None
        t = _elements_text({"elements": elements}) if elements else str(inner or "").strip()
        if t:
            return (f"```{lang}\n", t + "\n```")
    return None


def render_blocks(blocks: list[dict]) -> str:
    """按文档顺序渲染全部块为 Markdown（children 递归，ordered 同级编号）。"""
    by_id = {b.get("block_id"): b for b in blocks if b.get("block_id")}
    lines: list[str] = []

    def walk(bid: str, depth: int, ordered_ix: list[int]) -> None:
        b = by_id.get(bid)
        if not b:
            return
        indent = "  " * max(0, depth - 1)
        is_ordered = "ordered" in b
        if is_ordered:
            r = _block_md(b)
            if r:
                ordered_ix[0] += 1
                lines.append(f"{indent}{ordered_ix[0]}. {r[1]}")
        else:
            ordered_ix[0] = 0
            if "divider" in b:
                lines.append("---")
            else:
                r = _block_md(b)
                if r:
                    lines.append(f"{indent}{r[0]}{r[1]}")
        for child in (b.get("children") or []):
            walk(child, depth + 1, ordered_ix)

    for b in blocks:
        walk(b.get("block_id"), 0, [0])
    return "\n".join(lines).strip()


def fetch_doc(url: str) -> tuple[str, str]:
    """读取飞书文档 → (title, markdown)。"""
    tok = tenant_token()
    doc_token, obj_type, title = resolve_url(url, tok)
    if obj_type != "docx":
        raise RuntimeError(f"暂只支持 docx 文档（当前类型: {obj_type}）")
    blocks = fetch_docx_blocks(doc_token, tok)
    md = render_blocks(blocks)
    return title or doc_token, md


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print(f"用法: {sys.argv[0]} <飞书 wiki/docx 链接>")
        sys.exit(1)
    t, md = fetch_doc(sys.argv[1])
    print(f"# 标题: {t}\n---\n{md[:3000]}")
