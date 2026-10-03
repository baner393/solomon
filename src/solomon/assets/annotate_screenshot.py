#!/usr/bin/env python3
"""
Annotate screenshots with boxes, arrows, text labels, and zoom crops
for tutorial-style documentation.

Supports Set-of-Mark grid coordinates (e.g. G-4) via grid_overlay module.
Handles scaled grid images via JSON sidecar with scale_ratio.

Usage:
    python3 annotate_screenshot.py config.json
    python3 annotate_screenshot.py --image frame.jpg --output-dir out/ \
        --box G-4:"① 点击设置" --arrow A-1:G-4 --zoom G-4
    python3 annotate_screenshot.py --image frame.jpg --output-dir out/ \
        --fallback --box G-4:"① 点击设置"

Input JSON format:
    {
      "image": "path/to/frame.jpg",
      "output_dir": "assets/annotated/",
      "output_name": "step1",
      "annotations": [
        {"type": "box", "grid": "G-4", "label": "① 点击设置", "color": "red"},
        {"type": "arrow", "from_grid": "A-1", "to_grid": "G-4"},
        {"type": "text_label", "grid": "G-4", "text": "注意这里", "bg_color": "red"},
        {"type": "zoom", "grid": "G-4", "padding_ratio": 2.0, "scale": 2}
      ]
    }

Output JSON:
    {"full": "annotated_full.jpg", "zoom": "annotated_zoom.jpg" (or null)}
"""

import json
import sys
import os
import math
import shutil
import glob

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("ERROR: Pillow not installed. Run: pip install Pillow", file=sys.stderr)
    sys.exit(1)

# Import grid coordinate functions from grid_overlay module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from grid_overlay import grid_to_pixel, detect_target_grid as grid_region_to_pixels

COLS = 10
ROWS = 10

FONT_PATHS = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]

ZOOM_THRESHOLD_RATIO = 1 / 16  # auto-zoom if region < 1/16 of image area


def find_font(size=24):
    """Find a suitable font with CJK fallback chain."""
    for fp in FONT_PATHS:
        if os.path.exists(fp):
            try:
                return ImageFont.truetype(fp, size)
            except Exception:
                continue
    return ImageFont.load_default()


def _draw_with_stroke(draw_func, outline, stroke_width=2, stroke_keyword='outline'):
    """Helper: draw a shape with white stroke first, then colored outline.

    draw_func must be a callable(**kwargs) that draws the shape.
    stroke_keyword: 'outline' for rectangles, 'fill' for lines.
    """
    kw = {stroke_keyword: 'white', 'width': stroke_width + 2}
    draw_func(**kw)
    kw[stroke_keyword] = outline
    draw_func(**kw)


def draw_box(draw, x1, y1, x2, y2, color='red'):
    """Draw a rectangle with white stroke for visibility on any background."""
    _draw_with_stroke(
        lambda **kw: draw.rectangle([x1, y1, x2, y2], **kw),
        outline=color,
        stroke_width=2,
    )


def draw_arrow(draw, x1, y1, x2, y2, color='red', thickness=2):
    """Draw an arrow with white stroke from (x1,y1) to (x2,y2)."""
    # Arrow line with stroke (line uses 'fill' not 'outline')
    _draw_with_stroke(
        lambda **kw: draw.line([(x1, y1), (x2, y2)], **kw),
        outline=color,
        stroke_width=thickness,
        stroke_keyword='fill',
    )
    # Arrowhead triangle
    angle = math.atan2(y2 - y1, x2 - x1)
    arrow_len = 14
    arrow_angle = math.pi / 6

    ax1 = x2 - arrow_len * math.cos(angle - arrow_angle)
    ay1 = y2 - arrow_len * math.sin(angle - arrow_angle)
    ax2 = x2 - arrow_len * math.cos(angle + arrow_angle)
    ay2 = y2 - arrow_len * math.sin(angle + arrow_angle)

    _draw_with_stroke(
        lambda **kw: draw.polygon([(x2, y2), (ax1, ay1), (ax2, ay2)], **kw),
        outline=color,
        stroke_width=1,
    )


def draw_text_label(draw, x, y, text, font, bg_color='red'):
    """Draw a callout-style text label with colored background rectangle.

    Positioned above the point by default; shifts below if not enough space.
    The background has white outline for contrast.
    """
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad_x, pad_y = 6, 4

    lx = x - tw // 2
    ly = y - th - pad_y * 2 - 4  # above the point

    # Background rect
    rect = [lx - pad_x, ly - pad_y, lx + tw + pad_x, ly + th + pad_y]
    # White outline first
    draw.rectangle(
        [rect[0] - 1, rect[1] - 1, rect[2] + 1, rect[3] + 1],
        fill='white',
    )
    # Colored background
    draw.rectangle(rect, fill=bg_color)
    # Text
    draw.text((lx, ly), text, fill='white', font=font)


def draw_number_badge(draw, x, y, number, font):
    """Draw a circled sequence number (①②③) at the top-right of a box."""
    text = str(number)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    r = max(tw, th) // 2 + 5

    cx, cy = x + r + 2, y - r - 2
    # White circle with red outline
    draw.ellipse(
        [cx - r, cy - r, cx + r, cy + r],
        fill='white',
        outline='red',
        width=2,
    )
    draw.text((cx - tw // 2, cy - th // 2), text, fill='red', font=font)


# ---------------------------------------------------------------------------
# Grid JSON sidecar loading & coordinate scaling
# ---------------------------------------------------------------------------

def _find_grid_json_sidecar(grid_image_path):
    """
    Locate the JSON sidecar for a grid image.

    The grid_overlay module outputs JSON alongside the grid image with naming
    convention: <stem>_grid.json next to the grid image file.
    Also checks for the original image path if grid_image_path IS the grid image.

    Returns dict with keys like {scale_ratio, original_size, grid_size} or None.
    """
    if not grid_image_path or not os.path.exists(grid_image_path):
        return None

    # Try the image path itself if it's already a JSON
    if grid_image_path.lower().endswith('.json') and os.path.exists(grid_image_path):
        try:
            with open(grid_image_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    base, ext = os.path.splitext(grid_image_path)

    # Pattern 1: <stem>_grid.json  (next to the grid image)
    sidecar_candidates = [
        f"{base}_grid.json",
        f"{base}.json",
    ]

    # Also try replacing common grid suffixes
    # e.g. frame_grid.jpg -> frame_grid.json, frame.jpg -> frame.json
    # Also try one directory up or alongside
    for candidate in sidecar_candidates:
        if os.path.exists(candidate):
            try:
                with open(candidate, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if 'scale_ratio' in data:
                    return data
            except (json.JSONDecodeError, OSError):
                continue

    # Pattern 2: glob for *_grid.json in same directory
    dir_name = os.path.dirname(grid_image_path) or '.'
    stem = os.path.basename(base)  # e.g. "frame"
    for pattern in [f"*_grid.json", f"{stem}*.json"]:
        matches = glob.glob(os.path.join(dir_name, pattern))
        for match in matches:
            try:
                with open(match, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if 'scale_ratio' in data:
                    return data
            except (json.JSONDecodeError, OSError):
                continue

    return None


def _get_scale_ratio(image_path):
    """
    Determine the scale_ratio from a grid JSON sidecar.

    If the sidecar is found, returns (scale_ratio, grid_image_path) where
    grid_image_path is the scaled-down grid image.
    If not found, returns (1.0, None) as graceful fallback.
    """
    sidecar = _find_grid_json_sidecar(image_path)
    if sidecar and 'scale_ratio' in sidecar:
        scale = float(sidecar['scale_ratio'])
        grid_path = sidecar.get('grid_image_path', image_path)
        return scale, grid_path
    return 1.0, None


def _reverse_scale(coords, scale_ratio):
    """
    Reverse-scale pixel coordinates from grid-image space to original image space.

    Args:
        coords: (x, y) or (x1, y1, x2, y2)
        scale_ratio: the ratio used to downscale (e.g. 0.5 means grid image is half size)

    Returns:
        Scaled coordinates as integers in original image space.
    """
    if scale_ratio == 1.0:
        return coords
    factor = 1.0 / scale_ratio
    if len(coords) == 2:
        return (int(coords[0] * factor), int(coords[1] * factor))
    elif len(coords) == 4:
        return (
            int(coords[0] * factor), int(coords[1] * factor),
            int(coords[2] * factor), int(coords[3] * factor),
        )
    return coords


def _safe_grid_to_pixel(grid_str, img_width, img_height):
    """Wrapper around grid_to_pixel that returns (None, None) on error."""
    if not grid_str:
        return None, None
    try:
        result = grid_to_pixel(grid_str, img_width, img_height)
        return result
    except (ValueError, KeyError):
        return None, None


def _safe_grid_region(grid_str, img_width, img_height):
    """Wrapper around detect_target_grid that returns None on error."""
    if not grid_str:
        return None
    try:
        return grid_region_to_pixels(grid_str, img_width, img_height)
    except (ValueError, KeyError):
        return None


def annotate(config):
    """Main annotation function.

    Args:
        config: dict with keys:
            - image: str, path to source image (may be the grid image)
            - output_dir: str, directory for outputs (created if needed)
            - output_name: str, prefix for output filenames (default: 'annotated')
            - annotations: list of annotation dicts
            - fallback: int, fallback tier (0=normal, 1=crop-only, 2=original copy)

    Returns:
        {"full": "path_to_annotated_full", "zoom": "path_or_None"}
    """
    image_path = config['image']
    output_dir = config.get('output_dir', '.')
    output_name = config.get('output_name', 'annotated')
    annotations = config.get('annotations', [])
    fallback_tier = config.get('fallback', 0)

    os.makedirs(output_dir, exist_ok=True)

    # --- Load the ORIGINAL (full-resolution) image ---
    original_img = Image.open(image_path).convert('RGBA')
    orig_w, orig_h = original_img.size

    # --- Detect scale ratio from grid JSON sidecar ---
    scale_ratio, grid_image_path = _get_scale_ratio(image_path)
    if scale_ratio != 1.0:
        print(f"Detected grid sidecar: scale_ratio={scale_ratio}, "
              f"original_size={orig_w}x{orig_h}")

    # --- Three-tier fallback logic ---
    if fallback_tier == 2:
        # Priority 3: Original image copy, no changes
        orig_path = os.path.join(output_dir, f"{output_name}_original.jpg")
        original_img.convert('RGB').save(orig_path, quality=90)
        print(f"Fallback tier 2 (original): {orig_path}")
        return {"full": orig_path, "zoom": None}

    # --- Determine the image size for grid coordinate calculations ---
    # If scale_ratio != 1.0, grid coordinates are relative to the scaled grid image.
    # We compute grid coordinates against the grid image dimensions, then reverse-scale.
    if scale_ratio != 1.0 and grid_image_path and os.path.exists(grid_image_path):
        grid_img = Image.open(grid_image_path)
        grid_w, grid_h = grid_img.size
        grid_img.close()
    else:
        # No scaling — grid coordinates are in original image space
        grid_w, grid_h = orig_w, orig_h

    if fallback_tier == 1:
        # Priority 2: Crop-only mode — just zoom crop, no red box
        zoom_ann = None
        for ann in annotations:
            if ann.get('type') == 'zoom':
                zoom_ann = ann
                break
        # If no explicit zoom annotation, try the first annotation's grid
        if not zoom_ann and annotations:
            grid = annotations[0].get('grid', '')
            if grid:
                zoom_ann = {"type": "zoom", "grid": grid, "padding_ratio": 2.0}

        if zoom_ann:
            grid = zoom_ann.get('grid', '')
            region = _safe_grid_region(grid, grid_w, grid_h)
            if region:
                # Reverse-scale to original image coordinates
                rx1, ry1, rx2, ry2 = _reverse_scale(region, scale_ratio)
                rw, rh = rx2 - rx1, ry2 - ry1
                padding_ratio = zoom_ann.get('padding_ratio', 2.0)
                pw = int(rw * padding_ratio)
                ph = int(rh * padding_ratio)
                zx1 = max(0, rx1 - pw)
                zy1 = max(0, ry1 - ph)
                zx2 = min(orig_w, rx2 + pw)
                zy2 = min(orig_h, ry2 + ph)

                cropped = original_img.crop((zx1, zy1, zx2, zy2))
                zoom_path = os.path.join(output_dir, f"{output_name}_zoom.jpg")
                cropped.convert('RGB').save(zoom_path, quality=90)
                print(f"Fallback tier 1 (crop-only): {zoom_path}")
                return {"full": zoom_path, "zoom": None}

        # If no grid region found, fall through to tier 3
        orig_path = os.path.join(output_dir, f"{output_name}_original.jpg")
        original_img.convert('RGB').save(orig_path, quality=90)
        print(f"Fallback tier 2 (original): {orig_path}")
        return {"full": orig_path, "zoom": None}

    # --- Priority 1: Full annotation (normal mode) ---
    img = original_img
    w, h = orig_w, orig_h

    # Create transparent overlay for drawing at original resolution
    overlay = Image.new('RGBA', (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    font_label = find_font(max(16, int(min(w, h) / 30)))
    font_seq = find_font(max(20, int(min(w, h) / 25)))

    seq_num = 1
    zoom_region = None

    for ann in annotations:
        ann_type = ann.get('type', '')

        if ann_type == 'box':
            # --- Box annotation: red rectangle with optional label ---
            grid = ann.get('grid', '')
            color = ann.get('color', 'red')
            x, y = _safe_grid_to_pixel(grid, grid_w, grid_h)
            if x is None:
                continue

            # Reverse-scale to original image coordinates
            x, y = _reverse_scale((x, y), scale_ratio)

            # Compute cell size in original image space
            cell_w, cell_h = w // COLS, h // ROWS
            bx1, by1 = int(x) - cell_w // 2, int(y) - cell_h // 2
            bx2, by2 = int(x) + cell_w // 2, int(y) + cell_h // 2

            # Draw box with white stroke
            draw_box(draw, bx1, by1, bx2, by2, color)

            # Optional text label above the box
            if 'label' in ann:
                draw_text_label(draw, int(x), by1, ann['label'], font_label, color)

            # Sequence number badge
            draw_number_badge(draw, bx2, by1, seq_num, font_seq)
            seq_num += 1

        elif ann_type == 'arrow':
            # --- Arrow annotation: red arrow with white stroke ---
            from_grid = ann.get('from_grid', ann.get('from', ''))
            to_grid = ann.get('to_grid', ann.get('to', ''))
            color = ann.get('color', 'red')

            x1, y1 = _safe_grid_to_pixel(from_grid, grid_w, grid_h)
            x2, y2 = _safe_grid_to_pixel(to_grid, grid_w, grid_h)
            if x1 is not None and x2 is not None:
                # Reverse-scale to original image coordinates
                x1, y1 = _reverse_scale((x1, y1), scale_ratio)
                x2, y2 = _reverse_scale((x2, y2), scale_ratio)
                draw_arrow(draw, int(x1), int(y1), int(x2), int(y2), color)

        elif ann_type == 'text_label':
            # --- Text label callout: colored background + white text ---
            grid = ann.get('grid', '')
            text = ann.get('text', ann.get('label', ''))
            bg_color = ann.get('bg_color', ann.get('color', 'red'))

            x, y = _safe_grid_to_pixel(grid, grid_w, grid_h)
            if x is None or not text:
                continue
            # Reverse-scale to original image coordinates
            x, y = _reverse_scale((x, y), scale_ratio)
            draw_text_label(draw, int(x), int(y), text, font_label, bg_color)

        elif ann_type == 'zoom':
            # --- Zoom crop annotation ---
            # Store for post-processing (done after full annotation drawn)
            zoom_region = ann

    # Composite overlay onto original image
    result = Image.alpha_composite(img, overlay)
    full_path = os.path.join(output_dir, f"{output_name}_annotated.jpg")
    result.convert('RGB').save(full_path, quality=90)

    output = {"full": full_path, "zoom": None}

    # Generate zoom crop if requested
    if zoom_region:
        grid = zoom_region.get('grid', '')
        region = _safe_grid_region(grid, grid_w, grid_h)
        if region:
            # Reverse-scale to original image coordinates
            rx1, ry1, rx2, ry2 = _reverse_scale(region, scale_ratio)
            rw, rh = rx2 - rx1, ry2 - ry1
            area_ratio = (rw * rh) / (w * h)

            # Auto-zoom if region is small (< 1/16 of image) or forced
            if area_ratio < ZOOM_THRESHOLD_RATIO or zoom_region.get('force', False):
                padding_ratio = zoom_region.get('padding_ratio', 2.0)
                scale = zoom_region.get('scale', 2)

                # Expand region by padding_ratio
                pw = int(rw * padding_ratio)
                ph = int(rh * padding_ratio)
                zx1 = max(0, int(rx1) - pw)
                zy1 = max(0, int(ry1) - ph)
                zx2 = min(w, int(rx2) + pw)
                zy2 = min(h, int(ry2) + ph)

                # Crop and scale up
                cropped = img.crop((zx1, zy1, zx2, zy2))
                zoomed = cropped.resize(
                    (cropped.width * scale, cropped.height * scale),
                    Image.LANCZOS,
                )

                zoom_path = os.path.join(output_dir, f"{output_name}_zoom.jpg")
                zoomed.convert('RGB').save(zoom_path, quality=90)
                output['zoom'] = zoom_path

    print(f"Annotated: {output['full']}")
    if output['zoom']:
        print(f"Zoom: {output['zoom']}")

    return output


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 annotate_screenshot.py config.json")
        print("   or: python3 annotate_screenshot.py --image frame.jpg "
              "--output-dir out/ "
              "--box G-4:\"① 点击设置\" "
              "--arrow A-1:G-4 --zoom G-4")
        print()
        print("Options:")
        print("  --fallback    Enable fallback mode (0=normal, 1=crop-only, 2=original)")
        print("  --fallback-tier N  Set specific fallback tier (1=crop-only, 2=original)")
        sys.exit(1)

    if sys.argv[1] == '--image':
        # CLI mode
        import argparse
        parser = argparse.ArgumentParser(
            description='Annotate screenshots for tutorial documentation')
        parser.add_argument('--image', required=True, help='Source image path')
        parser.add_argument('--output-dir', default='.', help='Output directory')
        parser.add_argument('--output-name', default='annotated',
                            help='Output filename prefix')
        parser.add_argument('--box', action='append', nargs='?', const='',
                            default=[], help='Box: grid:label')
        parser.add_argument('--arrow', action='append', nargs='?', const='',
                            default=[], help='Arrow: from_grid:to_grid')
        parser.add_argument('--text-label', action='append', nargs='?',
                            const='', default=[], dest='text_label',
                            help='Text label: grid:text')
        parser.add_argument('--zoom', action='append', nargs='?', const='',
                            default=[], help='Zoom region: grid')
        parser.add_argument('--fallback', action='store_true',
                            help='Enable fallback mode (auto-degrade on failure)')
        parser.add_argument('--fallback-tier', type=int, default=0, choices=[0, 1, 2],
                            help='Force specific fallback tier: '
                                 '0=normal, 1=crop-only, 2=original copy')
        args = parser.parse_args()

        annotations = []
        for b in args.box:
            if ':' in b:
                grid, label = b.split(':', 1)
                annotations.append({
                    "type": "box", "grid": grid,
                    "label": label, "color": "red",
                })
        for a in args.arrow:
            if ':' in a:
                fr, to = a.split(':', 1)
                annotations.append({
                    "type": "arrow", "from_grid": fr, "to_grid": to,
                })
        for t in args.text_label:
            if ':' in t:
                grid, text = t.split(':', 1)
                annotations.append({
                    "type": "text_label", "grid": grid, "text": text,
                })
        for z in args.zoom:
            if z:
                annotations.append({"type": "zoom", "grid": z})

        # Determine fallback tier
        fallback_tier = args.fallback_tier
        if args.fallback and fallback_tier == 0:
            fallback_tier = 1  # default fallback tier is crop-only

        config = {
            "image": args.image,
            "output_dir": args.output_dir,
            "output_name": args.output_name,
            "annotations": annotations,
            "fallback": fallback_tier,
        }
    else:
        # JSON config mode
        with open(sys.argv[1], 'r', encoding='utf-8') as f:
            config = json.load(f)
        # Support fallback in JSON config too
        if 'fallback' not in config:
            config['fallback'] = 0

    annotate(config)


if __name__ == '__main__':
    main()
