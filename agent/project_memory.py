"""A project's memory, skills and episodes as the store holds them, and the
memory block a seat's prompt carries.

The store layer: the agent-visible paths (/memories/, /org-memory/,
/skills/, /episodes/), the namespace each maps to, the CompositeBackend that
routes them, route_local_path (the key a route's StoreBackend actually
stores a path under), seeding, and the skills manifest. On top of it,
ProjectMemory and load_project_memory: the whole file for a project that
was never split, the pinned sections plus the index for one that was
(agent/memory_sections.py), falling back to the whole file whenever the
split cannot be trusted.

Every name here is re-exported from agent.deep_agent, which is where the
seats, the consolidator, the scripts and the tests import it from.
"""

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from langgraph.store.base import BaseStore

from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from deepagents.backends.utils import file_data_to_string

from agent import episode_recall
from agent import memory_sections
from agent import runtime_settings as _rs
from agent.memory_freshness import memory_with_freshness
from agent.store_paging import all_items

# Three-tier memory:
#
#   /memories/AGENTS.md   -- semantic, per-project, agent-writable during
#                            normal work (durable facts about this repo).
#   /org-memory/AGENTS.md -- semantic, cross-project, read-only to every task
#                            agent (enforced via `permissions`, not just
#                            prompting -- this system doesn't trust
#                            instruction-following alone for anything that
#                            matters). Populated by application code only:
#                            one task's agent should never be able to alter
#                            what every other project's agent believes.
#   /episodes/{repo}/...  -- structured records of past task outcomes
#                           (goal, result, cost, escalation reason if any),
#                           written by verify_and_ship at terminal state, not
#                           auto-loaded into every task's context (that would
#                           defeat the point of keeping the hot path small).
#                           Read only by the consolidation agent
#                           (agent/consolidation.py), which distills them
#                           into /memories/AGENTS.md updates on a schedule --
#                           background consolidation, not the agent editing
#                           its own memory ad hoc as the only mechanism.
MEMORY_PATH = "/memories/AGENTS.md"
ORG_MEMORY_PATH = "/org-memory/AGENTS.md"
ORG_NAMESPACE = ("org",)
EPISODES_ROUTE = "/episodes/"

# Skills -- deepagents' progressive-disclosure mechanism, distinct from
# memory: memory is small, durable, always-relevant facts (loaded in full,
# every task); skills are large, situational domain knowledge (a subsystem's
# real architecture, a non-obvious integration's rules) that would bloat
# every task's context if treated as memory, so only a one-line
# name+description loads by default -- the agent reads a skill's full
# SKILL.md via its own read_file tool only when a task actually touches that
# area. Per-project namespace (skills are repo-specific domain knowledge,
# not cross-project policy like org-memory). Read-only to the agent, same
# reasoning as org-memory: skills are curated reference material, updated
# deliberately (seed_skill), not something that should drift from an
# agent's own mid-task edits.
SKILLS_ROUTE = "/skills/"
SKILLS_MANIFEST_PATH = "/skills/_manifest.json"

# Memory, once it is too big to carry whole: the same progressive disclosure,
# one level down. /memories/sections/_index.json + /memories/sections/<slug>.md
# hold what /memories/AGENTS.md used to hold alone, and agent/memory_sections.py
# decides which of them stay resident. Nothing here exists until a project's
# memory has actually been split -- with no index present every read below
# falls back to the whole file, which is what this system did before and what
# it keeps doing for a project small enough not to need any of this.
SECTIONS_INDEX_PATH = memory_sections.SECTIONS_INDEX_PATH
SECTIONS_CORE_PATH = memory_sections.SECTIONS_CORE_PATH

logger = logging.getLogger("tektonix")


def project_namespace(repo: str) -> Callable[[object], tuple[str, ...]]:
    # One shared, cross-task memory file per project -- every task/thread for
    # this repo reads and writes the same store-backed AGENTS.md, not a
    # per-conversation copy. `rt` (Runtime) is unused here since the
    # namespace is fully determined by which project this factory call is
    # for, not by anything request-time.
    def namespace(rt: object) -> tuple[str, ...]:
        return (repo,)

    return namespace


def org_namespace(rt):
    return ORG_NAMESPACE


def episodes_namespace(repo: str):
    def namespace(rt):
        return ("episodes", repo)

    return namespace


def skills_namespace(repo: str):
    def namespace(rt):
        return ("skills", repo)

    return namespace


def build_memory_backend(repo: str, store: BaseStore) -> CompositeBackend:
    return CompositeBackend(
        default=StateBackend(),  # ephemeral, thread-scoped scratch for everything else
        routes={
            "/memories/": StoreBackend(namespace=project_namespace(repo), store=store),
            "/org-memory/": StoreBackend(namespace=org_namespace, store=store),
            EPISODES_ROUTE: StoreBackend(namespace=episodes_namespace(repo), store=store),
            SKILLS_ROUTE: StoreBackend(namespace=skills_namespace(repo), store=store),
        },
    )


def route_local_path(route: str, path: str) -> str:
    """The key a CompositeBackend route's StoreBackend actually stores `path`
    under: the route prefix is stripped down to a leading slash before the
    path reaches the route's backend (a composite aread of
    "/memories/AGENTS.md" errors with "File '/AGENTS.md' not found").
    Every piece of app code that touches an agent-visible file via a bare
    StoreBackend (seeding, the system-prompt reads, consolidation) must use
    this stripped form, or it reads/writes a key the agent's own file tools
    can never see.
    """
    assert path.startswith(route), f"{path!r} is not under route {route!r}"
    return "/" + path[len(route):]


async def _seed_if_absent(backend: StoreBackend, path: str, content: str) -> None:
    existing = await backend.aread(path)
    if existing.error is None:
        return
    await backend.awrite(path, content)


async def seed_memory(repo: str, store: BaseStore, content: str) -> None:
    """Writes the initial AGENTS.md content for a project if nothing is
    there yet -- idempotent, safe to call on every server startup. Does not
    overwrite existing content, since the agent is expected to extend this
    file over time and a redeploy shouldn't clobber what it's learned since
    the last seed.
    """
    backend = StoreBackend(namespace=project_namespace(repo), store=store)
    # route_local_path, not MEMORY_PATH -- a bare StoreBackend must use the
    # same stripped key the composite's /memories/ route produces, or the
    # agent's own file tools can never see what was seeded.
    await _seed_if_absent(backend, route_local_path("/memories/", MEMORY_PATH), content)


async def seed_org_memory(store: BaseStore, content: str) -> None:
    """Same idempotent-seed contract as seed_memory, but for the single
    cross-project org-memory file. Application-code-only -- the agent has no
    write access to this path (see the `permissions` rule in
    build_deep_agent), so this is the only way this file is ever updated,
    by design.
    """
    backend = StoreBackend(namespace=org_namespace, store=store)
    await _seed_if_absent(backend, route_local_path("/org-memory/", ORG_MEMORY_PATH), content)


async def seed_skill(repo: str, store: BaseStore, name: str, description: str, content: str) -> None:
    """Writes (or overwrites) one skill's SKILL.md and registers it in the
    project's manifest. Unlike seed_memory/seed_org_memory this is not
    idempotent-skip -- a skill is curated reference material authored
    deliberately (application code, not the agent), so re-running this with
    updated content is the intended way to revise a skill.
    """
    backend = StoreBackend(namespace=skills_namespace(repo), store=store)
    await backend.awrite(route_local_path(SKILLS_ROUTE, f"{SKILLS_ROUTE}{name}/SKILL.md"), content)

    manifest_key = route_local_path(SKILLS_ROUTE, SKILLS_MANIFEST_PATH)
    manifest_result = await backend.aread(manifest_key)
    manifest = {}
    if manifest_result.error is None and manifest_result.file_data:
        try:
            manifest = json.loads(file_data_to_string(manifest_result.file_data))
        except (json.JSONDecodeError, TypeError):
            manifest = {}
    manifest[name] = description
    await backend.awrite(manifest_key, json.dumps(manifest, indent=2))


async def load_skills_manifest(repo: str, store: BaseStore) -> dict[str, str]:
    """{skill name: description} for every skill registered to `repo`; empty
    when nothing is registered or the manifest is unreadable."""
    backend = StoreBackend(namespace=skills_namespace(repo), store=store)
    manifest_result = await backend.aread(route_local_path(SKILLS_ROUTE, SKILLS_MANIFEST_PATH))
    if manifest_result.error is not None or not manifest_result.file_data:
        return {}
    try:
        manifest = json.loads(file_data_to_string(manifest_result.file_data))
    except (json.JSONDecodeError, TypeError):
        return {}
    return manifest if isinstance(manifest, dict) else {}


async def unregister_skill(repo: str, store: BaseStore, name: str) -> int:
    """Removes one skill from `repo`: its manifest entry and every file under
    /skills/<name>/. Returns the number of files deleted. The inverse of
    seed_skill, for a skill the operator has decided the agents must not see.

    Goes to the store directly rather than through StoreBackend: the backend's
    `ls` is synchronous, which AsyncPostgresStore refuses inside a running
    event loop, and the async search is private. The namespace tuple and the
    key shape ("/<name>/<relpath>") are the ones skills_namespace/route_local_path
    produce, so this sees exactly what the agent's own read_file sees.
    """
    namespace = skills_namespace(repo)(None)
    manifest = await load_skills_manifest(repo, store)
    if name in manifest:
        del manifest[name]
        backend = StoreBackend(namespace=skills_namespace(repo), store=store)
        await backend.awrite(route_local_path(SKILLS_ROUTE, SKILLS_MANIFEST_PATH), json.dumps(manifest, indent=2))
    prefix = f"/{name}/"
    deleted = 0
    # Read the whole namespace first, then delete: deleting while paging
    # moves every later row up under the offset, so half of a multi-page
    # skill would survive the sweep that was meant to remove it.
    for item in await all_items(store, namespace):
        if item.key.startswith(prefix):
            await store.adelete(namespace, item.key)
            deleted += 1
    return deleted


async def load_skills_summary(repo: str, store: BaseStore) -> str:
    """The level-1 progressive-disclosure load: name+description only, for
    every registered skill, formatted for the system prompt. Manual
    workaround for the same AsyncPostgresStore incompatibility documented on
    build_deep_agent (SkillsMiddleware's own startup load calls the same
    broken download_files/adownload_files path) -- deepagents' `skills=`
    parameter is not used here for the same reason `memory=` isn't.
    """
    manifest = await load_skills_manifest(repo, store)
    if not manifest:
        return "(no skills registered for this project)"
    lines = [
        f"- {name}: {description} (full instructions: read {SKILLS_ROUTE}{name}/SKILL.md)"
        for name, description in manifest.items()
    ]
    return "\n".join(lines)


async def read_memory_or_empty(backend: StoreBackend, path: str) -> str:
    result = await backend.aread(path)
    if result.error is not None or not result.file_data:
        return "(nothing recorded yet)"
    return file_data_to_string(result.file_data)


@dataclass(frozen=True)
class ProjectMemory:
    """One project's memory as a seat's prompt carries it.

    `entries` is empty for a project that has never been split -- the content
    is then the whole file, exactly as it always was. When it is not empty,
    the content is the preamble plus the pinned sections plus the index, and
    `entries` is what the seat's read_memory_section tool is allowed to fetch.
    """

    content: str
    entries: list[memory_sections.IndexEntry] = field(default_factory=list)


async def _read_text(backend: StoreBackend, key: str) -> str | None:
    """The file's text, or None when it is not there. Distinct from
    read_memory_or_empty, which answers with a sentence for a system prompt --
    here the difference between "empty" and "absent" decides whether a whole
    code path applies."""
    result = await backend.aread(key)
    if result.error is not None or not result.file_data:
        return None
    return file_data_to_string(result.file_data)


async def load_memory_index(backend: StoreBackend) -> memory_sections.MemoryIndex:
    """This project's section index, with no entries for a project whose
    memory has never been split -- which is every project until the migration
    runs, and permanently for one small enough that splitting it would cost
    more than it saves (memory_sections.SPLIT_FLOOR_CHARS)."""
    raw = await _read_text(backend, route_local_path("/memories/", SECTIONS_INDEX_PATH))
    return memory_sections.parse_index_document(raw) if raw is not None else memory_sections.MemoryIndex(entries=[])


async def resplit_memory_sections(
    backend: StoreBackend, text: str, *, budget_tokens: int | None = None,
) -> list[str]:
    """Rewrite the section layer from `text`, for a project that already has one.

    The nightly consolidator (agent/consolidation.py) reads the whole memory,
    asks a model for an updated whole memory, and writes it back. Once a
    project is split, that write lands on a key the prompt no longer reads --
    so without this the consolidator would go on working, report success
    every night, and quietly stop reaching any agent. That is the exact
    failure this subsystem exists to prevent, arriving through the back door.

    Deterministic, and no model in the path: the same split the migration
    performs, against the text it is handed. Stale section files from a
    heading the consolidator removed are deleted, and the index is written
    LAST so an interruption leaves an orphaned body rather than an index
    entry pointing at nothing.

    Returns the slugs written. Does nothing, and returns [], for a project
    that has never been split -- whether because it is under
    SPLIT_FLOOR_CHARS or because the migration has not run.
    """
    index = await load_memory_index(backend)
    if not index.entries:
        return []

    budget = budget_tokens if budget_tokens is not None else int(
        _rs.value("memory_inline_token_budget"))
    preamble, sections = memory_sections.split_sections(text)
    entries = memory_sections.build_index(sections, preamble=preamble, budget_tokens=budget)

    await backend.awrite(route_local_path("/memories/", SECTIONS_CORE_PATH), preamble)
    for section in sections:
        await backend.awrite(
            route_local_path("/memories/", memory_sections.section_path(section.slug)), section.body)

    # Anything the consolidator dropped. Left behind it is unreachable rather
    # than harmful -- nothing indexes it -- but it would be read back by a
    # later "all" and reappear as memory nobody wrote.
    fresh = {s.slug for s in sections}
    for old in index.entries:
        if old.slug not in fresh:
            await backend.adelete(route_local_path("/memories/", memory_sections.section_path(old.slug)))

    await backend.awrite(
        route_local_path("/memories/", SECTIONS_INDEX_PATH),
        # source_sha256, NOT source_digest: the reader (_sections_match_source)
        # looks for that exact key, and a mismatch does not fail -- an index
        # with no recorded digest is taken at its word, so the drift check
        # that protects against an agent writing the whole file just stops
        # running. Silently. This wrote the wrong key once and disabled that
        # protection on live data until someone compared the two spellings.
        memory_sections.index_to_json(
            entries,
            source_sha256=memory_sections.source_digest(text),
        ))
    return [s.slug for s in sections]


async def gather_memory_sections(
    backend: StoreBackend, entries: list[memory_sections.IndexEntry],
) -> tuple[list[str], list[str]]:
    """(the core and every section body in index order, the slugs whose file
    was not there).

    With nothing missing, joining the parts is the original file byte for
    byte: a section body is a slice of it and the index preserves the order
    the cuts were made in. Shared by the rollback path here and by
    read_memory_section("all"), because a second copy of this loop is a second
    place for "in index order" to stop being true.
    """
    parts: list[str] = []
    missing: list[str] = []
    core = await _read_text(backend, route_local_path("/memories/", SECTIONS_CORE_PATH))
    if core is None:
        missing.append(SECTIONS_CORE_PATH)
    else:
        parts.append(core)
    for entry in entries:
        body = await _read_text(backend, route_local_path("/memories/", memory_sections.section_path(entry.slug)))
        if body is None:
            missing.append(entry.slug)
        else:
            parts.append(body)
    return parts, missing


async def _render_sectioned_memory(backend: StoreBackend, entries: list[memory_sections.IndexEntry]) -> str | None:
    """The prompt block for a split memory, or None if the index turns out to
    describe files that are not there.

    None matters more than it looks. An index without its sections is the one
    way this subsystem could silently amputate a project's memory -- the
    prompt would carry a confident list of sections and nothing behind it --
    so it degrades to the whole-file read instead, which is the behaviour of
    every version of this system before today.

    Which is why "all or nothing" is the rule here rather than "as much as we
    can find". A PARTIALLY present split is worse than an absent one, and the
    worst case of all is the one that reads as healthy: a pinned section whose
    file is gone renders as a prompt with the body missing and an index line
    next to it reading "already above, do not re-read" -- the content removed
    AND the model told not to go looking. The sections that get pinned are the
    ones the operator's policy pins BECAUSE they fail silently (test wiring,
    sandbox constraints, collision traps), so that is a silent failure about
    silent failures, and the only evidence would be work that quietly breaks a
    rule nobody can see any more. A whole-file read costs tokens; this costs a
    rule.

    Reachable without a bad migration, which writes the index last precisely
    so that an interruption leaves no index at all: a restore that replays
    some of a namespace, an operator deleting a section by hand to force a
    re-split, a later consolidator edit that writes an index entry whose
    section write did not land.

    An EMPTY /sections/_core.md is a real state -- a memory file that opens
    straight into `## ` has no preamble -- and is not the same as a missing
    one. The store distinguishes them (an empty file reads back as file_data
    with content ""), so _read_text's None genuinely means absent.
    """
    core = await _read_text(backend, route_local_path("/memories/", SECTIONS_CORE_PATH))
    if core is None:
        logger.warning("memory index exists but /sections/_core.md does not; reading the whole file")
        return None
    bodies: dict[str, str] = {}
    for entry in entries:
        if not entry.always:
            continue
        body = await _read_text(backend, route_local_path("/memories/", memory_sections.section_path(entry.slug)))
        if body is None:
            logger.warning("pinned memory section %s is indexed but missing; reading the whole file", entry.slug)
            return None
        bodies[entry.slug] = body
    # The budget is spent here and not only in the migration that set the
    # `always` flags, so an operator who lowers it sees the next task's prompt
    # shrink instead of having to re-run a migration to find out whether the
    # dial does anything. A section that no longer fits is demoted to an
    # ordinary index line, not dropped.
    return memory_sections.render_prompt_block(
        core, entries, bodies, budget_tokens=_rs.as_int("memory_inline_token_budget"),
    )


async def _sections_match_source(backend: StoreBackend, index: memory_sections.MemoryIndex) -> bool:
    """Whether the sections are still a faithful copy of /memories/AGENTS.md.

    They are cut from it, and it stays the authoritative copy until the
    pointer-stub commit -- while the nightly consolidator rewrites the whole
    file and the coordinator's prompt still tells the agent to record facts
    there. Both of those writes land somewhere no prompt reads the moment an
    index exists, and nothing about that is visible: the prompt still renders,
    the sections are still valid, they are just a snapshot of a file that has
    moved on. That is the memory subsystem losing memory, which is the one
    failure it cannot be allowed to have.

    So the index records what it was cut from, and a source that no longer
    matches turns this back into a whole-file read until the next migration
    run re-splits it -- self-healing rather than silent. An index with no
    recorded digest (hand-written, or from before this check) makes no claim
    about the source and is taken at its word.

    A note for whoever writes the pointer stub: rewriting /AGENTS.md must
    rewrite the index's source_sha256 with it, or every project falls back
    here -- loudly, to a prompt containing the stub, which is the failure
    being visible rather than quiet.
    """
    if not index.source_sha256:
        return True
    whole = await _read_text(backend, route_local_path("/memories/", MEMORY_PATH))
    if whole is None:
        return True
    if memory_sections.source_digest(whole) == index.source_sha256:
        return True
    logger.warning("/memories/AGENTS.md has changed since the split; reading it whole instead of the sections")
    return False


async def _whole_memory(backend: StoreBackend, entries: list[memory_sections.IndexEntry]) -> str:
    """The whole memory, for every path that is not a rendered split: the
    disclosure toggle turned off, an index that does not resolve, a project
    that was never split at all.

    Reassembled from the sections when there are any, because /AGENTS.md
    becomes a pointer stub one commit after the migration and the documented
    rollback ("the toggle restores the whole-file behaviour, without touching
    data") would otherwise hand the agent the stub. Reassembly IS the whole
    file -- byte for byte, asserted in tests -- so this is the same answer by
    a route that survives the stub. With no sections, or with one of them
    missing, it is the read this function has always been.
    """
    if entries:
        parts, missing = await gather_memory_sections(backend, entries)
        if not missing:
            return "".join(parts)
        logger.warning("cannot reassemble memory from sections (missing %s); reading /AGENTS.md", ", ".join(missing))
    return await read_memory_or_empty(backend, route_local_path("/memories/", MEMORY_PATH))


async def load_project_memory(repo: str, store: BaseStore, *, task_id: str | None = None) -> ProjectMemory:
    """The project memory block, for any seat that has one.

    ONE function, called by both the build coordinator and the planning chat.
    They had two copies of the same four lines, and the two seats drifting is
    not a hypothetical: the planner is where a fact gets recorded and the
    coordinator is where it has to be obeyed, so a section pinned in one seat
    and indexed in the other is a plan written against rules the build cannot
    see.

    With no index present this is byte for byte what it always did -- read
    /memories/AGENTS.md whole, attach the stale-flags block -- which is what
    makes deploying the reader before the data migration a no-op rather than a
    change to be verified in production.
    """
    backend = StoreBackend(namespace=project_namespace(repo), store=store)
    index = await load_memory_index(backend)
    entries = index.entries
    current = await _sections_match_source(backend, index)
    content: str | None = None
    # A dial rather than a redeploy, because the thing being rolled back is a
    # prompt: if progressive disclosure turns out to lose work, the operator
    # needs the old prompt on the next task, not after a deploy.
    if entries and current and _rs.value("memory_progressive_disclosure") >= 1:
        content = await _render_sectioned_memory(backend, entries)
    if content is None:
        # Reassembled from the sections when they are the good copy, and only
        # then -- if /AGENTS.md has moved on since the split it holds facts
        # the sections do not, and serving a stale reassembly instead would be
        # this subsystem losing exactly what it exists to keep.
        content = await _whole_memory(backend, entries if current else [])
        entries = []
    else:
        episode_recall.record_sections_offered(
            repo, [e.slug for e in entries],
            always=[e.slug for e in entries if e.always], task_id=task_id,
        )
    return ProjectMemory(content=await memory_with_freshness(backend, content), entries=entries)
