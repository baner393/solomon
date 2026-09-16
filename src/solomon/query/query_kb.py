#!/usr/bin/env python3
"""
query_kb.py — Solomon 知识库问答（FTS5 主路径 + 分级查询兜底）

流程：
  1. 读 index.md 分类/关联表 → 圈定检索范围（可选 --category）
  2. FTS5 检索 top-N 页面（含命中片段）
  3. LLM 综合回答：结论 → 依据(来源页+引用) → 延伸相关
  4. FTS5 无命中 → 降级 index→wiki→raw 分级查询（LLM 直接读 index 定位）

用法：
    python3 query_kb.py "关于Codex我知道什么"
    python3 query_kb.py "RAG 原理" --category concept
    python3 query_kb.py "XXX" --raw        # 显示原始检索结果（不调 LLM）
    python3 query_kb.py "XXX" --top 5       # 检索深度

依赖：kb_index.py（FTS5 索引）+ llm_client.py（SenseNova）
"""

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline"))
from _paths import default_temp_root, default_vault  # noqa: E402

VAULT = default_vault()
TEMP_ROOT = default_temp_root()
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "pipeline"))
from kb_index import search, like_search, status  # noqa: E402
from llm_client import llm_chat  # noqa: E402

INDEX_PATH = os.path.join(VAULT, "index.md")

# 临时学习区独立 FTS 库（--peek 产物索引；随临时区走，与主库隔离）
_TMP_KB = os.path.join(TEMP_ROOT, ".kb", "kb_fts.db") if TEMP_ROOT else ""


def read_index_categories():
    """从 index.md 提取分类/主题，供圈定范围用。返回 [(分类名, 描述)]"""
    if not os.path.exists(INDEX_PATH):
        return []
    cats = []
    with open(INDEX_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line.startswith("###") or line.startswith("##"):
                cats.append(line.lstrip("#").strip())
    return cats


def _filter_scope(hits, category, top_n):
    """按分类过滤 + 截断（对单个库的检索结果）。"""
    if category:
        hits = [h for h in hits if h[1] == category]
    return hits[:top_n]


def gather_evidence(question, top_n=5, category=None, scope="all"):
    """FTS5 联合检索。返回命中页面列表 [(path, category, title, snippet, rank, tag)]

    scope: all=主库+临时库联合（临时命中标 tag='tmp'）；tmp=只查临时库；
           main=只查主库。临时命中在回答/来源里标「📌 临时」。
    """
    q = question.strip()
    all_hits = []
    if scope in ("all", "main"):
        hits = search(q, top_n=top_n * 3)
        all_hits += [(*h, "main") for h in _filter_scope(hits, category, top_n * 3)]
    if scope in ("all", "tmp") and _TMP_KB and os.path.exists(_TMP_KB):
        try:
            hits = search(q, top_n=top_n * 3, db_path=_TMP_KB)
            all_hits += [(*h, "tmp") for h in _filter_scope(hits, category, top_n * 3)]
        except Exception:  # noqa: BLE001 临时库损坏不应阻断主查询
            pass
    # 跨库统一按 rank 排，取 top_n（主库与临时库同表结构，rank 语义一致）
    all_hits.sort(key=lambda h: h[4])
    return all_hits[:top_n]


def read_page_content(path, tag="main"):
    """读页面正文（去 frontmatter），限制长度。tag='tmp' 读临时学习区。"""
    root = TEMP_ROOT if tag == "tmp" else VAULT
    abs_path = os.path.join(root, path)
    if not os.path.exists(abs_path):
        return ""
    with open(abs_path, encoding="utf-8") as f:
        text = f.read()
    text = re.sub(r"^---\n.*?\n---\n", "", text, flags=re.S)
    return text[:3000]  # 每页最多 3000 字


# ================= 查询语义缓存（重复问题秒回，KB 更新自动失效） =================
# 缓存与 FTS 库同放 vault/.kb/（每 vault 独立，随 vault 走；兼容 SOLOMON_FTS_DB 旧环境）。
_KB_DIR = os.environ.get("SOLOMON_FTS_DB", os.path.join(VAULT, ".kb"))
CACHE_PATH = os.path.join(_KB_DIR, "query_cache.json")
CACHE_TTL = 24 * 3600  # 缓存保留 24 小时


def _kb_fingerprint():
    """KB 版本指纹：FTS 库 mtime + index.md mtime。任一变化 → 指纹变 → 缓存整体失效。"""
    db = os.path.join(_KB_DIR, "kb_fts.db")
    parts = []
    for p in (db, INDEX_PATH):
        try:
            parts.append(str(int(os.path.getmtime(p))))
        except OSError:
            parts.append("0")
    return ":".join(parts)


def _load_cache():
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"fingerprint": "", "entries": {}}


def _save_cache(cache):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
    except OSError:
        pass


def _cache_get(question):
    """查缓存：命中且未过期且 KB 指纹一致 → 返回答案；否则 None。"""
    cache = _load_cache()
    if cache.get("fingerprint") != _kb_fingerprint():
        return None  # KB 已更新，缓存整体失效
    entry = cache.get("entries", {}).get(question.strip().lower())
    if not entry:
        return None
    if time.time() - entry.get("ts", 0) > CACHE_TTL:
        return None
    return entry.get("answer")


def _cache_put(question, answer):
    """写入缓存（KB 指纹变了会重建）。"""
    cache = _load_cache()
    fp = _kb_fingerprint()
    if cache.get("fingerprint") != fp:
        cache = {"fingerprint": fp, "entries": {}}
    cache["entries"][question.strip().lower()] = {"answer": answer, "ts": time.time()}
    _save_cache(cache)


def answer_with_llm(question, hits):
    """LLM 综合回答：结论 → 依据 → 延伸（流式输出到 stdout）。

    成功时答案已逐 token 流式打印，返回完整文本（调用方不再重复打印）；
    失败返回错误串（以「（LLM 调用失败」开头，调用方需打印）。
    """
    if not hits:
        return None
    # ① 语义缓存：命中直接返回（KB 未变 + 未过期），跳过 LLM
    cached = _cache_get(question)
    if cached is not None:
        print(cached)
        print()  # 空行，与「来源页」隔开
        return cached
    # 组装上下文
    ctx_parts = []
    for path, cat, title, snippet, rank, tag in hits:
        is_tmp = tag == "tmp"
        mark = "📌 临时" if is_tmp else ""
        body = read_page_content(path, tag)
        ctx_parts.append(f"### {mark}[{cat}] {title}（{path}）\n{body}")
    context = "\n\n".join(ctx_parts)[:15000]
    system = (
        "你是 Solomon 知识库问答管家。根据提供的知识库页面内容回答用户问题。\n"
        "回答结构固定为三段：\n"
        "**结论**：直接回答\n"
        "**依据**：列出引用的页面（用 [[页面名]] 格式）和相关内容\n"
        "**延伸**：相关知识关联、未覆盖的方向\n"
        "如果检索内容不足以回答，明确说'知识库中没有直接答案'，并列出最接近的相关页面。"
    )
    user = (
        f"问题：{question}\n\n"
        f"知识库相关页面（FTS5 检索 top-{len(hits)}）：\n{context}\n\n"
        "请按 结论/依据/延伸 三部分回答。"
    )
    try:
        answer = llm_chat(system, user, temperature=0.2, stream=True)
        _cache_put(question, answer)
        return answer
    except Exception as e:
        lines = [f"（LLM 调用失败: {e}）", ""]
        for p, c, t, s, r, tg in hits:
            mark = "📌 临时 " if tg == "tmp" else ""
            lines.append(f"- {mark}[[{t}]]（{p}）")
        return "\n".join(lines)


def fallback_hierarchical(question):
    """FTS5 无命中时降级：读 index.md → 找相关页面名 → 读页面"""
    if not os.path.exists(INDEX_PATH):
        return "（index.md 不存在，无法分级查询）"
    with open(INDEX_PATH, encoding="utf-8") as f:
        index_text = f.read()
    # 提取所有 [[页面名]]
    pages = re.findall(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", index_text)
    # 关键词匹配：问题中的词在页面名或描述中命中
    q_words = set(re.findall(r"[\u4e00-\u9fffA-Za-z]{2,}", question))
    relevant = []
    for p in pages:
        if any(w in p for w in q_words):
            relevant.append(p)
    if not relevant:
        return "知识库中没有找到直接相关内容。\n相关页面索引可查 index.md：\n" + "\n".join(
            f"- [[{p}]]" for p in pages[:15]
        )
    # 读最相关的页面
    parts = [f"知识库分级查询命中 {len(relevant)} 页："]
    for p in relevant[:3]:
        body = read_page_content(f"concepts/{p}.md") or read_page_content(f"entities/{p}.md")
        parts.append(f"### {p}\n{body[:1500]}")
    return "\n\n".join(parts)


def main():
    ap = argparse.ArgumentParser(description="Solomon 知识库问答")
    ap.add_argument("question", nargs="+", help="问题（可多词）")
    ap.add_argument("--category", help="限定分类 concept/entity/raw")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--raw", action="store_true", help="只显示检索结果，不调 LLM")
    ap.add_argument("--scope", choices=("all", "tmp", "main"), default="all",
                    help="检索范围：all=主库+临时学习区联合（临时命中标📌）；"
                         "tmp=只查临时学习区；main=只查主库")
    args = ap.parse_args()
    question = " ".join(args.question)

    hits = gather_evidence(question, top_n=args.top, category=args.category,
                           scope=args.scope)

    if args.raw:
        print(f"=== FTS5 检索结果（top-{len(hits)}，scope={args.scope}）===")
        for path, cat, title, snippet, rank, tag in hits:
            mark = "📌 临时 " if tag == "tmp" else "     "
            print(f"  {mark}[{cat}] {title}  ({path})")
            print(f"    …{snippet}…")
        if args.scope != "tmp":
            print(f"知识库分类：{read_index_categories()}")
        return

    if hits:
        answer = answer_with_llm(question, hits)
        # 成功时答案已流式打印到 stdout；只有失败串（以「（LLM 调用失败」开头）才需补打印
        if answer and answer.startswith("（LLM 调用失败"):
            print(answer)
        # 附检索到的来源页（临时来源标 📌）
        print("\n---\n📚 来源页：")
        for path, cat, title, snippet, rank, tag in hits:
            mark = "📌 临时 " if tag == "tmp" else ""
            print(f"- {mark}[[{title}]]（{path}）")
    else:
        # 降级
        print("（FTS5 无命中，降级分级查询）\n")
        print(fallback_hierarchical(question))


if __name__ == "__main__":
    main()
