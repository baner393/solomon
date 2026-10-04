#!/usr/bin/env python3
"""roadmap_sync.py — 学习路线自动沉淀（多路线分文件版）。

读 solomon 会话库（state.db）自上次水位线以来的对话 → LLM 判断对话属于哪条
学习路线 → 更新对应路线文件的自动区（新路线自动建文件）。

文件布局：memories/roadmaps/<路线名>.md
  每个文件分两区：手动区（用户/agent 直接编辑）+ <!-- AUTO --> 自动区
  （本脚本全权管理，替换式合并——LLM 只重写自动区，不碰手动区）。

设计：
- 多路线分文件：一段对话可能涉及多条路线 → LLM 多块输出（===FILE=== 分隔）。
- 水位线：memories/.roadmap_watermark（epoch 秒）。
- LLM 失败/无新消息/对话与路线无关（NONE）→ 静默退出（消息不会丢，下次重试）。

用法：
    python3.14 roadmap_sync.py [--dry-run]
"""

import argparse
import os
import re
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_client import llm_chat  # noqa: E402

HERMES_HOME = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
PROFILE_HOME = os.path.join(HERMES_HOME, "profiles", "solomon")
DB = os.path.join(PROFILE_HOME, "state.db")
MEM_DIR = os.path.join(PROFILE_HOME, "memories")
ROADMAPS_DIR = os.path.join(MEM_DIR, "roadmaps")
WATERMARK = os.path.join(MEM_DIR, ".roadmap_watermark")
AUTO_BEGIN, AUTO_END = "<!-- AUTO:BEGIN（本区块由每日自动沉淀维护） -->", "<!-- AUTO:END -->"

MAX_MSG_CHARS = 400       # 单条消息截断（要点在头尾）
MAX_DIGEST_CHARS = 12000  # 喂给 LLM 的对话摘录总上限


def _read_watermark() -> float:
    try:
        return float(open(WATERMARK, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        return 0.0


def _write_watermark(ts: float) -> None:
    os.makedirs(MEM_DIR, exist_ok=True)
    with open(WATERMARK, "w", encoding="utf-8") as f:
        f.write(str(ts))


def _fetch_messages(since: float):
    """自水位线以来的 user/assistant 消息（跳过空/工具/压缩残片）。"""
    if not os.path.exists(DB):
        return [], 0.0
    db = sqlite3.connect(DB, timeout=10)
    try:
        rows = db.execute(
            "SELECT role, content, timestamp FROM messages "
            "WHERE timestamp > ? AND role IN ('user','assistant') "
            "AND content IS NOT NULL AND TRIM(content) != '' "
            "ORDER BY timestamp LIMIT 400",
            (since,)).fetchall()
    finally:
        db.close()
    out, newest = [], since
    for role, content, ts in rows:
        if ts:
            newest = max(newest, float(ts))
        c = content.strip()[:MAX_MSG_CHARS]
        if len(content.strip()) > MAX_MSG_CHARS:
            c += "…（截断）"
        out.append((role, c))
    return out, newest


def _split_roadmap(text: str):
    """拆手动区/自动区。无标记视为全是手动区（自动区=空）。"""
    if AUTO_BEGIN in text and AUTO_END in text:
        head = text.split(AUTO_BEGIN, 1)[0]
        auto = text.split(AUTO_BEGIN, 1)[1].split(AUTO_END, 1)[0].strip()
        tail = text.split(AUTO_END, 1)[1]
        return head, auto, tail
    return text, "", ""


def _route_inventory() -> str:
    """现有路线清单（文件名 + 自动区前 300 字）——喂给 LLM 判断归属。"""
    items = []
    if os.path.isdir(ROADMAPS_DIR):
        for name in sorted(os.listdir(ROADMAPS_DIR)):
            if not name.endswith(".md"):
                continue
            _, auto, _ = _split_roadmap(
                open(os.path.join(ROADMAPS_DIR, name), encoding="utf-8").read())
            items.append(f"- {name}\n  当前自动区摘要: {(auto or '（空）')[:300]}")
    return "\n".join(items) or "（还没有任何路线文件）"


def _apply_block(fname: str, new_auto: str) -> bool:
    """把新自动区写进路线文件（保留手动区）。非法文件名拒绝。"""
    fname = fname.strip()
    if not re.fullmatch(r"[\w\u4e00-\u9fff·（）() -]{1,40}\.md", fname):
        print(f"[roadmap] 拒绝非法文件名: {fname!r}", file=sys.stderr)
        return False
    path = os.path.join(ROADMAPS_DIR, fname)
    head, _, tail = _split_roadmap(
        open(path, encoding="utf-8").read() if os.path.exists(path)
        else f"# {fname[:-3]}\n\n> 手动区：直接编辑本文件顶部；自动区由每日沉淀维护。\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{head.rstrip()}\n\n{AUTO_BEGIN}\n{new_auto}\n{AUTO_END}{tail}")
    return True


def sync(dry_run: bool = False) -> int:
    """执行一次自动沉淀。返回更新的路线文件数。"""
    if not os.path.exists(DB):
        print(f"[roadmap] 会话库不存在: {DB}", file=sys.stderr)
        return 0
    wm = _read_watermark()
    msgs, newest = _fetch_messages(wm)
    if not msgs:
        return 0
    if wm == 0.0:  # 首跑：只取最近 60 条，防止吞下全部历史
        msgs = msgs[-60:]

    digest = "\n".join(f"[{role}] {c}" for role, c in msgs)[:MAX_DIGEST_CHARS]
    os.makedirs(ROADMAPS_DIR, exist_ok=True)

    system = (
        "你是学习路线维护员（多路线分文件制）。输入=现有路线清单 + 上次沉淀以来的对话摘录。\n"
        "任务：判断摘录属于哪条路线，输出更新后的完整自动区。\n"
        "- 摘录属于现有路线 → FILE: 用现有文件名\n"
        "- 摘录开启全新主题（现有路线都不覆盖）→ FILE: 起简短中文文件名（如 资料分析入库.md）\n"
        "- 摘录同时涉及多条路线 → 多块输出，块间用 ===FILE=== 分隔\n"
        "- 摘录与任何学习/项目路线无关（闲聊、系统任务）→ 只输出 NONE\n"
        "每块格式严格为：\n"
        "FILE: <文件名.md>\n"
        "<自动区内容：## 当前主题（▶）/ ## 进行中（▶+卡点）/ ## 已完成（✅+一句话结论，新的在前）/ ## 待办（⬜）四节>\n"
        "只输出上述内容，不要解释、不要代码围栏。"
    )
    user = (f"现有路线清单：\n{_route_inventory()}\n\n对话摘录：\n{digest}")

    raw = llm_chat(system, user, temperature=0.2, max_tokens=3000)
    if not raw or raw.strip().startswith("（LLM 调用失败"):
        print("[roadmap] LLM 提取失败，本次跳过（消息不会丢，下次重试）", file=sys.stderr)
        return 0
    raw = raw.strip()
    if raw.strip() == "NONE":
        return 0
    if raw.startswith("```"):  # 剥可能的围栏
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

    blocks, cur_name, cur_body = [], None, []
    for line in raw.splitlines() + ["===FILE==="]:
        if line.strip() == "===FILE===":
            if cur_name and cur_body:
                blocks.append((cur_name, "\n".join(cur_body).strip()))
            cur_name, cur_body = None, []
            continue
        m = re.match(r"^FILE:\s*(.+\.md)\s*$", line.strip())
        if m and cur_name is None:
            cur_name = m.group(1)
        elif cur_name is not None:
            cur_body.append(line)

    if dry_run:
        for name, body in blocks:
            print(f"[roadmap] DRY-RUN → {name}\n{body}\n", file=sys.stderr)
        return len(blocks)

    updated = sum(1 for name, body in blocks if _apply_block(name, body))
    if updated:
        _write_watermark(newest)
    print(f"[roadmap] 沉淀完成：{len(msgs)} 条消息 → {updated} 个路线文件", file=sys.stderr)
    return updated


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    sys.exit(0 if sync(dry_run=a.dry_run) >= 0 else 1)
