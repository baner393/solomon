#!/usr/bin/env python3
"""统一转写入口：sherpa-onnx SenseVoice（主引擎，CPU int8，x10-17 实时，无 torch）
    + faster-whisper batched（可选 GPU 引擎，服务器上云后可用）。

用法: transcribe_cli.py <audio.wav> <out.json> [--lang auto|zh|en] [--engine auto|sherpa|batched]
输出: [{"start": float, "end": float, "text": str}]（与 whisper_cli.py 兼容）
长音频按 CHUNK_SEC 分块（控内存，WSL 4GB 限制）。模型目录可用 SHERPA_SENSEVOICE_DIR 覆盖。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import wave

CHUNK_SEC = 300          # 5 分钟/块
DEFAULT_MODEL_DIR = os.path.expanduser(
    "~/models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17")
_SENT_END = re.compile(r"[。！？!?；;]")


def _probe_duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def _iter_chunks(audio, chunk_sec=CHUNK_SEC):
    """yield (offset_sec, path)。短音频整段；长音频 ffmpeg 切块（控内存峰值）。"""
    total = _probe_duration(audio)
    if total <= chunk_sec * 1.5:
        yield 0.0, audio
        return
    tmpdir = tempfile.mkdtemp(prefix="svchunk_", dir=os.environ.get("TMPDIR") or "/tmp")
    try:
        n = int(total // chunk_sec) + (1 if total % chunk_sec else 0)
        for i in range(n):
            off = i * chunk_sec
            piece = os.path.join(tmpdir, "seg_%03d.wav" % i)
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(off), "-i", audio, "-t", str(chunk_sec),
                 "-ar", "16000", "-ac", "1", piece], capture_output=True)
            if not os.path.exists(piece) or os.path.getsize(piece) < 1024:
                continue
            yield off, piece
            os.remove(piece)
    finally:
        for f in glob.glob(os.path.join(tmpdir, "*")):
            try:
                os.remove(f)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


def load_sherpa(model_dir=None, num_threads=4):
    import sherpa_onnx
    md = model_dir or os.environ.get("SHERPA_SENSEVOICE_DIR") or DEFAULT_MODEL_DIR
    model = os.path.join(md, "model.int8.onnx")
    tokens = os.path.join(md, "tokens.txt")
    if not (os.path.exists(model) and os.path.exists(tokens)):
        raise RuntimeError(f"sherpa-onnx SenseVoice 模型缺失: {md}")
    return sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=model, tokens=tokens, num_threads=num_threads, use_itn=True)


def _words_to_segments(words, max_chars=40, gap=1.5):
    """词级 (token, ts) → 句级段。句末标点/词间隔/长度阈值切句。"""
    segs, buf_txt, buf_start, last_ts = [], "", None, None
    for tok, ts in words:
        if not tok.strip():
            continue
        if buf_txt and (ts - last_ts > gap or len(buf_txt) >= max_chars
                        or _SENT_END.search(buf_txt[-1])):
            segs.append((buf_start, last_ts + 0.3, buf_txt))
            buf_txt, buf_start = "", None
        if not buf_txt:
            buf_start = ts
        buf_txt += tok
        last_ts = ts
    if buf_txt:
        segs.append((buf_start, last_ts + 0.3, buf_txt))
    return segs


def sherpa_transcribe(rec, audio, prog):
    import numpy as np
    segs = []
    for i, (off, path) in enumerate(_iter_chunks(audio), 1):
        with wave.open(path, "rb") as w:
            sr = w.getframerate()
            arr = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16
                                ).astype(np.float32) / 32768.0
        s = rec.create_stream()
        s.accept_waveform(sr, arr)
        rec.decode_stream(s)
        words = list(zip(s.result.tokens, s.result.timestamps))
        segs.extend({"start": round(st + off, 3), "end": round(en + off, 3), "text": t.strip()}
                    for st, en, t in _words_to_segments(words) if t.strip())
        prog(i, len(segs))
    return segs


def load_batched(device):
    # large-v3-turbo：英文场景 GPU 最优（x9.2 实测，比 medium 快质量高）；
    # 中文场景反正走 sherpa 主引擎，这里不背中文质量的锅
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    m = WhisperModel("large-v3-turbo", device=device, compute_type="float16")
    return BatchedInferencePipeline(model=m)


def _fw_one(pipe, path, lang):
    segments, _info = pipe.transcribe(path, batch_size=8,
                                      language=(lang if lang in ("zh", "en") else None))
    return [(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]


def fw_transcribe(pipe, audio, lang, prog):
    segs = []
    for i, (off, path) in enumerate(_iter_chunks(audio), 1):
        part = _fw_one(pipe, path, lang)
        segs.extend({"start": round(st + off, 3), "end": round(en + off, 3), "text": t}
                    for st, en, t in part)
        prog(i, len(segs))
    return segs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("audio")
    ap.add_argument("out")
    ap.add_argument("--lang", default="auto", choices=["auto", "zh", "en"])
    ap.add_argument("--engine", default="auto", choices=["auto", "sherpa", "batched"])
    ap.add_argument("--device", default="cuda",
                    help="batched 引擎的设备（sherpa 恒为 CPU int8）")
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()

    order = ["batched"] if (args.engine == "batched" or args.lang == "en") else ["sherpa"]
    if args.engine == "auto":
        order = ["sherpa"]  # 主引擎：CPU 稳定 + 最快；batched 仅显式选择
    if args.engine == "batched" and args.lang != "en":
        order = ["sherpa", "batched"] if args.engine == "auto" else ["batched"]

    def prog(name):
        return lambda i, n: print(f"[transcribe] {name} 块{i}（累计 {n} 段）", flush=True)

    errors = []
    for eng in order:
        t0 = time.time()
        try:
            if eng == "sherpa":
                rec = load_sherpa(num_threads=args.threads)
                print(f"[transcribe] sherpa 加载 {time.time() - t0:.1f}s", flush=True)
                t1 = time.time()
                segs = sherpa_transcribe(rec, args.audio, prog("sherpa"))
            else:
                pipe = load_batched(args.device)
                print(f"[transcribe] batched 加载 {time.time() - t0:.1f}s", flush=True)
                t1 = time.time()
                segs = fw_transcribe(pipe, args.audio, args.lang, prog("batched"))
            dt = time.time() - t1
            if not segs:
                raise RuntimeError("转写结果为空")
            dur = segs[-1]["end"]
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump(segs, f, ensure_ascii=False)
            speed = f"{dur / dt:.1f}x" if dt > 0 else "?"
            print(f"[transcribe] {eng} 完成: {len(segs)} 段 / {dur:.0f}s 音频 / "
                  f"{dt:.1f}s 转写（{speed} 实时）→ {args.out}", flush=True)
            return 0
        except Exception as e:
            errors.append(f"{eng}: {e}")
            print(f"[transcribe] {eng} 失败: {e}", flush=True)
    print("[transcribe] 全部引擎失败: " + " | ".join(errors), file=sys.stderr, flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
