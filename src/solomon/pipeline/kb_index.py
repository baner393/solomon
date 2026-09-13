#!/usr/bin/env python3
"""
kb_index.py — Solomon 知识库 FTS5 索引层（查询侧 + 入库侧共用）

索引对象：vault 下 concepts/、entities/、comparisons/、queries/、raw/articles/
  （.md 文件；排除 index.md / log.md / SCHEMA.md / .obsidian / assets）

分词：FTS5 trigram tokenizer（中文/英文均支持，无需 jieba）

用法：
    python3 kb_index.py build              # 全量重建
    python3 kb_index.py update             # 增量更新（按 mtime）
    python3 kb_index.py update --file X.md # 单文件更新（入库后调用）
    python3 kb_index.py search "关键词" [top_n]   # 检索（供 query_kb.py 调用）
    python3 kb_index.py status             # 显示索引统计

索引库：~/.hermes/infra/data/kb_fts.db
"""

import os
import re
import sys
import sqlite3
import time
import glob

# ---- 路径配置 ----
VAULT = os.environ.get("SOLOMON_VAULT", "/mnt/d/all/ai_agent_about/solomon")
# FTS5 索引库：默认放 vault/.kb/（每 vault 独立索引，天然隔离，随 vault 走）。
# 兼容旧环境：SOLOMON_FTS_DB 显式指定时用它（如迁移期指向旧 ~/.hermes/infra/data/kb_fts.db）。
DATA_DIR = os.environ.get("SOLOMON_FTS_DB", os.path.join(VAULT, ".kb"))
DB_PATH = os.path.join(DATA_DIR, "kb_fts.db")

# 索引范围（目录 → 类别标签）
INDEX_DIRS = {
    "concepts": "concept",
    "entities": "entity",
    "comparisons": "comparison",
    "queries": "query",
    "raw/articles": "raw",
}

# 不索引的文件（根级控制文件）
SKIP_FILES = {"index.md", "log.md", "SCHEMA.md", "README.md"}
SKIP_DIRS = {".obsidian", "assets", ".git"}

# frontmatter 提取（用于标题）
FM_TITLE_RE = re.compile(r"^title:\s*(.+?)\s*$", re.M)


def _db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db(conn):
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS kb_pages USING fts5(
            path UNINDEXED,
            category UNINDEXED,
            title,
            body,
            tokenize = 'trigram'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS kb_meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()


def iter_md_files():
    """遍历 vault 下所有应索引的 .md 文件，产出 (abs_path, category, rel_path)"""
    for subdir, category in INDEX_DIRS.items():
        base = os.path.join(VAULT, subdir)
        if not os.path.isdir(base):
            continue
        for md in glob.glob(os.path.join(base, "**", "*.md"), recursive=True):
            rel = os.path.relpath(md, VAULT)
            # 跳过 SKIP_DIRS（如 assets 下的 markdown）
            parts = rel.split(os.sep)
            if any(p in SKIP_DIRS for p in parts[:-1]):
                continue
            name = os.path.basename(md)
            if name in SKIP_FILES:
                continue
            yield md, category, rel


def extract_body(abs_path):
    """提取正文：去 frontmatter、去 wikilink/图片语法符号，保留文字"""
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as e:
        return None, None
    # frontmatter 标题
    title = None
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    if fm:
        m = FM_TITLE_RE.search(fm.group(1))
        if m:
            title = m.group(1).strip()
        text = text[fm.end():]
    # 去代码块（保留语言标签提示）
    text = re.sub(r"```.*?```", " [code] ", text, flags=re.S)
    # 去行内代码
    text = re.sub(r"`[^`]*`", " ", text)
    # 去图片 ![[x]] 与 ![](x)
    text = re.sub(r"!\[\[[^\]]*\]\]", " ", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    # wikilink [[页面名|别名]] → 保留页面名
    text = re.sub(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", r"\1", text)
    # 普通链接 [文字](url) → 保留文字
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    if title is None:
        title = os.path.splitext(os.path.basename(abs_path))[0]
    return title, text


def build_full(verbose=True):
    conn = _db()
    init_db(conn)
    conn.execute("DELETE FROM kb_pages")
    count = 0
    for abs_path, category, rel in iter_md_files():
        title, body = extract_body(abs_path)
        if title is None:
            continue
        mtime = os.path.getmtime(abs_path)
        conn.execute(
            "INSERT INTO kb_pages(path, category, title, body) VALUES (?,?,?,?)",
            (rel, category, title, body),
        )
        count += 1
    conn.execute(
        "INSERT OR REPLACE INTO kb_meta(key, value) VALUES ('last_full_index', ?)",
        (str(time.time()),),
    )
    conn.commit()
    if verbose:
        print(f"全量索引完成：{count} 个页面 → {DB_PATH}")
    conn.close()
    return count


def get_indexed_mtimes(conn):
    rows = conn.execute("SELECT path FROM kb_pages").fetchall()
    return {r[0] for r in rows}


def update_incremental(verbose=True, only_file=None):
    conn = _db()
    init_db(conn)
    indexed = get_indexed_mtimes(conn)
    changed = 0
    added = 0
    for abs_path, category, rel in iter_md_files():
        if only_file and rel != only_file and abs_path != only_file:
            continue
        mtime = os.path.getmtime(abs_path)
        row = conn.execute("SELECT 1 FROM kb_pages WHERE path=?", (rel,)).fetchone()
        if row is None:
            # 新增页面
            title, body = extract_body(abs_path)
            if title is None:
                continue
            conn.execute(
                "INSERT INTO kb_pages(path, category, title, body) VALUES (?,?,?,?)",
                (rel, category, title, body),
            )
            added += 1
        else:
            # 检查 mtime 是否变化（存在则更新）
            old = conn.execute(
                "SELECT substr(body,1,0) FROM kb_pages WHERE path=?", (rel,)
            ).fetchone()
            # 简单策略：始终刷新内容（本库规模小，重写成本低）
            conn.execute("DELETE FROM kb_pages WHERE path=?", (rel,))
            title, body = extract_body(abs_path)
            if title is None:
                continue
            conn.execute(
                "INSERT INTO kb_pages(path, category, title, body) VALUES (?,?,?,?)",
                (rel, category, title, body),
            )
            changed += 1
    conn.commit()
    if verbose:
        print(f"增量更新：新增 {added}，刷新 {changed} → {DB_PATH}")
    conn.close()
    return added + changed


def index_file(abs_path):
    """单文件更新（入库后自动调用）。返回 (rel_path, category) 或 None。"""
    rel = None
    category = None
    for subdir, cat in INDEX_DIRS.items():
        base = os.path.join(VAULT, subdir)
        if abs_path.startswith(base + os.sep) or abs_path == base:
            rel = os.path.relpath(abs_path, VAULT)
            category = cat
            break
    if rel is None:
        return None
    conn = _db()
    init_db(conn)
    conn.execute("DELETE FROM kb_pages WHERE path=?", (rel,))
    title, body = extract_body(abs_path)
    if title is not None:
        conn.execute(
            "INSERT INTO kb_pages(path, category, title, body) VALUES (?,?,?,?)",
            (rel, category, title, body),
        )
    conn.commit()
    conn.close()
    return rel


def build_match_expr(query):
    """把自然语言查询拆成 FTS5 可匹配的词组（trigram 兼容）。

    trigram tokenizer 只对 ≥3 字符的连续串建索引，因此：
    - 中文：优先 jieba 分词取 ≥3 字有效词（过滤停用词），拆不出再退回 ≥3 字连续串
    - 英文：保留 ≥3 字符的单词
    - 用 OR 连接，任一命中即返回
    """
    terms = []
    # ① jieba 分词（可用时）——自然问句拆出有效词，避免整句当短语匹配导致 0 命中
    try:
        import jieba  # noqa: F401
        _STOP = {
            "概括", "一下", "这个", "那个", "知识", "什么", "关于", "从", "到",
            "的", "是", "我", "你", "他", "知道", "介绍", "讲讲", "说", "了",
            "吗", "呢", "啊", "和", "与", "或", "在", "有", "一个", "哪些",
            "怎么", "如何", "请", "给我", "讲讲", "聊", "聊聊", "想", "了解",
        }
        for w in jieba.lcut(query):
            w = w.strip()
            if len(w) >= 3 and w not in _STOP and not w.isascii():
                if w not in terms:
                    terms.append(w)
    except ImportError:
        pass
    # ② 兜底：中文连续串 ≥3 字（jieba 不可用或没拆出词时）
    if not terms:
        for m in re.findall(r"[\u4e00-\u9fff]{3,}", query):
            if m not in terms:
                terms.append(m)
    # ③ 英文单词（≥3 字符）
    for m in re.findall(r"[A-Za-z]{3,}", query):
        if m not in terms:
            terms.append(m)
    if not terms:
        return None
    return " OR ".join(f'"{t}"' for t in terms)


def search(query, top_n=10):
    """FTS5 检索。返回 [(rel_path, category, title, snippet, rank)]"""
    conn = _db()
    init_db(conn)
    # trigram 模式：中文需 ≥3 字、英文需 ≥3 字符；拆词 OR 匹配
    q = query.strip()
    if not q:
        conn.close()
        return []
    match_expr = build_match_expr(q)
    if match_expr is None:
        conn.close()
        return like_search(query, top_n)
    try:
        rows = conn.execute(
            """
            SELECT path, category, title, snippet(kb_pages, 3, '…', '…', '…', 12), rank
            FROM kb_pages
            WHERE kb_pages MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (match_expr, top_n),
        ).fetchall()
    except sqlite3.OperationalError:
        # 语法错误（特殊字符）时退化为 LIKE
        conn.close()
        return like_search(query, top_n)
    conn.close()
    return rows


def like_search(query, top_n=10):
    """FTS 失败时的 LIKE 兜底（大小写不敏感 + 词包含）"""
    conn = _db()
    results = []
    for term in re.split(r"[\s,，。；;]+", query):
        if not term:
            continue
        pat = f"%{term}%"
        rows = conn.execute(
            """
            SELECT path, category, title, substr(body,1,120), 0.0
            FROM kb_pages WHERE title LIKE ? OR body LIKE ?
            LIMIT ?
            """,
            (pat, pat, top_n),
        ).fetchall()
        results.extend(rows)
    # 去重保序
    seen = set()
    out = []
    for r in results:
        if r[0] not in seen:
            seen.add(r[0])
            out.append(r)
    conn.close()
    return out[:top_n]


def status():
    conn = _db()
    init_db(conn)
    total = conn.execute("SELECT COUNT(*) FROM kb_pages").fetchone()[0]
    by_cat = conn.execute(
        "SELECT category, COUNT(*) FROM kb_pages GROUP BY category ORDER BY 2 DESC"
    ).fetchall()
    last = conn.execute("SELECT value FROM kb_meta WHERE key='last_full_index'").fetchone()
    conn.close()
    print(f"索引库：{DB_PATH}")
    print(f"页面总数：{total}")
    for cat, cnt in by_cat:
        print(f"  {cat}: {cnt}")
    if last:
        print(f"上次全量索引：{time.strftime('%Y-%m-%d %H:%M', time.localtime(float(last[0])))}")


if __name__ == "__main__":
    args = sys.argv[1:]
    cmd = args[0] if args else "status"
    if cmd == "build":
        build_full()
    elif cmd == "update":
        if len(args) > 2 and args[1] == "--file":
            index_file(args[2])
            print(f"已索引: {args[2]}")
        else:
            update_incremental()
    elif cmd == "search":
        if len(args) < 2:
            print("用法: kb_index.py search <关键词> [top_n]", file=sys.stderr)
            sys.exit(1)
        n = int(args[2]) if len(args) > 2 else 10
        for row in search(args[1], n):
            print(f"  [{row[1]}] {row[2]}  ({row[0]})")
            print(f"    …{row[3]}…")
    elif cmd == "status":
        status()
    else:
        print(__doc__)
