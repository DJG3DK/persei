'use strict';
/**
 * The service worker's push handler, exercised directly.
 *
 * A browser is the wrong harness for this: headless Chromium accepts
 * ServiceWorker.deliverPushMessage and then declines to display anything, so
 * "no notification appeared" proves nothing either way. The handler is plain
 * JavaScript over a handful of globals, so it runs here against stubs and the
 * assertions are about what it CALLED rather than what a window manager did.
 *
 * The property that matters most: a push must ALWAYS end in
 * showNotification(). Every browser treats a push that shows nothing as a
 * "silent push" -- Chrome substitutes its own "This site has been updated in
 * the background" notice, and a pattern of them costs the subscription
 * outright. So a malformed, empty or non-JSON payload still has to notify.
 *
 * Run: node tests/test_sw_push.js
 */

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const SW = path.join(__dirname, '..', 'frontend', 'public', 'sw.js');

function loadWorker() {
    const handlers = {};
    const shown = [];
    const opened = [];
    const focused = [];
    const waited = [];

    const clientsList = [];
    const self_ = {
        addEventListener: (name, fn) => { handlers[name] = fn; },
        location: { origin: 'https://perseiagent.ai' },
        registration: {
            showNotification: (title, opts) => { shown.push({ title, ...opts }); return Promise.resolve(); },
        },
        clients: {
            matchAll: () => Promise.resolve(clientsList),
            openWindow: (u) => { opened.push(u); return Promise.resolve({}); },
            claim: () => Promise.resolve(),
        },
        skipWaiting: () => Promise.resolve(),
    };

    const sandbox = {
        self: self_,
        caches: {
            open: () => Promise.resolve({ add: () => Promise.resolve(), put: () => Promise.resolve(),
                                          keys: () => Promise.resolve([]) }),
            keys: () => Promise.resolve([]),
            match: () => Promise.resolve(undefined),
            delete: () => Promise.resolve(true),
        },
        fetch: () => Promise.resolve({ ok: true, clone: () => ({}) }),
        Request: class { constructor(u) { this.url = u; } },
        Response: class { constructor(body, init) { this.body = body; Object.assign(this, init); } },
        URL,
        console,
    };
    sandbox.self.fetch = sandbox.fetch;
    vm.createContext(sandbox);
    vm.runInContext(fs.readFileSync(SW, 'utf8'), sandbox);
    return { handlers, shown, opened, focused, clientsList, waited, self: self_ };
}

/** Fire an event and wait for whatever it passed to waitUntil. */
async function fire(handlers, name, event) {
    const pending = [];
    const e = { ...event, waitUntil: (p) => pending.push(p) };
    handlers[name](e);
    await Promise.all(pending);
    return e;
}

let passed = 0;
const tests = [];
function test(name, fn) { tests.push([name, fn]); }

test('a normal payload becomes a notification with the app icon', async () => {
    const w = loadWorker();
    await fire(w.handlers, 'push', {
        data: { json: () => ({ title: 'Task ESCALATED', body: 'webapp: needs you', url: '/', tag: 'webapp' }) },
    });
    assert.strictEqual(w.shown.length, 1);
    const n = w.shown[0];
    assert.strictEqual(n.title, 'Task ESCALATED');
    assert.strictEqual(n.body, 'webapp: needs you');
    assert.strictEqual(n.tag, 'webapp', 'the tag collapses repeats about one project');
    assert.strictEqual(n.icon, '/icon-192.png', 'without this it shows the browser globe');
    assert.strictEqual(n.data.url, '/', 'notificationclick reads this back');
});

test('a payload that is not JSON still notifies', async () => {
    // The silent-push rule: showing nothing costs the subscription.
    const w = loadWorker();
    await fire(w.handlers, 'push', {
        data: { json: () => { throw new SyntaxError('not json'); }, text: () => 'something happened' },
    });
    assert.strictEqual(w.shown.length, 1);
    assert.strictEqual(w.shown[0].title, 'Persei', 'falls back to the app name');
});

test('a push with no data at all still notifies', async () => {
    const w = loadWorker();
    await fire(w.handlers, 'push', { data: null });
    assert.strictEqual(w.shown.length, 1);
    assert.strictEqual(w.shown[0].title, 'Persei');
});

test('clicking focuses a window that is already open', async () => {
    const w = loadWorker();
    let focused = false;
    w.clientsList.push({
        url: 'https://perseiagent.ai/', focus() { focused = true; return Promise.resolve(this); },
    });
    let closed = false;
    await fire(w.handlers, 'notificationclick', {
        notification: { close: () => { closed = true; }, data: { url: '/' } },
    });
    assert.ok(closed, 'the notification must be dismissed');
    assert.ok(focused, 'an open dashboard holds live WebSocket streams -- reuse it');
    assert.strictEqual(w.opened.length, 0, 'a second window would open a second set of streams');
});

test('clicking opens a window when none is open', async () => {
    const w = loadWorker();
    await fire(w.handlers, 'notificationclick', {
        notification: { close() {}, data: { url: '/' } },
    });
    assert.deepStrictEqual(w.opened, ['/']);
});

test('a window on another origin is not adopted', async () => {
    const w = loadWorker();
    w.clientsList.push({ url: 'https://example.com/', focus: () => Promise.resolve() });
    await fire(w.handlers, 'notificationclick', {
        notification: { close() {}, data: { url: '/' } },
    });
    assert.deepStrictEqual(w.opened, ['/'], 'focused someone else\'s tab instead of opening ours');
});

(async () => {
    for (const [name, fn] of tests) {
        await fn();
        passed++;
        console.log(`  ok  ${name}`);
    }
    console.log(`\n${passed} passed`);
})().catch((e) => { console.error('FAILED:', e.message); process.exit(1); });
