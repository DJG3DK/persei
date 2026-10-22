// A check that fails on the branch AND on the base commit is not the diff's
// fault: it must not force NEEDS_FIXES, and the agent must be told not to
// chase it (2026-09-09: pnpm audit's 14 high vulns on main looped a storefront
// task at the gate).
const test = require('node:test');
const assert = require('node:assert/strict');
const { applyBaseline, buildAgentMessage } = require('../services/commit-reviewer/reviewer.js');

test('applyBaseline marks only failures that also fail on base', () => {
  const results = [
    { name: 'lint', ok: true },
    { name: 'audit', ok: false, output: '14 high' },
    { name: 'unit-tests', ok: false, output: '1 failing' },
  ];
  applyBaseline(results, { audit: false, 'unit-tests': true });
  assert.equal(results[1].preexisting, true, 'audit fails on base too');
  assert.equal(results[2].preexisting, undefined, 'unit-tests pass on base, so this failure is the diff\'s');
  assert.equal(results[0].preexisting, undefined);
});

test('applyBaseline with no baseline leaves everything blocking', () => {
  const results = [{ name: 'audit', ok: false }];
  applyBaseline(results, undefined);
  assert.equal(results[0].preexisting, undefined);
});

test('the agent message separates pre-existing failures and asks for a fix only when it is small', () => {
  const review = { verdict: 'NEEDS_FIXES', summary: 'One real problem.', findings: [{ severity: 'blocking', file: 'a.ts', issue: 'null deref' }] };
  const msg = buildAgentMessage(review, [
    { name: 'audit', ok: false, preexisting: true },
    { name: 'unit-tests', ok: false },
  ]);
  assert.match(msg, /Failed checks: unit-tests/);
  assert.doesNotMatch(msg, /Failed checks: [^\n]*audit/);
  assert.match(msg, /Pre-existing failing checks: audit/);
  assert.match(msg, /NOT counted against this change and do not block it/);
  assert.match(msg, /only if the fix is small and contained/);
  assert.match(msg, /project-wide audit backlog, say\), leave it/);
});

test('mechanical failure is derived from non-pre-existing checks only', () => {
  const checks = [{ name: 'audit', ok: false, preexisting: true }, { name: 'lint', ok: true }];
  const mechanicalFailed = checks.some((c) => !c.ok && !c.preexisting);
  assert.equal(mechanicalFailed, false);
});


test('every compared check failing on the base too is the environment, not a pre-existing red suite', () => {
    const { applyBaseline } = require('../services/commit-reviewer/reviewer.js');
    const all = [{ name: 'typecheck', ok: false, output: 'TS2307' }, { name: 'lint', ok: false, output: 'x' }, { name: 'secrets', ok: true }];
    applyBaseline(all, { typecheck: false, lint: false });
    assert.ok(all[0].infrastructure && all[1].infrastructure, 'both flagged as the harness');
    assert.ok(!all[0].preexisting && /every check fails on the base/.test(all[0].output));
    const some = [{ name: 'typecheck', ok: false, output: 'TS2307' }, { name: 'lint', ok: true }];
    applyBaseline(some, { typecheck: false, lint: true });
    assert.ok(some[0].preexisting && !some[0].infrastructure, 'one red check on both is genuinely pre-existing');
});


test('a single configured check failing on both commits is the environment, not pre-existing', () => {
    const { applyBaseline } = require('../services/commit-reviewer/reviewer.js');
    const only = [{ name: 'test', ok: false, output: 'vitest: not found' }];
    applyBaseline(only, { test: false });
    assert.ok(only[0].infrastructure && !only[0].preexisting, JSON.stringify(only));
    // One red check beside a passing one on both commits stays pre-existing.
    const beside = [{ name: 'audit', ok: false, output: 'x' }, { name: 'test', ok: true }];
    applyBaseline(beside, { audit: false, test: true });
    assert.ok(beside[0].preexisting && !beside[0].infrastructure);
});


// 2026-10-17: the model blocked three rounds on a test the harness had marked
// pre-existing, and the agent, told not to fix it, could only argue.
const { normalizeReview, reclassifyPreexisting } = require('../services/commit-reviewer/prompt.js');
const { failureMarkers } = require('../services/commit-reviewer/checks.js');

const STUDIO = '[review-tests] test:studio-paper ... ok\n[review-tests] test:studio-agent ... FAIL\n'
  + '  operator: match\n  stack: |-\n    TestContext.<anonymous> (/workspace/tests/test_studio_agent.js:80:16)\n'
  + '[review-tests] FAILED: test:studio-agent';

test('a blocking finding about a pre-existing failure is kept as a diagnosis, not a block', () => {
  const review = normalizeReview({ verdict: 'NEEDS_FIXES', summary: 's', findings: [
    { severity: 'blocking', file: 'src/studio/agent.js', issue: 'test:studio-agent fails at test_studio_agent.js:80; the safeThreadId change is the prime suspect' },
    { severity: 'blocking', file: 'src/studio/jobs.js', issue: 'a real bug: the write path is not checked' },
  ] });
  const checks = [{ name: 'lint', ok: true }, { name: 'test', ok: false, output: STUDIO }];
  applyBaseline(checks, { lint: { ok: true, markers: [] }, test: { ok: false, markers: failureMarkers(STUDIO) } });
  const moved = reclassifyPreexisting(review, checks);
  assert.equal(moved, 1);
  assert.deepEqual(review.findings.map((f) => f.severity), ['preexisting', 'blocking'], 'the real bug still blocks');
});

test('nothing is moved when the failing check is the change\'s own', () => {
  const review = normalizeReview({ verdict: 'NEEDS_FIXES', summary: 's', findings: [
    { severity: 'blocking', file: 'src/studio/agent.js', issue: 'test:studio-agent fails at test_studio_agent.js:80' }] });
  assert.equal(reclassifyPreexisting(review, [{ name: 'test', ok: false, output: STUDIO }]), 0);
  assert.equal(review.findings[0].severity, 'blocking');
});

test('a bare check name is not enough to move a finding', () => {
  // "test" alone would match half the findings ever written.
  const review = normalizeReview({ verdict: 'NEEDS_FIXES', summary: 's', findings: [
    { severity: 'blocking', file: 'a.js', issue: 'this test is missing an edge case' }] });
  assert.equal(reclassifyPreexisting(review, [{ name: 'test', ok: false, preexisting: true, output: 'no markers here' }]), 0);
});

test('the reviewer can file a diagnosis, and the agent is shown it with the harness output', () => {
  const review = normalizeReview({ verdict: 'READY', summary: 'fine', findings: [
    { severity: 'Pre-existing', file: 'tests/test_studio_agent.js', issue: 'validate_code dry-runs on market data the harness does not have; give the test a fixture' }] });
  assert.equal(review.findings[0].severity, 'preexisting');
  const msg = buildAgentMessage(review, [{ name: 'test', ok: false, preexisting: true, output: STUDIO }]);
  assert.match(msg, /The reviewer's diagnosis \(tests\/test_studio_agent.js\): validate_code dry-runs/);
  assert.match(msg, /--- test \(pre-existing\) ---[\s\S]*test:studio-agent \.\.\. FAIL/);
  assert.match(msg, /"it passes for me"/);
  assert.doesNotMatch(msg, /Other findings/, 'a diagnosis is not filed as a minor finding');
});


// 2026-10-22 audit, R15: "red on base" excused whatever the BRANCH's output
// named, so a flake on main let a new failing test file through as READY.
const verdictOf = (review, checks) => (checks.some((c) => !c.ok && !c.preexisting)
  || review.findings.some((f) => f.severity === 'blocking') ? 'NEEDS_FIXES' : 'READY');

test('a failure only the branch has stays blocking when the base is red for another reason', () => {
  const baseOut = 'not ok 1 - flaky\n    at /workspace/tests/a.test.js:12:3';
  const branchOut = `${baseOut}\nnot ok 2 - charges twice\n    at /workspace/tests/new.test.js:7:5`;
  const checks = [{ name: 'lint', ok: true }, { name: 'test', ok: false, output: branchOut }];
  applyBaseline(checks, { test: { ok: false, markers: failureMarkers(baseOut) } });
  assert.equal(checks[1].preexisting, true, 'the check as a whole is red on main too');
  const review = normalizeReview({ verdict: 'NEEDS_FIXES', summary: 's', findings: [
    { severity: 'blocking', file: 'tests/new.test.js', issue: 'the new test fails: charge() runs twice' },
    { severity: 'blocking', file: 'tests/a.test.js', issue: 'tests/a.test.js fails' }] });
  assert.equal(reclassifyPreexisting(review, checks), 1, 'only the base\'s own failure is excused');
  assert.deepEqual(review.findings.map((f) => f.severity), ['blocking', 'preexisting']);
  assert.equal(verdictOf(review, checks), 'NEEDS_FIXES');
});

test('a branch-only `test:x ... FAIL` line downgrades nothing and un-excuses the check', () => {
  const baseOut = '[review-tests] test:flaky ... FAIL';
  const branchOut = `${baseOut}\n[review-tests] test:charge ... FAIL`;
  const checks = [{ name: 'lint', ok: true }, { name: 'test', ok: false, output: branchOut }];
  applyBaseline(checks, { test: { ok: false, markers: failureMarkers(baseOut) } });
  assert.ok(!checks[1].preexisting, 'a named test failing only here is this change\'s');
  assert.match(checks[1].output, /not test:charge/);
  const review = normalizeReview({ verdict: 'NEEDS_FIXES', summary: 's', findings: [
    { severity: 'blocking', file: 'src/charge.js', issue: 'test:charge fails' }] });
  assert.equal(reclassifyPreexisting(review, checks), 0);
  assert.equal(verdictOf(review, checks), 'NEEDS_FIXES');
});

test('paths printed only into the branch output excuse nothing', () => {
  const checks = [{ name: 'lint', ok: true }, { name: 'test', ok: false, output: 'see tests/unrelated.test.js and src/api/x.js' }];
  applyBaseline(checks, { test: { ok: false, markers: ['tests/a.test.js'] } });
  const review = normalizeReview({ verdict: 'NEEDS_FIXES', summary: 's', findings: [
    { severity: 'blocking', file: 'tests/unrelated.test.js', issue: 'wrong assertion' }] });
  assert.equal(reclassifyPreexisting(review, checks), 0);
});

test('a baseline cached as a bare boolean is still pre-existing but excuses no finding', () => {
  const checks = [{ name: 'lint', ok: true }, { name: 'test', ok: false, output: STUDIO }];
  applyBaseline(checks, { test: false });
  assert.equal(checks[1].preexisting, true);
  const review = normalizeReview({ verdict: 'NEEDS_FIXES', summary: 's', findings: [
    { severity: 'blocking', file: 'src/studio/agent.js', issue: 'test:studio-agent fails' }] });
  assert.equal(reclassifyPreexisting(review, checks), 0);
});
