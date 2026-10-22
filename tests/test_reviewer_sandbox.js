'use strict';
/**
 * Where agent-authored code is allowed to run.
 *
 * The reviewer executes each project's configured checks against the agent's
 * worktree. The agent's shell is sandboxed, but its write/edit tools put
 * files in that worktree -- so on a host install, where this service runs as
 * root, a test file the agent wrote was arbitrary code executing as root
 * outside any container. sealedEnv() stops secrets reaching those commands;
 * it does nothing about what they can do once running.
 *
 * These pin the three decisions that close it, because each one is the kind
 * that gets "simplified" later by someone who does not know what it was for:
 * fail closed rather than fall back, leave the bundle alone because it is
 * already contained, and mount what a check needs so containment does not
 * quietly turn every project red.
 *
 * Run: node tests/test_reviewer_sandbox.js
 */

const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');

const sandbox = require('../services/commit-reviewer/sandbox');

let passed = 0;
// A test that has to wait on a process pushes its promise here; the file
// exits non-zero if any of them rejects.
const pendingAsync = [];
function test(name, fn) {
    fn();
    passed++;
    console.log(`  ok  ${name}`);
}
process.on('beforeExit', () => {
    if (!pendingAsync.length) return;
    const waiting = pendingAsync.splice(0);
    Promise.all(waiting).then(() => console.log(`  ok  ${waiting.length} async assertion(s)`))
        .catch((err) => { console.error(`  FAIL ${err.stack}`); process.exitCode = 1; });
});

console.log('reviewer sandbox');

// --- what a check needs in order to behave as it does on the host ---------

function scratchProject() {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), 'revsbx-'));
    const live = path.join(root, 'live');
    const wt = path.join(root, 'work');
    fs.mkdirSync(path.join(live, 'node_modules', '.bin'), { recursive: true });
    fs.mkdirSync(path.join(live, '.git'), { recursive: true });
    fs.mkdirSync(wt, { recursive: true });
    return { root, live, wt };
}

function mountsOf(args) {
    const out = [];
    args.forEach((a, i) => { if (a === '-v') out.push(args[i + 1]); });
    return out;
}

test('the worktree is the working directory', () => {
    const { live, wt } = scratchProject();
    assert.ok(mountsOf(sandbox.mountArgs({ live }, wt)).includes(`${wt}:/workspace`));
});

test('a symlinked node_modules is mounted so the link resolves', () => {
    // The one that actually bit. A symlink is carried into the container
    // faithfully and then dangles, so `npx eslint` dies with "not found"
    // inside while passing on the host.
    const { live, wt } = scratchProject();
    fs.symlinkSync(path.join(live, 'node_modules'), path.join(wt, 'node_modules'));
    const mounts = mountsOf(sandbox.mountArgs({ live }, wt));
    assert.ok(mounts.some(m => m === `${fs.realpathSync(path.join(live, 'node_modules'))}:${fs.realpathSync(path.join(live, 'node_modules'))}:ro`),
        `no mount for the link target: ${mounts.join(' , ')}`);
});

test('a symlink pointing outside the project is refused', () => {
    // The worktree is agent-writable, so the link target is agent-chosen:
    // node_modules -> /root would otherwise mount /root into the container.
    const { root, live, wt } = scratchProject();
    const elsewhere = path.join(root, 'elsewhere');
    fs.mkdirSync(elsewhere, { recursive: true });
    fs.symlinkSync(elsewhere, path.join(wt, 'node_modules'));
    const mounts = mountsOf(sandbox.mountArgs({ live }, wt));
    assert.ok(!mounts.some(m => m.startsWith(elsewhere)), `mounted ${elsewhere}`);
});

test('a committed link named node_modules cannot mount the live checkout itself', () => {
    // 2026-09-29: "inside the live checkout" accepted the checkout, because a
    // directory is inside itself. `node_modules -> <live>` became
    // `-v <live>:<live>:ro`, and a check could read live's .env into its
    // output, the review, and the agent's feedback. The receiving side in
    // the bundle refused this already; a host install runs docker itself.
    const { live, wt } = scratchProject();
    fs.writeFileSync(path.join(live, '.env'), 'SECRET=1\n');
    fs.symlinkSync(live, path.join(wt, 'node_modules'));
    const specs = sandbox.mountSpecs({ live }, wt);
    assert.deepStrictEqual(specs, [], `mounted: ${JSON.stringify(specs)}`);
    assert.ok(!mountsOf(sandbox.mountArgs({ live }, wt)).some(m => m.startsWith(`${fs.realpathSync(live)}:`)));
});

test('a link into live that is not a node_modules tree is refused', () => {
    // live/backend holds source and configuration; only a dependency tree
    // is something a worktree may borrow through a link it wrote itself.
    const { live, wt } = scratchProject();
    fs.mkdirSync(path.join(live, 'backend'), { recursive: true });
    fs.symlinkSync(path.join(live, 'backend'), path.join(wt, 'node_modules'));
    assert.deepStrictEqual(sandbox.mountSpecs({ live }, wt), []);
});

test('a link to a nested node_modules inside live is the one shape that is mounted', () => {
    const { live, wt } = scratchProject();
    fs.mkdirSync(path.join(live, 'x', 'node_modules'), { recursive: true });
    fs.symlinkSync(path.join(live, 'x', 'node_modules'), path.join(wt, 'node_modules'));
    const specs = sandbox.mountSpecs({ live }, wt);
    const nm = fs.realpathSync(path.join(live, 'x', 'node_modules'));
    assert.deepStrictEqual(specs, [{ src: nm, dst: nm }]);
});

test('a generated-code link is mounted only when it points at live\'s copy of that directory', () => {
    // The generated loop accepted anything inside live too, so a committed
    // `generated -> <live>` (or `-> <live>/config`) mounted it read-only.
    const { live, wt } = scratchProject();
    fs.mkdirSync(path.join(live, 'generated', 'prisma'), { recursive: true });
    fs.mkdirSync(path.join(live, 'config'), { recursive: true });
    const cfg = { live, generated: [{ dir: 'generated', schemaFile: 'schema.prisma' }] };
    for (const wrong of [live, path.join(live, 'config'), path.join(live, 'generated', 'prisma')]) {
        fs.rmSync(path.join(wt, 'generated'), { force: true });
        fs.symlinkSync(wrong, path.join(wt, 'generated'));
        assert.deepStrictEqual(sandbox.mountSpecs(cfg, wt), [], `mounted ${wrong}`);
    }
    fs.rmSync(path.join(wt, 'generated'), { force: true });
    fs.symlinkSync(path.join(live, 'generated'), path.join(wt, 'generated'));
    const gen = fs.realpathSync(path.join(live, 'generated'));
    assert.deepStrictEqual(sandbox.mountSpecs(cfg, wt), [{ src: gen, dst: gen }]);
});

test('a configured node_modules dir whose live copy resolves outside live is refused', () => {
    // cfg.nodeModulesDirs mounts live/<rel>/node_modules by its real path.
    // Were that a link to the checkout, the root would be mounted whole;
    // were it a link into the agent's workspace template, a tree every task
    // shares by hardlink would be the review's tools.
    const { root, live, wt } = scratchProject();
    const tpl = path.join(root, 'tpl');
    fs.mkdirSync(path.join(tpl, 'node_modules'), { recursive: true });
    fs.mkdirSync(path.join(live, 'pkg'), { recursive: true });
    fs.symlinkSync(tpl, path.join(live, 'pkg', 'node_modules'));
    assert.deepStrictEqual(sandbox.mountSpecs({ live, sandbox: tpl, nodeModulesDirs: ['pkg'] }, wt), []);
    fs.rmSync(path.join(live, 'pkg', 'node_modules'));
    fs.symlinkSync(path.join(tpl, 'node_modules'), path.join(live, 'pkg', 'node_modules'));
    assert.deepStrictEqual(sandbox.mountSpecs({ live, sandbox: tpl, nodeModulesDirs: ['pkg'] }, wt), [],
        'the template is not a source, whatever live links to');
    // A real tree inside live is what this loop is for.
    fs.rmSync(path.join(live, 'pkg', 'node_modules'));
    fs.mkdirSync(path.join(live, 'pkg', 'node_modules'));
    const nm = fs.realpathSync(path.join(live, 'pkg', 'node_modules'));
    assert.deepStrictEqual(sandbox.mountSpecs({ live, nodeModulesDirs: ['pkg'] }, wt), [{ src: nm, dst: nm }]);
});

test('a real node_modules directory needs no mount of its own', () => {
    const { live, wt } = scratchProject();
    fs.mkdirSync(path.join(wt, 'node_modules'), { recursive: true });
    const mounts = mountsOf(sandbox.mountArgs({ live }, wt));
    assert.equal(mounts.filter(m => m.includes('node_modules')).length, 0,
        'a real directory is already inside the /workspace mount');
});

test('declared dependency and data dirs are bound read-only from live', () => {
    // A bind mount NESTED in the worktree is not carried into a container by
    // a plain bind of its parent, so these have to be named explicitly.
    const { live, wt } = scratchProject();
    fs.mkdirSync(path.join(live, 'data'), { recursive: true });
    const mounts = mountsOf(sandbox.mountArgs(
        { live, dependencyDirs: ['node_modules'], readOnlyMounts: ['data'] }, wt));
    assert.ok(mounts.includes(`${path.join(live, 'node_modules')}:/workspace/node_modules:ro`));
    assert.ok(mounts.includes(`${path.join(live, 'data')}:/workspace/data:ro`));
});

test("the live .git is mounted at its own path, since a worktree's .git is a pointer", () => {
    const { live, wt } = scratchProject();
    fs.writeFileSync(path.join(wt, '.git'), `gitdir: ${live}/.git/worktrees/work\n`);
    assert.ok(mountsOf(sandbox.mountArgs({ live }, wt))
        .includes(`${path.join(live, '.git')}:${path.join(live, '.git')}:ro`));
});

test('a pointer file naming someone else\'s .git is refused', () => {
    // Same reasoning as the symlink: the agent can rewrite this file.
    const { root, live, wt } = scratchProject();
    const other = path.join(root, 'other');
    fs.mkdirSync(path.join(other, '.git'), { recursive: true });
    fs.writeFileSync(path.join(wt, '.git'), `gitdir: ${other}/.git/worktrees/work\n`);
    const mounts = mountsOf(sandbox.mountArgs({ live }, wt));
    assert.ok(!mounts.some(m => m.startsWith(other)), `mounted ${other}`);
});

test('a directory that is not there is skipped rather than mounted empty', () => {
    const { live, wt } = scratchProject();
    const mounts = mountsOf(sandbox.mountArgs({ live, dependencyDirs: ['not-here'] }, wt));
    assert.ok(!mounts.some(m => m.includes('not-here')));
});

// --- the walk that finds those symlinks -----------------------------------

test('nested node_modules links are found, and the walk never descends into one', () => {
    const { live, wt } = scratchProject();
    fs.mkdirSync(path.join(wt, 'apps', 'api'), { recursive: true });
    fs.symlinkSync(path.join(live, 'node_modules'), path.join(wt, 'apps', 'api', 'node_modules'));
    const found = sandbox.nodeModulesLinks(wt);
    assert.deepStrictEqual(found, [path.join('apps', 'api', 'node_modules')]);
});

test('an unreadable directory does not abort the walk', () => {
    const { wt } = scratchProject();
    assert.deepStrictEqual(sandbox.nodeModulesLinks(path.join(wt, 'nope')), []);
});

test('isInside is not a string prefix test', () => {
    // /home/proj-old must not count as inside /home/proj.
    assert.ok(sandbox.isInside('/home/proj/x', '/home/proj'));
    assert.ok(sandbox.isInside('/home/proj', '/home/proj'));
    assert.ok(!sandbox.isInside('/home/proj-old/x', '/home/proj'));
    assert.ok(!sandbox.isInside('/home/other', '/home/proj'));
});

console.log(`\n${passed} passed`);

// --- per-stack images -----------------------------------------------------
//
// The sandbox image carries Node and Python. agent/provisioning.py detects
// and configures checks for Go, Rust, Ruby, Elixir, Java, PHP and .NET, none
// of which are in it -- so containing checks without this would have turned
// every review red for anyone whose project is not JavaScript.

test('a stack with its own image gets it, with the env that toolchain needs', () => {
    const go = sandbox.imageFor('go');
    assert.ok(go.known);
    assert.match(go.image, /golang/);
    // Not decoration: with no network and a read-only HOME, go writes its
    // cache somewhere or it does not run at all.
    assert.ok(go.env.GOCACHE, 'go needs a writable cache directory');
});

test('no stack means the default image, which is what every project had before', () => {
    const d = sandbox.imageFor(undefined);
    assert.equal(d.known, false);
    assert.equal(d.image, sandbox.IMAGE);
    assert.deepStrictEqual(d.env, {});
});

test('an unknown stack falls back rather than failing', () => {
    // The map can gain entries after a project's config was written.
    // Refusing to run a check because its label is new is worse than running
    // it where it probably works.
    const u = sandbox.imageFor('fortran');
    assert.equal(u.known, false);
    assert.equal(u.image, sandbox.IMAGE);
});

test('every stack in the map names an image and an env', () => {
    for (const [name, entry] of Object.entries(sandbox.STACKS.stacks || {})) {
        assert.ok(entry.image, `${name} has no image`);
        assert.ok(entry.env && typeof entry.env === 'object', `${name} has no env`);
    }
});

test('the map is the same one the Python side reads', () => {
    // Three callers, one list: this file, agent/provisioning.py's stamp at
    // detection, and scripts/verify_stack_checks.py's CI proof. A second
    // inventory is how the review dashboard ended up labelling live models
    // from a list deleted months earlier.
    const onDisk = JSON.parse(fs.readFileSync(
        path.join(__dirname, '..', 'docker', 'stack-images.json'), 'utf8'));
    assert.deepStrictEqual(Object.keys(sandbox.STACKS.stacks).sort(),
                           Object.keys(onDisk.stacks).sort());
});

test('a missing toolchain is recognised from docker\'s own words, where and how docker says them', () => {
    const dockerSays = 'docker: Error response from daemon: ... exec: "go": executable file not found in $PATH';
    assert.equal(sandbox.missingTool(dockerSays, 127), 'go');
    assert.equal(sandbox.missingTool(`Unable to find image locally\n${dockerSays}`, 126), 'go');
    // And not from a program that merely prints something similar --
    // mislabelling a real failure as a setup problem hides a genuine break.
    assert.equal(sandbox.missingTool('FAIL: expected "executable file not found in $PATH"', 1), null);
    assert.equal(sandbox.missingTool('2 tests failed', 1), null);
    assert.equal(sandbox.missingTool('', 127), null);
    // 2026-09-29: a test suite that quoted docker's sentence in its output,
    // failing normally, was filed as a setup problem and escalated. Docker's
    // complaint comes at exit 126/127, on the first lines, before any of
    // the check's own output; a check's own words come neither.
    assert.equal(sandbox.missingTool(dockerSays, 1), null, 'a normal failure is the code\'s, whatever it printed');
    assert.equal(sandbox.missingTool(`ran 40 tests\nFAIL: 1\nsee below\n${dockerSays}`, 127), null,
        'a sentence deep in the output is the check\'s, not docker\'s');
});

test('the host run sniffs docker\'s stderr, never the check\'s stdout', () => {
    // A fake docker on PATH: the sentence on stdout is the check's own words
    // (a container ran and its program printed them); on stderr, before
    // anything else, it is docker's.
    const dockerSays = 'docker: Error response from daemon: exec: "go": executable file not found in $PATH: unknown';
    const bin = fs.mkdtempSync(path.join(os.tmpdir(), 'fakedocker-'));
    const savedPath = process.env.PATH;
    process.env.PATH = `${bin}:${savedPath}`;
    const { live, wt } = scratchProject();
    const run = async (script) => {
        fs.writeFileSync(path.join(bin, 'docker'), `#!/bin/sh\n${script}\n`, { mode: 0o755 });
        return sandbox.runSandboxed({ live }, wt, '.', 'go', ['test'], 5_000, {}, 'none');
    };
    pendingAsync.push((async () => {
        try {
            const printed = await run(`echo '${dockerSays}'; exit 127`);
            assert.equal(printed.missingTool, undefined, `the check's stdout was taken for docker's: ${JSON.stringify(printed)}`);
            assert.equal(printed.ok, false);
            const docker = await run(`echo '${dockerSays}' >&2; exit 127`);
            assert.equal(docker.missingTool, 'go');
            assert.match(docker.output, /^SETUP: this check needs `go`/);
        } finally {
            process.env.PATH = savedPath;
            fs.rmSync(bin, { recursive: true, force: true });
        }
    })());
});

// --- the hardening itself -------------------------------------------------
//
// Asserted against the argv docker is actually given, not against the source.
// A grep passes for a flag that has been moved into a branch which never
// runs, or reordered so it applies to the wrong thing -- and the failure mode
// of losing one of these is silent: the checks still pass, in a container
// that no longer contains.

function argvFor(over = {}) {
    const { live, wt } = scratchProject();
    const { docker } = sandbox.dockerArgs(
        { live, ...(over.cfg || {}) }, wt, over.dir || '.',
        over.cmd || 'npm', over.args || ['test'],
        over.env || {}, over.network, over.stack);
    return docker;
}

/** The value following a flag, so order cannot be asserted by accident. */
function valueOf(argv, flag) {
    const i = argv.indexOf(flag);
    return i === -1 ? null : argv[i + 1];
}

test('every capability is dropped', () => {
    assert.equal(valueOf(argvFor(), '--cap-drop'), 'ALL');
});

test('privilege cannot be regained inside', () => {
    assert.equal(valueOf(argvFor(), '--security-opt'), 'no-new-privileges');
});

test('there is no network unless a check asked for one', () => {
    assert.equal(valueOf(argvFor(), '--network'), 'none');
    assert.equal(valueOf(argvFor({ network: 'bridge' }), '--network'), 'bridge');
    // Anything else is not a way to open the network by accident.
    assert.equal(valueOf(argvFor({ network: 'host' }), '--network'), 'none');
    assert.equal(valueOf(argvFor({ network: 'HOST' }), '--network'), 'none');
    assert.equal(valueOf(argvFor({ network: true }), '--network'), 'none');
});

test('the container is bounded and disposable', () => {
    const argv = argvFor();
    assert.ok(argv.includes('--rm'), 'a container that outlives its check is a leak');
    assert.equal(valueOf(argv, '--memory'), '2g');
    assert.equal(valueOf(argv, '--cpus'), '2');
    assert.equal(valueOf(argv, '--pids-limit'), '512');
});

test('the command is the entrypoint, and its arguments follow the image', () => {
    // Not `IMAGE cmd args`: this image inherits ENTRYPOINT
    // ["docker-entrypoint.sh"] from the Node base, which hands anything it
    // does not recognise to `node` -- so a missing tool came back as a Node
    // module stack trace instead of "executable file not found".
    const argv = argvFor({ cmd: 'go', args: ['vet', './...'] });
    assert.equal(valueOf(argv, '--entrypoint'), 'go');
    const image = argv.findIndex(a => a.includes('tektonix-sandbox'));
    assert.deepStrictEqual(argv.slice(image + 1), ['vet', './...'],
        'the check arguments must come after the image, as arguments to the entrypoint');
});

test('nothing is passed through a shell', () => {
    const argv = argvFor({ cmd: 'npm', args: ['run', 'test:review; rm -rf /'] });
    assert.ok(!argv.includes('sh') && !argv.includes('bash') && !argv.includes('-c'),
        'an argv array has no shell to interpret a semicolon: ' + argv.join(' '));
    assert.ok(argv.includes('run "test:review; rm -rf /"'.split(' ')[0]));
    assert.ok(argv.includes('test:review; rm -rf /'), 'the argument survives intact, uninterpreted');
});

test('a stack image brings its toolchain env and still drops capabilities', () => {
    const argv = argvFor({ stack: 'go' });
    assert.ok(argv.some(a => a.includes('golang')), 'go runs in the go image');
    assert.ok(argv.some(a => a.startsWith('GOCACHE=')), 'with somewhere writable to cache');
    assert.equal(valueOf(argv, '--cap-drop'), 'ALL', 'hardening is not per-image');
    assert.equal(valueOf(argv, '--network'), 'none');
});

test("a check's own env wins over the toolchain's", () => {
    const argv = argvFor({ stack: 'go', env: { GOFLAGS: '-tags=integration' } });
    assert.ok(argv.includes('GOFLAGS=-tags=integration'));
    assert.equal(argv.filter(a => a.startsWith('GOFLAGS=')).length, 1);
});

test('a dangling symlink is skipped, not thrown', () => {
    // An agent that deleted the directory its symlink pointed at is an
    // ordinary state for a worktree. Unguarded, realpath's ENOENT came out of
    // mountArgs and failed the whole review rather than one mount.
    const { live, wt } = scratchProject();
    fs.symlinkSync(path.join(live, 'gone-away'), path.join(wt, 'node_modules'));
    const mounts = mountsOf(sandbox.mountArgs({ live }, wt));
    assert.ok(mounts.includes(`${wt}:/workspace`), 'the worktree is still mounted');
    assert.ok(!mounts.some(m => m.includes('gone-away')));
});

// --- a timeout stops the container -----------------------------------------
//
// 2026-10-22 audit, R16: execFile's timeout only signalled the docker CLI; a
// test runner as the container's PID 1 ignored it, ran to the end, and its
// exit 0 was recorded as a pass while every other review waited behind it.

test('the container is named and has an init, so it can be stopped', () => {
    const { live, wt } = scratchProject();
    const { docker } = sandbox.dockerArgs({ live }, wt, '.', 'npm', ['test'], {}, 'none', null, 'rvw-abc123');
    assert.equal(valueOf(docker, '--name'), 'rvw-abc123');
    assert.ok(docker.includes('--init'), 'a test runner as PID 1 ignores SIGTERM');
    assert.ok(docker.indexOf('--init') < docker.indexOf('--entrypoint'), 'a run flag, not an argument to the check');
});

test('a check that outlives its timeout is killed by name and reported as timed out', () => {
    // A fake docker: `run` hangs as a check that ignores signals would, and
    // `kill`/`rm` are recorded. Chained after the other PATH-swapping test so
    // the two never see each other's docker.
    const prior = Promise.allSettled(pendingAsync.slice());
    pendingAsync.push(prior.then(async () => {
        const bin = fs.mkdtempSync(path.join(os.tmpdir(), 'fakedocker-'));
        const calls = path.join(bin, 'calls');
        fs.writeFileSync(path.join(bin, 'docker'),
            `#!/bin/sh\necho "$@" >> '${calls}'\n[ "$1" = run ] && exec sleep 30\nexit 0\n`, { mode: 0o755 });
        const savedPath = process.env.PATH;
        process.env.PATH = `${bin}:${savedPath}`;
        const { live, wt } = scratchProject();
        try {
            const started = Date.now();
            const r = await sandbox.runSandboxed({ live }, wt, '.', 'npm', ['test'], 500, {}, 'none');
            assert.ok(Date.now() - started < 10_000, 'returned when the timeout fired, not when the check ended');
            assert.equal(r.ok, false);
            assert.equal(r.code, 124);
            assert.equal(r.infrastructure, true);
            assert.match(r.output, /timed out after/);
            const lines = fs.readFileSync(calls, 'utf8').trim().split('\n');
            const name = valueOf(lines[0].split(' '), '--name');
            assert.match(name || '', /^rvw-[0-9a-f]+$/);
            assert.deepEqual(lines.slice(1), [`kill ${name}`, `rm -f ${name}`]);
        } finally {
            process.env.PATH = savedPath;
            fs.rmSync(bin, { recursive: true, force: true });
        }
    }));
});

test('with docker and the image here, a hung check leaves no container behind', () => {
    // Docker-gated: skipped on a machine with no daemon or no sandbox image.
    // Asked inside the chain: the tests above put a fake docker on PATH until
    // their own runs finish.
    const { execFileSync } = require('child_process');
    const prior = Promise.allSettled(pendingAsync.slice());
    pendingAsync.push(prior.then(async () => {
        try {
            execFileSync('docker', ['image', 'inspect', sandbox.IMAGE], { stdio: 'ignore' });
        } catch {
            console.log('  skip  no docker or no sandbox image');
            return;
        }
        // Compared with what was there before: on a machine running the
        // bundle, the agent's own review containers are also called rvw-.
        const rvw = () => execFileSync('docker', ['ps', '-aq', '--filter', 'name=rvw-'], { encoding: 'utf8' })
            .split('\n').filter(Boolean);
        const before = new Set(rvw());
        const { live, wt } = scratchProject();
        const started = Date.now();
        const r = await sandbox.runSandboxed({ live }, wt, '.', 'sleep', ['40'], 3_000, {}, 'none');
        assert.ok(Date.now() - started < 20_000, `took ${Date.now() - started} ms`);
        assert.equal(r.ok, false);
        assert.equal(r.code, 124);
        // `rm -f` racing `--rm` leaves the container "Dead" for a moment
        // while the daemon finishes removing it; gone within seconds.
        let left = [];
        for (let i = 0; i < 20; i++) {
            left = rvw().filter((id) => !before.has(id));
            if (!left.length) break;
            await new Promise((resolve) => setTimeout(resolve, 500));
        }
        assert.deepEqual(left, [], `containers left behind: ${left}`);
    }));
});
