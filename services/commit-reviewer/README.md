# commit-reviewer

An independent post-commit review gate for [Persei](https://github.com/DJG3DK/persei).

The agent's own claim of "done" carries no authority. When a task commits, this service checks out
that commit, runs the project's **real** checks, and hands the diff to a model for review. Only a
`READY` verdict merges. It runs as its own process, with its own model pin, deliberately: a gate the
agent can influence is not a gate.

## How a review runs

1. **Detect.** Poll each project for an unreviewed `agent/<task-id>` branch. The review unit is that
   **branch plus its merge-base** — fixed at the fork point.

   This matters more than it sounds. The original design compared the sandbox HEAD to the live HEAD
   and inferred everything else. When live moved ahead, the diff was computed *backwards*: a
   branch's additions read as deletions of everything live had gained since. That manufactured two
   `blocking` findings against a commit which had in fact **added** the trading-safety settings it
   was accused of removing. A fixed base makes direction unambiguous by construction rather than by
   guard.

2. **Set up a worktree.** Detached checkout at the reviewed sha, with:
   - `node_modules` linked or bind-mounted read-only from the live checkout — or installed fresh
     with `--ignore-scripts` when the branch changed a manifest or lockfile, or when live's own
     install no longer matches its lockfile (a merged Dependabot bump nobody installed)
   - **review-only credentials**, never the live ones (see below)
   - gitignored test inputs bound **read-only** — one project's is 348MB of recorded market data,
     without which 31 of 49 suites self-skip and report green having asserted nothing
   - PHP/Elixir/Ruby dependency directories bound read-only from live, or installed without
     scripts when the branch changed its manifest

3. **Run the real checks.** Lint, tests, build, database drift, secret scan — the commands the
   project's config names, contained in the sandbox container (or in this process in the bundle).
   A check that fails identically on the base commit is marked pre-existing and blocks nothing; a
   check whose command was missing is an infrastructure failure, escalated rather than handed to
   the agent as something to fix.

4. **Review the diff.** One call, one `submit_review` tool call back. The model never writes or runs
   code; the mechanical work already happened. Besides the diff and commit messages it sees the
   prior round's findings, the agent's own **responses** to earlier rounds (written into the
   follow-up commit under `Response to review round N` and read from the branch's log), the
   current content of test files and of files the diff references or calls, and the mechanical
   results — placed after the agent-authored material, which is fenced as untrusted data. A prior
   blocking finding that a response has disproved with a run is withdrawn unless the diff shows
   otherwise.

5. **Decide and record.** The verdict is derived in Node from failed checks and blocking findings,
   never taken from the model's own field. Verdict, findings and usage go to `state.json` (per
   project and per branch) / `history.jsonl` / `usage.jsonl`. Three rounds of `NEEDS_FIXES` in a
   row, one file drawing findings round after round, or an unrunnable check mark the record
   `escalated`; the agent's verify_and_ship gate reads that and hands the task to a human.

## Credentials

Review worktrees get a **non-production** credential set from `review-secrets/<project>/`, and the
path fails closed — a missing review secret is recorded as a failed check and never falls back to
the live one.

This replaced copying the live `.env` and `config/*.json` straight in, which meant a review — the
step whose entire job is to run code nobody has vetted yet — executed that code holding production
exchange keys, the live JWT secret, real SMTP and Telegram credentials, and for the e-commerce
project a live payment-provider secret, supplier keys able to submit real orders, and the production
`DATABASE_URL`.

The replacements are structurally valid rather than blank: encrypted exchange keys that decrypt to
obvious dummies, a real bcrypt hash, mail pointed at an unroutable host, side-effecting flags forced
off, and `DATABASE_URL` aimed at the test database. All 49 suites pass on them.

## Model

Routed through the model router as the `agent-reviewer` alias, so the model is swappable
from the agent's dashboard and its spend is rated and logged. It previously called OpenRouter
directly with a hardcoded model id, which made it invisible three ways: absent from the model
picker, no cost rates anywhere, and never seen by the router's logging callback.

**What this role needs, in order:** instruction-following and judgement, long context, then reliable
tool calling. It is *not* agentic — one call, no loop — and it never writes code. Its failure mode is
**false positives**: a wrong `blocking` finding costs a full fix-and-review round. Coding-tuned
models are the wrong instinct here, because coding benchmarks reward producing plausible code while
this job rewards *not* producing a plausible finding.

Note it pins `tool_choice` to `submit_review` — a forced tool call, which several model families
reject in thinking mode. `REVIEW_MODEL_OVERRIDE` is an evaluation-only escape hatch for A/B'ing a
candidate against a real commit before pinning it; a raw model id (containing `/`) bypasses the
router and talks to OpenRouter directly.

## Layout

```
reviewer.js        the entry point — project map, state and history, which branch is the
                   review unit, the queue, reviewProject's orchestration and verdict, the
                   control server; re-exports every name the tests import from the modules
exec.js            how anything is run — run/runSealed, git with hooks off, runAgentCode
                   (the one contained path for agent-authored code), the control secret
worktree.js        the review checkout — setup, node_modules and dependency-dir
                   provisioning, read-only binds, review secrets, cleanup, startup sweep
checks.js          the mechanical checks, build, database job and secret scan, and whose
                   failures they are (infrastructure vs pre-existing vs this change's)
prompt.js          what the model is shown (diff packing, test and referenced files, the
                   agent's responses, untrusted fencing), the router call, and how its
                   answer is normalised and turned into the agent's message
sandbox.js         the sandbox container: probe, run there, or delegate in the bundle
review-secrets/    non-production credentials per project (gitignored)
state.json         latest verdict per project and per task branch (gitignored)
history.jsonl      append-only review log (gitignored)
usage.jsonl        per-review model usage and cost (gitignored)
worktrees/         ephemeral per-review checkouts (gitignored)
```

## Running it

Runs under pm2 alongside the agent. It needs the router reachable at `MODEL_ROUTER_URL`
(default `http://127.0.0.1:4001/v1`) and `MODEL_ROUTER_KEY`, from the environment or the router's
`.env`. `REVIEW_CONTROL_SECRET` comes from `services/shared/.env` (see `services/shared/service-env.js`).

**Zero npm dependencies** — `reviewer.js` is Node stdlib only, so there is no `package.json` and
nothing to install.

**Projects come from `projects.json`**, written by the console's onboarding wizard: each repo's
live and workspace paths, its check commands, which gitignored inputs get bind-mounted read-only,
and which credential files come from `review-secrets/`. The file is re-read every poll tick, so a
project onboarded while the service runs is reviewed without a restart. An optional, gitignored
`builtin-projects.local.js` (see the `.example`) overrides any entry for a deployment whose checks
need hand-tuning — those commands deserve to be read, not copied, because they encode which of
your test suites are safe to run against a detached checkout.
