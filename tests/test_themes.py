"""The scheme list exists in three places; this keeps them identical.

agent/auth.py THEMES gates what the API will store, frontend/src/themes.ts is
what the picker offers, and frontend/src/theme.css is what actually paints.
A scheme in two of the three is the failure worth preventing, and it is
silent in every direction: the server accepts a value the stylesheet has no
rule for (the operator saves a colour and nothing changes), or the picker
offers one the server rejects (Save returns 400 for a scheme that visibly
exists), or a stylesheet block nothing can ever select.

The contrast ratios are checked too, for the same reason the original palette
carried computed ratios in its comments: --text-faint spent a long time at
3.04:1 against the card surface, which is why meta text read as mush, and
nothing caught it because nobody measured.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.auth import DEFAULT_THEME, THEMES

ROOT = Path(__file__).resolve().parent.parent
THEME_CSS = ROOT / "frontend" / "src" / "theme.css"
THEMES_TS = ROOT / "frontend" / "src" / "themes.ts"

# WCAG AA for body text. Every foreground token is measured against the
# surface it sits on, not against the page ground.
MIN_RATIO = 4.5


def _srgb_to_linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _srgb_to_linear(r) + 0.7152 * _srgb_to_linear(g) + 0.0722 * _srgb_to_linear(b)


def contrast(fg: str, bg: str) -> float:
    a, b = _luminance(fg), _luminance(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def _css_blocks() -> dict[str, dict[str, str]]:
    """Every [data-theme="x"] block, plus :root as the default scheme."""
    css = THEME_CSS.read_text()
    out: dict[str, dict[str, str]] = {}

    def tokens(body: str) -> dict[str, str]:
        return {m.group(1): m.group(2).strip()
                for m in re.finditer(r"(--[a-z0-9-]+):\s*([^;]+);", body)}

    root = re.search(r":root\s*\{(.*?)\n\}", css, re.DOTALL)
    assert root, "theme.css has no :root block"
    out[DEFAULT_THEME] = tokens(root.group(1))

    for m in re.finditer(r'\[data-theme="([a-z]+)"\]\s*\{(.*?)\n\}', css, re.DOTALL):
        out[m.group(1)] = tokens(m.group(2))
    return out


@pytest.fixture(scope="module")
def blocks() -> dict[str, dict[str, str]]:
    return _css_blocks()


def test_the_three_lists_are_the_same_set(blocks):
    ts = set(re.findall(r"id: '([a-z]+)'", THEMES_TS.read_text()))
    assert set(THEMES) == ts, "agent/auth.py THEMES and frontend/src/themes.ts disagree"
    assert set(THEMES) == set(blocks), "theme.css has no rule for some scheme, or one nothing offers"


def test_the_default_is_the_root_block_and_carries_no_attribute():
    """Drafting is :root, so applyTheme removes the attribute for it. If the
    default ever gained its own [data-theme] block the two would both be live
    and the last one in the file would win."""
    assert DEFAULT_THEME == "drafting"
    css = THEME_CSS.read_text()
    assert f'[data-theme="{DEFAULT_THEME}"]' not in css


def test_there_are_five_schemes():
    """The operator asked for five. A sixth added without a preview tile, or a
    fifth quietly dropped, is worth noticing here."""
    assert len(THEMES) == 5


@pytest.mark.parametrize("theme", THEMES)
def test_every_scheme_defines_the_tokens_that_carry_its_identity(blocks, theme):
    required = {
        "--bg", "--surface", "--surface-raised", "--surface-hover", "--surface-sunken",
        "--border", "--border-soft", "--border-strong",
        "--text", "--text-dim", "--text-faint",
        "--accent", "--accent-hover", "--accent-dim", "--accent-glow", "--accent-ring",
        "--ambient-1", "--ambient-2", "--ambient-3",
    }
    missing = required - set(blocks[theme])
    assert not missing, f"{theme} inherits {sorted(missing)} from the default, which will clash"


@pytest.mark.parametrize("theme", THEMES)
@pytest.mark.parametrize("token", ["--text", "--text-dim", "--text-faint", "--accent", "--accent-hover"])
def test_every_foreground_clears_the_body_minimum(blocks, theme, token):
    b = blocks[theme]
    ratio = contrast(b[token], b["--surface"])
    assert ratio >= MIN_RATIO, f"{theme} {token} is {ratio:.2f}:1 on its own --surface"


@pytest.mark.parametrize("theme", THEMES)
def test_the_state_colours_stay_readable_on_every_ground(blocks, theme):
    """State colours are deliberately shared across schemes -- running, waiting,
    done and failed must not change meaning with a preference -- so each new
    surface has to be checked against them rather than the other way round."""
    root = blocks[DEFAULT_THEME]
    surface = blocks[theme]["--surface"]
    for name in ("--blue", "--amber", "--green", "--red", "--cyan"):
        value = root[name].split()[0]
        ratio = contrast(value, surface)
        assert ratio >= MIN_RATIO, f"{name} is {ratio:.2f}:1 on {theme}'s surface"


def test_the_ambient_light_is_a_token_not_a_literal():
    """App.css paints the body-level glow. While it held a literal brass rgba,
    every scheme changed its widgets and kept the original's light -- which is
    most of what makes a theme read as a different room."""
    app_css = (ROOT / "frontend" / "src" / "App.css").read_text()
    assert "var(--ambient-1)" in app_css
    assert "rgba(201, 162, 39" not in app_css


def test_the_landing_page_is_not_themed():
    """The public page is the product's own colours: a visitor has no account
    and therefore no preference, and the brand should not depend on whoever
    signed in last.

    It lives in site/ now, built and served separately (perseiagent.ai), which is
    what makes this structural rather than a convention: it carries its own
    copy of the Drafting tokens and never imports the console's stylesheet,
    so the five schemes cannot reach it even by accident.
    """
    landing = (ROOT / "site" / "src" / "LandingPage.css").read_text()
    assert "rgba(201, 162, 39" in landing, "the landing page should keep Drafting's brass"

    site_src = ROOT / "site" / "src"
    for f in site_src.rglob("*.ts*"):
        assert "theme.css" not in f.read_text(), (
            f"{f.name} imports the console's theme.css; the public page must not inherit themes")
    tokens = (site_src / "tokens.css").read_text()
    assert "[data-theme" not in tokens, "the public page must carry no theme switching"


def test_the_public_page_and_the_console_agree_on_the_brand():
    """site/src/tokens.css is a COPY of Drafting, so the two can drift. They
    are the same product, so the accent and the ground have to match."""
    tokens = (ROOT / "site" / "src" / "tokens.css").read_text()
    root = _css_blocks()[DEFAULT_THEME]
    for name in ("--bg", "--surface", "--accent", "--text"):
        value = root[name]
        assert f"{name}:" in tokens, f"the public page is missing {name}"
        got = re.search(rf"{name}:\s*(#[0-9a-fA-F]{{6}})", tokens)
        assert got and got.group(1).lower() == value.lower(), (
            f"{name} drifted: console {value}, public page {got.group(1) if got else 'missing'}")
