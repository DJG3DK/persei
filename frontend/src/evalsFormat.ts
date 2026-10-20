/* Formatting and comparison for the golden-suite panel (components/EvalsPanel.tsx),
 * kept apart from the component so they can be tested on their own and the
 * component file exports only a component. */
import type { EvalRunSummary } from "./types";

export const pct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v)}%`);
export const usd = (v: number | null | undefined) => (v == null ? "—" : `$${v.toFixed(2)}`);

export function minutes(seconds: number | null | undefined): string {
  if (seconds == null) return "—";
  const m = Math.round(seconds / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

export function day(iso: string | null): string {
  if (!iso) return "—";
  return iso.slice(0, 10);
}

/** What changed between two full runs, task by task. Only tasks both ran. */
export function compareRuns(current: EvalRunSummary, previous: EvalRunSummary | undefined) {
  const regressed: string[] = [];
  const fixed: string[] = [];
  if (!previous) return { regressed, fixed };
  for (const [id, passed] of Object.entries(current.results)) {
    const before = previous.results[id];
    if (before === undefined) continue;
    if (before && !passed) regressed.push(id);
    if (!before && passed) fixed.push(id);
  }
  return { regressed: regressed.sort(), fixed: fixed.sort() };
}

/** The paragraph "Copy scorecard" puts on the clipboard. */
export function scorecardText(run: EvalRunSummary): string {
  const b = run.benchmarks || {};
  const perTask = run.total_cost_usd != null && run.tasks_attempted
    ? ` (${usd(run.total_cost_usd / run.tasks_attempted)} per task)` : "";
  const cats = Object.entries(run.by_category)
    .sort(([a], [z]) => a.localeCompare(z))
    .map(([c, v]) => `${c} ${v.passed}/${v.tasks}`)
    .join(", ");
  return [
    `Persei golden suite, ${day(run.started_at)}: ${run.tasks_passed}/${run.tasks_attempted} tasks passed (${pct(run.pass_rate)}),`,
    `${pct(b.first_pass_rate as number | undefined)} passed independent review first time,`,
    `${b.escalated ?? 0} escalated, ${usd(run.total_cost_usd)} total${perTask}, ${minutes(run.duration_s)}.`,
    cats ? `By category: ${cats}.` : "",
    "Real agent, real checks, real reviewer; scored by assertions, not by whether it shipped.",
  ].filter(Boolean).join(" ");
}
