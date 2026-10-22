// checks.js -- the mechanical half of a review.
//
// The project's own checks, its build and post-build assertions, the
// database job and the secret scan, run in the review checkout. Each
// returns a list of { name, ok, output } results, and two more facts are
// attached to them here: whether a failure is the harness's rather than the
// code's (a missing tool, a read-only filesystem -- `infrastructure`), and
// whether it fails identically on the base commit (`preexisting`). The
// verdict in reviewer.js is derived from those.

const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const sandbox = require('./sandbox');
const { log, run, runSealed, runAgentCode, REVIEW_CONTROL_SECRET } = require('./exec');
const { setupWorktree, cleanupWorktree } = require('./worktree');

// A check whose COMMAND was never found did not fail -- it did not run, and
// nothing was learned about the code. Telling those apart matters because the
// two need opposite responses: a failing check is the agent's to fix, a
// missing tool is the harness's, and asking an agent to fix the second is
// asking it to debug an environment it cannot see. It will try anyway, which
// is how a correct commit gets rejected round after round.
//
// Matches a SHELL failing to locate an executable ("sh: 1: eslint: not found",
// "prettier: command not found"), never a module-resolution error from inside
// a program ("Cannot find module './x'") -- that one is usually a real defect
// in the code under review and must stay the agent's problem.
const MISSING_TOOL_RE =
  /(?:\b(?:sh|bash|zsh|dash)(?::\s*\d+)?:\s*\S+:\s*not found)|(?:\S+:\s*command not found)/i;

// A check that could not WRITE where it needed to. EROFS is the kernel's
// "read-only file system", and nothing in the commit under review can cause
// it -- it means the review environment handed the tool a read-only path. It
// is here for the same reason as a missing tool: otherwise the base fails
// identically, the failure is filed as pre-existing, and the gate never ran
// the check on either commit. That is exactly what `vite build` did on
// 2026-09-22 until it was caught by reading the output rather than the label.
const READ_ONLY_FS_RE = /\bEROFS\b|read-only file system/i;

// Pure, and exported for tests: marks each FAILED check whose output says its
// own command was missing or its filesystem read-only. Leaves passing checks
// alone -- output on a passing check may quote anything.
function classifyInfrastructureFailures(checkResults) {
  for (const c of checkResults) {
    if (c.ok) continue;
    const out = c.output || '';
    // "not found" is the shell's word only when the shell also exited 126
    // or 127; a test that prints the phrase and fails with 1 is the agent's
    // failure (2026-09-29 audit, R9). A row with no code (older callers,
    // the host path) keeps the sniff.
    const shellSaid = typeof c.code !== 'number' || c.code === 126 || c.code === 127;
    if ((shellSaid && MISSING_TOOL_RE.test(out)) || READ_ONLY_FS_RE.test(out)) c.infrastructure = true;
  }
  return checkResults;
}

const GITLEAKS_BIN = path.join(__dirname, 'bin', 'gitleaks');

// A baseline result is only an answer about the environment it was measured
// in, so the cache key names that environment as well as the commit.
function baselineKey(base, depsChanged) {
  return `${base}:${depsChanged ? 'install' : 'linked'}`;
}

async function runChecks(cfg, worktreePath) {
  const results = [];
  // `|| []`: a brand-new project has no review.checks yet (its first merge is
  // what triggers detection), and iterating undefined threw a TypeError out
  // of reviewProject -- "review failed with an internal error", no verdict,
  // and the agent's wait_for_review timed out.
  for (const check of cfg.checks || []) {
    log(`  running ${check.name} (${check.cmd} ${check.args.join(' ')}) in ${check.dir}`);
    // A check may declare its own budget; test:review runs 50 suites and
    // needs more than run()'s 5-minute default.
    // audit C-2: sealed env -- these run agent-authored code, always in a
    // sandbox container: started here on a host install, by the agent in
    // the bundle. See runAgentCode and SECURITY.md.
    const startedAt = Date.now();
    const r = await runAgentCode(cfg, worktreePath, check.dir, check.cmd, check.args,
                                 check.timeoutMs, check.env, check.network,
                                 check.stack || cfg.stack);
    log(`  ${check.name}: ${r.ok ? 'passed' : (r.infrastructure || r.missingTool ? 'could not run' : 'failed')} in ${Math.round((Date.now() - startedAt) / 1000)}s`);
    results.push({
      name: check.name, ok: r.ok, output: r.output.slice(-4000), code: r.code,
      // Set by runAgentCode when the check could not be RUN -- a missing
      // toolchain or an uncontainable host. It is not the agent's problem
      // and must not be handed to it as one.
      ...(r.infrastructure || r.missingTool ? { infrastructure: true } : {}),
    });
  }
  return results;
}

// A check that fails on the branch AND fails identically on the base commit is
// not this change's fault. Seen live 2026-09-09 (storefront, multi-category
// products): `pnpm audit` reports 14 high vulnerabilities in nodemailer/multer
// on main itself, the diff touched no package.json, Sonnet wrote "no blocking
// issues, pre-existing audit failure" -- and the harness forced NEEDS_FIXES
// on the failed check anyway, round after round, until escalation. True
// verdict, wrong cause, infinite-loop shape (packDiff's comment describes
// the same shape). The gate is for what the DIFF breaks.
//
// Only cfg.checks commands are compared (build/db/secrets are not re-run on
// base). Results are cached in state.json per base sha, so a slow suite is
// re-run on base once per base, not once per round.
// What a failing check's output names: `[review-tests] test:x ... FAIL` lines
// and the test files in its stack traces. Read from the BASE run's output as
// well as the branch's, because "fails on base" is only an excuse for the
// failures the base actually has (see applyBaseline).
function failureMarkers(output) {
  const text = String(output || '');
  const markers = new Set();
  for (const m of text.matchAll(/\b(test:[\w./-]+)\s*\.\.\.\s*FAIL/g)) markers.add(m[1]);
  for (const m of text.matchAll(/(?:\/workspace\/)?((?:tests?|spec|__tests__)\/[\w./-]+\.\w+)(?::\d+)?/g)) markers.add(m[1]);
  return [...markers];
}

// A baseline entry is `{ ok, markers }`. A bare boolean is a cache written
// before 2026-10-22: it knows the base failed but not what failed, so
// `markers` is null -- the check stays pre-existing as it was, and no finding
// is excused by it.
function _baseEntry(entry) {
  if (entry && typeof entry === 'object') return { ok: entry.ok !== false, markers: Array.isArray(entry.markers) ? entry.markers : [] };
  return { ok: entry !== false, markers: null };
}

function applyBaseline(checkResults, baselineForBase) {
  // Pure: marks each failed check whose baseline entry is a recorded FAILURE
  // as pre-existing. Returns the same objects, mutated, for the caller.
  //
  // "Red on base" is not "red for the same reason". 2026-10-22 audit, R15: a
  // flake on main made the whole `test` check pre-existing, the branch's new
  // failing test file then excused the model's blocking finding about it, and
  // the commit came back READY. So the base's failure markers ride along as
  // `baseMarkers` (reclassifyPreexisting excuses only those), and a branch
  // that fails a named test the base does not (`test:x ... FAIL`) is not
  // pre-existing at all.
  //
  // NEVER an infrastructure failure. A check whose command was missing failed
  // on the base for the same reason it failed on the branch -- the tool is
  // not installed -- so "it also fails on base" says nothing about the code.
  // It means the gate ran the check on NEITHER commit.
  //
  // Treating it as pre-existing is what turned the review into a rubber stamp
  // on 2026-09-22: a project whose three apps each keep their own
  // node_modules had none installed in the review worktree, so lint, build
  // and test all failed with `oxlint: not found` / `vite: not found`, the base
  // failed identically, every one was marked pre-existing, and every commit
  // came back "READY -- no issues found" having run nothing at all. The
  // infrastructure escalation in reviewProject existed for exactly this and
  // never fired, because it filters on `!c.preexisting`.
  for (const c of checkResults) {
    if (c.infrastructure) continue;
    if (c.ok || !baselineForBase || !Object.hasOwn(baselineForBase, c.name)) continue;
    const base = _baseEntry(baselineForBase[c.name]);
    if (base.ok) continue;
    const baseFailing = new Set(base.markers || []);
    const newFailing = base.markers === null ? []
      : failureMarkers(c.output).filter((m) => m.startsWith('test:') && !baseFailing.has(m));
    if (newFailing.length) {
      c.output = `NOT PRE-EXISTING: the base commit fails this check too, but not ${newFailing.join(', ')}; those fail only on this branch.\n${c.output || ''}`;
      continue;
    }
    c.preexisting = true;
    c.baseMarkers = base.markers || [];
  }
  // When EVERY compared check fails on the base commit as well, the base is
  // not "a project whose suite is red": it is a review environment that
  // cannot run this project at all. Counting that as pre-existing passed a
  // commit with nothing checked (2026-09-29: a dangling link to generated
  // code failed typecheck, lint, tests and coverage on both commits alike).
  const compared = checkResults.filter((c) => !c.infrastructure && baselineForBase && Object.hasOwn(baselineForBase, c.name));
  // Two or more all red on both commits, or the only configured check red
  // on both with nothing else passing (2026-09-29 audit, R7: a project with
  // one check was left unguarded by the count alone).
  // A genuine failure that main already has stays pre-existing while some
  // other check still verified the commit; the only check red on both
  // commits verified nothing, whatever broke it, and says so.
  const nothingPassed = !checkResults.some((c) => !c.infrastructure && c.ok);
  if (compared.length >= 1 && compared.every((c) => !c.ok && c.preexisting) && (compared.length >= 2 || nothingPassed)) {
    const why = compared.length >= 2
      ? 'every check fails on the base commit as well, so the review environment cannot run this project'
      : 'the only configured check fails on the base commit as well, so no check verified this commit';
    for (const c of compared) {
      c.preexisting = false;
      c.infrastructure = true;
      c.output = `SETUP: ${why}; nothing is known about the code either way.\n${c.output || ''}`;
    }
  }
  return checkResults;
}

async function markPreexistingFailures(project, cfg, base, checkResults, prevBaseline, branchDepsChanged = null) {
  const eligible = new Set((cfg.checks || []).map((c) => c.name));
  const failed = checkResults.filter((c) => !c.ok && eligible.has(c.name));
  const baseline = { ...(prevBaseline || {}) };
  // Keyed by base sha AND provisioning mode, never by sha alone. The two modes
  // produce genuinely different environments, so a result cached from one is
  // not an answer about the other -- caching across them is the same
  // apples-to-oranges comparison this function exists to prevent.
  const baseKey = baselineKey(base, branchDepsChanged);
  const forBase = { ...(baseline[baseKey] || {}) };
  const missing = failed.filter((c) => !(c.name in forBase));
  if (missing.length) {
    log(`[${project}] ${missing.map((c) => c.name).join(', ')} failed on the branch -- checking the base commit ${base.slice(0, 12)} for a pre-existing failure`);
    let basePath = null;
    try {
      // Provision the base EXACTLY as the branch was provisioned. Without
      // this the comparison is meaningless: a base worktree diffs against
      // itself, so depsChanged is always false there, and any check that only
      // fails under the install path looked branch-specific no matter what.
      // That is not hypothetical -- it blocked a correct commit for three
      // rounds until the circuit breaker escalated it, because the base run
      // inherited tooling from the live checkout that the branch run had to
      // install for itself.
      ({ worktreePath: basePath } = await setupWorktree(project, cfg, base, base, { depsChangedOverride: branchDepsChanged }));
      const only = { ...cfg, checks: cfg.checks.filter((c) => missing.some((m) => m.name === c.name)) };
      // The output's markers, not just the boolean: what the base fails is
      // what a branch failure may be excused by (applyBaseline).
      for (const r of await runChecks(only, basePath)) forBase[r.name] = { ok: r.ok, markers: r.ok ? [] : failureMarkers(r.output) };
    } catch (err) {
      log(`[${project}] baseline check failed to run (treating failures as this change's): ${err.message}`);
    } finally {
      if (basePath) await cleanupWorktree(cfg, basePath).catch(() => {});
    }
  }
  baseline[baseKey] = forBase;
  applyBaseline(checkResults, forBase);
  const pre = checkResults.filter((c) => c.preexisting).map((c) => c.name);
  if (pre.length) log(`[${project}] pre-existing failing checks (also fail on base): ${pre.join(', ')}`);
  return baseline;
}

// The build itself, then the project's post-build assertions -- each exists
// because it caught a real incident (see buildCheck.assertions in
// builtin-projects.local.js.example), not just "did the build not crash".
async function runBuildCheck(cfg, worktreePath) {
  const bc = cfg.buildCheck;
  if (!bc) return [];
  const results = [];
  log(`  running build (${bc.cmd} ${bc.args.join(' ')}) in ${bc.dir}`);
  // audit C-2: sealed env -- the build runs agent-authored code, and is
  // contained for the same reason the checks are.
  const build = await runAgentCode(cfg, worktreePath, bc.dir, bc.cmd, bc.args,
                                   300_000, bc.env, bc.network, bc.stack || cfg.stack);
  results.push({ name: 'build', ok: build.ok, output: build.output.slice(-4000), code: build.code });
  if (!build.ok) return results; // assertions need the build to have actually produced output

  for (const a of bc.assertions) {
    if (a.kind === 'file-exists') {
      const exists = fs.existsSync(path.join(worktreePath, a.file));
      results.push({ name: a.name, ok: exists, output: exists ? '' : `expected ${a.file} to exist` });
      continue;
    }
    log(`  running ${a.name} (${a.cmd} ${a.args.join(' ')}) in ${a.dir}`);
    const r = await runAgentCode(cfg, worktreePath, a.dir, a.cmd, a.args, 60_000, undefined, a.network);
    results.push({ name: a.name, ok: r.ok, output: r.output.slice(-4000), code: r.code });
  }
  return results;
}

// Mirrors ci.yml's `database` job (migrations+drift, seed, e2e) — same
// commands, but against a throwaway database on the existing local Postgres
// rather than a fresh container, since that's what's actually available
// here. Never touches the live database: a brand-new DB is created for this
// run alone and dropped in the `finally`, regardless of outcome. Connection
// details (host/port/user/password) are read from the worktree's own copied
// .env -- the review-only credentials secretFiles provides -- with only the
// database name swapped for a throwaway one.
async function runDatabaseCheck(cfg, worktreePath) {
  const dc = cfg.databaseCheck;
  if (!dc) return [];
  if (sandbox.IN_CONTAINER) {
    // Never in this container: it holds the merge secret. The agent runs
    // them in the checks container against the bundle's throwaway
    // checks-postgres and checks-redis, on a network that reaches nothing
    // else (2026-09-27). Without an agent to ask, or a bundle without those
    // services, the agent's 503 comes back as the refusal it always was.
    if (!sandbox.DELEGATE_URL) {
      return [{
        name: 'db-setup', ok: false, infrastructure: true,
        output: 'SETUP: the database checks run the project\'s code and cannot be sandboxed here: '
              + 'AGENT_SANDBOX_URL is unset, so there is no agent to start the checks container. '
              + 'Nothing about the code under review is known either way.',
      }];
    }
    log(`  delegating the database checks (drift, seed, e2e) in ${dc.apiDir} to the agent`);
    return sandbox.runDelegatedDatabaseCheck(cfg, worktreePath, cfg.stack, { secret: REVIEW_CONTROL_SECRET });
  }
  const apiDir = path.join(worktreePath, dc.apiDir);
  const envPath = path.join(apiDir, '.env');
  let baseUrl;
  try {
    const envText = fs.readFileSync(envPath, 'utf8');
    const m = envText.match(/^DATABASE_URL\s*=\s*"?([^"\n]+)"?/m);
    if (!m) throw new Error('DATABASE_URL not found in apps/api/.env');
    baseUrl = m[1];
  } catch (err) {
    return [{ name: 'db-setup', ok: false, output: `could not read DATABASE_URL: ${err.message}` }];
  }
  const parsed = baseUrl.match(/^postgresql:\/\/([^:]+):([^@]+)@([^:/]+):(\d+)\/([^?]+)/);
  if (!parsed) return [{ name: 'db-setup', ok: false, output: `could not parse DATABASE_URL` }];
  const [, dbUser, dbPass, dbHost, dbPort] = parsed;
  const throwawayDb = `tektonix_ci_review_${crypto.randomBytes(4).toString('hex')}`;
  // psql/libpq doesn't understand Prisma's ?schema= query param, so the
  // admin URL used for CREATE/DROP DATABASE omits it; the app-facing
  // throwaway URL (passed to pnpm db:drift/db:seed/test:e2e below, which go
  // through Prisma) keeps it.
  // audit M-25: no password in the admin URL -- it would be visible in
  // /proc/<pid>/cmdline to every local user. PGPASSWORD (set on the psql env
  // below) is sufficient for libpq.
  const adminUrl = `postgresql://${dbUser}@${dbHost}:${dbPort}/postgres`;
  const throwawayUrl = `postgresql://${dbUser}:${dbPass}@${dbHost}:${dbPort}/${throwawayDb}?schema=public`;
  const env = {
    DATABASE_URL: throwawayUrl,
    REDIS_URL: 'redis://localhost:6379/15',
    JWT_ACCESS_SECRET: crypto.randomBytes(32).toString('hex'),
    SECRETS_ENCRYPTION_KEY: crypto.randomBytes(32).toString('hex'),
    CORS_ORIGIN_STOREFRONT: 'http://localhost:5173',
    CORS_ORIGIN_ADMIN: 'http://localhost:5174',
    ORDER_NOTIFY_EMAIL: 'orders@example.test',
  };

  const results = [];
  // Sealed too, though psql is not agent-authored: it costs nothing, and it
  // means no child of this function sees the reviewer's own variables.
  const psql = (sql) => runSealed('psql', [adminUrl, '-c', sql], '/', 30_000, { PGPASSWORD: dbPass });
  try {
    const create = await psql(`CREATE DATABASE ${throwawayDb};`);
    if (!create.ok) return [{ name: 'db-setup', ok: false, output: create.output.slice(-2000) }];
    // Sealed like everything else this function starts, though redis-cli is
    // not agent-authored: the rule is simpler to hold as "no child of this
    // function sees the reviewer's environment" than as a list of exceptions.
    await runSealed('redis-cli', ['-n', '15', 'flushdb'], '/', 10_000);

    // THE ONE PLACE agent-authored code still runs on the host, and it is
    // deliberate rather than missed. These three talk to Postgres and Redis
    // on this machine's loopback: inside a container "localhost" is the
    // container, so containing them means either --network host, which is
    // not containment, or rewriting each project's DSN to the bridge gateway
    // and opening those services to it. Both trade a real, working review
    // for a weaker boundary than the one they would buy.
    //
    // What bounds it instead: the commands come from projects.json, which
    // the agent cannot write; the database is a throwaway created and
    // dropped around them; and the env is sealedEnv() plus the throwaway
    // DSN, Redis URL and generated secrets above -- runSealed, never run().
    // Until 2026-09-23 these went through run(), which spreads process.env
    // underneath, while this comment and SECURITY.md both said the env was
    // built rather than inherited: agent-authored scripts could read the
    // reviewer's router key and control secret. tests/test_db_check_env.py
    // now plants both and requires they do not arrive. What is NOT bounded
    // is the code those commands execute, which is the repository under
    // review. SECURITY.md says so plainly.
    log(`  running db-drift (pnpm db:drift) in ${dc.apiDir}`);
    const drift = await runSealed(dc.driftCmd.cmd, dc.driftCmd.args, apiDir, 120_000, env);
    results.push({ name: 'db-drift', ok: drift.ok, output: drift.output.slice(-4000), code: drift.code });
    if (!drift.ok) return results; // seed/e2e need a migrated, non-drifted schema

    log(`  running db-seed (pnpm db:seed) in ${dc.apiDir}`);
    const seed = await runSealed(dc.seedCmd.cmd, dc.seedCmd.args, apiDir, 60_000, env);
    results.push({ name: 'db-seed', ok: seed.ok, output: seed.output.slice(-4000), code: seed.code });
    if (!seed.ok) return results;

    log(`  running e2e (pnpm test:e2e) in ${dc.apiDir}`);
    const e2e = await runSealed(dc.e2eCmd.cmd, dc.e2eCmd.args, apiDir, 300_000, env);
    results.push({ name: 'e2e', ok: e2e.ok, output: e2e.output.slice(-4000), code: e2e.code });
    return results;
  } finally {
    // Terminate any lingering connections before dropping — a leaked
    // connection from a crashed/timed-out test run would otherwise make
    // DROP DATABASE hang or fail, leaking the throwaway DB permanently.
    await psql(`SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${throwawayDb}' AND pid <> pg_backend_pid();`);
    const drop = await psql(`DROP DATABASE IF EXISTS ${throwawayDb};`);
    if (!drop.ok) log(`  WARN: failed to drop throwaway DB ${throwawayDb} (leaked): ${drop.output.slice(-300)}`);
  }
}

// Mirrors ci.yml's `secrets` job: gitleaks over the full worktree history
// (--exit-code 1 means "leaks found", not a script error, so ok tracks that
// specifically rather than the generic exit-code-0 convention).
async function runSecretScan(cfg, worktreePath) {
  if (!cfg.secretScan) return [];
  if (!fs.existsSync(GITLEAKS_BIN)) {
    return [{ name: 'secrets', ok: false, output: `gitleaks binary not found at ${GITLEAKS_BIN}` }];
  }
  log(`  running secrets (gitleaks) in worktree`);
  const r = await run(GITLEAKS_BIN, ['git', '--no-banner', '--redact', '--exit-code', '1'], worktreePath, 60_000);
  return [{ name: 'secrets', ok: r.ok, output: r.output.slice(-4000) }];
}

module.exports = {
  classifyInfrastructureFailures, baselineKey, runChecks, applyBaseline, markPreexistingFailures, failureMarkers,
  runBuildCheck, runDatabaseCheck, runSecretScan,
};
