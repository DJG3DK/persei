"""Splitting a project's AGENTS.md into sections, and deciding which of them
stay in the prompt.

Project memory is loaded whole into every seat's system prompt, on every
model call, of every task. That was right while it was small. Measured on
2026-09-21 the largest project's memory was 41,699 chars (~10,424 tokens)
across 14 `##` sections, a median 108 model calls per task, and one of those
sections -- relevant to perhaps one task in ten -- was 55% of the file. The
file also only ever grows: the nightly consolidator is asked for everything
still true, plus what is new.

So memory becomes what skills already are here: a small always-resident part
plus an index of what can be fetched. The operator's rule for which is which
is implemented in `stays_always` below, and the reasoning for it is worth
keeping next to the code, because it is not the
obvious rule.

Everything here is text in, text out -- no store, no backend, no IO. The
reader (agent/project_memory.py) and the migration that writes the section files
both drive these functions. That is what makes the one property this whole
subsystem rests on -- the preamble plus every section body, joined in index
order, IS the original file, byte for byte -- something a unit test can
assert instead of something a live database has to be trusted for.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace

# Agent-visible paths, mirroring /skills/_manifest.json + /skills/<name>/SKILL.md.
# The store keys are these with the route prefix stripped, which is what
# route_local_path (agent/project_memory.py) is for -- app code that uses the full
# path as a raw key writes somewhere the agent's own file tools can never read.
SECTIONS_DIR = "/memories/sections/"
SECTIONS_INDEX_PATH = SECTIONS_DIR + "_index.json"
SECTIONS_CORE_PATH = SECTIONS_DIR + "_core.md"

# Rule 0: a memory smaller than this is not split at all.
#
# The index costs prompt tokens and a section costs a tool call, so below some
# size the machinery costs more than the file it is saving. Two of the four
# live projects are under this today; they get nothing, which is the right
# outcome, and they start splitting by themselves if they grow past it. It
# also means a brand-new project, whose memory is a starter stub, is
# unaffected -- onboarding does not change shape.
SPLIT_FLOOR_CHARS = 8_000

# Rule 1's size clause. A section has to be small to be worth pinning: a
# 10,000-char "Conventions" is important by any measure, but pinning it would
# spend most of the saving on one section, and a convention is something the
# agent knows it is about to need the moment it starts writing code -- the
# case on-demand retrieval handles well.
ALWAYS_MAX_CHARS = 2_500

# What the whole rendered block may cost: preamble + pinned sections + the
# index itself, in tokens.
#
# This is a budget and not a cap on the pinned sections alone, which is what
# it was first written as (12,000 chars = ~3,000 tok of bodies) -- above the
# 2,500 the design is built to hit before the index was even charged, so the
# floor this subsystem exists to create was not actually a floor. The index
# has to be inside the number for the number to mean anything: on a 15-section
# memory it is ~676 tok on its own, a quarter of the budget, and it grows with
# every section the consolidator invents while the pinned set does not.
INLINE_TOKEN_BUDGET = 2_500

# The same chars/4 approximation the sizing above was measured with. Good
# enough to print in an index entry; nothing decides anything on it.
CHARS_PER_TOKEN = 4

_INDEX_HEADER = "--- MEMORY INDEX ---"


@dataclass(frozen=True)
class Section:
    """One `##` section. `body` includes its own heading line and every byte
    up to the next heading, so joining bodies reproduces the source exactly --
    a section is a SLICE of the file, never a reformatting of it."""

    title: str
    slug: str
    body: str

    @property
    def chars(self) -> int:
        return len(self.body)


@dataclass(frozen=True)
class IndexEntry:
    """What the prompt advertises about a section it is not carrying.

    `summary` is the whole bet: a section nobody reads is a section lost, and
    the index line is the only thing standing between the two. It says what is
    IN the section and when to read it, never the title again.
    """

    slug: str
    title: str
    summary: str
    chars: int = 0
    always: bool = False
    reason: str = ""

    @property
    def tokens(self) -> int:
        return estimate_tokens_from_chars(self.chars)

    def to_dict(self) -> dict:
        return {"slug": self.slug, "title": self.title, "summary": self.summary,
                "chars": self.chars, "tokens": self.tokens, "always": self.always,
                "reason": self.reason}


def estimate_tokens_from_chars(chars: int) -> int:
    return max(1, round(chars / CHARS_PER_TOKEN)) if chars else 0


def estimate_tokens(text: str) -> int:
    return estimate_tokens_from_chars(len(text))


def source_digest(text: str) -> str:
    """The checksum the index records for the file it was cut from.

    Written by the migration, checked by the reader. /memories/AGENTS.md stays
    the authoritative copy until the pointer-stub commit, and two other
    writers reach it in the meantime -- the nightly consolidator rewrites the
    whole file, and the agent's own file-edit tool appends to it. Once
    sections exist, neither of those writes is read by any prompt, so without
    this the subsystem's own failure mode is the one it was built to prevent:
    memory that quietly stops being there. A digest that no longer matches
    means "someone wrote to the source since the split", and the reader can
    then fall back to the source instead of serving a stale copy of it.

    One function so the two ends cannot disagree about encoding, which is the
    way a checksum comparison usually goes wrong.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- splitting -------------------------------------------------------------


def split_sections(text: str) -> tuple[str, list[Section]]:
    """(preamble, sections). The preamble is everything before the first
    `##` heading, and is usually two or three lines saying what the project
    is -- it is never indexed away.

    Headings inside a fenced code block do not start a section: memory files
    quote markdown at each other (a skill template, an example AGENTS.md
    entry), and splitting there would produce a section whose body is the
    back half of someone's code fence. Reassembly would still be exact --
    that is a property of slicing, not of where the cuts are -- but the
    sections would be nonsense.
    """
    lines = text.splitlines(keepends=True)
    fence = ""
    starts: list[int] = []
    for n, line in enumerate(lines):
        stripped = line.lstrip()
        if fence:
            if stripped.startswith(fence):
                fence = ""
            continue
        opening = re.match(r"(```|~~~)", stripped)
        if opening:
            fence = opening.group(1)
            continue
        if line.startswith("## "):
            starts.append(n)

    if not starts:
        return text, []

    preamble = "".join(lines[:starts[0]])
    sections: list[Section] = []
    used: set[str] = set()
    bounds = [*starts, len(lines)]
    for start, end in zip(bounds, bounds[1:], strict=False):
        body = "".join(lines[start:end])
        title = lines[start][3:].strip().rstrip("#").strip()
        sections.append(Section(title=title, slug=slugify(title, used), body=body))
    return preamble, sections


def render_full(preamble: str, sections: list[Section]) -> str:
    """The inverse of split_sections, and the reason `body` keeps its heading.
    What read_memory_section("all") returns once /AGENTS.md is a stub."""
    return preamble + "".join(s.body for s in sections)


_MAX_SLUG_CHARS = 60


def slugify(title: str, used: set[str] | None = None) -> str:
    """A store-key-safe name for a heading. `used` is mutated: two sections
    genuinely can share a title (two "Notes" headings a year apart), and the
    second one must not silently overwrite the first's file."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    if len(slug) > _MAX_SLUG_CHARS:
        # On a word boundary, because a slug is something a model retypes
        # into read_memory_section from an index line it read a thousand
        # tokens ago. A long heading used to come out cut mid-word
        # ("...tests-silentl"), which reads as a typo and invites a guess at
        # what it "should" say -- and a guessed slug is a section not read.
        slug = slug[:_MAX_SLUG_CHARS].rsplit("-", 1)[0] or slug[:_MAX_SLUG_CHARS]
    slug = slug.rstrip("-") or "section"
    if used is None:
        return slug
    candidate, n = slug, 1
    while candidate in used:
        n += 1
        candidate = f"{slug}-{n}"
    used.add(candidate)
    return candidate


def is_worth_splitting(text: str) -> bool:
    """Rule 0. Two sections minimum as well as the size floor: one section
    plus an index entry pointing at it is strictly worse than the file."""
    if len(text) < SPLIT_FLOOR_CHARS:
        return False
    _, sections = split_sections(text)
    return len(sections) >= 2


# --- what stays in the prompt ----------------------------------------------

# Rule 1's second clause, and the part that is not obvious. The question is
# NOT "is this important" -- importance selects everything, which is where
# this started. The question that discriminates is: if the agent never reads
# this, does it find out?
#
#   * A build gotcha announces itself. The build breaks, the agent reads the
#     error and goes looking. It can be on demand.
#   * A test-wiring rule does not. The suite passes because the tests never
#     ran, and nothing says so.
#   * A collision trap is, by definition, something you fall into without
#     knowing the section existed. A model does not search for a trap it has
#     never met.
#   * Sandbox constraints fail as mysterious rather than silent, and the
#     recovery costs whole iterations.
#
# Matched on the heading first, because a heading is what the author chose to
# call the thing, and only then on the body's own admission that it fails
# quietly.
#
# Order counts where two families share a word: "environment" belongs to both
# the configuration list and the sandbox one, and a section headed
# "Environment variables and secrets" is the first of those. The reason is only
# ever shown to the operator, but a reason that names the wrong family is how
# an always-block gets argued about instead of adjusted.
_SILENT_FAMILIES: dict[str, tuple[str, ...]] = {
    "test wiring -- a suite that never ran still passes": (
        "test", "pytest", "spec", "suite", "coverage", "fixture", "conftest", "assert",
    ),
    "configuration -- wrong config usually no-ops rather than raising": (
        "secret", "secrets", "credential", "credentials", "config", "configuration", "token", "tokens",
    ),
    "sandbox and environment constraints -- these fail as mysterious": (
        "sandbox", "environment", "env", "container", "docker", "workspace",
        "permission", "offline", "network", "runtime", "venv",
    ),
    "traps -- you do not go looking for one you have not met": (
        "trap", "pitfall", "collision", "conflict", "caution", "careful",
        "naming", "warning",
    ),
    # A number from the wrong source is the purest silent failure there is:
    # it arrives looking exactly like the right one, nothing objects, and the
    # answer is quoted to a human as fact. The section that says which source
    # is authoritative is therefore worth its residency even though nothing
    # in it reads like a trap -- it is the reason the agent would ever doubt
    # a plausible number.
    "authority -- the wrong source gives a plausible number and nothing objects": (
        "authoritative", "canonical", "ledger", "billing", "billed", "invoice",
        "figures", "truth",
    ),
}

# The same idea, for two families whose titles are too easy to match.
#
# Rule 1 has two clauses -- small, AND fails silently -- and a title-only
# match evaluates one of them. "convention", "style", "rule", "standard" or
# "migration" in a heading is close to a free pin, which inverts the whole
# selection: on the repo's own fixture a 224-char duplicate "Conventions"
# stub was pinned while the real 2,664-char Conventions was indexed, because
# the size clause was the only thing discriminating and the junk section was
# smaller. So for these two the body has to corroborate: the section itself
# has to say, somewhere, that getting this wrong does not announce itself.
# The strong families above keep title-only matching, because there the
# argument is about the category -- a test-wiring section is silent by what
# it is about, whatever its prose sounds like.
_CORROBORATED_FAMILIES: dict[str, tuple[str, ...]] = {
    "conventions -- a violation ships and nothing objects": (
        "convention", "conventions", "style", "house", "standard", "standards", "rule", "rules",
    ),
    "wiring and ordering -- the failure arrives after the step that caused it": (
        "wiring", "deploy", "deployment", "restart", "startup", "boot", "migration", "ordering",
    ),
}

# A section this short that names no repo path and never says it fails
# quietly is a note, not a rule -- it cannot be carrying a constraint worth a
# permanent place in every prompt of every call, and admitting them is how the
# always-block fills up with consolidation leftovers (the duplicate-heading
# stub in the fixture is exactly this shape). A short section that DOES say it
# fails quietly is the cheapest pin there is, so the size never overrides its
# own admission.
_STUB_CHARS = 300

# What a section says about itself when it knows it fails quietly. Only
# consulted when the heading matched nothing, and then only on the strength of
# its own framing: ONE of these words somewhere in two thousand characters is
# an aside, not a subject. A section earns a pin by opening with it, or by
# coming back to it in a different word -- otherwise the section on supplier
# pagination that happens to mention one silent mapping bug gets pinned for
# the life of the project.
_OPENING_CHARS = 400
# "gotcha" and "footgun" are deliberately not here, and not in the traps
# family either. The operator's policy uses a build gotcha as its worked
# example of something that must NOT be pinned: it fails loudly, the agent
# reads the error and goes looking. The word was pinning "Lint gotchas" --
# a real heading -- into every prompt of every call, forever, which is the
# opposite of what the policy argues for. A trap, a collision or a pitfall is
# something you fall into without knowing the section existed; a gotcha
# announces itself. A specific gotcha section that must be resident is what
# the operator override is for.
_SILENT_BODY_SIGNALS = (
    "silently", "quietly", "without warning", "no error", "does not error",
    "nothing tells you", "you will not notice", "easy to miss", "still passes",
    "passes anyway",
)


def _title_words(section: Section) -> set[str]:
    return set(re.findall(r"[a-z]+", section.title.lower()))


def _matches(words: set[str], patterns: tuple[str, ...]) -> bool:
    return any(word.startswith(p) for word in words for p in patterns)


def _body_signals(section: Section) -> bool:
    """Whether the section says of itself that it fails quietly.

    ONE of these words somewhere in two thousand characters is an aside, not a
    subject. A section earns this by opening with it, or by coming back to it
    in a different word -- otherwise the section on supplier pagination that
    happens to mention one silent mapping bug gets pinned for the life of the
    project.
    """
    lowered = section.body.lower()
    hits = [signal for signal in _SILENT_BODY_SIGNALS if signal in lowered]
    opening = lowered[:_OPENING_CHARS]
    return len(hits) >= 2 or any(signal in opening for signal in hits)


def always_reason(section: Section) -> str:
    """Why this section is pinned, or "" if it is indexed. The migration's dry
    run prints this so the operator can argue with the classification before
    any of it is written, which is the point of having a reason at all."""
    if section.chars > ALWAYS_MAX_CHARS:
        return ""
    corroborated = _body_signals(section)
    if section.chars < _STUB_CHARS and not cited_paths(section.body) and not corroborated:
        return ""
    words = _title_words(section)
    for reason, patterns in _SILENT_FAMILIES.items():
        if _matches(words, patterns):
            return reason
    for reason, patterns in _CORROBORATED_FAMILIES.items():
        if _matches(words, patterns) and corroborated:
            return reason
    if corroborated:
        return "the section says itself that this one fails quietly"
    return ""


def stays_always(section: Section) -> bool:
    return bool(always_reason(section))


def choose_always(sections: list[Section], *, overhead_chars: int = 0,
                  budget_tokens: int = INLINE_TOKEN_BUDGET) -> list[str]:
    """The slugs pinned into the prompt, in document order, within what is
    left of the budget once the preamble and the index are paid for.

    Order is the file's own, not size or score: a memory reads as an argument,
    and reordering it mid-prompt makes it read as a list of unrelated
    assertions. A section that does not fit is skipped rather than ending the
    loop, because the next qualifying one may be small enough -- and the whole
    point is to hold a floor, not to fill it in order.

    `overhead_chars` is what the block costs before a single section is
    pinned. A memory with enough sections that the index alone spends the
    budget pins nothing, which is the honest answer: the index is what makes
    every section reachable, and there is no version of this where dropping it
    to keep a pinned body is the better trade.
    """
    room = budget_tokens * CHARS_PER_TOKEN - overhead_chars
    chosen: list[str] = []
    spent = 0
    for section in sections:
        if not stays_always(section):
            continue
        if spent + section.chars > room:
            continue
        chosen.append(section.slug)
        spent += section.chars
    return chosen


# --- the index -------------------------------------------------------------

# Longest extension first, and a lookahead after it: with `js` ahead of
# `json` in the alternation, `package.json` came out of here as "package.js",
# and an index entry that tells a model to read a file that does not exist is
# worse than one that says nothing.
_PATH_RE = re.compile(
    r"(?<![\w/.\-])((?:[\w.-]+/)+[\w.-]+\.[A-Za-z]{1,6}"
    r"|[\w-]+\.(?:tsx|jsx|json|yaml|toml|yml|mjs|cjs|env|cfg|ini|sql|scss|lock"
    r"|html|css|txt|sh|md|py|ts|js|rs|go|rb)"
    r"|Dockerfile|Makefile)"
    r"(?![A-Za-z0-9])"
)

# A hyphen counts as "inside a token" in the lookbehind above, which is what
# keeps a URL out of the trigger: a memory citing
# https://github.com/github/codeql-action/releases/download/... was yielding
# "action/releases/download/codeql-bundle-linux64.tar.gz" as a repo path, and
# an entry that tells a model to read a file that does not exist is worse than
# one that says nothing.

# Spellings that look exactly like a file and are not one. A memory says "we
# use Next.js" far more often than it names a file called Next.js, and the
# trigger only has room for three names -- so an invented one does not merely
# add noise, it pushes out the real path that would have sent the model to
# the section. Matched on the whole token, case-insensitively.
_NOT_PATHS = frozenset({
    "next.js", "node.js", "nuxt.js", "vue.js", "three.js", "react.js",
    "express.js", "d3.js", "chart.js", "ember.js", "backbone.js",
})
_BACKTICK_RE = re.compile(r"`([^`\n]{2,60})`")
_SUBHEADING_RE = re.compile(r"(?m)^#{3,6}\s+(.+?)\s*#*$")
# memory.md specifies <=160 for an index summary, and an index entry is
# resident in every prompt of every call -- the one place a round number
# chosen for comfort is paid for a hundred thousand times.
_MAX_SUMMARY_CHARS = 160
_MAX_COVERS = 3
# Above this a section is not "about" the two or three files it happens to
# name first, whatever those are.
_BROAD_SECTION_CHARS = 4_000


def cited_paths(text: str) -> list[str]:
    """Repo paths a section names, first mention first."""
    out: list[str] = []
    for match in _PATH_RE.finditer(text):
        path = match.group(1).rstrip(".,;:)")
        if path.lower() in _NOT_PATHS or path in out:
            continue
        out.append(path)
    return out


def _trigger_paths(paths: list[str]) -> list[str]:
    """The cited paths worth putting in a trigger, best first. A path with a
    slash in it points at one place in the tree; a bare filename may be any of
    several, so it is the weaker signal and goes second when both exist."""
    return [p for p in paths if "/" in p] + [p for p in paths if "/" not in p]


def _covers(section: Section) -> list[str]:
    """The concrete things named inside the section: its sub-headings, then
    whatever it puts in backticks. What is IN the section, in its own words.

    A single lowercase word in backticks is dropped. Memory prose backticks
    keywords and ordinary words as often as it backticks names -- a trigger
    reading "Read before working on except" or "...on main" names nothing the
    model can match a task against, and it crowds out the sub-heading that
    would have. Anything with a dot, a slash, a space or a capital in it is a
    name; `except` is punctuation.
    """
    out: list[str] = []
    for raw in _SUBHEADING_RE.findall(section.body):
        text = raw.strip().replace("`", "")
        if text and text not in out:
            out.append(text)
    for raw in _BACKTICK_RE.findall(section.body):
        text = raw.strip()
        if not text or text in out:
            continue
        if text.islower() and not any(c in text for c in " ./_-()"):
            continue
        out.append(text)
    return out


# A sentence that is about the section rather than about the project. It
# says nothing a model can act on, and it is the shape a consolidator writes
# when it is explaining itself ("This section is long because the work is
# fiddly..."), so it turns up at the top of exactly the long sections whose
# index entry has to work hardest.
_META_OPENING_RE = re.compile(
    r"^(this|these|those)\s+(section|file|document|page|note|notes|list|entries)\b"
    r"|^(what follows|below (is|are)|the (rest|remainder) of this)\b",
    re.IGNORECASE,
)
_LIST_MARKER_RE = re.compile(r"^([-*+]\s+|\d+[.)]\s+)")
_MAX_LIST_ITEMS = 3


def _prose_blocks(section: Section, limit: int = 2) -> list[tuple[list[str], bool]]:
    """The section's first blocks of prose, each with whether it is a list.

    Everything that is not prose is skipped rather than read as prose: a
    fenced block and its contents, a table row, a blockquote, a sub-heading.
    The fence mattered most -- the old version stepped over the ``` line and
    then treated the code inside as the section's first claim, so "Running
    the checks" described itself as "make check." A sub-heading is a title,
    and an entry built from one restates a heading with a heading.

    Two blocks rather than one, because a section that opens by explaining
    itself ("This section is long because the work is fiddly") has its actual
    first claim in the paragraph after -- and those are the long, fiddly
    sections whose index entry has the most work to do.
    """
    blocks: list[tuple[list[str], bool]] = []
    block: list[str] = []
    listed = True

    def flush() -> None:
        nonlocal block, listed
        if block:
            blocks.append((block, listed))
        block, listed = [], True

    fence = ""
    for line in section.body.splitlines()[1:]:
        if len(blocks) >= limit:
            break
        text = line.strip()
        if fence:
            if text.startswith(fence):
                fence = ""
            continue
        opening = re.match(r"(```|~~~)", text)
        if opening:
            fence = opening.group(1)
            flush()
            continue
        rule_off = set(text) <= set("-=*_# ") and not _LIST_MARKER_RE.match(text)
        if not text or text.startswith(("|", ">", "<!--", "#")) or rule_off:
            flush()
            continue
        listed = listed and bool(_LIST_MARKER_RE.match(text))
        block.append(_LIST_MARKER_RE.sub("", text).replace("**", "").replace("`", "").strip())
    flush()
    return blocks[:limit]


# Abbreviations that end in a full stop and do not end a sentence. Without
# them an entry gets cut at the first "(e.g." and is served to every call of
# every task as a sentence that stops mid-clause -- which reads as a bug in
# the memory rather than as a summary.
_ABBREVIATIONS = ("e.g.", "i.e.", "etc.", "cf.", "vs.", "approx.", "fig.", "no.")


def _sentences(text: str) -> list[str]:
    out: list[str] = []
    for part in re.split(r"(?<=[.!?])\s+", text):
        part = part.strip()
        if not part:
            continue
        if out and out[-1].lower().endswith(_ABBREVIATIONS):
            out[-1] = f"{out[-1]} {part}"
        else:
            out.append(part)
    return out


def _clip(text: str) -> str:
    if len(text) > _MAX_SUMMARY_CHARS:
        return text[:_MAX_SUMMARY_CHARS].rsplit(" ", 1)[0] + "..."
    return text


def _gist(section: Section) -> str:
    """The section's own first claim, as one sentence.

    Read across the whole opening paragraph rather than the first line: these
    files are hard-wrapped at about 78 columns, so a first line is half a
    sentence and an entry built from one reads as though it was cut off --
    which is exactly what a reader takes it for.

    A section that opens with a list has no first claim, and presenting its
    first bullet as one says something untrue about the other nine ("Lint
    rules: line length is 120"). The first few items together at least
    describe the shape of the list.

    Generated from the body, never asked of a model at render time: an index
    is read on every call of every task, and paying a model to describe a file
    that has not changed since last night is a bill with no matching benefit.
    """
    paths = cited_paths(section.body)
    for block, listed in _prose_blocks(section):
        if listed:
            items = [item.rstrip(".;,") for item in block[:_MAX_LIST_ITEMS]]
            return _clip("; ".join(items))
        sentences = _sentences(" ".join(block))
        for n, sentence in enumerate(sentences):
            # A sentence that names a file is about the project whatever it
            # opens with, so only a meta sentence that names nothing is
            # stepped over -- and then only when there is something behind it
            # to step to, here or in the paragraph after.
            names_something = any(path in sentence for path in paths)
            if _META_OPENING_RE.match(sentence) and not names_something and n + 1 >= len(sentences):
                break
            if _META_OPENING_RE.match(sentence) and not names_something:
                continue
            return _clip(sentence)
    return ""


def describe_section(section: Section) -> str:
    """The index entry's text: what is in this section, and when to open it.

    Two parts, and no third. The section's own first claim says what is in it;
    a trigger built from the repo paths it cites says when to spend the call
    on it -- "read this before you touch that file" is a decision a model can
    make without thinking about it. An entry that only restates the heading is
    the failure this whole subsystem has to beat, and the heading is already
    printed next to it.

    Kept short deliberately. The index is resident in every prompt of every
    call, so an entry that argues its case at length is paying, forever, to
    save one tool call.
    """
    parts = []
    gist = _gist(section)
    if gist:
        parts.append(gist if gist.endswith((".", "!", "?", ":")) else gist + ".")
    parts.append(_trigger(section))
    return " ".join(parts)


def _trigger(section: Section) -> str:
    """When to spend a call on this section.

    A closed list of three files is a promise that the section is about those
    three, and for a broad section that promise is false in the direction that
    costs something: a model writing code anywhere else reads the entry and
    concludes it does not need it. The policy accepts the risk of that miss
    for a big Conventions section -- it explicitly says such a section "gets a
    strong index line instead" -- so the line has to be honest about its own
    breadth rather than narrowing it. Hence the count: three names the model
    can match on, and an explicit statement that they are not the whole of it.
    """
    paths = cited_paths(section.body)
    if paths:
        shown = _trigger_paths(paths)[:_MAX_COVERS]
        rest = len(paths) - len(shown)
        if rest:
            plural = "file" if rest == 1 else "files"
            return f"Read before touching {', '.join(shown)} and {rest} other {plural} across this repo."
        if section.chars > _BROAD_SECTION_CHARS:
            return (f"Read before touching {', '.join(shown)}, and before writing "
                    f"code anywhere in this repo -- it is long and covers more than those.")
        return "Read before touching " + ", ".join(shown) + "."
    covers = _covers(section)[:_MAX_COVERS]
    if covers:
        return "Read before working on " + ", ".join(covers) + "."
    # Rather than "read before acting on anything to do with <title>", which
    # is the title again in a sentence that sounds like guidance. An entry
    # that admits it has no trigger at least tells the operator's eye where
    # the index is weak, and the section is still reachable by name.
    return "No trigger recorded -- read it if the title sounds relevant."


def build_index(sections: list[Section], *, preamble: str = "",
                budget_tokens: int = INLINE_TOKEN_BUDGET) -> list[IndexEntry]:
    """The index, with the pinned set already decided against the budget the
    whole block has to fit inside.

    The index is costed before anything is pinned, from a render in which
    nothing is pinned -- which is the LONGEST it can be, since a pinned entry
    collapses to one short line. Overestimating the overhead spends slightly
    less of the budget on bodies than it could; underestimating it would blow
    the floor, and only one of those two is a problem.
    """
    entries = [
        IndexEntry(slug=s.slug, title=s.title, summary=describe_section(s), chars=s.chars)
        for s in sections
    ]
    overhead = len(preamble.strip()) + len(render_index_block(entries))
    always = set(choose_always(sections, overhead_chars=overhead, budget_tokens=budget_tokens))
    return [
        replace(e, always=True, reason=always_reason(s)) if s.slug in always else e
        for e, s in zip(entries, sections, strict=True)
    ]


def index_to_json(entries: list[IndexEntry], **meta) -> str:
    """The stored index. `meta` carries the migration's own record (source
    checksum, when it ran) so a re-run can tell an unchanged file from one
    that has moved on."""
    return json.dumps({"version": 1, **meta, "sections": [e.to_dict() for e in entries]}, indent=2)


def parse_index(raw: str) -> list[IndexEntry]:
    """Entries from stored JSON, tolerantly. A hand-edited index must degrade
    to "no sections" -- which is a whole-file read, today's behaviour -- and
    never take the prompt down with it."""
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    rows = doc.get("sections") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        return []
    entries: list[IndexEntry] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("slug"):
            continue
        entries.append(IndexEntry(
            slug=str(row["slug"]),
            title=str(row.get("title") or row["slug"]),
            summary=str(row.get("summary") or ""),
            chars=int(row.get("chars") or 0),
            always=bool(row.get("always")),
            reason=str(row.get("reason") or ""),
        ))
    return entries


@dataclass(frozen=True)
class MemoryIndex:
    """A stored index, with the migration's own record of what it was cut
    from. `source_sha256` is empty for an index written before that record
    existed or by hand, which reads as "no claim about the source" rather
    than as a mismatch."""

    entries: list[IndexEntry]
    source_sha256: str = ""
    migrated_at: str = ""


def parse_index_document(raw: str) -> MemoryIndex:
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        doc = {}
    meta = doc if isinstance(doc, dict) else {}
    return MemoryIndex(
        entries=parse_index(raw),
        source_sha256=str(meta.get("source_sha256") or ""),
        migrated_at=str(meta.get("migrated_at") or ""),
    )


def render_index_block(entries: list[IndexEntry], resident: set[str] | None = None) -> str:
    """The index as the model sees it. Pinned sections are listed too, marked
    as already present: an entry the model cannot see is an entry it may
    re-fetch, and paying a tool call for text already in the prompt is the one
    way this design can cost more than it saves.

    `resident` is which of them actually made it into the block. A pinned
    entry whose body did not is demoted to an ordinary indexed line here,
    because the alternative is the worst line this module can emit: "already
    above, do not re-read" printed next to a gap, which removes the content
    AND withdraws permission to go and get it. `always` is a flag in a file
    and the body is a separate key; nothing guarantees the two agree, so this
    renders what is in front of it rather than what the flag claims.
    """
    if not entries:
        return ""
    lines = [
        _INDEX_HEADER,
        "The rest of this project's memory is NOT in your prompt. Each line says what is in a section",
        'and when to read it -- pull one with read_memory_section("<slug>") BEFORE you act in its area,',
        'or read_memory_section("all") when you are unsure which. A section you read and did not need',
        "cost one call; one you needed and did not read costs a build-review-rework cycle.",
        "",
    ]
    for entry in entries:
        if entry.always and (resident is None or entry.slug in resident):
            # Already in the prompt, a few lines up. Listed so the model does
            # not spend a call re-fetching text it is looking at, and listed in
            # one line because everything an entry would say about it is there.
            lines.append(f"- {entry.slug} -- {entry.title} (already above, do not re-read)")
        else:
            lines.append(f"- {entry.slug} (~{entry.tokens} tok) -- {entry.title}: {entry.summary}")
    return "\n".join(lines)


def fit_to_budget(preamble: str, entries: list[IndexEntry], bodies: dict[str, str],
                  budget_tokens: int = INLINE_TOKEN_BUDGET) -> set[str]:
    """Which pinned sections actually fit, in document order.

    The index decided this once already, at migration time, against the budget
    that was set then. This is the same sum enforced where it is spent, so
    lowering the knob takes effect on the next prompt rather than on the next
    migration -- an operator who turns a dial down and watches nothing happen
    concludes the dial is decorative, and the floor this subsystem promises is
    only a floor if something holds it at render time.

    A section dropped here is not lost: it goes back to being an ordinary
    indexed line the model can fetch, which is the whole difference between a
    budget and a deletion.
    """
    room = budget_tokens * CHARS_PER_TOKEN - len(preamble.strip()) - len(render_index_block(entries))
    resident: set[str] = set()
    spent = 0
    for entry in entries:
        if not entry.always or entry.slug not in bodies:
            continue
        if spent + len(bodies[entry.slug]) > room:
            continue
        resident.add(entry.slug)
        spent += len(bodies[entry.slug])
    return resident


def render_prompt_block(preamble: str, entries: list[IndexEntry], bodies: dict[str, str],
                        budget_tokens: int = INLINE_TOKEN_BUDGET) -> str:
    """What lands in the `<project_memory>` block: the preamble, the pinned
    sections in document order, then the index of everything else.

    The index goes last on purpose -- it is the part that has to be acted on,
    and it sits closest to the conversation rather than buried above a
    thousand tokens of pinned rules.

    What is advertised as resident is what was actually put above, not what
    the index flags claim -- see render_index_block.
    """
    resident = fit_to_budget(preamble, entries, bodies, budget_tokens)
    parts = [preamble.strip()]
    parts += [bodies[e.slug].strip() for e in entries if e.slug in resident]
    index = render_index_block(entries, resident)
    if index:
        parts.append(index)
    return "\n\n".join(p for p in parts if p)


def section_path(slug: str) -> str:
    """The agent-visible path of one section, which is also what an index
    entry is pointing at when the model decides to spend a call on it."""
    return f"{SECTIONS_DIR}{slug}.md"
