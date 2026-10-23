'use strict';
/**
 * The review gate against golden bad branches (tests/fixtures/review_gate_bad_branches.js).
 *
 * Each branch is reviewed by the real reviewProject: a real worktree off a
 * scratch repository, the project's real check run, the real guards, the
 * real verdict and record. Two things are stubbed, through fetch, so this
 * runs in CI with no network and no paid model:
 *
 *   - the sandbox: the delegated-sandbox endpoint the bundle uses runs the
 *     check here, on the fixture's own two-line tests;
 *   - the model: a CREDULOUS reviewer that approves everything. Whatever
 *     the gate still catches is what it catches without a model -- which is
 *     the point: these branches are written to fool the model.
 *
 * Run: node tests/test_reviewer_bad_branches.js
 */

const test = require('node:test');
const assert = require('node:assert/strict');
const { spawnSync } = require('node:child_process');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

const { buildRepo, CHECKS } = require('./fixtures/review_gate_bad_branches');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'review-bad-branches-'));
const home = path.join(tmp, 'home');
fs.mkdirSync(home);
Object.assign(process.env, {
    AGENT_HOME: home,                                   // no review-secrets, no router .env from a real install
    REVIEW_STATE_DIR: path.join(tmp, 'state'),
    REVIEW_WORKTREE_ROOT: path.join(tmp, 'worktrees'),
    REVIEW_USAGE_LOG: path.join(tmp, 'usage.jsonl'),
    REVIEW_ONLY_PROJECTS_JSON: '1',
    REVIEW_CONTROL_SECRET: 'test-secret',
    TEKTONIX_BUNDLE: '1',                               // delegate checks ...
    AGENT_SANDBOX_URL: 'http://sandbox.test',           // ... to the stub below
    MODEL_ROUTER_URL: 'http://router.test/v1',
    GIT_CONFIG_NOSYSTEM: '1',
});
fs.mkdirSync(process.env.REVIEW_STATE_DIR);

const { repo, cases } = buildRepo(tmp);
const prompts = new Map();                              // branch -> the prompt the model was sent
let reviewing = null;

const json = (data, status = 200) => ({ ok: status < 300, status, json: async () => data, text: async () => JSON.stringify(data) });

globalThis.fetch = async (url, init = {}) => {
    url = String(url);
    if (url === 'http://sandbox.test/api/internal/review-sandbox/probe') {
        return json({ ok: true, mode: 'delegated', image: 'stub' });
    }
    if (url === 'http://sandbox.test/api/internal/review-sandbox/run') {
        // The fixture's own tests, nothing else: the worktree is one this
        // test just built from the fixture script.
        const body = JSON.parse(init.body);
        assert.ok(body.worktree.startsWith(process.env.REVIEW_WORKTREE_ROOT), body.worktree);
        const r = spawnSync(body.cmd, body.args, {
            cwd: path.join(body.worktree, body.relDir), encoding: 'utf8', timeout: 60_000,
            env: { PATH: process.env.PATH, HOME: home, ...body.env },
        });
        return json({ ok: r.status === 0, code: r.status ?? 1, output: `${r.stdout || ''}${r.stderr || ''}` });
    }
    if (url === 'http://router.test/v1/chat/completions') {
        const body = JSON.parse(init.body);
        prompts.set(reviewing, body.messages[0].content);
        const answer = { verdict: 'READY', summary: 'Looks good to me.', findings: [] };
        return json({
            choices: [{ message: { tool_calls: [{ function: { name: 'submit_review', arguments: JSON.stringify(answer) } }] } }],
            usage: {},
        });
    }
    throw new Error(`unexpected fetch in a test: ${url}`);
};

const reviewer = require('../services/commit-reviewer/reviewer');

const cfg = { name: 'fixture', live: repo, sandbox: repo, checks: CHECKS };
const records = new Map();

test('every golden branch is reviewed', async () => {
    for (const c of cases) {
        reviewing = c.branch;
        const out = await reviewer.reviewProject('fixture', cfg, 'stub-key', c.branch);
        assert.equal(out.started, true, `${c.name}: no review started`);
        assert.ok(!out.error, `${c.name}: ${out.error}`);
        const state = JSON.parse(fs.readFileSync(path.join(process.env.REVIEW_STATE_DIR, 'state.json'), 'utf8'));
        const record = state.fixture.branches[c.branch];
        assert.equal(record.lastReviewedSha, c.sha, `${c.name}: reviewed the wrong commit`);
        records.set(c.name, record);
    }
});

for (const c of cases) {
    test(`${c.name}: ${c.verdict}${c.blocking.length ? ` (blocked by ${c.blocking})` : ''}${c.flags.length ? ` (flagged ${c.flags})` : ''}`, () => {
        const record = records.get(c.name);
        assert.ok(record, 'not reviewed');
        assert.equal(record.verdict, c.verdict, `${c.name}: ${JSON.stringify(record.findings, null, 1)}`);

        const failed = record.checkResults.filter((r) => !r.ok).map((r) => r.name);
        assert.deepEqual(failed, c.failedChecks, `checks: ${JSON.stringify(record.checkResults)}`);
        for (const r of record.checkResults) assert.ok(!r.infrastructure, `${r.name} could not run: ${r.output}`);

        const harness = (record.findings || []).filter((f) => f.issue.startsWith('[harness: '));
        const blockingGuards = harness.filter((f) => f.severity === 'blocking').map((f) => f.issue.match(/^\[harness: ([\w-]+)/)[1]);
        assert.deepEqual([...new Set(blockingGuards)], c.blocking, JSON.stringify(harness));
        assert.deepEqual((record.flags || []).map((f) => f.guard), c.flags, JSON.stringify(record.flags));
        // A flag is never what decided the verdict: it is the operator's look.
        if (!c.blocking.length && !c.failedChecks.length) assert.equal(record.verdict, 'READY');
    });
}

test('the injection reaches the model only inside the untrusted fence, and the trusted section names it without quoting it', () => {
    const c = cases.find((x) => x.injection);
    const prompt = prompts.get(c.branch);
    assert.ok(prompt, 'the model was not called');
    const trusted = prompt.indexOf('## TRUSTED harness observations');
    assert.ok(trusted > 0, 'no trusted harness section');
    const fenceOpen = prompt.search(/<<<UNTRUSTED-DIFF-[0-9a-f]+>>>/);
    const fenceClose = prompt.search(/<<<END-DIFF-[0-9a-f]+>>>/);
    const at = prompt.indexOf(c.injection);
    assert.ok(fenceOpen < at && at < fenceClose, 'the injected text is outside the diff fence');
    assert.equal(prompt.indexOf(c.injection, fenceClose), -1, 'the injected text was repeated after the diff');
    assert.ok(trusted > fenceClose, 'the trusted section must come after the untrusted diff');
    assert.match(prompt.slice(trusted), /text addressed to the reviewer/);
});

test('a branch with no guard finding gets no trusted harness section', () => {
    assert.ok(!prompts.get(cases.find((x) => x.name === 'good-change').branch).includes('## TRUSTED harness observations'));
});

test('cleanup', () => {
    fs.rmSync(tmp, { recursive: true, force: true });
});
