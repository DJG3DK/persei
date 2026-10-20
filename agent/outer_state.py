"""State schema for the outer graph (work -> verify_and_ship).

The deep agent's own `write_todos` state (living in its own checkpointed
thread, keyed `f"{task_id}:work"`, not this outer state) is the plan; the
outer graph doesn't keep its own parallel copy of it.
"""

from typing import Annotated, TypedDict

import operator


class LogEntry(TypedDict):
    node: str
    step_id: str | None
    summary: str
    detail: str
    cost_usd: float
    timestamp: str


class AgentState(TypedDict):
    # Inputs, set once at task creation
    task_id: str
    goal: str
    repo: str
    budget_usd: float
    max_iterations: int  # overall ceiling on work<->verify_and_ship cycles

    iteration_count: int  # counts work<->verify_and_ship cycles (loops where a check or review failed)
    # Consecutive "checks pass but no diff" passes -- see verify_and_ship.py's
    # own comment: a legitimately investigation-only task (correctly
    # producing no diff) needs to reach a genuine "done, no changes needed"
    # outcome rather than being nudged indefinitely. Reset to 0 on any other
    # outcome (a real diff, a check failure) -- only a repeated no-diff pass
    # counts.
    no_diff_streak: int
    cost_so_far: float
    escalated: bool
    escalation_reason: str | None
    # Which coder seat this task runs on ("frontend" -> the Kimi alias, else
    # the general coder) and why (agent/frontend_route.py). Decided once at
    # creation; a resume must keep it, like category.
    route: str
    route_reason: str | None
    review_gate_result: dict | None

    # The sha of a commit that's been made but not yet confirmed shipped
    # (review READY + merged + deployed) or explicitly superseded.
    # verify_and_ship's own "is there something to ship" check is `git diff`
    # (uncommitted changes only) -- once a commit succeeds, `git diff` goes
    # empty, so without this field, any failure between a successful commit
    # and a successful deploy (a transient review-service network blip, an
    # operator-resumed escalation) would make the next verify_and_ship pass
    # see an empty diff and incorrectly treat a real, unreviewed commit as
    # "no changes needed" -- silently stranding it. Set the moment a fresh
    # commit succeeds; persists across a loop-back/escalation so a resume
    # re-polls review for the same sha; cleared only once that sha is
    # confirmed shipped. See verify_and_ship.py's comments at each transition.
    committed_sha: str | None

    # Where the work went, for a project that ships as a pull request. Set on
    # the shipped return so the dashboard can link it: a PR is not finished
    # work, it is work waiting for a person, and making them read the step log
    # to find the URL is how it gets forgotten.
    pull_request_url: str | None

    # Set by verify_and_ship when routing back to "work" with something new
    # to tell the deep agent (a check failure's real output, or the review
    # service's findings) -- consumed and cleared by work_node the next time
    # it runs. None on a fresh task or an orphan-restart resume (see
    # work.py's own docstring for the four cases this distinguishes).
    pending_feedback: str | None

    # The task's reflections on its failed attempts, newest last, at most
    # three (agent/reflection.py). Read by the next reflection so it does not
    # propose what already failed, and written into the task's episode.
    reflections: list[str]

    # Human-in-the-loop pre-execution approval -- see deep_agent.py's own
    # INTERRUPT_ON comment. Set by work_node when the inner deep-agent
    # thread hits a native LangGraph interrupt() (a tool call matched
    # INTERRUPT_ON's `when` predicate); a simplified, JSON-safe view of the
    # real HITLRequest (action name/args/description per pending tool call)
    # for the dashboard to render and an operator to decide on.
    # `pending_feedback` is set to a placeholder alongside this (not None)
    # purely so `_route_after_verify`'s existing "pending_feedback set ->
    # route to work" check fires on resume, reusing that mechanism rather
    # than adding a parallel one.
    pending_approval: dict | None

    # The operator's decision(s) on a pending_approval request, submitted via
    # POST /api/tasks/{id}/approve and picked up by work_node on its next
    # pass -- see work.py's own docstring for how this takes priority over
    # the normal goal/pending_feedback graph_input logic and gets turned into
    # a real `Command(resume={"decisions": [...]})` sent to the same inner
    # thread (resuming exactly where interrupt() paused it, not a fresh
    # message). Consumed and cleared by work_node the same pass it's read,
    # same discipline as pending_feedback.
    approval_decision: list[dict] | None

    # The deep agent's own write_todos state, as of the end of the last
    # "work" pass -- persisted here (not just streamed live) so a client
    # that (re)connects between updates still sees current progress, not a
    # blank plan. [{"content": str, "status": "pending"|"in_progress"|
    # "completed"}, ...] or None before the deep agent's first write_todos
    # call. verify_and_ship's plan gate holds the commit while items are
    # unfinished (INCOMPLETE_PLAN_LIMIT passes at most), but "completed" here
    # carries no authority over what ships: the check suite runs regardless.
    latest_todos: list | None

    # Append-only, via operator.add reducer -- a mutable "latest state"
    # field alone loses the ability to answer "did this actually happen"
    # once something moves past it.
    execution_log: Annotated[list[LogEntry], operator.add]

    # The agent_tools.py edit-repeat guard's state (a JSON-encoded
    # [path, old_string, new_string], or None) as of the end of the last
    # "work" pass. A guard scoped only to a single build_deep_agent call's
    # own closure would never see two identical failing-edit attempts made
    # across separate work<->verify_and_ship passes. Round-tripped here the
    # same way committed_sha/no_diff_streak already are, so the guard
    # survives a loop-back instead of resetting to empty every pass.
    last_failed_edit_signature: str | None

    # Consecutive passes where verify_and_ship saw an unchanged pending
    # commit that already has a non-READY verdict for that exact sha -- see
    # verify_and_ship.py's own comment on the pending_sha branch. Re-
    # triggering the review service's check suite on an unchanged commit is
    # pure waste (review is deterministic against unchanged input), and
    # without this counter that branch has no way to notice the model has
    # simply stopped acting on the feedback. Reset to 0 whenever a fresh
    # review verdict actually runs (real progress); escalates once past
    # STALE_PENDING_REVIEW_LIMIT.
    stale_pending_review_streak: int
    # The pending sha whose harness-failed verdict was already re-asked
    # about once (verify_and_ship): a second harness failure on the same
    # sha is nudged and escalated like any other stale verdict.
    harness_retry_sha: str | None

    # Which inner deep-agent thread this task is on -- 0 is the original
    # (un-suffixed thread_id, so existing tasks are unaffected); each
    # increment makes work.py derive a fresh inner thread. Bumped by
    # verify_and_ship as a last resort before escalating a stale pending
    # review: a summarization-compressed thread can degenerate into an
    # all-text-no-tool-calls history that every subsequent model call just
    # pattern-matches and continues -- unfixable by appending more messages,
    # fixable by starting clean (the real work stays safe in the pending
    # commit, not the conversation).
    inner_thread_generation: int

    # Consecutive verify_and_ship passes that sent the agent back to finish
    # its own todo plan instead of committing a mid-plan diff -- see
    # verify_and_ship.py's INCOMPLETE_PLAN_LIMIT. Reset to 0 the moment the
    # plan reads complete (or there's no plan to read), so this only ever
    # counts an unbroken run of "still not finished" passes, not a lifetime
    # total across a task that legitimately loops several times.
    incomplete_plan_streak: int

    # Consecutive no-diff passes whose final response was too short to read as
    # a real conclusion, and were nudged for it -- see verify_and_ship.py's
    # MAX_SHORT_CONCLUSION_NUDGES. Capped because that nudge resets
    # no_diff_streak: uncapped, a model that habitually ends terse could never
    # reach the "no changes needed" exit, and the task looped until
    # max_iterations (live, 2026-09-13).
    short_conclusion_streak: int
    # How many times this task's coordinator has delegated to the `verifier`
    # subagent, and whether the ship gate has already sent it back once for
    # not doing so (a benchmark task; verify_and_ship.py).
    verifier_runs: int
    verifier_nudged: bool
    # Diff patterns the gate has already sent back once (agent/nodes/diff_patterns.py).
    pattern_nudges: list[str]
    # Ids of the inner thread's messages the stream has already published as
    # log entries. A pass that continues the thread (review feedback, a
    # loop-back, a resume) starts from these so the first state snapshot does
    # not republish the whole earlier conversation (agent/nodes/work.py).
    streamed_message_ids: list[str]

    # Whether the operator who created this task has auto-approve enabled
    # (User.auto_approve_commands). Captured onto the task at creation
    # rather than read live, so changing the setting can't retroactively
    # loosen the gate on a task already in flight -- and so a task's own
    # record shows the gate it actually ran under. See deep_agent.py's
    # interrupt_on_for.
    auto_approve_commands: bool

    # Whether the operator who created this task wants a FINAL human look at
    # the diff after the review service approves it, before anything merges.
    # Captured at creation for the same reason as auto_approve_commands: a
    # setting change must not retroactively remove a gate from a task already
    # in flight. See verify_and_ship's merge-approval pause.
    require_merge_review: bool

    # Set while the task is parked waiting for that final look: {sha, repo,
    # review_summary, findings, at}. A resting state exactly like
    # pending_approval -- _route_after_verify returns END on it, and the
    # operator moves it via POST /api/tasks/{id}/merge-decision.
    pending_merge_approval: dict | None

    # The sha the operator explicitly approved for merge. verify_and_ship
    # merges only when this matches the outstanding committed_sha, so an
    # approval can never accidentally ship a LATER commit than the one the
    # operator actually looked at.
    merge_approved_sha: str | None

    # A hand edit from the final-look panel, waiting to be applied:
    # {base_sha, files: [{path, content}], note, by}. Carried in state rather
    # than written by the endpoint so the write happens inside the task's own
    # run, under its project lock -- the workspace is shared, and a request
    # handler writing into it could land in the middle of another task.
    # verify_and_ship applies it, commits, and runs the full gate again.
    operator_edits: dict | None

    # Which OTHER projects this task may read, for "use the one in X as a
    # template". Resolved at creation from the creator's own access -- always
    # a concrete list, never None-means-everything, so a task resumed from a
    # checkpoint written before this existed falls back to its own repo alone
    # rather than silently to all of them. Same capture-at-creation rule as
    # auto_approve_commands: a later access change neither widens nor narrows
    # a task already in flight. See agent/tools/reference_tools.py.
    reference_repos: list[str]


def initial_state(
    task_id: str,
    goal: str,
    repo: str,
    budget_usd: float,
    max_iterations: int = 40,
    auto_approve_commands: bool = False,
    require_merge_review: bool = True,
    route: str = "general",
    route_reason: str | None = None,
    reference_repos: list[str] | None = None,
) -> AgentState:
    return AgentState(
        task_id=task_id,
        goal=goal,
        repo=repo,
        budget_usd=budget_usd,
        route=route,
        route_reason=route_reason,
        max_iterations=max_iterations,
        iteration_count=0,
        no_diff_streak=0,
        cost_so_far=0.0,
        escalated=False,
        escalation_reason=None,
        review_gate_result=None,
        committed_sha=None,
        pending_feedback=None,
        reflections=[],
        pending_approval=None,
        approval_decision=None,
        latest_todos=None,
        execution_log=[],
        last_failed_edit_signature=None,
        stale_pending_review_streak=0,
        harness_retry_sha=None,
        inner_thread_generation=0,
        incomplete_plan_streak=0,
        short_conclusion_streak=0,
        verifier_runs=0,
        verifier_nudged=False,
        pattern_nudges=[],
        streamed_message_ids=[],
        auto_approve_commands=auto_approve_commands,
        require_merge_review=require_merge_review,
        pending_merge_approval=None,
        merge_approved_sha=None,
        operator_edits=None,
        reference_repos=list(reference_repos or []),
    )
