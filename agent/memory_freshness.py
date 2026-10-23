"""Memory freshness: which remembered facts cite a file that changed since
the fact was recorded.

/memories/AGENTS.md is loaded in full on every planning turn and every build
task and treated as established fact. It has no timestamps, and consolidation
rewrites the whole file nightly, so nothing in the file itself says how old a
line is. The operator opened the 2026-09-08 trendSignal planning session
with "a lot of changes have happened to this repo, so your memory is stale" --
and the planner's answer was to re-read the repo for twenty minutes.

This module makes staleness announce itself instead:

- `find_path_mentions` pulls every repo path a memory line cites (exact path
  or a basename unique in the tree).
- The cartographer keeps a LEDGER per project (/.memory-freshness.json in the
  project namespace): line-hash -> first date the cartographer saw that line
  cite that path. That date is the best available proxy for when the fact was
  recorded, and it is exact from the day this ships.
- `stale_flags` asks git for each cited file's last commit date; a fact whose
  file changed AFTER the fact was first seen is flagged.
- The flags are rendered once, stored beside the ledger (/.memory-stale.md),
  and appended to the memory block of both agents' prompts by
  `memory_with_freshness` -- no git call on the prompt hot path.

A flag is a hint, not a verdict: the file changed, so the fact deserves a
glance before it is trusted. Nothing is deleted from memory automatically.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import subprocess
from datetime import date
from pathlib import Path

logger = logging.getLogger("tektonix")

LEDGER_PATH = "/.memory-freshness.json"
STALE_PATH = "/.memory-stale.md"
MAX_FLAGS = 12

_PATH_TOKEN = re.compile(
    r"(?<![\w/])((?:[\w.-]+/)*[\w.-]+\.(?:js|mjs|cjs|ts|tsx|jsx|py|go|rs|java|rb|php|json|ya?ml|toml|md|sql|sh))(?![\w/])"
)


def find_path_mentions(memory_text: str, tree: list[str]) -> list[tuple[str, str]]:
    """[(memory line, repo path)] for every line that cites a file in `tree`
    (repo-relative paths). A bare basename counts only if exactly one tree
    path ends in it -- ambiguity is not evidence."""
    by_path = set(tree)
    by_base: dict[str, list[str]] = {}
    for p in tree:
        by_base.setdefault(Path(p).name, []).append(p)
    out: list[tuple[str, str]] = []
    for raw in memory_text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        seen: set[str] = set()
        for tok in _PATH_TOKEN.findall(line):
            tok = tok.strip("`'\"()[],.")
            resolved = None
            if tok in by_path:
                resolved = tok
            else:
                candidates = by_base.get(Path(tok).name, [])
                if len(candidates) == 1 and (tok == Path(tok).name or candidates[0].endswith(tok)):
                    resolved = candidates[0]
            if resolved and resolved not in seen:
                seen.add(resolved)
                out.append((line, resolved))
    return out


def _key(line: str, path: str) -> str:
    return hashlib.sha1(f"{path}\n{line}".encode()).hexdigest()[:16]


def update_ledger(ledger: dict, mentions: list[tuple[str, str]], today: str) -> dict:
    """Records first-seen dates for new (line, path) pairs and drops entries
    whose line no longer exists in memory. Idempotent for an unchanged file."""
    fresh: dict[str, dict] = {}
    for line, path in mentions:
        k = _key(line, path)
        prior = ledger.get(k)
        fresh[k] = {
            "path": path,
            "line": line[:200],
            "first_seen": prior["first_seen"] if prior and prior.get("first_seen") else today,
        }
    return fresh


def last_change_dates(repo_root: str, paths: set[str]) -> dict[str, str]:
    """{path: YYYY-MM-DD of its last commit}. Best effort: a path git knows
    nothing about (untracked, deleted) is simply absent."""
    out: dict[str, str] = {}
    for p in sorted(paths):
        try:
            r = subprocess.run(
                ["git", "log", "-1", "--format=%cs", "--", p],
                cwd=repo_root, capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0 and r.stdout.strip():
                out[p] = r.stdout.strip()
        except Exception:  # noqa: BLE001 -- one bad path must not lose every flag
            continue
    return out


def stale_flags(ledger: dict, changed: dict[str, str]) -> list[dict]:
    """Entries whose cited file's last commit is later than the day the fact
    was first seen. Most recently changed first."""
    flags = []
    for entry in ledger.values():
        last = changed.get(entry["path"])
        if last and last > entry["first_seen"]:
            flags.append({**entry, "changed": last})
    flags.sort(key=lambda f: (f["changed"], f["path"]), reverse=True)
    return flags


def section_of(memory_text: str, line: str) -> str:
    """The `##` section a memory line sits in, as its slug, or "".

    A flag quotes the fact and names the repo file, which was enough while
    the whole memory was in every prompt. Once a project's memory is split
    (agent/memory_sections.py) the fact may live in a section the prompt is
    not carrying, so the agent is told a fact it cannot see is stale and has
    nothing to act on. Naming the section turns that into one tool call.

    Falls back to "" rather than guessing: an unsplit project has no sections
    and a line that matches none of them is better left unattributed than
    attributed wrongly.
    """
    if not line.strip():
        return ""
    from agent import memory_sections  # noqa: PLC0415 -- avoids an import cycle at module load

    preamble, sections = memory_sections.split_sections(memory_text)
    for section in sections:
        if line in section.body:
            return section.slug
    return ""


def render_flags(flags: list[dict], repo: str) -> str:
    if not flags:
        return ""
    lines = [
        f"POSSIBLY STALE -- {len(flags)} memory fact(s) for {repo} cite a file that changed after "
        "the fact was recorded. Verify against the current file before relying on them:",
    ]
    for f in flags[:MAX_FLAGS]:
        where = f.get("section") or ""
        cite = f"{f['path']} changed {f['changed']}, fact recorded {f['first_seen']}"
        if where:
            cite += f", in section {where}"
        lines.append(f"- [{cite}] {f['line'][:140]}")
    if len(flags) > MAX_FLAGS:
        lines.append(f"- ... and {len(flags) - MAX_FLAGS} more")
    return "\n".join(lines)


async def refresh_memory_freshness(repo: str, repo_root: str, project_backend, memory_text: str, tree: list[str], today: str | None = None) -> dict:
    """The cartographer's step: update the ledger, compute flags, store the
    rendered block. Returns a small summary for the run log."""
    from deepagents.backends.utils import file_data_to_string

    today = today or date.today().isoformat()
    ledger: dict = {}
    existing = await project_backend.aread(LEDGER_PATH)
    if existing.error is None and existing.file_data:
        try:
            ledger = json.loads(file_data_to_string(existing.file_data))
        except (json.JSONDecodeError, TypeError):
            ledger = {}
    mentions = find_path_mentions(memory_text, tree)
    ledger = update_ledger(ledger, mentions, today)
    # One `git log` per cited path, off the loop (see cartographer.run_cartographer).
    changed = await asyncio.to_thread(last_change_dates, repo_root, {e["path"] for e in ledger.values()})
    flags = stale_flags(ledger, changed)
    # Which section each flagged fact lives in, so a flag about a fact the
    # prompt is not carrying says where to read it.
    for f in flags:
        f["section"] = section_of(memory_text, f.get("line", ""))
    await project_backend.awrite(LEDGER_PATH, json.dumps(ledger, indent=2, sort_keys=True))
    await project_backend.awrite(STALE_PATH, render_flags(flags, repo))
    return {"cited_paths": len(ledger), "stale": len(flags)}


async def memory_with_freshness(project_backend, memory_text: str) -> str:
    """Memory content plus the stored stale-flags block, for a system prompt.
    Never raises and never touches git: a missing block means no flags."""
    try:
        from deepagents.backends.utils import file_data_to_string

        r = await project_backend.aread(STALE_PATH)
        block = file_data_to_string(r.file_data).strip() if r.error is None and r.file_data else ""
    except Exception:  # noqa: BLE001
        logger.warning("stale-memory flags unreadable; prompt goes without them", exc_info=True)
        block = ""
    return f"{memory_text}\n\n{block}" if block else memory_text
