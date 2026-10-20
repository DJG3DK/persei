"""The landing page's constellation: HDR-style bloom, a bevelled 3D diamond,
and three depth layers the page's CSS sways in 3D (site/src/LandingPage.css,
.lp-sky). The page ships no JavaScript, so the motion is CSS keyframes, not
pointer tracking.

Writes site/src/constellation.ts (the three layers as SVG strings) and
scripts/brand/svg/persei-constellation.svg (one static file of the same art).

    python scripts/brand/build_hero.py
"""
import json
import pathlib
import random
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import persei as b  # noqa: E402

REPO = pathlib.Path(__file__).parent.parent.parent

# The constellation, re-centred on its own bounds for a hero (not an icon).
xs = [x for x, _, _ in b.STARS.values()] + [b.P_DIAMOND[0]]
ys = [y for _, y, _ in b.STARS.values()] + [b.P_DIAMOND[1]]
cx, cy = (min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2
W = H = 900
SC = 1.28


def T(x, y):
    return ((x - cx) * SC + W / 2, (y - cy) * SC + H / 2)


STARS = {k: (*T(x, y), s) for k, (x, y, s) in b.STARS.items()}
PD = T(*b.P_DIAMOND)
R = {1: 7.5, 2: 10.5, 3: 13.5}


def P(name):
    return PD if name == "P" else STARS[name][:2]


def defs():
    return f"""
<defs>
  <radialGradient id="pz-core" cx="50%" cy="50%" r="50%">
    <stop offset="0" stop-color="#FFFFFF"/>
    <stop offset="0.35" stop-color="#FFF4D6"/>
    <stop offset="0.7" stop-color="{b.GOLD_HI}"/>
    <stop offset="1" stop-color="{b.GOLD}"/>
  </radialGradient>
  <linearGradient id="pz-beam" gradientUnits="userSpaceOnUse" x1="0" y1="0" x2="{W}" y2="{H}">
    <stop offset="0" stop-color="#9FB2D6"/><stop offset="1" stop-color="#6B7CA0"/>
  </linearGradient>
  <!-- Bloom: the source, a tight glow and a wide one, added together. -->
  <filter id="pz-bloom" x="-200%" y="-200%" width="500%" height="500%" color-interpolation-filters="sRGB">
    <feGaussianBlur in="SourceGraphic" stdDeviation="3" result="b1"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="9" result="b2"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="22" result="b3"/>
    <feComponentTransfer in="b3" result="b3d"><feFuncA type="linear" slope="0.55"/></feComponentTransfer>
    <feMerge><feMergeNode in="b3d"/><feMergeNode in="b2"/><feMergeNode in="b1"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
  <filter id="pz-lineglow" x="-20%" y="-20%" width="140%" height="140%">
    <feGaussianBlur stdDeviation="2.4" result="g"/>
    <feMerge><feMergeNode in="g"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
  <filter id="pz-gemshadow" x="-100%" y="-100%" width="300%" height="300%">
    <feGaussianBlur in="SourceAlpha" stdDeviation="10"/>
    <feOffset dx="0" dy="12" result="s"/>
    <feFlood flood-color="#000" flood-opacity="0.55"/><feComposite in2="s" operator="in" result="shadow"/>
    <feGaussianBlur in="SourceGraphic" stdDeviation="16" result="halo"/>
    <feComponentTransfer in="halo" result="halo2"><feFuncA type="linear" slope="0.65"/></feComponentTransfer>
    <feMerge><feMergeNode in="halo2"/><feMergeNode in="shadow"/><feMergeNode in="SourceGraphic"/></feMerge>
  </filter>
  <linearGradient id="pz-faceG" x1="0" y1="0" x2="1" y2="1">
    <stop offset="0" stop-color="#FFE29A"/><stop offset="0.55" stop-color="{b.GOLD}"/><stop offset="1" stop-color="#E2A93A"/>
  </linearGradient>
  <linearGradient id="pz-spike" x1="0" y1="0" x2="1" y2="0">
    <stop offset="0" stop-color="#FFF6DD" stop-opacity="0"/><stop offset="0.5" stop-color="#FFF6DD" stop-opacity="0.9"/>
    <stop offset="1" stop-color="#FFF6DD" stop-opacity="0"/>
  </linearGradient>
</defs>"""


def lines_svg():
    segs = []
    for a, c in b.EDGES:
        (x1, y1), (x2, y2) = P(a), P(c)
        segs.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>')
    base = f'<g class="beams" stroke="url(#pz-beam)" stroke-width="2.6" stroke-linecap="round" stroke-opacity="0.85" filter="url(#pz-lineglow)">{"".join(segs)}</g>'
    pulse = f'<g class="pulse" stroke="#FFE7A6" stroke-width="2.2" stroke-linecap="round" stroke-dasharray="14 260">{"".join(segs)}</g>'
    return base + pulse


def star_svg(name, x, y, s, i):
    r = R[s]
    spikes = ""
    if s >= 2:
        L = r * (6.5 if s == 3 else 4.5)
        spikes = (f'<g class="spikes" opacity="0.75">'
                  f'<rect x="{x - L:.1f}" y="{y - 0.9:.1f}" width="{2 * L:.1f}" height="1.8" fill="url(#pz-spike)"/>'
                  f'<rect x="{x - L:.1f}" y="{y - 0.9:.1f}" width="{2 * L:.1f}" height="1.8" fill="url(#pz-spike)" transform="rotate(90 {x:.1f} {y:.1f})"/></g>')
    delay = (i * 0.73) % 5
    return (f'<g class="star s{s}" style="--d:{delay:.2f}s;transform-origin:{x:.1f}px {y:.1f}px">{spikes}'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r * 1.9:.1f}" fill="{b.GOLD}" fill-opacity="0.18" filter="url(#pz-bloom)"/>'
            f'<circle class="core" cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="url(#pz-core)" filter="url(#pz-bloom)"/></g>')


def gem_svg():
    x, y = PD
    h = 46  # half-diagonal
    inner = h * 0.66
    top, right, bot, left = (x, y - h), (x + h, y), (x, y + h), (x - h, y)
    it, ir, ib, il = (x, y - inner), (x + inner, y), (x, y + inner), (x - inner, y)
    def poly(*ps, fill): return f'<polygon points="{" ".join(f"{a:.1f},{c:.1f}" for a, c in ps)}" fill="{fill}"/>'
    facets = (poly(top, right, ir, it, fill="#FFE7A8") + poly(right, bot, ib, ir, fill="#C98F25")
              + poly(bot, left, il, ib, fill="#B47E1E") + poly(left, top, it, il, fill="#FFD36E")
              + poly(it, ir, ib, il, fill="url(#pz-faceG)"))
    edge = f'<polygon points="{top[0]:.1f},{top[1]:.1f} {right[0]:.1f},{right[1]:.1f} {bot[0]:.1f},{bot[1]:.1f} {left[0]:.1f},{left[1]:.1f}" fill="none" stroke="#FFF1C8" stroke-opacity="0.55" stroke-width="1.2" stroke-linejoin="round"/>'
    glint = f'<path d="M{it[0] - inner*0.55:.1f} {it[1] + inner*0.62:.1f} L{it[0]:.1f} {it[1] + inner*0.1:.1f}" stroke="#FFFFFF" stroke-opacity="0.55" stroke-width="2.4" stroke-linecap="round"/>'
    glyph = b.p_glyph(x, y + 0.5, inner * 1.0, b.NAVY)
    return f'<g class="gem" filter="url(#pz-gemshadow)">{facets}{edge}{glint}{glyph}</g>'


def starfield(n=140, seed=7):
    rnd = random.Random(seed)
    dots = []
    for _ in range(n):
        x, y = rnd.uniform(0, W), rnd.uniform(0, H)
        r = rnd.choice([0.6, 0.8, 0.8, 1.0, 1.3])
        o = rnd.uniform(0.15, 0.55)
        dots.append(f'<circle cx="{x:.0f}" cy="{y:.0f}" r="{r}" fill="#CFD8F0" fill-opacity="{o:.2f}"/>')
    return "".join(dots)


def layer(cls, body, with_defs=False):
    return (f'<svg class="lp-sky-layer {cls}" viewBox="0 0 {W} {H}" aria-hidden="true" focusable="false">'
            f'{defs() if with_defs else ""}{body}</svg>')


def static_svg():
    stars = "".join(star_svg(k, x, y, s, i) for i, (k, (x, y, s)) in enumerate(STARS.items()))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" '
            f'aria-label="Persei">{defs()}{lines_svg()}{stars}{gem_svg()}</svg>\n')


def main():
    stars = "".join(star_svg(k, x, y, s, i) for i, (k, (x, y, s)) in enumerate(STARS.items()))
    layers = {
        "FAR": layer("lp-sky-far", starfield(110, 7)),
        "NEAR": layer("lp-sky-near", starfield(70, 11)),
        "MAIN": layer("lp-sky-main", lines_svg() + stars + gem_svg(), with_defs=True),
    }
    ts = ["// Generated by scripts/brand/build_hero.py -- edit that, not this.",
          "// The landing page's constellation, as three depth layers (see .lp-sky in LandingPage.css)."]
    ts += [f"export const SKY_{k} = {json.dumps(v)};" for k, v in layers.items()]
    (REPO / "site/src/constellation.ts").write_text("\n".join(ts) + "\n")
    (pathlib.Path(__file__).parent / "svg" / "persei-constellation.svg").write_text(static_svg())
    print("site/src/constellation.ts written")


if __name__ == "__main__":
    main()
