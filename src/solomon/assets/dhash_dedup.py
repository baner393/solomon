#!/usr/bin/env python3
"""
dhash_dedup.py — 关键帧 dHash 去重 + 边缘密度选帧

逻辑：
1. 按课程（文件名前缀）分组，防止跨课程误合并
2. 遮黑底部字幕区（默认15%），消除字幕变化干扰
3. dHash 分组（阈值8），合并同一张PPT的不同标注阶段
4. 每组用边缘密度（Canny）选最复杂帧 = 标注最完整的版本
5. 输出去重后的帧（原图，不裁剪）

用法：
  python3.14 dhash_dedup.py <keyframes_dir> [output_dir] [threshold] [subtitle_ratio]

示例：
  python3.14 dhash_dedup.py ./keyframes_一 ./deduped_output 8 0.15

参数：
  keyframes_dir    关键帧目录（或包含多个 keyframes_XXX 子目录的父目录）
  output_dir       输出目录（默认：keyframes_dir 同级的 keyframes_deduped）
  threshold        dHash 阈值（默认：8）
  subtitle_ratio   底部字幕区比例（默认：0.15 = 15%）
"""

import os, sys, re, shutil, json
from pathlib import Path

try:
    from PIL import Image, ImageDraw
    import imagehash
    import cv2
    import numpy as np
except ImportError as e:
    print(f"缺少依赖: {e}")
    print("安装: pip install Pillow imagehash opencv-python numpy")
    sys.exit(1)


# ============================================================
# 配置
# ============================================================

DEFAULT_THRESHOLD = 8
DEFAULT_SUBTITLE_RATIO = 0.15
CANNY_LOW = 50
CANNY_HIGH = 150
MAX_TIME_SPAN = 600      # 时间窗口保底：组内最大时间跨度（秒），超限则拆分
SPLIT_WINDOW = 150       # 拆分子窗口宽度（秒）


# ============================================================
# 工具函数
# ============================================================

def get_ts(fname):
    """从文件名提取时间戳（毫秒→秒）"""
    m = re.search(r'_(\d+)_', fname)
    return int(m.group(1)) / 1000.0 if m else 0


def ts_to_str(ts):
    """秒→HH:MM:SS"""
    h = int(ts // 3600)
    m = int((ts % 3600) // 60)
    s = int(ts % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def compute_dhash_masked(image_path, subtitle_ratio):
    """遮黑底部字幕后计算 dHash"""
    img = Image.open(image_path)
    w, h = img.size
    masked = img.copy()
    draw = ImageDraw.Draw(masked)
    sub_y = int(h * (1 - subtitle_ratio))
    draw.rectangle([0, sub_y, w, h], fill=(0, 0, 0))
    return imagehash.dhash(masked)


def compute_edge_density(image_path, subtitle_ratio):
    """计算边缘密度（遮黑字幕区后，Canny 边缘检测）"""
    img = cv2.imread(str(image_path))
    if img is None:
        return 0.0
    h, w = img.shape[:2]

    # 遮黑底部字幕区
    sub_y = int(h * (1 - subtitle_ratio))
    img[sub_y:, :] = 0

    # 灰度 → Canny → 数边缘像素占比
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, CANNY_LOW, CANNY_HIGH)
    edge_pixels = np.count_nonzero(edges)
    total_pixels = w * h
    return round(edge_pixels / total_pixels * 100, 2)


# ============================================================
# 分组逻辑
# ============================================================

def detect_lectures(base_dir):
    """
    检测课程目录。
    支持两种结构：
    1. base_dir 下有 keyframes_一, keyframes_二 等子目录
    2. base_dir 本身就是单个关键帧目录
    """
    sub_dirs = sorted([d for d in base_dir.iterdir()
                       if d.is_dir() and d.name.startswith('keyframes_')])
    if sub_dirs:
        return sub_dirs
    # 单目录模式
    return [base_dir]


def collect_frames(lecture_dir, subtitle_ratio):
    """收集单个课程目录的所有帧，计算 dHash"""
    frames = []
    for fname in sorted(os.listdir(lecture_dir)):
        if not fname.endswith(('.jpg', '.png', '.jpeg')):
            continue
        fpath = lecture_dir / fname
        try:
            dh = compute_dhash_masked(fpath, subtitle_ratio)
            frames.append({
                'file': fname,
                'path': fpath,
                'timestamp': get_ts(fname),
                'dhash': dh,
            })
        except Exception as e:
            print(f"  ⚠️ 跳过 {fname}: {e}")
    frames.sort(key=lambda f: f['timestamp'])
    return frames


def dhash_group(frames, threshold):
    """dHash 分组：视觉相似的帧归为一组"""
    groups = []
    for f in frames:
        placed = False
        for group in groups:
            dist = f['dhash'] - group[0]['dhash']
            if dist <= threshold:
                group.append(f)
                placed = True
                break
        if not placed:
            groups.append([f])
    return groups


def select_best_frame(group, subtitle_ratio):
    """
    从分组中选出最复杂的帧（边缘密度最高）。
    如果是单帧组，直接返回。
    """
    if len(group) == 1:
        return group[0], False

    for f in group:
        f['edge_density'] = compute_edge_density(f['path'], subtitle_ratio)

    best = max(group, key=lambda f: f['edge_density'])
    last = group[-1]
    changed = best['file'] != last['file']
    return best, changed


# ============================================================
# 主流程
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    base_dir = Path(sys.argv[1])
    threshold = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_THRESHOLD
    subtitle_ratio = float(sys.argv[4]) if len(sys.argv) > 4 else DEFAULT_SUBTITLE_RATIO

    # 输出目录
    if len(sys.argv) > 2:
        output_dir = Path(sys.argv[2])
    else:
        output_dir = base_dir.parent / "keyframes_deduped"

    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    print(f"🔧 dHash 去重 + 边缘密度选帧")
    print(f"   阈值: {threshold} | 字幕区: {subtitle_ratio:.0%}")
    print(f"   输出: {output_dir}\n")

    # 检测课程目录
    lecture_dirs = detect_lectures(base_dir)

    total_original = 0
    total_kept = 0
    total_changed = 0
    all_results = []

    for lec_dir in lecture_dirs:
        lec_name = lec_dir.name
        print(f"=== {lec_name} ===")

        # 收集帧
        frames = collect_frames(lec_dir, subtitle_ratio)
        print(f"  帧数: {len(frames)}")
        total_original += len(frames)

        if not frames:
            print()
            continue

        # dHash 分组
        groups = dhash_group(frames, threshold)
        print(f"  分组: {len(groups)} 组")

        # 时间窗口保底：PPT视觉同质化严重时，防止不同时间的帧被错误合并
        # 例：12min和108min的白底PPT会被判为"相同"，但内容完全不同
        split_count = 0
        new_groups = []
        for group in groups:
            if len(group) <= 1:
                new_groups.append(group)
                continue
            timestamps = [f['timestamp'] for f in group]
            span = max(timestamps) - min(timestamps)
            if span > MAX_TIME_SPAN:
                group.sort(key=lambda f: f['timestamp'])
                cur = [group[0]]
                for f in group[1:]:
                    if f['timestamp'] - cur[-1]['timestamp'] > SPLIT_WINDOW:
                        new_groups.append(cur)
                        cur = [f]
                    else:
                        cur.append(f)
                new_groups.append(cur)
                split_count += 1
            else:
                new_groups.append(group)
        if split_count:
            print(f"  ⚠️ {split_count} 组时间跨度>{MAX_TIME_SPAN//60}min → 拆分为 {len(new_groups)} 组")
        groups = new_groups

        # 每组选最复杂帧
        lec_changed = 0
        for gi, group in enumerate(groups):
            best, changed = select_best_frame(group, subtitle_ratio)
            if changed:
                lec_changed += 1
                total_changed += 1

            # 复制到输出目录
            ext = Path(best['file']).suffix
            kept_name = f"{lec_name}_{gi+1:03d}_{ts_to_str(best['timestamp'])}{ext}"
            shutil.copy2(best['path'], output_dir / kept_name)

            # 记录结果
            all_results.append({
                'lecture': lec_name,
                'group_id': gi + 1,
                'group_size': len(group),
                'kept_file': kept_name,
                'kept_timestamp': best['timestamp'],
                'edge_density': best.get('edge_density', 0),
                'changed_from_last': changed,
                'deleted': [f['file'] for f in group if f['file'] != best['file']],
            })

        total_kept += len(groups)
        removed = len(frames) - len(groups)
        print(f"  保留: {len(groups)} | 删除: {removed} | 换帧: {lec_changed}/{sum(1 for g in groups if len(g)>1)}")
        print()

    # 汇总
    print(f"=== 汇总 ===")
    print(f"原始: {total_original} 帧")
    print(f"保留: {total_kept} 帧")
    print(f"删除: {total_original - total_kept} 帧")
    print(f"压缩率: {1 - total_kept/total_original:.1%}" if total_original > 0 else "")
    print(f"换帧: {total_changed} 组（边缘密度≠时间最晚）")

    # 保存结果 JSON
    result_path = output_dir / "dhash_dedup_result.json"
    with open(result_path, 'w', encoding='utf-8') as f:
        json.dump({
            'config': {
                'threshold': threshold,
                'subtitle_ratio': subtitle_ratio,
            },
            'summary': {
                'original': total_original,
                'kept': total_kept,
                'removed': total_original - total_kept,
                'changed': total_changed,
            },
            'groups': all_results,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n结果: {result_path}")
    print(f"输出: {output_dir}（{total_kept} 帧）")


if __name__ == '__main__':
    main()
