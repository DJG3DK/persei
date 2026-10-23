// commit-reviewer.js — the independent post-commit review gate.
//
// Polls each project's live repository for an `agent/<task-id>` branch whose
// tip has not been reviewed. The review unit is that branch plus its
// merge-base with live. The branch is checked out into a detached worktree
// off the live repo, provisioned with live's node_modules (or a fresh
// --ignore-scripts install when a manifest changed or live's install is
// stale), read-only binds of the data the tests need, and review-only
// credentials -- never the live ones. The project's real checks, build,
// database and secret scans run there, contained (see runAgentCode).
//
// The model (the router's `agent-reviewer` alias) then gets one call: the
// diff, the commit messages, the agent's "Response to review round N"
// answers from the branch's commit log, the prior round's findings, the
// current test and referenced files, and the mechanical results. It answers
// with one submit_review tool call. The verdict is derived in Node from
// failed checks and blocking findings, never trusted from the model's field.
//
// Findings go to state.json (per project and per branch) and history.jsonl
// (append-only). The agent reads the verdict through its own verify_and_ship
// gate; nothing is pushed at it here, and nothing merges from here.
//
// This file is the entry point pm2 runs and keeps what ties a review
// together: the project map, the state and history files, which branch is
// the review unit, the queue, reviewProject's orchestration and the verdict,
// and the control server. The rest is in six modules -- exec.js (how
// anything is run), sandbox.js (where agent-authored code runs: a container
// this process starts, or one the agent starts for it), node-modules-source.js
// (whose installed dependencies a review borrows), worktree.js (the review
// checkout), checks.js (the mechanical checks and whose failures they are)
// and prompt.js (what the model is shown and how its answer is read) -- and
// every name the tests import is still exported from here.

const fs = require('fs');
const path = require('path');
const http = require('http');
const crypto = require('crypto');
// The base layer: the installation root, the control secret, logging, the
// process runners, git with hooks off, and the one contained path for
// agent-authored code. See exec.js.
const { AGENT_HOME, REVIEW_CONTROL_SECRET, log, git } = require('./exec');
// What the model is shown, the call, and how its answer is read. See prompt.js.
const {
  REVIEW_DIRECT, readWorktreeFile, gatherExistingTestCoverage, gatherReferencedFiles,
  REVIEW_RESPONSE_MARKER, extractAgentResponses, reviewWithSonnet, stripLeakedMarkup,
  buildAgentMessage,
  reclassifyPreexisting,
} = require('./prompt');
// The review checkout: built, provisioned, torn down and swept. See worktree.js.
const worktree = require('./worktree');
const {
  WORKTREE_ROOT, NM_BUILD_CACHES, detectNodeModulesDirs,
  materializeDependencyDirs, installChangedDependencies, packagesNeedingOwnInstall,
  liveInstallIsStale, setupWorktree, cleanupWorktree,
} = worktree;
// The mechanical checks, and whose failures they are. See checks.js.
const {
  classifyInfrastructureFailures, baselineKey, runChecks, applyBaseline, markPreexistingFailures,
  runBuildCheck, runDatabaseCheck, runSecretScan,
} = require('./checks');
// What the review decides without the model: symlinks to secrets, CI edited
// to skip its checks, tests skipped or gutted, text addressed to the
// reviewer. See guards.js.
const { inspectBranch } = require('./guards');

// REVIEW_STATE_DIR exists for the bundle, where this service is a container
// and its verdicts have to outlive it -- and be readable by agent-review,
// which is a different container. On a host install it is unset and the files
// sit beside the code, exactly as before.
const STATE_DIR = process.env.REVIEW_STATE_DIR || __dirname;
const STATE_PATH = path.join(STATE_DIR, 'state.json');
// state.json is mutable: agent-review's clearReviewState() drops a branch's
// record when it merges, so a stale review can't gate the next commit -- and
// every finding on it, minor ones included, goes with it. This file is
// append-only and untouched by clearReviewState, so a review's full findings
// survive its own merge.
const HISTORY_PATH = path.join(STATE_DIR, 'history.jsonl');
const POLL_MS = 120_000; // 2 min — commits aren't frequent enough to need faster
const MAX_CONSECUTIVE_FIXES = 3; // this many NEEDS_FIXES in a row marks the record escalated
const HARNESS_RETRY_MS = 30 * 60 * 1000; // a harness-failed verdict is retried on the poll after this long

// MAX_CONSECUTIVE_FIXES only catches straight-line failure — it resets to 0
// the instant a round comes back READY. Seen live on a monorepo project'
// variantOptions.ts: 8 rounds total, but verdict kept flipping
// READY/NEEDS_FIXES/READY/NEEDS_FIXES as each round "fixed" the newest
// complaint and immediately opened a new one, so the consecutive counter
// never got past 1-2 and never tripped, even though the same file was
// visibly not converging. CHURN_* catches that pattern instead: how many
// rounds, within a window, has THIS SPECIFIC FILE shown up in findings —
// regardless of whether the verdicts in between were READY.
const CHURN_WINDOW_MS = 6 * 60 * 60 * 1000; // 6h — long enough to span a bad afternoon, short enough that old churn doesn't haunt a file forever
const CHURN_THRESHOLD = 3; // same file in findings across this many rounds -> escalate
const ROUTER_ENV_PATH = path.join(AGENT_HOME, 'services/model-router/.env');

// Each project's review config -- check commands (`dir` relative to the
// worktree root), which gitignored inputs are bound read-only, which
// credential files come from review-secrets/ -- comes from projects.json,
// with deployment-specific overrides in an OPTIONAL gitignored file so a
// public checkout ships no one's infrastructure. See
// builtin-projects.local.js.example. Anything defined there wins over
// projects.json (see services/shared/projects-config.js).
//
// REVIEW_ONLY_PROJECTS_JSON=1 skips them entirely, and agent/evals sets it.
// Built-ins are merged in on top of projects.json AND a built-in-only project
// still appears, which is right for the live reviewer and wrong for a second
// instance: without this an eval run polls the operator's real projects, and
// the moment one of them has a live task branch two reviewers are racing on
// the same repository -- writing verdicts into different state files, each
// unaware of the other's worktree. Observed on the first smoke run of the
// eval reviewer, which polled a real project and logged its branch.
let BUILTIN_PROJECTS = {};
if (process.env.REVIEW_ONLY_PROJECTS_JSON !== '1') {
    try {
        BUILTIN_PROJECTS = require('./builtin-projects.local');
    } catch (err) {
        if (err.code !== 'MODULE_NOT_FOUND') throw err;
    }
}

// Merged with projects.json so a wizard-onboarded project is reviewed without
// editing any file here; the built-in entries stay authoritative. See
// services/shared/projects-config.js for the merge rule.
const { loadProjects, healthProjectsCheck } = require('../shared/projects-config');
const { readJson, updateJsonSync } = require('../shared/json-state');

// A function, never a constant bound at startup. Until 2026-09-16 this was
// `const PROJECTS = loadProjects(...)`, so a project created from the
// dashboard did not exist here until pm2 restarted the service: the poll
// never looked at its branches, /check answered 404, and the agent's
// wait_for_review timed out. loadProjects reads a small file; once per tick
// and per request is nothing.
const loggedDetection = new Set();
function currentProjects() {
  const projects = loadProjects(BUILTIN_PROJECTS, { section: 'review' });
  for (const [name, cfg] of Object.entries(projects)) {
    // An explicit empty list is a decision -- "this project needs none" -- and
    // is honoured as written.
    if (Array.isArray(cfg.nodeModulesDirs) && cfg.nodeModulesDirs.length === 0) continue;

    const detected = detectNodeModulesDirs(cfg.live);
    const configured = Array.isArray(cfg.nodeModulesDirs) ? cfg.nodeModulesDirs : [];
    // A configured list is a FLOOR, not a ceiling. Treating it as the whole
    // answer only moves the staleness problem: a project onboarded with the
    // right list goes wrong the day it grows another package, which is exactly
    // how the project that prompted this broke -- its layout arrived after it
    // was onboarded. So anything the tree has now is added to what was
    // configured, and nothing configured is ever dropped.
    const added = detected.filter((d) => !configured.includes(d));
    if (!added.length) continue;
    cfg.nodeModulesDirs = [...configured, ...added];
    cfg.nodeModulesDirsDetected = added;
    const key = `${name}:${added.join(',')}`;
    if (!loggedDetection.has(key)) {
      loggedDetection.add(key);
      log(`[${name}] ${configured.length ? 'nodeModulesDirs is missing' : 'no nodeModulesDirs configured --'} `
        + `${added.join(', ')}${configured.length ? ', which the tree now has -- adding' : ' detected from the tree'}`);
    }
  }
  return projects;
}

// Confirmed live (2026-08-23, a monorepo project): state.json tracks one rolling
// review record PER PROJECT, not per task/thread -- if the sandbox branch
// ever gets reset/force-pushed (a stuck task cleaned up, a rebase, an
// abandoned branch), a previously-reviewed sha can stop being an ancestor
// of the new commit entirely. reviewProject used to trust prevState
// unconditionally regardless, so "carried over from rounds 1-4" findings
// kept getting repeated at the model even when round 4 reviewed a commit
// that was later discarded and has nothing to do with the current lineage
// -- confirmed one such case where the round-4 sha (a pastel-theming
// commit) wasn't reachable from the round-5 commit at all. `merge-base
// --is-ancestor` exits 0 only when the first sha is a real ancestor of the
// second (or missing/unreachable objects also fail it, which is the right
// behavior here too -- an unknown sha is not a valid "prior round").
async function isAncestor(cfg, ancestorSha, sha) {
  const result = await git(cfg.live, ['merge-base', '--is-ancestor', ancestorSha, sha]);
  return result.ok;
}

function loadState() {
  return readJson(STATE_PATH);
}
// Every write goes through here: agent-review writes the same file, and a
// load/save pair outside the lock can undo its write (see shared/json-state).
function updateState(mutate) {
  return updateJsonSync(STATE_PATH, mutate);
}

// A verdict belongs to a BRANCH. Each task has its own branch and, since
// 2026-09-23, its own workspace, so several branches of one project can be in
// review, parked READY, or looping on fixes at the same time. `state[project]`
// stays what the dashboard shows -- the latest review -- and
// `state[project].branches[branch]` holds each branch's own last verdict, which
// is what the round counter, the churn detector, the dedup check, the agent's
// wait and the merge gate all read. Without it, a second task's review
// overwrote the first's and the first lost its round history and its READY.
const MAX_BRANCH_RECORDS = 40;

// What a record means -- harnessFailed (a verdict that blocked only because
// a check could not run) and branchRecord (one branch's record, per branch
// since 2026-09-23) -- is defined once, in services/shared/review-records.js,
// with the agent's Python gate as its mirror. Both stay exported from here.
const { harnessFailed, branchRecord, TASK_BRANCH_RE } = require('../shared/review-records');

function withBranchRecord(projectState, branch, record) {
  const branches = { ...((projectState && projectState.branches) || {}), [branch]: record };
  const names = Object.keys(branches);
  if (names.length > MAX_BRANCH_RECORDS) {
    names.sort((a, b) => String(branches[a].reviewedAt || '').localeCompare(String(branches[b].reviewedAt || '')));
    for (const old of names.slice(0, names.length - MAX_BRANCH_RECORDS)) delete branches[old];
  }
  return branches;
}

// One line per review, never overwritten or cleared — the durable record
// state.json can't provide. Best-effort: a history-append failure should
// never take down a review that otherwise succeeded.
function appendHistory(project, entry) {
  try {
    fs.appendFileSync(HISTORY_PATH, JSON.stringify({ project, ...entry }) + '\n');
  } catch (err) {
    log(`[${project}] failed to append review history: ${err.message}`);
  }
}

// See CHURN_WINDOW_MS/CHURN_THRESHOLD above for why this exists alongside
// (not instead of) the consecutive-failure escalation. Reads history.jsonl
// rather than state.json specifically because state.json only ever holds
// the latest round — this needs the last several hours of them.
function computeFileChurn(project, currentFindings, branch = null) {
  const counts = new Map();
  const bump = (file) => counts.set(file, (counts.get(file) || 0) + 1);

  let lines = [];
  try {
    lines = fs.readFileSync(HISTORY_PATH, 'utf8').split('\n').filter(Boolean);
  } catch { /* no history file yet — first review ever for this instance */ }

  const cutoff = Date.now() - CHURN_WINDOW_MS;
  for (const line of lines) {
    let entry;
    try { entry = JSON.parse(line); } catch { continue; }
    if (entry.project !== project) continue;
    // Per branch: two tasks each drawing a finding on the same file is two
    // tasks, not one task going round in circles.
    if (branch && entry.branch && entry.branch !== branch) continue;
    if (!entry.reviewedAt || new Date(entry.reviewedAt).getTime() < cutoff) continue;
    for (const f of entry.findings || []) {
      // A diagnosis of a pre-existing failure is about the base commit, not
      // this branch going round in circles.
      if (f?.file && f.severity !== 'preexisting') bump(f.file);
    }
  }
  for (const f of currentFindings || []) {
    if (f?.file && f.severity !== 'preexisting') bump(f.file);
  }

  let churnFile = null;
  let churnCount = 0;
  for (const [file, count] of counts) {
    if (count >= CHURN_THRESHOLD && count > churnCount) {
      churnFile = file;
      churnCount = count;
    }
  }
  return churnFile ? { file: churnFile, count: churnCount } : null;
}

// The router's key. The upstream OpenRouter key only on the
// REVIEW_MODEL_OVERRIDE evaluation path, which bypasses the router.
function getOpenRouterKey() {
  const name = REVIEW_DIRECT ? 'OPENROUTER_API_KEY' : 'MODEL_ROUTER_KEY';
  // The environment first, because in the container bundle there is no
  // services/model-router/.env to read -- the key arrives as an env var and
  // the router is a sibling container. On a host install nothing changes:
  // pm2 does not export it, so the file is still what answers.
  if (process.env[name]) return process.env[name].trim();
  // The bundle generates the router key on first boot and hands it to each
  // consumer as a file, so it is never a compose default anyone can guess.
  const keyFile = process.env[`${name}_FILE`];
  if (keyFile) {
    const key = fs.readFileSync(keyFile, 'utf8').trim();
    if (!key) throw new Error(`${name}_FILE (${keyFile}) is empty`);
    return key;
  }
  const env = fs.readFileSync(ROUTER_ENV_PATH, 'utf8');
  const m = env.match(new RegExp(`^${name}=(.+)$`, 'm'));
  if (!m) throw new Error(`${name} is set neither in the environment nor in ${ROUTER_ENV_PATH}`);
  return m[1].trim();
}

// A review unit is a BRANCH plus the base it forked from -- not "whatever the
// sandbox HEAD happens to be right now". The old model compared sandbox HEAD to
// live HEAD and inferred everything else, which has produced two distinct
// classes of false finding: stale-lineage carry-over (patched at the prevState
// check) and fully inverted diffs when live moved ahead of the sandbox (two
// false `blocking` findings on one project's pump-protection settings, which the
// commit had in fact ADDED). Both are symptoms of having no stable review unit.
//
// With per-task branches the base is `merge-base(live, branch)`, fixed at the
// point the work forked. It cannot drift when live moves, so direction is
// unambiguous by construction rather than by guard.
// A task branch is `agent/<task-id>` and nothing else. Anything else under
// refs/heads/agent -- a manual backup taken before a rebase, a rename -- is
// not work the agent is shipping, and reviewing it is worse than useless:
// 2026-09-16, `agent/230eed5b-pre-rebase-backup` sat unmerged beside the live
// task's branch, so every round found "new work" on whichever of the two the
// previous round had not reviewed. Forty reviews of webapp in a day, each
// one overwriting the project's single review record, and the merge gate
// refused the real task's READY as "stale" because `branch` had just been
// clobbered by the backup branch's in-progress review. The regex is
// TASK_BRANCH_RE, shared with agent-review (services/shared/review-records.js).
const ignoredRefs = new Set();

// The worktree that has `branch` checked out, from live's own worktree list.
async function worktreeFor(cfg, branch) {
  const out = (await git(cfg.live, ['worktree', 'list', '--porcelain'])).output || '';
  let path = null;
  for (const line of out.split('\n')) {
    if (line.startsWith('worktree ')) path = line.slice('worktree '.length);
    else if (line === `branch refs/heads/${branch}` && path) return path;
  }
  return null;
}

// `prev` is the project's current review record; a parameter so a test can
// drive this against a scratch repository without a state file. `requested`
// is a branch the caller is waiting on, which wins over the usual choice.
async function detectNewCommit(project, cfg, prev = loadState()[project], requested = null) {
  // The agent's workspace is now a git worktree of this same repository, so its
  // per-task branch is already a local ref here -- there is no clone to fetch
  // from and no `agent` remote in the picture. Branches are `agent/<task-id>`.
  const refsOut = (await git(cfg.live, [
    'for-each-ref', '--sort=-committerdate', '--format=%(refname:short)',
    'refs/heads/agent',
  ])).output.trim();
  const all = refsOut ? refsOut.split('\n').map((r) => r.trim()).filter(Boolean) : [];
  const candidates = all.filter((r) => TASK_BRANCH_RE.test(r));
  for (const r of all) {
    if (candidates.includes(r) || ignoredRefs.has(`${project}:${r}`)) continue;
    ignoredRefs.add(`${project}:${r}`);
    log(`[${project}] ignoring ${r}: not a task branch (agent/<task-id>), so not a review unit`);
  }
  if (!candidates.length) return null;

  const liveHead = (await git(cfg.live, ['rev-parse', 'HEAD'])).output.trim();

  // Exactly ONE branch per project is the review unit. The merge endpoint
  // merges the branch the verdict names, so a verdict on any other branch is
  // either useless (a finished task) or wrong (it would be merged instead).
  // The workspace's own branch is the live task by construction; when the
  // workspace is on main or detached, the newest branch live does not yet
  // contain is the best guess. Older unmerged branches are never visited.
  const wsBranch = (await git(cfg.sandbox, ['rev-parse', '--abbrev-ref', 'HEAD'])).output.trim();
  // A caller that knows WHICH commit it is waiting on names its branch, and
  // that wins over every guess below. "Older unmerged branches are never
  // visited" was true when a project had one task at a time and branches
  // merged promptly; with a queue and pull-request shipping it became
  // starvation. 2026-09-22: a task parked READY, a second task's branch was
  // then reviewed and became the project's unit, the operator approved the
  // first -- and its re-review asked for a branch the reviewer would never
  // pick, so it waited out 900s and escalated. Only a real task branch that
  // exists is honoured; anything else falls through to the old choice.
  let ref = (requested && candidates.includes(requested)) ? requested : null;
  if (requested && !ref) {
    log(`[${project}] asked to review ${requested}, which is not a current task branch -- choosing as usual`);
  }
  if (!ref) ref = candidates.includes(wsBranch) ? wsBranch : null;
  if (!ref) {
    for (const c of candidates) {
      const h = (await git(cfg.live, ['rev-parse', c])).output.trim();
      if (h && !(await isAncestor(cfg, h, liveHead))) { ref = c; break; }
    }
  }
  if (!ref) return null;

  const head = (await git(cfg.live, ['rev-parse', ref])).output.trim();
  if (!head) return null;

  // Already contained in live -- merged, or live moved past it. Reviewing
  // that case is what produced inverted diffs, where a branch's additions
  // read as deletions of everything live had gained since.
  if (await isAncestor(cfg, head, liveHead)) {
    // Said out loud only when asked: a caller that named this branch is now
    // waiting on it, and silence here reads as a hung reviewer.
    if (requested === ref) log(`[${project}] asked to review ${ref}, but live already contains it -- nothing to review`);
    return null;
  }

  // Same branch at the same tip as last round -> already reviewed. Keyed on
  // branch AND sha so a re-tipped branch still counts as new work -- and read
  // from THAT branch's record, so another task's review in between does not
  // make this one look new.
  const prevForRef = branchRecord(prev, ref);
  if (prevForRef && prevForRef.lastReviewedSha === head && !harnessFailed(prevForRef)) return null;
  if (prevForRef && prevForRef.lastReviewedSha === head) {
    // Asked for by name: now. On the poll: after a while, so a harness that
    // is still broken is not re-run every tick (it was, every five minutes,
    // the afternoon this was written).
    const age = prevForRef.reviewedAt ? Date.now() - Date.parse(prevForRef.reviewedAt) : Infinity;
    if (requested !== ref && !(age >= HARNESS_RETRY_MS)) return null;
    log(`[${project}] ${ref} at ${head.slice(0, 12)} was last blocked by the review harness itself, not by a finding -- reviewing it again`);
  }

  // Don't review a moving target: the workspace holding this branch must be
  // clean. Since 2026-09-23 each task has its own worktree, so that is
  // whichever worktree has the branch checked out -- not the project's
  // `sandbox`, which no task works in any more. A branch no worktree holds
  // is not being written to.
  const holder = (await worktreeFor(cfg, ref)) || (wsBranch === ref ? cfg.sandbox : null);
  if (holder) {
    const statusOut = (await git(holder, ['status', '--short'])).output.trim();
    if (statusOut) {
      if (requested === ref) log(`[${project}] asked to review ${ref}, but its workspace has uncommitted changes -- waiting for a clean tree`);
      return null;
    }
  }

  const base = (await git(cfg.live, ['merge-base', 'HEAD', head])).output.trim();
  if (!base) {
    log(`[${project}] ${ref} shares no history with live -- skipping rather than reviewing an unrelated tree`);
    return null;
  }
  return { sha: head, branch: ref, base };
}

// Projects currently mid-review — guards against the 2-min poll and a manual
// "Check now" click (see startControlServer) racing each other onto the same
// worktree path for the same project.
const inProgressProjects = new Set();
// Branches asked for while their project was already being reviewed, in the
// order asked. Before this a busy reviewer answered "already reviewing" and
// forgot the request, and the task that made it waited out its whole review
// timeout for a review nobody was going to run -- rare with one task per
// project, routine with several.
const pendingReviews = new Map();

// The queue is written to state.json as `queued` too, so the agent's gate
// can see that its branch is waiting its turn and keep waiting, the way it
// does for a review the service says is in progress. 2026-09-29: ten
// approvals in a row on one project; each merge moved the base, each
// waiting task rebased and asked for a fresh review, and a request behind
// two or three five-minute reviews looked, to the gate, like a request
// nobody would ever run.
function persistQueue(project) {
  const q = pendingReviews.get(project) || [];
  updateState((state) => {
    if (!state[project] || typeof state[project] !== 'object') {
      if (!q.length) return false;
      state[project] = {};
    }
    if (q.length) state[project].queued = [...q];
    else delete state[project].queued;
  });
}

function queueReview(project, branch) {
  const q = pendingReviews.get(project) || [];
  if (!q.includes(branch)) q.push(branch);
  pendingReviews.set(project, q);
  persistQueue(project);
  return q.indexOf(branch) + 1;
}

function runNextQueued(project, routerKey) {
  const q = pendingReviews.get(project);
  if (!q || !q.length) return;
  const next = q.shift();
  if (!q.length) pendingReviews.delete(project);
  persistQueue(project);
  const cfg = currentProjects()[project];
  if (!cfg) return;
  log(`[${project}] reviewing queued request for ${next}`);
  setImmediate(() => reviewProject(project, cfg, routerKey, next)
    .catch((err) => log(`[${project}] queued check failed: ${err.message}`)));
}

/** The first sentence of a check's SETUP line: the reason it could not run. */
function setupReason(output) {
  const text = String(output || '').replace(/^SETUP:\s*/, '').trim();
  const first = text.split(/\.\s|\n/)[0].trim();
  return first || 'no reason recorded';
}

function setStep(project, step) {
  updateState((state) => {
    if (!state[project]?.inProgress) return false; // review already finished/aborted
    state[project].inProgress.step = step;
  });
}

async function reviewProject(project, cfg, routerKey, requested = null) {
  const unit = await detectNewCommit(project, cfg, undefined, requested);
  if (!unit) return { started: false };
  if (inProgressProjects.has(project)) {
    if (requested) queueReview(project, requested);
    return { started: false, reason: 'already reviewing' };
  }
  inProgressProjects.add(project);

  // `base` is the fork point, fixed for the life of the branch. Every range
  // below is base..sha, so what the reviewer sees is exactly the branch's own
  // work -- never re-interpreted when live moves underneath it.
  const { sha, branch, base } = unit;

  log(`[${project}] reviewing ${branch} @ ${sha.slice(0, 12)} (base ${base.slice(0, 12)})`);
  {
    // Only `inProgress` changes here. `branch`/`base`/`lastReviewedSha` stay
    // the last VERDICT's until this review produces its own: writing `branch`
    // at start left the record naming one branch while `lastReviewedSha`
    // still belonged to another, and agent-review's merge gate read that
    // pair as "newer commits since the last review" on the real task.
    updateState((state) => {
      state[project] = { ...state[project], inProgress: { sha, branch, base, startedAt: new Date().toISOString(), step: 'setting up worktree' } };
    });
  }

  let worktreePath;
  try {
    let setupIssues;
    // branchDepsChanged is carried to the baseline run so the base commit is
    // provisioned the same way this branch was.
    let branchDepsChanged;
    ({ worktreePath, setupIssues, depsChanged: branchDepsChanged } =
      await setupWorktree(project, cfg, sha, base));
    setStep(project, 'running checks');
    const checkResults = [
      ...setupIssues,
      ...await runChecks(cfg, worktreePath),
      ...await runBuildCheck(cfg, worktreePath),
      ...await runDatabaseCheck(cfg, worktreePath),
      ...await runSecretScan(cfg, worktreePath),
    ];
    // Full messages including bodies -- `--oneline` dropped everything after
    // the subject (2026-08-20: the agent cited file/line/commit evidence that
    // the "missing" wiring already existed, and the reviewer never saw it).
    // Reading the body was half the fix: nothing put the agent's answer in a
    // commit message until 2026-09-25, when verify_and_ship started writing
    // it under REVIEW_RESPONSE_MARKER and extractAgentResponses started
    // reading it.
    const fullCommitLog = (await git(cfg.live, ['log', `--format=%h %s%n%b`, `${base}..${sha}`])).output;
    const commitLog = fullCommitLog.slice(0, 8_000);
    // From the whole log, not the 8k the prompt shows: a long round-2 answer
    // must not be cut before the reviewer sees it.
    const agentResponses = extractAgentResponses(fullCommitLog);
    // audit H-10: a git-diff failure (pruned object, 300s timeout, >20MB
    // maxBuffer overrun -- which also truncates stdout MID-FILE, defeating
    // packDiff's boundary guarantee) must ABORT the review, not silently review
    // an empty or half-cut diff and compute READY from green checks alone.
    const diffResult = await git(cfg.live, ['diff', base, sha]);
    if (!diffResult.ok || (diffResult.output || '').length === 0) {
      // Thrown, not returned: the review-shaped object returned here reached
      // no caller that reads verdicts and left `inProgress` set on the
      // project (2026-09-25). The catch below clears it; the agent's wait
      // times out into its own escalation, which an unreadable diff deserves.
      throw new Error(`git diff ${base.slice(0, 12)}..${sha.slice(0, 12)} did not return a usable diff `
        + `(ok=${diffResult.ok}, bytes=${(diffResult.output || '').length}); failing the review closed`);
    }
    const diff = diffResult.output;
    // Before the model, and thrown on failure like the diff above: a review
    // that could not run its guards must not read as one that passed them.
    const guardFindings = await inspectBranch(cfg.live, base, sha);
    if (guardFindings.length) {
      log(`[${project}] guards: ${guardFindings.map((g) => `${g.guard} (${g.severity}) ${g.file}`).join('; ')}`);
    }

    // Loaded before the review call (not just before the verdict/escalation
    // bookkeeping below, where this used to live) so reviewWithSonnet can see
    // the prior round's findings and be asked directly whether this round's
    // issues share a root cause with them — see priorRoundContext in prompt.js.
    const projectState = loadState()[project];
    let prevState = branchRecord(projectState, branch);
    if (prevState?.lastReviewedSha && !(await isAncestor(cfg, prevState.lastReviewedSha, sha))) {
      // The prior round's commit isn't in this commit's own history anymore
      // (see isAncestor's own comment) -- its findings/streak belong to a
      // different, now-discarded lineage. Starting fresh here is exactly
      // the same as this project's very first review ever: no carried-over
      // context, no inherited consecutiveNeedsFixes/escalated state.
      log(`[${project}] prior reviewed sha ${prevState.lastReviewedSha.slice(0, 12)} is not an ancestor of ${sha.slice(0, 12)} -- discarding stale review history instead of carrying it forward`);
      prevState = null;
    }
    // Before the baseline, so a missing tool is already labelled if the base
    // run turns out to be missing it too (in which case it is pre-existing and
    // blocks nothing).
    classifyInfrastructureFailures(checkResults);
    // The baseline is a fact about the BASE commit, cached per base sha, so
    // any branch's cache serves -- this one's first, else the project's latest.
    const baseline = await markPreexistingFailures(project, cfg, base, checkResults,
      prevState?.baseline || projectState?.baseline, branchDepsChanged);
    const existingTestCoverage = gatherExistingTestCoverage(worktreePath, diff);
    const referencedFiles = gatherReferencedFiles(worktreePath, commitLog, diff);

    let review;
    try {
      setStep(project, 'awaiting Sonnet review');
      review = await reviewWithSonnet(routerKey, project, commitLog, diff, checkResults, prevState, existingTestCoverage, referencedFiles, agentResponses, guardFindings);
    } catch (err) {
      log(`[${project}] Sonnet review call FAILED — failing closed (NEEDS_FIXES): ${err.message}`);  // audit C-3: was fabricating READY
      review = { verdict: 'NEEDS_FIXES', summary: `The automated review could not complete (${err.message}). This is NOT an approval — the qualitative review did not run.`, findings: [{ severity: 'blocking', file: undefined, issue: `Review call failed (${err.message}); no qualitative review was performed. Blocking by policy until a real review runs.` }] };
    }

    // Derived from concrete, structured data (failed checks, blocking
    // findings) rather than trusting Sonnet's own self-reported `verdict`
    // field directly — seen live: a response with verdict=NEEDS_FIXES but
    // zero blocking findings (and all checks green), which produced a
    // nudge with nothing concrete to act on (see buildAgentMessage). This
    // way verdict can never be NEEDS_FIXES without something specific to
    // point at, by construction.
    const mechanicalFailed = checkResults.some((c) => !c.ok && !c.preexisting);
    // A check that could not run still blocks -- nothing was learned about the
    // code, so READY would be a lie. But it must not be handed to the agent as
    // something to fix: it is the harness's failure, invisible from inside the
    // worktree. Escalate on the FIRST round instead of after three, because
    // three rounds of an agent guessing at an environment it cannot see is
    // exactly the loop this is here to stop.
    const infraFailures = checkResults.filter((c) => !c.ok && !c.preexisting && c.infrastructure);
    const infraFailed = infraFailures.length > 0;
    if (infraFailed) {
      // Say so in the summary, which is the field everything downstream
      // actually reads. Without this the escalation reads as "your commit was
      // rejected" and the next person to look starts debugging the diff.
      const names = infraFailures.map((c) => c.name).join(', ');
      const plural = infraFailures.length > 1 ? 'those checks' : 'that check';
      const why = infraFailures.map((c) => `${c.name}: ${setupReason(c.output)}`).join('; ');
      review.summary = `The gate could not RUN ${names} (${why}), so ${plural} never executed and `
        + `nothing was learned about this commit either way. This is a fault in the review harness, `
        + `not in the code under review -- it cannot be fixed from inside the repository, and the `
        + `commit is blocked only because an unrun check cannot be counted as a pass.\n\n${review.summary || ''}`;
    }
    // A pre-existing failure cannot block, whatever the model filed it as.
    const reclassified = reclassifyPreexisting(review, checkResults);
    if (reclassified) {
      const names = checkResults.filter((c) => !c.ok && c.preexisting).map((c) => c.name).join(', ');
      review.summary = `Not blocking: ${names} also fail${names.includes(',') ? '' : 's'} on the base commit in the `
        + `review harness, so ${reclassified === 1 ? 'the finding that blamed this change for it was' : `the ${reclassified} findings that blamed this change for it were`} `
        + `kept as a diagnosis instead.\n\n${review.summary || ''}`;
    }
    // The guards' answers go in after the model's and after the pre-existing
    // reclassification, so neither can talk them out of the record: a
    // blocking guard is NEEDS_FIXES whatever the model said, and a flag is
    // carried on the record (`flags`) for verify_and_ship, which parks a
    // flagged READY for the operator instead of merging it.
    review.findings = review.findings || [];
    for (const g of guardFindings) {
      review.findings.push({
        severity: g.severity === 'blocking' ? 'blocking' : 'minor',
        file: g.file,
        issue: `[harness: ${g.guard}${g.severity === 'flag' ? ', needs the operator\'s look before merge' : ''}] ${g.issue}`,
      });
    }
    const flags = guardFindings.filter((g) => g.severity === 'flag');
    const hasBlockingFindings = (review.findings || []).some((f) => f.severity === 'blocking');
    // audit H-9: ANY omitted file forces NEEDS_FIXES in NODE -- not left to
    // the model, which the prompt could talk out of it. (An unreadable diff
    // never reaches this point: the git diff call throws, and the outer
    // catch records a harness failure for the agent to see.)
    const omittedFiles = review._omittedFiles || [];
    const diffIncomplete = omittedFiles.length > 0;
    if (diffIncomplete) {
      review.findings = review.findings || [];
      review.findings.push({
        severity: 'blocking',
        file: null,
        issue: `${omittedFiles.length} file(s) were too large to include and were NOT reviewed: ${omittedFiles.join('; ')}. Blocking by policy until they can be reviewed (split the commit or review them out of band).`,
      });
    }
    const verdict = mechanicalFailed || hasBlockingFindings || diffIncomplete ? 'NEEDS_FIXES' : 'READY';

    // The circuit breaker. `escalated` is set on the record after
    // MAX_CONSECUTIVE_FIXES NEEDS_FIXES rounds in a row, when one file keeps
    // drawing findings (churn), or when a check could not run at all, and
    // stays set until a READY clears it. This service only records the flag;
    // verify_and_ship reads it -- on a real project it stops looping and
    // hands the task to a human, on a benchmark it ships the fix as disputed.
    // A round the harness lost says nothing about the code, so it neither
    // adds to the run of failures nor resets it.
    const consecutiveNeedsFixes = verdict === 'READY' ? 0
      : infraFailed ? (prevState?.consecutiveNeedsFixes || 0)
      : (prevState?.consecutiveNeedsFixes || 0) + 1;
    // An escalation the harness caused (a review that could not run) is not
    // a judgement that carries over: the first real review starts clean.
    const wasEscalated = Boolean(prevState?.escalated) && !harnessFailed(prevState);
    // Computed BEFORE appendHistory below writes this round's own entry —
    // otherwise a later round would double-count this one (once read back
    // from history, once from currentFindings).
    const churn = verdict === 'READY' ? null : computeFileChurn(project, review.findings, branch);
    const escalated = verdict === 'READY' ? false : (wasEscalated || infraFailed || consecutiveNeedsFixes >= MAX_CONSECUTIVE_FIXES || Boolean(churn));

    const record = {
      // The review unit, recorded in full: a verdict is only meaningful for the
      // branch and base it was produced against. agent-review's merge endpoint
      // reads `branch` so it merges exactly what was reviewed, and the next
      // round compares both branch and sha before deciding this is new work.
      branch,
      base,
      lastReviewedSha: sha,
      verdict,
      summary: review.summary,
      findings: review.findings,
      omittedFiles,  // audit H-9: record what the review could not see
      flags,  // guards.js: a READY with any of these waits for the operator
      agentResponses: agentResponses.length,  // how many of the agent's answers this round was given
      // Failures keep their output. Stripping it was why nothing downstream
      // could say WHY a check failed -- the text was captured, shown to the
      // review model, then dropped before anyone else could read it.
      checkResults: checkResults.map((c) => ({
        name: c.name,
        ok: c.ok,
        ...(c.preexisting ? { preexisting: true } : {}),
        ...(c.infrastructure ? { infrastructure: true } : {}),
        ...(c.ok ? {} : { output: (c.output || '').slice(-2000) }),
      })),
      // The message for whoever acts on this verdict. Built here rather than
      // by each consumer so there is one wording, and so buildAgentMessage is
      // on a live path instead of being exercised only by its own tests.
      agentMessage: verdict === 'READY' ? null : buildAgentMessage(review, checkResults),
      baseline,  // per-base-sha cache of which checks fail on base, so a slow suite is re-run there once
      reviewedAt: new Date().toISOString(),
      consecutiveNeedsFixes,
      escalated,
    };
    updateState((state) => {
      state[project] = { ...record, branches: withBranchRecord(state[project], branch, record) };
    });
    appendHistory(project, record);

    if (verdict === 'NEEDS_FIXES' && !wasEscalated) {
      log(`[${project}] NEEDS_FIXES — findings recorded in state.json/history.jsonl for the dashboard`);
    } else if (verdict === 'NEEDS_FIXES') {
      log(`[${project}] still NEEDS_FIXES (${consecutiveNeedsFixes} in a row) — already escalated`);
    } else {
      log(`[${project}] READY — no issues found`);
    }
    return { started: true };
  } catch (err) {
    log(`[${project}] review failed with an internal error: ${err.message}`);
    // Recorded as a verdict the harness produced, so the agent waiting on
    // this sha learns the reason now instead of waiting out its timeout and
    // reporting "did not review" (2026-09-29). Like any harness verdict it
    // does not count as a review of the commit: the next request tries again.
    const record = {
      branch: unit.branch, base: unit.base, lastReviewedSha: sha, verdict: 'NEEDS_FIXES',
      summary: `The gate could not set up the review (${String(err.message || err).slice(0, 300)}), so no check ran `
        + 'and nothing was learned about this commit either way. This is a fault in the review harness, '
        + 'not in the code under review.',
      findings: [], omittedFiles: [], agentResponses: 0,
      checkResults: [{ name: 'setup', ok: false, infrastructure: true, output: `SETUP: ${String(err.stack || err.message || err).slice(-2000)}` }],
      agentMessage: null, reviewedAt: new Date().toISOString(), consecutiveNeedsFixes: 0, escalated: true,
    };
    updateState((state) => {
      // Own-property check first: `project` came in over HTTP, and a key like
      // __proto__ must never reach the delete (CodeQL js/prototype-polluting-assignment).
      if (!(Object.hasOwn(state, project) && state[project]?.inProgress?.sha === sha)) return false;
      delete state[project].inProgress;
      state[project] = { ...state[project], ...record, branches: withBranchRecord(state[project], unit.branch, record) };
    });
    return { started: true, error: err.message };
  } finally {
    if (worktreePath) await cleanupWorktree(cfg, worktreePath);
    inProgressProjects.delete(project);
    runNextQueued(project, routerKey);
  }
}

// Control port so the dashboard (a separate process, port 4100) can trigger
// an immediate review instead of the caller waiting out the rest of a 2-min
// poll window — same "Check now" idea as manually refreshing, just without
// waiting. Loopback on a host install; in the bundle it binds 0.0.0.0 so the
// sibling containers can reach it, with the ports still unpublished (see the
// bind below). Nothing proxies to it from outside: agent-review's server.js
// calls it server-side, and the mutating call takes the shared secret.
const CONTROL_PORT = Number(process.env.REVIEW_CONTROL_PORT) || 4101;
function startControlServer(routerKey) {
  // Any throw is a 500, never an exit. 2026-10-22 audit, R17: an
  // unauthenticated `GET //` made `new URL` throw in here, the process exited
  // and took the in-flight review and the queue with it -- a crash loop for
  // anyone who could reach the port.
  const server = http.createServer((req, res) => {
    try {
      handleControl(req, res, routerKey);
    } catch (err) {
      log(`control: ${req.method} request failed: ${err.message}`);
      if (!res.headersSent) res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'internal error' }));
    }
  });
  // See agent-review/server.js for why this is not simply loopback. The
  // control port is the one that takes mutating calls, so it stays behind the
  // shared secret either way.
  const bind = process.env.REVIEW_BIND_ADDRESS
      || (process.env.TEKTONIX_BUNDLE === '1' ? '0.0.0.0' : '127.0.0.1');
  server.listen(CONTROL_PORT, bind, () => log(`control server listening on ${bind}:${CONTROL_PORT}`));
  return server;
}

function badRequest(res, error) {
  res.writeHead(400, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ ok: false, error }));
}

function handleControl(req, res, routerKey) {
  // Liveness, unauthenticated on purpose: this port is reachable only from
  // the host's loopback or the bundle's own containers, and nothing here
  // is a secret (a configured secret reports `true`, never its value). No
  // model call, no review started -- safe to poll. 503 when a
  // dependency is missing, so a status-code-only probe is still correct.
  if (req.method === 'GET' && req.url === '/health') {
    const checks = {
      review_secret: {
        ok: Boolean(REVIEW_CONTROL_SECRET),
        detail: REVIEW_CONTROL_SECRET ? null : 'REVIEW_CONTROL_SECRET unset: the control endpoint is disabled',
      },
      // Same rule as agent-review, and literally the same function:
      // healthProjectsCheck in services/shared/projects-config.js.
      projects: healthProjectsCheck(currentProjects()),
      state_file: (() => {
        try {
          loadState();
          return { ok: true };
        } catch (e) {
          // A fixed sentence: /health is unauthenticated, and the error
          // text carries the file's path.
          log(`health: state.json unreadable: ${e.message}`);
          return { ok: false, detail: 'the review state file is unreadable' };
        }
      })(),
    };
    const ok = Object.values(checks).every((c) => c.ok);
    // A COUNT for anyone, the names only for a caller holding the control
    // secret -- the same rule as agent/health.py's project_count. In the
    // bundle this port is on the compose network, and "which private repo
    // is being worked on right now" is not this route's to publish.
    const presented = Buffer.from(req.headers['x-review-secret'] || '');
    const expected = Buffer.from(REVIEW_CONTROL_SECRET || '');
    const trusted = REVIEW_CONTROL_SECRET && presented.length === expected.length
      && crypto.timingSafeEqual(presented, expected);
    res.writeHead(ok ? 200 : 503, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      ok,
      service: 'commit-reviewer',
      checks,
      reviewing_count: inProgressProjects.size,
      ...(trusted ? { reviewing: [...inProgressProjects] } : {}),
    }));
    return;
  }
  // What each project actually verifies. The agent needs this to refuse
  // "Auto" on the GitHub inbox for a project with no checks: auto-starting
  // work whose gate runs nothing mechanical is a review in name only, and
  // the checks live here (projects.json plus this service's own built-ins),
  // nowhere the agent can read. Names only -- no commands, no paths, no
  // secrets -- and the shared secret is still required.
  if (req.method === 'GET' && req.url === '/projects') {
    if (!REVIEW_CONTROL_SECRET) {
      res.writeHead(503, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'REVIEW_CONTROL_SECRET not configured' }));
      return;
    }
    const provided = Buffer.from(req.headers['x-review-secret'] || '');
    const expected = Buffer.from(REVIEW_CONTROL_SECRET);
    if (provided.length !== expected.length || !crypto.timingSafeEqual(provided, expected)) {
      res.writeHead(401, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ ok: false, error: 'invalid or missing X-Review-Secret' }));
      return;
    }
    const out = {};
    for (const [name, cfg] of Object.entries(currentProjects())) {
      const checks = Array.isArray(cfg.checks) ? cfg.checks : [];
      out[name] = {
        checks: checks.length,
        names: checks.map((c) => c.name).filter(Boolean),
        // Generated code outside git and node_modules (a Prisma client) and
        // how to regenerate it. The agent keeps its own workspaces' copies
        // current with the same rule this service applies to its worktrees.
        generated: (cfg.generated || []).filter((g) => g && g.dir && g.schemaFile && g.regenerate)
          .map((g) => ({ dir: g.dir, schemaFile: g.schemaFile, regenerate: g.regenerate })),
      };
    }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, projects: out }));
    return;
  }
  // Split the query off BEFORE matching: `[^/]+` would otherwise read
  // "storefront?branch=agent/..." as the project name.
  let parsed;
  try {
    parsed = new URL(req.url, 'http://control.local');
  } catch {
    badRequest(res, 'malformed request path');
    return;
  }
  const m = parsed.pathname.match(/^\/check\/([^/]+)$/);
  const requestedBranch = parsed.searchParams.get('branch') || null;
  if (req.method !== 'POST' || !m) {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: 'not found' }));
    return;
  }
  // audit C-4: fail closed on a missing secret; constant-time compare.
  if (!REVIEW_CONTROL_SECRET) {
    res.writeHead(503, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: 'REVIEW_CONTROL_SECRET not configured' }));
    return;
  }
  const provided = Buffer.from(req.headers['x-review-secret'] || '');
  const expected = Buffer.from(REVIEW_CONTROL_SECRET);
  if (provided.length !== expected.length || !crypto.timingSafeEqual(provided, expected)) {
    res.writeHead(401, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: 'invalid or missing X-Review-Secret' }));
    return;
  }
  let project;
  try {
    project = decodeURIComponent(m[1]);
  } catch {
    badRequest(res, 'malformed project name');
    return;
  }
  const cfg = currentProjects()[project];
  if (!cfg) {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: false, error: `unknown project "${project}"` }));
    return;
  }
  if (inProgressProjects.has(project)) {
    // Queued, not dropped: it runs as soon as the current review ends.
    const position = requestedBranch && TASK_BRANCH_RE.test(requestedBranch)
      ? queueReview(project, requestedBranch) : null;
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ ok: true, started: false, queued: Boolean(requestedBranch), position, reason: 'already reviewing' }));
    return;
  }
  // Fire-and-forget: reviewProject can take minutes (real builds/tests/LLM
  // call), so the HTTP response doesn't wait for it — the dashboard polls
  // state.json's `inProgress` field (set synchronously before this returns,
  // via detectNewCommit + the inProgressProjects guard racing the request
  // itself) to show progress instead.
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ ok: true, started: true }));
  reviewProject(project, cfg, routerKey, requestedBranch)
    .catch((err) => log(`[${project}] manual check failed: ${err.message}`));
}

// worktree.js does the sweeping; the project map it needs is this file's.
function sweepLeftoverWorktrees(root = WORKTREE_ROOT, projects = currentProjects()) {
  return worktree.sweepLeftoverWorktrees(root, projects);
}

async function main() {
  fs.mkdirSync(WORKTREE_ROOT, { recursive: true });
  const routerKey = getOpenRouterKey();
  log('commit-reviewer started');
  await sweepLeftoverWorktrees();

  const tick = async () => {
    // Re-read per tick: a project onboarded since the last tick is polled on
    // this one, with no restart.
    for (const [project, cfg] of Object.entries(currentProjects())) {
      try {
        await reviewProject(project, cfg, routerKey);
      } catch (err) {
        log(`[${project}] tick failed: ${err.message}`);
      }
    }
  };

  // A review left "in progress" by a crash or a restart would hold the
  // agent's wait (and the merge gate) for a sha nobody is working on.
  updateState((state) => {
    let changed = false;
    for (const project of Object.keys(state)) {
      if (state[project] && typeof state[project] === 'object' && state[project].inProgress) {
        log(`[${project}] a review of ${String(state[project].inProgress.sha || '').slice(0, 12)} was in progress when this service last stopped; it will be asked for again`);
        delete state[project].inProgress;
        changed = true;
      }
      // The queue lives in this process; what the file says was queued is
      // gone with the last one, and the gates will ask again.
      if (state[project] && typeof state[project] === 'object' && state[project].queued) {
        delete state[project].queued;
        changed = true;
      }
    }
    return changed;
  });
  // The control port first: the first tick can run for many minutes when
  // a branch is waiting, and the agent's trigger got connection-refused
  // after every restart until it finished (2026-09-29).
  startControlServer(routerKey);
  tick().catch((err) => log(`first tick failed: ${err.message}`));
  setInterval(tick, POLL_MS);
}

if (require.main === module) {
  main();
}

module.exports = {
  currentProjects,
  // Kept for anything that still reads the old constant; it now answers
  // fresh too rather than handing out a startup snapshot.
  get PROJECTS() { return currentProjects(); },
  setupWorktree, cleanupWorktree, runChecks, runBuildCheck, runDatabaseCheck, runSecretScan,
  materializeDependencyDirs, installChangedDependencies,
  detectNewCommit, reviewWithSonnet, buildAgentMessage, applyBaseline, TASK_BRANCH_RE,
  classifyInfrastructureFailures, packagesNeedingOwnInstall, baselineKey,
  detectNodeModulesDirs, NM_BUILD_CACHES,
  branchRecord, withBranchRecord, harnessFailed, computeFileChurn, queueReview, runNextQueued, pendingReviews, sweepLeftoverWorktrees,
  liveInstallIsStale, readWorktreeFile, gatherReferencedFiles,
  extractAgentResponses, stripLeakedMarkup, REVIEW_RESPONSE_MARKER,
  startControlServer, reviewProject,
};
