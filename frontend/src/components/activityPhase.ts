import type { LogEntry } from "../types";

/** A phase the task can sit in silently, and how long that silence is normal.
 *
 *  The idle banner used to say the same thing at every kind of quiet: "the
 *  agent is either on a long model call or stuck". During a check run that is
 *  simply wrong, and it contradicted the line directly above it, which said
 *  several minutes of quiet was normal — the operator saw both at once and
 *  reasonably read it as a stall (2026-09-13).
 *
 *  `quietSeconds` is how long this phase may be silent before anything is
 *  worth saying. The backend sends a real per-project estimate where it has
 *  one (`expected_seconds`); these are the floors used until it does.
 */
interface ActivityPhase {
  id: "checks" | "review" | "deploy";
  /** What it is doing, as a sentence opener. */
  label: string;
  /** Silence below this is unremarkable. */
  quietSeconds: number;
  /** Where the estimate came from, so the copy can be honest about it. */
  expectedSeconds: number | null;
}

/** Matched against the summary when the backend has not sent a `phase` yet —
 *  the dashboard updates before the server does, and an operator on the old
 *  server should still get the better banner. */
const PATTERNS: { id: ActivityPhase["id"]; label: string; re: RegExp; floor: number }[] = [
  {
    id: "checks",
    label: "Running the project's full check suite",
    re: /running the full check suite/i,
    // A big suite is 111 tests and takes five minutes; a small repo
    // takes twenty seconds. The floor is set for the slow end, because the
    // cost of warning late is a mild delay and the cost of warning early is
    // an operator killing a healthy run.
    floor: 480,
  },
  {
    id: "review",
    label: "Waiting for the review service",
    re: /waiting for the review service|sent for review|polling review/i,
    floor: 480,
  },
  {
    id: "deploy",
    label: "Merging and deploying",
    re: /merging|deploying|merged and deployed/i,
    floor: 240,
  },
];

/** The phase the task is in RIGHT NOW, or null for ordinary model work.
 *
 *  Only the last entry is considered, deliberately: these phases announce
 *  themselves and then emit nothing until they finish, so the announcement
 *  stays last for exactly as long as the phase is running. Anything newer
 *  means the phase ended.
 */
export function currentPhase(log: LogEntry[]): ActivityPhase | null {
  const last = log[log.length - 1];
  if (!last) return null;

  const declared = (last as LogEntry & { phase?: string }).phase;
  const expected = (last as LogEntry & { expected_seconds?: number }).expected_seconds ?? null;
  const match = PATTERNS.find((p) => (declared ? p.id === declared : p.re.test(last.summary || "")));
  if (!match) return null;

  return {
    id: match.id,
    label: match.label,
    // A real measurement beats a guess, but never lowers the floor: a project
    // whose suite took 20s once should not make a 3-minute run look wrong.
    quietSeconds: Math.max(match.floor, expected ? Math.round(expected * 1.5) : 0),
    expectedSeconds: expected,
  };
}

function humanDuration(seconds: number): string {
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  return `${(seconds / 3600).toFixed(1)} h`;
}

/** Silence outside a known phase that is still normal. */
export const ORDINARY_QUIET_SECONDS = 300;

/** What the banner should say, or null when there is nothing worth saying. */
export function idleMessage(log: LogEntry[], idleSeconds: number): string | null {
  const phase = currentPhase(log);
  const elapsed = humanDuration(idleSeconds);

  if (phase) {
    if (idleSeconds < phase.quietSeconds) return null;  // normal for this phase
    const usual = phase.expectedSeconds
      ? ` This project usually takes about ${humanDuration(phase.expectedSeconds)}.`
      : "";
    return `${phase.label} — ${elapsed} so far, which is longer than usual.${usual}`
      + " The Stop button ends it, and a resume keeps the work so far.";
  }

  // Five minutes, not two (2026-10-22): a model call on a big context
  // routinely runs two or three minutes, and a warning that fires on ordinary
  // work teaches people to ignore it.
  if (idleSeconds < ORDINARY_QUIET_SECONDS) return null;
  return `No activity for ${elapsed}. The connection is live, so the agent is either on a long model`
    + " call or stuck — the Stop button ends it, and a resume keeps the work so far.";
}
