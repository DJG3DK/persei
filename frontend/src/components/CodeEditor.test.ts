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

describe("Monaco's DOMPurify", () => {
  it("the accepted-risk note names the version Monaco vendors", () => {
    const note = readFileSync(join(__dirname, "CodeEditor.tsx"), "utf8");
    expect(note).toContain(`vendors DOMPurify ${vendoredVersion()}`);
  });

  it("Monaco never imports the npm package, so an override would patch nothing", () => {
    const importers = [...jsFiles(MONACO)].filter((f) =>
      /from\s*["']dompurify["']|require\(\s*["']dompurify["']\s*\)/.test(readFileSync(f, "utf8")));
    expect(importers).toEqual([]);
    const pkg = JSON.parse(readFileSync(join(FRONTEND, "package.json"), "utf8"));
    expect(pkg.overrides?.dompurify).toBeUndefined();
  });
});
