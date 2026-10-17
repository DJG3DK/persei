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

const STUDIO = '[review-tests] test:studio-paper ... ok\n[review-tests] test:studio-agent ... FAIL\n'
  + '  operator: match\n  stack: |-\n    TestContext.<anonymous> (/workspace/tests/test_studio_agent.js:80:16)\n'
  + '[review-tests] FAILED: test:studio-agent';

test('a blocking finding about a pre-existing failure is kept as a diagnosis, not a block', () => {
  const review = normalizeReview({ verdict: 'NEEDS_FIXES', summary: 's', findings: [
    { severity: 'blocking', file: 'src/studio/agent.js', issue: 'test:studio-agent fails at test_studio_agent.js:80; the safeThreadId change is the prime suspect' },
    { severity: 'blocking', file: 'src/studio/jobs.js', issue: 'a real bug: the write path is not checked' },
  ] });
  const moved = reclassifyPreexisting(review, [{ name: 'lint', ok: true }, { name: 'test', ok: false, preexisting: true, output: STUDIO }]);
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
