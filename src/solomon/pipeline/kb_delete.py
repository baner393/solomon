#!/usr/bin/env python3
"""kb_delete.py — 删除知识库中的一篇笔记，把它的全部关联清干净。

删除范围（一篇笔记 = 概念页 + 原文 + 图片 + 索引 + 日志 + 反向引用）：
  1. concepts/<title>.md / entities/<title>.md / comparisons/<title>.md（标题匹配或路径匹配）
  2. raw/articles/<title>_raw.md / <title>_notes.md（同标题原文）
  3. raw/assets/<title>/（图片目录，整目录删）
  4. index.md 条目行 + Total pages -1
  5. log.md 中该标题的 ingest 段
  6. FTS 索引 kb_pages 行
  7. 其他页面对它的 [[wikilink]] 引用（断链预防）
"""
from __future__ import annotations

import os
import re
import sys
import shutil

# ---- 路径（与 ingest.py 一致：env 优先，缺省走 _paths 包定位）----
from _paths import default_vault

VAULT = os.environ.get("SOLOMON_VAULT", default_vault())


def _log(msg):
    print(f"[delete] {msg}")


def delete_page(target: str, dry_run: bool = False) -> bool:
    """按标题或文件路径删除一篇笔记及其全部关联。返回是否找到并删除。

    target: 页面标题（如「十分钟了解RAG基本原理」）或相对路径（concepts/x.md）
    或文件名（x.md）。不区分「-五层知识提炼」后缀（删除时自动尝试匹配）。
    """
    if not VAULT or not os.path.isdir(VAULT):
        _log(f"❌ vault 不存在: {VAULT}")
        return False

    # ---- 1. 解析目标：标题 / 文件名 → 候选页面路径 ----
    target = target.strip().rstrip("/")
    base = os.path.basename(target)
    stem = os.path.splitext(base)[0]

    # 候选：直接路径 / 标题匹配（含去掉「-五层知识提炼」后缀的尝试）
    candidates = []
    if os.path.exists(target) and os.path.isfile(target):
        candidates.append(os.path.abspath(target))
    else:
        for sub in ("concepts", "entities", "comparisons", "queries"):
            d = os.path.join(VAULT, sub)
            if not os.path.isdir(d):
                continue
            for fn in os.listdir(d):
                if not fn.endswith(".md"):
                    continue
                fn_stem = os.path.splitext(fn)[0]
                if fn_stem == stem or fn_stem == target:
                    candidates.append(os.path.join(d, fn))
                # 模糊：去掉「-五层知识提炼」再比（LLM 标题尾缀不稳定）
                elif stem and fn_stem.replace("-五层知识提炼", "") == stem.replace("-五层知识提炼", ""):
                    candidates.append(os.path.join(d, fn))

    # 去重 + 只保留 vault 内的
    seen = set()
    pages = []
    for p in candidates:
        p = os.path.abspath(p)
        if p not in seen and p.startswith(os.path.abspath(VAULT)):
            seen.add(p)
            pages.append(p)

    # 标题：优先从匹配到的页文件取；页不存在时用传入目标推断（半删场景仍能清关联）
    if pages:
        page = pages[0]
        title = os.path.splitext(os.path.basename(page))[0]
        subdir = os.path.relpath(os.path.dirname(page), VAULT)
        _log(f"删除笔记: {subdir}/{title}.md")
        for extra in pages[1:]:
            _log(f"  （也匹配: {os.path.relpath(extra, VAULT)}）")
    else:
        page = None
        title = stem
        subdir = "concepts"
        # 页文件不存在（可能已被移走/半删）：仍继续清理 raw/assets/index/log/FTS
        _log(f"删除笔记: {title}（页文件未找到，继续清理关联物）")

    deleted_any = False

    # ---- 2. 页面文件本体（页存在才删；.trash 里的同名页一并处理）----
    if page and os.path.exists(page):
        if dry_run:
            _log(f"  [dry] 删 {os.path.relpath(page, VAULT)}")
        else:
            os.remove(page)
            _log(f"  ✓ 删页面: {os.path.relpath(page, VAULT)}")
        deleted_any = True

    # ---- 2b. .trash 里的同名页（coordinator 手拼 mv 半删遗留）----
    trash_dir = os.path.join(VAULT, ".trash")
    if os.path.isdir(trash_dir):
        for fn in os.listdir(trash_dir):
            if not fn.endswith(".md"):
                continue
            fn_stem = os.path.splitext(fn)[0]
            if fn_stem == title or fn_stem.replace("-五层知识提炼", "") == title.replace("-五层知识提炼", ""):
                p = os.path.join(trash_dir, fn)
                if dry_run:
                    _log(f"  [dry] 删 .trash/{fn}")
                else:
                    os.remove(p)
                    _log(f"  ✓ 删 .trash 残留页: {fn}")
                deleted_any = True

    # ---- 3. raw 原文（_raw.md / _notes.md 及标题变体）----
    raw_dir = os.path.join(VAULT, "raw", "articles")
    if os.path.isdir(raw_dir):
        for fn in os.listdir(raw_dir):
            fn_stem = os.path.splitext(fn)[0]
            # 匹配 <title>_raw / <title>_notes / <title>（去掉尾缀变体）
            variants = [
                f"{title}_raw", f"{title}_notes", title,
                f"{title.replace('-五层知识提炼', '')}_raw",
                f"{title.replace('-五层知识提炼', '')}_notes",
            ]
            if fn_stem in variants:
                p = os.path.join(raw_dir, fn)
                if dry_run:
                    _log(f"  [dry] 删 {os.path.relpath(p, VAULT)}")
                else:
                    os.remove(p)
                    _log(f"  ✓ 删原文: {os.path.relpath(p, VAULT)}")
                deleted_any = True

    # ---- 4. assets 图片目录（raw/assets/<title>/）----
    assets_root = os.path.join(VAULT, "raw", "assets")
    seen_dirs = set()
    for cand in (os.path.join(assets_root, title),
                 os.path.join(assets_root, title.replace("-五层知识提炼", ""))):
        if cand in seen_dirs:
            continue
        seen_dirs.add(cand)
        if os.path.isdir(cand):
            if dry_run:
                _log(f"  [dry] 删目录 raw/assets/{os.path.basename(cand)}/")
            else:
                shutil.rmtree(cand)
                _log(f"  ✓ 删图片目录: raw/assets/{os.path.basename(cand)}/")
            deleted_any = True

    # ---- 5. index.md：删条目行 + Total pages -1 ----
    index_path = os.path.join(VAULT, "index.md")
    if os.path.exists(index_path) and not dry_run:
        _clean_index(index_path, title)
        deleted_any = True

    # ---- 6. log.md：删 ingest 段 ----
    log_path = os.path.join(VAULT, "log.md")
    if os.path.exists(log_path) and not dry_run:
        _clean_log(log_path, title)

    # ---- 7. FTS 索引：删 kb_pages 行 ----
    if not dry_run:
        _clean_fts(page, title, subdir)

    # ---- 8. 反向引用：其他页面里 [[title]] 链接移除 ----
    if not dry_run:
        n = _clean_backlinks(title)
        if n:
            _log(f"  ✓ 清理反向引用: {n} 处")

    _log(f"✅ 删除完成: {title}" + ("（dry-run，未实际删除）" if dry_run else ""))
    return True


def _clean_index(index_path: str, title: str):
    """index.md：删除含 [[title]] 或 [[title|别名]] 的条目行，Total pages -1。"""
    with open(index_path, encoding="utf-8") as f:
        lines = f.readlines()
    kept = []
    removed = 0
    for line in lines:
        if re.search(r"\[\[" + re.escape(title) + r"(\||\]\])", line):
            removed += 1
            continue
        kept.append(line)
    if removed:
        content = "".join(kept)
        m = re.search(r"Total pages:\s*(\d+)", content)
        if m:
            n = max(0, int(m.group(1)) - 1)
            content = content.replace(m.group(0), f"Total pages: {n}", 1)
        with open(index_path, "w", encoding="utf-8") as f:
            f.write(content)
        _log(f"  ✓ index.md 移除 {removed} 条 + Total pages -1")


def _clean_log(log_path: str, title: str):
    """log.md：删除含该标题的 ingest 段（## [date] ingest | <title> 到下一个 ## 前）。"""
    with open(log_path, encoding="utf-8") as f:
        content = f.read()
    pattern = re.compile(
        r"\n?## \[[^\]]+\] ingest \| " + re.escape(title) + r".*?(?=\n?## |\Z)",
        re.S,
    )
    new_content, n = pattern.subn("", content)
    if n:
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        _log(f"  ✓ log.md 移除 {n} 段")


def _clean_fts(page_path, title: str, subdir: str):
    """FTS 索引：删除 kb_pages 中该页面行（path 匹配页面相对路径）。

    ⚠️ FTS5 虚拟表的 path 列是 UNINDEXED，`WHERE path LIKE` 不生效（返回空）。
    必须用**精确等值**匹配。这里枚举标题的所有已知 path 形态逐一 DELETE。
    """
    try:
        from kb_index import _db, init_db
        conn = _db()
        init_db(conn)
        rows = 0
        # 精确等值候选：页本体 + raw 变体（_raw/_notes）+ 各子目录
        cands = []
        if page_path:
            cands.append(os.path.relpath(page_path, VAULT).replace("\\", "/"))
        for sub in (subdir, "raw/articles", "concepts", "entities", "comparisons"):
            for suffix in ("", "_raw", "_notes"):
                cands.append(f"{sub}/{title}{suffix}.md")
        seen = set()
        for c in cands:
            if c in seen:
                continue
            seen.add(c)
            cur = conn.execute("DELETE FROM kb_pages WHERE path=?", (c,))
            rows += cur.rowcount or 0
        conn.commit()
        _log(f"  ✓ FTS 索引移除 {rows} 行（含 raw）")
        conn.close()
    except Exception as exc:
        _log(f"  ⚠️ FTS 清理失败（可 solomon index update 重建）: {exc}")


def _clean_backlinks(title: str) -> int:
    """扫描 vault 所有 .md，移除指向该标题的 [[wikilink]]（含别名）。"""
    n = 0
    pat = re.compile(r"\[\[" + re.escape(title) + r"(\|[^\]]*)?\]\]")
    for root, _dirs, files in os.walk(VAULT):
        for fn in files:
            if not fn.endswith(".md"):
                continue
            p = os.path.join(root, fn)
            try:
                with open(p, encoding="utf-8") as f:
                    content = f.read()
            except OSError:
                continue
            new_content, cnt = pat.subn("", content)
            if cnt:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(new_content)
                n += cnt
                _log(f"  ✓ {os.path.relpath(p, VAULT)}: 移除 {cnt} 处引用")
    return n


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="solomon delete", description="删除一篇或多篇笔记及其全部关联")
    ap.add_argument("targets", nargs="+", help="页面标题或文件路径（可多个，空格分隔），如：十分钟了解RAG基本原理 concepts/x.md")
    ap.add_argument("--dry-run", action="store_true", help="只列出将删除的内容，不实际删除")
    args = ap.parse_args(argv)
    all_ok = True
    for t in args.targets:
        print(f"\n===== 目标: {t} =====")
        if not delete_page(t, dry_run=args.dry_run):
            all_ok = False
    if all_ok and not args.dry_run:
        # 删除完成 → vault 自动推送（fire-and-forget，非 dry-run 才推）
        from vault_git_sync import spawn_async
        spawn_async()
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
