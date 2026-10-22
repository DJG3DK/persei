# Todo

Work that is agreed but not built. One entry per item: what breaks, what
already exists, and the shape of the fix. `docs/roadmap-packaging.md` holds the
packaging/Windows/CLI milestones; this is everything else.

---

## Keep a task moving when live main moves under it

**Status:** done, 2026-09-19.

The merge stays `--ff-only`, which is what guarantees the thing that merges is
the thing that was reviewed. The branch moves instead:

* `rebase_onto_base` (`agent/tools/git.py`) rebases the task branch onto the
  live tip, aborting cleanly on conflict so the branch is never left half
  rebased. It reports whether the branch's own patch survived the move
  unchanged.
* `verify_and_ship` rebases after committing, so the review measures the
  branch against the base it will actually merge into.
* The merge endpoint answers `diverged` instead of throwing when live has
  moved since the review, and the agent rebases and goes round once more
  rather than stopping.
* A conflict goes back to the agent as feedback, with the file list, the same
  shape as a review finding.

Covered by `tests/test_rebase_on_moved_base.py`, and simulated against the
real merge endpoint: a reviewed branch, a concurrent push, a `diverged`
refusal, a rebase, and a fast-forward that lands both changes.

---

## The review dashboard is unreachable in the bundle

**Status:** done, 2026-09-20.

### What breaks

The gate runs in compose and the agent drives it, but a person cannot. Ports
4100 and 4101 are unpublished, and the console's review calls go to
`/_review/…`, a path nginx serves on a host install and nothing serves here.
So *Check now*, the diff view and the manual merge button are host-install
only, while the thing they control runs in the bundle.

### Shape of the fix

Proxy `/_review/*` from the agent's own server to `REVIEW_SERVICE_HOST`,
gated on the same admin auth the rest of the API uses, injecting the control
secret the way nginx does. That keeps the ports unpublished — the console is
already the authenticated front door — and it works on a host install too,
which would make the nginx snippet in INSTALL.md §7 optional rather than
required.

### What was done

`/_review/{path}` on the agent's own server forwards to the review service,
admitting only an admin over the session the console already required. The
secret is SET there, never forwarded: a caller who sends their own
`X-Review-Secret` cannot influence what the review service sees, because this
endpoint's authority comes from the session rather than from a header.

The nginx location stays, as an optimisation rather than a prerequisite. It
keeps the traffic out of the Python process where somebody already has nginx;
INSTALL.md §7 says so now.

Covered by `tests/test_review_proxy.py`, and verified against the running
service: a read route returns real data, and a mutating one returns the gate's
own `not_reviewed` rather than `invalid or missing X-Review-Secret`, which is
what proves the injection reached it.

---

## Split `agent/server.py` along the seams that already exist

**Status:** started, 2026-09-21. Twelve seams out (`server.py` was 5,659 lines
before them and is 2,069 on 2026-09-27):
`push`, `analytics`, `env_config`, `settings` (with the audit log it is
interleaved with), `model_config`, `github` (the inbox and approve links,
2026-09-23, once task creation had moved) `tasks` (2026-09-23, once the
live run state had its own module, `agent/task_runtime.py`), `planning`
(2026-09-23, reaching the turn machinery through call-time wrappers on
`app.state`), `auth` (2026-09-27, the `/api/auth/*` routes with the
request models and cookie/scope helpers only they use) and `projects`
(2026-09-27, onboarding, creation, removal and the deploy keys; the shared
helpers take the app explicitly, `_spawn_background` joined `live_state`,
and `provisioning.py` stayed put), `uploads` (2026-09-27; the body-size
middleware imports its ceiling back from the router) and the review proxy
(2026-09-27, `agent/routers/review_proxy.py`, on its own as required). Do
not flatten the rest in one pass.

**The remaining seams need one more move first, and it is a specific one.**
The shared state is done: `agent/live_state.py` holds the five live
registries (`running_tasks`, `task_recorders`, the two subscriber maps, the
background-task set) as MUTATED dicts, so server.py and any router hold the
same objects; `github_poll_once` and `github_create_task` are on `app.state`
so a route can trigger them without importing the machinery.

That was not enough for `github`, and the reason names the next piece
exactly. Approving an inbox item starts a real task, so the route reaches
`_github_create_task` -> `_start_task` -> classification, routing, the budget
default, the recorder and the graph stream. Exposing the narrow function on
`app.state` gets a route past it, but `_github_act` -- the helper the three
inbox routes share -- reaches it too, and lifting that helper drags the chain
across.

**`_start_task` and `_run_task` are now in `agent/tasks.py`** (2026-09-23),
the way the registries came out: `start_task(app, ...)` / `run_task(app, ...)`,
reaching the graph stream through `app.state.stream_graph`, and importing
nothing from `server.py` (`tests/test_tasks_module.py` pins that). `github` has
followed; `tasks` and `planning` are ordinary cuts now. Do not extract `provisioning.py`. An automated lift that
follows undefined names WILL follow this chain into most of server.py --
that is how this was found, and it is why the cut waits for the move.

**A pattern worth reusing:** state a route needs and a background task
mutates goes on `app.state` (`github_poll_wake`, `github_last_poll`, and
`config` itself now live there). `config` is set the moment the app is
created rather than in lifespan, because a TestClient exercises routes
without ever entering lifespan.

**Shared helpers now have homes**, which is most of what the first two seams
were for: `require_full_auth` and `forced_screen_block` in `agent/auth.py`,
`read_with_retry` in `agent/graph.py`, `audit_store(request)` in
`agent/routers/__init__.py`. A seam that needs one of those no longer has to
choose between a cycle and a copy.

**What the first extraction found, and it matters for every later seam:**
this FastAPI does not flatten `include_router` into `app.routes` -- it
appends one opaque wrapper. `tests/test_route_inventory.py` iterated
`app.routes` directly, so a seam moved into a router counted as ZERO routes:
the snapshot shrank by exactly what moved, and the obvious fix (update
EXPECTED) would have put every extracted route outside the only test that
checks a route still has a guard. `_walk()` now follows included routers and
a test asserts it still does.

Also moved: `require_full_auth` and `forced_screen_block` now live in
`agent/auth.py`, because a module under `agent/routers/` cannot import them
from `server.py` without a cycle. `server.py` re-exports them rather than
redefining, so they stay the SAME objects -- the inventory identifies a guard
by `__name__` and every test overriding auth is keyed on identity.

### What breaks

`agent/server.py` still holds the machinery the seams reach on `app.state`
(the graph stream, the planning turn runner, the binding to the projects
router's `_create_project`), the lifespan, and four routes: `/api/health`,
`/api/router-balance`, `/api/consolidation/status` and
`POST /api/github/repos`. Every named seam is out. What is left is ordinary: one seam per
commit, inventory and repo-scope green, and `provisioning.py`,
`history_index.py` and the review proxy never in the same change. The repo
check inside each handler is now pinned too — `tests/test_repo_scope.py`
snapshots every repo-scoped route with the check that guards it, so a seam
that moves and loses `check_repo_access` fails CI (2026-09-23).

### What already exists

- `tests/test_route_inventory.py` is the safety net. The playbooks README
  already forbids a big-bang split: keep that test green through every step,
  and do not lose the incident comments.
- Seams named there: auth, tasks, planning, github, settings, uploads. The
  review proxy (`/_review/{path}`) is a seventh, already isolated at the
  bottom of the file.

### Shape of the fix

One router module per seam, imported and included from `server.py`. First
commit extracts one seam and updates the inventory comment. Repo-scoped
checks stay next to the handler that needs them until a later commit can
make `check_repo_access` a dependency rather than a manual call. Do not
extract `provisioning.py` or `history_index.py` in the same change.

---

## Virtualize the task log

**Status:** done, 2026-09-21. `TaskView` mounts a window anchored to the end
of the log, with a "Show earlier" control that grows it and holds the reading
position. Anchored rather than spacer-based because these rows are chat
bubbles: a one-line status and a rendered diff are two orders of magnitude
apart, and a spacer sized from the average of those is a scrollbar that lies.
`tests/TaskView.window.test.tsx` renders 3,000 entries and asserts the DOM
stays under 400 rows; removing the window fails three of its four tests.

### What breaks

`useTaskStream.ts` caps the in-memory log at 3,000 entries. `ChatMessage`
is memoized (audit H-16) so an append does not re-render every row, but
the DOM still mounts every row. A multi-hour task hits the cap and the tab
gets slow before it does.

### What already exists

`ChatMessage` is already `React.memo`. `useTaskStream` already drops from
the front of the array. There is no windowing library in `frontend/`.

### Shape of the fix

A viewport-sized window over the existing array — either a small existing
pattern (absolute-positioned spacers + a slice) or one dependency if the
bundle cost is measured. Keep the 3,000 cap. A test that renders a long
log must not mount 3,000 nodes.

---

## A CI job that actually starts the compose stack

**Status:** done, 2026-09-21. The `bundle` job (workflow_dispatch) runs
`docker compose up -d --wait`, polls `/api/health` until 200, asserts every
declared service is still running -- `--wait` returns on healthchecks, so a
service that crashes just after would otherwise look like success -- dumps
logs on failure and always `down -v`s. A placeholder key proves the stack
comes up without reaching an upstream: compose waits on the router's
LIVENESS probe, which needs no key.

### What breaks

Bundle regressions (`70e3cd9`, `29f7305`) were found by hand. Offline CI
cannot see a missing healthcheck, a secret file that is not written before
the reviewers start, or an image that does not contain `curl`.

### What already exists

Image HEALTHCHECKs on agent and router. Compose now waits on them (see
`docker-compose.yml`). The shell job validates the file. The manual
"Stack checks" job is the precedent for an expensive, opt-in run.

### Shape of the fix

A `workflow_dispatch` (or nightly) job: `docker compose up -d --wait`,
curl `/api/health` until 200, `docker compose down`. Needs
`OPENROUTER_API_KEY` only if the readiness probe on the router requires a
real upstream — liveness does not. Do not put this on every push; the
images are the cost.

---

## The reviewer runs agent-authored checks on the host

**Status:** done, 2026-09-21. Checks, the build, and the composer/mix
dependency installs go through one `runAgentCode` in
`services/commit-reviewer/reviewer.js`: the sandbox container on a host
install, in-process in the bundle (already contained, and deliberately not
given the docker socket), and a refusal otherwise -- never a fall back to the
host. The decision is written up in `SECURITY.md`.

Verified against the real projects before wiring: nine checks across three
projects, eight identical sandboxed vs host. The ninth, `pnpm audit`, needs
egress, so a check may opt in with `network: "bridge"` -- in `projects.json`,
which the agent cannot write. Two things would have broken every project and
did not, because they were tested rather than reasoned about: a symlinked
`node_modules` dangles inside a container, and a `mount --bind` nested in the
worktree is not carried in by a plain bind of its parent.

### What breaks

`services/commit-reviewer` runs the project's typecheck/lint/test suite
against a review worktree, on the host (or, in the bundle, in the reviewer
container with the projects mount). Env is sealed. That is not isolation.
A branch that changes `package.json` to add a `postinstall` is the
residual path the reviewer already refuses to borrow for; a test file that
opens a socket is not.

### What already exists

The agent sandbox (`docker/agent-sandbox`, `--cap-drop ALL`, mount
allow-list). The reviewer already installs with `--ignore-scripts` when
the branch changed a manifest. Bundler is already refuse-rather-than-run.

### Shape of the fix

Pick one and write it down in `SECURITY.md` before coding:

1. Run the check suite in the same sandbox image the agent uses, with
   network only when the project's own checks need it, and label that
   case in the verdict.
2. Keep host execution, and treat network-touching tests as an explicit
   project setting (onboarding already disables some of those).

Do not silently move checks into `--network none` — that is how a suite
that needs a registry becomes a false `NEEDS_FIXES`.

---

## The sandbox is filesystem isolation, not network isolation

**Status:** ACCEPTED, 2026-09-21. Not planned. Closed as "it is what it is",
which is a decision rather than a backlog item, so it should stop appearing
as work.

The Docker socket is mounted into the agent so it can start sibling sandbox
containers. Anything that can reach that socket can ask for a privileged
container, so within the bundle that container is host-root equivalent, and
`agent/tools/sandbox.py`'s mount allow-list is a guard in the CLIENT -- a
socket proxy enforcing it server-side is the only real fix.

Why it is accepted rather than fixed:

* On a host install it changes nothing. The agent already runs as root on
  that machine, so the socket grants it nothing it does not have. The
  exposure is specific to the compose bundle.
* The proxy would be real work -- an allow-list of API calls and of mount
  sources, kept in step with what `sandbox.py` legitimately needs -- to
  harden a boundary that, in the deployment where it matters, sits between an
  operator and their own machine.
* The thing that WAS a genuine escalation on a host install -- the reviewer
  running agent-authored code as root outside any container -- is closed
  (SECURITY.md). That was the one worth the work.

`SECURITY.md` lists it under "what this does not fix", which is where it
should be read: a known, stated property of the design, not an oversight.
Revisit it if the bundle is ever run somewhere the operator is not also the
machine's owner -- a shared host, or a hosted edition.

### What already exists

`agent/tools/url_guard.py` on the host browse/preview path (scheme,
resolve-then-validate, re-check redirects). In-sandbox `curl` is not
that guard. `SECURITY.md` already calls arbitrary shell the product.

### Shape of the fix

Either default the sandbox to no network and punch a hole for
`run_checks` / preview, or document that egress is intended and keep the
guard on the host-side URL tools only. The wrong fix is a prompt
instruction.

## Benchmarks: is the agent actually getting better? — **built, 2026-09-22**

Every item in this file is a change to the agent, and until now there was no
number that said whether any of them helped. Analytics answered "what
happened" — spend by day, model mix, which tasks ran — which is a different
question from "did last week's change work".

### What was built

`agent/benchmarks.py`, `GET /api/analytics/benchmarks`, and a Benchmarks panel
at the top of the Analytics page. Six numbers, each shown against the same
length window immediately before it:

| Metric | Reads |
| --- | --- |
| First-pass reviews | of the tasks that reached a review, how many passed without a redo |
| Fix cycles (median, p90) | how many times a task was sent back |
| Escalations | of all tasks, how many gave up and asked for a human |
| Cost per shipped task | median `cost_usd` over the episodes that shipped |
| History searches used | of the searches that ran, how many led to a record being read |
| Memory reads / prompt | how often an indexed section was actually fetched |

Sources are the ones that already exist: the `("episodes", repo)` namespace
(`outcome`, `iteration_count`, `cost_usd`, `review_verdict`) and
`logs/retrieval_events.jsonl`. No model call, no network, no new store — two
reads and some arithmetic, recomputed per request the way the rest of
Analytics is.

Three decisions worth keeping:

* **A comparison, not a number.** A 60% first-pass rate means nothing on its
  own. Every metric carries the previous window of the same length and the
  delta between them, and the delta is omitted — not shown as zero — when
  either window had nothing to divide by.
* **`null` is not `0`.** "No tasks ran" and "no tasks passed" are different
  answers, and rendering the first as 0% sends somebody looking for a
  regression that did not happen. The panel renders an em dash.
* **`sample_warning`.** Under ten tasks in either window, the panel says so.
  A confident green arrow drawn over three tasks is worse than no arrow.

The one trap, now covered by a test: `iteration_count` counts **redos**, not
passes — `verify_and_ship.py` only increments it in `_loop_back` — so a task
written once and passed records `0`. A first-pass threshold of 1 silently
folds every one-redo task into the headline number.

### The eval suite — **built, 2026-09-22**

These measure *production* tasks, so they move with whatever the operator
happened to ask for that fortnight. That is the right measure for "is it
getting better in practice" and the wrong one for "did this prompt change
help", which needs the same inputs both times.

`evals/` is the same question asked twice: twelve fixed goals against three
fixed fixture repos, run on demand by `scripts/run_evals.py`, scored by
per-task assertions AND by the same six metrics above. `evals/README.md` has
the full shape; the three decisions worth repeating here:

* **It runs the real pipeline, and stops before the merge without a special
  mode.** `require_merge_review` already parks a task after a READY verdict;
  the harness is an operator who never approves. A second "run but don't ship"
  code path would be one production never takes, and it would drift.
* **A task is scored on assertions, never on its outcome.** A task can ship,
  pass its checks and earn READY having "fixed" the bug by weakening the test.
  Assertions also split into goals (must *become* true) and guards (must
  *stay* true), and `--verify` refuses a suite whose goals already pass on the
  pristine fixture — an assertion true before the agent runs tests nothing.
* **Isolation is structural, not a convention.** Own SQLite store, own
  projects.json, own reviewer pair on free ports, own verdict state, own usage
  log. The live `projects.json` is never written.

**Found while building it:** pointing `AGENT_PROJECTS_JSON` at the fixtures is
not enough — the reviewer merges in `builtin-projects.local.js` and a
built-in-only project appears regardless, so the eval instance came up polling
real repositories. `REVIEW_ONLY_PROJECTS_JSON=1` closes it, and
`tests/test_evals_isolation.py` pins it.

**First full run, 2026-09-22:** 11/12 passed, $0.28, 67 minutes, 100%
first-pass reviews, 0 escalations — and on investigation the one failure was
the SUITE's fault. `py-top-n-heap` guarded `diff_excludes` on the test file
while meaning "do not weaken the tests"; the agent had added eight edge-case
tests and touched no existing line. The guard is now `file_matches` on the
functions that must survive, and the honest score for that run is 12/12.

**Second run, 2026-09-23:** 12/12, $0.24, 24 minutes, 100% first-pass, 0
escalations: a regression check after that day's lifecycle, supervisor,
workspace and router-split changes.

**30 tasks, and on the dashboard, 2026-09-23.** Two back-end fixtures
(`pyservice`, `nodeapp`) add 18 tasks: security, concurrency, time zones,
money, performance, refactors, and two test-writing tasks scored by mutation.
Each was checked both ways: it fails on the pristine fixture (`--verify`), and
a hand-written correct fix passes it (that check caught one assertion no
correct answer could pass). Analytics → Golden suite runs it detached, with
progress, Stop, history, per-task diffs and a copyable scorecard.

Worth keeping because it is the failure mode a benchmark is most prone to: a
spec that is stricter than its own intent marks good work as a regression,
and the number looks like a result. It only surfaced because the failing task
was investigated rather than believed — and diagnosing it is what added diff
capture to the report.

**Money is not the constraint; time is.** A full run costs pennies and takes
over an hour, so this is an overnight or CI tool, not something to run between
two edits.

**Not built, and deliberately:** no model judges the output. A rubric would
catch more and would make the benchmark's own verdict drift run to run, which
defeats the point of a fixed suite. The remaining gap is breadth — twelve
tasks over three synthetic fixtures is thin, and the honest next step is more
tasks rather than more machinery.

---

## Six accuracy improvements (operator's list, 2026-09-21)

Stored for later. Annotated against what this codebase already has, because
two of the six are built, two are half-built, and only two are new work — and
building what exists again is the expensive kind of mistake.

### 1. AST code graphing (tree-sitter) — NOT BUILT, and the largest of the six

Splitting files by token count cuts through function boundaries, so a chunk
arrives holding half a signature. Parse to an AST instead and extract the
symbol map: classes, signatures, imports/exports, call hierarchy. An agent
editing one file is then handed the exact signatures of what depends on it
rather than whole files.

**What exists:** `agent/cartographer.py` builds a codebase map, but it is an
LLM writing prose about structure, refreshed when the inventory hash changes.
Useful for orientation, and not a symbol graph — it cannot answer "who calls
this" or "what is this function's signature" without reading the files.

**Shape:** `tree-sitter` plus the grammars for the languages actually in these
projects. The map becomes data rather than prose. The interesting question is
whether it replaces the cartographer skill or feeds it; the honest answer is
probably feeds — the prose map is good at "why is this laid out this way",
which an AST cannot tell anyone.

### 2. Line-exact patch tools — ALREADY BUILT

`edit(path, old_string, new_string)` in `agent/tools/agent_tools.py` is exactly
this: exact string replacement that must match once, never a full-file rewrite.
There is also an edit-repeat guard that refuses the same failed
(path, old_string, new_string) a second time, seeded across work-node passes so
it survives a loop-back.

**Remaining gap, small:** no unified-diff tool for a patch spanning several
hunks, where the agent currently makes several `edit` calls.

### 3. Inline typecheck before the gate — HALF BUILT, and the gap is worth closing

**What exists:** `run_checks`, and the review gate re-runs the same checks.
**What is missing is that it is the model's choice.** The prompt asks the agent
to call it; nothing makes it. So a syntax error or a missing import can survive
until the gate, which is a Docker run away, and comes back as a review failure
rather than a compiler line.

**Shape:** run the project's fastest diagnostic (`tsc --noEmit`, `ruff`, the
configured lint) automatically after a patch, feed the raw diagnostics straight
back, and cap it at one or two micro-loops so a genuinely broken build still
reaches a human. The per-project check config already exists in
`projects.json`; what is missing is a "cheap subset" marker on it and the node
that runs it.

### 4. Reflexion — HALF BUILT, and the built half is the wrong half

**What exists is loop DETECTION:** `no_diff_streak`, `last_failed_edit_signature`,
`incomplete_plan_streak`, `short_conclusion_streak` in `agent/outer_state.py`.
These notice a stuck agent and eventually escalate.

**What is missing is the reflection itself.** Nothing forces a critique before
the next attempt, so the counters catch the third identical fix rather than
preventing the second.

**Shape:** a reflection key in the state schema; on a failed verification the
agent writes why it failed and what it will try differently, and that critique
is in the prompt for the retry. Note it composes with the new episode/history
search: a critique is exactly the kind of durable "what went wrong" the history
index is now able to retrieve across tasks.

### 5. Hybrid retrieval with re-ranking — SPARSE BUILT, DENSE ON (2026-09-29), RE-RANKER NOT

**Built (2026-09-21):** `agent/history_index.py`, Postgres full-text with a
weighted tsvector, over episodes, tasks and build transcripts.
**Built but NOT SWITCHED ON:** the dense/vector leg (`agent/episode_vectors.py`),
registered through `agent/episode_recall.py`'s leg registry.
**Not built:** the cross-encoder re-ranker over the fused candidates.

**2026-09-29:** the dense leg was switched on (router restarted, `EMBEDDINGS_ENABLED=1`,
agent restarted, 42 episodes backfilled for $0.0003). The gate below was never
measurable because the log held offers and uses as separate events; a use now
carries `rank` and a section read carries `position`. The re-ranker decision is
taken from those: a used episode at rank six or worse in more than a fifth of
uses means ordering is the problem and the re-ranker is the fix.

**The gate this was supposed to pass was never evaluated, and that is on the
record here rather than left to be inferred.** The build order made the dense
leg contingent on four weeks of retrieval telemetry showing a top-5 miss rate
above 20%. `logs/retrieval_events.jsonl` holds 15 `query` events and 5 `use`
events, all inside one 54-minute window on 2026-09-21 — no proportion can be
computed from that, so the decision the gate existed to make was never taken.
The code landed anyway. What did not land is the decision to run it:
`EMBEDDINGS_ENABLED` is unset, which is the default and which is what the gate
actually governs. Leave it unset until the telemetry has four weeks behind it.

**Switching it on takes three steps, in this order**, because the running
router predates `/v1/embeddings` and answers 404 to it:

1. restart `model-router` (never with a build task mid-call),
2. set `EMBEDDINGS_ENABLED=1`,
3. restart `tektonix`.

Until step 1, `embeddings.available()` correctly reports false and the store
opens with no index — the capability probe working as designed, not a fault.

**What a first measurement of the leg actually found (2026-09-21, 146 embedded
episodes, 11 queries, `openai/text-embedding-3-small`):** 2 clear wins on
paraphrase, 1 marginal, 3 neutral and 5 worse. Every one of the 5 came from the
same cause — a nearest-neighbour search has no concept of "no match", so the
leg filled all 8 slots of every page whether or not it had found anything, and
fusion counts positions. `episode_vectors.MIN_SIMILARITY` (0.35, measured) now
drops those, and the re-run shows both nonsense queries returning empty and the
paraphrase wins untouched. On an identifier-heavy corpus this leg is a
minority contributor by design; it is worth having for the query full text
cannot answer, and it is not worth turning on before the telemetry says
retrieval is missing.

Worth being precise about the claim in the list: the reason exact identifiers
rank correctly here is not a re-ranker, it is that sparse retrieval already
wins those and the fusion keeps its ordering. A re-ranker earns its place when
BOTH legs return plausible candidates and the ordering between them is wrong —
which is a question the retrieval telemetry
(`logs/retrieval_events.jsonl`) is being written to answer. Do not add a third
model to the path before that data says the ordering is the problem.

### 6. Dynamic context compression — ALREADY BUILT

Summarization middleware, with `summarization_trigger()` /
`summarization_keep()` in `agent/deep_agent.py`, the keep window clamped to 60%
of the trigger so summarization cannot fire immediately after summarizing.
Both are runtime-settable.

**Remaining gap:** it summarizes by token count, indiscriminately. The list's
sharper idea is to collapse *old tool output* while preserving active diffs and
the goal. That is a real improvement and smaller than it sounds, since the
middleware already exists and this is a change to what it keeps.

---

**If picking these up in order of value on this codebase:** 3 (cheap, closes a
real feedback gap), 4 (composes with history search), 6's refinement (existing
machinery), then 1 (large, and the only one that needs a new dependency). 5's
re-ranker last, and only if the telemetry says ordering is the problem -- that
telemetry is now the "History searches used" number on the Benchmarks panel
above. 2 needs nothing.

## Bundle ship path: what the 2026-09-29 audit left open

**Status:** not started. Found by an audit of the review-and-ship path as it
runs in the compose bundle; the findings that broke the first Windows ship
are fixed. These did not, and are cheap to lose track of.

- The agent image has no ssh client, so a project with an SSH origin cannot push from the bundle: install openssh-client, or push over https with the token for GitHub remotes.
- The base branch is `main` in four places (verify_and_ship, review_gate, github_ci): a `master` repository opens its pull request against a branch that does not exist. Record the default branch as `base_branch` at onboarding.
- In pull-request mode live never receives merged code, so post-merge check detection never runs and a project onboarded without checks is reviewed with none; and `fetch origin main` runs without the token, so a private https repo's base goes stale. Detect from the task branch, and fetch with the credential helper.
- Push mode in the bundle runs the deploy steps unsandboxed in the agent-review container, on the operator's checkout: skip them, or run them through the delegated sandbox.
- A failed review model call becomes a blocking finding the coder is asked to fix; mark it a harness failure.
- The agent runs npm scripts while the reviewer runs `review.checks`, so the two gates can disagree; one list.
- A review queued behind another is not `inProgress`, so the agent's wait is not extended for it; record the queue in state.
- Not in the bundle image: gitleaks (secret scan reports "binary not found" as a plain failure), and usage.jsonl lives in the image rather than a volume.
- An escalated task keeps the project's merge slot: an approved task on the same project waited sixteen minutes for "another task to finish its review and merge" while that task sat escalated. A task that escalates should release the slot.
- A lockfile the deploy's own install rewrote in live ("hasInstallScript", regenerated integrity fields) blocks the fast-forward merge of a dependency update with "your local changes would be overwritten". Seen 2026-09-29; fixed by hand with `git checkout -- package-lock.json` in the live checkout. The merge could discard such a lockfile itself when the incoming merge replaces it; an operator should decide whether they want an automated discard on the live checkout.
- The router test suite warns, about one run in five, that `connect_tcp.<locals>.try_connect` was never awaited: the fastest-providers refresh starts a real connection from `extra_body_for` in tests that never awaited it. Stub the refresh in the test fixture, or refuse to start one when the client is None.
- Reviews of one project run one at a time because each verdict is about a tree that live must still match at merge time. Parallel reviews per project would need the review split from the merge: review N branches against one base, merge them in turn, and re-run only the checks (not the model review) after each rebase; a review can then pass and still fail at merge, which the dashboard has to say plainly. Asked for on 2026-09-29; not started.

## Left open by the 2026-09-29 audit

- R3: a review of a foreign live install (the Windows bundle) runs `npm ci` in the sandbox on every review and every baseline, with no cache. A reviewer-owned install cache keyed by the lockfile hash, mounted read-only into the check container and accepted as a third mount root by the agent, would make it one install per lockfile change.
- A4: the empty-reply quiet retry runs on the coder seat for planner turns and pays twice; `_attempts()` should skip the quiet attempt when the request's model already has reasoning off, and planner turns should pick the planner's quiet seat.
- B20: `review_gate.py` still interpolates `base` raw in two shell strings (the loader now refuses a non-branch value); quote them with `shlex.quote` like `tools/git.py`.
- In the bundle `/app/keys` (deploy keys, keys/vapid.json) is not a volume and is lost when the container is recreated.
- Cargo.toml `rust-version` is 1.80 while the code is written for newer clippy; bump when the toolchain is settled.
