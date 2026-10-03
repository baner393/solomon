#!/usr/bin/env python3
"""
whisper_cli.py — Bilibili 视频字幕转写脚本
使用 faster-whisper 将音频转为带时间戳的 JSON 文件。

用法:
    python3 whisper_cli.py <audio_path> [output_json_path]
"""

import sys
import os
import json

# CUDA 库路径
cuda_path = "/usr/local/cuda-12.6/lib64"
if os.path.isdir(cuda_path):
    ld_path = os.environ.get("LD_LIBRARY_PATH", "")
    if cuda_path not in ld_path:
        os.environ["LD_LIBRARY_PATH"] = f"{cuda_path}:{ld_path}" if ld_path else cuda_path


def main():
    if len(sys.argv) < 2:
        print("用法: python3 whisper_cli.py <audio_path> [output_json_path]", file=sys.stderr)
        sys.exit(1)

    audio_path = sys.argv[1]
    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    if not os.path.exists(audio_path):
        print(f"错误: 音频文件不存在: {audio_path}", file=sys.stderr)
        sys.exit(1)

    from faster_whisper import WhisperModel

    # 尝试 CUDA，失败回退 CPU
    for device, compute_type in [("cuda", "int8"), ("cpu", "int8")]:
        try:
            print(f"加载 Whisper 模型 (device={device}, compute_type={compute_type})...")
            model = WhisperModel("small", device=device, compute_type=compute_type)
            print(f"模型加载成功 ({device})")
            break
        except Exception as e:
            print(f"{device} 模式失败: {e}", file=sys.stderr)
            if device == "cpu":
                print("所有模式都失败了", file=sys.stderr)
                sys.exit(1)
            print("回退到 CPU 模式...")
            continue

    print(f"开始转写: {audio_path}")
    segments, info = model.transcribe(audio_path, beam_size=5, language="zh")

    result = []
    for seg in segments:
        result.append({
            "start": round(seg.start, 2),
            "end": round(seg.end, 2),
            "text": seg.text.strip()
        })
        if len(result) % 50 == 0:
            print(f"  已转写 {len(result)} 段...")

    print(f"转写完成，共 {len(result)} 段")

    output = json.dumps(result, ensure_ascii=False, indent=2)

    if output_path:
        os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(output)
        print(f"已保存到: {output_path}")
    else:
        print(output)


if __name__ == "__main__":
    main()
