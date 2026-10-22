"""Planning chat -- a conversational, research/design-consulting agent
distinct from the build system's own coordinator (agent/deep_agent.py). No
outer plan/execute/verify graph here: this is a single deepagents graph
invoked directly per chat turn, its own conversation persisted via the same
Postgres checkpointer the build tasks use, under a distinct thread_id
namespace (f"planning:{session_id}") that never collides with a task's own
thread_id (a plain string).

Deliberately no write/edit/bash against the primary repo -- no
INTERRUPT_ON-style approval gate is needed the way investigator/test-writer
require for their bash access, since nothing here can mutate a real repo's
source. The one tool that leads to a mutation, create_project, does not
perform it: it records a proposal, and the project is created only behind an
admin-gated Confirm in the dashboard (server.py's new-project route, the same
code path as POST /api/projects/create). It DOES get real, persistent memory access though (see below): a
planning conversation that can't remember anything between sessions isn't
much of a planning assistant, and this system already has a proven, working
memory mechanism (build_deep_agent's own /memories//org-memory setup) --
reusing it here rather than inventing a separate planning-only memory store
keeps one project's "what we know" in one place, shared by both planning
sessions and real build tasks.

Same shared memory backend as build_deep_agent (build_memory_backend), keyed
to this session's own primary repo -- both `/memories/AGENTS.md` (real,
project-scoped, agent-writable) and `/org-memory/AGENTS.md` (real,
cross-project, read-only -- same deny-write permission build tasks get) are
embedded into the system prompt AND backed by a real StoreBackend, so
deepagents' own native write_file tool can actually persist an update to
project memory (unlike the plain StateBackend fallback a planning agent with
no backend= would get, where write_file goes nowhere real). This is the
"remember stuff across sessions" mechanism: whatever a planning conversation
writes to /memories/ is visible to every future planning session AND every
future build task for that project, and vice versa.

Read access is deliberately cross-project though (list_project_dir/
read_project_file, agent/tools/planning_tools.py) -- planning is explicitly
allowed to look at a DIFFERENT one of the operator's projects for comparison
or inspiration, unlike a build task's single-repo scope. Memory stays scoped
to the session's own primary repo either way; there's no cross-project
memory write from here.

Three seats, not one. agent-planning-chat (the EASY pin) handles the
default case -- web search, research, simple design/UX planning -- and
agent-planning-chat-hard is reserved for a turn that actually reads as
bug-fixing/debugging or a genuinely hard problem. classify_planning_difficulty
picks between them FRESH on every turn (a deterministic keyword floor first,
then routing through the SAME classify_task classifier this system already
built for task categorization -- its own "bug-fix" category is the HARD
signal, no second parallel classifier needed). The session then ratchets
upward: once a turn has needed HARD, later short follow-ups stay on HARD
(server.py's sticky-upward floor -- a continuation "also check X" classifies
EASY on its text alone and used to flip the model mid-plan). A third seat,
agent-planning-chat-frontend, sits ahead of that ladder for a session that
is frontend work (agent/frontend_route.py), so the plan is written by the
same model that will build it. Everything else about the agent -- tools,
memory backend, permissions, middleware -- is identical between the seats;
only `model=` changes. What each alias currently resolves to is a dashboard
pin, not a fact of this module.
"""

import logging
import time

from langchain.agents.middleware import ModelCallLimitMiddleware, SummarizationMiddleware, ToolCallLimitMiddleware
from langgraph.store.base import BaseStore

from deepagents import FilesystemPermission, create_deep_agent

from agent.classify import classify_task
from agent.message_text import content_text
from agent.config import Config, PROJECTS
from agent.frontend_route import PLANNING_ROLE
from agent.deep_agent import (
    ORG_MEMORY_PATH,
    PLANNING_SUMMARIZATION_KEEP,
    PLANNING_SUMMARIZATION_TRIGGER,
    SUMMARIZATION_TRIM_TOKENS,
    build_memory_backend,
    load_project_memory,
    llm_for_role,
    load_skills_manifest,
    load_skills_summary,
    org_namespace,
    read_memory_or_empty,
    route_local_path,
)
from agent.middleware.hidden_tools import HiddenToolsMiddleware
from agent.middleware.repeat_guard import RepeatCallGuardMiddleware
from agent.middleware.sanitize_tool_calls import SanitizeToolCallsMiddleware
from agent.middleware.budget_guard import BudgetMeterCallback, BudgetGuardMiddleware, BudgetTracker
from agent.middleware.pinned_brief import BriefFirstMiddleware, PinnedBriefMiddleware
from agent.model_config import resolve_alias
from agent.tools.agent_tools import make_agent_tools
from agent.tools.github_tools import make_github_inbox_tool, make_github_tools, token_source
from agent.tools.planning_tools import make_planning_tools
from deepagents.backends import StoreBackend

logger = logging.getLogger("tektonix")

# Deterministic floor, checked before the classifier call below -- mirrors
# model-router/config.yaml's own smart-router keyword_tier_rules philosophy
# (its exact REASONING-tier floor is "super hard", "extremely difficult",
# "think as hard as you can", "hardest part", "spare no effort" -- echoed
# here since classify_task's own bug-fix/feature/ui-styling/performance/
# investigation/other taxonomy has no "this is a hard problem" category of
# its own to catch that phrasing). An obvious case shouldn't depend on a
# classifier call (cost, latency, one more thing that can fail) when a plain
# keyword match already settles it. Kept short and specific, not a broad net
# -- a false HARD just costs a pricier turn, but a broad net would defeat
# the point of having a cheap default tier at all.
# Per-turn spend ceiling for a planning conversation -- generous enough for
# a genuinely hard investigation (the deepest useful sessions have landed
# $1-2 with caching), tight enough that a runaway marathon is interrupted
# while it is a surprise, not a bill.
# Operator-tunable at runtime (Settings -> Runtime limits). Read at use, not
# at import, so a change lands on the next turn without a restart.
from agent import runtime_settings as _rs

_HARD_KEYWORDS = (
    "super hard", "extremely difficult", "think as hard as you can",
    "hardest part", "spare no effort", "really hard", "difficult problem",
    # The operator's actual phrasing when they want the better model
    # (2026-08-26: "this is a complex request" routed EASY because nothing
    # here matched it). Deliberately multi-word or unambiguous forms only --
    # a bare "hard" would match "hard-coded", a bare "complex" is safe but
    # "complicated"/"tricky" cover the natural synonyms.
    "complex", "complicated", "tricky", "this is hard", "very hard",
)


async def classify_planning_difficulty(text: str, config: Config) -> str:
    """Returns "EASY" or "HARD" -- which of the EASY/HARD planning-chat
    models (see this module's docstring) should handle this turn. The
    frontend seat is chosen separately, ahead of this ladder.

    Routed through the SAME classifier this system already built for task
    categorization (agent/classify.py's classify_task, the pinned
    agent-classifier alias) rather than a second, parallel one:
    classify_task's own "bug-fix" category is exactly the HARD signal this
    needs, and reusing it means one classifier, one cost/latency profile,
    one thing to trust -- not two doing overlapping jobs. classify_task is
    already best-effort (falls back to "other" on any failure), so this
    inherits that same fail-toward-cheap behavior for free rather than
    needing its own separate fallback handling.
    """
    lowered = text.lower()
    if any(keyword in lowered for keyword in _HARD_KEYWORDS):
        return "HARD"
    classification = await classify_task(text, config)
    return "HARD" if classification.category == "bug-fix" else "EASY"

PLANNING_SYSTEM_PROMPT = """You are a planning and research assistant. Your job is a conversation, not a \
build: help the user think through a project -- researching options, discussing design/UI/UX direction, \
asking clarifying questions -- until there's a clear, complete plan. You never write or edit code yourself; \
a separate build system does that afterward, from the plan document you produce here.

You are consulting on: the "{repo}" project. The operator's other configured projects are: {other_repos} -- \
you can read any of them (not just "{repo}") via list_project_dir/read_project_file when it's useful to \
compare approaches or borrow a pattern.

What this project's own memory already holds (accumulated from past planning sessions and real build \
tasks -- treat it as established fact about this project, not something to re-derive):
--- /memories/AGENTS.md ({repo}) ---
{project_memory_content}

Cross-project notes (apply to every project, not just this one):
--- /org-memory/AGENTS.md ---
{org_memory_content}

This project also has REGISTERED SKILLS -- durable curated reference docs your built-in read_file tool CAN \
read (they live in your own file space, not the repo):
{skills_summary}

FIRST ACTION OF A NEW REQUEST: call save_brief. Until the session has a brief, save_brief (and \
describe_image) are the only tools you have -- restate the goal in the operator's terms, name the \
deliverable, say what is out of scope, and list what you expect to need. The brief is pinned into \
this prompt for the rest of the conversation, so compaction can never lose the request, and its tool \
result names the registered skills that match the request -- read THOSE before any repo file, then \
search_project for each item in your brief's "expected to need" list before reading anything. When a \
later message changes what the operator wants, call save_brief again before doing anything else. A \
one-line follow-up ("yes", "go ahead") needs no new brief.

START every code investigation with the CODEBASE MAP: read /skills/codebase-map/SKILL.md (with built-in \
read_file) BEFORE walking the repo with list_project_dir. It is the maintained map of "{repo}" -- where \
things live, entry points, conventions -- rebuilt on a schedule from the real tree. One read replaces a \
dozen exploratory directory listings and wrong-path guesses. Trust it for orientation; verify specifics \
with read_project_file (the map can lag a very recent rename).

Tools available to you:
- read_project_file(repo, path) / list_project_dir(repo, path): read/list files in any configured \
project's real repo (read-only). BOTH arguments every time -- `repo` (default to "{repo}" unless you're \
deliberately looking at another one) AND `path`, repo-relative. read_project_file's `path` is required and \
names one file ("src/App.tsx"); calling it with only `repo` is a wasted round trip that just comes back \
"path: Field required". list_project_dir's `path` defaults to the repo root, so pass "." when you mean it. \
Paths run through the repo's real working tree, so nothing under .git/ is readable -- ask for a file, not \
for git metadata. A big file comes back TRUNCATED: asking for it again returns the identical text, so page \
through it with read_project_file(repo, path, offset=<1-based line>, limit=<lines>) in big windows \
(600-800 lines), never by re-requesting the whole file and never in 50-line slices.
- search_project(repo, pattern, path=".", glob=None, fixed=False): ripgrep over the real repo -- regex by \
default, `fixed=True` for a literal; `path` narrows to a directory, `glob` to a file pattern ("*.tsx"). \
Returns file:line hits. SEARCH FIRST: for each thing the brief says you need, search for it, then read only \
the window a hit points to (read_project_file with offset/limit). A search with no hits tells you how many \
files it scanned and what to change -- never rerun it unchanged, and never open every file in a directory \
to find something a search would find in one call.
- find_files(repo, glob, path="."): the repo files matching a glob ("**/*.css"), .gitignore-aware -- the \
answer to "where are all the X files", cheaper than listing directories one by one.
- github_inbox_items(repo): the GitHub inbox -- every code scanning (CodeQL) alert grouped per rule with its \
  file:line locations, Dependabot PR, security alert (with the patched version), review and failing check the \
  poller found. When the request mentions the inbox or "the alerts", read this FIRST and put every item in the \
  brief; it is the exact list, and for code scanning items the locations in the summary ARE the findings -- \
  read those files, not the pull requests.
- github_pull_request(repo, number, part="all") / github_pull_requests(repo, state="open"): read a GitHub pull \
request (description, checks, review comments with file:line, diff) or list them. When the request names a \
PR, read it FIRST -- the review comments are the findings a fix has to address -- and put each finding in \
the brief and the plan. These tools exist only when the deployment has a GitHub token; if they are absent, \
say so rather than guessing at a PR's contents.
- write_file: your own write access is limited to /memories/AGENTS.md (this project's memory) -- use it to \
record a durable fact worth remembering for next time (a decision made, a constraint discovered, a \
direction the user confirmed). Don't use it for anything else; everything else you "write" doesn't \
actually go anywhere real. You cannot write to /org-memory/ at all.
- describe_image: look at an image the user attached.
- web_search: search the web for research, documentation, or design inspiration.
- browse_page: load a real webpage and read it -- pass screenshot=True to also get a description of what \
it actually looks like (layout, colors, typography), which is exactly what you need when the user wants to \
reference or compare a design/competitor site.
- show_images(images): PUT IMAGES IN FRONT OF THE OPERATOR, in this chat -- each an "svg" you wrote \
or a "path" to an image in the repo, with a "caption" (and a "background" to show it on dark or light). \
It is the only tool that shows them anything: logo_render, describe_image, browse_page and preview_app \
tell YOU what something looks like and show them nothing. When they ask to see something, or a choice \
is theirs to make by looking, show it -- and repeat the image lines it returns in your reply.
- logo_render(svg, question="", background=""): render an SVG and get told what it actually shows, on \
white unless you name a background. You write SVG blind otherwise -- this is how you check your own \
work before you show it. It is a check for you, not an approval: only the operator approves a design. \
Ask it factual questions (is anything clipped, overlapping, off-centre, unreadable) -- never questions \
of taste like "does it look like a real gem" or "is it polished"; those have no answer it can give you, \
and chasing one is a loop. After a few renders it will refuse until you have shown the operator.
- logo_text_to_path(svg), logo_optimize_svg(svg), logo_trace_image(image_path): outline the type in a \
wordmark so it does not depend on the viewer's fonts, clean up the markup, and vectorise an existing \
PNG/JPG logo. Nothing here DESIGNS a logo -- you write the SVG; these handle everything after that.
- DRAFTS: anything large -- a trace always is -- comes back as a draft id (like 3f9a1c2b7d4e), not \
markup. Pass that id as `draft` to logo_render, show_images, logo_compose and the rest. NEVER retype a \
large SVG: a traced logo is tens of kilobytes, and writing it out takes minutes and times out.
- logo_compose(draft, defs, group_attributes, before, after): add lighting, glow, a sheen or a backdrop \
to a draft WITHOUT retyping it -- put filters/gradients in `defs`, apply them with `group_attributes` \
(e.g. filter="url(#glow)"), draw highlights in `after`. It returns a new draft; the original stays, so \
show them side by side.
- Logo work happens HERE, in planning, and in this order. (1) If the project already has a logo, it is \
the starting point: "make the logo a proper SVG" means THAT logo, vectorised and refined -- \
logo_trace_image it, clean the result, keep its look -- not a new design, unless they ask for one. \
(2) Show the options with show_images -- the current logo beside your versions, on the site's real \
backgrounds -- and ask which they want. (3) Iterate on what they say, showing each round. (4) Only \
once they have chosen, put the chosen DRAFT ID in the plan (and the markup too if it is small), with \
where the files go. Exporting the brand kit (PNGs, favicons, social images, BRAND.md) is the build \
task's job, from that draft.
- preview_app(repo, command, port, path="/", question=""): run one of the operator's projects in a sandbox \
and LOOK at it in a real browser. Use it when the question is about how something LOOKS -- a restyle, a \
layout, "what does this page do now", or comparing one project's UI against another's. The CSS tells you \
what the rules say; this tells you what the page is. The command must listen on 0.0.0.0, not localhost. \
For a site that is ALREADY running somewhere, use browse_page -- far cheaper than starting a copy.
You have NO way to delegate: there are no subagents here, and investigation you want done is \
investigation you do yourself, with the tools above. Read the files, page through the big ones, and reason \
about what you found -- that is the job, not a detour from it. If a question is too big to answer fully in \
one turn, say what you found, say what is still open, and save the plan you can justify now; a partial \
plan the operator can act on beats a perfect one that never gets written.
- save_brief: pin the brief for the current request (see FIRST ACTION above).
- create_project(name, description="", github=False): when the request is a NEW application rather than a \
change to "{repo}" or another configured project, first ask the operator for a project name and whether a \
private GitHub repo should be created, restate both, and only after they confirm call create_project once. \
It creates nothing itself -- the operator gets a Confirm card and the project is created when they confirm, \
after which this conversation continues on the new project. Do not call it again unless the operator \
changes the name; keep planning as if the project exists.
- THE DRAFT GATE: after a set number of repo file reads without a saved plan (Settings: "Planning reads \
before a draft"), read_project_file closes until you call save_plan. Save early -- a draft with open \
questions beats reading on -- then KEEP GOING in the same turn: reads reopen after the save, so investigate \
the draft's open questions and save the finished plan. A gate-forced save is a checkpoint, never the end of \
the turn. Paged reads return at least 500 lines; page in 500+ line steps, never 100.
- save_plan: save the current draft plan document (Markdown). Call this once the plan is genuinely ready, \
and again any time it meaningfully changes -- never wait to be asked. The user can hit "Build Now" the \
moment a plan exists, so don't leave a stale or half-finished draft saved if the conversation has moved on. \
CRITICAL: your chat reply is NOT the plan. A plan, report, or spec that exists only as message text cannot \
be built from and is lost the moment the conversation moves on -- if your reply contains the plan document, \
you MUST call save_plan with that same content in the SAME turn, then summarize briefly in chat. Ending a \
turn with a plan in prose and no save_plan call is a failure, not a style choice.

IMPORTANT -- your built-in `ls`/`read_file`/`write_file`/`edit_file` tools see YOUR OWN file space \
(/memories/, /org-memory/, /skills/ -- including the codebase map above), NEVER the actual repo, no matter \
what path you give them. A "file not found" from THOSE tools on a repo-looking path does not mean the file \
doesn't exist in the real project -- it means you used the wrong tool: switch to read_project_file/\
list_project_dir, the ONLY tools (plus describe_image, for attached images) that ever reach the real repo. \
The repo search is search_project/find_files -- NEVER the built-in grep/glob, which only see your own file \
space. Read the codebase map for orientation, search for the specific thing, read the window the hit points \
to; repeating a failed lookup harder is never the fix. If search_project, read_project_file and \
list_project_dir all come up empty, that's when it's actually real -- say so plainly instead of guessing, \
and don't let a research dead end turn into losing track of what the user actually asked for.

Whenever you mention a specific color (a palette, an accent, anything from a screenshot's visual \
description), always give a real value -- a hex code (#3fb950) or rgb(...) -- alongside any descriptive \
name, never a name or vague description alone ("a nice teal" is not enough, "#008080 (teal)" is). The \
chat interface renders an actual color swatch next to any hex/rgb value you write, so this is how the \
user actually sees the color, not just reads about it -- a color with no real value attached is invisible \
to them.

The plan document you save must be self-contained -- a build agent with no memory of this conversation \
has to be able to act on it directly. Include: the concrete goal, key requirements/decisions made here, \
and any design/UX direction (with specifics: colors, layout choices, references found) -- not just a \
summary of the chat. Prefer asking a clarifying question over guessing at an ambiguous requirement, but \
don't stall on genuinely minor details a builder could reasonably decide on its own."""


def planning_thread_config(session_id: str, repo: str) -> dict:
    return {
        "configurable": {"thread_id": f"planning:{session_id}"},
        "metadata": {"planning_session_id": session_id, "repo": repo},
        "tags": [repo, "planning"],
    }


async def build_planning_agent(
    config: Config,
    repo: str,
    checkpointer,
    store: BaseStore,
    starting_cost: float = 0.0,
    difficulty: str = "EASY",
    existing_plan: str | None = None,
    allowed_repos: list[str] | None = None,
    existing_brief: dict | None = None,
    route: str = "general",
    is_admin: bool = False,
    actor: str | None = None,
    session_id: str | None = None,
):
    """Returns (agent, plan_ref, tracker).

    `is_admin`/`actor` reach make_planning_tools' create_project gate; they
    are threaded explicitly because `allowed_repos` is None for admins and
    for legacy unscoped accounts alike, so role cannot be read off it.

    `difficulty` ("EASY" or "HARD", from classify_planning_difficulty)
    picks which of the EASY/HARD planning-chat models actually answers this
    turn -- see this module's docstring. `route="frontend"` sits ahead of
    that ladder and pins agent-planning-chat-frontend instead. Defaults to
    "EASY" / "general" for callers that build the agent without driving a
    real turn (e.g. get_planning_session's read-only state fetch, which never
    invokes the model at all, so which one gets wired in there is inert).
    Every other argument to create_deep_agent below -- tools, memory backend,
    permissions, middleware -- is identical regardless of `difficulty` or
    `route`; only `model=` changes, so the harder (or frontend) model never
    gets a reduced tool/memory set.

    `plan_ref` is a mutable {"markdown": str | None} dict that
    make_planning_tools' save_plan tool writes into; the caller reads it
    back after each turn to persist the current draft into the session's
    Store meta (see run_planning_turn).

    `tracker` is a BudgetTracker whose ceiling is the session spend so far
    plus one turn's allowance (Settings -> Runtime limits,
    `planning_turn_budget_usd`). Planning used to run with math.inf -- the
    one agent with no budget was the one that once spent $7 on a single
    157-call turn. The cost is the router's own billed figure where it has
    landed (BudgetGuardMiddleware / router_ledger.py).
    """
    repo_root = PROJECTS[repo]["sandbox"]
    project_tools, _ = make_agent_tools(repo_root)
    tool_by_name = {t.name: t for t in project_tools}
    skills_manifest = await load_skills_manifest(repo, store)
    github_tools = make_github_tools(token_source(config), allowed_repos)
    # The inbox reads the store, not GitHub, so it is there with or without a token.
    github_tools = [*github_tools, make_github_inbox_tool(store, allowed_repos)]
    planning_tools, plan_ref = make_planning_tools(
        existing_plan, allowed_repos, existing_brief=existing_brief, skills_manifest=skills_manifest,
        is_admin=is_admin, actor=actor, session_id=session_id,
    )

    # Designing a mark, without exporting one (agent/tools/logo_tools.py).
    # `can_export=False` keeps this module's first promise: the planning chat
    # never mutates the target repo. A brand kit is two dozen binary files
    # with nowhere to go in a conversation -- the plan carries the SVG, and
    # the build task that acts on it exports the kit. Tracing reads from this
    # session's own project, which is where an existing logo lives.
    from agent.tools.logo_tools import make_logo_tools, new_show_state  # noqa: PLC0415

    # logo_render stops after a few renders the operator has not seen, until
    # show_images shows them (agent/tools/logo_tools.py RENDERS_BEFORE_SHOWING).
    show_state = new_show_state()
    logo_tools = make_logo_tools(
        lambda: (PROJECTS.get(repo) or {}).get("sandbox") or "", can_export=False, show_state=show_state,
        repo=repo,
    )
    # And showing the operator what it made (agent/tools/show_tools.py): the
    # logo tools only let the planner look, and a design is chosen by looking.
    from agent.tools.show_tools import make_show_images_tool  # noqa: PLC0415
    show_tools = [make_show_images_tool(repo, lambda: (PROJECTS.get(repo) or {}).get("sandbox") or "",
                                        show_state=show_state)]

    # What past tasks ran into (agent/tools/history_tools.py). The planner is
    # the seat this is worth most to: "we tried that in June and it
    # escalated" changes a plan, while the same fact discovered mid-build
    # only changes the damage. The gate is `allowed_repos`, where None means
    # an admin and therefore every project -- not the build task's rule, so
    # it is passed with none_means_all rather than silently widened.
    from agent.tools.history_tools import guidance as history_guidance  # noqa: PLC0415
    from agent.tools.history_tools import make_history_tools  # noqa: PLC0415

    history_tools = make_history_tools(
        repo, allowed_repos, store, none_means_all=True,
        task_id=f"planning:{session_id}" if session_id else None,
    )
    history_note = history_guidance(repo, allowed_repos, none_means_all=True)

    # The same loader the build coordinator uses, not a second copy of it:
    # the planner is where a durable fact gets written down and the
    # coordinator is where it has to be obeyed, so the two seats seeing
    # different memory is a plan written against rules the build cannot read.
    # The planner's own thread id, so a memory_offered event can be joined to
    # the conversation that caused it. Without it every planning event lands
    # with task_id=None and the retrieval log cannot answer the question it
    # exists for -- which sections get offered and never read, and in which
    # seat. The prefix is planning_thread_config's, so the two agree.
    project_memory = await load_project_memory(
        repo, store, task_id=f"planning:{session_id}" if session_id else None,
    )
    project_memory_content = project_memory.content
    org_memory_backend = StoreBackend(namespace=org_namespace, store=store)
    org_memory_content = await read_memory_or_empty(org_memory_backend, route_local_path("/org-memory/", ORG_MEMORY_PATH))

    from agent.tools.memory_tools import make_memory_tools  # noqa: PLC0415

    memory_tools = make_memory_tools(repo, store, project_memory.entries)

    # audit H-2: render only repos this session's user may actually read, not
    # every configured project -- the prompt used to advertise "you can read any
    # of them" across all of PROJECTS regardless of the caller's allow-list.
    _visible = [r for r in PROJECTS if r != repo and (allowed_repos is None or r in allowed_repos)]
    other_repos = ", ".join(_visible) or "(none)"

    # A per-turn dollar ceiling, enforced per model call like a task's
    # budget (2026-08-28: with none, one turn burned $7 over 157 calls and
    # 10.6M input tokens). Scoped per TURN: the ceiling rides on top of
    # whatever the session has already spent, so a long session isn't
    # strangled by its own history -- each turn gets the same allowance.
    # Healthy turns run $0.10-$1.50; the runaways are the
    # investigate-everything marathons this exists to interrupt. On breach,
    # BudgetGuardMiddleware raises out of the turn; the teardown paths bank
    # the draft plan, the spend, and fire the planning_error Telegram alert,
    # so nothing is lost -- the operator decides whether another turn's
    # allowance is worth it with a one-line "continue".
    tracker = BudgetTracker(
        budget_usd=starting_cost + _rs.value("planning_turn_budget_usd"),
        starting_cost=starting_cost,
    )
    skills_summary = await load_skills_summary(repo, store)

    # Frontend sessions get their own tier (agent/frontend_route.py) ahead of
    # the EASY/HARD ladder: a frontend plan written by the model that will
    # build it is the point.
    if route == "frontend":
        planning_model_role = PLANNING_ROLE["frontend"]
    else:
        planning_model_role = "agent-planning-chat-hard" if difficulty == "HARD" else "agent-planning-chat"
    agent = create_deep_agent(
        model=llm_for_role(config, planning_model_role, reasoning_effort="high", timeout=_rs.as_int("planning_model_call_timeout_s")),
        tools=[tool_by_name["describe_image"], *planning_tools, *github_tools, *logo_tools, *show_tools,
               *history_tools, *memory_tools],
        system_prompt=PLANNING_SYSTEM_PROMPT.format(
            repo=repo,
            other_repos=other_repos,
            project_memory_content=project_memory_content,
            org_memory_content=org_memory_content,
            skills_summary=skills_summary,
        ) + history_note,
        middleware=[
            # create_deep_agent adds a general-purpose subagent (and therefore
            # a `task` tool) unconditionally -- `subagents=None` does NOT mean
            # none, see agent/middleware/hidden_tools.py. This module is built
            # as one flat conversation and its system prompt never mentions
            # `task`, but the model found it in the schema anyway and spent
            # whole turns inside nested agent loops: 115 model calls and 4.7M
            # input tokens against the 1800s ceiling on 2026-08-27, timing out
            # with only ~22 tool calls in its own transcript. That subagent
            # also carried no BudgetGuardMiddleware (it has to be attached per
            # spec by hand), so the same turn reported $0.44 against $3.98 of
            # real router spend. Withholding the tool closes both.
            # grep/glob joined the hidden list 2026-08-27: they search the
            # SCRATCH space, not the repo, and despite the prompt's warning a
            # live session burned 8 consecutive identical grep('runBacktest',
            # 'src/core/backtester...') calls -- "No matches found" every time
            # -- against a repo path the tool cannot see. The repo search is
            # search_project/find_files (real-tree, .gitignore-aware); the
            # codebase map is orientation, not a substitute for search.
            # execute/delete go too: execute has no sandbox behind it in this
            # agent, delete has nothing it should ever delete.
            SanitizeToolCallsMiddleware(),  # a malformed tool call in history never reaches a provider
            HiddenToolsMiddleware("task", "grep", "glob", "execute", "delete"),
            RepeatCallGuardMiddleware(),  # the same call with the same result is not run a third time
            # Brief first, then pinned: the request is written down before any
            # file is read, and stays in the system message through every
            # compaction (agent/middleware/pinned_brief.py).
            BriefFirstMiddleware(plan_ref),
            PinnedBriefMiddleware(plan_ref),
            BudgetGuardMiddleware(tracker),
            SummarizationMiddleware(
                # Metered via callback -- SummarizationMiddleware ainvoke()s
                # this model directly, outside any agent middleware (see
                # BudgetMeterCallback in budget_guard.py).
                model=llm_for_role(config, "agent-summarizer", callbacks=[BudgetMeterCallback(tracker)]),
                trigger=PLANNING_SUMMARIZATION_TRIGGER,
                keep=PLANNING_SUMMARIZATION_KEEP,
                # Not optional -- the library default (4000) is a REAL, live
                # bug here, confirmed 2026-08-23: a planning turn opens with
                # one long HumanMessage (the operator's own detailed
                # problem/ask) followed by a long tool-heavy stretch
                # (repo reads/greps), the exact shape deep_agent.py's own
                # SUMMARIZATION_TRIM_TOKENS comment already documents as
                # producing an empty trim once that HumanMessage sits
                # further back than any finite budget reaches. The
                # summarizer call then fails outright and the middleware
                # silently substitutes "Previous conversation was too long
                # to summarize" for the ENTIRE history -- destroying the
                # original ask with no error surfaced anywhere, which is
                # exactly why a planning session went silent and asked the
                # user to restate a problem it had just been given in
                # detail. SUMMARIZATION_TRIGGER already bounds the untrimmed
                # batch at fire time, so trimming is redundant here too --
                # disable it the same way the coordinator already does.
                trim_tokens_to_summarize=SUMMARIZATION_TRIM_TOKENS,
            ),
            ModelCallLimitMiddleware(run_limit=_rs.as_int("model_call_run_limit"), exit_behavior="error"),
            ToolCallLimitMiddleware(run_limit=_rs.as_int("tool_call_run_limit"), exit_behavior="error"),
        ],
        # Real memory backend (not the plain StateBackend fallback) -- gives
        # deepagents' own native write_file/edit_file tools somewhere real to
        # write, scoped to this project's memory. Everything else (any other
        # path a model tries) still falls back to StateBackend's ephemeral
        # scratch space, same as before -- this only adds the two memory
        # routes, it doesn't grant real filesystem access anywhere else.
        backend=build_memory_backend(repo, store),
        permissions=[
            FilesystemPermission(operations=["write"], paths=["/org-memory/*"], mode="deny"),
        ],
        checkpointer=checkpointer,
        store=store,
    )
    return agent, plan_ref, tracker


def _translate_message(msg) -> dict | None:
    """Mirrors agent/nodes/work.py's _translate_message, simplified: no
    node_label (planning chat has no subagents to distinguish)."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    if isinstance(msg, HumanMessage):
        # The operator's own turns. Previously dropped entirely (this
        # translator only knew AIMessage/ToolMessage), so a refreshed page
        # showed the agent answering nobody. Summarization's synthetic
        # "Here is a summary..." HumanMessage is internal plumbing, not an
        # operator turn -- skipped rather than rendered as a giant fake
        # user bubble.
        text = content_text(msg.content).strip()
        if not text or text.startswith("Here is a summary of the conversation"):
            return None
        return {
            "kind": "user",
            "summary": text[:200],
            "detail": text[:4000],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    if isinstance(msg, AIMessage):
        if msg.tool_calls:
            calls = ", ".join(f"{tc['name']}({str(tc.get('args'))[:80]})" for tc in msg.tool_calls)
            summary = f"calling: {calls}"
        else:
            text = content_text(msg.content).strip()
            if not text:
                return None
            summary = text[:200]
        response_metadata = getattr(msg, "response_metadata", None) or {}
        alias = response_metadata.get("model_name") or response_metadata.get("model")
        # content_text, never str(content): Kimi block-list content rendered
        # as raw dict repr here, which the operator read as failed/no-output
        # calls (see agent/message_text.py). A tool-call-only turn falls back
        # to the call summary instead of showing block debris.
        return {
            "kind": "agent",
            "summary": summary,
            "detail": (content_text(msg.content).strip() or summary)[:4000],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "model": resolve_alias(alias),
        }
    if isinstance(msg, ToolMessage):
        tool_text = content_text(msg.content)
        return {
            "kind": "tool-result",
            "summary": f"tool result: {tool_text[:200]}",
            "detail": tool_text[:4000],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
    return None


def _message_key(msg) -> str:
    """A stable identity for a message across stream ticks. LangChain assigns
    ids; the fallback keeps a message without one from being re-published
    forever (identity of the object, which the graph keeps stable per tick)."""
    mid = getattr(msg, "id", None)
    return str(mid) if mid else f"obj:{id(msg)}"


# A plan describes work to DO. These are the ways a planning turn says the
# opposite -- that it investigated and found nothing to build -- and they are
# checked because the safety net below cannot otherwise tell the two apart.
#
# Live 2026-09-22: a turn on a real project ended "So there's no plan to save
# -- any 'fix' would be a no-op", in a 2,719-character answer with three
# markdown headings. That cleared the net's length-and-structure bar, landed
# in the plan panel, and armed the Build Now button. The operator pressed it
# and a task started with a $10 budget whose GOAL was an essay explaining that
# nothing needed doing.
#
# Phrase matching is not a technique to be proud of, and it is the right one
# here: this is not inferring intent from prose, it is reading the model's own
# explicit statement about whether a plan exists. A turn that says there is no
# plan to save is not a turn whose text should be saved as the plan.
_NO_WORK_MARKERS = (
    "no plan to save",
    "nothing to build",
    "nothing to do",
    "no changes are needed",
    "no changes needed",
    "already done",
    "already exists, exactly as",
    "would be a no-op",
    "there is no work",
    "there's no work",
)

# Only the tail is searched. A plan may legitimately say "X already exists" in
# passing while still proposing work; a CONCLUSION that nothing is needed lands
# at the end, which is also where a reader looks for it.
_CONCLUSION_TAIL_CHARS = 700


def reply_disclaims_a_plan(text: str) -> bool:
    """Does this reply say, in its own words, that there is nothing to plan?"""
    tail = (text or "")[-_CONCLUSION_TAIL_CHARS:].lower()
    return any(marker in tail for marker in _NO_WORK_MARKERS)


def looks_like_a_plan(text: str) -> bool:
    """Is this final reply a plan the operator could press Build Now on?

    Three conditions, and the third is the one added after the 2026-09-22
    incident. Length and heading structure say it is a DOCUMENT; they cannot
    say it is a document proposing work, and a research answer with headings
    satisfies both.
    """
    text = (text or "").strip()
    if len(text) < 1500 or text.count("\n#") < 3:
        return False
    return not reply_disclaims_a_plan(text)


async def run_planning_turn(agent, plan_ref: dict, thread_config: dict, text: str | None, publish, tracker=None) -> str | None:
    """Runs one turn (text=None resumes/continues the thread with no new
    input, e.g. after a process restart) and publishes each new message via
    `publish(event)` as it happens, mirroring work.py's live-streaming
    approach at a smaller scale (one flat conversation, no subagents/todos to
    multiplex). Returns the current plan_ref["markdown"] after the turn
    (None if never saved).
    """
    from langchain_core.messages import HumanMessage

    graph_input = {"messages": [HumanMessage(content=text)]} if text else None
    # Each turn is its own astream_events() call (unlike work.py's single
    # continuous run for a whole task), and `run.values` always reports the
    # FULL accumulated message list, not a delta -- without seeding
    # seen_count from what's already in the checkpoint, every turn would
    # re-publish the entire conversation history from message 0, not just
    # what's new this turn.
    existing = await agent.aget_state(thread_config)
    # Track WHICH messages have been published, not how many. A count broke
    # the moment SummarizationMiddleware compacted the thread: the list got
    # shorter than the count already sent, `len(messages) > seen_count` went
    # false, and nothing was published again until the thread had regrown
    # past the old count -- 13 silent minutes on a turn that was making a
    # call every 10 seconds (2026-09-09), and since the stall watchdog beats
    # on published events, a working turn was about to be killed as stalled.
    seen_ids: set[str] = {
        _message_key(m) for m in (existing.values.get("messages", []) if existing and existing.values else [])
    }
    _last_cost_emitted = tracker.total_cost if tracker is not None else 0.0
    async with await agent.astream_events(graph_input, config=thread_config, version="v3") as run:
        async for values in run.values:
            if not isinstance(values, dict):
                continue
            # Live cost, same contract as a build task's stream (operator
            # report 2026-08-28: "doing a plan live now and it still shows 0
            # cost" -- planning only reported cost at turn_complete). Any
            # real move emits; the superstep cadence bounds volume.
            if tracker is not None and tracker.total_cost - _last_cost_emitted >= 0.0001:
                _last_cost_emitted = tracker.total_cost
                publish({"type": "cost", "cost_usd": tracker.total_cost})
            messages = values.get("messages") or []
            fresh = [m for m in messages if _message_key(m) not in seen_ids]
            published_any = False
            for msg in fresh:
                seen_ids.add(_message_key(msg))
                translated = _translate_message(msg)
                # kind=="user" entries are NOT published live: the client
                # renders its own copy the moment the operator hits send,
                # and the turn handler seeds the live-log buffer with it
                # -- streaming the translated HumanMessage too painted a
                # SECOND user bubble (with the attachments note appended)
                # right under the first (reported live 2026-08-28). The
                # translation exists for checkpoint HYDRATION, where no
                # client-side copy exists.
                if translated and translated.get("kind") != "user":
                    publish(translated)
                    published_any = True
            if not published_any:
                # Still a heartbeat: the graph advanced but nothing was worth
                # showing (a superstep with no new message, or only the
                # compaction summary, which is never displayed). The stall
                # watchdog must see progress; the client's "ping" type is
                # already a no-op for it.
                publish({"type": "ping"})
    if plan_ref.get("markdown") is None:
        # Safety net for a model that writes the plan as CHAT TEXT and ends
        # the turn without ever calling save_plan -- which a real session did
        # on 2026-08-27: a $6.12 turn compiled a full 8k-char audit report
        # into its final message, called no tool, and left the plan panel
        # empty (the operator read the older plan visible on ANOTHER session
        # as a cross-save). The prompt now forbids it outright, but a prompt
        # is a request, not a guarantee. Heuristic is deliberately narrow --
        # a long final message with real markdown-heading structure -- so an
        # ordinary conversational answer never lands in the plan panel; and
        # this only fills an ABSENT plan (plan_ref None means no save_plan
        # this turn; the caller's preserve-not-clobber rule still governs
        # what gets stored).
        try:
            state = await agent.aget_state(thread_config)
            messages = (state.values or {}).get("messages", []) if state else []
            from langchain_core.messages import AIMessage as _AIMessage
            last = messages[-1] if messages else None
            if isinstance(last, _AIMessage) and not getattr(last, "tool_calls", None):
                text = content_text(last.content).strip()
                if looks_like_a_plan(text):
                    logger.warning("planning turn ended with an unsaved plan-shaped reply "
                                   "(%d chars) -- adopting it as the draft plan", len(text))
                    plan_ref["markdown"] = text
                elif reply_disclaims_a_plan(text):
                    # Said out loud rather than passed over: the panel staying
                    # empty here is the CORRECT outcome, and without a line in
                    # the log it reads exactly like the bug this net exists to
                    # fix -- a plan that went missing.
                    logger.info("planning turn concluded there is nothing to build; leaving the "
                                "plan panel empty so Build Now stays disabled")
        except Exception:  # noqa: BLE001 -- the net must never break a healthy turn
            logger.exception("unsaved-plan recovery failed; returning without a plan")
    return plan_ref.get("markdown")
