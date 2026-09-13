#!/usr/bin/env python3
"""
分段转写脚本 v2：将长音频切成30分钟片段，逐段转写，合并JSON。
修复：ffmpeg切片后验证文件存在；更好的错误处理；转写失败不阻塞。
用法：python3.14 -u whisper_chunked.py <audio.wav> <output.json> [chunk_seconds]
"""
import sys
import os
import json
import subprocess

def split_audio(audio_path, chunk_seconds=1800):
    """将音频分成多个片段"""
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", audio_path
    ]
    duration = float(subprocess.check_output(cmd).decode().strip())
    print(f"音频总时长: {duration:.1f}秒 ({duration/60:.1f}分钟)")
    
    chunks = []
    start = 0
    chunk_idx = 0
    while start < duration:
        end = min(start + chunk_seconds, duration)
        chunk_path = f"/tmp/chunk_{chunk_idx:03d}.wav"
        
        actual_duration = min(chunk_seconds, duration - start)
        cmd = [
            "ffmpeg", "-y", "-i", audio_path,
            "-ss", str(start), "-t", str(actual_duration),
            "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
            chunk_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if os.path.exists(chunk_path) and os.path.getsize(chunk_path) > 0:
            chunks.append((chunk_path, start))
            print(f"  切片 {chunk_idx+1}: {start/60:.1f}-{end/60:.1f}分钟 ({actual_duration:.0f}s) ✓")
        else:
            print(f"  切片 {chunk_idx+1}: FAILED ✗")
        
        start = end
        chunk_idx += 1
    
    return chunks, duration

def main():
    if len(sys.argv) < 3:
        print("用法: python3.14 -u whisper_chunked.py <audio.wav> <output.json> [chunk_seconds]")
        sys.exit(1)
    
    audio_path = sys.argv[1]
    output_path = sys.argv[2]
    chunk_seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 1800
    
    if not os.path.exists(audio_path):
        print(f"错误: 音频文件不存在: {audio_path}")
        sys.exit(1)
    
    # 切片
    print("=== 切片 ===")
    chunks, duration = split_audio(audio_path, chunk_seconds)
    print(f"共生成 {len(chunks)} 个有效片段")
    
    if not chunks:
        print("错误: 没有生成任何有效片段")
        sys.exit(1)
    
    # 加载模型
    print("=== 加载 Whisper 模型 ===")
    from faster_whisper import WhisperModel
    for device, compute_type in [("cuda", "int8"), ("cpu", "int8")]:
        try:
            model = WhisperModel("small", device=device, compute_type=compute_type)
            print(f"模型加载成功 ({device})")
            break
        except Exception as e:
            print(f"{device} 模式失败: {e}")
            if device == "cpu":
                sys.exit(1)
            continue
    
    # 逐段转写
    print("=== 转写 ===")
    all_chunks = []
    chunk_starts = []
    for i, (chunk_path, offset) in enumerate(chunks):
        print(f"转写片段 {i+1}/{len(chunks)} (偏移 {offset/60:.1f}分钟)...", end=" ", flush=True)
        try:
            segments, info = model.transcribe(chunk_path, beam_size=5, language="zh")
            segs = []
            for seg in segments:
                segs.append({
                    "start": round(seg.start, 2),
                    "end": round(seg.end, 2),
                    "text": seg.text.strip()
                })
            all_chunks.append(segs)
            chunk_starts.append(offset)
            print(f"✓ ({len(segs)} 段)")
        except Exception as e:
            print(f"✗ 失败: {e}")
            all_chunks.append([])
            chunk_starts.append(offset)
        
        # 清理临时文件
        try:
            os.remove(chunk_path)
        except:
            pass
    
    # 合并
    print("=== 合并 ===")
    merged = []
    for chunk_segs, offset in zip(all_chunks, chunk_starts):
        for seg in chunk_segs:
            merged.append({
                "start": round(seg["start"] + offset, 2),
                "end": round(seg["end"] + offset, 2),
                "text": seg["text"]
            })
    merged.sort(key=lambda x: x["start"])
    print(f"合并完成，共 {len(merged)} 段")
    
    # 保存
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else ".", exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    print(f"已保存到: {output_path}")

if __name__ == "__main__":
    main()
