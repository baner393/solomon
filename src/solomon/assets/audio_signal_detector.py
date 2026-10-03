#!/usr/bin/env python3
"""
audio_signal_detector.py - Analyze Whisper JSON output to detect audio emphasis signals.

Detects:
  - Dynamic pauses (silent gaps between segments)
  - Keyword repetition via jieba TF-IDF in sliding windows
  - Pace changes (slow emphasis / fast information density)

Requires:
  pip install jieba  (or: pip install --break-system-packages jieba)

CLI:
  python3 audio_signal_detector.py whisper.json title="视频标题" --output signals.json

Input:  Whisper JSON file (array of {"start": float, "end": float, "text": str})
Output: JSON file with {"signals": [...], "video_type_hint": str}
"""

import json
import math
import re
import sys
import argparse
from collections import defaultdict

try:
    import jieba
    import jieba.analyse
except ImportError:
    print(
        "ERROR: jieba is required.\n"
        "  pip install jieba\n"
        "  # or on system-managed Python:\n"
        "  # pip install --break-system-packages jieba",
        file=sys.stderr,
    )
    sys.exit(1)

# ---------------------------------------------------------------------------
# Three-level stopwords defense
# ---------------------------------------------------------------------------

# Level 1: Static Chinese water words (common filler/emphasis terms)
STATIC_STOPWORDS: set[str] = {
    "文件", "点击", "大家", "这里", "然后", "这个", "我们", "看到",
    "非常", "重要", "操作", "就是", "可以", "一个", "什么", "对吧",
    "是不是", "的话", "其实", "所以说", "因为", "所以", "如果", "那么",
    "现在", "还有", "但是", "不是", "这样", "那个", "怎么", "还没",
    "还是", "东西", "知道", "意思", "一下", "时候", "比较", "应该",
    "可能", "朋友", "问题", "功能", "设置", "使用", "进行", "通过",
    "以及", "关于",
    # v4.1 新增：教程类视频高频水词
    "命令", "项目", "对话", "界面", "窗口", "终端", "提示", "输入",
    "输出", "参数", "效果", "运行", "安装", "配置", "代码", "版本",
    "文件夹", "目录", "页面", "网站", "程序", "工具", "方式", "方法",
    "流程", "步骤", "地方", "情况", "例子", "测试", "模型", "数据",
}

# Level 3: Whitelist — core technical terms that must NEVER be filtered
TECH_WHITELIST: set[str] = {
    "RAG", "Docker", "K8s", "Embedding", "Prompt", "ComfyUI", "Claude",
    "Codex", "Agent", "API", "MCP", "SDK", "JSON", "YAML", "Markdown",
    "Git", "GitHub", "Node", "Python", "React", "Vue", "Angular", "Next",
    "Vercel", "Netlify", "SSH", "GPU", "CUDA", "CPU", "RAM", "SSD",
    "HTTP", "HTTPS", "URL", "UI", "UX", "IDE", "CLI", "TTS", "ASR",
    "LLM", "SLM", "VLM", "GPT", "GPT-4", "GPT-4o", "DALL", "Whisper",
    "PyTorch", "TensorFlow", "Pandas", "NumPy", "Matplotlib", "OpenCV",
    "Pillow", "Selenium", "Puppeteer", "Playwright",
}
# Build case-insensitive lookup for whitelist
_WHITELIST_LOWER: set[str] = {w.lower() for w in TECH_WHITELIST}


# Load static stopwords into jieba so TF-IDF itself down-weights them
jieba.analyse.default_tfidf.stop_words.update(STATIC_STOPWORDS)


def _is_whitelisted(keyword: str) -> bool:
    """Level 3: Return True if keyword is a core technical term."""
    return keyword.lower() in _WHITELIST_LOWER


def _is_dynamic_stopword(
    keyword: str,
    full_text: str,
    num_blocks: int = 50,
    block_threshold: float = 0.70,
) -> bool:
    """Level 2: Check if keyword appears in >block_threshold of text blocks.

    Splits the full subtitle text into *num_blocks* roughly equal chunks and
    counts how many chunks contain the keyword.  If it appears in more than
    *block_threshold* fraction of blocks, it's a ubiquitous water word.
    """
    if not full_text or not keyword:
        return False

    chunk_size = max(1, len(full_text) // num_blocks)
    hits = 0
    for idx in range(num_blocks):
        start = idx * chunk_size
        end = start + chunk_size if idx < num_blocks - 1 else len(full_text)
        chunk = full_text[start:end]
        if keyword in chunk:
            hits += 1

    ratio = hits / num_blocks
    return ratio > block_threshold


def _passes_stopword_defense(
    keyword: str, weight: float, full_text: str
) -> bool:
    """Apply all three levels and return True if the keyword should be kept."""
    # Level 3: Whitelist always passes
    if _is_whitelisted(keyword):
        return True
    # Level 1: Static stopwords filtered
    if keyword in STATIC_STOPWORDS:
        return False
    # Level 2: Dynamic distribution filter
    if _is_dynamic_stopword(keyword, full_text):
        return False
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

WORD_RE = re.compile(r"[A-Za-z]+")


def inject_english_words(title: str) -> None:
    """Dynamically inject English words from the video title into jieba's
    dictionary so they are tokenised correctly rather than split into
    individual characters."""
    for word in WORD_RE.findall(title):
        if len(word) >= 2:
            jieba.add_word(word, freq=999999, tag="eng")


def extract_keywords(texts: list[str], topk: int = 20) -> dict[str, float]:
    """Use jieba TF-IDF to extract keywords from a list of segment texts.
    Returns {keyword: tfidf_score} for the top-k keywords."""
    combined = " ".join(texts)
    tags = jieba.analyse.extract_tags(
        combined, topK=topk, withWeight=True,
        allowPOS=("n", "eng", "vn"),
    )
    return {tag: weight for tag, weight in tags}


# ---------------------------------------------------------------------------
# Signal detection
# ---------------------------------------------------------------------------

def detect_pauses(segments: list[dict]) -> list[dict]:
    """Dynamic pause detection.

    Threshold = max(1.5, T_avg * 2.0) where T_avg is the average gap between
    consecutive segments.  A gap larger than the threshold is flagged as a
    pause emphasising the spoken content that precedes or follows it.
    """
    if len(segments) < 2:
        return []

    gaps = []
    for i in range(1, len(segments)):
        gap = segments[i]["start"] - segments[i - 1]["end"]
        if gap > 0:
            gaps.append(gap)

    if not gaps:
        return []

    t_avg = sum(gaps) / len(gaps)
    threshold = max(1.5, t_avg * 2.0)

    signals: list[dict] = []
    for i in range(1, len(segments)):
        gap = segments[i]["start"] - segments[i - 1]["end"]
        if gap >= threshold:
            intensity = min(gap / threshold, 5.0)  # cap at 5×
            ctx = segments[i - 1]["text"][-40:] + " ..."
            signals.append({
                "type": "pause",
                "start_time": round(segments[i - 1]["end"], 3),
                "end_time": round(segments[i]["start"], 3),
                "intensity": round(intensity, 3),
                "context_text": ctx.strip(),
            })
    return signals


def detect_repetitions(segments: list[dict], window_sec: float = 30.0) -> list[dict]:
    """Repetition detection using a 30-second sliding window and jieba TF-IDF.

    For each window we extract keywords; any keyword appearing >=min_count times
    in the window AND passing the three-level stopwords defense is flagged as a
    repetition signal.

    Three-level defense:
      Level 1: Static stopwords (common Chinese water words)
      Level 2: Dynamic distribution filter (>70% of blocks = water word)
      Level 3: Whitelist protection (core technical terms always pass)
    """
    if not segments:
        return []

    # Build full text once for Level 2 dynamic distribution check
    full_text = " ".join(seg["text"] for seg in segments)

    signals: list[dict] = []
    seen: set[tuple[str, int]] = set()  # deduplicate: (keyword, 60s_window)

    for i, seg in enumerate(segments):
        window_start = seg["start"]
        window_end = window_start + window_sec

        # Collect all segments that overlap with [window_start, window_end)
        window_segs: list[str] = []
        for j in range(i, len(segments)):
            if segments[j]["start"] >= window_end:
                break
            if segments[j]["end"] > window_start:
                window_segs.append(segments[j]["text"])

        if len(window_segs) < 2:
            continue

        # TF-IDF keyword extraction for the window
        keywords = extract_keywords(window_segs, topk=30)

        combined_window = " ".join(window_segs)
        for kw, weight in keywords.items():
            # Minimum weight threshold: only consider keywords with TF-IDF > 0.3
            if weight <= 0.3:
                continue

            # Adaptive count threshold:
            #   Chinese words (non-ASCII) → need 4+ occurrences
            #   English words (ASCII only) → need 2+ occurrences
            min_count = 2 if kw.isascii() else 4

            count = combined_window.count(kw)
            if count >= min_count:
                # Three-level stopwords defense
                if not _passes_stopword_defense(kw, weight, full_text):
                    continue

                # Dedup: same keyword within 60s window only recorded once
                key = (kw, round(window_start / 60) * 60)
                if key in seen:
                    continue
                seen.add(key)
                signals.append({
                    "type": "repetition",
                    "start_time": round(window_start, 3),
                    "end_time": round(min(window_end, segments[-1]["end"]), 3),
                    "keyword": kw,
                    "count": count,
                })

    # Sort by start time
    signals.sort(key=lambda s: s["start_time"])
    return signals


def detect_pace_changes(segments: list[dict]) -> list[dict]:
    """Pace change detection.

    Computes chars_per_second for each segment.  Flags:
      - pace_slow: rate < 60% of the overall average  (slow emphasis)
      - pace_fast: rate > 150% of the overall average (fast info density)
    """
    if not segments:
        return []

    rates: list[tuple[int, float]] = []
    for idx, seg in enumerate(segments):
        duration = seg["end"] - seg["start"]
        if duration <= 0:
            continue
        chars = len(seg["text"])
        cps = chars / duration
        rates.append((idx, cps))

    if not rates:
        return []

    avg_cps = sum(r for _, r in rates) / len(rates)
    slow_threshold = avg_cps * 0.60
    fast_threshold = avg_cps * 1.50

    signals: list[dict] = []
    for idx, cps in rates:
        seg = segments[idx]
        if cps < slow_threshold:
            signals.append({
                "type": "pace_slow",
                "start_time": round(seg["start"], 3),
                "end_time": round(seg["end"], 3),
                "chars_per_sec": round(cps, 3),
                "avg_chars_per_sec": round(avg_cps, 3),
            })
        elif cps > fast_threshold:
            signals.append({
                "type": "pace_fast",
                "start_time": round(seg["start"], 3),
                "end_time": round(seg["end"], 3),
                "chars_per_sec": round(cps, 3),
                "avg_chars_per_sec": round(avg_cps, 3),
            })
    return signals


# ---------------------------------------------------------------------------
# Video type inference
# ---------------------------------------------------------------------------

def infer_video_type(signals: list[dict]) -> str:
    """Infer a rough video-type hint from signal patterns.

    Heuristics:
      - Many pace_slow + pauses          → "lecture / tutorial"
      - Many pace_fast signals           → "news / short-form"
      - Many repetitions                 → "marketing / pitch"
      - Balanced signals                 → "vlog / general"
    """
    counts = defaultdict(int)
    for s in signals:
        counts[s["type"]] += 1

    total = len(signals) or 1
    slow_ratio = (counts["pace_slow"] + counts["pause"]) / total
    fast_ratio = counts.get("pace_fast", 0) / total
    rep_ratio = counts.get("repetition", 0) / total

    if slow_ratio > 0.45:
        return "lecture / tutorial"
    if fast_ratio > 0.40:
        return "news / short-form"
    if rep_ratio > 0.30:
        return "marketing / pitch"
    return "vlog / general"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Detect audio emphasis signals from Whisper JSON output.",
    )
    parser.add_argument("whisper_json", help="Path to Whisper JSON file")
    parser.add_argument(
        "--title", default="",
        help="Video title (English words injected into jieba dict)",
    )
    parser.add_argument(
        "--output", "-o", default="signals.json",
        help="Output JSON path (default: signals.json)",
    )
    args = parser.parse_args()

    # Load Whisper segments
    with open(args.whisper_json, "r", encoding="utf-8") as f:
        segments = json.load(f)

    # Inject English words from title into jieba
    if args.title:
        inject_english_words(args.title)

    # Detect signals
    pause_signals = detect_pauses(segments)
    rep_signals = detect_repetitions(segments)
    pace_signals = detect_pace_changes(segments)

    all_signals = pause_signals + rep_signals + pace_signals
    all_signals.sort(key=lambda s: s["start_time"])

    video_type_hint = infer_video_type(all_signals)

    output = {
        "signals": all_signals,
        "video_type_hint": video_type_hint,
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"Detected {len(all_signals)} signals → {args.output}")
    print(f"  Pauses: {len(pause_signals)}")
    print(f"  Repetitions: {len(rep_signals)}")
    print(f"  Pace changes: {len(pace_signals)}")
    print(f"  Video type hint: {video_type_hint}")


if __name__ == "__main__":
    main()
