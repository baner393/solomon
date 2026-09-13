#!/usr/bin/env python3
"""
grid_vlm_pipeline.py — 完整流水线：拼图 → VLM 分析 → 文字分类

用法：
  python3.14 grid_vlm_pipeline.py <deduped_dir> [output_dir] [vlm_url]

参数：
  deduped_dir   dhash_dedup.py 输出的去重帧目录
  output_dir    输出目录（默认：deduped_dir 同级的 vlm_results）
  vlm_url       VLM 地址（默认：http://127.0.0.1:8081）

输出：
  output_dir/
  ├── grids/                  ← 拼图网格
  ├── vlm_raw.json            ← VLM 原始输出
  ├── classified.json         ← 分类结果
  └── summary.json            ← 统计摘要
"""

import os, sys, re, json, time, base64, io, shutil
from pathlib import Path
from PIL import Image, ImageDraw

try:
    import urllib.request
except ImportError:
    print("需要 urllib")
    sys.exit(1)

# 导入分类模块
sys.path.insert(0, str(Path(__file__).parent))
from classify_by_text import classify, classify_with_detail


# ============================================================
# 配置
# ============================================================

VLM_PROMPT = """分析这张2x2网格图（4张教学视频截图，左上A 右上B 左下C 右下D）。
对每张图用两行回答：

[图A]
文字：（画面中所有可见文字，没有则写"无"）
描述：（一句话说明画面展示什么）

[图B]
文字：
描述：

[图C]
文字：
描述：

[图D]
文字：
描述：

注意：文字部分要尽量完整，包括标题、正文、按钮文字等。"""

CELL_WIDTH = 480
LABEL_HEIGHT = 30
IMG_QUALITY = 80


# ============================================================
# 拼图
# ============================================================

def get_ts(fname):
    m = re.search(r'_(\d+)_', fname)
    return int(m.group(1)) / 1000.0 if m else 0

def ts_to_str(ts):
    h = int(ts // 3600)
    m = int((ts % 3600) // 60)
    s = int(ts % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"

def stitch_grid(frame_paths):
    """将最多4帧拼成2×2网格"""
    n = len(frame_paths)
    cell_h = int(CELL_WIDTH * 9 / 16)
    grid_w = CELL_WIDTH * 2
    grid_h = (cell_h + LABEL_HEIGHT) * 2
    grid = Image.new('RGB', (grid_w, grid_h), (40, 40, 40))
    draw = ImageDraw.Draw(grid)
    labels = ['A', 'B', 'C', 'D']
    
    for i in range(min(n, 4)):
        r, c = divmod(i, 2)
        x = c * CELL_WIDTH
        y = r * (cell_h + LABEL_HEIGHT)
        img = Image.open(frame_paths[i])
        img = img.resize((CELL_WIDTH, cell_h), Image.LANCZOS)
        grid.paste(img, (x, y + LABEL_HEIGHT))
        # 标签
        draw.rectangle([x+2, y+2, x+38, y+30], fill=(0, 0, 0))
        draw.text((x+10, y+4), labels[i], fill="white")
    
    return grid


# ============================================================
# VLM 调用
# ============================================================

def call_vlm(image_path, vlm_url, timeout=90):
    """调用 VLM 分析单张网格图"""
    img = Image.open(image_path)
    if img.width > 960:
        ratio = 960 / img.width
        img = img.resize((960, int(img.height * ratio)), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert('RGB').save(buf, format='JPEG', quality=IMG_QUALITY)
    b64 = base64.b64encode(buf.getvalue()).decode()
    
    payload = json.dumps({
        "model": "default",
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            {"type": "text", "text": VLM_PROMPT}
        ]}],
        "max_tokens": 1500,
        "temperature": 0.1,
    }).encode()
    
    req = urllib.request.Request(f"{vlm_url}/v1/chat/completions", data=payload,
                                 headers={"Content-Type": "application/json"})
    no_proxy = urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(no_proxy)
    with opener.open(req, timeout=timeout) as resp:
        r = json.loads(resp.read())
        return r["choices"][0]["message"]["content"]


# ============================================================
# VLM 输出解析
# ============================================================

def parse_vlm_output(text):
    """解析 VLM 输出，提取每张图的文字和描述"""
    results = []
    labels = ['A', 'B', 'C', 'D']
    
    # 按 [图X] 分割
    parts = re.split(r'\[图([ABCD])\]', text)
    
    i = 1  # 跳过 parts[0]（标题前的内容）
    while i < len(parts) - 1:
        label = parts[i]
        content = parts[i + 1].strip()
        i += 2
        
        # 提取文字和描述
        text_match = re.search(r'文字[：:]\s*(.*?)(?=描述[：:]|$)', content, re.DOTALL)
        desc_match = re.search(r'描述[：:]\s*(.*?)(?=\[图|$)', content, re.DOTALL)
        
        extracted_text = text_match.group(1).strip() if text_match else ""
        description = desc_match.group(1).strip() if desc_match else ""
        
        # 清理 markdown 格式
        extracted_text = re.sub(r'\*\*|\*|- ', '', extracted_text)
        description = re.sub(r'\*\*|\*|- ', '', description)
        
        # 分类
        cls = classify_with_detail(extracted_text)
        
        results.append({
            "quadrant": label,
            "extracted_text": extracted_text,
            "description": description,
            "type": cls["type"],
            "matched_rule": cls["matched"],
        })
    
    return results


# ============================================================
# 主流程
# ============================================================

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    deduped_dir = Path(sys.argv[1])
    vlm_url = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8081"
    
    if len(sys.argv) > 2:
        output_dir = Path(sys.argv[2])
    else:
        output_dir = deduped_dir.parent / "vlm_results"
    
    grids_dir = output_dir / "grids"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    grids_dir.mkdir(parents=True)
    
    print(f"🔧 VLM 流水线")
    print(f"   输入: {deduped_dir}")
    print(f"   输出: {output_dir}")
    print(f"   VLM:  {vlm_url}\n")
    
    # 1. 收集帧
    frames = []
    for fname in sorted(os.listdir(deduped_dir)):
        if not fname.endswith(('.jpg', '.png')):
            continue
        frames.append({
            'file': fname,
            'path': deduped_dir / fname,
            'timestamp': get_ts(fname),
        })
    frames.sort(key=lambda f: f['timestamp'])
    print(f"📄 去重帧: {len(frames)}")
    
    # 2. 分组拼图
    grids = []
    for i in range(0, len(frames), 4):
        group = frames[i:i+4]
        grid_img = stitch_grid([f['path'] for f in group])
        grid_name = f"grid_{len(grids)+1:03d}.jpg"
        grid_path = grids_dir / grid_name
        grid_img.save(grid_path, quality=90)
        grids.append({
            'name': grid_name,
            'path': grid_path,
            'frames': [f['file'] for f in group],
            'timestamps': [f['timestamp'] for f in group],
        })
    print(f"🖼️  网格: {len(grids)}")
    
    # 3. 检查 VLM
    try:
        no_proxy = urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(no_proxy)
        req = urllib.request.Request(f"{vlm_url}/v1/models")
        opener.open(req, timeout=5)
        print(f"🔌 VLM: ✓\n")
    except Exception as e:
        print(f"❌ VLM 不可用: {e}")
        sys.exit(1)
    
    # 4. VLM 分析
    all_results = []
    ok = fail = 0
    
    for gi, grid in enumerate(grids):
        print(f"[{gi+1}/{len(grids)}] {grid['name']}...", end=" ", flush=True)
        
        try:
            text = call_vlm(grid['path'], vlm_url)
            parsed = parse_vlm_output(text)
            
            # 关联帧文件名
            for j, r in enumerate(parsed):
                if j < len(grid['frames']):
                    r['frame_file'] = grid['frames'][j]
                    r['timestamp'] = grid['timestamps'][j]
                r['grid'] = grid['name']
            
            all_results.extend(parsed)
            
            types = [r['type'] for r in parsed]
            print(f"✅ {types}")
            ok += 1
            
        except Exception as e:
            print(f"❌ {str(e)[:60]}")
            fail += 1
        
        time.sleep(0.5)
    
    # 5. 保存结果
    with open(output_dir / "vlm_raw.json", 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    
    with open(output_dir / "classified.json", 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)
    
    # 6. 统计
    from collections import Counter
    types = Counter(r['type'] for r in all_results)
    
    summary = {
        'total_frames': len(frames),
        'total_grids': len(grids),
        'vlm_ok': ok,
        'vlm_fail': fail,
        'analyzed_frames': len(all_results),
        'type_distribution': dict(types.most_common()),
    }
    
    with open(output_dir / "summary.json", 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    
    print(f"\n=== 结果 ===")
    print(f"分析帧数: {len(all_results)}")
    print(f"成功/失败: {ok}/{fail}")
    print(f"\n标签分布:")
    for tag, count in types.most_common():
        print(f"  {tag}: {count}")
    print(f"\n输出: {output_dir}")

if __name__ == '__main__':
    main()
