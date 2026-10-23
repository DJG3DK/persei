"""The cartographer -- builds and maintains a per-project CODEBASE MAP.

Why this exists
---------------
The other memory tiers are episodic or factual: /memories/AGENTS.md holds small
durable facts, /episodes holds what happened, consolidation distils the second
into the first. None of them holds STRUCTURE. So every task re-derived "where
does this live, what covers it, what are this repo's conventions" from scratch
with ripgrep, spent tokens doing it, and threw the answer away at the end.

This closes that gap. The map is written as a SKILL rather than into
/memories/AGENTS.md on purpose: memory loads in full on every single task and
must stay small, while skills are progressive-disclosure -- only a one-line
name+description loads by default and the agent reads the full map with its own
read tool when a task actually needs it. A repo map is exactly that shape:
large, situational, and worth reading deliberately.

Two deliberate constraints
--------------------------
1. The INVENTORY IS GATHERED DETERMINISTICALLY, in Python, before any model is
   called. The model never walks the tree itself. That keeps the cost of a map
   proportional to one prompt rather than to an agentic exploration loop, and it
   means the same repo always produces the same inventory -- so the only
   variable in the output is the summarisation.
2. The inventory is HASHED and the hash stored. If nothing structural changed
   since the last run, the model is never called at all. Mapping is idempotent
   and cheap to schedule aggressively.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
from collections import Counter
from pathlib import Path

from langchain.chat_models import init_chat_model
from langgraph.store.base import BaseStore

from agent.config import PROJECTS, Config
from agent.deep_agent import (
    MEMORY_PATH,
    project_namespace,
    route_local_path,
    seed_skill,
)
from deepagents.backends.store import StoreBackend

logger = logging.getLogger("tektonix")

# The project memory both agents load, at the key the project namespace stores it under.
_MEMORY_FILE = route_local_path("/memories/", MEMORY_PATH)

SKILL_NAME = "codebase-map"
MAP_MARKER_PATH = "/.last_mapped"
CHANGES_SKILL_NAME = "recent-changes"
CHANGES_MARKER_PATH = "/.last_changes_head"
CHANGES_COMMITS = 30            # newest commits the changelog skill carries
CHANGES_BODY_LINES = 14         # of each commit message body
CHANGES_FILES = 12              # of each commit's touched files

# Directories that are never part of a repo's architecture. Walking them is
# both slow and actively misleading -- a node_modules tree would dominate the
# language histogram and bury the actual source.
SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
    ".next", ".turbo", "coverage", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "vendor", ".cache", "target", ".gradle", "site-packages",
}
CODE_EXT = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript", ".cjs": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript/React", ".jsx": "JavaScript/React",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".rb": "Ruby", ".php": "PHP",
    ".sql": "SQL", ".sh": "Shell", ".css": "CSS", ".scss": "SCSS",
}
MAX_TREE_ENTRIES = 700          # a map is a map, not a directory listing
MAX_HOT_FILES = 40


def _run(cmd: list[str], cwd: str) -> str:
    """Best-effort subprocess capture. Never raises -- an inventory missing one
    section is fine; an exception here would block the whole map."""
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=60)
        return out.stdout.strip()
    except Exception:  # noqa: BLE001 -- see the docstring: a missing section beats no map
        return ""


def _walk(repo_root: str) -> list[Path]:
    root = Path(repo_root)
    files: list[Path] = []
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in SKIP_DIRS for part in p.parts):
            continue
        files.append(p)
    return files


def build_inventory(repo: str, repo_root: str) -> dict:
    """Everything the model gets, gathered without the model. Deterministic:
    same repo state -> same inventory -> same hash -> no re-run."""
    root = Path(repo_root)
    files = _walk(repo_root)

    langs = Counter()
    for f in files:
        lang = CODE_EXT.get(f.suffix)
        if lang:
            langs[lang] += 1

    # Directory shape: how much code sits under each top two levels.
    dirs = Counter()
    for f in files:
        if f.suffix not in CODE_EXT:
            continue
        rel = f.relative_to(root)
        parts = rel.parts[:-1]
        if parts:
            dirs["/".join(parts[:2])] += 1

    # Package manifests -- the declared entry points, scripts and deps.
    manifests: dict[str, str] = {}
    for name in ("package.json", "pyproject.toml", "requirements.txt", "go.mod", "Cargo.toml"):
        for hit in list(root.rglob(name))[:6]:
            if any(part in SKIP_DIRS for part in hit.parts):
                continue
            try:
                text = hit.read_text(errors="replace")
            except OSError:
                continue
            if name == "package.json":
                try:
                    data = json.loads(text)
                    text = json.dumps(
                        {k: data.get(k) for k in ("name", "main", "scripts", "workspaces") if k in data},
                        indent=2,
                    )
                except json.JSONDecodeError:
                    pass
            manifests[str(hit.relative_to(root))] = text[:3000]

    tests = sorted(
        str(f.relative_to(root)) for f in files
        if ("test" in f.name.lower() or "spec" in f.name.lower()) and f.suffix in CODE_EXT
    )

    docs = sorted(
        str(f.relative_to(root)) for f in files
        if f.suffix in (".md", ".mdx") and f.stat().st_size > 400
    )

    # Churn: the files this repo actually works on. A far better signal of
    # what matters than file size or alphabetical order.
    churn = _run(
        ["git", "log", "--since=6 months ago", "--name-only", "--pretty=format:"], repo_root
    )
    hot = Counter(
        line for line in churn.splitlines()
        if line and not any(part in SKIP_DIRS for part in Path(line).parts)
        and Path(line).suffix in CODE_EXT
    )

    tree = sorted(str(f.relative_to(root)) for f in files if f.suffix in CODE_EXT)

    return {
        "repo": repo,
        "repo_root": repo_root,
        "file_count": len(files),
        "languages": dict(langs.most_common()),
        "top_directories": dict(dirs.most_common(40)),
        "manifests": manifests,
        "test_files": tests[:120],
        "docs": docs[:40],
        "hot_files": dict(hot.most_common(MAX_HOT_FILES)),
        "tree": tree[:MAX_TREE_ENTRIES],
        "tree_truncated": len(tree) > MAX_TREE_ENTRIES,
        "recent_commits": _run(["git", "log", "-25", "--pretty=format:%s"], repo_root),
    }


def inventory_hash(inv: dict) -> str:
    """Hash the STRUCTURAL parts only. Deliberately excludes recent_commits and
    hot_files: those change on every commit, and re-mapping a repo because
    someone edited one line would defeat the point of the marker."""
    stable = {
        k: inv[k] for k in
        ("file_count", "languages", "top_directories", "manifests", "test_files", "docs", "tree")
    }
    return hashlib.sha256(json.dumps(stable, sort_keys=True).encode()).hexdigest()[:16]


def _render_inventory(inv: dict) -> str:
    parts = [
        f"REPO: {inv['repo']}  (root: {inv['repo_root']})",
        f"FILES: {inv['file_count']}",
        f"LANGUAGES: {json.dumps(inv['languages'])}",
        "",
        "CODE PER DIRECTORY (file counts):",
        json.dumps(inv["top_directories"], indent=2),
        "",
        "PACKAGE MANIFESTS:",
    ]
    for name, text in inv["manifests"].items():
        parts.append(f"--- {name} ---\n{text}")
    parts += [
        "",
        "MOST-CHANGED FILES (last 6 months, by commit count):",
        json.dumps(inv["hot_files"], indent=2),
        "",
        f"TEST FILES ({len(inv['test_files'])} shown):",
        "\n".join(inv["test_files"]),
        "",
        "DOCS:",
        "\n".join(inv["docs"]),
        "",
        f"SOURCE TREE{' (truncated)' if inv['tree_truncated'] else ''}:",
        "\n".join(inv["tree"]),
        "",
        "RECENT COMMIT SUBJECTS:",
        inv["recent_commits"],
    ]
    return "\n".join(parts)


CARTOGRAPHER_SYSTEM_PROMPT = """You are the cartographer for the `{repo}` codebase.

You are given a complete, deterministically-gathered inventory of the repo. You
do not need to explore anything; everything you get is everything there is.

Write a CODEBASE MAP in Markdown that lets another engineer -- or another agent
-- orient in this repo in one read. Optimise for a reader who is about to make a
change and needs to know where to look and what not to break.

Cover, in this order, and only where the inventory actually supports it:

1. **What this project is** -- one short paragraph, inferred from manifests,
   directory names and commit subjects.
2. **Architecture** -- the real top-level pieces (services, entry points,
   deployables) and how they relate. Name actual paths.
3. **Where things live** -- a compact table mapping a concern ("order
   execution", "auth", "the dashboard") to its directory or file. This is the
   single most useful section; spend your effort here.
4. **Conventions** -- naming, module boundaries, layering, anything the file
   layout makes evident (e.g. a `core/` vs `modules/` split, a registry
   pattern, pure-vs-IO separation).
5. **Testing** -- what the suites are, how they are run (read the manifest
   scripts), and which areas are well covered versus bare.
6. **Hot spots** -- the most-changed files, and what that churn implies about
   where the active work and the risk are.
7. **Reference docs** -- point at the repo's own docs rather than restating
   them.

Rules:
- Ground EVERY claim in the inventory. If the inventory does not tell you
  something, do not assert it. "Not evident from the file layout" is a correct
  and useful thing to write.
- Prefer real paths over prose. `src/strategies/modules/gridCore.js` beats "the
  grid core module".
- Do NOT restate the file tree. The reader has the repo; they need the map.
- No preamble, no sign-off. Start with the `# ` heading and nothing before it.
"""


def build_recent_changes(repo: str, repo_root: str) -> tuple[str, str]:
    """(HEAD sha, Markdown) -- the last CHANGES_COMMITS commits with their
    message bodies and touched files, plus which files those commits touched
    most. Deterministic and model-free: this repo's commit messages already
    say WHY, so summarising them would only add cost and a chance to be
    wrong. The operator's 2026-09-08 planning session opened with "a lot of
    changes have happened to this repo, so your memory is stale" and the
    planner answered by re-reading the repo for twenty minutes; this is the
    one read that answers that question."""
    head = _run(["git", "rev-parse", "HEAD"], repo_root)
    if not head:
        return "", ""
    sep = "\x1e"
    raw = _run(
        ["git", "log", f"-{CHANGES_COMMITS}", "--date=short", "--name-only",
         f"--pretty=format:{sep}%h%x1f%ad%x1f%an%x1f%s%x1f%b"],
        repo_root,
    )
    commits: list[dict] = []
    touched: Counter = Counter()
    for chunk in raw.split(sep):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        header, _, files_blob = chunk.partition("\n\n") if "\x1f" in chunk else (chunk, "", "")
        parts = header.split("\x1f")
        if len(parts) < 5:
            continue
        sha, day, subject, body = parts[0], parts[1], parts[3], "\x1f".join(parts[4:])  # parts[2] is the author
        # body and file list are separated by the blank line git emits before
        # --name-only output; the body itself may contain blank lines, so take
        # the file list as the trailing run of path-looking lines instead.
        lines = (body + ("\n\n" + files_blob if files_blob else "")).split("\n")
        files: list[str] = []
        while lines and (not lines[-1].strip() or ("/" in lines[-1] or "." in lines[-1]) and " " not in lines[-1].strip()):
            line = lines.pop().strip()
            if line:
                files.insert(0, line)
        body_text = "\n".join(line.rstrip() for line in lines).strip()
        files = [f for f in files if not any(part in SKIP_DIRS for part in Path(f).parts)]
        touched.update(files)
        commits.append({"sha": sha, "date": day, "subject": subject, "body": body_text, "files": files})
    if not commits:
        return head, ""
    out = [
        f"# Recent changes in {repo}",
        "",
        f"The newest {len(commits)} commits, newest first, as of {commits[0]['date']} (HEAD {head[:10]}). "
        "Each entry is the commit's own message -- the WHY is in there -- followed by the files it touched. "
        "Read this before concluding that memory or the codebase map is out of date: if the thing you are "
        "planning around changed recently, the change and its reasoning are here.",
        "",
        "## Most-touched files in these commits",
        "",
    ]
    out += [f"- `{path}` ({n} commits)" for path, n in touched.most_common(15)]
    out += ["", "## Commits", ""]
    for c in commits:
        out.append(f"### {c['date']} -- {c['subject']}  (`{c['sha']}`)")
        if c["body"]:
            body_lines = c["body"].splitlines()
            out.extend(body_lines[:CHANGES_BODY_LINES])
            if len(body_lines) > CHANGES_BODY_LINES:
                out.append(f"... ({len(body_lines) - CHANGES_BODY_LINES} more lines in the commit message)")
        if c["files"]:
            shown = ", ".join(f"`{f}`" for f in c["files"][:CHANGES_FILES])
            more = f" and {len(c['files']) - CHANGES_FILES} more" if len(c["files"]) > CHANGES_FILES else ""
            out.append(f"files: {shown}{more}")
        out.append("")
    return head, "\n".join(out).rstrip() + "\n"


async def refresh_recent_changes(repo: str, repo_root: str, store: BaseStore, project_backend) -> dict:
    """Rebuilds the recent-changes skill when HEAD moved since the last build.
    No model call, so this runs on every cartographer tick for free."""
    from deepagents.backends.utils import file_data_to_string

    head, content = await asyncio.to_thread(build_recent_changes, repo, repo_root)
    if not head or not content:
        return {"changes": "no git history"}
    marker = await project_backend.aread(CHANGES_MARKER_PATH)
    if marker.error is None and marker.file_data and file_data_to_string(marker.file_data).strip() == head:
        return {"changes": "unchanged"}
    description = (
        f"The newest {CHANGES_COMMITS} commits to {repo} with their full messages and touched files, "
        f"rebuilt whenever HEAD moves. Read this when a request says the repo has changed, when memory "
        f"or the codebase map might lag a recent change, or before re-deriving what a recently touched "
        f"file does -- the commit message already says why it changed."
    )
    import json as _json
    content = (
        "---\n"
        f"name: {CHANGES_SKILL_NAME}\n"
        f"description: {_json.dumps(description)}\n"
        "---\n\n"
        + content
    )
    await seed_skill(repo, store, CHANGES_SKILL_NAME, description, content)
    await project_backend.awrite(CHANGES_MARKER_PATH, head)
    return {"changes": "rebuilt", "head": head[:10]}


async def refresh_freshness(repo: str, repo_root: str, project_backend, inv: dict) -> dict:
    """Flags memory facts whose cited file changed after the fact was first
    seen (agent/memory_freshness.py). Reads the project's memory directly from
    the project namespace -- the same file both agents load."""
    from deepagents.backends.utils import file_data_to_string
    from agent.memory_freshness import refresh_memory_freshness

    r = await project_backend.aread(_MEMORY_FILE)
    memory_text = file_data_to_string(r.file_data) if r.error is None and r.file_data else ""
    if not memory_text.strip():
        return {"freshness": "no memory"}
    tree = inv.get("tree") or []
    return await refresh_memory_freshness(repo, repo_root, project_backend, memory_text, tree)


async def run_cartographer(
    config: Config, repo: str, store: BaseStore, *, force: bool = False
) -> dict:
    """Builds (or refreshes) one project's codebase map. Returns a small summary
    dict for whatever scheduled it -- same contract as run_consolidation."""
    repo_root = PROJECTS[repo]["sandbox"]
    project_backend = StoreBackend(namespace=project_namespace(repo), store=store)

    # Off the loop: the inventory walks the tree and runs git, and since the
    # cartographer moved in-process (agent/jobs.py) doing that here stalled
    # every request and WebSocket for each project's walk (2026-09-29).
    inv = await asyncio.to_thread(build_inventory, repo, repo_root)
    digest = inventory_hash(inv)

    # Cheap, model-free companions that run on every tick regardless of
    # whether the structure changed: the changelog skill and the memory
    # freshness flags both key on git state, not on the inventory hash.
    extras: dict = {}
    for step in (
        lambda: refresh_recent_changes(repo, repo_root, store, project_backend),
        lambda: refresh_freshness(repo, repo_root, project_backend, inv),
    ):
        try:
            extras.update(await step())
        except Exception as e:  # noqa: BLE001 -- a companion must never block the map
            logger.warning("cartographer companion failed for %s: %s", repo, e)

    if not force:
        marker = await project_backend.aread(MAP_MARKER_PATH)
        if marker.error is None and marker.file_data:
            from deepagents.backends.utils import file_data_to_string

            if file_data_to_string(marker.file_data).strip() == digest:
                return {
                    "repo": repo, "mapped": False, "hash": digest,
                    "reasoning": "repo structure unchanged since last map", **extras,
                }

    model = init_chat_model(
        "agent-cartographer",
        model_provider="openai",
        base_url=config.router_base_url,
        api_key=config.router_api_key,
        temperature=0,
        timeout=300,
    )
    # No tools and no structured output on purpose: the inventory is already
    # gathered, so this is a single summarisation call. That keeps the role free
    # of the tools+structured-output constraint that pinned the consolidator.
    reply = await model.ainvoke([
        {"role": "system", "content": CARTOGRAPHER_SYSTEM_PROMPT.format(repo=repo)},
        {"role": "user", "content": _render_inventory(inv)},
    ])
    content = (reply.content or "").strip()
    if not content:
        return {"repo": repo, "mapped": False, "hash": digest, "reasoning": "model returned nothing"}

    description = (
        f"Structural map of the {repo} codebase: architecture, where each concern lives, "
        f"conventions, test layout and hot spots. Auto-generated by the cartographer from a "
        f"deterministic file inventory; refreshed when the repo's structure changes. "
        f"Read this FIRST when starting work in an unfamiliar area of {repo}."
    )
    # deepagents' skill loader requires YAML frontmatter (name + description) at
    # the top of SKILL.md and SKIPS the file otherwise -- "no valid YAML
    # frontmatter found", confirmed in the live agent log 2026-08-26. The first
    # generation of maps shipped without it, so they sat in the store fully
    # written and completely invisible to every agent. The manifest description
    # alone is not enough; the file itself must carry the frontmatter.
    # json.dumps produces a double-quoted scalar that is ALSO valid YAML --
    # the reliable way to embed arbitrary prose. The first attempt used a bare
    # f-string and the description's own colon made the YAML invalid
    # ("mapping values are not allowed here"), so the loader skipped the file
    # and the maps stayed invisible for a second time. Same lesson as the
    # commit-message backticks: never splice prose into a structured format
    # without the format's own quoting.
    import json as _json
    content = (
        "---\n"
        f"name: {SKILL_NAME}\n"
        f"description: {_json.dumps(description)}\n"
        "---\n\n"
        + content
    )
    await seed_skill(repo, store, SKILL_NAME, description, content)
    await project_backend.awrite(MAP_MARKER_PATH, digest)

    return {
        "repo": repo, "mapped": True, "hash": digest,
        "chars": len(content), "files_seen": inv["file_count"],
        "reasoning": "map rebuilt", **extras,
    }
