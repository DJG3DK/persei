"""Asking what past tasks ran into, from inside a task.

Every terminal task leaves an episode -- the goal, how it ended, and when it
went wrong, the actual text of the failure. Nothing could read one. The
consolidation agent turns recurring patterns into memory, which is right for
a pattern and useless for a one-off: the single escalation from three months
ago that says exactly why this merge cannot fast-forward is not a pattern, so
it never reaches memory, and until now it was reachable from nowhere.

Two tools, deliberately, and not three. `search_history` returns a DIGEST --
a line of metadata and a highlighted fragment per hit, enough to decide which
record is worth opening -- and `read_history` opens one. The split is the
whole economy of the thing: an episode runs to thousands of tokens, so eight
of them returned whole would be ~25,000 tokens spent before the model has
decided any of them is relevant, while eight digest entries are under a
thousand.

There is no second search tool either, and there will not be one when an
embedding index arrives. Two tools answering "have we hit this before" over
one corpus is a routing decision a model gets wrong half the time, and each
description is prompt tokens in every seat that carries it. A new way of
looking things up registers as a LEG in agent/episode_recall.py, which is
what these tools ask -- never the index directly.

Cross-project reach goes through agent/tools/reference_tools.py's own gate,
imported rather than re-derived: a task may search the history of exactly
the projects its creator could read. The one difference is inverted on
purpose -- the reference tools REFUSE your own project (two path semantics
for one file is how a model edits the wrong copy) and these DEFAULT to it,
because there is no file to confuse and the question almost always means
"here".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from langchain_core.tools import BaseTool, tool

from agent import episode_recall, history_index
from agent.config import PROJECTS
# The read window is reference_tools' own, shared so both readers page alike.
# pyright: reportPrivateUsage=false
from agent.tools.reference_tools import _READ_CAP_CHARS, _READ_MIN_WINDOW, check_readable
from agent.tools.tool_errors import tool_errors_to_text

# What the model is allowed to say for `corpus`, and what each one means.
# Spelled in the words a model reaches for rather than the table's column
# values: a tool that refuses "episodes" because the column says "episode"
# costs a round trip to teach something that could have been accepted.
_CORPUS_CHOICES: dict[str, tuple[str, ...]] = {
    "all": history_index.CORPORA,
    "episodes": (history_index.CORPUS_EPISODE,),
    "episode": (history_index.CORPUS_EPISODE,),
    "tasks": (history_index.CORPUS_TASK,),
    "task": (history_index.CORPUS_TASK,),
    "build_logs": (history_index.CORPUS_TASK_LOG,),
    "build_log": (history_index.CORPUS_TASK_LOG,),
    "task_log": (history_index.CORPUS_TASK_LOG,),
    "task_logs": (history_index.CORPUS_TASK_LOG,),
    "logs": (history_index.CORPUS_TASK_LOG,),
}

# How each corpus is named back to the model. "task_log" is the store
# namespace; "build log" is what it actually is.
_CORPUS_WORD = {
    history_index.CORPUS_EPISODE: "episode",
    history_index.CORPUS_TASK: "task",
    history_index.CORPUS_TASK_LOG: "build log",
}

# The digest's budget, per hit. Four short lines: what and when, the ref to
# open it with, the one-line label, and the matched fragment. Measured at
# these values a default page of eight hits is well under 1,000 tokens,
# which is the number that decides whether a coordinator can afford to ask
# the question on a whim.
_LABEL_CHARS = 110
_SNIPPET_CHARS = 230

# A year is long enough that nobody sets it, and short enough that
# `since_days` cannot be turned into an overflow.
_MAX_SINCE_DAYS = 3650


def readable_projects(own_repo: str, readable: list[str] | None, *,
                      none_means_all: bool = False) -> list[str]:
    """Which projects' history this seat may search.

    The two callers genuinely differ and the difference is not a bug. A
    build task carries `reference_repos`, a concrete list resolved from its
    creator's access at creation time, and an empty one means its own repo
    alone -- a task checkpointed before that field existed must not silently
    widen to everything. The planner carries `allowed_repos`, where None is
    how an admin is represented, so there None means every project.

    Own repo is always in, because it is the default and the common case.
    """
    if readable is None and none_means_all:
        allowed = set(PROJECTS)
    else:
        allowed = {r for r in (readable or [])}
    allowed.add(own_repo)
    return sorted(r for r in allowed if r in PROJECTS)


def available() -> bool:
    """Whether this process can answer a history search at all.

    The capability convention (agent/capabilities.py): when this is False
    the factory below returns nothing and the seats are built without the
    tools, rather than carrying two tools that can only ever say the index
    is unreachable.
    """
    return episode_recall.available() and history_index.default_index() is not None


def _trim(text: str, cap: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= cap else text[:cap - 1].rstrip() + "…"


def _without_prefix(label: str, prefix: str | None) -> str:
    """The label minus the outcome, when the header already printed it."""
    if prefix and label.startswith(prefix):
        return label[len(prefix):].strip()
    return label


def _digest_entry(n: int, hit: episode_recall.EpisodeHit) -> str:
    extra = hit.extra or {}
    when = extra.get("occurred_at")
    corpus = _CORPUS_WORD.get(extra.get("corpus") or "", str(extra.get("corpus") or "?"))
    # The other corpora describing this same task, named rather than given
    # slots of their own -- see history_index._fold_by_task.
    also = [_CORPUS_WORD.get(c, str(c)) for c in extra.get("also") or ()]
    bits = [corpus + (f" (+{', '.join(also)})" if also else ""),
            when.date().isoformat() if isinstance(when, datetime) else "undated",
            hit.repo]
    if extra.get("outcome"):
        bits.append(str(extra["outcome"]))
    if extra.get("found_by") == ["vector"]:
        # Said out loud because a correct semantic hit is the one result
        # that looks like a broken search: it is here precisely BECAUSE it
        # shares no word with the query, and unlabelled it reads as noise
        # the model should ignore -- which is the one case this leg exists
        # for.
        bits.append("semantic match, no shared words")
    if extra.get("stage") == "widened":
        # Hit.stage was computed, carried through the leg and into this
        # dict, and then never shown to anybody. It is the difference
        # between a hit worth trusting and a hit worth glancing at, and
        # without it a question the system has never seen comes back looking
        # exactly like one it has -- which is the failure the design named
        # as the worst, because it looks useless rather than broken and so
        # nobody reports it.
        bits.append("widened")
    if not extra.get("source_live", True):
        # Said plainly rather than left to be discovered: the record is
        # still readable, but nothing else in the system remembers it, so a
        # model that goes looking for the task in the dashboard will not
        # find one.
        bits.append("archived — the store record has been pruned")
    lines = [f"[{n}] " + " · ".join(bits), f"    ref={hit.ref}"]
    label = _trim(_without_prefix(extra.get("label") or "", extra.get("outcome")), _LABEL_CHARS)
    if label:
        lines.append(f"    {label}")
    snippet = _trim(hit.snippet, _SNIPPET_CHARS)
    if snippet:
        lines.append(f"    {snippet}")
    return "\n".join(lines)


def make_history_tools(own_repo: str, readable: list[str] | None, store: object, *,
                       none_means_all: bool = False, task_id: str | None = None) -> list[BaseTool]:
    """The two history tools, or nothing when this installation has no index.

    `store` is handed to the recall registry rather than used here: the leg
    that exists today reads its own table, and the one that comes next reads
    the store, and a tool that knows which is which would have to change
    when the second arrives.
    """
    if not available():
        return []
    scope = readable_projects(own_repo, readable, none_means_all=none_means_all)
    others = [r for r in scope if r != own_repo]

    @tool
    @tool_errors_to_text
    async def search_history(query: str, repo: str = "", corpus: str = "all",
                             since_days: int = 0, limit: int = 8) -> str:
        """Search what past tasks in this system ran into: their goals, how they
        ended, and the text of what failed.

        `query` is free text -- paste the error message or describe the symptom.
        `repo` defaults to this project ("*" searches every project you may
        read: {others}). `corpus` is "all", "episodes" (how a task ended),
        "tasks", or "build_logs" (the step-by-step transcript). `since_days`
        limits how far back; 0 is no limit.

        Returns a short digest per hit -- never the records themselves. Open
        one with read_history(ref=...) using the ref printed with it.
        """
        corpora = _CORPUS_CHOICES.get((corpus or "all").strip().lower())
        if corpora is None:
            return (f"ERROR: unknown corpus {corpus!r} -- use all, episodes, tasks "
                    "or build_logs")
        target = (repo or "").strip()
        if target in ("", own_repo):
            repos = [own_repo]
        elif target == "*":
            repos = scope
        else:
            try:
                check_readable(target, scope)
            except ValueError as e:
                return f"ERROR: {e}"
            repos = [target]

        since = None
        if since_days and since_days > 0:
            since = datetime.now(UTC) - timedelta(days=min(since_days, _MAX_SINCE_DAYS))

        # Capped here as well as in the index, because the model is told.
        # Query length is superlinear in cost -- 1,200 characters measured
        # at 0.19s against 24,000 at 23.67s -- and a model whose paste was
        # silently truncated has no way to know why it got the wrong answer.
        note = ""
        if len(query) > history_index.MAX_QUERY_CHARS:
            note = (f"\n\n(Only the first {history_index.MAX_QUERY_CHARS} characters of that "
                    "query were searched. Search the distinctive lines -- the error string, "
                    "the command, the path -- rather than a whole transcript.)")
            query = query[:history_index.MAX_QUERY_CHARS]

        errors: list[str] = []
        hits = await episode_recall.recall_episodes(
            store, own_repo, query,
            limit=max(1, min(limit or history_index.DEFAULT_LIMIT, history_index.MAX_LIMIT)),
            repos=repos, corpora=corpora, since=since, errors=errors,
        )
        # Recorded whether or not anything came back, and recorded here
        # rather than inside the index: a search that returns nothing is the
        # measurement that decides whether a second retrieval leg is worth
        # building, and it is the one the index never sees a reason to write
        # down. `own_repo` is who asked; each ref carries where it came from.
        episode_recall.record_query(
            query, own_repo, [h.ref for h in hits], task_id=task_id,
            found_by={h.ref: (h.extra or {}).get("found_by") or [] for h in hits},
        )

        where = repos[0] if len(repos) == 1 else f"{len(repos)} projects"
        if not hits:
            if errors:
                # A search that FAILED, said as a failure. Reported as "no
                # history matched" it is indistinguishable from a corpus
                # that holds nothing, and the model's correct response to
                # the two is opposite: retry narrower, or stop asking.
                return (f"ERROR: the history search did not complete, so this is not an "
                        f"answer about whether {query[:120]!r} has a history: "
                        f"{'; '.join(errors)[:300]}" + note)
            return (
                f"No history matched {query!r} in {where}. The index covers this system's own "
                "past tasks, not the code -- if you are looking for code, use search. "
                "Otherwise try the distinctive words alone (an error string, a command, a "
                "path), drop the ones that could be in any task, or widen with repo='*'."
            ) + note
        body = "\n".join(_digest_entry(n, h) for n, h in enumerate(hits, 1))
        widened = sum(1 for h in hits if (h.extra or {}).get("stage") == "widened")
        caveat = ""
        if widened:
            caveat = (f"\nNothing matched every term. {widened} of these are marked "
                      "widened: they matched only some of the query.\n")
        return (
            f"{len(hits)} match(es) for {query!r} in {where}, best first:\n{caveat}\n{body}\n\n"
            f"That is a digest. Open one in full with "
            f"read_history(ref={hits[0].ref!r}) -- and only the ones you actually need." + note
        )

    @tool
    @tool_errors_to_text
    async def read_history(ref: str, offset: int = 0, limit: int = 0) -> str:
        """Read one record search_history found, in full.

        `ref` is the ref printed with a hit ("episode:project:/episodes/...").
        `offset`/`limit` page through a long one by line, the same way
        read_project_file does.
        """
        try:
            corpus, repo, item_key = history_index.parse_ref(ref)
        except ValueError as e:
            return f"ERROR: {e}"
        # Re-checked on every call, not trusted because a search produced it:
        # a ref is a name, not a capability, and one can reach this seat
        # through a plan, a checkpoint or a summary written when the task had
        # a different scope.
        try:
            check_readable(repo, scope)
        except ValueError as e:
            return f"ERROR: {e}"
        if corpus not in history_index.CORPORA:
            return (f"ERROR: unknown history corpus {corpus!r} -- refs start with one of "
                    f"{', '.join(history_index.CORPORA)}")

        index = history_index.default_index()
        if index is None:
            return "ERROR: the history index is not available in this process"
        record = await index.fetch(corpus, repo, item_key)
        if record is None:
            return f"ERROR: no history record at {ref!r}"
        episode_recall.record_use(ref, repo, task_id=task_id)

        when = record["occurred_at"]
        head = [f"{_CORPUS_WORD.get(corpus, corpus)} · {repo} · "
                f"{when.isoformat() if isinstance(when, datetime) else 'undated'}"]
        if record.get("outcome"):
            head.append(f"outcome: {record['outcome']}")
        if record.get("task_id"):
            head.append(f"task: {record['task_id']}")
        if not record.get("source_live", True):
            head.append("the store record has been pruned; this is the archived copy")
        parts = [" · ".join(head)]
        if record.get("err"):
            parts.append(f"\nFAILED WITH:\n{record['err']}")
        parts.append(f"\n{record['text']}")
        content = "\n".join(parts)

        lines = content.split("\n")
        if offset or limit:
            start = max(0, (offset or 1) - 1)
            count = max(limit if limit and limit > 0 else 400, _READ_MIN_WINDOW)
            window = lines[start:start + count]
            if not window:
                return f"(no lines at offset {offset} -- {ref!r} has {len(lines)} lines)"
            numbered = "\n".join(f"{start + i + 1}\t{ln}" for i, ln in enumerate(window))
            remaining = len(lines) - (start + len(window))
            return numbered + (
                f"\n\n[lines {start + 1}-{start + len(window)} of {len(lines)}"
                + (f"; {remaining} more after this]" if remaining > 0 else "]")
            )
        if len(content) > _READ_CAP_CHARS:
            # The same message read_project_file gives, because it exists for
            # a failure that happens here identically: a model whose read came
            # back truncated asks for the same thing again, and again.
            head_text = content[:_READ_CAP_CHARS]
            shown = head_text.count("\n") + 1
            return (
                f"{head_text}\n\n[TRUNCATED. {ref!r} is {len(content)} chars / {len(lines)} "
                f"lines; you have seen lines 1-{shown}. Reading it again the same way returns "
                f"this SAME text -- page with read_history(ref={ref!r}, offset={shown}, "
                "limit=800).]"
            )
        return content

    # Named in the description rather than left to be guessed: the only way
    # to get `repo` wrong is not to know what the options were.
    search_history.description = search_history.description.replace(
        "{others}", ", ".join(others) if others else own_repo)
    return [search_history, read_history]


def guidance(own_repo: str, readable: list[str] | None, *, none_means_all: bool = False) -> str:
    """The prompt note, or nothing when the tools are not there.

    Conditional for the reason agent/tools/logo_tools.py's block is: a model
    told about a tool it does not have will try it, be refused, and record
    that this system cannot do the thing.

    It says WHEN, not "often". These tools cost a model call and real money
    each time, and a coordinator that searches history at every step is the
    failure mode this wording exists to avoid.
    """
    if not available():
        return ""
    scope = readable_projects(own_repo, readable, none_means_all=none_means_all)
    others = [r for r in scope if r != own_repo]
    note = (
        "\n\nWHAT PAST TASKS RAN INTO:\n\n"
        f"`search_history(query)` searches this system's own record of past work on {own_repo} "
        "-- how tasks ended, why they escalated, and their build transcripts. It returns a "
        "digest; `read_history(ref=...)` opens one.\n\n"
        "Two moments, not more. When something FAILS in a way that looks like it has a "
        "history (a merge that will not land, a check that will not pass, an environment "
        "that is not what the code expects) -- paste the error text as the query. And "
        "before committing to an approach that may already have been tried, where \"that "
        "was tried and it escalated\" changes the plan.\n\n"
        "It searches past TASKS, never the code -- search the repo for that. Two searches "
        "that find nothing mean there is nothing there."
    )
    if others:
        note += f" Pass repo='*' for every project you may read: {', '.join(others)}."
    return note
