import { describe, expect, it } from "vitest";
import { currentPhase, idleMessage } from "./activityPhase";
import type { LogEntry } from "../types";

/* The banner used to say one thing at every kind of silence.
 *
 * Reported 2026-09-13 with a screenshot: a task on webapp sat in its check
 * suite, the stream said "running the full check suite (typecheck/lint/tests)
 * — several minutes of quiet is normal here", and immediately under it the
 * yellow banner said "No activity for 3 min ... the agent is either on a long
 * model call or stuck". Both on screen at once. The suite was healthy: it
 * takes 307-311s on that project, 111 tests, and it passed.
 *
 * A check run and a review wait announce themselves and then emit nothing
 * until they finish, so the announcement is the last entry for exactly as long
 * as the phase lasts. That is the signal.
 */

function entry(summary: string, extra: Record<string, unknown> = {}): LogEntry {
  return {
    node: "verify_and_ship", step_id: null, summary, detail: "",
    cost_usd: 0, timestamp: "2026-09-13T14:26:00Z", ...extra,
  } as LogEntry;
}

const CHECKS = entry("running the full check suite (typecheck/lint/tests) — several minutes of quiet is normal here");

describe("which phase the task is in", () => {
  it("recognises a check run from the announcement the server already sends", () => {
    const phase = currentPhase([entry("work pass complete"), CHECKS]);
    expect(phase?.id).toBe("checks");
  });

  it("prefers an explicit phase field over matching prose", () => {
    const phase = currentPhase([entry("something we have never seen", { phase: "review" })]);
    expect(phase?.id).toBe("review");
  });

  it("only looks at the last entry, because a newer one means the phase ended", () => {
    expect(currentPhase([CHECKS, entry("checks passed")])).toBeNull();
  });

  it("is null for ordinary model work", () => {
    expect(currentPhase([entry("calling: read({path: 'a.ts'})")])).toBeNull();
    expect(currentPhase([])).toBeNull();
  });
});

describe("what the banner says", () => {
  it("says nothing during a check run that is still within normal", () => {
    // The exact case from the report: 3 minutes into a suite that takes 5.
    expect(idleMessage([CHECKS], 180)).toBeNull();
    expect(idleMessage([CHECKS], 300)).toBeNull();
  });

  it("speaks up once a check run passes what is normal, and names the phase", () => {
    const msg = idleMessage([CHECKS], 600);
    expect(msg).toContain("check suite");
    expect(msg).toContain("longer than usual");
    expect(msg).not.toContain("either on a long model call or stuck");
  });

  it("uses the server's own per-project estimate when it has one", () => {
    const withEstimate = entry("running the full check suite", { phase: "checks", expected_seconds: 310 });
    // 310 * 1.5 is under the floor, so the floor still governs -- a project
    // whose suite is quick must not make a slower run look broken.
    expect(idleMessage([withEstimate], 400)).toBeNull();
    expect(idleMessage([withEstimate], 600)).toContain("about 5 min");
  });

  it("a slow project raises the bar rather than warning on schedule", () => {
    const slow = entry("running the full check suite", { phase: "checks", expected_seconds: 900 });
    expect(idleMessage([slow], 900)).toBeNull();          // still inside 900*1.5
    expect(idleMessage([slow], 1400)).toContain("longer than usual");
  });

  it("keeps the old warning for ordinary silence, after five minutes", () => {
    const work = [entry("calling: bash({command: 'npm test'})")];
    expect(idleMessage(work, 60)).toBeNull();
    expect(idleMessage(work, 200)).toBeNull();   // a long model call is normal
    expect(idleMessage(work, 299)).toBeNull();
    expect(idleMessage(work, 300)).toContain("either on a long model call or stuck");
  });

  it("covers a review wait, which is silent for up to fifteen minutes", () => {
    const review = [entry("waiting for the review service", { phase: "review" })];
    expect(idleMessage(review, 300)).toBeNull();
    expect(idleMessage(review, 700)).toContain("review service");
  });

  it("always offers the way out", () => {
    const cases: [LogEntry[], number][] = [[[CHECKS], 900], [[entry("x")], 360]];
    for (const [log, secs] of cases) {
      expect(idleMessage(log, secs)).toContain("Stop button");
    }
  });
});
