#!/usr/bin/env python3
"""
ingest.py — Solomon 知识库统一入库入口（全面脚本化）

三种输入形态：
  ① 视频链接（任意平台，yt-dlp 支持）：https://www.bilibili.com/... / b23.tv / youtube / ...
  ② 报视频名字/标题：ingest.py --name "XXX"  → 搜索发现候选 → 入库
  ③ 文档/文件/粘贴内容：ingest.py <本地文件> 或 ingest.py --text "内容"

视频 pipeline（多平台共用中段）：
  元数据 → 下载(yt-dlp/API降级) → 字幕三级降级(CC→Whisper→纯视觉)
  → 关键帧+去重 → SenseNova识图(逐张串行) → LLM传统笔记 → LLM五层提炼
  → 写 raw/wiki + frontmatter → postprocess 自动闭环

用法：
    python3 ingest.py "https://www.bilibili.com/video/BVxxx" [--title 标题]
    python3 ingest.py --name "Codex 教程"
    python3 ingest.py /path/to/document.md [--title 标题] [--category concept|entity]
    python3 ingest.py --text "粘贴的内容..." [--title 标题]
    python3 ingest.py --url "https://..."            # 网页抓取入 raw

通用选项：
    --force            忽略已有产物全量重跑（默认断点续跑：产物存在则跳过）
    --skip-preflight   跳过环境自检

依赖：yt-dlp / ffmpeg / python3.14 / 本地脚本资产（newsolomon profile）
      SenseNova 代理（127.0.0.1:3456/3458）— 已运行
"""

import argparse
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

# ---- 路径配置 ----
# 资产已从 skill 目录独立到 infra/assets（2026-09-12）：skill 未来清理/升级不影响入库。
# 保留环境变量覆盖以便测试/多机；缺省自动定位仓库内资源（无个人路径）。
from _paths import (  # noqa: E402
    default_assets,
    default_python,
    default_pythonpath,
    default_templates,
    default_temp_root,
    default_vault,
    default_work_root,
)

VAULT = default_vault()
SKILL_ASSETS = default_assets()
WORK_ROOT = default_work_root()
TEMP_ROOT = default_temp_root()
TEMPLATE_DIR = default_templates()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from llm_client import llm_json, llm_chat, vision_batch  # noqa: E402
import postprocess  # noqa: E402
from doc_convert import _to_markdown, _web_to_markdown, _download_web_images  # noqa: E402

PROXY = os.environ.get("HTTP_PROXY", "http://127.0.0.1:7890")
PYTHON = default_python()
PYTHONPATH = default_pythonpath()

# 字幕三级降级用 whisper 模型（与 skill 一致：medium+int8 甜点）
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "medium")


# 进度文件（--progress-file）：每阶段 append 一行 "[时间][耗时] 消息" 并 flush。
# 供 coordinator/看门狗轮询后 `hermes send` 推给用户——解决管道块缓冲下看不到实时进度的问题。
_PROGRESS_FILE = None
_PROGRESS_START = time.time()


def _progress(msg):
    if _PROGRESS_FILE:
        try:
            elapsed = int(time.time() - _PROGRESS_START)
            ts = datetime.datetime.now().strftime("%H:%M:%S")
            with open(_PROGRESS_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{ts}] [{elapsed}s] {msg}\n")
                f.flush()
        except OSError:
            pass


def log(msg):
    print(f"[ingest] {msg}")
    _progress(msg)


def run(cmd, timeout=1800, env_extra=None, check=False, bg=False):
    """执行命令，返回 (code, stdout+stderr)。支持后台运行。"""
    env = dict(os.environ)
    env.setdefault("http_proxy", PROXY)
    env.setdefault("https_proxy", PROXY)
    env.setdefault("PYTHONPATH", PYTHONPATH)
    if env_extra:
        env.update(env_extra)
    if bg:
        p = subprocess.Popen(cmd, env=env, shell=isinstance(cmd, str),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return (0, f"后台启动 PID={p.pid}")
    try:
        p = subprocess.run(cmd, env=env, shell=isinstance(cmd, str),
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "") + (p.stderr or "")
        if check and p.returncode != 0:
            raise RuntimeError(f"命令失败({p.returncode}): {cmd}\n{out[-2000:]}")
        return p.returncode, out
    except subprocess.TimeoutExpired:
        return -1, f"超时({timeout}s): {cmd}"


# ================= preflight 环境自检 =================
def _tcp_ok(port, host="127.0.0.1", timeout=3):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def preflight(kind):
    """入库前环境自检（内联实现，替代旧 preflight.sh——旧脚本还指向已废弃的 llama.cpp:8081）。

    kind: "video"（全量检查）/ "doc"（仅 vault + LLM 代理）。有失败项则退出。
    """
    failures = []

    def ok(msg):
        log(f"  ✓ {msg}")

    def bad(msg):
        failures.append(msg)
        log(f"  ✗ {msg}")

    log(f"=== preflight 环境自检（{kind}）===")
    if os.path.isdir(VAULT) and os.access(VAULT, os.W_OK):
        ok(f"vault 可写: {VAULT}")
    else:
        bad(f"vault 不存在或不可写: {VAULT}")
    if _tcp_ok(3456) or _tcp_ok(3458):
        ok("SenseNova 代理可达（3456/3458）")
    else:
        bad("SenseNova 代理不可达（127.0.0.1:3456/3458）— LLM 生成会失败")

    if kind == "video":
        for tool in ("yt-dlp", "ffmpeg", "ffprobe", "curl"):
            if shutil.which(tool):
                ok(f"{tool} 可用")
            else:
                bad(f"{tool} 不可用")
        if _tcp_ok(7890):
            ok("下载代理 7890 可达")
        else:
            bad("下载代理 127.0.0.1:7890 不可达 — 视频下载/元数据会失败")
        assets = ("whisper_cli.py", "video_keyframe_detector.py", "dhash_dedup.py")
        if os.path.isdir(SKILL_ASSETS):
            missing = [a for a in assets if not os.path.exists(os.path.join(SKILL_ASSETS, a))]
            if missing:
                bad(f"skill 资产缺失: {missing}")
            else:
                ok(f"skill 资产齐全: {SKILL_ASSETS}")
        else:
            bad(f"SKILL_ASSETS 不存在: {SKILL_ASSETS}")
        if os.path.isdir(WORK_ROOT) or os.access(os.path.dirname(WORK_ROOT), os.W_OK):
            ok(f"工作目录可写: {WORK_ROOT}")
        else:
            bad(f"工作目录不可写: {WORK_ROOT}")
        code, out = run([PYTHON, "-c", "import faster_whisper, cv2, numpy"], timeout=120)
        if code == 0:
            ok("python 依赖可导入（faster_whisper/cv2/numpy）")
        else:
            bad(f"python 依赖导入失败: {out[-300:]}")
        # jieba 仅音频信号检测用，缺失不致命
        code, _ = run([PYTHON, "-c", "import jieba"], timeout=60)
        if code == 0:
            ok("jieba 可导入（音频信号检测）")
        else:
            log("  ⚠ jieba 不可导入 — 音频信号检测将跳过（非致命）")

    if failures:
        for f in failures:
            print(f"❌ preflight: {f}")
        raise SystemExit(f"preflight {len(failures)} 项失败，请先修复（或用 --skip-preflight 跳过）")
    log("preflight 全部通过")


# ================= 输入形态识别 =================
URL_RE = re.compile(r"^https?://", re.I)
LOCAL_FILE_RE = re.compile(r"^(/|\./|\.\./|[A-Za-z]:[\\/]|~)")
# 视频域名：这些走视频 pipeline；其它 http(s) 视为网页正文（trafilatura 提取后文档入库）
_VIDEO_HOSTS = (
    "bilibili.com", "b23.tv", "youtube.com", "youtu.be", "douyin.com", "ixigua.com",
    "bilibili.tv", "vimeo.com", "twitch.tv",
)


def _is_video_url(url: str) -> bool:
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).netloc or "").lower()
        return any(v in host for v in _VIDEO_HOSTS)
    except Exception:
        return False


def _to_wsl_path(path):
    """Windows 路径 → WSL 路径：D:\\foo\\bar.md → /mnt/d/foo/bar.md。非 Windows 路径原样返回。"""
    m = re.match(r'^([A-Za-z]):[\\/](.*)$', path)
    if m:
        drive = m.group(1).lower()
        rest = m.group(2).replace('\\', '/')
        return f"/mnt/{drive}/{rest}"
    return path.replace('\\', '/')


def classify_input(arg):
    """返回 ('url'|'web'|'name'|'file'|'text', 值)。
    'url' = 视频链接；'web' = 普通网页（trafilatura 提正文入库）。"""
    if arg.startswith("--"):
        return "text", arg
    if URL_RE.match(arg):
        return ("url" if _is_video_url(arg) else "web"), arg
    if os.path.isfile(arg) or LOCAL_FILE_RE.match(arg):
        return "file", _to_wsl_path(arg)
    return "name", arg


# ================= 断点续跑工具 =================
def _extract_image_refs(md_path):
    """从 md 内容提取 ![[...]] 图片引用（用于跳过 LLM 步骤时恢复配图清单）。"""
    try:
        with open(md_path, encoding="utf-8") as f:
            content = f.read()
    except OSError:
        return []
    return re.findall(r"!\[\[([^\]]+\.(?:jpg|png|jpeg))\]\]", content, re.I)


def _resolve_frame_ref(kf_dir, ref):
    """把 md 里的图片引用解析为 kf_dir 里的真实文件名（兼容 vault 侧冒号→横杠命名）。"""
    if not kf_dir or not os.path.isdir(kf_dir):
        return None
    if os.path.exists(os.path.join(kf_dir, ref)):
        return ref
    for f in os.listdir(kf_dir):
        if f.replace(":", "-") == ref:
            return f
    return None


def _as_list(x):
    """str → [str]，list → 原样，None → []。合集模式下同形参可传多个目录/文件。"""
    if x is None:
        return []
    return [x] if isinstance(x, str) else list(x)


def _resolve_ref_any(kf_dirs, ref):
    """跨多个关键帧目录解析图片引用。返回 (所在目录, 真实文件名) 或 (None, None)。"""
    for d in _as_list(kf_dirs):
        real = _resolve_frame_ref(d, ref)
        if real:
            return d, real
    return None, None


# ================= 音频信号检测 =================
def detect_audio_signals(workdir, subs_path, video_title):
    """音频信号检测（停顿/重复/语速 → 💡/🔥 重点标记）。产物 audio_signals.json。

    存在则跳过（断点续跑）；无字幕或检测失败返回 None（非致命，不影响主线）。
    """
    if not subs_path or not os.path.exists(subs_path):
        return None
    out_path = os.path.join(workdir, "audio_signals.json")
    if os.path.exists(out_path):
        log("⏭️ 跳过音频信号检测（audio_signals.json 已存在）")
        return out_path
    det = os.path.join(SKILL_ASSETS, "audio_signal_detector.py")
    code, out = run([PYTHON, det, subs_path, "--title", video_title, "-o", out_path],
                    timeout=600)
    if code == 0 and os.path.exists(out_path):
        first = out.strip().splitlines()[0] if out.strip() else out_path
        log(f"音频信号检测完成: {first}")
        return out_path
    log(f"⚠️ 音频信号检测失败（不影响主线）: {out[-300:]}")
    return None


_SIGNAL_EMOJI = {"pause": "💡", "pace_slow": "💡", "repetition": "🔥", "pace_fast": "⏩"}


def signals_prompt(signals_paths, max_items=15, time_range=None):
    """把音频信号格式化为提示词文本块。合集模式可传多个 signals.json。无信号返回空串。
    time_range=(start_sec, end_sec) 时只保留窗内信号。"""
    sigs = []
    for sp in _as_list(signals_paths):
        if sp and os.path.exists(sp):
            try:
                with open(sp, encoding="utf-8") as f:
                    sigs.extend(json.load(f).get("signals", []))
            except Exception:
                pass
    if not sigs:
        return ""
    sigs.sort(key=lambda s: s.get("start_time", 0))
    if time_range:
        sigs = [s for s in sigs if time_range[0] <= s.get("start_time", 0) <= time_range[1]]
    lines = []
    for s in sigs[:max_items]:
        t = str(datetime.timedelta(seconds=int(s.get("start_time", 0))))
        em = _SIGNAL_EMOJI.get(s.get("type"), "💡")
        ctx = (s.get("context_text") or "").strip().replace("\n", " ")[:50]
        lines.append(f"- {t} {em}（{s.get('type')}）{ctx}")
    return "\n".join(lines)


# ================= 多模态交叉验证（字幕×关键帧） =================
# 非对称时间窗（秒），对齐原 skill v4 规范：(帧前, 帧后)
ALIGN_WINDOWS = {
    "操作教程": (5, 2),    # 讲师先说后操作：向前多找
    "技术编程": (5, 2),
    "知识讲解": (2, 5),    # 画面先出讲解后到：向后多找
    "公考行测": (2, 5),
}
ALIGN_MIXED = (5, 5)       # 未识别/混合型：取并集（安全策略）


def cross_validate_modalities(workdir, subs_path, kf_dir, video_type, max_subs_per_frame=5):
    """字幕×关键帧 非对称时间窗对齐。产物 alignment.json，供笔记 prompt 交叉核对。

    脚本只做确定性时间窗配对；语义核对（一致性/口误纠错/画面补充）交给笔记 LLM。
    无字幕、无关键帧或失败返回 None（非致命，不影响主线）。断点续跑：产物存在则跳过。
    """
    out_path = os.path.join(workdir, "alignment.json")
    if os.path.exists(out_path):
        log("⏭️ 跳过交叉验证（alignment.json 已存在）")
        return out_path
    if not subs_path or not os.path.exists(subs_path) or not kf_dir or not os.path.isdir(kf_dir):
        log("交叉验证跳过（无字幕或无关键帧）")
        return None
    try:
        with open(subs_path, encoding="utf-8") as f:
            raw = json.load(f)
    except Exception as e:
        log(f"⚠️ 交叉验证跳过（字幕读取失败: {e}）")
        return None
    segs = []
    if isinstance(raw, list):
        for s in raw:
            try:
                st, en = float(s.get("start", 0)), float(s.get("end", s.get("start", 0)))
            except (TypeError, ValueError):
                continue
            txt = (s.get("text") or "").strip()
            if txt:
                segs.append((st, en, txt))
    if not segs:
        return None
    before, after = ALIGN_WINDOWS.get(video_type or "", ALIGN_MIXED)
    log(f"交叉验证: 类型 {video_type or '未识别'} → 非对称时间窗 前{before}s/后{after}s")
    records = []
    for fn in sorted(os.listdir(kf_dir)):
        if not fn.lower().endswith((".jpg", ".png")):
            continue
        # 两种帧名：keyframes_XXX_HH:MM:SS.jpg / {type}_{毫秒}_{hash}.jpg（code_/ppt_ 旧格式）
        m = re.search(r"_(\d{2}):(\d{2}):(\d{2})", fn)
        if m:
            t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        else:
            m2 = re.search(r"_(\d{6,})_", fn)
            if not m2:
                continue
            t = int(int(m2.group(1)) / 1000)
        lo, hi = t - before, t + after
        hits = [(st, en, txt) for st, en, txt in segs if en >= lo and st <= hi]
        if not hits:
            continue
        # 按与窗口的重叠时长排序，取最相关的 N 条
        hits.sort(key=lambda it: min(it[1], hi) - max(it[0], lo), reverse=True)
        records.append({
            "frame": fn,
            "ts": f"{m.group(1)}:{m.group(2)}:{m.group(3)}",
            "window": [before, after],
            "subtitles": [{"start": round(st, 1), "text": txt} for st, en, txt in hits[:max_subs_per_frame]],
        })
    if not records:
        log("交叉验证: 无帧命中字幕窗口")
        return None
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    log(f"交叉验证对齐完成: {len(records)} 帧命中 → alignment.json")
    return out_path


def alignment_prompt(alignment_path, max_frames=30, max_subs=3, sub_len=60, time_range=None):
    """把字幕×帧对齐表格式化为笔记 prompt 块。无产物返回空串。time_range=(s,e) 只留窗内帧。"""
    if not alignment_path or not os.path.exists(alignment_path):
        return ""
    try:
        with open(alignment_path, encoding="utf-8") as f:
            records = json.load(f)
    except Exception:
        return ""
    if not records:
        return ""
    lines = []
    for r in records:
        if time_range:
            sec = _hms_to_sec(r.get("ts", ""))
            if sec is None or not (time_range[0] <= sec <= time_range[1]):
                continue
        subs_txt = " / ".join(
            (s.get("text") or "").strip()[:sub_len] for s in r.get("subtitles", [])[:max_subs]
        )
        lines.append(f"- {r['frame']}（{r.get('ts', '')}）前后讲解：{subs_txt}")
    return "\n".join(lines[:max_frames])


# ================= 标注截图（操作类视频） =================
def annotate_frames_for_notes(refs, kf_dir, workdir, video_type, max_annotate=5):
    """grid_overlay 网格叠加 → SenseNova 网格定位 → annotate_screenshot 红框标注。

    只对操作类视频（操作教程/技术编程）的笔记引用帧执行，最多 max_annotate 张。
    返回 {原始帧名: 标注文件名}（产物在 workdir/annotated/）。单帧失败跳过不阻断。
    """
    if video_type not in ("操作教程", "技术编程") or not refs or not kf_dir:
        return {}
    anno_dir = os.path.join(workdir, "annotated")
    os.makedirs(anno_dir, exist_ok=True)

    def _find_annotated(out_name):
        """annotate_screenshot.py 产物固定为 {output_name}_annotated.jpg（单后缀）。"""
        candidate = f"{out_name}_annotated.jpg"
        return candidate if os.path.exists(os.path.join(anno_dir, candidate)) else None

    result = {}
    for ref in list(refs)[:max_annotate]:
        real = _resolve_frame_ref(kf_dir, ref)
        if not real:
            continue
        base = os.path.splitext(real)[0]
        out_name = base  # 脚本会自动拼 _annotated，这里不要重复加
        existing = _find_annotated(out_name)
        if existing:  # 断点续跑
            result[real] = existing
            continue
        src = os.path.join(kf_dir, real)
        # ① 网格叠加
        grid_path = os.path.join(anno_dir, f"{base}_grid.jpg")
        run([PYTHON, os.path.join(SKILL_ASSETS, "grid_overlay.py"), src, grid_path],
            timeout=120)
        if not os.path.exists(grid_path):
            log(f"⚠️ 网格叠加失败（跳过标注）: {real}")
            continue
        # ② SenseNova 网格定位（关键操作区域 → 网格坐标 JSON）
        prompt = ("这张图叠加了 10×10 网格：列标签 A-J 印在图像顶部边缘，行标签 1-10 印在左侧边缘。"
                  "请找出画面中最关键的操作区域/主体元素（如按钮、菜单、代码区、图表），"
                  "先定位该元素，再读它正上方最近的列标签和正左方最近的行标签，组合成坐标（如 G-4）。"
                  '只返回 JSON：{"cells": ["G-4"], "label": "不超过10字的中文说明"}。')
        desc = vision_batch([grid_path], prompt=prompt, gap=0).get(grid_path, "")
        cells, label = [], ""
        m = re.search(r"\{.*\}", desc, re.S)
        if m:
            try:
                obj = json.loads(m.group(0))
                cells = [c for c in obj.get("cells", [])
                         if re.match(r"^[A-J]-(10|[1-9])$", str(c))]
                label = (obj.get("label") or "")[:10]
            except Exception:
                pass
        if not cells:
            # 模型常返回散文而非 JSON（如"位于G-4单元格"）→ 正则回退提取坐标
            cells = [f"{c}-{r}" for c, r in
                     re.findall(r"([A-J])[-\s]?(10|[1-9])(?!\d)", desc)][:3]
            lm = re.search(r"[「“]([^」”]{2,10})[」”]", desc)
            if lm:
                label = lm.group(1)
        if not cells:
            log(f"⚠️ 网格定位失败（跳过标注）: {real}")
            continue
        # ③ 红框标注
        cmd = [PYTHON, os.path.join(SKILL_ASSETS, "annotate_screenshot.py"),
               "--image", src, "--output-dir", anno_dir, "--output-name", out_name]
        for c in cells[:3]:
            cmd += ["--box", f"{c}:{label or '关键区域'}"]
        run(cmd, timeout=120)
        produced = _find_annotated(out_name)
        if produced:
            result[real] = produced
            log(f"标注截图: {real} → {produced}（{','.join(cells[:3])}）")
        else:
            log(f"⚠️ 标注失败（跳过）: {real}")
    return result


def _apply_annotations(anno_map, notes_path, wiki_path, workdir, topic):
    """标注版图片复制到 vault assets，并把笔记/wiki 中的原图引用替换为标注版。"""
    if not anno_map:
        return
    anno_dir = os.path.join(workdir, "annotated")
    target_dir = os.path.join(VAULT, "raw", "assets", topic)
    os.makedirs(target_dir, exist_ok=True)
    for orig, anno in anno_map.items():
        src = os.path.join(anno_dir, anno)
        if not os.path.exists(src):
            continue
        anno_vault = anno.replace(":", "-")
        dst = os.path.join(target_dir, anno_vault)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
        # 笔记里引用是原始冒号名；wiki 里是横杠名
        for path, old in ((notes_path, orig), (wiki_path, orig.replace(":", "-"))):
            if path and os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    content = f.read()
                new = content.replace(f"![[{old}]]", f"![[{anno_vault}]]")
                if new != content:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new)
    log(f"标注截图入库: {len(anno_map)} 张（笔记/wiki 引用已替换为标注版）")


# ================= 视频 pipeline =================
def fetch_bilibili_metadata(url):
    """B站 API 直连获取元数据（yt-dlp 对 B站新签名失效时的兜底）。"""
    bvid = re.search(r"(BV[0-9A-Za-z]+)", url)
    if not bvid:
        raise RuntimeError("无法从 URL 解析 BV 号")
    bvid = bvid.group(1)
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    code, out = run([
        "curl", "-s", "--max-time", "15",
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        "-H", f"User-Agent: {UA}",
    ], timeout=30)
    info = json.loads(out)
    if info.get("code") != 0:
        raise RuntimeError(f"B站 API 错误: {info.get('message')}")
    d = info["data"]
    return {
        "title": d.get("title", "未命名视频"),
        "duration": d.get("duration", 0),
        "extractor_key": "BilibiliAPI",
        "webpage_url": url,
        "_bvid": bvid,
        "_cid": d.get("cid"),
    }


def fetch_bilibili_parts(url):
    """B站 合集/分P 探测（对齐原 skill Phase 0b：ugc_season = 合集，videos>1 = 分P）。

    返回 {'kind': 'season'|'multi-p', 'series_title': str,
          'parts': [{'title','bvid','cid','page','duration'}, ...]} 或 None（单视频/非B站）。
    """
    m = re.search(r"(BV[0-9A-Za-z]+)", url)
    if not m:
        return None
    bvid = m.group(1)
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    code, out = run([
        "curl", "-s", "--max-time", "15",
        f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        "-H", f"User-Agent: {UA}",
    ], timeout=30)
    try:
        info = json.loads(out)
    except Exception:
        return None
    if info.get("code") != 0:
        return None
    d = info["data"]
    # 合集（ugc_season：多个独立 BV 组成的系列）
    season = d.get("ugc_season")
    if season:
        eps = []
        for sec in season.get("sections", []):
            for ep in sec.get("episodes", []):
                dur = ep.get("duration") or (ep.get("arc") or {}).get("duration", 0)
                eps.append({"title": ep.get("title") or f"第{len(eps)+1}集",
                            "bvid": ep.get("bvid"), "cid": ep.get("cid"),
                            "page": None, "duration": dur})
        if len(eps) > 1:
            return {"kind": "season",
                    "series_title": season.get("title") or d.get("title"),
                    "parts": eps}
    # 分P（同一 BV 多个 page）
    pages = d.get("pages") or []
    if len(pages) > 1:
        parts = [{"title": p.get("part") or f"P{p.get('page')}",
                  "bvid": bvid, "cid": p.get("cid"), "page": p.get("page"),
                  "duration": p.get("duration") or 0}
                 for p in pages]
        return {"kind": "multi-p", "series_title": d.get("title"), "parts": parts}
    return None


def _run_ytdlp(cmd_args, url, timeout, retry_extra=None):
    """yt-dlp 调用；YouTube 反爬（page needs to be reloaded）时自动用 mweb 客户端重试。

    retry_extra: 重试时追加的参数（如下载时的格式覆盖；mweb 下 bv* 流需 PO Token，
    只有 format 18 渐进式 mp4 可直接下）。
    """
    code, out = run(cmd_args + [url], timeout=timeout)
    if code != 0 and ("youtube.com" in url or "youtu.be" in url):
        log("yt-dlp 默认客户端被 YouTube 拦截，用 mweb 客户端重试…")
        code, out = run(cmd_args + ["--extractor-args", "youtube:player_client=mweb"]
                        + (retry_extra or []) + [url], timeout=timeout)
    return code, out


def fetch_video_metadata(url):
    """yt-dlp 获取元数据（标题/时长/字幕/平台）。B站短链接先展开；B站 API 兜底。"""
    if "b23.tv" in url:
        try:
            req = urllib.request.Request(url, method="HEAD",
                                         headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=10) as r:
                url = r.geturl()
            log(f"短链接展开 → {url}")
        except Exception as e:
            log(f"短链接展开失败: {e}，继续原 URL")
    cmd = [
        "yt-dlp", "--dump-single-json", "--no-playlist", "--skip-download",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "--proxy", "http://127.0.0.1:7890",
    ]
    code, out = _run_ytdlp(cmd, url, timeout=120)
    # yt-dlp 可能一边打印元数据 JSON 一边因次要错误返回非零（如 mweb PO Token 警告），
    # 所以不看退出码，直接从输出里抓 JSON
    m = re.search(r"\{.*\}", out, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    # yt-dlp 失败（B站新签名等）→ B站 API 兜底
    if "bilibili.com" in url or "b23.tv" in url:
        log("yt-dlp 元数据失败，降级 B站 API…")
        return fetch_bilibili_metadata(url)
    raise RuntimeError(f"yt-dlp 元数据获取失败: {out[-1000:]}")


def has_video_stream(path):
    """ffprobe 检查文件是否真的包含视频流（yt-dlp 可能静默降级为纯音频）。"""
    code, out = run(["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                     "-show_entries", "stream=codec_name", "-of", "csv=p=0", path],
                    timeout=60)
    return code == 0 and bool(out.strip())


def download_video(url, workdir):
    """下载视频+音频（H.264 优先，AV1 兜底）。返回 (video_path, cover_path)。"""
    os.makedirs(workdir, exist_ok=True)
    video_path = os.path.join(workdir, "video.mp4")
    cover_path = os.path.join(workdir, "cover.jpg")
    # 主路径：yt-dlp 下载 mp4（H.264 优先）
    cmd = [
        "yt-dlp", "-f", "bv*[vcodec^=avc1]+ba/bv*+ba/b", "--merge-output-format", "mp4",
        "-o", os.path.join(workdir, "video.%(ext)s"),
        "--write-thumbnail", "--convert-thumbnails", "jpg",
        "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "--proxy", "http://127.0.0.1:7890",
    ]
    code, out = _run_ytdlp(cmd, url, timeout=3600,
                           retry_extra=["-f", "18/best[ext=mp4]/b"])
    # 找到实际下载的视频文件（可能是 video.mp4 或 video.webm 等）
    cands = [f for f in os.listdir(workdir) if f.startswith("video.") and not f.endswith(".jpg")]
    if cands:
        src = os.path.join(workdir, cands[0])
        if src != video_path:
            os.replace(src, video_path)
    # 封面
    covers = [f for f in os.listdir(workdir) if f.endswith(".jpg")]
    if covers:
        os.replace(os.path.join(workdir, covers[0]), cover_path)
    if os.path.exists(video_path) and os.path.getsize(video_path) > 1024 * 1024 \
            and has_video_stream(video_path):
        log(f"视频下载完成: {video_path} ({os.path.getsize(video_path)//1024//1024}MB)")
        return video_path, cover_path if os.path.exists(cover_path) else None
    log("yt-dlp 产出无视频流（可能静默降级为音频），走 B站 API…")
    # 降级：B站 API dash 下载（412 / yt-dlp 失败 / 无视频流时）
    if "bilibili.com" in url or "b23.tv" in url:
        return download_bilibili_api(url, workdir)
    raise RuntimeError(f"下载失败:\n{out[-1500:]}")


def download_bilibili_api(url, workdir):
    """B站 API 直连下载（dash 格式，视频+音频独立流）。"""
    log("yt-dlp 下载失败，降级 B站 API dash 下载…")
    bvid = re.search(r"(BV[0-9A-Za-z]+)", url)
    if not bvid:
        raise RuntimeError("无法从 URL 解析 BV 号")
    bvid = bvid.group(1)
    UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    # 1. 视频信息 → cid
    code, out = run([
        "curl", "-s", f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}",
        "-H", f"User-Agent: {UA}",
    ], timeout=30)
    info = json.loads(out)
    if info.get("code") != 0:
        raise RuntimeError(f"B站 API 错误: {info.get('message')}")
    cid = info["data"]["cid"]
    title = info["data"]["title"]
    # 分P：?p=N 时取对应页的 cid
    pm = re.search(r"[?&]p=(\d+)", url)
    if pm:
        pno = int(pm.group(1))
        pages = info["data"].get("pages") or []
        if 1 <= pno <= len(pages):
            cid = pages[pno - 1]["cid"]
            title = f"{title} P{pno} {pages[pno - 1].get('part', '')}".strip()
            log(f"分P 下载：P{pno}（cid={cid}）")
    # 2. 播放 URL（dash）
    code, out = run([
        "curl", "-s",
        f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=64&fnval=4048",
        "-H", f"User-Agent: {UA}", "-H", "Referer: https://www.bilibili.com/",
    ], timeout=30)
    play = json.loads(out)
    dash = play["data"]["dash"]
    # 选 H.264 视频流 + 音频流
    vids = [v for v in dash["video"] if v.get("codecid") == 7] or dash["video"]
    vids.sort(key=lambda x: x.get("width", 0) * x.get("height", 0), reverse=True)
    auds = sorted(dash["audio"], key=lambda x: x.get("bandwidth", 0), reverse=True)
    video_url = vids[0]["baseUrl"]
    audio_url = auds[0]["baseUrl"]
    # 3. 下载
    vpath = os.path.join(workdir, "video.m4s")
    apath = os.path.join(workdir, "audio.m4s")
    for target, u in [(vpath, video_url), (apath, audio_url)]:
        run([
            "curl", "-L", "-o", target, "-H", f"User-Agent: {UA}",
            "-H", "Referer: https://www.bilibili.com/",
            "-H", "Origin: https://www.bilibili.com",
            "--connect-timeout", "30", "--max-time", "900", u,
        ], timeout=1000)
    # 4. 合并
    out_path = os.path.join(workdir, "video.mp4")
    code, out = run([
        "ffmpeg", "-y", "-i", vpath, "-i", apath, "-c", "copy",
        "-movflags", "+faststart", out_path,
    ], timeout=600)
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024 * 1024:
        raise RuntimeError(f"ffmpeg 合并失败:\n{out[-1000:]}")
    log(f"B站 API 下载完成: {out_path}（标题: {title}）")
    return out_path, None


def parse_srt_vtt(path):
    """srt/vtt → [{start, end, text}]（秒为 float），与 Whisper 输出格式对齐。失败返回 []。"""
    try:
        txt = open(path, encoding="utf-8", errors="ignore").read()
    except OSError:
        return []
    subs = []
    for block in re.split(r"\n\s*\n", txt):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        tline = next((l for l in lines if "-->" in l), None)
        if not tline:
            continue
        m = re.search(
            r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})\s*-->\s*"
            r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{1,3})", tline)
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = m.groups()

        def _sec(h, mi, s, ms):
            return int(h) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0

        idx = lines.index(tline) + 1
        text = re.sub(r"<[^>]+>", "", " ".join(lines[idx:])).strip()
        if text:
            subs.append({"start": round(_sec(h1, m1, s1, ms1), 3),
                         "end": round(_sec(h2, m2, s2, ms2), 3), "text": text})
    return subs


def get_subtitles(url, workdir, video_path):
    """字幕三级降级：①平台CC/AI字幕 → ②Whisper → ③纯视觉。返回字幕 JSON 路径或 None。

    yt-dlp 产原生 srt/vtt 后本地解析为 subtitles.json（--convert-subs 不支持 json，
    传非法值曾导致 yt-dlp 直接报错、字幕获取 100% 失败全部掉进 Whisper）。
    环境变量 BILI_COOKIE（SESSDATA 等完整 Cookie 串）可解锁需登录态的 AI 字幕。
    """
    # ① 平台字幕
    subs_path = os.path.join(workdir, "subtitles.json")
    cmd = ["yt-dlp", "--write-subs", "--sub-langs", "zh-Hans,zh-CN,zh,en,ai-zh",
           "--skip-download", "--no-playlist",
           "--user-agent", "Mozilla/5.0", "--proxy", "http://127.0.0.1:7890",
           "-o", os.path.join(workdir, "subs.%(ext)s")]
    cookie = os.environ.get("BILI_COOKIE", "").strip()
    if cookie:
        cmd += ["--add-headers", f"Cookie: {cookie}"]
    code, out = _run_ytdlp(cmd, url, timeout=300)
    # 查找产出的字幕文件（srt/vtt/json3），解析为结构化 JSON
    sub_files = [f for f in os.listdir(workdir)
                 if f.startswith("subs.") and
                 f.lower().rsplit(".", 1)[-1] in ("srt", "vtt", "json3")]
    if sub_files:
        subs = parse_srt_vtt(os.path.join(workdir, sorted(sub_files)[0]))
        if subs:
            with open(subs_path, "w", encoding="utf-8") as f:
                json.dump(subs, f, ensure_ascii=False)
            log(f"① 平台字幕获取成功: {len(subs)} 段 → {subs_path}")
            for f in sub_files:
                os.remove(os.path.join(workdir, f))
            return subs_path
        log("⚠️ 平台字幕文件解析为空，转语音识别…")
    # ② 转写：新引擎 transcribe_cli（SenseVoice 中文 ~7x / faster-whisper batched 多语言 ~4x）
    #    长音频无需预切分（vad 按段处理）；失败再回退旧 whisper_cli/whisper_chunked 兜底
    wav = os.path.join(workdir, "audio.wav")
    transcribe_cli = os.path.join(os.path.dirname(os.path.abspath(__file__)), "transcribe_cli.py")
    if os.path.exists(transcribe_cli):
        code, dur_out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", video_path], timeout=60)
        log(f"音频提取完成（{float(dur_out.strip() or 0) / 60:.0f} 分钟），新引擎转写中…")
        code, out = run([PYTHON, transcribe_cli, wav, subs_path, "--lang", "auto"], timeout=7200)
        if code == 0 and os.path.exists(subs_path):
            log(f"② 转写完成（新引擎）: {subs_path}")
            return subs_path
        log(f"⚠️ 新引擎转写失败，回退旧 Whisper: {out[-200:]}")
    # ②c 旧 Whisper 兜底（whisper_cli / 长音频 whisper_chunked）
    log("无平台字幕，转 Whisper 转写…")
    run(["ffmpeg", "-y", "-i", video_path, "-ar", "16000", "-ac", "1", wav], timeout=600)
    if not os.path.exists(wav):
        log("⚠️ 音频提取失败，尝试纯视觉模式")
        open(os.path.join(workdir, ".subs_none"), "w").close()
        return None
    whisper = os.path.join(SKILL_ASSETS, "whisper_cli.py")
    # 长音频（>25min）用分段转写（whisper_chunked：30min 切片逐段转写合并，防超时/VRAM 爆）
    code, dur_out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                         "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                        timeout=60)
    try:
        dur = float(dur_out.strip())
    except ValueError:
        dur = 0
    if dur > 1500:
        chunked = os.path.join(SKILL_ASSETS.rstrip("/"), "whisper_chunked.py")
        if os.path.exists(chunked):
            log(f"长音频（{dur/60:.0f} 分钟）→ 分段转写 whisper_chunked")
            code, out = run([PYTHON, "-u", chunked, wav, subs_path], timeout=7200)
        else:
            code, out = run([PYTHON, whisper, wav, subs_path], timeout=3600)
    else:
        code, out = run([PYTHON, whisper, wav, subs_path], timeout=3600)
    if code == 0 and os.path.exists(subs_path):
        log(f"② Whisper 转写完成: {subs_path}")
        return subs_path
    log("⚠️ Whisper 失败，尝试纯视觉模式")
    open(os.path.join(workdir, ".subs_none"), "w").close()
    return None


def extract_keyframes(video_path, workdir):
    """关键帧提取 + 去重。返回关键帧目录。"""
    # AV1 转 H.264 检查
    code, out = run(["ffprobe", "-v", "quiet", "-show_streams", video_path], timeout=60)
    if re.search(r'codec_name=av1', out):
        log("检测到 AV1，转码 H.264…")
        h264 = os.path.join(workdir, "video_h264.mp4")
        run(["ffmpeg", "-y", "-i", video_path, "-c:v", "libx264", "-preset", "fast",
             "-crf", "23", h264], timeout=1800)
        video_path = h264
    kf_dir = os.path.join(workdir, "keyframes")
    det = os.path.join(SKILL_ASSETS, "video_keyframe_detector.py")
    code, out = run([PYTHON, det, video_path, kf_dir], timeout=1800)
    if not os.path.isdir(kf_dir) or not os.listdir(kf_dir):
        log(f"⚠️ 关键帧提取失败: {out[-500:]}")
        return None
    # 去重
    dedup = os.path.join(SKILL_ASSETS, "dhash_dedup.py")
    dedup_dir = os.path.join(workdir, "keyframes_deduped")
    code, out = run([PYTHON, dedup, kf_dir, dedup_dir], timeout=600)
    if os.path.isdir(dedup_dir) and os.listdir(dedup_dir):
        log(f"关键帧去重完成: {len(os.listdir(dedup_dir))} 帧")
        return dedup_dir
    return kf_dir


def vision_analyze_frames(kf_dir, workdir, max_frames=30):
    """SenseNova 逐张串行识图，产出 vision_results.json。

    断点续跑：已有结果的帧跳过（识图是全 pipeline 最慢的一步，串行不可并行），
    每 5 张落盘一次，中断后重跑只补缺口。
    """
    frames = sorted([f for f in os.listdir(kf_dir) if f.lower().endswith((".jpg", ".png"))])
    if not frames:
        log("无关键帧可分析")
        return None
    frames = frames[:max_frames]
    out_path = os.path.join(workdir, "vision_results.json")
    results = {}
    if os.path.exists(out_path):
        try:
            with open(out_path, encoding="utf-8") as f:
                results = json.load(f)
        except Exception:
            results = {}
    todo = [f for f in frames if not results.get(f)]
    if not todo:
        log(f"⏭️ 跳过识图（vision_results.json 已完整，{len(results)} 帧）")
        return out_path
    log(f"识图 {len(todo)} 张（共 {len(frames)} 帧，已完成 {len(frames) - len(todo)}，并发）…")
    todo_paths = [os.path.join(kf_dir, f) for f in todo]
    descs = vision_batch(todo_paths, prompt="请用中文简要描述图片内容（主体、界面、文字、操作区域），简洁准确。")
    for f in todo:
        results[f] = descs.get(os.path.join(kf_dir, f), "")
    with open(out_path, "w", encoding="utf-8") as fp:
        json.dump(results, fp, ensure_ascii=False, indent=2)
    log(f"识图完成: {out_path}")
    return out_path


def build_frame_manifest(kf_dirs, vision_paths):
    """构建关键帧清单：[(文件名, 时间戳, 帧描述)]，供 LLM 提示词引用。

    文件名来自 keyframes_deduped/ 真实文件名（含冒号）；描述来自 vision_results.json。
    合集模式：kf_dirs / vision_paths 可传列表，跨分集合并（同名帧先见先得）。
    """
    manifest = []
    seen = set()
    vision = {}
    for vp in _as_list(vision_paths):
        if vp and os.path.exists(vp):
            try:
                with open(vp, encoding="utf-8") as f:
                    for k, v in json.load(f).items():
                        vision.setdefault(k, v)
            except Exception:
                pass
    for kf_dir in _as_list(kf_dirs):
        if not kf_dir or not os.path.isdir(kf_dir):
            continue
        frames = sorted(
            f for f in os.listdir(kf_dir) if f.lower().endswith((".jpg", ".png"))
        )
        for fn in frames:
            if fn in seen:
                continue
            seen.add(fn)
            # 两种帧名：keyframes_XXX_HH:MM:SS.jpg（新）/{type}_{毫秒}_{hash}.jpg（旧 code_/ppt_ 格式）
            m = re.search(r"_(\d{2}):(\d{2}):(\d{2})", fn)
            if m:
                ts = f"{m.group(1)}:{m.group(2)}:{m.group(3)}"
            else:
                m2 = re.search(r"_(\d{6,})_", fn)
                ts = _sec_to_hms(int(m2.group(1)) / 1000.0) if m2 else ""
            desc = (vision.get(fn, "") or "").strip().replace("\n", " ")
            manifest.append((fn, ts, desc))
    return manifest


def frame_manifest_prompt(manifest, max_frames=30, desc_len=90, time_range=None):
    """把帧清单格式化为提示词文本块。time_range=(start_sec, end_sec) 时只保留窗内帧。"""
    if not manifest:
        return "（无关键帧）"
    lines = []
    for fn, ts, desc in manifest:
        if time_range:
            sec = _hms_to_sec(ts)
            if sec is None or not (time_range[0] <= sec <= time_range[1]):
                continue
        d = desc[:desc_len] + ("…" if len(desc) > desc_len else "")
        lines.append(f"- {fn}（{ts}）{d}")
    return "\n".join(lines) if lines else "（本时间窗内无关键帧）"


def _hms_to_sec(ts):
    """'HH:MM:SS' → 秒；解析失败返回 None。"""
    m = re.match(r"^(\d{1,2}):(\d{2}):(\d{2})$", (ts or "").strip())
    if not m:
        return None
    return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))


def _sec_to_hms(sec):
    """秒 → 'HH:MM:SS'。"""
    sec = max(0, int(sec))
    return f"{sec // 3600:02d}:{sec % 3600 // 60:02d}:{sec % 60:02d}"


def split_transcript_windows(segs, max_chars=6000, max_windows=5):
    """把带时间戳的转写 [(start, end, text)] 切成时间窗（长视频分段笔记用）。

    返回 [(w_start, w_end, text)]。窗数按每窗 max_chars 估算，封顶 max_windows
    （超长视频自动放宽每窗上限，控制 LLM 调用次数）。
    """
    if not segs:
        return []
    total = sum(len(t) for _, _, t in segs)
    n = min(max_windows, max(1, -(-total // max_chars)))
    per = max(max_chars, -(-total // n))
    windows, cur, chars, cur_start = [], [], 0, segs[0][0]
    for st, en, t in segs:
        cur.append(t)
        chars += len(t)
        if chars >= per:
            windows.append((cur_start, en, "\n".join(cur)))
            cur, chars = [], 0
            cur_start = st if not windows or st > windows[-1][1] else windows[-1][1]
    if cur:
        windows.append((cur_start, segs[-1][1], "\n".join(cur)))
    return windows


def sanitize_filename(name):
    """清洗文件名，去掉 Windows/文件系统非法字符，冒号转横杠。"""
    name = re.sub(r'[\\/:*?"<>|]', "-", name).strip()
    name = re.sub(r"\s+", " ", name)
    return name or "未命名"


# ================= 视频类型识别 → 模板系统 =================
# 对齐 video-assembly-line Phase 2a 的 TYPE_KEYWORDS（中英通用，大小写不敏感）
# 顺序=优先级：更具体的类型排前（面试求职→安全逆向→公考→测评→访谈→项目→方法论→
# Vibe→操作→知识→技术编程兜底）。字典遍历按定义顺序，先命中先返回。
TYPE_KEYWORDS = {
    "面试求职": ["面试", "八股", "面经", "求职", "offer", "简历", "笔试",
                "interview", "resume", "job hunting", "大厂真题", "算法题"],
    "安全逆向": ["逆向", "反编译", "反汇编", "脱壳", "抓包", "解密", "破解",
                "漏洞", "渗透", "验证码", "反爬", "frida", "xposed",
                "reverse", "crack", "exploit", "reversing", "malware"],
    "公考行测": ["判断推理", "判断理论", "言语理解", "数量关系", "资料分析",
                "逻辑判断", "逻辑部分", "削弱", "加强", "归纳推理", "行测"],
    "测评对比": ["测评", "评测", "横评", "对比", "选购", "推荐", "哪个好",
                "值得买", "reviews", "comparison",
                "test", "benchmark"],
    "访谈对话": ["访谈", "对话", "圆桌", "对谈", "专访", "播客", "连麦",
                "interview with", "podcast", "conversation", "AMA", "Q&A"],
    "项目解析": ["项目解析", "项目全景", "项目架构", "系统设计", "架构解析",
                "解剖", "拆解", "源码分析", "项目实战", "architecture",
                "system design", "code walkthrough", "project breakdown"],
    "方法论": ["方法论", "效率", "笔记法", "知识管理", "时间管理",
              "学习法", "心法", "第二大脑", "思维模型", "个人成长",
              "second brain", "methodology", "productivity", "GTD", "PARA",
              "zettelkasten", "卡片盒"],
    "操作教程": ["安装", "教程", "操作", "入门", "配置", "部署",
                "手把手", "零基础", "从零开始", "指南", "上手",
                "tutorial", "how to", "setup", "install", "guide",
                "walkthrough", "demo", "getting started"],
    "知识讲解": ["原理", "概念", "为什么", "本质", "基础",
                "入门知识", "理论", "科普", "是什么", "怎么回事", "详解",
                "lecture", "explanation", "concept", "theory", "explained",
                "introduction", "overview", "deep dive", "fundamentals", "what is"],
    # VibeCoding 须在「技术编程」之前：Vibe Coding 标题含 "coding" 会被编程类抢先
    "VibeCoding": ["Vibe Code", "Vibe Coding", "vibe code", "氛围编程",
                   "UI设计", "界面设计", "审美积累", "style.md"],
    "技术编程": ["编程", "代码", "开发", "程序", "Python", "Java",
                "coding", "programming", "code", "build", "develop",
                "software", "engineering", "实战", "实现", "搭建", "git", "github",
                "implementation", "implement", "hands-on"],
}


def detect_video_type(title):
    """从视频标题识别类型。返回类型名或 None（新类型）。"""
    tl = (title or "").lower()
    for type_name, keywords in TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in tl:
                return type_name
    return None


def is_english_video(title, url=""):
    """英文视频判断（对齐原 skill）：YouTube 链接或标题含英文课程指示词。"""
    if "youtube.com" in url or "youtu.be" in url:
        return True
    indicators = ["full course", "tutorial", "fundamentals", "basics"]
    tl = (title or "").lower()
    return any(ind in tl for ind in indicators)


def is_english_content(text):
    """按转写正文判断英文内容（对齐原 skill：采样段 ascii 字母占比 >70%）。"""
    if not text:
        return False
    sample = text[:3000]
    english_chars = sum(1 for c in sample if c.isascii() and c.isalpha())
    total_chars = sum(1 for c in sample if c.isalpha())
    return english_chars / max(total_chars, 1) > 0.7


# 五层篇幅占比（对齐 worker-solomon/references/five-layer-framework.md）
LAYER_RATIOS = {
    "方法讲解": "L1事实 5% / L2操作 10% / L3原理 25% / L4方法 45% / L5体系 15%",
    "操作教学": "L1事实 10% / L2操作 30% / L3原理 15% / L4方法 30% / L5体系 15%",
    "概念教学": "L1事实 10% / L2操作 10% / L3原理 20% / L4方法 45% / L5体系 15%",
}
_TYPE_TO_RATIO = {
    "公考行测": "方法讲解",
    "操作教程": "操作教学", "技术编程": "操作教学", "VibeCoding": "操作教学",
    "安全逆向": "操作教学", "项目解析": "操作教学",
    "知识讲解": "概念教学",
    "面试求职": "方法讲解", "测评对比": "方法讲解", "访谈对话": "方法讲解",
    "方法论": "方法讲解",
}


def layer_ratio_block(video_type):
    """类型 → 五层篇幅占比提示。未识别类型回落概念教学。"""
    cat = _TYPE_TO_RATIO.get(video_type, "概念教学")
    return f"篇幅占比（{cat}类视频）：{LAYER_RATIOS[cat]}，按此分配各层详略"


# 英文视频→中文笔记的翻译指南（对齐 worker-notewriter 翻译策略表，压缩版）
TRANSLATION_GUIDE = """翻译规则（英文视频→中文笔记）：
- 课程介绍/方法讲解/解题步骤：完整翻译为中文，符合中文表达习惯，不做字面直译
- 专业术语：保留英文+中文注释，首次出现写「中文（English）」，如：批判性思维（Critical Thinking）
- 工具/软件名（Claude Code、VS Code 等）、编程语言、框架、命令行、代码：保留英文不翻译
- 笔记标题和全部正文必须用中文"""


def resolve_template(video_type, title, url=""):
    """类型 → 模板文件内容。英文优先 → 精确匹配类型 → 默认中文技术教程。

    英文优先：英文视频（YouTube / 标题含 full course 等指示词 / 正文英文占比高）
    走英文模板，避免被中文类型关键词（如 "code"）抢先匹配；
    否则按类型精确匹配（如 面试求职 → 面试求职.md）；未识别回落默认模板。
    返回 (模板名, 模板内容) 或 (None, None)。
    """
    if not os.path.isdir(TEMPLATE_DIR):
        return None, None

    def _load(name):
        p = os.path.join(TEMPLATE_DIR, f"{name}.md")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return name, f.read()
        return None, None

    if is_english_video(title, url):
        n, c = _load("英文技术教程")
        if c:
            return n, c
    if video_type:
        n, c = _load(video_type)
        if c:
            return n, c
    return _load("中文技术教程")


def checkpoint_b_threshold(duration_sec):
    """按时长算 min_kb / min_images（对齐 worker-notewriter 质量门槛表）。"""
    t = duration_sec / 60.0  # 分钟
    if t <= 10:
        return 2, 2
    if t <= 20:
        return 4, 3
    over = t - 20
    tier = int(over // 20)
    kb = min(4 + tier * 2, 60)
    img = min(3 + tier, 30)
    return kb, img


def checkpoint_b_check(notes_path, duration_sec, require_images=True):
    """CHECKPOINT B：校验笔记大小/配图数/图文顺序/独立截图子标题。

    require_images=False（纯文字模式）时跳过配图数检查。
    返回 (通过?, 问题列表)。只报告，不抛异常——知识已入库，问题由上层决定是否修复。
    """
    issues = []
    if not notes_path or not os.path.exists(notes_path):
        return False, ["笔记文件不存在"]
    min_kb, min_images = checkpoint_b_threshold(duration_sec)
    size_kb = os.path.getsize(notes_path) / 1024.0
    with open(notes_path, encoding="utf-8") as f:
        content = f.read()
    img_count = len(set(re.findall(r"!\[\[([^\]]+\.(?:jpg|png|jpeg))\]\]", content, re.I)))
    if size_kb < min_kb:
        issues.append(f"笔记过小：{size_kb:.1f}KB < 门槛 {min_kb}KB")
    if require_images and img_count < min_images:
        issues.append(f"配图不足：{img_count} 张 < 门槛 {min_images} 张")
    if re.search(r"###\s*(截图|画面回顾|画面)", content):
        issues.append("含独立截图子标题（应为图后教学说明）")
    return (len(issues) == 0), issues


def copy_images_to_vault(refs, kf_dirs, topic):
    """把引用的关键帧复制到 vault raw/assets/<topic>/，冒号→横杠。返回实际复制数。

    kf_dirs 可传单个目录或列表（合集模式跨分集解析，同名先见先得）。
    """
    if not refs or not kf_dirs:
        return 0
    target_dir = os.path.join(VAULT, "raw", "assets", topic)
    os.makedirs(target_dir, exist_ok=True)
    copied = 0
    for ref in refs:
        d, real = _resolve_ref_any(kf_dirs, ref)
        if not real:
            continue
        src = os.path.join(d, real)
        dst_name = real.replace(":", "-")
        dst = os.path.join(target_dir, dst_name)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
        copied += 1
    return copied


def save_raw_transcript(url, video_title, subs_path, raw_root=None):
    """保存转写原文到 raw/articles/<标题>_raw.md（带 frontmatter）。返回相对路径。

    raw_root: 目标根。缺省 = vault 的 raw/articles（正式入库）；
              传 workdir（peek 临时读取）时 raw 只落临时产物区，不进 vault：
              rel 返回相对 raw_root 的路径（peek 场景仅用于定位，不入库引用）。
    """
    title_safe = sanitize_filename(video_title)
    raw_dir = raw_root if raw_root is not None else os.path.join(VAULT, "raw", "articles")
    os.makedirs(raw_dir, exist_ok=True)
    rel = f"raw/articles/{title_safe}_raw.md"
    if raw_root is not None:
        rel = f"{title_safe}_raw.md"
    raw_path = os.path.join(raw_dir, rel)
    if os.path.exists(raw_path):
        log(f"⏭️ 跳过 raw 转写（{os.path.basename(raw_path)} 已存在）")
        return rel
    today = datetime.date.today().isoformat()
    # 转写正文
    body_lines = []
    if subs_path and os.path.exists(subs_path):
        try:
            with open(subs_path, encoding="utf-8") as f:
                data = json.load(f)
            segs = data if isinstance(data, list) else []
            for s in segs:
                text = (s.get("text", "") or "").strip()
                if text:
                    body_lines.append(text)
        except Exception as e:
            body_lines.append(f"（转写解析失败: {e}）")
    body = "\n\n".join(body_lines)
    md = f"""---
source_url: "{url}"
ingested: {today}
type: raw-transcript
---

# {video_title}（转写原文）

{body}
"""
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(md)
    log(f"raw 转写原文: {rel}")
    return rel


def llm_generate_notes(workdir, video_title, transcription_path, vision_path, kf_dir,
                       template=None, signals_path=None, url="", alignment_path=None):
    """LLM 生成传统笔记（schema 约束 → 渲染 md）。喂关键帧清单 + 帧描述，按真实文件名配图。

    长视频（转写时长 >20min）切时间窗分段生成再合并（split_transcript_windows），
    每窗只喂窗内的帧清单/对齐表/音频信号，避免首尾采样丢中段。
    template: (模板名, 模板内容)，来自 resolve_template（视频类型识别 → templates/{type}.md）。
    signals_path: audio_signals.json（💡/🔥 重点标记）。url: 用于英文视频判断。
    alignment_path: alignment.json（字幕×关键帧 交叉验证对齐表）。
    """
    # 读转写（保留时间戳，供长视频切窗；短视频首尾 20% 采样）
    segs = []
    if transcription_path and os.path.exists(transcription_path):
        with open(transcription_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            for s in data:
                txt = (s.get("text") or "").strip()
                if not txt:
                    continue
                try:
                    segs.append((float(s.get("start", 0)),
                                 float(s.get("end", s.get("start", 0))), txt))
                except (TypeError, ValueError):
                    segs.append((0.0, 0.0, txt))
    duration_est = segs[-1][1] if segs else 0.0
    # 英文视频 → 中文笔记（URL/标题判断 + 转写正文 70% 规则双保险）
    sample_head = " ".join(t for _, _, t in segs[:50])
    translation_block = ""
    if is_english_video(video_title, url) or is_english_content(sample_head):
        translation_block = TRANSLATION_GUIDE + "\n"
        log("检测到英文视频 → 笔记按翻译指南用中文写")
    # 关键帧清单（真实文件名 + 帧描述）
    manifest = build_frame_manifest(kf_dir, vision_path)
    template_block = ""
    if template and template[1]:
        template_block = f"""0. 笔记结构遵循以下「{template[0]}」类型模板的章节结构与写作要点（按其组织内容，不要输出模板里的占位符）：
---
{template[1][:4000]}
---
"""
    # 音频信号重点标记块（💡停顿/放慢强调，🔥重复核心，⏩信息密集）
    # 字幕×关键帧交叉验证块（非对称时间窗对齐 → LLM 语义核对）
    schema = """
{"title": string, "sections": [{"heading": string, "body": string, "image_ref": string|null, "image_note": string|null}], "summary": string}
"""
    system = "你是资深技术笔记整理专家，擅长把口语化视频转写整理成结构清晰、图文并茂的中文教学笔记。"

    def _window_reqs(tr):
        """单个时间窗的信号/对齐/帧清单块。tr=None 表示全片。"""
        sig_block = signals_prompt(signals_path, time_range=tr)
        signal_req = ""
        if sig_block:
            signal_req = f"""5. 重点标记：参考下方「重点时刻清单」，在覆盖这些时刻的章节标题或关键句前加对应 emoji（💡=讲师停顿/放慢语速强调的重点，🔥=反复强调的核心概念，⏩=信息密集区）；没有对应内容不要强加。

重点时刻清单（音频信号检测）：
{sig_block}
"""
        align_block = alignment_prompt(alignment_path, time_range=tr)
        align_req = ""
        if align_block:
            align_req = f"""6. 交叉核对（字幕×画面）：下方「字幕×关键帧对齐表」列出每个关键帧时刻前后的讲解原文。引用某帧前，把该帧的字幕与画面描述对照：
   - 一致 → 正常书写，不要强行加批注；
   - 明显矛盾（讲师口误：说的方向/位置/名称/数值与画面明确不符）→ 在该节正文相关句子后加一行 `> ⚠️ 纠错：讲师口误为"X"，实际画面为"Y"（见图）`。注意：字幕是画面的口语化概括、画面标题只是字幕概念的完整写法，这些都不算矛盾，不要批注；
   - 画面含字幕未提的重要信息 → 可在 image_note 中补充说明。

字幕×关键帧对齐表：
{align_block}
"""
        frame_block = frame_manifest_prompt(manifest, time_range=tr)
        return signal_req, align_req, frame_block

    def _one_call(wi, total, w_start, w_end, w_text):
        signal_req, align_req, frame_block = _window_reqs((w_start, w_end) if tr_mode else None)
        part_head = ""
        if tr_mode:
            part_head = f"""这是视频的第 {wi}/{total} 部分（{_sec_to_hms(w_start)}-{_sec_to_hms(w_end)}）。只根据本部分转写内容生成本部分的 section（不要写全片总结；title 填视频标题；summary 用 2-3 句概括本部分要点）。
"""
        prompt = f"""请根据以下视频转写片段，生成结构化传统笔记 JSON。
视频标题：{video_title}
{translation_block}{part_head}要求：
{template_block}1. 按教学逻辑组织为多个 section（每节含 heading、正文 body）。
2. 图文结合：每节可引用一张最相关的关键帧图。image_ref 必须且只能填下方「关键帧清单」中的真实文件名（含冒号，如 keyframes_001_00:00:01.jpg），严禁编造不存在的文件名；没有合适的图则填 null。
3. image_note 填教学说明（1-2 句，写「读者从图里学到什么」，不要写「图里画了什么」）；无图时填 null。
4. 末尾 summary 总结核心知识点。
{signal_req}{align_req}
关键帧清单（真实文件名 + 画面描述）：
{frame_block}

转写片段（采样）：
{w_text[:10000]}
"""
        return llm_json(system, prompt, schema, max_retries=2)

    # 长视频（>20min）切时间窗分段生成再合并，避免首尾采样丢中段
    tr_mode = duration_est > 1200 and len(segs) > 20
    if tr_mode:
        windows = split_transcript_windows(segs)
        log(f"长视频（{duration_est / 60:.0f} 分钟）→ 笔记分段生成（{len(windows)} 窗）")
    else:
        # 短视频：单窗，首 20% + 尾 20% 采样
        texts = [t for _, _, t in segs]
        n = len(texts)
        sample = texts[: max(1, n // 5)] + texts[max(1, n * 4 // 5):]
        windows = [(0.0, duration_est, "\n".join(sample)[:12000])]
    obj_all, failures = None, 0
    for wi, (w_start, w_end, w_text) in enumerate(windows, 1):
        try:
            obj = _one_call(wi, len(windows), w_start, w_end, w_text)
        except Exception as e:
            failures += 1
            log(f"⚠️ 笔记生成失败（窗 {wi}/{len(windows)}）: {e}")
            continue
        if obj_all is None:
            obj_all = obj
        else:
            obj_all.setdefault("sections", []).extend(obj.get("sections", []))
            sm = (obj.get("summary") or "").strip()
            if sm:
                obj_all["summary"] = (obj_all.get("summary") or "").strip() + "\n" + sm
    if obj_all is None:
        log("⚠️ 笔记生成失败（全部窗失败）")
        return None, []
    if failures:
        log(f"⚠️ 笔记分段生成有 {failures} 窗失败，已用成功窗合并")
    # 渲染 markdown：图片在后、教学说明在图片下一行；校验图片真实存在
    md_lines = [f"# {obj_all.get('title') or video_title}", ""]
    used_refs = []
    for sec in obj_all.get("sections", []):
        md_lines.append(f"## {sec.get('heading', '')}")
        md_lines.append("")
        md_lines.append(sec.get("body", ""))
        img = sec.get("image_ref")
        note = (sec.get("image_note") or "").strip()
        if img and kf_dir and os.path.exists(os.path.join(kf_dir, img)):
            md_lines.append("")
            md_lines.append(f"![[{img}]]")
            if note:
                md_lines.append(note)
            md_lines.append("")
            used_refs.append(img)
        elif img:
            # 幻觉文件名：模型引用了不存在的图，丢弃
            log(f"⚠️ 忽略不存在的图片引用: {img}")
    md_lines.append("## 总结")
    md_lines.append(obj_all.get("summary", ""))
    notes_path = os.path.join(workdir, f"{video_title}_notes.md")
    with open(notes_path, "w", encoding="utf-8") as f:
        f.write("\n".join(md_lines))
    log(f"传统笔记生成: {notes_path}")
    return notes_path, used_refs


def _raw_link_block(title: str, sources: str, raw_rel) -> str:
    """在 wiki 页标题下生成「原文链接」块：raw 内部跳转 + 外部来源 URL（如网页）。

    sources 形如 `[raw/articles/xxx_raw.md]`；raw_rel 是单个 raw 相对路径。
    有 raw 则生成 Obsidian 内部链接 [[raw/articles/xxx]]；raw 原文里若带
    source_url（网页），再补一个外部链接。
    """
    lines = []
    raw_target = None
    if raw_rel:
        raw_target = raw_rel[0] if isinstance(raw_rel, (list, tuple)) else raw_rel
    elif sources and sources not in ("[]", ""):
        m = re.search(r"raw/articles/([\w\-\u4e00-\u9fff]+)", sources)
        if m:
            raw_target = f"raw/articles/{m.group(1)}"
    if raw_target:
        stem = os.path.splitext(raw_target)[0]
        lines.append(f"> **📄 原文：** [[{stem}|查看原文 raw]]")
    # 外部来源（网页入库时 raw frontmatter 记了 source_url）
    ext = ""
    if raw_target:
        raw_path = os.path.join(VAULT, raw_target)
        if os.path.exists(raw_path):
            try:
                with open(raw_path, encoding="utf-8") as f:
                    head = f.read(2000)
                m = re.search(r"source_url:\s*(\S+)", head)
                if m:
                    ext = m.group(1).strip()
            except OSError:
                pass
    if ext:
        lines.append(f"> **🔗 来源：** [{ext}]({ext})")
    return "\n".join(lines) + "\n" if lines else ""


def _split_doc_chunks(text: str, max_chars: int = 8000) -> list:
    """把长文档按段落边界切成块（分批提炼用）。

    - 每块目标 ≤ max_chars 字，尽量在段落边界切（\n\n），避免句子被劈开
    - 单块超限（无段落边界的长段落）则硬切
    - 相邻小块（<min_chars）合并进邻块，减少 LLM 调用次数/提升信息密度
    - 返回 [chunk, ...]；总长 ≤ max_chars 时返回单块
    """
    min_chars = 4000
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    paras = [p for p in text.split("\n\n") if p.strip()]
    chunks, cur = [], ""
    for p in paras:
        # 单段超限：按句子边界硬切，避免产生 >max_chars 的巨块
        if len(p) > max_chars:
            if cur:
                chunks.append(cur)
                cur = ""
            sub = p
            while len(sub) > max_chars:
                # 在最近的句号/换行处切（保证语义完整），无则硬切
                cut = max_chars
                for sep in ("。", "\n", "；", "."):
                    idx = sub.rfind(sep, max_chars // 2, max_chars)
                    if idx > 0:
                        cut = idx + 1
                        break
                chunks.append(sub[:cut])
                sub = sub[cut:]
            cur = sub
            continue
        if cur and len(cur) + len(p) + 2 > max_chars and len(cur) >= min_chars:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    # 合并相邻小块：块 < min_chars 且后面还有块 → 并入下一块（或上一块）
    # 合并后允许略超 max_chars（≤1.5x），且硬切阶段用同一上限避免刚合并又被切开
    hard_cap = int(max_chars * 1.5)
    merged = []
    for c in chunks:
        if merged and (len(c) < min_chars or len(merged[-1]) < min_chars) and len(merged[-1]) + len(c) <= hard_cap:
            merged[-1] = f"{merged[-1]}\n\n{c}"
        else:
            merged.append(c)
    chunks = merged
    # 硬切超长块（罕见：单段就超限）；用与合并一致的 hard_cap
    final = []
    for c in chunks:
        while len(c) > hard_cap:
            final.append(c[:hard_cap])
            c = c[hard_cap:]
        final.append(c)
    return [c for c in final if c.strip()]


def llm_generate_five_layer(notes_path, video_title, kf_dir, vision_path, raw_rel=None,
                            signals_path=None, video_type=None, source_kind="video"):
    """LLM 生成五层知识提炼（9段模板 schema → 渲染 wiki 页）。L2/L3 配图。

    合集模式：kf_dir / vision_path / raw_rel 均可传列表（跨分集合并）。
    video_type: detect_video_type 的结果，决定五层篇幅占比（类型动态占比）。
    source_kind: "video"（视频，默认，含关键帧配图+信号标记）| "doc"（文档/网页，
        全文不截断 + 要求摘录原文金句/关键数据，配图取 img_dir 里的已下载图片）。
    """
    if not notes_path or not os.path.exists(notes_path):
        return None, []
    is_doc = source_kind == "doc"
    with open(notes_path, encoding="utf-8") as f:
        # 文档场景读全文（网页长文 30KB 不能被 12KB 截断丢弃后半细节）；
        # 视频场景保持原截断（转写本来就有上限）。
        notes = f.read() if is_doc else f.read()[:12000]
    # 关键帧清单（真实文件名 + 帧描述）
    manifest = build_frame_manifest(kf_dir, vision_path)
    frame_block = frame_manifest_prompt(manifest)
    # 音频信号重点标记块（合集模式可传多个 signals.json）
    sig_block = signals_prompt(signals_path)
    signal_req = ""
    if sig_block:
        signal_req = f"""3. 重点标记：下方「重点时刻清单」是讲师强调信号。仅对真正被反复强调的 1-3 个核心概念，在对应结论/条目开头加 🔥；💡/⏩ 标记如笔记中已有则保留，没有不要新增。严禁给每条结论都加标记。

重点时刻清单（音频信号检测）：
{sig_block}
"""
    schema = """
{"title": string, "core_conclusions": [string], "facts": string, "operations": string, "principles": string, "methodology": string, "framework": string, "templates": string, "insights": [string], "todos": [string], "quotes": [string], "images": [{"ref": string, "note": string, "layer": string, "where": string}]}
"""
    if is_doc:
        subject_word = "文章/文档"
        # 保真要求（对齐 ytkn「Full extract」：保全主张/例子/数字/细节，不做空泛压缩）
        source_req = (
            "6. 原文金句（重点）：quotes 数组摘录 3-8 条原文中最有信息量/最精彩的原话（保留原句措辞，每条一句话，注明所属主题）。\n"
            "7. 保真提炼（重点）：这是长文/论坛帖，不是教程——禁止空泛概括。核心结论、facts、methodology 必须保留原文的"
            "具体细节：数字（star 数/金额/时间/周期）、人名/项目名、具体事例、完整论证链。宁可多写具体证据，不要浓缩成抽象口号。\n"
            "8. 关键数据：facts 中优先用表格列出原文里的关键数据/数字/案例（时间线、规模、收益等），至少 6 行。"
        )
    else:
        subject_word = "视频"
        source_req = ""
    prompt = f"""请根据以下{subject_word}，生成 Solomon 五层知识提炼 JSON（L1事实/L2操作/L3原理/L4方法/L5体系）。
{subject_word}标题：{video_title}
要求：
1. 五层是精华提炼不是全文复制：{("各段总篇幅控制在 6000 字以内，长文/论坛帖允许保留更多细节（原文 30KB 级别时不要压缩成骨架）" if is_doc else "各段总篇幅控制在 3500 字以内（内容越长越要浓缩）")}，禁止把原文整段照搬。
2. core_conclusions 给 3-10 条核心结论；facts/operations/principles/methodology/framework/templates 每项为 markdown 文本（可用表格/列表/代码块，facts 的一览表最多 12 行）；insights 给 3-5 条启示（字符串数组，每条一句话）；todos 给 3-5 个待研究问题（字符串数组）。
3. 各段内容规范（9段模板）：facts=信息+知识点一览表（L1）；operations=公式速查/操作步骤/标准化流程（L2）；principles=数学推导/概念本质/设计原因（L3）；methodology=决策矩阵/适用范围/对比框架/易错清单（L4）；framework=体系定位/依赖关系/跨领域关联（L5）；templates=2-3 个可复用标准化模板；insights=3-5 条可迁移启示；todos=3-5 个待研究方向。
4. {layer_ratio_block(video_type)}
5. 图文结合（重点）：images 数组列出要嵌入 L2（操作手册）和 L3（底层原理）的教学配图，共 2-8 张。每张：ref 必须且只能填下方「关键帧清单」中的真实文件名，严禁编造；note 填教学说明（一句话点明该图对应哪一步/哪个概念，读者从图里学到什么）；layer 填 "operations" 或 "principles" 表明嵌入哪一层；where 填该图要嵌入的具体位置——**从 operations/principles 正文里原样抄那个步骤标题或概念小标题**，渲染时会把图插到该段落之后。没有合适的图可为空数组。
{source_req}
{signal_req}
关键帧清单（真实文件名 + 画面描述）：
{frame_block}

{subject_word}内容（{"全文" if is_doc else "采样"}）：
{notes[:30000] if is_doc else notes[:10000]}
"""
    # 分块提炼：doc 长文按段落切块逐块 LLM（避免 >3万字被 30000 截断丢后半），
    # 各块结果合并；视频/短文保持单次调用。
    chunks = _split_doc_chunks(notes, 8000) if is_doc else [notes[:10000]]

    def _gen_one(chunk_text: str, part_label: str) -> dict:
        """对单块内容调 LLM 生成五层 JSON。失败返回空 dict。"""
        _prompt = prompt.replace(
            "{subject_word}内容（" + ("全文" if is_doc else "采样") + "）：\n"
            + (notes[:30000] if is_doc else notes[:10000]),
            f"{subject_word}内容（{part_label}）：\n{chunk_text}",
        )
        try:
            return llm_json(
                "你是知识库提炼专家，把技术笔记整理成 Solomon 五层知识框架，并嵌入真实关键帧配图。",
                _prompt, schema, max_retries=2, optional_keys=["images"],
                max_tokens=12000 if is_doc else 8192, timeout=480,
            ) or {}
        except Exception as e:
            log(f"⚠️ 五层提炼失败（{part_label}）: {e}")
            return {}

    if len(chunks) == 1:
        obj = _gen_one(chunks[0], "全文")
        if not obj:
            return None, []
    else:
        # 分块多轮：逐块提炼，合并各块结果
        objs = []
        for i, chunk in enumerate(chunks, 1):
            log(f"五层提炼 第 {i}/{len(chunks)} 块（{len(chunk)} 字）…")
            objs.append(_gen_one(chunk, f"第 {i}/{len(chunks)} 部分"))
        objs = [o for o in objs if o]
        if not objs:
            return None, []
        # 合并：列表字段并集去重，markdown 字段按块拼接
        def _uniq(items):
            seen, out = set(), []
            for x in items:
                if x not in seen:
                    seen.add(x)
                    out.append(x)
            return out
        obj = {
            "core_conclusions": _uniq([c for o in objs for c in _as_list(o.get("core_conclusions"))]),
            "facts": "\n\n".join(o.get("facts", "") for o in objs if o.get("facts")),
            "operations": "\n\n".join(o.get("operations", "") for o in objs if o.get("operations")),
            "principles": "\n\n".join(o.get("principles", "") for o in objs if o.get("principles")),
            "methodology": "\n\n".join(o.get("methodology", "") for o in objs if o.get("methodology")),
            "framework": "\n\n".join(o.get("framework", "") for o in objs if o.get("framework")),
            "templates": "\n\n".join(o.get("templates", "") for o in objs if o.get("templates")),
            "insights": _uniq([x for o in objs for x in _as_list(o.get("insights"))]),
            "todos": _uniq([x for o in objs for x in _as_list(o.get("todos"))]),
            "quotes": _uniq([q for o in objs for q in _as_list(o.get("quotes"))]),
            "images": [im for o in objs for im in (o.get("images") or [])],
        }
        log(f"五层提炼完成：{len(objs)} 块合并 → {len(obj['core_conclusions'])} 结论 / {len(obj['quotes'])} 金句")
    # 收集 L2/L3 配图（按真实文件名存在性过滤）
    imgs_by_layer = {"operations": [], "principles": []}
    used_refs = []
    for im in obj.get("images", []) or []:
        ref = im.get("ref")
        note = (im.get("note") or "").strip()
        layer = (im.get("layer") or "").strip()
        where = (im.get("where") or "").strip()
        if layer not in imgs_by_layer:
            layer = "operations"
        if ref and _resolve_ref_any(kf_dir, ref)[1]:
            imgs_by_layer[layer].append((ref, note, where))
            used_refs.append(ref)
        elif ref:
            log(f"⚠️ 五层忽略不存在的图片引用: {ref}")
    # 渲染 wiki 页
    # 标题/文件名固定为视频标题（确定性，避免 LLM 每次生成不同标题导致重复页）
    title = video_title
    today = datetime.date.today().isoformat()
    sources = "[" + ", ".join(_as_list(raw_rel)) + "]" if raw_rel else "[]"

    def _weave_images(text, images):
        """把图片按 where 锚点插入正文段落之后；锚点匹配失败则落到末尾。"""
        if not text or not images:
            return text
        paras = text.split("\n\n")
        for ref, note, where in images:
            # vault assets 文件名冒号→横杠（Windows 不支持冒号）
            block = f"![[{ref.replace(':', '-')}]]"
            if note:
                block += f"\n{note}"
            placed = False
            if where:
                for i, p in enumerate(paras):
                    if where in p:
                        paras.insert(i + 1, block)
                        placed = True
                        break
            if not placed:
                paras.append(block)
        return "\n\n".join(paras)

    ops_text = _weave_images(obj.get('operations', ''), imgs_by_layer.get("operations", []))
    pri_text = _weave_images(obj.get('principles', ''), imgs_by_layer.get("principles", []))

    md = f"""---
title: {title}
created: {today}
updated: {today}
type: concept
tags: [五层提炼]
sources: {sources}
---

# {title}

{_raw_link_block(title, sources, raw_rel)}

## 一、核心结论（3分钟看懂）

""" + "\n".join(f"- {c}" for c in obj.get("core_conclusions", [])) + f"""

## 二、功能整理（Facts）

{obj.get('facts', '')}

## 三、操作手册（How）

{ops_text}

## 四、底层原理（Why）

{pri_text}

## 五、方法论（Method）

{obj.get('methodology', '')}

## 六、知识图谱（Framework）

{obj.get('framework', '')}

## 七、可复用模板（Template）

{obj.get('templates', '')}

## 八、我的项目启发（Insight）

""" + "\n".join(f"- {x}" for x in _as_list(obj.get("insights", []))) + f"""

""" + (("## 九、原文金句（Quotes）\n\n"
      + "\n".join(f"> {q}" for q in _as_list(obj.get("quotes", []))) + "\n\n") if _as_list(obj.get("quotes")) else "") + f"""## 十、待研究问题（TODO）

""" + "\n".join(f"- {t}" for t in obj.get("todos", [])) + "\n"
    wiki_path = os.path.join(VAULT, "concepts", f"{sanitize_filename(title)}.md")
    os.makedirs(os.path.dirname(wiki_path), exist_ok=True)
    with open(wiki_path, "w", encoding="utf-8") as f:
        f.write(md)
    log(f"五层提炼写入: {wiki_path}")
    return wiki_path, used_refs


def _ingest_video_single(url, video_title, workdir, force=False, duration=0,
                         write_wiki=True, raw_title=None, images=False,
                         raw_to_vault=True):
    """单视频 pipeline 主体（合集模式下每个分集各跑一次，write_wiki=False）。

    raw_to_vault: raw 转写是否进 vault（正式入库 True；peek 临时读取 False，只落 workdir）。

    返回 dict(notes_path, notes_refs, kf_dir, vision_path, subs, raw_rel,
              wiki_path, wiki_refs, duration)。
    """
    log(f"=== 视频入库: {video_title}（{duration or '?'}s）{'[force 全量重跑]' if force else ''}===")
    # 类型识别 → 模板
    vtype = detect_video_type(video_title)
    template = resolve_template(vtype, video_title, url)
    log(f"类型识别: {vtype or '未识别'} → 模板: {template[0] or '无'}")

    # ① 下载
    video_path = None
    if not force:
        cand = os.path.join(workdir, "video.mp4")
        if os.path.exists(cand) and os.path.getsize(cand) > 1024 * 1024 and has_video_stream(cand):
            video_path = cand
            log(f"⏭️ 跳过下载（video.mp4 {os.path.getsize(cand) // 1024 // 1024}MB 已存在）")
    if not video_path:
        video_path, _cover = download_video(url, workdir)

    # ② 字幕（.subs_none 标记 = 上次已确认无字幕，避免每次重跑 Whisper）
    subs_path = os.path.join(workdir, "subtitles.json")
    subs_none = os.path.join(workdir, ".subs_none")
    if not force and os.path.exists(subs_path):
        subs = subs_path
        log("⏭️ 跳过字幕（subtitles.json 已存在）")
    elif not force and os.path.exists(subs_none):
        subs = None
        log("⏭️ 跳过字幕（已知无字幕，纯视觉模式）")
    else:
        subs = get_subtitles(url, workdir, video_path)

    # ②b 音频信号检测（💡/🔥 重点标记；无字幕则跳过，失败不影响主线）
    signals_path = detect_audio_signals(workdir, subs, video_title)

    # ③ 关键帧（优先去重后的目录）——仅配图版（默认纯文字，--images 才跑图片管线）
    kf_dir = None
    if images:
        if not force:
            for d in ("keyframes_deduped", "keyframes"):
                p = os.path.join(workdir, d)
                if os.path.isdir(p) and os.listdir(p):
                    kf_dir = p
                    log(f"⏭️ 跳过关键帧（{d}/ {len(os.listdir(p))} 帧已存在）")
                    break
        if not kf_dir:
            kf_dir = extract_keyframes(video_path, workdir)
    else:
        log("纯文字模式：跳过关键帧/识图/交叉验证/配图（要配图版加 --images）")

    # ④ 识图（函数内部增量续跑：已识别的帧跳过）
    vision_path = vision_analyze_frames(kf_dir, workdir) if kf_dir else None

    # ④b 多模态交叉验证（字幕×关键帧 非对称时间窗对齐；无字幕/失败不影响主线）
    alignment_path = cross_validate_modalities(workdir, subs, kf_dir, vtype) if kf_dir else None

    # ⑤ raw 转写原文入库（函数内部已存在则跳过；peek 模式只落 workdir 不进 vault）
    raw_rel = save_raw_transcript(url, raw_title or video_title, subs,
                                  raw_root=None if raw_to_vault else workdir)

    # ⑥ LLM 传统笔记（按类型模板）
    notes_path = os.path.join(workdir, f"{video_title}_notes.md")
    if not force and os.path.exists(notes_path):
        notes_refs = _extract_image_refs(notes_path)
        log(f"⏭️ 跳过笔记生成（{os.path.basename(notes_path)} 已存在，{len(notes_refs)} 图）")
    else:
        notes_path, notes_refs = llm_generate_notes(workdir, video_title, subs, vision_path, kf_dir,
                                                    template=template, signals_path=signals_path,
                                                    url=url, alignment_path=alignment_path)

    result = {"notes_path": notes_path, "notes_refs": notes_refs, "kf_dir": kf_dir,
              "vision_path": vision_path, "subs": subs, "raw_rel": raw_rel,
              "signals_path": signals_path,
              "wiki_path": None, "wiki_refs": [], "duration": duration}
    if not write_wiki:
        return result

    # CHECKPOINT B 质量门槛（程序化：不达标自动重生笔记一次，二次不达标单列问题继续）
    ok, issues = checkpoint_b_check(notes_path, duration, require_images=images)
    if not ok:
        log(f"⚠️ CHECKPOINT B 不达标，自动重生笔记: {'; '.join(issues[:3])}")
        try:
            os.remove(notes_path)
        except OSError:
            pass
        notes_path, notes_refs = llm_generate_notes(
            workdir, video_title, subs, vision_path, kf_dir,
            template=template, signals_path=signals_path,
            url=url, alignment_path=alignment_path)
        result["notes_path"], result["notes_refs"] = notes_path, notes_refs
        ok, issues = checkpoint_b_check(notes_path, duration, require_images=images)
    if ok:
        if images:
            log(f"✅ CHECKPOINT B 通过（{checkpoint_b_threshold(duration)[0]}KB/"
                f"{checkpoint_b_threshold(duration)[1]}图门槛）")
        else:
            log("✅ CHECKPOINT B 通过（纯文字模式，跳过配图门槛）")
    else:
        log(f"⚠️ CHECKPOINT B 重生后仍不达标（继续入库，待人工复盘）: {'; '.join(issues[:3])}")

    # ⑦ LLM 五层提炼 → wiki 页
    wiki_path = os.path.join(VAULT, "concepts", f"{sanitize_filename(video_title)}.md")
    if not force and os.path.exists(wiki_path):
        wiki_refs = _extract_image_refs(wiki_path)
        log(f"⏭️ 跳过五层提炼（wiki 页已存在，{len(wiki_refs)} 图）")
    else:
        wiki_path, wiki_refs = llm_generate_five_layer(notes_path, video_title, kf_dir,
                                                       vision_path, raw_rel,
                                                       signals_path=signals_path,
                                                       video_type=vtype)
    result["wiki_path"] = wiki_path
    result["wiki_refs"] = wiki_refs

    if wiki_path:
        # 复制引用的关键帧到 vault assets（冒号→横杠；已存在则跳过复制）
        if kf_dir and os.path.isdir(kf_dir):
            refs = set(notes_refs) | set(wiki_refs)
            n = copy_images_to_vault(refs, kf_dir, sanitize_filename(video_title))
            log(f"图片入库: {n} 张 → raw/assets/{sanitize_filename(video_title)}/")
        # ⑥b 标注截图（操作类视频）——已禁用（2026-09-09 用户拍板）：标注不准且极慢
        # （5 张 ~148s，含单张 88s 的异常帧）。恢复：取消下面注释即可。
        # anno_map = annotate_frames_for_notes(notes_refs, kf_dir, workdir, vtype)
        # if anno_map:
        #     _apply_annotations(anno_map, notes_path, wiki_path, workdir,
        #                        sanitize_filename(video_title))
        postprocess.main([wiki_path, "--category", "concept"])
        log(f"✅ 视频入库完成: {wiki_path}")
    else:
        log("⚠️ 视频入库未完成（wiki 页未生成）")
    return result


def merge_part_notes(part_notes, series_title, workdir):
    """合并各分集传统笔记为一份合集笔记（`# 第X讲` 标题结构，不用 --- 分隔课时）。

    part_notes: [(part_title, notes_path)]。返回合并笔记路径。
    """
    lines = [f"# {series_title}", ""]
    for i, (pt, path) in enumerate(part_notes, 1):
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            content = f.read()
        # 去掉单篇自己的一级标题，内容并入合集
        content = re.sub(r"^#\s+.*(\n|$)", "", content, count=1).strip()
        lines += [f"# 第{i}讲：{pt}", "", content, ""]
    merged = os.path.join(workdir, f"{sanitize_filename(series_title)}_notes.md")
    with open(merged, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"合集笔记合并: {merged}（{len(part_notes)} 讲）")
    return merged


def ingest_video_collection(coll, series_title, workdir, force=False, images=False):
    """合集/分P 入库：每个分集独立跑单视频 pipeline（不出 wiki），
    然后合并笔记 → 一份五层提炼 → 一次入库（对齐原 skill 合集模式）。"""
    parts = coll["parts"]
    kind_cn = "合集" if coll["kind"] == "season" else "分P"
    log(f"=== {kind_cn}入库: {series_title}（{len(parts)} 个分集）===")

    part_notes, kf_dirs, vision_paths, raw_rels, all_refs = [], [], [], [], []
    signals_paths = []
    total_duration = 0
    for i, p in enumerate(parts, 1):
        part_title = p["title"] or f"第{i}集"
        purl = f"https://www.bilibili.com/video/{p['bvid']}"
        if p.get("page"):
            purl += f"?p={p['page']}"
        part_workdir = os.path.join(workdir, f"p{i:02d}_{sanitize_filename(part_title)[:40]}")
        log(f"--- 分集 {i}/{len(parts)}: {part_title} ---")
        r = _ingest_video_single(purl, part_title, part_workdir, force=force,
                                 duration=p.get("duration") or 0,
                                 write_wiki=False,
                                 raw_title=f"{series_title}·{part_title}",
                                 images=images)
        total_duration += r.get("duration") or 0
        part_notes.append((part_title, r["notes_path"]))
        if r["kf_dir"]:
            kf_dirs.append(r["kf_dir"])
        if r["vision_path"]:
            vision_paths.append(r["vision_path"])
        if r["raw_rel"]:
            raw_rels.append(r["raw_rel"])
        if r.get("signals_path"):
            signals_paths.append(r["signals_path"])
        all_refs.extend(r["notes_refs"])

    # 合并笔记 → 一份五层提炼
    topic = sanitize_filename(series_title)
    merged = os.path.join(workdir, f"{topic}_notes.md")
    if not force and os.path.exists(merged):
        log(f"⏭️ 跳过合集笔记合并（{os.path.basename(merged)} 已存在）")
    else:
        merged = merge_part_notes(part_notes, series_title, workdir)

    ok, issues = checkpoint_b_check(merged, total_duration, require_images=images)
    if ok:
        log(f"✅ CHECKPOINT B 通过（合集总时长 {total_duration}s）")
    else:
        for i in issues:
            log(f"⚠️ CHECKPOINT B: {i}")

    wiki_path = os.path.join(VAULT, "concepts", f"{sanitize_filename(series_title)}.md")
    if not force and os.path.exists(wiki_path):
        wiki_refs = _extract_image_refs(wiki_path)
        log(f"⏭️ 跳过五层提炼（wiki 页已存在，{len(wiki_refs)} 图）")
    else:
        wiki_path, wiki_refs = llm_generate_five_layer(
            merged, series_title, kf_dirs, vision_paths, raw_rels,
            signals_path=signals_paths, video_type=detect_video_type(series_title))
    if wiki_path:
        refs = set(all_refs) | set(wiki_refs)
        n = copy_images_to_vault(refs, kf_dirs, topic)
        log(f"图片入库: {n} 张 → raw/assets/{topic}/")
        postprocess.main([wiki_path, "--category", "concept"])
        log(f"✅ {kind_cn}入库完成: {wiki_path}")
        return wiki_path
    log(f"⚠️ {kind_cn}入库未完成（wiki 页未生成）")
    return None


def ingest_video(url, title=None, workdir=None, force=False, max_parts=None, images=False):
    """视频完整 pipeline。自动识别 合集/分P；默认断点续跑（产物存在则跳过）。

    max_parts: 合集/分P 只处理前 N 集（尝鲜/测试用；None = 全部）。
    images: 配图版（关键帧/识图/图文结合）；默认 False = 纯文字，跳过图片相关步骤。
    """
    meta = fetch_video_metadata(url)
    video_title = title or meta.get("title", "未命名视频")
    if workdir is None:
        workdir = os.path.join(WORK_ROOT, video_title)
    os.makedirs(workdir, exist_ok=True)
    log(f"=== 视频入库: {video_title}（{meta.get('duration', '?')}s）{'[force 全量重跑]' if force else ''}===")

    # 合集/分P 判断（B站：ugc_season = 合集，videos>1 = 分P）
    coll = fetch_bilibili_parts(url)
    if coll:
        if max_parts and len(coll["parts"]) > max_parts:
            log(f"合集共 {len(coll['parts'])} 集，--max-parts={max_parts} 只处理前 {max_parts} 集")
            coll["parts"] = coll["parts"][:max_parts]
        log(f"检测到{'合集' if coll['kind'] == 'season' else '分P'}: {coll['series_title']}（{len(coll['parts'])} 集）")
        return ingest_video_collection(coll, title or coll["series_title"], workdir, force=force, images=images)

    r = _ingest_video_single(url, video_title, workdir, force=force,
                             duration=meta.get("duration", 0), images=images)
    return r.get("wiki_path")


# ================= 临时读取（--peek：处理到传统笔记，不入库） =================
def _rebuild_tmp_index():
    """更新临时学习区 FTS 索引（TEMP_ROOT/.kb/kb_fts.db，独立于主库）。

    子进程跑 kb_index.py build（env 换 SOLOMON_VAULT/SOLOMON_FTS_DB 即独立库）。
    失败不阻断（查询侧会静默跳过缺失的临时库）。
    """
    if not TEMP_ROOT or not os.path.isdir(TEMP_ROOT):
        return
    kb_script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb_index.py")
    env = dict(os.environ)
    env["SOLOMON_VAULT"] = TEMP_ROOT
    env["SOLOMON_FTS_DB"] = os.path.join(TEMP_ROOT, ".kb")
    try:
        p = subprocess.run([PYTHON, kb_script, "build"], env=env, cwd=os.path.dirname(kb_script),
                           capture_output=True, text=True, timeout=300)
        if p.returncode == 0:
            log("临时库索引更新: OK（TEMP_ROOT/.kb/kb_fts.db）")
        else:
            log(f"⚠️ 临时库索引更新失败: {p.stderr.strip()[:200]}")
    except Exception as e:  # noqa: BLE001
        log(f"⚠️ 临时库索引更新异常: {e}")


def ingest_video_peek(url, title=None, workdir=None, max_parts=None, images=True):
    """临时读取：视频处理到传统笔记（含配图），产物落临时学习区 TEMP_ROOT/<标题>。

    不写 vault、不生成正式知识页、不 postprocess（write_wiki=False + raw 落 workdir）。
    完成后把笔记挂入临时库 concepts/ 并重建临时 FTS，供 query_kb --scope 联合检索/
    @solomon 追问。视频产物（下载/转写/关键帧/识图/notes）全部保留在临时 workdir，
    之后可由 `ingest.py --from-notes <临时笔记> --workdir <临时目录> --title <标题>` 转正入库。
    """
    meta = fetch_video_metadata(url)
    video_title = title or meta.get("title", "未命名视频")
    if workdir is None:
        workdir = os.path.join(TEMP_ROOT, video_title)
    os.makedirs(workdir, exist_ok=True)
    log(f"=== 临时读取: {video_title}（{meta.get('duration', '?')}s）"
        f"{'[纯文字--fast]' if not images else '[配图]'}，产物落临时区，不入库 ===")

    coll = fetch_bilibili_parts(url)
    if coll:
        log(f"⚠️ 检测到{'合集' if coll['kind'] == 'season' else '分P'}（{len(coll['parts'])} 集）："
            f"临时读取默认只取本集；合集请用正式入库 ingest.py（含 --max-parts N 逐集）")

    r = _ingest_video_single(url, video_title, workdir, force=False,
                             duration=meta.get("duration", 0),
                             write_wiki=False, raw_to_vault=False, images=images)
    notes_path = r.get("notes_path")
    if not notes_path or not os.path.exists(notes_path):
        log("⚠️ 临时读取未生成笔记（见上方日志）")
        return None

    # 笔记挂入临时库（concepts/<标题>.md 供 FTS 索引；copy 而非 move，workdir 原笔记保留）
    topic = sanitize_filename(video_title)
    concept_dir = os.path.join(TEMP_ROOT, "concepts")
    os.makedirs(concept_dir, exist_ok=True)
    concept_path = os.path.join(concept_dir, f"{topic}.md")
    if (not os.path.exists(concept_path)
            or os.path.getmtime(notes_path) > os.path.getmtime(concept_path)):
        shutil.copy2(notes_path, concept_path)
        log(f"笔记挂入临时库: concepts/{topic}.md")

    _rebuild_tmp_index()
    log(f"✅ 临时读取完成（未入库）: {video_title}\n"
        f"  笔记: {notes_path}\n"
        f"  临时库页: concepts/{topic}.md\n"
        f"  转正: python3.14 {os.path.basename(__file__)} --from-notes {notes_path} "
        f"--workdir {workdir} --title \"{video_title}\"")
    return concept_path


# ================= 已有笔记 → 五层提炼 + 入库（独立入口） =================
def _fix_embed_colons(path):
    """把 md 里 ![[...]] 图片引用内的冒号改成横杠（与 vault assets 命名一致）。返回替换数。"""
    if not path or not os.path.exists(path):
        return 0
    with open(path, encoding="utf-8") as f:
        content = f.read()
    n = len(re.findall(r"!\[\[[^\]]*:", content))
    content = re.sub(r"!\[\[[^\]]+\]\]",
                     lambda m: m.group(0).replace(":", "-"), content)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return n


def ingest_five_layer_from_notes(notes_path, title=None, workdir=None,
                                 category="concept", force=False):
    """从已有传统笔记生成五层提炼 + 入库（跳过下载/转写/关键帧/笔记生成）。

    适用 worker-solomon 的「从传统笔记生成五层」独立场景（单视频重做 / 合集合并笔记）。
    workdir 用于定位 keyframes_deduped/（配图来源）+ vision_results.json + audio_signals.json。
    复用 llm_generate_five_layer + copy_images_to_vault + postprocess 闭环。
    """
    notes_path = os.path.abspath(os.path.expanduser(notes_path))
    if not os.path.exists(notes_path):
        raise SystemExit(f"❌ 笔记不存在: {notes_path}")
    base = os.path.splitext(os.path.basename(notes_path))[0]
    video_title = title or re.sub(r"_notes$", "", base)
    if workdir is None:
        workdir = os.path.dirname(notes_path)
    workdir = os.path.abspath(os.path.expanduser(workdir))
    log(f"=== 已有笔记 → 五层提炼 + 入库: {video_title} ===")

    # 定位断点续跑产物：关键帧目录 / 帧描述 / 音频信号
    kf_dir = None
    for d in ("keyframes_deduped", "keyframes"):
        p = os.path.join(workdir, d)
        if os.path.isdir(p) and os.listdir(p):
            kf_dir = p
            break
    vision_path = os.path.join(workdir, "vision_results.json")
    if not os.path.exists(vision_path):
        vision_path = None
    signals_path = os.path.join(workdir, "audio_signals.json")
    if not os.path.exists(signals_path):
        signals_path = None

    # raw 转写引用：已入库则关联，否则留空（sources 指向 raw）
    raw_rel = None
    raw_cand = os.path.join(VAULT, "raw", "articles",
                            f"{sanitize_filename(video_title)}_raw.md")
    if os.path.exists(raw_cand):
        raw_rel = f"raw/articles/{sanitize_filename(video_title)}_raw.md"

    # ① 传统笔记入库 raw/articles/<title>_notes.md（修冒号，供五层 sources 引用）
    topic = sanitize_filename(video_title)
    notes_rel = f"raw/articles/{topic}_notes.md"
    notes_vault = os.path.join(VAULT, "raw", "articles", f"{topic}_notes.md")
    os.makedirs(os.path.dirname(notes_vault), exist_ok=True)
    if force or not os.path.exists(notes_vault):
        shutil.copy2(notes_path, notes_vault)
        fixed = _fix_embed_colons(notes_vault)
        log(f"传统笔记入库: {notes_rel}" + (f"（修冒号 {fixed} 处）" if fixed else ""))

    # ② 五层提炼 → concepts/<title>.md
    vtype = detect_video_type(video_title)
    wiki_path = os.path.join(VAULT, "concepts", f"{topic}.md")
    if not force and os.path.exists(wiki_path):
        log(f"⏭️ 跳过五层提炼（wiki 页已存在，{len(_extract_image_refs(wiki_path))} 图）")
    else:
        sources_raw = [r for r in (raw_rel, notes_rel) if r]
        wiki_path, _wiki_refs = llm_generate_five_layer(
            notes_vault, video_title, kf_dir, vision_path, sources_raw,
            signals_path=signals_path, video_type=vtype)
        if not wiki_path:
            raise SystemExit("❌ 五层提炼生成失败")

    # ③ 复制引用图（raw_notes ∪ 五层提炼 引用并集，冒号→横杠）
    if kf_dir:
        refs = set(_extract_image_refs(notes_vault)) | set(_extract_image_refs(wiki_path))
        n = copy_images_to_vault(refs, kf_dir, topic)
        log(f"图片入库: {n} 张 → raw/assets/{topic}/")

    # ④ postprocess 闭环（FTS5 索引 / 相关页 / index / log / verify gate）
    postprocess.main([wiki_path, "--category", category])
    log(f"✅ 五层提炼+入库完成: {wiki_path}")
    return wiki_path


# ================= 文档入库 =================
def _extract_title_from_content(content):
    """从文档内容提取标题：优先第一个 # 一级标题，其次第一个非标记非空行（截 50 字）。"""
    for line in content.splitlines():
        s = line.strip()
        if s.startswith("# "):
            return s[2:].strip()
    for line in content.splitlines():
        s = line.strip()
        if s and not s.startswith(("#", ">", "|", "-", "```", "<!--", "---")):
            return s[:50]
    return ""


def _llm_generate_title(content, timeout=60):
    """用 LLM 从文档内容生成一个简洁、适合知识库检索的标题。失败返回空串。"""
    try:
        snippet = content[:2500]
        system = "你是知识库标题助手，只输出一个简洁中文标题，不要引号、不要标点结尾、不要解释。"
        user = f"给下面的文档起一个最能概括其主题、方便检索的标题（20 字以内）：\n\n{snippet}"
        t = llm_chat(system, user, temperature=0.2, max_tokens=40, timeout=timeout)
        t = (t or "").strip().strip('"\'“”《》【】。；，,; \n\t')
        return t[:40]
    except Exception as e:
        log(f"⚠️ LLM 标题生成失败，回退: {e}")
        return ""


def _dedupe_existing_page(topic: str, category: str) -> None:
    """去重替换：入库前若 concepts/entities 下已存在同标题（或去掉「-五层知识提炼」
    尾缀的变体）页面，先整体删除旧的（复用 kb_delete 清理），保证重复录入是替换
    而不是新建（2026-09-14 新设定）。"""
    try:
        from kb_delete import delete_page
    except Exception:
        return  # 删除模块缺失时跳过去重（不阻断入库）
    cat_dir = os.path.join(VAULT, "concepts" if category == "concept" else "entities")
    if not os.path.isdir(cat_dir):
        return
    for fn in os.listdir(cat_dir):
        if not fn.endswith(".md"):
            continue
        stem = os.path.splitext(fn)[0]
        if stem == topic or stem.replace("-五层知识提炼", "") == topic.replace("-五层知识提炼", ""):
            log(f"♻️ 检测到同标题已有页面，先删除旧页再入库: {fn}")
            delete_page(os.path.join(cat_dir, fn))
            break


def ingest_document(filepath, title=None, category="concept", web_url=None):
    """文件/粘贴内容入库：存 raw → 建 wiki → postprocess。支持 .md/.txt/.pptx 及
    markitdown 可转的格式（pdf/docx/xlsx/html/epub…）。

    web_url：非空表示内容来自网页正文，图片会下载到 vault raw/assets/<topic>/
    并改写引用（![[文件名]]），raw frontmatter 记 source_url 为原网页。
    """
    _lower = filepath.lower()
    if _lower.endswith(".pptx"):
        # PPTX：用 extract-pptx.py（stdlib zipfile+ET）抽文本。
        # 注意单文件模式 stdout 只有摘要行，须用 --output 落盘再读全文（2026-09-13 修）。
        extractor = os.path.join(SKILL_ASSETS.rstrip("/"), "extract-pptx.py")
        tmp_pptx = os.path.join("/tmp", f"pptx_{int(time.time())}.txt")
        code, _ = run([PYTHON, extractor, filepath, "--output", tmp_pptx], timeout=300)
        if code != 0 or not os.path.exists(tmp_pptx):
            raise RuntimeError(f"PPTX 文本提取失败: {code}（{filepath}）")
        with open(tmp_pptx, encoding="utf-8") as f:
            content = f.read()
        try:
            os.remove(tmp_pptx)
        except OSError:
            pass
        content = content.strip()
        if len(content) < 20:
            raise RuntimeError(f"PPTX 文本提取为空（可能是纯图片型 PPT，无文字层）: {filepath}")
        log(f"PPTX 文本提取: {len(content)} 字")
    elif _lower.endswith((".md", ".txt", ".text", ".markdown", ".qmd", ".rmd")):
        with open(filepath, encoding="utf-8") as f:
            content = f.read()
    else:
        # 其它格式（pdf/docx/xlsx/html/epub…）：markitdown 转 Markdown（纯 Python 零 torch）
        content = _to_markdown(filepath)
        if not content:
            raise RuntimeError(f"无法提取文本（markitdown 转换失败）: {filepath}")
        log(f"markitdown 转换: {len(content)} 字")
    base = os.path.splitext(os.path.basename(filepath))[0]
    # 标题优先 --title，其次 LLM 从内容生成，再回退第一个 # 标题，最后回退文件名
    # 防御：--title 可能带引号（subprocess 不经 shell 时引号是字面字符），剥掉
    if title:
        title = title.strip().strip('"\'“”《》【】。；，,; \t\n')
    doc_title = title or _llm_generate_title(content) or _extract_title_from_content(content) or base
    topic = sanitize_filename(doc_title)
    today = datetime.date.today().isoformat()
    # 去重替换：同标题页已存在 → 先删旧（页/raw/图片/index/log/FTS/反向引用全清），
    # 再入库，避免重复录入同一知识产生多个近似页（2026-09-14 新设定）。
    _dedupe_existing_page(topic, category)
    # 网页来源：下载正文里的图片到 vault raw/assets/<topic>/，引用改写为 ![[文件名]]
    if web_url and ("![" in content):
        img_dir = os.path.join(VAULT, "raw", "assets", topic)
        before = len(re.findall(r"!\[[^\]]*\]\([^)]+\)", content))
        content = _download_web_images(content, web_url, img_dir)
        after = len(re.findall(r"!\[[^\]]*\]\([^)]+\)", content))
        dl = len(os.listdir(img_dir)) if os.path.isdir(img_dir) else 0
        log(f"网页图片: 引用 {before}→{after}, 落盘 {dl} 张 → raw/assets/{topic}/")
    # 1. raw 原文
    raw_dir = os.path.join(VAULT, "raw", "articles")
    os.makedirs(raw_dir, exist_ok=True)
    raw_path = os.path.join(raw_dir, f"{topic}_raw.md")
    with open(raw_path, "w", encoding="utf-8") as f:
        f.write(f"---\nsource_url: {web_url or 'local'}\ningested: {today}\n---\n\n{content}")
    log(f"raw 原文: {raw_path}")
    # 2. wiki 页：raw 足够长 → LLM 五层提炼生成正式知识页（替代占位空壳）
    #    视频侧同款：raw 原文作为笔记输入；网页图片目录作为关键帧目录（有图则 L2/L3 配图）
    cat_dir = os.path.join(VAULT, "concepts" if category == "concept" else "entities")
    os.makedirs(cat_dir, exist_ok=True)
    wiki_path = os.path.join(cat_dir, f"{topic}.md")
    es = os.environ.get("SOLOMON_DOC_SKIP_WIKI")
    if not os.path.exists(wiki_path) and not es and len(content) > 800:
        # 网页图片目录（已有下载的 raw/assets/<topic>/）作为配图来源
        img_dir = os.path.join(VAULT, "raw", "assets", topic)
        img_dir = img_dir if os.path.isdir(img_dir) else None
        try:
            tmp_notes = os.path.join("/tmp", f"docnotes_{int(time.time())}.md")
            with open(tmp_notes, "w", encoding="utf-8") as f:
                f.write(content)
            wiki_path, _refs = llm_generate_five_layer(
                tmp_notes, doc_title, img_dir, None,
                raw_rel=[f"raw/articles/{os.path.basename(raw_path)}"],
                video_type=detect_video_type(doc_title),
                source_kind="doc",
            )
            try:
                os.remove(tmp_notes)
            except OSError:
                pass
        except Exception as exc:
            log(f"⚠️ 文档五层提炼失败，回落占位页: {exc}")
            wiki_path = os.path.join(cat_dir, f"{topic}.md")
            os.makedirs(cat_dir, exist_ok=True)
            if not os.path.exists(wiki_path):
                with open(wiki_path, "w", encoding="utf-8") as f:
                    f.write(f"""---
title: {doc_title}
created: {today}
updated: {today}
type: {category}
tags: []
sources: [raw/articles/{os.path.basename(raw_path)}]
---

# {doc_title}

（由 ingest.py 自动创建，内容待 LLM 提炼补充）

## 来源

- raw/articles/{os.path.basename(raw_path)}
""")
    elif not os.path.exists(wiki_path) and not es:
        # 短文本（≤800字）：直接建页（原文摘录），跳过 LLM 提炼（不值得）
        with open(wiki_path, "w", encoding="utf-8") as f:
            f.write(f"""---
title: {doc_title}
created: {today}
updated: {today}
type: {category}
tags: []
sources: [raw/articles/{os.path.basename(raw_path)}]
---

# {doc_title}

{content[:2000]}

## 来源

- raw/articles/{os.path.basename(raw_path)}
""")
    # 3. postprocess 自动闭环
    postprocess.main([wiki_path, "--category", category])
    log(f"✅ 文档入库完成: {wiki_path}")
    return wiki_path


# ================= 名称搜索 =================
# B站 wbi 签名（搜索接口反爬）
MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]
WBI_CACHE = {"keys": None}


def _get_wbi_keys():
    import hashlib
    if WBI_CACHE["keys"]:
        return WBI_CACHE["keys"]
    try:
        code, out = run([
            "curl", "-s", "--max-time", "10",
            "https://api.bilibili.com/x/web-interface/nav",
            "-H", "User-Agent: Mozilla/5.0",
        ], timeout=15)
        d = json.loads(out)
        img = d["data"]["wbi_img"]["img_url"].rsplit("/", 1)[1].split(".")[0]
        sub = d["data"]["wbi_img"]["sub_url"].rsplit("/", 1)[1].split(".")[0]
        WBI_CACHE["keys"] = (img, sub)
        return WBI_CACHE["keys"]
    except Exception:
        return None


def _wbi_sign(params):
    import hashlib
    import urllib.parse
    keys = _get_wbi_keys()
    if not keys:
        return params
    raw = keys[0] + keys[1]
    mixin = "".join(raw[i] for i in MIXIN_KEY_ENC_TAB)[:32]
    params["wts"] = int(time.time())
    query = urllib.parse.urlencode(sorted(params.items()))
    params["w_rid"] = hashlib.md5((query + mixin).encode()).hexdigest()
    return params


def search_by_name(name):
    """名称搜索发现。返回候选列表 [(title, url, platform)]。

    优先 B站搜索 API（wbi 签名），兜底 yt-dlp ytsearch（标注平台）。
    """
    candidates = []
    seen = set()

    def add(t, u, platform):
        if u and u not in seen:
            seen.add(u)
            candidates.append((t, u, platform))

    # ① B站搜索 API（wbi 签名）
    try:
        params = _wbi_sign({
            "search_type": "video", "keyword": name, "page": 1,
        })
        import urllib.parse as up
        qs = up.urlencode(params)
        code, out = run([
            "curl", "-s", "--max-time", "15",
            f"https://api.bilibili.com/x/web-interface/wbi/search/type?{qs}",
            "-H", "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "-H", "Referer: https://search.bilibili.com/",
        ], timeout=30)
        data = json.loads(out)
        for r in data.get("data", {}).get("result", [])[:5]:
            bvid = r.get("bvid", "")
            if not bvid:
                continue  # 跳过无 BV 号的空 URL（避免生成 https://.../video/ 坏链接）
            t = re.sub(r"<[^>]+>", "", r.get("title", ""))
            add(t, f"https://www.bilibili.com/video/{bvid}", "bilibili")
    except Exception:
        pass  # wbi 失败 → 兜底

    # ② 若 B站无结果，yt-dlp ytsearch 通用搜索（输出分离 + 跳 null）
    if not candidates:
        import subprocess as sp
        env = dict(os.environ)
        env.setdefault("http_proxy", PROXY)
        env.setdefault("https_proxy", PROXY)
        try:
            p = sp.run(
                ["yt-dlp", "--flat-playlist", "--dump-single-json", "--no-playlist",
                 "--proxy", "http://127.0.0.1:7890", f"ytsearch5:{name}"],
                capture_output=True, text=True, timeout=60, env=env,
            )
            raw = p.stdout
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                for e in json.loads(m.group(0)).get("entries", [])[:5]:
                    url = e.get("url") or ""
                    platform = "youtube" if "youtube" in url else "other"
                    add(e.get("title", "?"), url, platform)
        except Exception:
            pass
    return candidates


# ================= 主入口 =================
def main():
    ap = argparse.ArgumentParser(description="Solomon 统一入库入口")
    ap.add_argument("input", nargs="?", help="URL / 文件路径 / 名字 / 文本")
    ap.add_argument("--name", help="按名称搜索视频")
    ap.add_argument("--text", help="直接粘贴文本内容入库")
    ap.add_argument("--from-notes", dest="from_notes", metavar="NOTES_MD",
                    help="已有传统笔记路径 → 只生成五层提炼+入库（跳过下载/转写/关键帧/笔记生成，"
                         "配合 --workdir 定位关键帧/帧描述/音频信号）")
    ap.add_argument("--title", help="指定标题")
    ap.add_argument("--category", default="concept", help="文档类别 concept/entity")
    ap.add_argument("--workdir", help="视频工作目录（默认 WORK_ROOT/标题）")
    ap.add_argument("--force", action="store_true",
                    help="忽略已有产物全量重跑（默认断点续跑：产物存在则跳过）")
    ap.add_argument("--skip-preflight", action="store_true", help="跳过环境自检")
    ap.add_argument("--max-parts", type=int, default=None,
                    help="合集/分P 只处理前 N 集（默认全部）")
    ap.add_argument("--progress-file", metavar="PATH",
                    help="进度文件：每阶段 append 一行 \"[时间][耗时] 消息\" 并 flush（供轮询推送进度）")
    ap.add_argument("--images", action="store_true",
                    help="配图版：跑关键帧/识图/图文结合（默认纯文字，跳过图片相关步骤）")
    ap.add_argument("--peek", action="store_true",
                    help="临时读取（不入库）：视频处理到传统笔记，产物落临时学习区 TEMP_ROOT/<标题>，"
                         "供临时查看/@solomon 追问；可用 --from-notes 后续转正入库")
    ap.add_argument("--fast", action="store_true",
                    help="（配合 --peek）跳过识图，最快出文字（纯文字笔记）")
    args = ap.parse_args()

    if args.progress_file:
        global _PROGRESS_FILE, _PROGRESS_START
        _PROGRESS_FILE = os.path.abspath(os.path.expanduser(args.progress_file))
        _PROGRESS_START = time.time()
        os.makedirs(os.path.dirname(_PROGRESS_FILE), exist_ok=True)
        with open(_PROGRESS_FILE, "w", encoding="utf-8") as f:
            f.write(f"# ingest progress {datetime.datetime.now().strftime('%F %T')}\n")

    if not args.name and not args.text and not args.input and not args.from_notes:
        ap.print_help()
        sys.exit(1)

    # 已有笔记 → 五层提炼 + 入库（独立入口，不做下载/转写/笔记生成）
    if args.from_notes:
        return ingest_five_layer_from_notes(
            args.from_notes, title=args.title, workdir=args.workdir,
            category=args.category, force=args.force)

    # 临时读取（--peek）：视频处理到传统笔记，产物落 TEMP_ROOT/<标题>，不入库
    if args.peek:
        if not args.skip_preflight:
            preflight("video")
        if args.name:
            log(f"搜索视频: {args.name}")
            cands = search_by_name(args.name)
            if not cands:
                print("❌ 未找到匹配视频（可改用 --name 加关键词，或直接传 URL）")
                sys.exit(1)
            for i, (t, u, pf) in enumerate(cands[:5]):
                print(f"  [{i+1}] [{pf}] {t}\n      {u}")
            print("（默认取第 1 个候选，如需其它请直接传 URL）")
            return ingest_video_peek(
                cands[0][1], title=args.title, workdir=args.workdir,
                max_parts=args.max_parts, images=not args.fast)
        if not args.input:
            print("❌ --peek 需要视频 URL 或 --name 标题")
            sys.exit(1)
        kind, val = classify_input(args.input)
        if kind == "url":
            return ingest_video_peek(
                val, title=args.title, workdir=args.workdir,
                max_parts=args.max_parts, images=not args.fast)
        if kind == "name":
            log(f"按名称搜索: {val}")
            cands = search_by_name(val)
            if not cands:
                print("❌ 未找到匹配视频（可改用 --name 加关键词，或直接传 URL）")
                sys.exit(1)
            for i, (t, u, pf) in enumerate(cands[:5]):
                print(f"  [{i+1}] [{pf}] {t}\n      {u}")
            print("（默认取第 1 个候选，如需其它请直接传 URL）")
            return ingest_video_peek(
                cands[0][1], title=args.title, workdir=args.workdir,
                max_parts=args.max_parts, images=not args.fast)
        print("❌ --peek 仅支持视频（网址或 --name 标题）")
        sys.exit(1)

    # 输入形态 → preflight（视频全量检查，文档只查 vault + LLM 代理）
    video_mode = bool(args.name)
    if args.input and not args.name:
        kind0, _ = classify_input(args.input)
        video_mode = kind0 in ("url", "name")
    if not args.skip_preflight:
        preflight("video" if video_mode else "doc")

    # --name 显式参数优先
    if args.name:
        log(f"搜索视频: {args.name}")
        cands = search_by_name(args.name)
        if not cands:
            print("❌ 未找到匹配视频（可改用 --name 加关键词，或直接传 URL）")
            sys.exit(1)
        for i, (t, u, pf) in enumerate(cands[:5]):
            print(f"  [{i+1}] [{pf}] {t}\n      {u}")
        # 无交互：默认取第一个；如需其它请直接传 URL
        print("（默认取第 1 个候选，如需其它请直接传 URL）")
        return ingest_video(cands[0][1], title=args.title, workdir=args.workdir, force=args.force, max_parts=args.max_parts, images=args.images)

    # --text 显式参数：直接粘贴文本入库（写入临时文件走文档入库）
    if args.text:
        tmp = os.path.join("/tmp", f"ingest_{int(time.time())}.md")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(args.text)
        return ingest_document(tmp, title=args.title, category=args.category)

    kind, val = classify_input(args.input)
    if kind == "url":
        return ingest_video(val, title=args.title, workdir=args.workdir, force=args.force, max_parts=args.max_parts, images=args.images)
    if kind == "web":
        log(f"网页正文提取: {val}")
        content = _web_to_markdown(val)
        if not content:
            log("❌ 网页正文提取失败（网络/反爬/压缩响应/非文章页）")
            print("❌ 网页正文提取失败（网络/反爬/非文章页），可改用文件路径或 --text")
            sys.exit(1)
        tmp = os.path.join("/tmp", f"ingest_web_{int(time.time())}.md")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"来源: {val}\n\n{content}")
        return ingest_document(tmp, title=args.title, category=args.category, web_url=val)
    if kind == "file":
        return ingest_document(val, title=args.title, category=args.category)
    if kind == "text":
        # 写入临时文件走文档入库
        tmp = os.path.join("/tmp", f"ingest_{int(time.time())}.md")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(val)
        return ingest_document(tmp, title=args.title, category=args.category)
    # name 兜底
    log(f"按名称搜索: {val}")
    cands = search_by_name(val)
    if not cands:
        print("❌ 未找到匹配视频（可改用 --name 加关键词，或直接传 URL）")
        sys.exit(1)
    for i, (t, u, pf) in enumerate(cands[:5]):
        print(f"  [{i+1}] [{pf}] {t}\n      {u}")
    print("（默认取第 1 个候选，如需其它请直接传 URL）")
    return ingest_video(cands[0][1], title=args.title, workdir=args.workdir, force=args.force, max_parts=args.max_parts)


if __name__ == "__main__":
    main()
