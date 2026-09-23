#!/usr/bin/env python3
"""profile_sync.py — 把 hermes 各 profile 的自我记忆（USER.md 个人信息 + MEMORY.md 记忆）
增量同步到 Solomon 知识库的 entities/用户档案.md，形成自增长的个人档案。

设计（2026-09-15）：
- 双条目全收：USER.md（§ 分条）与 MEMORY.md（## 标题分段）的每条都进档案，
  不做人工判断「哪些是个人信息」——照单全收（用户要求）。
- 增量去重：每条内容算 sha256，已存在的条目跳过（档案页 footer 记 hash 清单）。
- 来源可追溯：每条标注 [来源 profile / USER|MEMORY]，冲突版本并列保留不合并
  （不同 profile 记忆不同版本时全部保留，供人工/后续消解）。
- 可扩展：未来任何格式的个人资料（文档/文本/语音转写…）加一个解析函数即可。

用法：
  python3 profile_sync.py sync          # 扫描 8 profile 记忆 → 增量合并进档案页
  python3 profile_sync.py add "我是XX"  # 主动追加一条 → 档案页 + 回写全部 profile USER.md
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import datetime

# ---- 路径 ----
from _paths import default_vault

VAULT = os.environ.get("SOLOMON_VAULT", default_vault())
HERMES_HOME = os.environ.get("HERMES_HOME", "/home/baner/.hermes")
PROFILES_DIR = os.path.join(HERMES_HOME, "profiles")

ARCHIVE_REL = "entities/用户档案.md"
ARCHIVE_PATH = os.path.join(VAULT, ARCHIVE_REL)


def _log(msg):
    print(f"[profile] {msg}")


# ---------- 记忆读取 ----------

def _iter_profiles() -> list:
    """所有含 memories/ 的 profile 目录名（含 root）。"""
    names = []
    for d in sorted(os.listdir(PROFILES_DIR)):
        if os.path.isdir(os.path.join(PROFILES_DIR, d, "memories")):
            names.append(d)
    # root（默认 profile 的 memories 在 HERMES_HOME/memories）
    if os.path.isdir(os.path.join(HERMES_HOME, "memories")):
        names.append("root")
    return names


def _read_user_entries(profile: str) -> list:
    """USER.md → 条目列表 [(text, source_label)]。§ 分隔，过滤空/纯标记。"""
    p = os.path.join(PROFILES_DIR, profile, "memories", "USER.md")
    if profile == "root":
        p = os.path.join(HERMES_HOME, "memories", "USER.md")
    if not os.path.exists(p):
        return []
    raw = open(p, encoding="utf-8").read()
    out = []
    for seg in re.split(r"\n?\s*§\s*\n?", raw):
        seg = seg.strip()
        if seg and not seg.startswith("#"):
            out.append((seg, f"{profile}/USER"))
    return out


def _read_memory_entries(profile: str) -> list:
    """MEMORY.md → 条目列表 [(text, source_label)]。## 标题分段。"""
    p = os.path.join(PROFILES_DIR, profile, "memories", "MEMORY.md")
    if profile == "root":
        p = os.path.join(HERMES_HOME, "memories", "MEMORY.md")
    if not os.path.exists(p):
        return []
    raw = open(p, encoding="utf-8").read()
    # 按 ## 标题切段：标题行 + 后续内容为一条
    parts = re.split(r"(?m)^(?=## )", raw)
    out = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        title_m = re.match(r"## (.+)", part)
        title = title_m.group(1).strip() if title_m else "未分类"
        body = part.split("\n", 1)[1].strip() if "\n" in part else ""
        text = f"【{title}】{body}" if body else f"【{title}】"
        if text and text != f"【{title}】":
            out.append((text, f"{profile}/MEMORY"))
    return out


def _collect_all() -> list:
    """全部 profile 的 (text, source) 条目。"""
    entries = []
    for profile in _iter_profiles():
        entries.extend(_read_user_entries(profile))
        entries.extend(_read_memory_entries(profile))
    return entries


# ---------- 增量合并 ----------

def _entry_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _load_known_hashes() -> set:
    """档案页 footer 里记录的已同步 hash。"""
    if not os.path.exists(ARCHIVE_PATH):
        return set()
    try:
        content = open(ARCHIVE_PATH, encoding="utf-8").read()
    except OSError:
        return set()
    m = re.search(r"<!-- hashes:\s*([0-9a-f,\s]+)\s*-->", content)
    if not m:
        return set()
    return set(m.group(1).replace(" ", "").split(","))


def _categorize(text: str) -> str:
    """按内容粗分类（只做归档分组，不做过滤）。"""
    if re.search(r"用户|User in|Douyin|XHS|学校|专业|时区|语言|称呼", text, re.I):
        return "👤 基本信息"
    if re.search(r"偏好|prefer|喜欢|铁律|必须|不要|反感|习惯", text, re.I):
        return "🎯 偏好与规则"
    if re.search(r"用 |使用 |工具|skill|脚本|ingest|query|profile|机器人|API", text, re.I):
        return "🛠 工作与技术"
    return "📝 其他"


def sync(verbose: bool = True) -> int:
    """扫描全部 profile 记忆 → 增量合并到档案页。返回新增条数。"""
    if not VAULT or not os.path.isdir(VAULT):
        _log(f"❌ vault 不存在: {VAULT}")
        return -1
    entries = _collect_all()
    known = _load_known_hashes()
    new_items = [(t, s) for t, s in entries if _entry_hash(t) not in known]
    if verbose:
        _log(f"扫描 {len(_iter_profiles())} profile → {len(entries)} 条记忆，其中 {len(new_items)} 条新增")
    if not new_items:
        _log("无新增，档案已最新")
        return 0

    # 构建档案页
    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    today = datetime.date.today().isoformat()
    cats = {}
    for t, s in new_items:
        cats.setdefault(_categorize(t), []).append((t, s))

    existing = ""
    if os.path.exists(ARCHIVE_PATH):
        existing = open(ARCHIVE_PATH, encoding="utf-8").read()
        # 去掉旧 footer（hash 清单）
        existing = re.sub(r"\n<!-- hashes:.*?-->\s*$", "", existing, flags=re.S)

    new_hashes = list(known)
    for cat, items in cats.items():
        block = f"\n## {cat}\n\n"
        for t, s in items:
            block += f"- {t}  `（{s}）`\n"
            new_hashes.append(_entry_hash(t))
        existing += block

    existing += (
        f"\n---\n\n> 自增长档案：由 solomon profile sync 自动维护（{today}）。"
        f"来源 = hermes 各 profile 的 USER/MEMORY 记忆，冲突版本并列保留。\n"
        f"<!-- hashes: {','.join(sorted(set(new_hashes)))} -->\n"
    )
    with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
        f.write(existing)
    _log(f"✅ 档案更新: {ARCHIVE_REL}（+{len(new_items)} 条）")
    return len(new_items)


def add(text: str) -> int:
    """主动追加一条个人信息 → 档案页 + 回写全部 profile USER.md。返回 1。"""
    text = text.strip()
    if not text:
        _log("❌ 空内容")
        return -1
    known = _load_known_hashes()
    if _entry_hash(text) in known:
        _log("⚠️ 该条已存在，跳过")
        return 0
    # 1) 档案页追加
    os.makedirs(os.path.dirname(ARCHIVE_PATH), exist_ok=True)
    today = datetime.date.today().isoformat()
    if os.path.exists(ARCHIVE_PATH):
        content = open(ARCHIVE_PATH, encoding="utf-8").read()
        content = re.sub(r"\n<!-- hashes:.*?-->\s*$", "", content, flags=re.S)
    else:
        content = "---\ntitle: 用户档案\ncreated: %s\nupdated: %s\ntype: entity\ntags: [用户, 个人档案]\n---\n\n# 用户档案\n" % (today, today)
    content += f"\n## 📝 主动追加\n\n- {text}  `（手动添加 {today}）`\n"
    new_hashes = list(known) + [_entry_hash(text)]
    content += (
        f"\n---\n\n> 自增长档案：由 solomon profile sync 自动维护（{today}）。"
        f"来源 = hermes 各 profile 的 USER/MEMORY 记忆，冲突版本并列保留。\n"
        f"<!-- hashes: {','.join(sorted(set(new_hashes)))} -->\n"
    )
    with open(ARCHIVE_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    _log(f"✅ 已追加到 {ARCHIVE_REL}: {text[:40]}…")
    # 2) 回写全部 profile USER.md（幂等：同内容不重复）
    for profile in _iter_profiles():
        p = os.path.join(PROFILES_DIR, profile, "memories", "USER.md")
        if profile == "root":
            p = os.path.join(HERMES_HOME, "memories", "USER.md")
        if not os.path.exists(p):
            continue
        cur = open(p, encoding="utf-8").read()
        if text in cur:
            continue
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"\n§\n{text}\n")
        _log(f"  ↪ 回写 {profile}/USER.md")
    return 1


def _postprocess_archive():
    """把档案页纳入标准入库善后：index.md 条目 + log.md 记录 + FTS 索引 + verify gate。
    与 ingest_document 入库后同一套 postprocess 闭环（2026-09-15 补，使档案成为正式知识库页）。"""
    if not os.path.exists(ARCHIVE_PATH):
        return
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import postprocess
        postprocess.main([ARCHIVE_PATH, "--category", "entity"])
    except Exception as exc:
        _log(f"⚠️ 档案页 postprocess 善后失败（不影响档案内容）: {exc}")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(prog="solomon profile", description="个人信息自增长档案（同步 hermes 记忆 → 知识库）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_sync = sub.add_parser("sync", help="扫描各 profile USER/MEMORY 记忆，增量合并到 entities/用户档案.md")
    p_sync.set_defaults(fn=lambda a: sync())
    p_add = sub.add_parser("add", help="主动追加一条个人信息（档案页 + 回写全部 profile）")
    p_add.add_argument("text", help="如：我是XXX，在YYY工作")
    p_add.set_defaults(fn=lambda a: add(a.text))
    args = ap.parse_args(argv)
    rc = args.fn(args)
    # 档案变更后统一走标准入库善后（index/log/FTS/verify），幂等
    _postprocess_archive()
    return rc


if __name__ == "__main__":
    sys.exit(main())
