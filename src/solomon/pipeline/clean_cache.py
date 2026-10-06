#!/usr/bin/env python3
"""clean_cache.py — 清理 Solomon 处理中间产物（缓存/临时文件）。

清理范围（白名单根，绝不碰 vault/.kb 知识库）：
  ① WORK_ROOT 下的视频 workdir（正式入库视频的中间产物——内容已在 vault，
     清了仅需重新下载/转写才能重入库）
  ② TEMP_ROOT 临时学习区（--peek 临时读取产物：<标题>/ + concepts/ + .kb/）
  ③ Windows 路径残留垃圾目录（WORK_ROOT 下目录名含 '\\' 或 ':' 的，
     如 D:\\360MoveData\\Users\\...——某次 Windows 路径未转换产生的半成品）

流程：默认 --dry-run 预览（逐目录：类型/路径/大小/已入库标注）
      → 确认后 --execute 删除。已入库 = vault 的 concepts|entities 下存在
      同名页面（含去「-五层知识提炼」尾缀变体）=> ✅ 可安全清；
      未入库 => ⚠️ 清了内容永久丢失。

用法：
    python3 clean_cache.py                     # dry-run 预览全部
    python3 clean_cache.py --targets 标题A     # 只预览匹配标题/路径片段的目标
    python3 clean_cache.py --execute           # 真实删除（全量）
    python3 clean_cache.py --execute --targets 标题A   # 真实删除匹配项
"""

import argparse
import datetime
import os
import re
import shutil
import subprocess
import sys

from _paths import default_temp_root, default_vault, default_work_root  # noqa: E402

VAULT = default_vault()
WORK_ROOT = default_work_root()
TEMP_ROOT = default_temp_root()

# 白名单根：只允许清理这两个根下面的条目（vault 永不触碰）
_SAFE_ROOTS = [r for r in (WORK_ROOT, TEMP_ROOT) if r]

# Windows 路径残留特征：含反斜杠（D:\360MoveData\...）或以「盘符:」开头（D:xxx），
# 都不能是任意冒号——视频标题里的冒号（如 "RAG Explained: ..."）是合法的。
_WIN_JUNK_RE = re.compile(r"\\|^[A-Za-z]:")


def _sanitize(name):
    """与 ingest.py sanitize_filename 一致：非法字符 → 横杠，冒号也转。"""
    name = re.sub(r'[\\/:*?"<>|]', "-", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name or "未命名"


def _is_ingested(name):
    """判断该 workdir 是否已正式入库（vault 有对应知识页）。"""
    if not VAULT:
        return False
    safe = _sanitize(name)
    variants = {safe, f"{safe}-五层知识提炼", safe.replace("-五层知识提炼", "")}
    for base in ("concepts", "entities"):
        for var in variants:
            if os.path.exists(os.path.join(VAULT, base, f"{var}.md")):
                return True
    return False


def _dir_size(path):
    """目录/文件占用字节数（os.walk 求和，避免 du 子进程）。"""
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _fmt(nbytes):
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024 or unit == "GB":
            return f"{nbytes:.0f} {unit}" if unit == "B" else f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} GB"


def _collect():
    """收集全部可清理目标。

    返回 [(kind, name, abs_path, is_dir)]，kind ∈ work（正式视频产物）/ tmp（临时区）/
    junk（Windows 残留）。按根分组、天然去重（每个路径只出现一次）。
    """
    items = []
    seen = set()

    def add(kind, name, abs_path, is_dir):
        key = os.path.abspath(abs_path)
        if key in seen:
            return
        seen.add(key)
        items.append((kind, name, abs_path, is_dir))

    # ① WORK_ROOT 下的视频 workdir（一级目录，跳过隐藏项）
    if WORK_ROOT and os.path.isdir(WORK_ROOT):
        for name in sorted(os.listdir(WORK_ROOT)):
            p = os.path.join(WORK_ROOT, name)
            if not os.path.isdir(p) or name.startswith("."):
                continue
            if name.startswith(".kb") or name in (".obsidian",):
                continue
            if _WIN_JUNK_RE.search(name):
                add("junk", name, p, True)
            else:
                add("work", name, p, True)

    # ② TEMP_ROOT 临时学习区（<标题>/ + concepts/ + .kb/ 全部可清；concepts/.kb 归 tmp 区）
    if TEMP_ROOT and os.path.isdir(TEMP_ROOT):
        for name in sorted(os.listdir(TEMP_ROOT)):
            p = os.path.join(TEMP_ROOT, name)
            if name.startswith(".") and name != ".kb":
                continue
            add("tmp", os.path.join("TempNotes", name), p, os.path.isdir(p))

    return items


# 区域中文说法 → kind 别名（「清理缓存 临时」这类自然说法也能命中对应区域；
# 临时区路径是英文 TempNotes/，子串匹配永远够不到「临时」二字）
_KIND_ALIASES = {
    "临时": "tmp", "临时区": "tmp", "临时学习区": "tmp",
    "工作目录": "work", "工作区": "work", "正式": "work", "视频": "work",
    "残留": "junk", "垃圾": "junk", "windows": "junk",
}


def _filter_targets(items, targets):
    """按关键词过滤：名称（或路径）包含任一 target 子串；
    另支持区域说法（临时/工作目录/残留…）按 kind 别名整区命中。
    空 targets = 全部。"""
    if not targets:
        return items
    kinds = set()
    for t in targets:
        alias = _KIND_ALIASES.get(t) or _KIND_ALIASES.get(t.lower())
        if alias:
            kinds.add(alias)
    out = []
    for kind, name, path, is_dir in items:
        if kind in kinds or any(t in name or t in path for t in targets):
            out.append((kind, name, path, is_dir))
    return out


def _print_preview(items):
    """打印 dry-run 预览（按 可安全清 / 未入库 / 垃圾残留 分组）。"""
    lines = []
    safe, risky, junk = [], [], []
    for kind, name, path, is_dir in items:
        size = _dir_size(path)
        if kind == "junk":
            junk.append((name, path, size))
        elif _is_ingested(name if kind == "work" else name.rsplit("/", 1)[-1]):
            safe.append((kind, name, path, size))
        else:
            risky.append((kind, name, path, size))

    total = sum(s for *_, s in safe) + sum(s for *_, s in risky) + sum(s for _, _, s in junk)

    lines.append("📦 清理预览（dry-run，未删除）")
    if safe:
        lines.append(f"\n✅ 可安全清（已入库，内容在 vault）：{len(safe)} 项")
        for kind, name, path, size in safe:
            lines.append(f"   [{kind}] {name}（{_fmt(size)}）")
    if risky:
        lines.append(f"\n⚠️ 未入库（清了内容永久丢）：{len(risky)} 项")
        for kind, name, path, size in risky:
            lines.append(f"   [{kind}] {name}（{_fmt(size)}）")
    if junk:
        lines.append(f"\n🗑 疑似垃圾残留（Windows 路径未转换）：{len(junk)} 项")
        for name, path, size in junk:
            lines.append(f"   [junk] {name}（{_fmt(size)}）")
    if not items:
        lines.append("   （没有可清理的目标）")
    lines.append(f"\n合计 {len(items)} 项，释放 ~{_fmt(total)}")
    lines.append("确认执行：回复「确认清理缓存」（全量）或「确认清理缓存 <目标>」（单个）")
    return "\n".join(lines)


def _delete(items):
    """真实删除（先校验每个路径都在白名单根下，防御误删）。"""
    removed = []
    for kind, name, path, is_dir in items:
        ap = os.path.abspath(path)
        if not any(ap == r or ap.startswith(r + os.sep) for r in map(os.path.abspath, _SAFE_ROOTS)):
            print(f"⛔ 拒绝删除白名单外路径: {ap}", file=sys.stderr)
            continue
        try:
            if is_dir:
                shutil.rmtree(ap)
            else:
                os.remove(ap)
            removed.append(name)
        except OSError as e:
            print(f"⚠️ 删除失败 {name}: {e}", file=sys.stderr)
    return removed


def _rebuild_tmp_index():
    """删除涉及临时区后重建临时 FTS（build 全量，清掉残留行）。失败不阻断。"""
    if not TEMP_ROOT or not os.path.exists(os.path.join(TEMP_ROOT, ".kb", "kb_fts.db")):
        return
    kb_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb_index.py")
    env = dict(os.environ)
    env["SOLOMON_VAULT"] = TEMP_ROOT
    env["SOLOMON_FTS_DB"] = os.path.join(TEMP_ROOT, ".kb")
    try:
        subprocess.run([sys.executable, kb_script, "build"], env=env,
                       cwd=os.path.dirname(kb_script),
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    except Exception:  # noqa: BLE001
        pass


def main():
    ap = argparse.ArgumentParser(description="清理 Solomon 处理中间产物（缓存/临时文件）")
    ap.add_argument("--targets", nargs="+", default=None,
                    help="只处理名称/路径包含这些关键词的目标（默认全部）")
    ap.add_argument("--execute", action="store_true", help="真实删除（默认 dry-run 预览）")
    args = ap.parse_args()

    if not _SAFE_ROOTS:
        print("❌ 未配置 WORK_ROOT / SOLOMON_TEMP（env 缺失），无法定位清理目标")
        return 1

    items = _filter_targets(_collect(), args.targets)
    if not items:
        print("📦 没有可清理的目标" + (f"（关键词: {', '.join(args.targets)}）" if args.targets else ""))
        return 2

    if not args.execute:
        print(_print_preview(items))
        return 0

    # 真实删除
    touched_tmp = any(k == "tmp" for k, *_ in items)
    removed = _delete(items)
    print(f"🗑 已删除 {len(removed)} 项：")
    for name in removed:
        print(f"  - {name}")
    if touched_tmp:
        _rebuild_tmp_index()
        print("（临时区索引已重建）")
    print(f"完成时间: {datetime.datetime.now().strftime('%F %T')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
