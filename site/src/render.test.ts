/**
 * The built index.html must contain the page's real text, and no JavaScript.
 *
 * The first half is the assertion that would have caught the original
 * problem: before 2026-09-16 the built page shipped `<div id="root"></div>`
 * and nothing else, so a crawler that does not execute JavaScript saw no body
 * content at all — the only occurrences of "coding agent" anywhere were
 * inside meta tags. Google renders JS on a second, queued pass; Bing,
 * DuckDuckGo and most LLM crawlers largely do not.
 *
 * The second half is new with the split (2026-09-17): this page ships as
 * static files, so a reintroduced runtime is a regression rather than a
 * detail. scripts/render.mjs fails the build over it; this fails the tests.
 *
 * Asserts on CONTENT, not on the mechanism. If the rendering is ever replaced
 * by SSG or a different framework, these still pass; if the step is dropped
 * or silently stops emitting, they fail.
 */
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

const DIST_INDEX = path.resolve(__dirname, '../dist/index.html');
const built = existsSync(DIST_INDEX) ? readFileSync(DIST_INDEX, 'utf8') : null;

// Skipped rather than failed on a tree that has not been built: `npm test`
// runs on a fresh checkout in CI before any build, and a test that demands
// build output would fail for a reason that has nothing to do with the code.
const withBuild = built ? describe : describe.skip;

// ...which also meant these never ran in CI at all: `npm test` came before
// `npm run build`, so all of them skipped on every push (2026-10-22, audit
// T7). CI now runs this file again after the build with SITE_REQUIRE_DIST=1,
// and there a missing dist/ is a failure, not a skip.
describe('the build output', () => {
  it.runIf(process.env.SITE_REQUIRE_DIST === '1')('exists when the build has run', () => {
    expect(built, `${DIST_INDEX} is missing; run npm run build first`).not.toBeNull();
  });
});

function bodyText(html: string): string {
  const marker = '<div id="root">';
  const body = html.slice(html.indexOf(marker) + marker.length);
  return body
    // Case-insensitive, and tolerant of whitespace in the closing tag:
    // <SCRIPT> and </script > are both valid, and a tag this misses leaves
    // its contents in the text these assertions then measure.
    .replace(/<script\b[\s\S]*?<\/script[^>]*>/gi, ' ')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

withBuild('the built page is readable without JavaScript', () => {
  it('has a non-empty root, not just an empty mount point', () => {
    expect(built).not.toContain('<div id="root"></div>');
  });

  it('carries a substantial amount of real text', () => {
    // 7.6K at the time of writing. The floor is low enough that ordinary copy
    // edits never trip it, and high enough that an empty or half-rendered
    // root does.
    expect(bodyText(built!).length).toBeGreaterThan(3000);
  });

  it('states what the product is, in the body and not only in meta tags', () => {
    const text = bodyText(built!).toLowerCase();
    for (const phrase of ['autonomous coding agent', 'review gate', 'test suite']) {
      expect(text).toContain(phrase);
    }
  });

  it('names the category words someone would search for', () => {
    // These were measured at 0 occurrences on the live page before this work.
    const text = bodyText(built!).toLowerCase();
    expect(text).toContain('openrouter');
    expect(text).toContain('self-hosted');
  });

  it('keeps the heading structure a crawler reads for hierarchy', () => {
    expect(built).toMatch(/<h1[^>]*>/);
    expect(built).toMatch(/<h2[^>]*>/);
  });

  it('ships no JavaScript at all', () => {
    // The controls are links and a plain HTML form. Anything that needed a
    // runtime would render and then do nothing, because there is no runtime.
    expect(built).not.toMatch(/<script[^>]+src=/);
  });

  it('keeps the structured data, which is content rather than code', () => {
    expect(built).toContain('application/ld+json');
    expect(built).toContain('SoftwareApplication');
  });
});
