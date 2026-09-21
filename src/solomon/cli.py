"""solomon — 统一命令行入口。

一条命令管理知识库管线：
    solomon init                    初始化 vault 目录结构（首次使用）
    solomon doctor                  环境自检（依赖/代理/LLM 端点/vault 可写）
    solomon ingest <url|文件|--name|--text>   入库（视频/文档/文本）
    solomon ask "问题"              知识库问答（FTS5 + LLM 综合）
    solomon index update            重建 FTS5 索引
    solomon verify                  校验知识库链接/断链

设计：薄转发层。核心逻辑全部在 pipeline/ 与 query/ 里（零改动），
本 CLI 负责：读配置(.env) → 校验环境 → 以正确子进程参数转发。
新用户通过 `solomon doctor` 一条命令完成所有环境检查。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from solomon import config

# ── 子进程入口定位 ─────────────────────────────────────────────
_PKG_DIR = Path(__file__).resolve().parent
_INGEST = _PKG_DIR / "pipeline" / "ingest.py"
_QUERY = _PKG_DIR / "query" / "query_kb.py"
_INDEX = _PKG_DIR / "pipeline" / "kb_index.py"
_VERIFY = _PKG_DIR / "assets" / "verify_solomon.py"
_DELETE = _PKG_DIR / "pipeline" / "kb_delete.py"
_CLEAN = _PKG_DIR / "pipeline" / "clean_cache.py"
_PROFILE = _PKG_DIR / "pipeline" / "profile_sync.py"

# ── 帮助横幅 ──────────────────────────────────────────────────
BANNER = r"""
   _____       _                   _
  / ____|     | |                 | |
 | (___   ___ | |_   _ __   _ __  | |__
  \___ \ / _ \| __| | '_ \ | '_ \ | '_ \
  ____) | (_) | |_  | | | || | | || |_) |
 |_____/ \___/ \__| |_| |_||_| |_||_.__/
 视频/文档 → 知识库 → 语义问答 确定性管线
"""


def _run(py: str, script: Path, args: list[str]) -> int:
    """在 config 环境里跑一个核心脚本子进程，返回退出码。"""
    env = config.to_env()
    cmd = [py, str(script), *args]
    print(f"[solomon] $ {' '.join(str(c) for c in cmd)}", file=sys.stderr)
    return subprocess.call(cmd, env=env)


def _which(name: str) -> bool:
    return shutil.which(name) is not None


# ── init：初始化 vault 结构 ────────────────────────────────────
def cmd_init(args: argparse.Namespace) -> int:
    vault = config.VAULT
    if vault.exists() and any(vault.iterdir()):
        print(f"[solomon] vault 已存在且非空：{vault}")
        print("          如需重建结构，请手动操作。")
        # 仍确保 .kb 存在（FTS 索引目录），幂等
        config.ensure_dirs()
        return 0
    config.ensure_dirs()
    for sub in ("raw/articles", "concepts", "entities", "comparisons", "queries", "raw/assets"):
        (vault / sub).mkdir(parents=True, exist_ok=True)
    # 空 index/log 骨架
    index = vault / "index.md"
    if not index.exists():
        index.write_text("# 知识库索引\n\n<!-- Total pages 由 postprocess 自动维护 -->\n", encoding="utf-8")
    log = vault / "log.md"
    if not log.exists():
        log.write_text("# 入库日志\n\n", encoding="utf-8")
    print(f"[solomon] ✓ vault 已初始化：{vault}")
    print(f"[solomon] 结构：raw/articles · concepts · entities · comparisons · queries · raw/assets")
    return 0


# ── doctor：环境自检 ───────────────────────────────────────────
def cmd_doctor(args: argparse.Namespace) -> int:
    print(BANNER)
    print("── 当前配置 ──")
    print(config.describe())
    print()
    problems: list[str] = []

    def check(label: str, ok: bool, hint: str = "") -> None:
        mark = "✓" if ok else "✗"
        print(f"  {mark} {label}")
        if not ok:
            problems.append(f"{label}: {hint}")

    print("── 依赖检查 ──")
    check("python", True, "")
    for tool in ("yt-dlp", "ffmpeg", "ffprobe"):
        check(f"{tool}", _which(tool), f"请安装 {tool}（pip install {tool} 或 apt）")
    try:
        import cv2  # noqa: F401
        check("opencv", True)
    except ImportError:
        check("opencv", False, "pip install opencv-python-headless")
    try:
        import jieba  # noqa: F401
        check("jieba", True)
    except ImportError:
        check("jieba", False, "pip install jieba")
    try:
        import sherpa_onnx  # noqa: F401
        check("sherpa-onnx", True)
    except ImportError:
        check("sherpa-onnx", False, "转写引擎：pip install sherpa-onnx（可选，无则回退 whisper）")

    print("── LLM 端点检查 ──")
    endpoint = os.environ.get("SENSENOVA_BASE_URL") or os.environ.get("LLM_BASE_URL", "http://127.0.0.1:3458/v1")
    api_key = os.environ.get("SENSENOVA_API_KEY") or os.environ.get("LLM_API_KEY", "proxy")
    check(f"LLM base_url={endpoint} (SENSENOVA_BASE_URL/LLM_BASE_URL 可改)", True)
    try:
        import json as _json
        import urllib.request
        body = _json.dumps({
            "model": "sensenova-6.8-flash-lite",
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 5,
        }).encode()
        req = urllib.request.Request(
            endpoint.rstrip("/") + "/chat/completions",
            data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            check(f"LLM 端点连通（chat/completions HTTP {r.status}）", True)
    except Exception as e:
        check("LLM 端点连通", False, f"无法连接 {endpoint}: {e}. 请确认本地 SenseNova 代理或改 LLM_BASE_URL")

    print("── vault 检查 ──")
    vault = config.VAULT
    check(f"vault 存在（{vault}）", vault.exists(), "运行 solomon init 或设置 SOLOMON_VAULT")
    if vault.exists():
        check("vault 可写", os.access(vault, os.W_OK), "检查目录权限")

    print()
    if problems:
        print(f"✗ {len(problems)} 个问题需要处理：")
        for p in problems:
            print(f"   - {p}")
        return 1
    print("✓ 环境就绪，可以直接使用 solomon ingest / solomon ask")
    return 0


# ── ingest：入库 ───────────────────────────────────────────────
def cmd_ingest(args: argparse.Namespace) -> int:
    # 先确保 vault/work 存在（等价于隐式 init；preflight 的写检查随后照常执行）
    config.ensure_dirs()
    argv: list[str] = []
    if args.input:
        argv.append(args.input)
    if args.name:
        argv += ["--name", args.name]
    if args.text:
        argv += ["--text", args.text]
    if args.from_notes:
        argv += ["--from-notes", args.from_notes]
    if args.title:
        argv += ["--title", args.title]
    if args.category:
        argv += ["--category", args.category]
    if args.workdir:
        argv += ["--workdir", args.workdir]
    if args.force:
        argv += ["--force"]
    if args.skip_preflight:
        argv += ["--skip-preflight"]
    if args.max_parts:
        argv += ["--max-parts", str(args.max_parts)]
    if args.progress_file:
        argv += ["--progress-file", args.progress_file]
    if args.images:
        argv += ["--images"]
    if not argv:
        print("[solomon] 需要提供：URL / 文件路径 / --name 标题 / --text 内容")
        print("          示例：solomon ingest https://www.bilibili.com/video/BVxxx")
        return 2
    return _run(config.PYTHON, _INGEST, argv)


# ── ask：问答 ──────────────────────────────────────────────────
def cmd_ask(args: argparse.Namespace) -> int:
    argv = [args.question]
    if args.category:
        argv += ["--category", args.category]
    if args.top:
        argv += ["--top", str(args.top)]
    if args.raw:
        argv += ["--raw"]
    if args.scope:
        argv += ["--scope", args.scope]
    return _run(config.PYTHON, _QUERY, argv)


# ── index：FTS5 索引 ───────────────────────────────────────────
def cmd_index(args: argparse.Namespace) -> int:
    return _run(config.PYTHON, _INDEX, [args.action])


# ── verify：断链校验 ───────────────────────────────────────────
def cmd_verify(args: argparse.Namespace) -> int:
    return _run(config.PYTHON, _VERIFY, [str(config.VAULT)])


# ── delete：删除笔记（清干净全部关联）────────────────────────
def cmd_delete(args: argparse.Namespace) -> int:
    argv = [args.target]
    if args.dry_run:
        argv.append("--dry-run")
    return _run(config.PYTHON, _DELETE, argv)


# ── profile：个人信息自增长档案 ────────────────────────────────
def cmd_profile(args: argparse.Namespace) -> int:
    argv = [args.action]
    if args.action == "add":
        argv.append(args.text)
    return _run(config.PYTHON, _PROFILE, argv)


# ── peek：临时读取（处理到传统笔记，不入库，产物落临时学习区）──
def cmd_peek(args: argparse.Namespace) -> int:
    argv = [args.input]
    if args.name:
        argv = ["--name", args.name]
    if args.title:
        argv += ["--title", args.title]
    if args.workdir:
        argv += ["--workdir", args.workdir]
    if args.max_parts:
        argv += ["--max-parts", str(args.max_parts)]
    if args.fast:
        argv += ["--fast"]
    argv += ["--peek"]
    return _run(config.PYTHON, _INGEST, argv)


# ── clean-cache：清理处理中间产物（dry-run 预览 → --execute 删）─
def cmd_clean_cache(args: argparse.Namespace) -> int:
    argv: list[str] = []
    if args.targets:
        argv += ["--targets", *args.targets]
    if args.execute:
        argv += ["--execute"]
    return _run(config.PYTHON, _CLEAN, argv)


# ── 主入口 ─────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="solomon",
        description="视频/文档 → 知识库 → 语义问答 确定性管线",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n  solomon init\n  solomon doctor\n  solomon ingest https://www.bilibili.com/video/BVxxx\n  solomon ask \"RAG 是什么\"",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化 vault 目录结构")
    p_init.set_defaults(fn=cmd_init)

    p_doctor = sub.add_parser("doctor", help="环境自检（依赖/LLM/vault）")
    p_doctor.set_defaults(fn=cmd_doctor)

    p_ingest = sub.add_parser("ingest", help="入库：视频 URL / 文档文件 / --name / --text")
    p_ingest.add_argument("input", nargs="?", help="视频 URL 或文档文件路径")
    p_ingest.add_argument("--name", help="按名称搜索视频入库")
    p_ingest.add_argument("--text", help="直接粘贴文本内容入库")
    p_ingest.add_argument("--from-notes", dest="from_notes", metavar="NOTES_MD",
                          help="已有笔记，跳过下载/转写直接五层提炼+入库")
    p_ingest.add_argument("--title", help="指定标题（文档/文本入库用）")
    p_ingest.add_argument("--category", default="concept", help="文档类别 concept/entity")
    p_ingest.add_argument("--workdir", help="视频工作目录")
    p_ingest.add_argument("--force", action="store_true", help="忽略已有产物全量重跑")
    p_ingest.add_argument("--skip-preflight", action="store_true", help="跳过环境自检")
    p_ingest.add_argument("--max-parts", type=int, help="合集最多处理前 N 集")
    p_ingest.add_argument("--progress-file", metavar="PATH", help="进度输出文件（供 watcher 推送）")
    p_ingest.add_argument("--images", action="store_true", help="配图版（关键帧/识图/图文结合）")
    p_ingest.set_defaults(fn=cmd_ingest)

    p_ask = sub.add_parser("ask", help="知识库问答")
    p_ask.add_argument("question", help="问题（如：RAG 是什么）")
    p_ask.add_argument("--category", help="限定分类 concept/entity/raw")
    p_ask.add_argument("--top", type=int, default=5, help="检索页数")
    p_ask.add_argument("--raw", action="store_true", help="只显示检索结果，不调 LLM")
    p_ask.add_argument("--scope", choices=("all", "tmp", "main"), default="all",
                       help="检索范围：all=主库+临时学习区联合；tmp=只查临时区；main=只查主库")
    p_ask.set_defaults(fn=cmd_ask)

    p_peek = sub.add_parser("peek", help="临时读取（不入库）：视频处理到传统笔记，产物落临时学习区")
    p_peek.add_argument("input", help="视频 URL / 或 --name 标题")
    p_peek.add_argument("--name", help="按名称搜索视频")
    p_peek.add_argument("--title", help="指定标题")
    p_peek.add_argument("--workdir", help="临时工作目录（默认 TEMP_ROOT/标题）")
    p_peek.add_argument("--max-parts", type=int, default=None, help="合集/分P 只取前 N 集")
    p_peek.add_argument("--fast", action="store_true", help="跳过识图，最快出文字")
    p_peek.set_defaults(fn=cmd_peek)

    p_clean = sub.add_parser("clean-cache", help="清理处理中间产物（缓存/临时区/Windows残留）")
    p_clean.add_argument("--targets", nargs="+", default=None, help="只清理匹配关键词的目标（默认全部）")
    p_clean.add_argument("--execute", action="store_true", help="真实删除（默认 dry-run 预览）")
    p_clean.set_defaults(fn=cmd_clean_cache)

    p_index = sub.add_parser("index", help="FTS5 索引维护")
    p_index.add_argument("action", choices=["update", "build"], default="update", nargs="?")
    p_index.set_defaults(fn=cmd_index)

    p_verify = sub.add_parser("verify", help="校验知识库链接/断链")
    p_verify.set_defaults(fn=cmd_verify)

    p_delete = sub.add_parser("delete", help="删除一篇笔记及其全部关联（页/raw/图片/index/log/FTS/引用）")
    p_delete.add_argument("target", help="页面标题或文件路径，如：十分钟了解RAG基本原理 或 concepts/x.md")
    p_delete.add_argument("--dry-run", action="store_true", help="只列出将删除的内容，不实际删除")
    p_delete.set_defaults(fn=cmd_delete)

    p_profile = sub.add_parser("profile", help="个人信息自增长档案（同步 hermes 记忆 → 知识库）")
    p_profile.add_argument("action", choices=["sync", "add"], help="sync=同步记忆进档案；add=主动追加一条")
    p_profile.add_argument("text", nargs="?", help="add 时的内容，如：我是XXX，在YYY工作")
    p_profile.set_defaults(fn=cmd_profile)

    return ap


def main(argv: list[str] | None = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        print("\n[solomon] 已中断", file=sys.stderr)
        return 130
    except Exception as e:  # noqa: BLE001
        print(f"[solomon] 执行失败：{e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())