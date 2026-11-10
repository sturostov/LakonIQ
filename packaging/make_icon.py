"""Логотип LakonIQ.

Знак: буква «Q» (вопрос) — белое кольцо, у которого хвост — искра ✦ (озарение, ответ).
Плитка — фирменный синий градиент интерфейса.

Результат:
  static/logo.svg       — знак на плитке с полями по сетке Apple (favicon)
  static/logo-tile.svg  — только плитка со знаком (экран входа)
  static/mark.svg       — белый знак без плитки (верхняя панель)
  build/icon.icns   — иконка приложения macOS
  build/icon_1024.png
"""
import math
import subprocess
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build"
STATIC = ROOT / "static"

# ---- геометрия (холст 1024×1024) ----
TILE = (100, 100, 924, 924)
TILE_R = 185
RING_C = (448, 448)
RING_R = 200
RING_W = 70
GAP = (12, 78)                # разрыв кольца справа снизу (градусы, по часовой от «3 часов»)
SPARK_C = (676, 676)
SPARK_R = 158
SPARK_P = 2.7                 # «вогнутость» лучей искры
COLORS = ("#86b0ff", "#3c83f6", "#2033dc")  # светлый → фирменный → глубокий синий


def sparkle_points(cx, cy, r, power=SPARK_P, n=240):
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        c, s = math.cos(a), math.sin(a)
        pts.append((cx + r * math.copysign(abs(c) ** power, c), cy + r * math.copysign(abs(s) ** power, s)))
    return pts


def ring_ends():
    a0, a1 = math.radians(GAP[1]), math.radians(GAP[0] + 360)
    cx, cy = RING_C
    return ((cx + RING_R * math.cos(a0), cy + RING_R * math.sin(a0)),
            (cx + RING_R * math.cos(a1), cy + RING_R * math.sin(a1)))


# ---------------- SVG ----------------
def svg_mark(fill="#fff") -> str:
    (x0, y0), (x1, y1) = ring_ends()
    spark = " ".join(f"{x:.1f},{y:.1f}" for x, y in sparkle_points(*SPARK_C, SPARK_R))
    return (f'<path d="M {x0:.1f} {y0:.1f} A {RING_R} {RING_R} 0 1 1 {x1:.1f} {y1:.1f}" fill="none" '
            f'stroke="{fill}" stroke-width="{RING_W}" stroke-linecap="round"/>'
            f'<polygon points="{spark}" fill="{fill}"/>')


def svg_tile(prefix: str, x0, y0, x1, y1) -> str:
    """Плитка + знак. Без SVG/CSS-фильтров: в WebKit (окно приложения) они размывают картинку.
    Объём знаку даёт чёткая сдвинутая «тень»-копия вместо размытой."""
    return f'''<defs>
  <linearGradient id="{prefix}bg" x1="0.15" y1="0" x2="0.85" y2="1">
    <stop offset="0" stop-color="{COLORS[0]}"/><stop offset="0.5" stop-color="{COLORS[1]}"/><stop offset="1" stop-color="{COLORS[2]}"/>
  </linearGradient>
  <radialGradient id="{prefix}shine" cx="0.28" cy="0.14" r="0.42">
    <stop offset="0" stop-color="#fff" stop-opacity="0.22"/><stop offset="1" stop-color="#fff" stop-opacity="0"/>
  </radialGradient>
</defs>
<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{TILE_R}" fill="url(#{prefix}bg)"/>
<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" rx="{TILE_R}" fill="url(#{prefix}shine)"/>
<rect x="{x0 + 2}" y="{y0 + 2}" width="{x1 - x0 - 4}" height="{y1 - y0 - 4}" rx="{TILE_R - 2}" fill="none"
      stroke="#fff" stroke-opacity="0.22" stroke-width="4"/>
<g transform="translate(0 6)" opacity="0.2">{svg_mark("#0b1a6b")}</g>
{svg_mark()}'''


def write_svgs() -> None:
    x0, y0, x1, y1 = TILE
    # logo.svg — с полями по сетке Apple (favicon); logo-tile.svg — только плитка (интерфейс)
    logo = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" role="img" aria-label="LakonIQ">\n'
            f'{svg_tile("l", x0, y0, x1, y1)}\n</svg>\n')
    tile = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0} {y0} {x1 - x0} {y1 - y0}" role="img" '
            f'aria-label="LakonIQ" shape-rendering="geometricPrecision">\n{svg_tile("t", x0, y0, x1, y1)}\n</svg>\n')
    lo = min(RING_C) - RING_R - RING_W / 2 - 12          # рамка точно по знаку
    hi = max(SPARK_C) + SPARK_R + 12
    mark = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{lo:.0f} {lo:.0f} {hi - lo:.0f} {hi - lo:.0f}" '
            f'role="img" aria-label="LakonIQ" shape-rendering="geometricPrecision">\n{svg_mark()}\n</svg>\n')
    (STATIC / "logo.svg").write_text(logo, encoding="utf-8")
    (STATIC / "logo-tile.svg").write_text(tile, encoding="utf-8")
    (STATIC / "mark.svg").write_text(mark, encoding="utf-8")


# ---------------- PNG / ICNS ----------------
def hex_rgb(h):
    return np.array([int(h[i:i + 2], 16) for i in (1, 3, 5)], dtype=np.float32)


def render_png(size=1024, ss=3) -> Image.Image:
    S = size * ss
    k = S / 1024
    y, x = np.mgrid[0:S, 0:S].astype(np.float32) / S
    t = np.clip((x - 0.15) * 0.42 + y * 0.78, 0, 1)[..., None]
    c0, c1, c2 = (hex_rgb(c) for c in COLORS)
    rgb = np.where(t < 0.5, c0 + (c1 - c0) * (t / 0.5), c1 + (c2 - c1) * ((t - 0.5) / 0.5))
    shine = np.exp(-(((x - 0.28) ** 2) + ((y - 0.14) ** 2)) / 0.035)[..., None] * 45
    tile_img = Image.fromarray(np.clip(rgb + shine, 0, 255).astype(np.uint8), "RGB").convert("RGBA")

    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    tile_box = tuple(v * k for v in TILE)
    # тень под плиткой
    sh = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sh).rounded_rectangle((tile_box[0], tile_box[1] + 14 * k, tile_box[2], tile_box[3] + 14 * k),
                                         TILE_R * k, fill=120)
    icon.paste((0, 0, 0, 255), (0, 0), sh.filter(ImageFilter.GaussianBlur(18 * k)))
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle(tile_box, TILE_R * k, fill=255)
    icon.paste(tile_img, (0, 0), mask)
    edge = Image.new("L", (S, S), 0)
    ImageDraw.Draw(edge).rounded_rectangle(tuple(v + (2 * k if i < 2 else -2 * k) for i, v in enumerate(tile_box)),
                                           (TILE_R - 2) * k, outline=56, width=max(1, int(4 * k)))
    icon.paste((255, 255, 255, 255), (0, 0), edge)

    # знак: кольцо-«Q» с хвостом-искрой
    m = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(m)
    cx, cy = RING_C[0] * k, RING_C[1] * k
    r, w = RING_R * k, RING_W * k
    d.arc((cx - r - w / 2, cy - r - w / 2, cx + r + w / 2, cy + r + w / 2), GAP[1], GAP[0] + 360, fill=255, width=int(w))
    for ex, ey in ring_ends():  # круглые концы
        ex, ey = ex * k, ey * k
        d.ellipse((ex - w / 2, ey - w / 2, ex + w / 2, ey + w / 2), fill=255)
    d.polygon([(px * k, py * k) for px, py in sparkle_points(*SPARK_C, SPARK_R)], fill=255)
    shadow = m.filter(ImageFilter.GaussianBlur(3 * k))
    icon.paste((11, 26, 107, 255), (0, int(4 * k)), Image.eval(shadow, lambda v: int(v * 0.22)))
    icon.paste((255, 255, 255, 255), (0, 0), m)
    return icon.resize((size, size), Image.LANCZOS)


def main() -> None:
    BUILD.mkdir(exist_ok=True)
    write_svgs()
    icon = render_png()
    icon.save(BUILD / "icon_1024.png")
    iconset = BUILD / "icon.iconset"
    iconset.mkdir(exist_ok=True)
    for base in (16, 32, 128, 256, 512):
        icon.resize((base, base), Image.LANCZOS).save(iconset / f"icon_{base}x{base}.png")
        icon.resize((base * 2, base * 2), Image.LANCZOS).save(iconset / f"icon_{base}x{base}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(BUILD / "icon.icns")], check=True)
    print("логотип: static/logo.svg, static/mark.svg; иконка:", BUILD / "icon.icns")


if __name__ == "__main__":
    main()
