"""Generate every Persei brand image from one geometry definition.

The mark (2026-10-20): the Perseus constellation in gold on navy, with a gold
diamond P as one of its stars. Two forms, by size:

* the constellation tile -- the app icon, the home-screen icons, anything
  128px and up;
* the gem tile (the diamond P alone, enlarged) -- favicons, the tray, and
  every icon under 128px, where the constellation's stars shrink to dust.

The wordmark is PERSEI set in Jost Medium and turned into outlines
(persei-wordmark.svg), so nothing here needs the font.

Geometry is in persei.py. Rasterising is Chromium's (Playwright), which
anti-aliases SVG properly; PIL then writes the .ico, .icns and .bmp files.

    python scripts/brand/build_assets.py          # writes into the repo
    python scripts/brand/build_assets.py OUT_DIR  # everything into one folder
"""

from __future__ import annotations

import asyncio
import io
import pathlib
import re
import sys

from PIL import Image

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import persei as P  # noqa: E402

HERE = pathlib.Path(__file__).parent
REPO = HERE.parent.parent
ONE_DIR = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else None

INK = "#E8ECF5"        # the wordmark on dark grounds
SUBTLE = "#8C9AB8"

_WORD = HERE / "persei-wordmark.svg"
_WORD_VB = [float(v) for v in re.search(r'viewBox="([^"]+)"', _WORD.read_text()).group(1).split()]
_WORD_D = re.search(r' d="([^"]+)"', _WORD.read_text()).group(1)


def _svg(body: str, w: float, h: float) -> str:
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:g} {h:g}" '
            f'width="{w:g}" height="{h:g}">{body}</svg>')


def wordmark(x: float, y: float, cap: float, fill: str = INK) -> str:
    """PERSEI with its cap height `cap`, top-left at (x, y)."""
    k = cap / _WORD_VB[3]
    return f'<path transform="translate({x:.2f} {y:.2f}) scale({k:.4f})" d="{_WORD_D}" fill="{fill}"/>'


def wordmark_width(cap: float) -> float:
    return _WORD_VB[2] * cap / _WORD_VB[3]


# ── the pieces ───────────────────────────────────────────────────────────────

def app_tile(full_bleed: bool = False) -> str:
    """The constellation tile. full_bleed: square corners and the art inside
    the 80% safe zone, for maskable icons that the OS crops itself."""
    bg = (f'<defs><linearGradient id="bg" x1="0" y1="0" x2="0" y2="1">'
          f'<stop offset="0" stop-color="{P.NAVY_HI}"/><stop offset="1" stop-color="{P.NAVY}"/>'
          f'</linearGradient></defs>'
          f'<rect width="1024" height="1024" rx="{0 if full_bleed else 196}" fill="url(#bg)"/>')
    art = P.constellation() + P.mark(*P.P_DIAMOND, 53.5)
    if full_bleed:
        art = f'<g transform="translate(512 512) scale(0.8) translate(-512 -512)">{art}</g>'
    return _svg(bg + art, 1024, 1024)


def gem_tile() -> str:
    return _svg(f'<rect width="1024" height="1024" rx="190" fill="{P.NAVY}"/>' + P.mark(512, 512, 372), 1024, 1024)


def lockup() -> str:
    """Gem beside PERSEI, on transparent, for the dashboard and the landing
    page (both dark)."""
    half, cap, gap, pad = 60, 58, 44, 8
    w = pad + 2 * half + gap + wordmark_width(cap) + pad
    h = 2 * half + 2 * pad
    body = P.mark(pad + half, h / 2, half) + wordmark(pad + 2 * half + gap, h / 2 - cap / 2, cap)
    return _svg(body, round(w), h)


def installer_header() -> str:
    """NSIS header, 150x57, on the white of the installer's top band."""
    return _svg('<rect width="150" height="57" fill="#FFFFFF"/>' + P.mark(124, 28.5, 19, ink=P.NAVY), 150, 57)


def installer_sidebar() -> str:
    """NSIS welcome/finish sidebar, 164x314: the constellation, the name."""
    w, h = 164, 314
    k = 0.2
    art = (f'<g transform="translate({w / 2 - 512 * k:.1f} 18) scale({k})">'
           f'{P.constellation(width=7)}{P.mark(*P.P_DIAMOND, 60)}</g>')
    cap = 13
    word = wordmark(w / 2 - wordmark_width(cap) / 2, 236, cap)
    rule = f'<rect x="{w / 2 - 18}" y="266" width="36" height="2" rx="1" fill="{P.GOLD}"/>'
    return _svg(f'<rect width="{w}" height="{h}" fill="{P.NAVY}"/>' + art + word + rule, w, h)


def og_card() -> str:
    """The link preview, 1200x630, as a page (it has real text in it)."""
    k = 0.52
    art = (f'<svg viewBox="0 0 1024 1024" width="{1024 * k:.0f}" height="{1024 * k:.0f}" '
           f'style="position:absolute;right:40px;top:50px">'
           f'{P.constellation(width=5)}{P.mark(*P.P_DIAMOND, 56)}</svg>')
    lock = re.sub(r'width="[\d.]+" height="[\d.]+"', 'height="78"', lockup(), count=1)
    return f"""<!doctype html><meta charset="utf-8"><style>
html,body{{margin:0;width:1200px;height:630px;overflow:hidden}}
body{{background:radial-gradient(120% 90% at 78% 30%,{P.NAVY_HI} 0%,{P.NAVY} 60%,#080F1D 100%);
  font-family:Lato,"DejaVu Sans",sans-serif;color:{INK};position:relative}}
.lock{{position:absolute;left:84px;top:84px}}
h1{{position:absolute;left:92px;top:250px;margin:0;font-size:52px;line-height:1.18;font-weight:700;width:640px}}
h1 em{{font-style:normal;color:{P.GOLD}}}
p{{position:absolute;left:92px;top:396px;margin:0;font-size:26px;line-height:1.5;color:#A9B4CC;width:600px}}
.foot{{position:absolute;left:92px;right:92px;bottom:58px;border-top:1px solid #23314F;padding-top:22px;
  display:flex;justify-content:space-between;font:22px "DejaVu Sans Mono",monospace;letter-spacing:.06em}}
.foot span:first-child{{color:{SUBTLE}}} .foot span:last-child{{color:{P.GOLD}}}
</style><body><div class="lock">{lock}</div>{art}
<h1>An autonomous coding agent <em>that ships.</em></h1>
<p>Plans the work, writes the code, runs your real test suite, and passes an independent review gate before anything merges.</p>
<div class="foot"><span>PLAN · BUILD · VERIFY · REVIEW · SHIP</span><span>perseiagent.ai</span></div></body>"""


# ── rasterising ──────────────────────────────────────────────────────────────

async def _render(jobs: list[tuple[str, int, int]]) -> list[Image.Image]:
    from playwright.async_api import async_playwright
    out = []
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        for markup, w, h in jobs:
            await page.set_viewport_size({"width": w, "height": h})
            if markup.lstrip().startswith("<svg"):
                markup = re.sub(r'width="[\d.]+" height="[\d.]+"', f'width="{w}" height="{h}"', markup, count=1)
                markup = ('<!doctype html><style>html,body{margin:0;background:transparent}'
                          'svg{display:block}</style>' + markup)
            await page.set_content(markup)
            png = await page.screenshot(omit_background=True, clip={"x": 0, "y": 0, "width": w, "height": h})
            out.append(Image.open(io.BytesIO(png)).convert("RGBA"))
        await browser.close()
    return out


def render(jobs):
    return asyncio.run(_render(jobs))


def main() -> None:
    app, gem, full = app_tile(), gem_tile(), app_tile(full_bleed=True)
    lock = lockup()
    lw, lh = (int(float(v)) for v in re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', lock).groups())

    sizes_gem = [16, 24, 32, 48, 64]
    sizes_app = [64, 128, 180, 192, 256, 512, 1024]
    jobs = ([(gem, s, s) for s in sizes_gem] + [(app, s, s) for s in sizes_app]
            + [(full, 192, 192), (full, 512, 512), (lock, lw * 3, lh * 3),
               (installer_header(), 150, 57), (installer_sidebar(), 164, 314), (og_card(), 1200, 630),
               (gem, 1024, 1024)])
    imgs = render(jobs)
    it = iter(imgs)
    G = {s: next(it) for s in sizes_gem}
    A = {s: next(it) for s in sizes_app}
    full192, full512, lock_png, header, sidebar, og, gem1024 = (next(it) for _ in range(7))

    def icon_at(px: int) -> Image.Image:
        """The right tile for a size: the gem below 128px, the constellation from 128 up."""
        if px in G:
            return G[px]
        if px >= 128 and px in A:
            return A[px]
        src = gem1024 if px < 128 else A[1024]
        return src.resize((px, px), Image.LANCZOS)

    def ico(path: pathlib.Path, sizes):
        frames = [icon_at(s) for s in sizes]
        frames[-1].save(path, format="ICO", sizes=[(f.width, f.height) for f in frames],
                        append_images=frames[:-1])

    def out(rel: str) -> pathlib.Path:
        p = (ONE_DIR / pathlib.Path(rel).name) if ONE_DIR else REPO / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    # The dashboard and the landing page.
    for root in ("frontend/public", "site/public"):
        icon_at(16).save(out(f"{root}/favicon-16x16.png"))
        icon_at(32).save(out(f"{root}/favicon-32x32.png"))
        ico(out(f"{root}/favicon.ico"), (16, 32, 48))
        A[180].save(out(f"{root}/apple-touch-icon.png"))
    A[192].save(out("frontend/public/icon-192.png"))
    A[512].save(out("frontend/public/icon-512.png"))
    full512.save(out("frontend/public/icon-maskable-512.png"))
    ico(out("frontend/public/persei.ico"), (16, 24, 32, 48, 64, 128, 256))
    for rel in ("frontend/src/assets/persei-logo.png", "site/src/assets/persei-logo.png"):
        lock_png.save(out(rel))
    og.convert("RGB").save(out("site/public/og-preview.png"))

    # The desktop app (the names Tauri's own `tauri icon` would write).
    icons = "app/src-tauri/icons"
    for name, px in (("32x32.png", 32), ("64x64.png", 64), ("128x128.png", 128), ("128x128@2x.png", 256),
                     ("icon.png", 512), ("Square30x30Logo.png", 30), ("Square44x44Logo.png", 44),
                     ("Square71x71Logo.png", 71), ("Square89x89Logo.png", 89), ("Square107x107Logo.png", 107),
                     ("Square142x142Logo.png", 142), ("Square150x150Logo.png", 150),
                     ("Square284x284Logo.png", 284), ("Square310x310Logo.png", 310), ("StoreLogo.png", 50)):
        icon_at(px).save(out(f"{icons}/{name}"))
    G[64].save(out(f"{icons}/tray.png"))
    ico(out(f"{icons}/icon.ico"), (16, 24, 32, 48, 64, 128, 256))
    A[1024].save(out(f"{icons}/icon.icns"), format="ICNS")
    header.convert("RGB").save(out(f"{icons}/installer/header.bmp"))
    sidebar.convert("RGB").save(out(f"{icons}/installer/sidebar.bmp"))

    # The vector sources, for anyone who needs the mark at another size.
    for name, text in (("persei-app-icon.svg", app), ("persei-tab-icon.svg", gem), ("persei-lockup.svg", lock)):
        out(f"scripts/brand/svg/{name}").write_text(text + "\n")
    print("Persei brand assets written", f"to {ONE_DIR}" if ONE_DIR else "into the repo")


if __name__ == "__main__":
    main()
