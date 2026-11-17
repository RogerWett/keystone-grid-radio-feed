#!/usr/bin/env python3
"""Generate the Keystone Grid Radio square cover (3000px)."""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

OUT_DIR = Path(__file__).resolve().parent / "public"
SIZE = 3000
NAVY2 = (11, 32, 58)
NAVY3 = (6, 12, 24)
GOLD = (212, 168, 72)
WIRE = (230, 196, 110)
TOWER = (160, 190, 220)
TOWER_DIM = (70, 100, 130)
WHITE = (242, 244, 247)
MUTED = (170, 186, 204)

FONT_BOLD = (
    "/usr/share/fonts/truetype/sand-box/google/Barlow Condensed/BarlowCondensed-Bold.ttf"
)
FONT_REG = (
    "/usr/share/fonts/truetype/sand-box/google/Barlow Condensed/BarlowCondensed-Regular.ttf"
)


def _gradient_base() -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), NAVY3)
    px = img.load()
    cx, cy = SIZE * 0.5, SIZE * 0.42
    for y in range(SIZE):
        t = y / (SIZE - 1)
        r0 = int(NAVY3[0] + (NAVY2[0] - NAVY3[0]) * t)
        g0 = int(NAVY3[1] + (NAVY2[1] - NAVY3[1]) * t)
        b0 = int(NAVY3[2] + (NAVY2[2] - NAVY3[2]) * t)
        for x in range(SIZE):
            r = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5 / SIZE
            lift = max(0.0, 1.0 - r * 1.35) * 0.22
            px[x, y] = (
                min(255, int(r0 + 18 * lift)),
                min(255, int(g0 + 28 * lift)),
                min(255, int(b0 + 40 * lift)),
            )
    return img


def _tower(draw, base_x, base_y, height, width_bot, width_top, color, width=7):
    top_y = base_y - height
    tl = (base_x - width_top / 2, top_y)
    tr = (base_x + width_top / 2, top_y)
    steps = 9
    pts_l, pts_r = [], []
    for i in range(steps + 1):
        t = i / steps
        xspan = width_bot / 2 * (1 - t) + width_top / 2 * t
        y = base_y - height * t
        pts_l.append((base_x - xspan, y))
        pts_r.append((base_x + xspan, y))
        draw.line([(base_x - xspan, y), (base_x + xspan, y)], fill=color, width=max(3, width - 3))
    draw.line([pts_l[0], pts_l[-1]], fill=color, width=width)
    draw.line([pts_r[0], pts_r[-1]], fill=color, width=width)
    for i in range(steps):
        draw.line([pts_l[i], pts_r[i + 1]], fill=color, width=max(3, width - 4))
        draw.line([pts_r[i], pts_l[i + 1]], fill=color, width=max(3, width - 4))
    arm_len = width_top * 2.4
    insulators = []
    for ayf in (0.78, 0.88, 0.97):
        y = base_y - height * ayf
        draw.line([(base_x - arm_len, y), (base_x + arm_len, y)], fill=color, width=width)
        for sx in (-arm_len * 0.82, arm_len * 0.82):
            ix = base_x + sx
            draw.line([(ix, y), (ix, y + 38)], fill=color, width=4)
            insulators.append((ix, y + 44))
    peak = (base_x, top_y - height * 0.06)
    draw.line([tl, peak, tr], fill=color, width=width)
    return insulators


def _wire(draw, p1, p2, sag, color, width=6):
    x1, y1 = p1
    x2, y2 = p2
    pts = []
    for i in range(41):
        t = i / 40
        x = x1 + (x2 - x1) * t
        y = y1 + (y2 - y1) * t + sag * 4 * t * (1 - t)
        pts.append((x, y))
    draw.line(pts, fill=color, width=width, joint="curve")


def _center_text(draw, text, font, y, fill, tracking=0):
    if tracking == 0:
        bbox = draw.textbbox((0, 0), text, font=font)
        w = bbox[2] - bbox[0]
        x = (SIZE - w) / 2
        draw.text((x + 4, y + 6), text, font=font, fill=(0, 0, 0, 160))
        draw.text((x, y), text, font=font, fill=fill)
        return
    widths = []
    for ch in text:
        bbox = draw.textbbox((0, 0), ch, font=font)
        widths.append(bbox[2] - bbox[0])
    total = sum(widths) + tracking * (len(text) - 1)
    x = (SIZE - total) / 2
    xx = x
    for ch, w in zip(text, widths):
        draw.text((xx + 3, y + 5), ch, font=font, fill=(0, 0, 0, 150))
        xx += w + tracking
    xx = x
    for ch, w in zip(text, widths):
        draw.text((xx, y), ch, font=font, fill=fill)
        xx += w + tracking


def generate(out_dir: Path = OUT_DIR) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    img = _gradient_base()
    overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    step = 75
    for i in range(0, SIZE + 1, step):
        od.line([(i, 0), (i, SIZE)], fill=(120, 160, 200, 18), width=2)
        od.line([(0, i), (SIZE, i)], fill=(120, 160, 200, 18), width=2)
    for i in range(0, SIZE + 1, step * 4):
        od.line([(i, 0), (i, SIZE)], fill=(120, 160, 200, 28), width=2)
        od.line([(0, i), (SIZE, i)], fill=(120, 160, 200, 28), width=2)
    img = Image.alpha_composite(img.convert("RGBA"), overlay)

    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    left_ins = _tower(d, 520, 2920, 1680, 420, 110, (*TOWER_DIM, 230), width=8)
    right_ins = _tower(d, 2480, 2780, 1520, 380, 100, (*TOWER, 210), width=8)
    pairs = [
        (left_ins[1], right_ins[0], 220),
        (left_ins[3], right_ins[2], 260),
        (left_ins[5], right_ins[4], 300),
    ]
    for a, b, sag in pairs:
        _wire(d, a, b, sag, (*WIRE, 230), width=7)
        d.ellipse([a[0] - 8, a[1] - 8, a[0] + 8, a[1] + 8], fill=(*GOLD, 255))
        d.ellipse([b[0] - 8, b[1] - 8, b[0] + 8, b[1] + 8], fill=(*GOLD, 255))
    img = Image.alpha_composite(img, layer.filter(ImageFilter.GaussianBlur(radius=0.6)))

    draw = ImageDraw.Draw(img)
    title_font = ImageFont.truetype(FONT_BOLD, 420)
    sub_font = ImageFont.truetype(FONT_REG, 92)
    title_y = 860
    draw.rectangle([1180, title_y - 70, 1820, title_y - 62], fill=(*GOLD, 230))
    _center_text(draw, "KEYSTONE", title_font, title_y, WHITE, tracking=18)
    _center_text(draw, "GRID RADIO", title_font, title_y + 390, GOLD, tracking=22)
    draw.rectangle([1000, title_y + 860, 2000, title_y + 868], fill=(*GOLD, 230))
    _center_text(
        draw,
        "THE MID-ATLANTIC GRID BRIEFING",
        sub_font,
        title_y + 900,
        MUTED,
        tracking=16,
    )

    img = img.convert("RGB")
    png = out_dir / "cover.png"
    jpg = out_dir / "cover.jpg"
    img.save(png, "PNG", optimize=True)
    img.save(jpg, "JPEG", quality=92, subsampling=0, optimize=True)
    return png


if __name__ == "__main__":
    path = generate()
    print(path)
