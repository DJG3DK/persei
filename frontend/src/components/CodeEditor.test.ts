import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/* Audit U25 (2026-10-22): an npm override of dompurify was the claimed fix
 * for Monaco's advisories, but Monaco bundles its own copy. These pin the
 * accepted-risk note in CodeEditor.tsx to what is actually shipped. */
const FRONTEND = join(__dirname, "..", "..");
const MONACO = join(FRONTEND, "node_modules", "monaco-editor", "esm");

function vendoredVersion(): string {
  const src = readFileSync(join(MONACO, "vs", "base", "browser", "dompurify", "dompurify.js"), "utf8");
  const m = /DOMPurify (\d+\.\d+\.\d+)/.exec(src.slice(0, 400));
  if (!m) throw new Error("no version in Monaco's vendored DOMPurify header");
  return m[1];
}

function* jsFiles(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* jsFiles(p);
    else if (p.endsWith(".js")) yield p;
  }
}

function npmVersion(): string {
  return JSON.parse(readFileSync(join(FRONTEND, "node_modules", "dompurify", "package.json"), "utf8")).version;
}

function newer(a: string, b: string): boolean {
  const [x, y] = [a, b].map((v) => v.split(".").map(Number));
  for (let i = 0; i < 3; i += 1) if (x[i] !== y[i]) return x[i] > y[i];
  return false;
}

describe("Monaco's DOMPurify", () => {
  it("the note names the version Monaco vendors", () => {
    const note = readFileSync(join(__dirname, "CodeEditor.tsx"), "utf8");
    expect(note).toContain(`vendors DOMPurify ${vendoredVersion()}`);
  });

  it("Monaco imports its copy from one place, and the build sends that import to the npm package", async () => {
    // A second importer would ship the vendored copy again, unredirected.
    const importers = [...jsFiles(MONACO)].filter((f) => /dompurify\/dompurify\.js["']/.test(readFileSync(f, "utf8")))
      .map((f) => f.slice(MONACO.length + 1).replace(/\\/g, "/"));
    expect(importers).toEqual(["vs/base/browser/domSanitize.js"]);
    const { monacoDompurify, PURIFY } = await import("../../vite.config");
    const resolve = monacoDompurify().resolveId as (s: string, i?: string) => string | null;
    expect(resolve("./dompurify/dompurify.js", join(MONACO, "vs", "base", "browser", "domSanitize.js"))).toBe(PURIFY);
    expect(resolve("./dompurify/dompurify.js", join(FRONTEND, "src", "x.ts"))).toBeNull();
  });

  it("the npm copy it gets instead is newer than the one Monaco vendors", () => {
    // Audit U25: an override once "patched" an unused copy. This is the
    // copy the editor now runs; when Monaco catches up, drop the redirect.
    expect(newer(npmVersion(), vendoredVersion())).toBe(true);
  });
});
