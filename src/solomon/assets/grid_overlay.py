#!/usr/bin/env python3
"""
grid_overlay.py — Generate a 10×10 grid overlay on an image for Set-of-Mark vision positioning.

Usage:
    python3 grid_overlay.py input.jpg [output.jpg]

If no output is specified, appends '_grid' to the input filename.

Dependencies:
    - Pillow (pip install Pillow)
    - fonts-noto-cjk (for clear label rendering on CJK systems)
      sudo apt install fonts-noto-cjk

Utility functions:
    - grid_to_pixel(label, w, h) → (x, y) center of cell
    - detect_target_grid(label, w, h) → (x1, y1, x2, y2) bounding box
"""

import sys
import os
import re
import math
import json

from PIL import Image, ImageDraw, ImageFont


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
COL_LABELS = "ABCDEFGHIJ"
ROW_LABELS = [str(i) for i in range(1, 11)]

DEFAULT_FONT_SIZE = 20  # will be scaled to image size


# ---------------------------------------------------------------------------
# Font helper
# ---------------------------------------------------------------------------
def _get_font(size: int) -> ImageFont.FreeTypeFont:
    """Try to load a monospace / sans font; fall back to Pillow default."""
    candidates = [
        # Noto Sans CJK (common on Linux with CJK support)
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        # DejaVu (common Linux default)
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        # Liberation
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            return ImageFont.truetype(path, size)
    # Ultimate fallback — Pillow built-in
    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Brightness detection
# ---------------------------------------------------------------------------
def _avg_brightness(img: Image.Image) -> float:
    """Return average brightness of the image (0-255 scale)."""
    gray = img.convert("L")
    hist = gray.histogram()
    total_pixels = sum(hist)
    if total_pixels == 0:
        return 128.0
    weighted_sum = sum(i * hist[i] for i in range(256))
    return weighted_sum / total_pixels


# ---------------------------------------------------------------------------
# Grid conversion utilities
# ---------------------------------------------------------------------------

def _parse_grid_label(label: str):
    """
    Parse a grid label like 'G-4' or 'G4' → (col_idx, row_idx), both 0-based.
    Col letter(s) → A=0, B=1, ..., J=9.
    Row number → 1=0, 2=1, ..., 10=9.
    """
    label = label.strip().upper().replace("-", "")
    m = re.match(r"([A-J])(\d{1,2})", label)
    if not m:
        raise ValueError(f"Invalid grid label: {label!r}. Expected format like 'G-4' or 'H10'.")
    col_char = m.group(1)
    row_num = int(m.group(2))
    col_idx = COL_LABELS.index(col_char)
    row_idx = row_num - 1
    if row_idx < 0 or row_idx >= 10:
        raise ValueError(f"Row number must be 1-10, got {row_num}")
    return col_idx, row_idx


def _parse_grid_range(label: str):
    """
    Parse a grid label or range like 'G-4' or 'G4-H5'.
    Returns list of (col_idx, row_idx) tuples (single cell or rectangular area).
    """
    label = label.strip().upper()
    # Check for range
    range_match = re.match(r"([A-J]\d{1,2})-([A-J]\d{1,2})", label.replace("-", ""))
    if not range_match:
        range_match = re.match(r"([A-J]\d{1,2})\s*[-–—]\s*([A-J]\d{1,2})", label)
    if range_match:
        c1, r1 = _parse_grid_label(range_match.group(1))
        c2, r2 = _parse_grid_label(range_match.group(2))
        cells = []
        for r in range(min(r1, r2), max(r1, r2) + 1):
            for c in range(min(c1, c2), max(c1, c2) + 1):
                cells.append((c, r))
        return cells
    else:
        return [_parse_grid_label(label)]


def grid_to_pixel(grid_label: str, image_width: int, image_height: int,
                  num_cols: int = 10, num_rows: int = 10):
    """
    Convert a grid label (or range) to pixel center coordinates (x, y).

    For a single cell like 'G-4', returns (x, y) of the cell center.
    For a range like 'G4-H5', returns the center of the bounding rectangle.

    Coordinates assume the grid starts at the top-left of the image
    (after the label margin, which is ~30px for the row/col labels).
    """
    cells = _parse_grid_range(grid_label)
    # Compute image region available for the grid (after label margins)
    margin_x, margin_y = _label_margins(image_width, image_height)
    gw = image_width - margin_x
    gh = image_height - margin_y
    cell_w = gw / num_cols
    cell_h = gh / num_rows

    xs = []
    ys = []
    for c, r in cells:
        cx = margin_x + c * cell_w + cell_w / 2
        cy = margin_y + r * cell_h + cell_h / 2
        xs.append(cx)
        ys.append(cy)

    center_x = (min(xs) + max(xs)) / 2
    center_y = (min(ys) + max(ys)) / 2
    return (center_x, center_y)


def detect_target_grid(grid_label: str, image_width: int, image_height: int,
                       num_cols: int = 10, num_rows: int = 10):
    """
    Get the pixel bounding box (x1, y1, x2, y2) for a grid cell or range.

    For a single cell, returns the cell's bounding box.
    For a range, returns the bounding box encompassing all cells.
    """
    cells = _parse_grid_range(grid_label)
    margin_x, margin_y = _label_margins(image_width, image_height)
    gw = image_width - margin_x
    gh = image_height - margin_y
    cell_w = gw / num_cols
    cell_h = gh / num_rows

    x1s, y1s, x2s, y2s = [], [], [], []
    for c, r in cells:
        x1s.append(margin_x + c * cell_w)
        y1s.append(margin_y + r * cell_h)
        x2s.append(margin_x + (c + 1) * cell_w)
        y2s.append(margin_y + (r + 1) * cell_h)

    return (min(x1s), min(y1s), max(x2s), max(y2s))


def _label_margins(image_width: int, image_height: int):
    """Return (margin_x, margin_y) used for row/col label space."""
    # Scale margins relative to image size
    margin_x = max(30, int(image_width * 0.035))
    margin_y = max(30, int(image_height * 0.035))
    return margin_x, margin_y


# ---------------------------------------------------------------------------
# Grid overlay drawing
# ---------------------------------------------------------------------------

def add_grid_overlay(input_path: str, output_path: str = None,
                     num_cols: int = 10, num_rows: int = 10,
                     resize: bool = True) -> dict:
    """
    Add a semi-transparent 10×10 grid overlay to the image.

    Args:
        input_path: Path to the source image.
        output_path: Path for the output image. If None, appends '_grid'.
        num_cols: Number of grid columns.
        num_rows: Number of grid rows.
        resize: If True, resize output to fit within 1024px (longest edge).

    Returns:
        Dict with keys: output_path, scale_ratio, original_size, grid_size.
    """
    img = Image.open(input_path).convert("RGBA")
    w, h = img.size

    # Determine grid line color based on image brightness
    brightness = _avg_brightness(img)
    if brightness > 128:
        # Light image → dark grid lines
        line_color = (40, 40, 40, 80)
        text_color = (20, 20, 20, 220)
        label_bg = (255, 255, 255, 200)
    else:
        # Dark image → light grid lines
        line_color = (220, 220, 220, 80)
        text_color = (240, 240, 240, 220)
        label_bg = (30, 30, 30, 200)

    # Create overlay layer
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    margin_x, margin_y = _label_margins(w, h)
    gw = w - margin_x
    gh = h - margin_y
    cell_w = gw / num_cols
    cell_h = gh / num_rows

    # Font size scales with image size
    font_size = max(14, int(min(cell_w, cell_h) * 0.35))
    font = _get_font(font_size)

    # --- Draw grid lines ---
    # Vertical lines
    for i in range(num_cols + 1):
        x = margin_x + i * cell_w
        draw.line([(x, margin_y), (x, margin_y + gh)], fill=line_color, width=2)
    # Horizontal lines
    for i in range(num_rows + 1):
        y = margin_y + i * cell_h
        draw.line([(margin_x, y), (margin_x + gw, y)], fill=line_color, width=2)

    # --- Draw column labels (A-J) across the top ---
    for i, label in enumerate(COL_LABELS[:num_cols]):
        x = margin_x + i * cell_w + cell_w / 2
        y = margin_y / 2
        _draw_label(draw, label, x, y, font, text_color, label_bg)

    # --- Draw row labels (1-10) down the left side ---
    for i, label in enumerate(ROW_LABELS[:num_rows]):
        x = margin_x / 2
        y = margin_y + i * cell_h + cell_h / 2
        _draw_label(draw, label, x, y, font, text_color, label_bg)

    # Composite overlay onto original
    result = Image.alpha_composite(img, overlay)

    # Determine output path
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_grid{ext}"

    # Save as PNG to preserve transparency, or as JPEG if input was JPEG
    scale_ratio = 1.0
    original_size = [w, h]

    if output_path.lower().endswith((".jpg", ".jpeg")):
        result = result.convert("RGB")

    # --- Resize to fit within 1024px longest edge ---
    if resize:
        grid_w, grid_h = result.size
        longest = max(grid_w, grid_h)
        if longest > 1024:
            scale_ratio = 1024.0 / longest
            new_w = int(grid_w * scale_ratio)
            new_h = int(grid_h * scale_ratio)
            result = result.resize((new_w, new_h), Image.LANCZOS)

    grid_size = list(result.size)
    result.save(output_path)

    # --- Write JSON sidecar ---
    sidecar = {
        "scale_ratio": scale_ratio,
        "original_size": original_size,
        "grid_size": grid_size,
    }
    json_path = os.path.splitext(output_path)[0] + ".json"
    with open(json_path, "w") as f:
        json.dump(sidecar, f, indent=2)

    return {"output_path": output_path, **sidecar}


def _draw_label(draw: ImageDraw.ImageDraw, text: str,
                x: float, y: float, font: ImageFont.FreeTypeFont,
                text_color: tuple, bg_color: tuple):
    """Draw a label with a semi-transparent background rectangle, centered at (x, y)."""
    bbox = draw.textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad = 4
    rect_x1 = x - tw / 2 - pad
    rect_y1 = y - th / 2 - pad
    rect_x2 = x + tw / 2 + pad
    rect_y2 = y + th / 2 + pad
    # Background rectangle
    draw.rounded_rectangle(
        [rect_x1, rect_y1, rect_x2, rect_y2],
        radius=3,
        fill=bg_color,
    )
    # Text centered
    draw.text((x - tw / 2, y - th / 2), text, fill=text_color, font=font)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print("Usage: python3 grid_overlay.py input.jpg [output.jpg] [--no-resize]")
        print()
        print("Generates a 10x10 Set-of-Mark grid overlay on the image.")
        print("Labels: columns A-J, rows 1-10.")
        print()
        print("Options:")
        print("  --cols N  Number of columns (default: 10)")
        print("  --rows N  Number of rows (default: 10)")
        print("  --no-resize  Skip resizing to 1024px (debugging)")
        sys.exit(1)

    # Parse arguments
    input_path = sys.argv[1]
    output_path = None
    num_cols = 10
    num_rows = 10
    resize = True

    args = sys.argv[2:]
    i = 0
    while i < len(args):
        if args[i] == "--cols" and i + 1 < len(args):
            num_cols = int(args[i + 1])
            i += 2
        elif args[i] == "--rows" and i + 1 < len(args):
            num_rows = int(args[i + 1])
            i += 2
        elif args[i] == "--no-resize":
            resize = False
            i += 1
        else:
            output_path = args[i]
            i += 1

    if not os.path.isfile(input_path):
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    result = add_grid_overlay(input_path, output_path, num_cols, num_rows, resize=resize)
    print(f"Grid overlay saved to: {result['output_path']}")
    print(f"Scale ratio: {result['scale_ratio']}")
    print(f"Original size: {result['original_size']}")
    print(f"Grid size: {result['grid_size']}")
    print(f"Sidecar JSON: {os.path.splitext(result['output_path'])[0]}.json")


if __name__ == "__main__":
    main()
