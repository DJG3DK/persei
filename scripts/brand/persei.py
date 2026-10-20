#!/usr/bin/env python3
"""Persei brand marks, built from geometry measured off the approved concept
(2026-10-20). build_assets.py turns them into every image the product ships."""


NAVY, NAVY_HI = "#0B1426", "#16233F"
GOLD, GOLD_HI, GOLD_LO = "#EFBF4E", "#FFD978", "#C9962B"
LINE = "#5A6884"

# The constellation, measured in the concept's app tile (169,208)-(437,475),
# scaled to a 1024 grid. Sizes are snapped to three steps for a clean read.
S = 1024 / 268
RAW = {  # name: (x, y, size)  size 1 small, 2 medium, 3 large
    "A": (343.0, 262.2, 1), "B": (326.8, 278.2, 2), "C": (321.9, 304.2, 1),
    "D": (281.9, 316.2, 2), "E": (320.6, 335.4, 1), "F": (279.2, 350.9, 1),
    "G": (323.1, 361.7, 3), "H": (264.0, 368.1, 2), "I": (326.5, 375.9, 1),
    "J": (263.0, 396.0, 1), "K": (280.6, 419.2, 1), "L": (268.9, 421.9, 2),
}
P_DIAMOND = ((304.2 - 169) * S, (302.5 - 208) * S)
STARS = {k: ((x - 169) * S, (y - 208) * S, s) for k, (x, y, s) in RAW.items()}
EDGES = ["AB", "BC", "CP", "PD", "PE", "DF", "FH", "HJ", "JL", "LK", "EG", "GI"]
RADIUS = {1: 10, 2: 14.5, 3: 18.5}


def pt(name):
    return P_DIAMOND if name == "P" else STARS[name][:2]


def p_glyph(cx, cy, h, fill):
    """The P as geometry (no font): stem, round bowl, counter. h = cap height."""
    u = h / 60  # designed on a 60-unit cap height
    x0, y0 = cx - 17 * u, cy - 30 * u
    def X(v): return f"{x0 + v * u:.2f}"
    def Y(v): return f"{y0 + v * u:.2f}"
    d = (f"M{X(0)} {Y(0)} H{X(20)} A{20*u:.2f} {20*u:.2f} 0 0 1 {X(20)} {Y(40)} H{X(13)} V{Y(60)} H{X(0)} Z "
         f"M{X(13)} {Y(12)} H{X(19.5)} A{8*u:.2f} {8*u:.2f} 0 0 1 {X(19.5)} {Y(28)} H{X(13)} Z")
    return f'<path d="{d}" fill="{fill}" fill-rule="evenodd"/>'


def diamond(cx, cy, half, fill, rx_frac=0.12, extra=""):
    side = half * 2 ** 0.5
    rx = side * rx_frac
    return (f'<rect x="{cx - side/2:.2f}" y="{cy - side/2:.2f}" width="{side:.2f}" height="{side:.2f}" '
            f'rx="{rx:.2f}" fill="{fill}" transform="rotate(45 {cx:.2f} {cy:.2f})"{extra}/>')


def mark(cx, cy, half, gold=GOLD, ink=NAVY, extra=""):
    return diamond(cx, cy, half, gold, extra=extra) + p_glyph(cx, cy + half * 0.02, half * 0.78, ink)


def constellation(line=LINE, star=GOLD, width=5.5):
    lines = "".join(
        f'<line x1="{pt(a)[0]:.1f}" y1="{pt(a)[1]:.1f}" x2="{pt(b)[0]:.1f}" y2="{pt(b)[1]:.1f}"/>'
        for a, b in EDGES)
    stars = "".join(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{RADIUS[s]}"/>' for x, y, s in STARS.values())
    return (f'<g stroke="{line}" stroke-width="{width}" stroke-linecap="round">{lines}</g>'
            f'<g fill="{star}">{stars}</g>')


def svg(body, size=1024, title="Persei"):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
            f'role="img" aria-label="{title}"><title>{title}</title>{body}</svg>\n')


def app_icon():
    bg = (f'<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
          f'<stop offset="0" stop-color="{NAVY_HI}"/><stop offset="1" stop-color="{NAVY}"/></linearGradient></defs>'
          f'<rect width="1024" height="1024" rx="196" fill="url(#bg)"/>')
    return svg(bg + constellation() + mark(*P_DIAMOND, 53.5), title="Persei app icon")


def social(square=False):
    c, r = 512, 512
    base = (f'<rect width="1024" height="1024" fill="{NAVY}"/>' if square
            else f'<circle cx="{c}" cy="{c}" r="{r}" fill="{NAVY}"/>')
    # Measured from the concept's circle (centre 608.5,382; r 93.5), on 1024.
    k = 512 / 93.5
    def P(x, y): return ((x - 608.5) * k + 512, (y - 382) * k + 512)
    mx, my = P(608.6, 343.4)
    layers = []
    for dy, fill, op in [(24, "#FFFFFF", 0.38), (12, "#FFFFFF", 0.62), (0, "#FFFFFF", 1.0)]:
        cx, cy = P(608.5, 384 + dy)
        hw, hh = 40.5 * k, 16 * k
        layers.append(f'<path d="M{cx - hw:.1f} {cy:.1f} L{cx:.1f} {cy - hh:.1f} L{cx + hw:.1f} {cy:.1f} '
                      f'L{cx:.1f} {cy + hh:.1f} Z" fill="{fill}" fill-opacity="{op}" stroke-linejoin="round"/>')
    return svg(base + "".join(layers) + mark(mx, my, 14.6 * k), title="Persei")


def tab_icon():
    # A favicon is a 16-32px job: the mark is enlarged and the corners kept
    # generous so it survives the downscale.
    return svg(f'<rect width="1024" height="1024" rx="190" fill="{NAVY}"/>' + mark(512, 512, 372),
               title="Persei")


def mark_only():
    return svg(mark(512, 512, 470), title="Persei mark")


