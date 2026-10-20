/* Wording for the SWE-bench panel (components/SwebenchPanel.tsx), apart from
 * the component so it can be tested on its own.
 *
 * The rules are evals/SWEBENCH.md's: only a full run of all 500 tasks is "X%
 * on SWE-bench Verified". A sample is "N of a 50-task sample" -- its
 * percentage shown, but never as a bare number (2026-09-25: the operator
 * looked for a grade beside "40/50" and found none) -- and a diagnostic run
 * is never a score at all. */
import type { SwebenchHost, SwebenchHostField, SwebenchRunSummary } from "./types";
import { day, minutes, usd } from "./evalsFormat";

export const KIND_LABEL: Record<SwebenchRunSummary["kind"], string> = {
  full: "full run",
  sample: "sample",
  selected: "selected tasks",
  diagnostic: "diagnostic",
};

/** The run the scorecard shows: the newest full run, else the newest run that is a score. */
export function headlineRun(runs: SwebenchRunSummary[]): SwebenchRunSummary | undefined {
  return runs.find((r) => r.kind === "full" && r.graded)
    ?? runs.find((r) => r.kind === "full")
    ?? runs.find((r) => r.kind !== "diagnostic");
}

/** "412/500"; while running, "9/12" -- resolved over the tasks graded so far
 *  (scoreLabel says so beside it). */
export function scoreText(r: SwebenchRunSummary): string {
  if (r.graded) return `${r.resolved ?? 0}/${r.total}`;
  if (r.graded_count > 0) return `${r.resolved ?? 0}/${r.graded_count}`;
  return "—";
}

export function scoreLabel(r: SwebenchRunSummary): string {
  const pct = r.graded && r.total ? `${((100 * (r.resolved ?? 0)) / r.total).toFixed(1)}%` : "";
  if (r.kind === "full") return pct ? `resolved · ${pct}` : "resolved";
  if (!r.graded) return r.graded_count > 0 ? "resolved among the tasks graded so far" : "not graded yet";
  return `resolved in a ${r.total}-task ${r.kind === "sample" ? "sample" : "selection"}${pct ? ` (${pct})` : ""} — not the published number`;
}

/** The first model of each role, "agent-coder -> deepseek/x" as "coder: x". */
export function modelList(models: Record<string, number>): string[] {
  const seen = new Map<string, string>();
  for (const key of Object.keys(models)) {
    const [alias, model] = key.split(" -> ");
    const role = (alias || "").replace(/^agent-/, "");
    if (!seen.has(role) && model && model !== "undefined") seen.set(role, model);
  }
  return [...seen].map(([role, model]) => `${role}: ${model}`);
}

/** The paragraph "Copy scorecard" puts on the clipboard. */
export function swebenchScorecard(r: SwebenchRunSummary, referenceFails: string[]): string {
  const models = modelList(r.models).join(", ");
  const resolved = r.resolved ?? 0;
  const head = r.kind === "full"
    ? `Persei resolves ${resolved} of the ${r.total} SWE-bench Verified tasks (${((100 * resolved) / r.total).toFixed(1)}%)`
    : `Persei resolved ${resolved} of a ${r.total}-task ${r.kind === "sample" ? "sample" : "selection"} of SWE-bench Verified`;
  const perTask = r.total_cost_usd != null && r.total ? `${usd(r.total_cost_usd / r.total)} per task` : "";
  return [
    `${head}, ${day(r.started_at)}, graded by the official SWE-bench harness.`,
    "One attempt per task, the official task images, no internet access.",
    [perTask, r.duration_s ? `${minutes(r.duration_s)} in total` : ""].filter(Boolean).join(", ") + ".",
    models ? `Models: ${models}.` : "",
    referenceFails.length
      ? `The official reference fix itself fails ${referenceFails.length} task${referenceFails.length === 1 ? "" : "s"} in these images (${referenceFails.join(", ")}).`
      : "",
  ].filter((s) => s && s !== ".").join(" ");
}

/** One row of the "Host during the run" peaks table. */
interface HostPeak {
  key: string;
  label: string;
  value: string;
}

const seriesMax = (h: SwebenchHost, k: SwebenchHostField): number | undefined =>
  h.series.reduce<number | undefined>((m, s) => (s[k] == null ? m : m == null || s[k] > m ? s[k] : m), undefined);
const seriesMin = (h: SwebenchHost, k: SwebenchHostField): number | undefined =>
  h.series.reduce<number | undefined>((m, s) => (s[k] == null ? m : m == null || s[k] < m ? s[k] : m), undefined);
const seriesSum = (h: SwebenchHost, k: SwebenchHostField): number | undefined =>
  h.series.reduce<number | undefined>((m, s) => (s[k] == null ? m : (m ?? 0) + s[k]), undefined);
const seriesLast = (h: SwebenchHost, k: SwebenchHostField): number | undefined => {
  for (let i = h.series.length - 1; i >= 0; i--) {
    const v = h.series[i][k];
    if (v != null) return v;
  }
  return undefined;
};

export const hostPct = (v: number | null | undefined) => (v == null ? "—" : `${Math.round(v)}%`);
const hostGb = (v: number | null | undefined) => (v == null ? "—" : `${v.toFixed(1)} GB`);
export const hostSec = (v: number | null | undefined) => (v == null ? "—" : `${v.toFixed(1)} s`);
export const hostNum = (v: number | null | undefined, digits = 0) => (v == null ? "—" : v.toFixed(digits));

/** The peaks row: what the box and the router hit while the run went. Peaks
 *  the runner kept (over every sample) win over the thinned series; the
 *  lowest available memory, the OOM total and the error total come from the
 *  series, which is the only place they can. */
export function hostPeaks(h: SwebenchHost): HostPeak[] {
  const peak = (k: SwebenchHostField) => h.peaks[k] ?? seriesMax(h, k);
  return [
    { key: "mem_pct", label: "Memory peak", value: hostPct(peak("mem_pct")) },
    { key: "mem_avail_gb", label: "Memory available, lowest", value: hostGb(seriesMin(h, "mem_avail_gb")) },
    { key: "cpu_pct", label: "CPU peak", value: hostPct(peak("cpu_pct")) },
    { key: "load1", label: "Load average peak (1 min)", value: hostNum(peak("load1"), 1) },
    { key: "disk_pct", label: "Docker disk peak", value: hostPct(peak("disk_pct")) },
    { key: "containers", label: "Containers running, peak", value: hostNum(peak("containers")) },
    { key: "oom_kills", label: "OOM kills in task containers", value: hostNum(seriesLast(h, "oom_kills") ?? h.peaks.oom_kills) },
    { key: "router_inflight", label: "Model calls in flight, peak", value: hostNum(peak("router_inflight")) },
    { key: "router_p90_s", label: "p90 model-call latency, worst minute", value: hostSec(peak("router_p90_s")) },
    { key: "router_errors", label: "Model-call errors", value: hostNum(seriesSum(h, "router_errors")) },
  ];
}
