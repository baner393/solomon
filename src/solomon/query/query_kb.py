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
    main_hits, tmp_hits = [], []
    if scope in ("all", "main"):
        hits = search(q, top_n=top_n * 3)
        main_hits = [(*h, "main") for h in _filter_scope(hits, category, top_n * 3)]
    if scope in ("all", "tmp") and _TMP_KB and os.path.exists(_TMP_KB):
        try:
            hits = search(q, top_n=top_n * 3, db_path=_TMP_KB)
            tmp_hits = [(*h, "tmp") for h in _filter_scope(hits, category, top_n * 3)]
        except Exception:  # noqa: BLE001 临时库损坏不应阻断主查询
            pass
    if scope == "tmp":
        return tmp_hits[:top_n]
    if scope == "main":
        return main_hits[:top_n]
    # all：临时命中保底一席。临时区内容少而短，bm25 上常被主库长文挤出 top_n，
    # 导致「刚临时读取完就查不到」（2026-09-26 实测）；保底让临时内容始终可见（📌）。
    merged = []
    if tmp_hits:
        merged.append(tmp_hits[0])
    merged += main_hits + tmp_hits[1:]
    # 跨库统一按 rank 排（临时库与主库同表结构，rank 语义一致），保底席之外正常竞争
    rest = sorted(merged[1:], key=lambda h: h[4])[: top_n - 1]
    return merged[:1] + rest


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


# ================= 图文回答（命中页面配图 → LLM 引用 → 渠道 MEDIA: 发图） =================
# 入库侧 --images 已把识图说明写进页面（图前文后：embed 行 + 紧跟的教学说明行）；
# 查询侧把「图 + 已有说明」带给 LLM，LLM 按需在解释段引用（MEDIA:<路径> 独立行），
# hermes 发送管道把 MEDIA: 解析成渠道原生附件（QQ/微信/飞书均支持）——不重新识图。
_IMG_EXTS = (".jpg", ".jpeg", ".png", ".gif", ".webp")
_EMBED_RE = re.compile(r"!\[\[([^\]|]+?\.(?:jpg|jpeg|png|gif|webp))(?:\|[^\]]*)?\]\]", re.I)


def _vault_image_index():
    """vault 图片 basename(小写) → 绝对路径 索引（raw/assets 与 assets 两大来源）。"""
    idx = {}
    for sub in ("raw/assets", "assets"):
        root = os.path.join(VAULT, sub)
        if not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(_IMG_EXTS):
                    idx.setdefault(f.lower(), os.path.join(dirpath, f))
    return idx


def _locate_image(name, idx):
    """页面 embed 图名 → vault 实际路径。兼容带路径 embed 与冒号↔横杠变体
    （笔记引用冒号原名 / wiki 侧横杠名是既有惯例）。"""
    base = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    for cand in (base, base.replace(":", "-"), base.replace("-", ":")):
        hit = idx.get(cand.lower())
        if hit:
            return hit
    return None


def _collect_page_images(hits, max_images=12):
    """命中页面的可用配图清单 [(vault_abs_path, source_title, caption)]。

    说明（caption）取 embed 行后第一条非空非 embed 文本（「图前文后」渲染规范：
    教学说明紧跟图片下一行）。定位失败的图静默跳过（临时区图不在主库索引）。"""
    idx = _vault_image_index()
    out, seen = [], set()
    for path, cat, title, snippet, rank, tag in hits:
        body = read_page_content(path, tag)
        lines = body.splitlines()
        for i, line in enumerate(lines):
            m = _EMBED_RE.search(line)
            if not m:
                continue
            ap = _locate_image(m.group(1), idx)
            if not ap or ap in seen:
                continue
            caption = ""
            for nxt in lines[i + 1:i + 4]:
                nxt = nxt.strip()
                if nxt and not _EMBED_RE.search(nxt):
                    caption = nxt[:80]
                    break
            seen.add(ap)
            out.append((ap, title, caption))
            if len(out) >= max_images:
                return out
    return out


def _sanitize_media_lines(answer, images):
    """剔除不在配图白名单里的 MEDIA: 行（防 LLM 幻觉路径）；合法引用保留。"""
    if "MEDIA:" not in answer:
        return answer
    allow = {ap for ap, *_ in images}
    kept = []
    for line in answer.splitlines():
        if line.strip().startswith("MEDIA:"):
            p = line.strip()[6:].strip()
            if p not in allow or not os.path.exists(p):
                continue
        kept.append(line)
    return "\n".join(kept)


# ================= 查询语义缓存（重复问题秒回，KB 更新自动失效） =================
# 缓存与 FTS 库同放 vault/.kb/（每 vault 独立，随 vault 走；兼容 SOLOMON_FTS_DB 旧环境）。
_KB_DIR = os.environ.get("SOLOMON_FTS_DB", os.path.join(VAULT, ".kb"))
CACHE_PATH = os.path.join(_KB_DIR, "query_cache.json")
CACHE_TTL = 24 * 3600  # 缓存保留 24 小时


def _kb_fingerprint():
    """KB 版本指纹：主库 FTS mtime + index.md mtime + 临时库 mtime。
    任一变化 → 指纹变 → 缓存整体失效。（临时库不进指纹会导致临时读取/清理后
    命中旧缓存，查询结果缺临时内容）"""
    db = os.path.join(_KB_DIR, "kb_fts.db")
    parts = []
    paths = [db, INDEX_PATH]
    if _TMP_KB:
        paths.append(_TMP_KB)
    for p in paths:
        try:
            parts.append(str(int(os.path.getmtime(p))))
        except OSError:
            parts.append("0")
    # prompt/行为版本标记：回答规范变更时使旧缓存整体失效
    # （2026-10-03 图文回答上线——旧缓存答案无 MEDIA: 引图，会残留 24h）
    parts.append("v2-media")
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
    # 可用配图清单（命中页面里的真实图片 + 入库时写好的教学说明）
    images = _collect_page_images(hits)
    media_block = ""
    if images:
        lines = ["可用配图（vault 实际文件；引图时单独一行输出 MEDIA:<完整路径>）："]
        for ap, src_title, cap in images:
            lines.append(f"- MEDIA:{ap}（来自[[{src_title}]]）" + (f" 图意：{cap}" if cap else ""))
        media_block = "\n".join(lines)
    system = (
        "你是 Solomon 知识库问答管家。根据提供的知识库页面内容回答用户问题。\n"
        "回答结构固定为三段：\n"
        "**结论**：直接回答\n"
        "**依据**：列出引用的页面（用 [[页面名]] 格式）和相关内容\n"
        "**延伸**：相关知识关联、未覆盖的方向\n"
        "如果检索内容不足以回答，明确说'知识库中没有直接答案'，并列出最接近的相关页面。\n"
        + (
            "图文规范：\n"
            "- 按需引图：当某张图能直观辅助解释（操作演示/界面截图/结构图/对比）时才引，"
            "纯概念叙述不要硬塞图；\n"
            "- 引图格式：在该段解释后单独一行输出 MEDIA:<完整路径>，图前用一句话说明"
            "这张图展示了什么；一段文字配一张图，图文交替；\n"
            "- 只准引用「可用配图」清单里的路径，清单里没有相关图就纯文字回答。\n"
            if images else ""
        )
    )
    user = (
        f"问题：{question}\n\n"
        f"知识库相关页面（FTS5 检索 top-{len(hits)}）：\n{context}\n\n"
        + (f"{media_block}\n\n" if media_block else "")
        + "请按 结论/依据/延伸 三部分回答。"
    )
    try:
        answer = llm_chat(system, user, temperature=0.2, stream=True)
        answer = _sanitize_media_lines(answer, images)
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
