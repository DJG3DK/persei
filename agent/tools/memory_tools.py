"""Pulling a section of project memory that the prompt only advertised.

The index in the system prompt is a promise: these sections exist, here is
what is in each one, fetch the one you need. This is the other half of it.

It is a named tool rather than the built-in read_file for one reason -- a
model reads what its prompt tells it to read, and "call read_memory_section
with the slug" is an instruction the index can state in the same line as the
slug, while "read /memories/sections/<slug>.md with read_file" asks it to
know which of the two filesystems in this system that path lives on (see
_FILESYSTEM_GUIDANCE in agent/deep_agent_prompts.py -- getting that wrong is an
established failure here, not a theoretical one). It also gives the read a
place to be counted, which is how anyone finds out whether the index works.

The escape hatch is deliberate. "all" returns the whole file, because when a
model is unsure, one expensive call that definitely contains the answer beats
three cheap ones that might not -- and it puts a floor under how badly a bad
index entry can fail.
"""

from __future__ import annotations

import logging

from langchain_core.tools import BaseTool, tool
from langgraph.store.base import BaseStore

from deepagents.backends import StoreBackend
from deepagents.backends.utils import file_data_to_string

from agent import episode_recall, memory_sections
# A top-level import, where these used to be fetched inside three one-line
# helpers to dodge a cycle through agent/deep_agent.py (which imports this
# module to build a seat's tools). The store layer lives in
# agent/project_memory.py now, which imports nothing that leads back here.
from agent.project_memory import MEMORY_PATH, gather_memory_sections, project_namespace, route_local_path

logger = logging.getLogger("tektonix")

ALL = "all"


def make_memory_tools(repo: str, store: BaseStore | None, entries: list[memory_sections.IndexEntry],
                      *, task_id: str | None = None) -> list[BaseTool]:
    """The memory-section toolset for one seat.

    Returns [] when there is nothing to fetch -- no store, or a project whose
    memory has never been split -- following agent/tools/logo_tools.py's rule
    that a seat is given a tool only when it would actually work. For an
    unsplit project the whole memory is already in the prompt, so the tool
    could only ever hand back text the model is looking at, and every seat
    would pay for its description on every call to be told so.
    """
    if store is None or not entries:
        return []

    backend = StoreBackend(namespace=project_namespace(repo), store=store)
    by_slug = {e.slug: e for e in entries}

    @tool
    async def read_memory_section(section: str) -> str:
        """Read one section of this project's memory, by the slug shown in the
        MEMORY INDEX in your system prompt (for example: "test-wiring").

        The index lists every section that is NOT already in your prompt, with
        what each one contains. Read a section BEFORE you act in its area --
        these are this project's own hard-won rules, and the usual way to
        break one is not to know it existed.

        Pass "all" to get the entire memory file when you are not sure which
        section you need. One read that definitely contains the answer is
        cheaper than a rework cycle.
        """
        wanted = (section or "").strip().strip("/").removesuffix(".md").lower()
        if wanted in (ALL, "*", "everything"):
            episode_recall.record_section_read(ALL, repo, task_id=task_id)
            return await _read_everything(backend, entries)
        entry = by_slug.get(wanted)
        if entry is None:
            # Naming what does exist, rather than just refusing: a model that
            # guessed a slug and is told only "not found" guesses again, and
            # the index it would have needed is a thousand tokens back up its
            # own context.
            listing = "\n".join(f"- {e.slug}: {e.title}" for e in entries)
            return (f'No memory section named "{section}". The sections of this project\'s memory are:\n'
                    f'{listing}\n\nOr call read_memory_section("all") for the whole file.')
        body = await _read_section(backend, entry.slug)
        if body is None:
            return (f'The index lists "{entry.slug}" but its file is missing. '
                    f'Call read_memory_section("all") to read the memory directly.')
        episode_recall.record_section_read(entry.slug, repo, task_id=task_id)
        return body

    return [read_memory_section]


async def _read_text(backend: StoreBackend, key: str) -> str | None:
    """The file's text, or None when it is not there. An empty file is text,
    not absence -- a memory that opens straight into `## ` has no preamble and
    its /sections/_core.md is legitimately empty."""
    result = await backend.aread(key)
    if result.error is not None or not result.file_data:
        return None
    return file_data_to_string(result.file_data)


async def _read_section(backend: StoreBackend, slug: str) -> str | None:
    body = await _read_text(backend, route_local_path("/memories/", memory_sections.section_path(slug)))
    if body is None:
        logger.warning("memory section %s is indexed but unreadable", slug)
    return body


async def _read_everything(backend: StoreBackend, entries: list[memory_sections.IndexEntry]) -> str:
    """Core plus every section in index order -- which is the original file,
    byte for byte, because a section body is a slice of it and the index
    preserves the order they were cut in (agent/memory_sections.py).

    Reassembly is the only thing here that can go quietly wrong, and it is the
    call a model makes precisely BECAUSE it does not know what it is looking
    for. A section whose file is missing would drop out of the join and leave
    a file that reads as complete and is not -- the model would conclude the
    project has no rule about whatever was in the gap, which is a stronger and
    more dangerous conclusion than "I could not find it". So a gap is never
    joined over: the whole file is read instead if it is still there, and said
    out loud if it is not. The escape hatch's entire value is being the answer
    a model can trust without checking.
    """
    parts, missing = await gather_memory_sections(backend, entries)
    if not missing:
        return "".join(parts) or "(nothing recorded yet)"
    # /memories/AGENTS.md is the source these sections were cut from and it
    # outlives the split (the migration writes only new keys; the pointer stub
    # replaces it later, and only after every project has been verified), so
    # for as long as it is real memory it is the better answer.
    whole = await _read_text(backend, route_local_path("/memories/", MEMORY_PATH))
    if whole:
        logger.warning("memory sections %s are missing; served /AGENTS.md instead", ", ".join(missing))
        return whole
    logger.warning("memory sections %s are missing and /AGENTS.md is not readable", ", ".join(missing))
    return "".join(parts) + (
        f"\n\n[INCOMPLETE: this project's memory is missing {', '.join(missing)}. "
        f"What is above is the rest of it. Tell the operator.]\n"
    )
