#!/usr/bin/env python3
"""
grid_stitch.py — 时间顺序拼图（每4帧拼2×2网格）

用法：
  python3.14 grid_stitch.py <input_dir> [output_dir] [grid_size] [cell_width]

参数：
  input_dir    帧目录（如 dhash_dedup.py 输出）
  output_dir   输出目录（默认：input_dir 同级的 grids）
  grid_size    每组帧数（默认：4 = 2×2）
  cell_width   每帧宽度px（默认：480）
"""

import os, sys, re
from pathlib import Path
from PIL import Image, ImageDraw

LABEL_HEIGHT = 24


def get_ts(fname):
    m = re.search(r'_(\d+)_', fname)
    return int(m.group(1)) / 1000.0 if m else 0


def ts_to_str(ts):
    h = int(ts // 3600)
    m = int((ts % 3600) // 60)
    s = int(ts % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def stitch_grid(frame_paths, cell_width=480, labels='ABCD'):
    """将多帧拼成网格"""
    n = len(frame_paths)
    cell_h = int(cell_width * 9 / 16)
    cols = 2 if n <= 4 else 3
    rows = (n + cols - 1) // cols

    grid_w = cell_width * cols
    grid_h = (cell_h + LABEL_HEIGHT) * rows
    grid = Image.new('RGB', (grid_w, grid_h), (30, 30, 30))
    draw = ImageDraw.Draw(grid)

    for i in range(n):
        r, c = divmod(i, cols)
        x = c * cell_width
        y = r * (cell_h + LABEL_HEIGHT)
        img = Image.open(frame_paths[i])
        img = img.resize((cell_width, cell_h), Image.LANCZOS)
        grid.paste(img, (x, y + LABEL_HEIGHT))
        draw.rectangle([x+2, y+2, x+30, y+20], fill=(0, 0, 0))
        draw.text((x+8, y+3), labels[i], fill="white")

    return grid


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    input_dir = Path(sys.argv[1])
    grid_size = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    cell_width = int(sys.argv[4]) if len(sys.argv) > 4 else 480

    if len(sys.argv) > 2:
        output_dir = Path(sys.argv[2])
    else:
        output_dir = input_dir.parent / "grids"

    if output_dir.exists():
        import shutil
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True)

    # 收集帧
    frames = []
    for fname in sorted(os.listdir(input_dir)):
        if not fname.endswith(('.jpg', '.png', '.jpeg')):
            continue
        frames.append({
            'file': fname,
            'path': input_dir / fname,
            'timestamp': get_ts(fname),
        })
    frames.sort(key=lambda f: f['timestamp'])

    print(f"输入: {len(frames)} 帧 | 每组: {grid_size} 帧 | 宽度: {cell_width}px")

    # 分组拼图
    labels = 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'[:grid_size]
    grids = []

    for i in range(0, len(frames), grid_size):
        group = frames[i:i+grid_size]
        grid_img = stitch_grid([f['path'] for f in group], cell_width, labels)
        grid_name = f"grid_{len(grids)+1:03d}.jpg"
        grid_path = output_dir / grid_name
        grid_img.save(grid_path, quality=90)
        grids.append({
            'name': grid_name,
            'frames': [f['file'] for f in group],
            'timestamps': [f['timestamp'] for f in group],
        })

    # 保存元数据
    import json
    meta_path = output_dir / "grid_meta.json"
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(grids, f, ensure_ascii=False, indent=2)

    print(f"输出: {len(grids)} 张网格 → {output_dir}")
    print(f"元数据: {meta_path}")


if __name__ == '__main__':
    main()
