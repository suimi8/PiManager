# -*- coding: utf-8 -*-
"""Generate Pi Manager brand logos (PNG + SVG)."""
from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(r"C:\Users\suimi\Documents\Codex\2026-07-11\new-chat\work\pi-manager\assets")
OUT.mkdir(parents=True, exist_ok=True)

# Brand colors (aligned with UI night + blue accent)
BG_DARK = (13, 17, 23, 255)
BG_CARD = (22, 27, 34, 255)
PRIMARY = (31, 111, 235, 255)
PRIMARY_LIGHT = (56, 139, 253, 255)
CYAN = (57, 197, 207, 255)
GREEN = (46, 160, 67, 255)
WHITE = (255, 255, 255, 255)
SOFT = (230, 237, 243, 255)
MUTED = (139, 148, 158, 255)


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        r"C:\Windows\Fonts\segoeuib.ttf" if bold else r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if bold else r"C:\Windows\Fonts\arial.ttf",
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\calibrib.ttf" if bold else r"C:\Windows\Fonts\calibri.ttf",
    ]
    for p in candidates:
        try:
            return ImageFont.truetype(p, size=size)
        except OSError:
            continue
    return ImageFont.load_default()


def _rounded_rect(draw: ImageDraw.ImageDraw, box, radius: int, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def _gradient_circle(size: int, c1, c2) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    cx = cy = size / 2
    r = size / 2
    for y in range(size):
        for x in range(size):
            dx, dy = x - cx + 0.5, y - cy + 0.5
            d = math.sqrt(dx * dx + dy * dy)
            if d <= r:
                t = d / r
                # subtle radial blend
                col = tuple(int(c1[i] * (1 - t * 0.55) + c2[i] * (t * 0.55)) for i in range(3)) + (255,)
                # soft edge
                if d > r - 1.5:
                    a = int(255 * max(0, (r - d) / 1.5))
                    col = col[:3] + (a,)
                px[x, y] = col
    return img


def draw_mark(size: int = 1024, *, transparent: bool = False) -> Image.Image:
    """App mark: rounded tile + pi glyph + switch/orbit accents."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0) if transparent else BG_DARK)
    draw = ImageDraw.Draw(img)

    pad = int(size * 0.08)
    radius = int(size * 0.22)
    # outer rounded square
    if transparent:
        # soft shadow
        shadow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shadow)
        _rounded_rect(sd, (pad + 8, pad + 12, size - pad + 8, size - pad + 12), radius, (0, 0, 0, 60))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=int(size * 0.03)))
        img = Image.alpha_composite(img, shadow)
        draw = ImageDraw.Draw(img)

    # gradient-ish tile via base + overlay
    tile = (pad, pad, size - pad, size - pad)
    _rounded_rect(draw, tile, radius, BG_CARD if transparent else (18, 24, 33, 255))

    # inner glow ring
    inset = int(size * 0.14)
    ring_box = (inset, inset, size - inset, size - inset)
    # outer ring
    draw.ellipse(ring_box, outline=PRIMARY[:3] + (220,), width=max(4, size // 64))
    # accent arc (suggest multi-provider switch)
    # draw partial arcs with thick strokes
    arc_w = max(6, size // 48)
    draw.arc(ring_box, start=-40, end=80, fill=PRIMARY_LIGHT, width=arc_w)
    draw.arc(ring_box, start=140, end=220, fill=CYAN, width=arc_w)
    draw.arc(ring_box, start=250, end=310, fill=GREEN, width=max(4, arc_w - 2))

    # center disc
    cpad = int(size * 0.28)
    disc = _gradient_circle(size - 2 * cpad, PRIMARY, (15, 50, 120, 255))
    img.paste(disc, (cpad, cpad), disc)
    draw = ImageDraw.Draw(img)

    # Pi text
    font = _font(int(size * 0.34), bold=True)
    text = "π"
    # fallback if font lacks pi well - also draw "Pi"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    # center slightly optical
    x = (size - tw) / 2 - bbox[0]
    y = (size - th) / 2 - bbox[1] - size * 0.02
    # subtle text shadow
    draw.text((x + size * 0.008, y + size * 0.01), text, font=font, fill=(0, 0, 0, 80))
    draw.text((x, y), text, font=font, fill=WHITE)

    # small switch badge bottom-right inside tile (skip on tiny sizes)
    if size >= 64:
        bx = size - pad - max(int(size * 0.28), 18)
        by = size - pad - max(int(size * 0.18), 12)
        bw, bh = max(int(size * 0.18), 14), max(int(size * 0.09), 8)
        _rounded_rect(draw, (bx, by, bx + bw, by + bh), max(bh // 2, 2), PRIMARY)
        kr = max(bh // 2 - 2, 2)
        kx = bx + bw - kr - max(2, size // 128)
        ky = by + bh // 2
        draw.ellipse((kx - kr, ky - kr, kx + kr, ky + kr), fill=WHITE)

    return img


def draw_wordmark(width: int = 1600, height: int = 480, *, dark: bool = True) -> Image.Image:
    bg = BG_DARK if dark else (246, 248, 250, 255)
    fg = WHITE if dark else (31, 35, 40, 255)
    sub = MUTED if dark else (87, 96, 106, 255)
    img = Image.new("RGBA", (width, height), bg)
    mark = draw_mark(height - 40, transparent=True)
    img.paste(mark, (20, 20), mark)

    draw = ImageDraw.Draw(img)
    title_font = _font(int(height * 0.28), bold=True)
    sub_font = _font(int(height * 0.11), bold=False)
    tx = height + 10
    ty = int(height * 0.28)
    draw.text((tx, ty), "Pi Manager", font=title_font, fill=fg)
    draw.text(
        (tx, ty + int(height * 0.32)),
        "Cross-platform · Multi-provider · Official Pi",
        font=sub_font,
        fill=sub,
    )
    return img


def draw_favicon(size: int = 256) -> Image.Image:
    return draw_mark(size, transparent=True)


def write_svg(path: Path):
    svg = '''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="1024" viewBox="0 0 1024 1024" role="img" aria-label="Pi Manager">
  <defs>
    <linearGradient id="disc" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#388bfd"/>
      <stop offset="100%" stop-color="#1f6feb"/>
    </linearGradient>
    <linearGradient id="tile" x1="0%" y1="0%" x2="0%" y2="100%">
      <stop offset="0%" stop-color="#1a2230"/>
      <stop offset="100%" stop-color="#121821"/>
    </linearGradient>
    <filter id="soft" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="12" stdDeviation="18" flood-color="#000" flood-opacity="0.35"/>
    </filter>
  </defs>
  <rect width="1024" height="1024" rx="220" fill="url(#tile)" filter="url(#soft)"/>
  <circle cx="512" cy="500" r="290" fill="none" stroke="#1f6feb" stroke-width="18" opacity="0.9"/>
  <path d="M220 500 A292 292 0 0 1 780 380" fill="none" stroke="#388bfd" stroke-width="22" stroke-linecap="round"/>
  <path d="M300 700 A292 292 0 0 1 220 500" fill="none" stroke="#39c5cf" stroke-width="18" stroke-linecap="round"/>
  <path d="M700 740 A292 292 0 0 1 820 560" fill="none" stroke="#2ea043" stroke-width="14" stroke-linecap="round"/>
  <circle cx="512" cy="500" r="210" fill="url(#disc)"/>
  <text x="512" y="560" text-anchor="middle" font-family="Segoe UI, Arial, sans-serif" font-size="320" font-weight="700" fill="#ffffff">π</text>
  <!-- switch badge -->
  <rect x="680" y="780" width="180" height="90" rx="45" fill="#1f6feb"/>
  <circle cx="815" cy="825" r="32" fill="#ffffff"/>
</svg>
'''
    path.write_text(svg, encoding="utf-8")


def main():
    files = []
    # App icons
    for size, name in [(1024, "logo-1024.png"), (512, "logo-512.png"), (256, "logo-256.png"), (128, "logo-128.png")]:
        im = draw_mark(size, transparent=True)
        p = OUT / name
        im.save(p, "PNG")
        files.append(p)

    # Solid dark square for store / splash
    solid = draw_mark(1024, transparent=False)
    p = OUT / "logo-solid-1024.png"
    solid.save(p, "PNG")
    files.append(p)

    # Wordmarks
    for dark, name in [(True, "logo-wordmark-dark.png"), (False, "logo-wordmark-light.png")]:
        wm = draw_wordmark(1600, 480, dark=dark)
        p = OUT / name
        wm.save(p, "PNG")
        files.append(p)

    # ICO multi-size
    ico_imgs = [draw_mark(s, transparent=True) for s in (256, 128, 64, 48, 32, 16)]
    p = OUT / "pi-manager.ico"
    ico_imgs[0].save(p, format="ICO", sizes=[(im.width, im.height) for im in ico_imgs])
    files.append(p)

    svg_path = OUT / "logo.svg"
    write_svg(svg_path)
    files.append(svg_path)

    # Also copy main icon to package-friendly names
    draw_mark(256, transparent=True).save(OUT / "icon.png", "PNG")
    files.append(OUT / "icon.png")

    print("Wrote:")
    for f in files:
        print(" ", f, f.stat().st_size if f.exists() else 0)


if __name__ == "__main__":
    main()
