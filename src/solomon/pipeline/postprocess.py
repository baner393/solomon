#!/usr/bin/env python3
"""
postprocess.py — 入库后自动闭环（任何来源写入后自动执行，不手动）

功能（顺序执行，失败不丢知识，只报告）：
  1. FTS5 增量索引更新（新文件）
  2. 相关页推荐：FTS5 查新页面主题 → 生成 [[wikilink]] 写入「相关页面」
     （写入后再增量索引一次）
  3. index.md 自动更新（按分类插入条目 + Total pages 计数）
  4. log.md 自动追加
  5. verify_gate：verify_solomon.py 校验图片/链接/frontmatter
  6. 结构化汇报

用法：
    python3 postprocess.py <新写入的文件绝对路径> [--page-title 标题] [--skip-verify]

退出码：0 = 全部完成（含"无需处理"）；1 = 有未完成项（知识已入库但善后不完整）
"""

import os
import re
import sys
import time
import datetime
import subprocess

# ---- 路径 ----
from _paths import default_vault  # noqa: E402
from _paths import _PKG_ROOT, default_python  # noqa: E402

VAULT = default_vault()
VERIFY_SCRIPT = os.environ.get(
    "VERIFY_SOLOMON",
    str(_PKG_ROOT / "assets" / "verify_solomon.py"),
)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kb_index import index_file, search, build_full  # noqa: E402

CATEGORY_LABELS = {
    "concept": "概念",
    "entity": "实体",
    "comparison": "对比",
    "query": "查询",
    "raw": "原文",
}


def log(msg):
    print(f"[postprocess] {msg}")


# ---------- 1. FTS5 索引 ----------
def step_fts(new_file):
    rel = index_file(new_file)
    if rel:
        log(f"FTS5 已索引: {rel}")
    else:
        log(f"⚠️ 文件不在索引范围内（跳过 FTS）: {new_file}")
    return rel


# ---------- 2. 相关页推荐 ----------
# 从正文提取检索关键词（FTS5 trigram 需要 ≥3 字符的连续串才有效）
STOP_WORDS = {
    "五层知识提炼", "知识提炼", "核心内容", "相关页面", "来源", "以及",
    "这个", "这些", "可以", "需要", "使用", "进行", "一个", "一种", "通过",
}


def extract_query_terms(new_file, page_title, max_terms=4):
    """提取检索词：优先正文长串关键词，次选标题去后缀。

    短页面（正文 <500 字）先用 LLM 生成关键词摘要再提取（短页面直接取词
    噪声大、命中率低——技术债「相关页推荐增强」）。
    """
    terms = []
    try:
        with open(new_file, "r", encoding="utf-8") as f:
            body = f.read()
        # frontmatter 去掉
        body = re.sub(r"^---\n.*?\n---\n", "", body, flags=re.S)
        # 短页面增强：LLM 关键词摘要前置
        if len(body.strip()) < 500:
            try:
                from llm_client import llm_chat
                summary = llm_chat(
                    "你是知识库索引助手，输出极简关键词。",
                    "请用 4-8 个关键词概括以下内容的核心主题"
                    f"（只要关键词，空格分隔，不要解释）：\n{body[:2000]}",
                    max_tokens=200)
                if summary and "❌" not in summary:
                    body = summary + "\n" + body
                    log("短页面 → 已用 LLM 摘要辅助提取检索词")
            except Exception as e:
                log(f"⚠️ 短页面 LLM 摘要失败（回退正文取词）: {e}")
        # 收集长度 ≥3 的中文/英文片段（排除标点、停用词）
        cands = re.findall(r"[\u4e00-\u9fffA-Za-z]{3,}", body)
        for c in cands:
            c = c.strip()
            if c in STOP_WORDS or len(c) < 3:
                continue
            if c not in terms:
                terms.append(c)
            if len(terms) >= max_terms:
                break
    except OSError:
        pass
    if not terms and page_title:
        t = re.sub(r"-五层知识提炼|_notes|_raw|\.md|（.*?）", "", page_title)
        terms = [x for x in re.split(r"[\s\-_·:：,，。；;]+", t) if len(x) >= 3][:max_terms]
    return terms


def step_related_pages(new_file, page_title, max_links=3):
    """用 FTS5 查新页面主题，找相关已有页面，写入「相关页面」section。"""
    rel = os.path.relpath(new_file, VAULT)
    terms = extract_query_terms(new_file, page_title)
    if not terms:
        log("相关页推荐：无检索词（跳过）")
        return []
    candidates = []
    seen = set()
    for term in terms:
        hits = search(term, top_n=5)
        for path, category, title, snippet, rank in hits:
            if path == rel:
                continue
            name = os.path.splitext(os.path.basename(path))[0]
            if name in seen:
                continue
            # 只推荐「页面文件真实存在」的（FTS 库可能有已删页残留索引，
            # 直接引用会产生断链幽灵链接——2026-09-14 实测）
            cand_path = os.path.join(VAULT, path.replace("\\", "/"))
            if not os.path.exists(cand_path):
                continue
            seen.add(name)
            if category in ("concept", "entity", "comparison"):
                candidates.append(name)
            if len(candidates) >= max_links:
                break
        if len(candidates) >= max_links:
            break
    if not candidates:
        log(f"相关页推荐：无命中（检索词: {terms}）")
        return []
    # 读文件，替换或追加「相关页面」section
    with open(new_file, "r", encoding="utf-8") as f:
        content = f.read()
    links = " ".join(f"[[{c}]]" for c in candidates)
    if "## 相关页面" in content:
        # 已有 section：在标题下插入一行（去重）
        section = "## 相关页面\n"
        lines = content.split("\n")
        out = []
        inserted = False
        for i, line in enumerate(lines):
            out.append(line)
            if line.strip() == "## 相关页面" and not inserted:
                # 收集已有链接
                existing = re.findall(r"\[\[([^\]|]+)\]\]", "\n".join(lines[i:i+6]))
                new_links = [c for c in candidates if c not in existing]
                if new_links:
                    out.append("")
                    out.append(" ".join(f"[[{c}]]" for c in new_links))
                inserted = True
        content = "\n".join(out)
    else:
        # 无 section：在「来源」前或文件末尾追加
        if "## 来源" in content:
            content = content.replace(
                "## 来源", f"## 相关页面\n\n{links}\n\n## 来源", 1
            )
        else:
            content = content.rstrip() + f"\n\n## 相关页面\n\n{links}\n"
    with open(new_file, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"相关页推荐：{candidates}")
    # 写入后再次增量索引（含新链接）
    index_file(new_file)
    return candidates


# ---------- 3. index.md 自动更新 ----------
# 分类 → index.md 中的 section 标题模式（模糊匹配）
SECTION_PATTERNS = {
    "concept": [r"### Concepts", r"### 概念"],
    "entity": [r"### Entities", r"### 实体"],
    "comparison": [r"### Comparisons", r"### 对比"],
    "query": [r"### Queries", r"### 查询"],
    "raw": [r"### Raw", r"### 原文"],
}
DEFAULT_SECTION = "## 按分类浏览"


def step_index(new_file, page_title, category):
    """在「按分类浏览」对应分类下插入条目 + Total pages 计数。"""
    index_path = os.path.join(VAULT, "index.md")
    if not os.path.exists(index_path):
        log("⚠️ index.md 不存在，跳过")
        return False
    with open(index_path, "r", encoding="utf-8") as f:
        content = f.read()
    name = os.path.splitext(os.path.basename(new_file))[0]
    title = page_title or name
    entry = f"- [[{name}]] — {title}"

    # 防重复：同名条目已存在 → 不重复插入、不 bump 计数
    if f"[[{name}]]" in content:
        log(f"index.md 已存在同名条目（跳过）: {name}")
        return False

    # 定位分类 section：category 匹配的 ### 块，块末尾 = 下一个 ###/## 之前
    lines = content.split("\n")
    insert_idx = None
    patterns = SECTION_PATTERNS.get(category or "", [])
    for i, line in enumerate(lines):
        if line.startswith("###") and any(re.search(p, line) for p in patterns):
            # 找到块末尾
            j = i + 1
            while j < len(lines) and not (
                lines[j].startswith("###") or lines[j].startswith("## ")
            ):
                j += 1
            insert_idx = j  # 在下一个标题前插入
            break
    if insert_idx is None:
        # 找不到分类块 → 插到「按分类浏览」区块末尾（下一个 ## 前）
        for i, line in enumerate(lines):
            if line.strip() == DEFAULT_SECTION:
                j = i + 1
                while j < len(lines) and not lines[j].startswith("## "):
                    j += 1
                insert_idx = j
                break
    if insert_idx is None:
        # 兜底：追加到文件末尾
        insert_idx = len(lines)

    # bump Total pages
    def bump_total(text):
        m = re.search(r"Total pages:\s*(\d+)", text)
        if m:
            n = int(m.group(1)) + 1
            return text.replace(m.group(0), f"Total pages: {n}", 1)
        # 旧库无该行（init 模板缺失时代）→ 初始化，修「Total pages 永不出现」
        return text.rstrip("\n") + "\n\nTotal pages: 1\n"

    lines.insert(insert_idx, entry)
    content = bump_total("\n".join(lines))
    with open(index_path, "w", encoding="utf-8") as f:
        f.write(content)
    log(f"index.md 已插入条目 + Total pages +1: {name}")
    return True


# ---------- 4. log.md 自动追加 ----------
def step_log(new_file, page_title, category, tags=None, source=None):
    log_path = os.path.join(VAULT, "log.md")
    if not os.path.exists(log_path):
        log("⚠️ log.md 不存在，跳过")
        return False
    today = datetime.date.today().isoformat()
    name = os.path.splitext(os.path.basename(new_file))[0]
    title = page_title or name
    tags_str = ", ".join(tags) if tags else category
    source_str = source or ""
    entry = f"""
## [{today}] ingest | {title}
- {os.path.relpath(new_file, VAULT)}（{category}）
- 标签：{tags_str}
- 来源：{source_str}
"""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(entry)
    log(f"log.md 已追加: {title}")
    return True


# ---------- 5. verify gate ----------
def step_verify(new_file):
    """运行 verify_solomon.py，只把「新页面相关的问题」计入 gate。
    历史欠账（既有页面的断链/缺 frontmatter）不阻塞本次入库，
    但会单独报告行数，提示可另行清理。
    """
    if not os.path.exists(VERIFY_SCRIPT):
        log(f"⚠️ verify_solomon.py 不存在（{VERIFY_SCRIPT}），跳过验证")
        return None
    try:
        result = subprocess.run(
            [default_python(), VERIFY_SCRIPT, VAULT],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
    except subprocess.TimeoutExpired:
        log("⚠️ verify 超时（>300s），跳过")
        return None

    new_name = os.path.splitext(os.path.basename(new_file))[0]
    lines = result.stdout.splitlines()
    # 新页面相关问题：错误行里包含新文件名
    mine = [l for l in lines if new_name in l]
    other = [l for l in lines if new_name not in l and ("❌" in l or "⚠️" in l)]
    for l in mine:
        print(f"  🔴 新页面: {l}")
    if other:
        print(f"  ℹ️ 历史欠账（非本次入库）: {len(other)} 条问题，可另行清理")
    if mine:
        log(f"verify gate: ❌ 新页面有 {len(mine)} 个问题，需修复")
        return False
    log("verify gate: ✅ 新页面零报错（历史欠账不计入）")
    return True


# ---------- 主流程 ----------
def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) < 1:
        print(__doc__)
        sys.exit(1)
    new_file = os.path.abspath(argv[0])
    page_title = None
    skip_verify = False
    category = None
    tags = None
    source = None
    for i, a in enumerate(argv[1:], start=1):
        if a == "--page-title" and i + 1 < len(argv):
            page_title = argv[i + 1]
        elif a == "--category":
            category = argv[i + 1] if i + 1 < len(argv) else None
        elif a == "--tags":
            tags = argv[i + 1].split(",") if i + 1 < len(argv) else None
        elif a == "--source":
            source = argv[i + 1] if i + 1 < len(argv) else None
        elif a == "--skip-verify":
            skip_verify = True

    if not os.path.exists(new_file):
        print(f"❌ 文件不存在: {new_file}")
        sys.exit(1)

    log(f"开始处理: {new_file}")

    # 1. FTS5 索引
    rel = step_fts(new_file)
    if rel:
        # 由路径推断 category（概念页/实体页等）
        if category is None:
            for prefix, cat in [
                ("concepts/", "concept"),
                ("entities/", "entity"),
                ("comparisons/", "comparison"),
                ("queries/", "query"),
                ("raw/", "raw"),
            ]:
                if rel.startswith(prefix):
                    category = cat
                    break
        if category is None:
            category = "concept"

    # 2. 相关页推荐（concepts/entities 才做，raw 原文不做链接推荐）
    if rel and category in ("concept", "entity", "comparison"):
        step_related_pages(new_file, page_title)

    # 3. index.md
    step_index(new_file, page_title, category)

    # 4. log.md
    step_log(new_file, page_title, category, tags=tags, source=source)

    # 5. verify gate
    problems = 0
    if not skip_verify:
        ok = step_verify(new_file)
        if ok is False:
            problems += 1

    # 6. 汇报
    print()
    print("=== postprocess 完成 ===")
    print(f"  文件: {rel or new_file}")
    print(f"  类别: {category}")
    if problems:
        print(f"  ⚠️ {problems} 项善后未完成（知识已入库，可修复后重跑）")
        sys.exit(1)
    print("  ✅ 全部完成")


if __name__ == "__main__":
    main()
