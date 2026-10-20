"""Designing a logo, and turning it into a brand kit.

LogoLoom (mcpware/logoloom, MIT) does the parts a language model is bad at:
converting `<text>` to outlined `<path>` so a logo renders without the font
installed, optimising the SVG, tracing a raster image, and exporting a full
kit of PNGs, favicons, social images and a BRAND.md. It does NOT design
anything -- the model writes the SVG. That is worth being plain about,
because the pitch reads as though the tool produces concepts and it does not:
what it produces is everything that happens after a concept exists.

It ships as an MCP server. Persei has no MCP client, and the server's four
tools are thin wrappers over four local Node modules, so this calls the
modules through services/logoloom/bridge.mjs rather than adding a protocol to
reach functions already on disk. Three of the four are used as they are; the
tracer is re-implemented there, because upstream's shells out with the
caller's path in the command string and passes flag names the current vtracer
no longer has. See that file's header.

`logo_render` is not upstream's. A model writing SVG is working blind in
exactly the way a model editing CSS was before preview_app: the tests cannot
tell it whether the mark looks like anything, and neither can the reviewer.
sharp is already here to rasterise, so the render goes to the vision model
and comes back as a description. Design without it is guessing.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os

from langchain_core.tools import tool

from agent import paths
from agent.tools.files import PathEscapeError, _resolve
from agent.tools.tool_errors import tool_errors_to_text

logger = logging.getLogger("tektonix")

BRIDGE = paths.REPO_ROOT / "services" / "logoloom" / "bridge.mjs"

# A brand-kit export rasterises ~20 files through sharp; the rest are fast.
_TIMEOUT_S = 180
# What comes back from a bridge call. An SVG is text, but a kit listing plus
# an error string has no business being unbounded either.
_MAX_OUTPUT = 4_000_000


class LogoLoomUnavailable(RuntimeError):
    pass


def installed() -> bool:
    """Whether LogoLoom's own dependencies are present.

    The bridge is committed; what a fresh clone lacks is node_modules, and
    `npm ci` in services/logoloom is deliberately optional in install.sh --
    the agent is perfectly usable without logo tools, and failing an install
    over a feature most people never touch would be the wrong trade. So a
    seat is given these tools only when they would actually work, rather than
    four that can only ever fail.
    """
    return BRIDGE.is_file() and (BRIDGE.parent / "node_modules" / "@mcpware" / "logoloom").is_dir()


async def _call(op: str, args: dict) -> dict:
    """One bridge process per call. Returns the parsed JSON, always a dict."""
    if not BRIDGE.is_file():
        raise LogoLoomUnavailable(
            f"LogoLoom is not installed at {BRIDGE} -- run `npm install` in services/logoloom"
        )
    proc = await asyncio.create_subprocess_exec(
        "node", str(BRIDGE), json.dumps({"op": op, "args": args}),
        cwd=str(BRIDGE.parent),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise LogoLoomUnavailable(f"{op} did not finish within {_TIMEOUT_S}s") from None
    if not out:
        detail = (err or b"").decode("utf-8", "replace").strip()[-400:]
        raise LogoLoomUnavailable(f"{op} produced no output: {detail or 'no error text'}")
    try:
        parsed = json.loads(out[:_MAX_OUTPUT].decode("utf-8", "replace"))
    except ValueError:
        raise LogoLoomUnavailable(f"{op} returned something that is not JSON") from None
    return parsed if isinstance(parsed, dict) else {"success": False, "error": "unexpected reply"}


def _svg_arg(svg: str) -> str | None:
    """A refusal, or None when the argument looks like an SVG.

    Cheap, and it saves a subprocess: the common mistake is passing a file
    path where the content goes, and upstream's answer to that is a stack
    trace about a missing root element.
    """
    text = (svg or "").strip()
    if not text:
        return "ERROR: give the SVG source itself, not a path -- this tool takes the markup"
    if "<svg" not in text.lower():
        return (
            "ERROR: that does not contain an <svg> element. Pass the SVG SOURCE "
            "(the markup you wrote), not a filename."
        )
    return None


def _drop_traced_background(svg: str) -> tuple[str, str | None]:
    """Remove the canvas a tracer turns a logo's background into.

    VTracer stacks shapes bottom-up, and the first is the image's background:
    a path from the origin across the whole canvas. Left in, a logo on a white
    PNG traces to a white box -- it cannot sit on a dark header, and a glow
    behind it is hidden. Found 2026-09-23 on a real logo: the first of 251
    paths, #FDFDFD from 0,0. Only a LIGHT first path from the origin is taken;
    anything else is left exactly as traced. Returns (svg, the colour removed
    or None)."""
    import re  # noqa: PLC0415

    m = re.search(r'<path\b[^>]*\bd="M\s*0[ ,]+0[^"]*"[^>]*/>', svg)
    first = re.search(r"<path\b[^>]*/>", svg)
    if not m or not first or m.start() != first.start():
        return svg, None
    fill = re.search(r'fill="#([0-9a-fA-F]{6})"', m.group(0))
    if not fill or min(int(fill.group(1)[i:i + 2], 16) for i in (0, 2, 4)) < 0xE8:
        return svg, None
    return svg[:m.start()] + svg[m.end():], f"#{fill.group(1).upper()}"


def _fmt(op: str, result: dict) -> str:
    if not result.get("success"):
        return f"ERROR: {op} failed: {result.get('error') or 'no reason given'}"
    return ""


# ---------------------------------------------------------------------------
# the tools
# ---------------------------------------------------------------------------

# How many times logo_render may run before the operator has been shown
# anything (agent/tools/show_tools.py resets the count). Found 2026-09-23, the
# second session in a row: a planner rendered one logo twenty times, asking the
# vision model "is it a faceted diamond?" and tweaking on each hedged answer --
# a question of taste no render can settle -- and showed the operator nothing.
# Checking your own work takes a render or two; past that it is a decision, and
# decisions are the operator's.
RENDERS_BEFORE_SHOWING = 3


def new_show_state() -> dict:
    """Shared by one seat's logo_render and show_images: renders since the
    operator last saw anything. Built per turn, so each message resets it."""
    return {"renders_since_show": 0}


# An SVG longer than this comes back as a draft id, not as markup: writing it
# back out costs the model roughly a token per three bytes (agent/artifacts.py).
INLINE_SVG_LIMIT = 6_000


def make_logo_tools(root_for_writes=None, *, can_export: bool = True, show_state: dict | None = None,
                    repo: str | None = None) -> list:
    """The logo toolset.

    `root_for_writes` is a callable returning the directory writes are
    confined to -- a build task's own workspace. `can_export` is False for a
    seat that must not write into a repository: the planning chat mutates
    nothing by contract (agent/tools/planning_tools.py), and a brand kit is
    two dozen binary files with nowhere to go there. Planning designs the
    mark and carries the SVG in the plan; the build task exports it.

    Returns [] when LogoLoom is not installed -- see `installed()`.
    """
    if not installed():
        return []

    from agent import artifacts  # noqa: PLC0415

    def _source(svg: str, draft: str) -> tuple[str | None, str | None]:
        """(markup, None) from `draft` or `svg`, or (None, an error to return)."""
        if (draft or "").strip():
            if not repo:
                return None, "ERROR: drafts are not available here -- pass the svg markup"
            text = artifacts.load_draft(repo, draft)
            if text is None:
                return None, f"ERROR: there is no draft {draft!r} -- use an id a logo tool returned"
            return text, None
        bad = _svg_arg(svg)
        return (None, bad) if bad else (svg, None)

    def _hand_back(svg_out: str, label: str) -> str:
        """Small SVG inline (and saved); big SVG as a draft id only."""
        draft_id = None
        if repo:
            try:
                draft_id = artifacts.save_draft(repo, svg_out, note=label)
            except artifacts.ArtifactError as e:
                logger.warning("could not save a logo draft: %s", e)
        if draft_id and len(svg_out) > INLINE_SVG_LIMIT:
            return (f"{label}: {len(svg_out):,} bytes of SVG, saved as draft {draft_id}. The markup is "
                    f"not repeated here -- writing it back out would take minutes. Pass "
                    f"draft=\"{draft_id}\" to logo_render, logo_compose, show_images, "
                    f"logo_text_to_path, logo_optimize_svg or the export, and name the draft in the plan.")
        return f"{label}" + (f" (saved as draft {draft_id})" if draft_id else "") + f".\n\n{svg_out}"

    @tool
    @tool_errors_to_text
    async def logo_text_to_path(svg: str = "", font_path: str = "", draft: str = "") -> str:
        """Convert every <text> in an SVG logo to outlined <path>.

        Do this before shipping any logo with type in it. A <text> element
        renders in whatever font the viewer happens to have, so a wordmark
        that looks right here looks like Times New Roman on a machine without
        the font -- and on every PNG exported from it. Outlines have no such
        dependency.

        `svg` is the markup itself, not a path -- or pass `draft`, the id of a
        saved draft. `font_path` is an optional .ttf/.otf; the bundled Inter is
        used when it is empty.

        Returns the converted SVG (or, when it is large, a draft id). Run
        logo_optimize_svg on the result.
        """
        svg, bad = _source(svg, draft)
        if bad:
            return bad
        r = await _call("text_to_path", {"svg": svg, "fontPath": font_path or None})
        err = _fmt("text_to_path", r)
        if err:
            return err
        return _hand_back(r.get("svg", ""), f"Converted {r.get('convertedCount', 0)} <text> element(s)")

    @tool
    @tool_errors_to_text
    async def logo_optimize_svg(svg: str = "", aggressive: bool = False, draft: str = "") -> str:
        """Clean up an SVG: drop metadata, merge paths, shorten coordinates.

        `svg` is the markup itself, or pass `draft`. `aggressive` also strips
        class and style attributes -- fine for a finished logo, wrong for an
        SVG something else is styling.

        Returns the optimised SVG (or a draft id when large) and what it saved.
        """
        svg, bad = _source(svg, draft)
        if bad:
            return bad
        r = await _call("optimize_svg", {"svg": svg, "aggressive": aggressive})
        err = _fmt("optimize_svg", r)
        if err:
            return err
        return _hand_back(r.get("svg", ""), f"{r.get('originalSize')} -> {r.get('optimizedSize')} bytes "
                                            f"({r.get('savedPercent')} smaller)")

    @tool
    @tool_errors_to_text
    async def logo_render(svg: str = "", question: str = "", width: int = 512,
                          height: int = 512, background: str = "", draft: str = "") -> str:
        """LOOK at an SVG. Renders it and describes what it actually shows.

        Use this on every concept before you offer it to anyone. You are
        writing coordinates; this is the only way to find out whether they
        add up to a mark rather than to overlapping shapes, clipped strokes,
        or text sitting outside its own viewBox.

        `svg` is the markup itself, or pass `draft`. `question` narrows the description -- ask
        about the thing you are unsure of ("is the monogram centred?", "does
        the wordmark overlap the icon?"). `background` is the hex colour it is
        rendered ON -- white when empty. Check a mark on a dark colour too.

        This is for YOU to check your work. It shows the operator nothing:
        to put a design in front of them, use show_images.

        Returns a description of the rendered image.
        """
        svg, bad = _source(svg, draft)
        if bad:
            return bad
        if show_state is not None:
            if show_state["renders_since_show"] >= RENDERS_BEFORE_SHOWING:
                return (
                    f"STOP RENDERING. You have rendered {show_state['renders_since_show']} times "
                    f"without showing the operator anything. Call show_images NOW with your current "
                    f"version (and the original, for comparison), say in one or two sentences what "
                    f"you changed, and ask what they want. Whether it looks right is their call, "
                    f"not the vision model's -- do not keep adjusting to its opinions."
                )
            show_state["renders_since_show"] += 1
        args = {"svg": svg, "width": max(16, min(int(width or 512), 2048)),
                "height": max(16, min(int(height or 512), 2048))}
        # White unless told otherwise: a transparent render is judged against
        # whatever background the vision model assumes, which is not an answer.
        args["background"] = background or "#ffffff"
        r = await _call("render_png", args)
        err = _fmt("render_png", r)
        if err:
            return err
        png = base64.b64decode(r["pngBase64"])
        from agent.tools.vision import describe_image_bytes  # noqa: PLC0415

        prompt = question or (
            "Describe this logo: the shapes, the text if any, the colours, and anything "
            "that looks like a mistake -- clipping, overlap, uneven spacing, elements "
            "outside the frame."
        )
        return await describe_image_bytes(png, "image/png", prompt)

    @tool
    @tool_errors_to_text
    async def logo_trace_image(image_path: str, color_mode: str = "color",
                               precision: int = 6, keep_background: bool = False) -> str:
        """Turn an existing raster logo (PNG/JPG) into SVG paths.

        For "here is our current logo, make it a vector". `image_path` is
        repo-relative. `color_mode` is "color" or "binary" (binary is a
        single-colour silhouette). Tracing a photograph produces thousands of
        useless paths; this is for flat marks. The image's own light background
        is dropped, so the logo is transparent and sits on any colour -- pass
        keep_background=True to keep it.

        Returns the traced SVG -- as a draft id, since a trace is large. Expect
        to clean it up afterwards -- a trace is a starting point, not a
        finished logo, and it will have more paths and more colours than
        anything you would draw by hand. Add effects with logo_compose; do not
        retype it.
        """
        if root_for_writes is None:
            return "ERROR: this seat has no project to read images from"
        try:
            target = _resolve(root_for_writes(), image_path)
        except PathEscapeError as e:
            return f"ERROR: {e}"
        if not target.is_file():
            return f"ERROR: {image_path!r} is not a file in this project"
        r = await _call("image_to_svg", {
            "imagePath": str(target),
            "colorMode": "binary" if color_mode == "binary" else "color",
            "precision": max(1, min(int(precision or 6), 10)),
        })
        err = _fmt("image_to_svg", r)
        if err:
            return err
        traced = r.get("svg", "")
        label = f"Traced {image_path}"
        if not keep_background:
            traced, dropped = _drop_traced_background(traced)
            if dropped:
                label += f" (its {dropped} background removed, so it is transparent)"
        return _hand_back(traced, label)

    @tool
    @tool_errors_to_text
    async def logo_compose(draft: str, defs: str = "", group_attributes: str = "", before: str = "",
                           after: str = "", svg_attributes: str = "") -> str:
        """Add to a draft WITHOUT retyping it: effects, a backdrop, extra shapes.

        The draft's own content is wrapped, untouched, in a <g>, so a 95 KB
        trace gets lighting in a few lines:

          defs: filters and gradients, e.g.
            '<filter id="glow"><feGaussianBlur stdDeviation="4" result="b"/>'
            '<feMerge><feMergeNode in="b"/><feMergeNode in="SourceGraphic"/></feMerge></filter>'
          group_attributes: applied to the wrapped logo, e.g. 'filter="url(#glow)"'
          before / after: markup drawn behind / in front of it (a highlight,
            a sheen, a backdrop), in the draft's own coordinates
          svg_attributes: extra attributes for the root <svg>, e.g. a viewBox

        Returns the id of a NEW draft; the original is unchanged, so you can
        show both.
        """
        import re  # noqa: PLC0415

        base, bad = _source("", draft)
        if bad:
            return bad
        m = re.search(r"<svg\b([^>]*)>(.*)</svg>", base, re.S | re.I)
        if not m:
            return f"ERROR: draft {draft} has no <svg> ... </svg> to wrap"
        attrs, inner = m.group(1), m.group(2)
        if "xmlns=" not in attrs:
            attrs += ' xmlns="http://www.w3.org/2000/svg"'
        extra = f" {svg_attributes.strip()}" if svg_attributes.strip() else ""
        out = (f"<svg{attrs}{extra}><defs>{defs}</defs>{before}"
               f"<g {group_attributes.strip()}>{inner}</g>{after}</svg>")
        return _hand_back(out, f"Composed onto draft {draft}")

    tools = [logo_text_to_path, logo_optimize_svg, logo_render, logo_trace_image, logo_compose]
    if not can_export:
        return tools

    @tool
    @tool_errors_to_text
    async def logo_export_brand_kit(svg: str = "", output_dir: str = "brand", name: str = "",
                                    primary: str = "", secondary: str = "",
                                    dark_svg: str = "", draft: str = "") -> str:
        """Export a finished SVG logo as a full brand kit, into this project.

        Writes roughly two dozen files: the logo and icon and wordmark as SVG
        in light, dark and single-colour variants, PNGs from 16px to 1024px,
        favicon.ico, WebP, an OG image, a GitHub social preview, a Twitter
        header, and a BRAND.md of the colours and usage.

        Do this LAST, once you have rendered the mark and it is right --
        every one of those files is the same logo, so exporting a bad one
        just makes two dozen copies of the problem.

        `svg` is the finished markup, with its text already converted by
        logo_text_to_path -- or `draft`, the id of the agreed draft (the plan
        names it when the logo was chosen in planning). `output_dir` is repo-relative (for example
        "brand" or "public/brand"). `name` is the brand name. `primary` and
        `secondary` are hex colours for BRAND.md. `dark_svg` is an optional
        dark-mode variant; without one the dark files are derived.

        Returns the list of files written.
        """
        svg, bad = _source(svg, draft)
        if bad:
            return bad
        if root_for_writes is None:
            return "ERROR: this seat has no project to write into"
        if not (name or "").strip():
            return "ERROR: give the brand name -- it goes in BRAND.md and the filenames"
        try:
            out = _resolve(root_for_writes(), output_dir or "brand")
        except PathEscapeError as e:
            return f"ERROR: {e}"
        if out.exists() and not out.is_dir():
            return f"ERROR: {output_dir!r} is a file, not a directory"
        os.makedirs(out, exist_ok=True)

        colors = {k: v for k, v in (("primary", primary), ("secondary", secondary)) if v}
        args = {"svg": svg, "outputDir": str(out), "name": name}
        if colors:
            args["colors"] = colors
        if dark_svg.strip():
            args["darkSvg"] = dark_svg
        r = await _call("export_brand_kit", args)
        err = _fmt("export_brand_kit", r)
        if err:
            return err
        files = r.get("files") or []
        listing = "\n".join(f"  {output_dir.rstrip('/')}/{f}" for f in files)
        return (f"Wrote {len(files)} files to {output_dir!r}:\n{listing}\n\n"
                f"These are binary assets in the repo -- commit them with the change that "
                f"uses them, and say in the final summary that the frontend needs them.")

    return [*tools, logo_export_brand_kit]
