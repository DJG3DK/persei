import { useCallback, useEffect, useState } from "react";
import { getConsolidationStatus, getJobs, runJob, type ConsolidationStatus, type JobStatus } from "../api";
import "./ConsolidationStatusPanel.css";

/** The daily jobs' health: memory consolidation, and the codebase map.
 *
 *  Exists because a failed run was previously indistinguishable from a healthy
 *  one: the script printed a line to a log and exited 0, so a provider
 *  incompatibility silently skipped consolidation for months. The three states
 *  that matter are "ran and succeeded", "ran and failed", and "hasn't run" —
 *  the last one being the case a log tail can never show you.
 *
 *  Since 2026-09-28 the agent schedules these itself (agent/jobs.py): once a
 *  day at the first quiet moment after they are due. The panel shows when
 *  each is next due and lets an admin run one now.
 */
// Collapsed by default on a phone (2026-10-01): the card sits above the model
// list, which scrolls inside its own pane, so open it held some 40% of the
// screen the whole time. The choice is remembered per browser.
const OPEN_KEY = "consolidation.open";
const NARROW = "(max-width: 760px)";

function initiallyOpen(): boolean {
  try {
    const saved = window.localStorage.getItem(OPEN_KEY);
    if (saved === "1" || saved === "0") return saved === "1";
  } catch {
    // storage blocked: fall through to the screen-size default
  }
  return !(typeof window.matchMedia === "function" && window.matchMedia(NARROW).matches);
}

export function ConsolidationStatusPanel() {
  const [open, setOpen] = useState<boolean>(initiallyOpen);
  const [status, setStatus] = useState<ConsolidationStatus | null>(null);
  const [jobs, setJobs] = useState<JobStatus[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [runError, setRunError] = useState<string | null>(null);
  const [showLog, setShowLog] = useState(false);
  const [starting, setStarting] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const s = await getConsolidationStatus();
      setStatus(s);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "failed to load");
    }
    try {
      setJobs((await getJobs()).jobs);
    } catch {
      // The schedule is a detail; the consolidation verdict above still shows.
    }
  }, []);

  useEffect(() => {
    // The first load is deferred a tick so the effect itself sets no state;
    // the interval is the only thing to clean up.
    const first = setTimeout(() => { void load(); }, 0);
    const t = setInterval(() => { void load(); }, 300_000);
    return () => { clearTimeout(first); clearInterval(t); };
  }, [load]);

  const anyRunning = jobs.some((j) => j.running);
  useEffect(() => {
    if (!anyRunning) return;
    const t = setInterval(() => { void load(); }, 5_000);
    return () => clearInterval(t);
  }, [anyRunning, load]);

  async function handleRun(name: string) {
    setStarting(name);
    setRunError(null);
    try {
      await runJob(name);
      await load();
    } catch (e) {
      setRunError(e instanceof Error ? e.message : "could not start");
    } finally {
      setStarting(null);
    }
  }

  if (error) return <div className="consol-panel consol-panel--err">Consolidation status: {error}</div>;
  if (!status) return <div className="consol-panel">Loading consolidation status…</div>;

  const neverRan = !status.ran_at;
  const failed = status.ok === false;
  const stale = status.stale && !neverRan;
  const tone = neverRan || failed ? "bad" : stale ? "warn" : "good";
  const headline = status.running
    ? "Running now"
    : neverRan
      ? "Never run"
      : failed
        ? `Failed (exit ${status.exit_code})`
        : stale
          ? "Stale"
          : "Healthy";
  const fmt = (ts: string | null | undefined) => (ts ? ts.replace("T", " ").replace("Z", " UTC") : "");
  // The one line a collapsed card keeps: when the next run is.
  const nextShort = status.running ? "running now" : status.due ? "due now"
    : status.due_at ? `next ${fmt(status.due_at).slice(11, 16)} UTC` : "";

  function toggle() {
    setOpen((v) => {
      try { window.localStorage.setItem(OPEN_KEY, v ? "0" : "1"); } catch { /* a convenience only */ }
      return !v;
    });
  }

  return (
    <div className={`consol-panel consol-panel--${tone}${open ? "" : " consol-panel--collapsed"}`}>
      <button type="button" className="consol-head" onClick={toggle} aria-expanded={open}
              aria-label={`Memory consolidation: ${headline}${open ? "" : `, ${nextShort}`}. ${open ? "Collapse" : "Expand"}`}>
        <span className="consol-dot" />
        <span className="consol-title">Memory consolidation</span>
        {!open && nextShort && <span className="consol-next">{nextShort.replace(" UTC", "")}</span>}
        <span className="consol-headline">{headline}</span>
        <span className={`consol-chevron${open ? " consol-chevron--open" : ""}`} aria-hidden="true">▾</span>
      </button>
      {open && (<>
      <div className="consol-meta">
        {neverRan ? (
          <>No run recorded yet. The agent runs it once a day at the first quiet moment
            {status.due_at === null && status.due ? " — it is due now, and starts as soon as no task is running" : ""}.</>
        ) : (
          <>
            Last run {fmt(status.ran_at)}
            {typeof status.age_hours === "number" && <> · {status.age_hours}h ago</>}
            {status.trigger && <> · {status.trigger}</>}
            {status.due_at && !status.due && <> · next due {fmt(status.due_at)}</>}
            {status.due && !status.running && <> · due now</>}
            {status.waiting && <> · {status.waiting}</>}
            {stale && <> · expected daily, so this has missed at least one</>}
            {failed && <> · memory was NOT updated; episodes stay unconsolidated until re-run</>}
            {failed && status.error && <> · {status.error}</>}
          </>
        )}
      </div>
      {jobs.length > 0 && (
        <ul className="consol-jobs">
          {jobs.map((j) => (
            <li key={j.name} className="consol-job">
              <span className="consol-job-title">{j.title}</span>
              <span className="consol-job-state">
                {j.running ? "running…" : j.ran_at ? `last ${fmt(j.ran_at)}${j.ok === false ? " (failed)" : ""}` : "never run"}
                {!j.running && j.due_at && !j.due && ` · next ${fmt(j.due_at)}`}
                {!j.running && j.due && " · due"}
              </span>
              <button
                className="consol-run-btn"
                disabled={j.running || starting === j.name}
                onClick={() => void handleRun(j.name)}
                aria-label={`Run ${j.title} now`}
              >
                {j.running ? "Running" : "Run now"}
              </button>
            </li>
          ))}
        </ul>
      )}
      {runError && <div className="consol-run-error">{runError}</div>}
      {status.tail && (
        <>
          <button className="consol-toggle" onClick={() => setShowLog((v) => !v)}>
            {showLog ? "Hide log" : "Show log"}
          </button>
          {showLog && <pre className="consol-log">{status.tail}</pre>}
        </>
      )}
      </>)}
    </div>
  );
}
