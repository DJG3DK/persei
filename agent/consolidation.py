"""The consolidation agent -- the documented deepagents pattern for
background memory maintenance: "a deep agent that reads recent conversation
history, extracts key facts, and merges them into the memory store... on a
cron schedule" (LangChain's own recommendation, not something invented for
this system). Runs separately from any task's own coordinator agent, on a
schedule (see scripts/run_consolidation.py), not during task work itself --
keeping the per-task hot path small is the entire point.

Reads this project's episodic records (agent/nodes/verify_and_ship.py writes
one per task at every terminal outcome) since the last consolidation run,
plus the CURRENT semantic memory (/memories/AGENTS.md), and produces an
UPDATED semantic memory -- distilling durable, non-obvious patterns across
multiple task outcomes rather than relying solely on the coordinator's own
ad-hoc mid-task edits (which still happen too; this is a second, more
deliberate mechanism layered on top, not a replacement).

Deliberately NOT a full agentic tool-loop: this is a bounded read-then-write
task with a small, fixed amount of input (recent episodes + current memory),
so a single structured completion is more reliable than an open-ended
tool-calling loop for it. Still built via create_deep_agent (not a bare LLM
call) per the docs' own "a deep agent" framing, and so it has real file
tools available if it ever needs to verify a claimed convention against the
actual repo rather than trusting an episode's summary blindly.
"""

import json
import time

from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI
from langgraph.store.base import BaseStore
from pydantic import BaseModel, Field

from langchain.agents.structured_output import ProviderStrategy
from langchain.agents.middleware import ModelCallLimitMiddleware, ToolCallLimitMiddleware

from agent.middleware.budget_guard import BudgetGuardMiddleware, BudgetTracker

from deepagents import create_deep_agent
from deepagents.backends import StoreBackend

from agent import history_index
from agent.config import Config, PROJECTS
from agent.deep_agent import (
    EPISODES_ROUTE,
    MEMORY_PATH,
    resplit_memory_sections,
    project_namespace,
    read_memory_or_empty,
    route_local_path,
    episodes_namespace,
)
from agent.store_paging import all_items
from agent.tools.agent_tools import make_agent_tools

# Stripped (route-local) keys, not the agent-visible /memories/... paths --
# these are read/written via a bare StoreBackend, which must use the same
# keys the composite's route stripping produces or this module reads a stale
# seeded copy while the agent's own memory updates sit invisible on the
# stripped key (see project_memory.route_local_path's docstring).
MEMORY_KEY = route_local_path("/memories/", MEMORY_PATH)
CONSOLIDATION_MARKER_PATH = "/.last_consolidated"
# Episodes are forensic records, not memory: consolidation transfers their
# content into /memories/AGENTS.md and nothing reads them afterwards. Keeping
# this many gives a recent trail to inspect by hand without letting the table
# grow one row per task forever.
EPISODE_RETENTION = 200

# audit C-7: consolidation ran with NO budget ceiling and no call-limit at all
# -- a nightly, unbounded tool loop that was the one create_deep_agent call in
# the system outside the hard dollar guard. A runaway (a model that loops
# reading files) could bill without limit until the 180s timeout, per project,
# every night. This is a bounded distillation job; a couple of dollars is
# already generous. Both guards below make "runaway" cost-bounded and
# call-bounded rather than time-bounded.
CONSOLIDATION_BUDGET_USD = 2.0
# 2026-10-01: a busy day (17 new episodes on one project) used all 40 model
# calls reading files before writing anything. The dollar ceiling above is
# what bounds a run's cost; these only stop a loop.
CONSOLIDATION_MODEL_CALL_LIMIT = 80
CONSOLIDATION_TOOL_CALL_LIMIT = 120
# The answer carries the whole rewritten memory, and the model thinks before
# it writes: on 2026-10-01 it spent 18,700 of the router's default 32,768
# output tokens reasoning and ran out mid-answer ($0.44, nothing written).
# Room for the answer, and a budget for the thinking.
CONSOLIDATION_MAX_OUTPUT_TOKENS = 60_000
CONSOLIDATION_REASONING_TOKENS = 8_000

MAX_EPISODES_PER_RUN = 50  # backstop -- see module docstring on why this stays small by design


class ConsolidationResult(BaseModel):
    updated_memory: str = Field(
        description=(
            "The full, complete new content for the project's semantic memory file (Markdown). "
            "Must include everything from the current memory that's still true, PLUS any new durable "
            "facts worth keeping from the episodes reviewed. Do not include noise, one-off details, "
            "or anything that only mattered for a single already-finished task."
        )
    )
    reasoning: str = Field(description="What changed and why, in a sentence or two.")


CONSOLIDATION_SYSTEM_PROMPT = """You are a memory-consolidation agent for the project at {repo_root} \
(project: {repo}). You do not write code and you do not modify the repo. Your only job: read the \
project's current semantic memory and a batch of recent task episodes (structured records of what \
happened on past tasks -- goal, outcome, cost, any escalation reason), and produce an UPDATED memory \
file that captures durable, genuinely useful patterns without becoming a noisy log of every task ever \
run.

Good candidates to add: a recurring failure mode across multiple episodes (worth a standing warning), \
a convention that got violated and caused an escalation (worth stating explicitly so it doesn't \
happen again), a gotcha that cost real iterations/budget to discover.

Some episodes carry `reflections`: what the task wrote after each failed attempt (what failed, why, \
what to do next). They are the best evidence of a project-specific gotcha, but only a task that went on \
to ship CONFIRMED its reflection; one from a task that escalated is an unproven guess. Turn a confirmed \
reflection that would apply again into a standing warning; do not record an unconfirmed one as fact.

Bad candidates to add: one-off task-specific details that won't matter again, anything already \
captured in the current memory, vague restatements that don't add real information.

If the episodes don't suggest anything worth adding, return the current memory UNCHANGED -- do not \
pad it with filler to seem useful. You may use your read/investigate tools against the real repo to \
verify a claimed convention is still accurate before committing it to memory, if that seems warranted."""


async def _list_recent_episode_paths(store: BaseStore, repo: str, since: str | None) -> list[str]:
    # audit H-21: return the OLDEST unconsolidated episodes, not the newest.
    # AsyncPostgresStore.asearch orders updated_at DESC, so the old
    # `asearch(limit=MAX_EPISODES_PER_RUN)` returned the 50 NEWEST since the
    # marker. When more than 50 were pending, the oldest were never reviewed --
    # yet the marker advanced to the newest of the batch, and
    # _prune_consolidated_episodes then deleted everything at-or-below the marker
    # (minus the retention window) as "already distilled into memory." So the
    # oldest episodes were dropped unread. Page through ALL pending episodes,
    # sort ascending (episode keys are timestamp-ordered), and hand back the
    # oldest MAX_EPISODES_PER_RUN; the caller sets the marker to the newest of
    # THIS batch, so the next run resumes exactly where this one stopped and
    # nothing is skipped or pruned before it is consolidated.
    ns = episodes_namespace(repo)(None)
    prefix = f"{EPISODES_ROUTE}{since}" if since is not None else None
    pending = [
        item.key for item in await all_items(store, ns)
        if prefix is None or item.key > prefix
    ]
    pending.sort()  # ascending == oldest first
    return pending[:MAX_EPISODES_PER_RUN]


async def _read_episode(backend: StoreBackend, path: str) -> dict | None:
    result = await backend.aread(path)
    if result.error is not None or not result.file_data:
        return None
    from deepagents.backends.utils import file_data_to_string

    try:
        return json.loads(file_data_to_string(result.file_data))
    except (json.JSONDecodeError, TypeError):
        return None


async def run_consolidation(config: Config, repo: str, checkpointer, store: BaseStore) -> dict:
    """Runs one consolidation pass for a single project. Returns a small
    summary dict (episodes_reviewed, memory_changed, reasoning) -- meant to
    be logged by whatever triggers this (scripts/run_consolidation.py).
    """
    repo_root = PROJECTS[repo]["sandbox"]
    episodes_backend = StoreBackend(namespace=episodes_namespace(repo), store=store)
    project_backend = StoreBackend(namespace=project_namespace(repo), store=store)

    marker = await project_backend.aread(CONSOLIDATION_MARKER_PATH)
    since = None
    if marker.error is None and marker.file_data:
        from deepagents.backends.utils import file_data_to_string

        since = file_data_to_string(marker.file_data).strip() or None

    # ORDER IS LOAD-BEARING: this is step (1) of (1) index, (2) write
    # memory, (3) advance the marker, (4) prune. _prune_consolidated_episodes
    # at the bottom of this function permanently DELETEs store rows, and an
    # episode deleted before it is indexed is gone -- there is no second
    # copy anywhere. Moving this call below the prune destroys history
    # silently; tests/test_history_index_ordering.py fails if it moves.
    #
    # The order alone was never the guarantee, though, and that was the
    # hole: the prune ran unconditionally, so a night when the index was
    # unreachable reported a failed sync and then deleted the rows anyway.
    # `indexed.copied` below is the actual guard -- the ordering only makes
    # it possible to ask.
    #
    # Before the early return below as well, and not only before the prune:
    # the other two corpora (a project's task rows and its build
    # transcripts) keep changing on a day when no task produced an episode,
    # and this nightly pass is the only thing that reconciles them.
    indexed = await history_index.sync_project(config, repo, store)

    episode_paths = await _list_recent_episode_paths(store, repo, since)
    if not episode_paths:
        return {"episodes_reviewed": 0, "memory_changed": False,
                **_history_summary(indexed),
                "reasoning": "no new episodes since last run"}

    episodes = []
    for path in episode_paths:
        record = await _read_episode(episodes_backend, path)
        if record:
            episodes.append(record)

    current_memory = await read_memory_or_empty(project_backend, MEMORY_KEY)
    episodes_text = "\n\n".join(
        f"- task {e['task_id']}: goal={e['goal']!r}, outcome={e['outcome']}, "
        f"cost=${e['cost_usd']:.2f}, iterations={e['iteration_count']}"
        + (f", escalation_reason={e['escalation_reason']!r}" if e.get("escalation_reason") else "")
        + (f", review_verdict={e['review_verdict']!r}" if e.get("review_verdict") else "")
        + (f", reflections={e['reflections']!r}" if e.get("reflections") else "")
        for e in episodes
    )

    model = ChatOpenAI(
        model="agent-consolidator",  # dedicated pin -- consolidation quality matters more than cost -- infrequent, small, background
        base_url=config.router_base_url,
        api_key=config.router_api_key,
        temperature=0,
        timeout=300,
        max_tokens=CONSOLIDATION_MAX_OUTPUT_TOKENS,
        extra_body={"reasoning": {"max_tokens": CONSOLIDATION_REASONING_TOKENS}},
    )
    project_tools, _ = make_agent_tools(repo_root)  # read-only consolidation -- no edit-guard state to track
    read_only_tools = [t for t in project_tools if t.name in ("read", "bash")]

    consolidation_tracker = BudgetTracker(budget_usd=CONSOLIDATION_BUDGET_USD)
    agent = create_deep_agent(
        model=model,
        tools=read_only_tools,
        middleware=[
            # audit C-7: the hard dollar ceiling now covers consolidation too.
            BudgetGuardMiddleware(consolidation_tracker),
            ModelCallLimitMiddleware(run_limit=CONSOLIDATION_MODEL_CALL_LIMIT, exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=CONSOLIDATION_TOOL_CALL_LIMIT, exit_behavior="error"),
        ],
        system_prompt=CONSOLIDATION_SYSTEM_PROMPT.format(repo_root=repo_root, repo=repo),
        # ProviderStrategy, not a bare schema. Passing the schema alone resolves
        # to AutoStrategy, which picked a FORCED TOOL CALL -- and OpenRouter's
        # providers for several model families reject tool_choice=required/object
        # while the model is in thinking mode. That failed the nightly run for
        # any project with episodes to consolidate, silently, for months.
        #
        # ProviderStrategy uses the provider's native structured output instead.
        # Verified 2026-08-25 on qwen3.8-max, the model that could not do it the
        # other way: bare(Auto) FAIL, ToolStrategy FAIL, ProviderStrategy OK.
        # This removes the model constraint entirely rather than routing around it.
        response_format=ProviderStrategy(ConsolidationResult),
        checkpointer=checkpointer,
        store=store,
    )

    prompt = (
        f"Current memory:\n<memory>\n{current_memory}\n</memory>\n\n"
        f"Episodes since last consolidation ({len(episodes)} total):\n<episodes>\n{episodes_text}\n</episodes>"
    )
    # Retry on structured-output parse failures (2026-08-28): a model can
    # stochastically wrap its JSON in a fence or preamble -- claude-haiku-4.5
    # did exactly once ("Expecting value: line 1 column 2") and, with zero
    # retries, one slip failed the whole night for that project. Parse slips
    # are non-deterministic, so a fresh attempt (fresh thread id -- never
    # resume into the malformed state) usually clears; a model that fails
    # ALL attempts still fails loudly through the existing path.
    last_err: Exception | None = None
    for attempt in range(3):
        thread_id = f"consolidation:{repo}:{time.strftime('%Y%m%d%H%M%S')}:a{attempt}"
        try:
            result = await agent.ainvoke(
                {"messages": [HumanMessage(content=prompt)]},
                config={"configurable": {"thread_id": thread_id}},
            )
            structured: ConsolidationResult = result["structured_response"]
            break
        except Exception as e:  # noqa: BLE001 -- classified below, re-raised if not a parse slip
            if "structured output" not in str(e).lower() and "parsing failed" not in str(e).lower():
                raise
            last_err = e
            print(f"[consolidation] {repo}: structured-output parse slip on attempt {attempt + 1}/3 -- retrying")
    else:
        raise RuntimeError(f"structured output failed on all 3 attempts: {last_err}")

    memory_changed = structured.updated_memory.strip() != current_memory.strip()
    if memory_changed:
        await project_backend.awrite(MEMORY_KEY, structured.updated_memory)
        # And the section layer, for a project that has one. This job writes
        # the whole memory to one key; every seat's prompt reads the sections
        # instead once a project is split, so without this the consolidation
        # would keep succeeding nightly and quietly stop reaching any agent --
        # which is the silent failure the split exists to prevent, arriving
        # from the other side. A no-op for a project that was never split.
        resplit = await resplit_memory_sections(project_backend, structured.updated_memory)
        if resplit:
            print(f"[consolidation] {repo}: re-split memory into {len(resplit)} section(s)")

    last_path = episode_paths[-1]
    new_marker = last_path[len(EPISODES_ROUTE):]
    await project_backend.awrite(CONSOLIDATION_MARKER_PATH, new_marker)

    # THE GUARD, not the ordering above it. _prune_consolidated_episodes
    # permanently DELETEs store rows whose only other copy is the row
    # sync_project was supposed to have written, so it may run only when
    # that copy actually happened. Skipping a prune costs one night of extra
    # rows in a table that already holds thousands; running it when the copy
    # did not happen costs history that exists nowhere else.
    pruned = 0
    if indexed.copied:
        pruned = await _prune_consolidated_episodes(store, repo, new_marker)

    return {
        "episodes_reviewed": len(episodes),
        "memory_changed": memory_changed,
        "episodes_pruned": pruned,
        **_history_summary(indexed),
        "reasoning": structured.reasoning,
    }


def _history_summary(indexed) -> dict:
    """What the nightly pass did to the search index, for the operator.

    Printed wholesale by scripts/run_consolidation.py, which is the only
    place anybody sees this job at all. `history_rows_written: 0` alone was
    not enough to tell anyone anything -- it is also exactly what a healthy
    night with nothing new prints -- so a failure is named here rather than
    left to a logger.warning that reaches the log only through Python's
    lastResort handler, no module in this tree having called basicConfig.
    """
    summary = {
        "history_rows_written": indexed.written,
        "history_rows_demoted": indexed.demoted,
    }
    if not indexed.copied:
        summary["history_failed"] = list(indexed.failed) or [indexed.skipped]
        summary["episodes_not_pruned"] = indexed.why_not_copied
    return summary


async def _prune_consolidated_episodes(store: BaseStore, repo: str, marker: str) -> int:
    """Drop episodes that have already been distilled into memory, keeping the
    most recent EPISODE_RETENTION as a forensic window.

    Episodes were never pruned, so /episodes/{repo} grew without bound -- one
    row per task, forever, in the same Postgres store that serves every task's
    memory reads. Nothing consumes an episode after consolidation has read it;
    its value has been transferred into /memories/AGENTS.md by then.

    Two safety rules make this conservative:
      * Only episodes at or below the just-written marker are eligible -- an
        unconsolidated episode can never be deleted, so a failed or skipped
        consolidation run cannot lose history.
      * The newest EPISODE_RETENTION are always kept regardless, so there is
        still a recent trail to read by hand when something goes wrong.
    """
    ns = episodes_namespace(repo)(None)
    # all_items, not a bare asearch(limit=1000): a limit on an updated_at
    # ordering is a PAGE, and past 1000 episodes both tests below -- "are
    # there more than the retention window" and "which are the newest
    # EPISODE_RETENTION" -- were being computed against an arbitrary window
    # of the namespace by the most destructive function in the repository.
    items = await all_items(store, ns)
    keys = sorted(item.key for item in items)
    if len(keys) <= EPISODE_RETENTION:
        return 0
    cutoff = f"{EPISODES_ROUTE}{marker}"
    eligible = [k for k in keys[:-EPISODE_RETENTION] if k <= cutoff]
    for key in eligible:
        await store.adelete(ns, key)
    return len(eligible)
