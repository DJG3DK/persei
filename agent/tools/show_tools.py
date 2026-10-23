"""Showing the operator an image, in the conversation.

logo_render, describe_image and preview_app let the agent LOOK at something:
the pixels go to a vision model and a description comes back. None of them
shows the operator anything, and a design question is one they answer by
looking. This does: each image is stored (agent/artifacts.py) and comes back as
a Markdown image the dashboard draws in the chat. See agent/artifacts.py for
the 2026-09-23 session this exists because of.
"""
from __future__ import annotations

import base64
import logging
from collections.abc import Callable
from typing import Any

from langchain_core.tools import BaseTool, tool

from agent import artifacts
# files.py's sandbox resolver and logo_tools' LogoLoom call, both shared on
# purpose rather than duplicated.
# pyright: reportPrivateUsage=false
from agent.tools.files import PathEscapeError, _resolve, read_bytes
from agent.tools.tool_errors import tool_errors_to_text

logger = logging.getLogger("tektonix")

MAX_IMAGES = 8


async def _render_svg(svg: str, background: str, width: int, height: int) -> bytes:
    from agent.tools.logo_tools import _call, installed  # noqa: PLC0415
    if not installed():
        raise artifacts.ArtifactError("rendering SVG needs LogoLoom (services/logoloom), which is not installed")
    args = {"svg": svg, "width": width, "height": height}
    if background:
        args["background"] = background
    r = await _call("render_png", args)
    if not r.get("success"):
        raise artifacts.ArtifactError(f"could not render the SVG: {str(r.get('error'))[:300]}")
    return base64.b64decode(r["pngBase64"])


def make_show_images_tool(repo: str, root_for_reads: Callable[[], str], *,
                          show_state: dict[str, Any] | None = None) -> BaseTool:
    """`root_for_reads` returns the directory `path` images are read from --
    the task's workspace, or the project's for planning."""

    @tool
    @tool_errors_to_text
    async def show_images(images: list[dict[str, Any]]) -> str:
        """SHOW the operator images, in the chat -- the only tool that does.

        logo_render and describe_image only tell YOU what something looks
        like; the operator sees nothing. Use this whenever they asked to see
        something (logo concepts, a before/after, options to choose between),
        and whenever a decision is theirs to make by looking. Show the real
        options side by side and let them choose -- never approve a design on
        their behalf.

        `images` is a list (at most 8) of objects, each with:
          "caption": what this one is -- "A: traced from your current logo",
          and ONE of
          "draft": the id of a saved SVG draft (what the logo tools return for
                   anything large -- never retype a big SVG, pass its draft), or
          "svg": SVG markup, rendered to an image here, or
          "path": a repo-relative image file (PNG, JPEG, GIF, WebP or SVG).
        Optional per image: "background" (hex, e.g. "#1e1338" -- show a mark
        on light AND dark when that matters; transparent when empty),
        "width" and "height" in pixels (default 800 x 400).

        Returns one Markdown image line per image. They are in the chat
        already; repeat those lines in your reply next to what you say about
        each one, so the pictures stay with your words.
        """
        # The tool schema already demands a list of objects; these checks are
        # for a caller that skips it.
        if not isinstance(images, list) or not images:  # pyright: ignore[reportUnnecessaryIsInstance]
            return "ERROR: pass a list of images, each with a caption and an svg or a path"
        if len(images) > MAX_IMAGES:
            return f"ERROR: at most {MAX_IMAGES} images at a time -- show the strongest options"
        lines: list[str] = []
        problems: list[str] = []
        for i, item in enumerate(images, 1):
            if not isinstance(item, dict):  # pyright: ignore[reportUnnecessaryIsInstance]
                problems.append(f"image {i}: not an object")
                continue
            caption = str(item.get("caption") or f"Image {i}").replace("]", ")").replace("\n", " ")[:200]
            try:
                width = max(64, min(int(item.get("width") or 800), 2048))
                height = max(64, min(int(item.get("height") or 400), 2048))
                background = str(item.get("background") or "")
                if item.get("draft"):
                    markup = artifacts.load_draft(repo, str(item["draft"]))
                    if markup is None:
                        raise artifacts.ArtifactError(f"there is no draft {item['draft']!r}")
                    data = await _render_svg(markup, background, width, height)
                    source = f"draft {item['draft']}"
                elif item.get("svg"):
                    data = await _render_svg(str(item["svg"]), background, width, height)
                    source = "svg"
                elif item.get("path"):
                    target = _resolve(root_for_reads(), str(item["path"]))
                    try:
                        raw = read_bytes(root_for_reads(), str(item["path"]))
                    except OSError:
                        raise artifacts.ArtifactError(
                            f"{item['path']!r} is not a file in this project") from None
                    if target.suffix.lower() == ".svg" or b"<svg" in raw[:2000]:
                        data = await _render_svg(raw.decode("utf-8", "replace"), background, width, height)
                    else:
                        data = raw
                    source = str(item["path"])
                else:
                    raise artifacts.ArtifactError("give a draft, an svg or a path")
                url = artifacts.save(repo, data, caption=caption, source=source)
            except (artifacts.ArtifactError, PathEscapeError, ValueError) as e:
                problems.append(f"image {i} ({caption}): {e}")
                continue
            lines.append(f"![{caption}]({url})")
        if lines and show_state is not None:
            show_state["renders_since_show"] = 0      # the operator has seen it (logo_tools)
        out = "\n".join(lines)
        if problems:
            out += ("\n\n" if out else "") + "Not shown:\n" + "\n".join(f"- {p}" for p in problems)
        if lines:
            out += ("\n\nShown in the chat. Repeat these lines in your reply beside what you say about "
                    "each, then ask the operator to choose.")
        return out

    return show_images
