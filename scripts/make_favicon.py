#!/usr/bin/env python3
"""Genera el favicon de agent-mesh (SVG + PNG + ICO) sin dependencias externas.

Motivo: un mini-grafo (nodos y aristas) sobre fondo oscuro. Se dibuja a 256px
y se reduce con LANCZOS para que los bordes queden limpios a 16/32px.
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

BASE = Path(__file__).resolve().parent.parent / "web" / "static"
SIZE = 256

BG = (13, 17, 23, 255)          # #0d1117
EDGE = (42, 106, 168, 255)      # #2a6aa8
CENTER = (78, 161, 255, 255)    # #4ea1ff
SAT = [
    (63, 185, 80, 255),         # #3fb950 verde
    (240, 136, 62, 255),        # #f0883e naranja
    (163, 113, 247, 255),       # #a371f7 violeta
    (57, 197, 207, 255),        # #39c5cf cian
]

# posiciones relativas (0..1) de los nodos satelite alrededor del centro
POS = [(0.50, 0.185), (0.845, 0.565), (0.50, 0.885), (0.155, 0.545)]


def rounded_mask(size, radius):
    m = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(m)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def draw_favicon(size=SIZE):
    img = Image.new("RGBA", (size, size), BG)

    # halo radial suave detras del centro
    glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy = size * 0.5, size * 0.56
    for r, a in ((86, 26), (64, 34), (44, 44)):
        gd.ellipse([cx - r, cy - r, cx + r, cy + r], fill=CENTER[:3] + (a,))
    glow = glow.filter(ImageFilter.GaussianBlur(size * 0.045))
    img = Image.alpha_composite(img, glow)

    d = ImageDraw.Draw(img)

    pts = [(size * x, size * y) for x, y in POS]
    center = (size * 0.5, size * 0.56)

    # aristas: centro -> satelites, y un anillo exterior entre satelites
    w = max(3, int(size * 0.028))
    for p in pts:
        d.line([center, p], fill=EDGE, width=w)
    for i in range(len(pts)):
        d.line([pts[i], pts[(i + 1) % len(pts)]], fill=(30, 62, 96, 255), width=w)

    # nodos satelite
    rs = max(4, int(size * 0.072))
    for p, c in zip(pts, SAT):
        d.ellipse([p[0] - rs, p[1] - rs, p[0] + rs, p[1] + rs], fill=c)
        d.ellipse([p[0] - rs, p[1] - rs, p[0] + rs, p[1] + rs],
                  outline=BG, width=max(2, int(size * 0.012)))

    # nodo central, mas grande y con aro claro
    rc = max(6, int(size * 0.115))
    d.ellipse([center[0] - rc, center[1] - rc, center[0] + rc, center[1] + rc],
              fill=CENTER)
    d.ellipse([center[0] - rc, center[1] - rc, center[0] + rc, center[1] + rc],
              outline=(230, 242, 255, 255), width=max(2, int(size * 0.016)))

    # esquinas redondeadas tipo app icon
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), rounded_mask(size, int(size * 0.19)))
    return out


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" role="img" aria-label="agent-mesh">
  <defs>
    <radialGradient id="g" cx="50%" cy="56%" r="52%">
      <stop offset="0%" stop-color="#4ea1ff" stop-opacity="0.34"/>
      <stop offset="100%" stop-color="#4ea1ff" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <rect width="64" height="64" rx="12" fill="#0d1117"/>
  <circle cx="32" cy="35.8" r="17" fill="url(#g)"/>
  <g stroke="#2a6aa8" stroke-width="2.1" stroke-linecap="round">
    <path d="M32 35.8 L32 11.8"/>
    <path d="M32 35.8 L54.1 36.2"/>
    <path d="M32 35.8 L32 56.6"/>
    <path d="M32 35.8 L9.9 34.9"/>
  </g>
  <g stroke="#1e3e60" stroke-width="2.1" stroke-linecap="round">
    <path d="M32 11.8 L54.1 36.2 L32 56.6 L9.9 34.9 Z"/>
  </g>
  <g stroke="#0d1117" stroke-width="1.1">
    <circle cx="32" cy="11.8" r="4.6" fill="#3fb950"/>
    <circle cx="54.1" cy="36.2" r="4.6" fill="#f0883e"/>
    <circle cx="32" cy="56.6" r="4.6" fill="#a371f7"/>
    <circle cx="9.9" cy="34.9" r="4.6" fill="#39c5cf"/>
  </g>
  <circle cx="32" cy="35.8" r="7.4" fill="#4ea1ff" stroke="#e6f2ff" stroke-width="1.9"/>
</svg>
"""


def main():
    BASE.mkdir(parents=True, exist_ok=True)
    (BASE / "favicon.svg").write_text(SVG, encoding="utf-8")

    big = draw_favicon(256)
    big.save(BASE / "favicon-256.png")

    for s in (16, 32, 48, 180):
        img = big.resize((s, s), Image.LANCZOS)
        name = "apple-touch-icon.png" if s == 180 else f"favicon-{s}.png"
        img.save(BASE / name)

    # ICO multi-tamano para navegadores viejos y marcadores
    ico_sizes = [(16, 16), (32, 32), (48, 48)]
    big.save(BASE / "favicon.ico", sizes=ico_sizes)

    for f in sorted(BASE.glob("favicon*")) + [BASE / "apple-touch-icon.png"]:
        print(f"{f.name:24} {f.stat().st_size:>7} B")


if __name__ == "__main__":
    main()
