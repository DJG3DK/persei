import { useEffect, useState } from "react";
import { moveProject } from "../api";
import {
  cloneProject,
  detectProject,
  looksLikeGitHubUrl,
  listProjectArchives,
  listProjectsConfig,
  deleteProjectArchive,
  provisionProject,
  type DetectionReport,
  type ProjectArchive,
  type ProvisionCandidate,
  type ProvisionStep,
  type RemoveProjectResult,
} from "../api";
import { RemoveProject } from "./RemoveProject";
import { DeployKeyCard } from "./DeployKeyCard";
import { Icon } from "./Icon";
import { StepList } from "./StepList";
import "./ProjectsPanel.css";

/* Onboarding a project the agent can work in.
 *
 * Three steps on purpose — path, review, provision — because the middle one
 * is a safety gate, not a formality. Detection can propose running a test
 * script that turns out to drive a live production service; this deployment
 * has one whose suite POSTed real trade orders. So anything we cannot verify
 * arrives switched OFF with the reason attached, and the operator turns it on
 * deliberately or not at all.
 */

type Stage = "idle" | "detecting" | "review" | "provisioning" | "done";

/** Candidate list with per-item toggles. Warned items render as a caution. */
function CandidateList({
  title, help, items, chosen, onToggle,
}: {
  title: string;
  help: string;
  items: ProvisionCandidate[];
  chosen: Record<string, boolean>;
  onToggle: (value: string, on: boolean) => void;
}) {
  if (!items.length) return null;
  return (
    <div className="wiz-group">
      <div className="wiz-group-head">
        <span className="wiz-group-title">{title}</span>
        <span className="wiz-group-help">{help}</span>
      </div>
      {items.map((c) => (
        <label key={c.value} className={`wiz-item ${c.warning ? "wiz-item--warn" : ""}`}>
          <input
            type="checkbox"
            checked={chosen[c.value] ?? c.enabled}
            onChange={(e) => onToggle(c.value, e.target.checked)}
          />
          <span className="wiz-item-body">
            <code className="wiz-item-value">{c.value}</code>
            <span className="wiz-item-reason">{c.reason}</span>
            {c.warning && <span className="wiz-item-warning">{c.warning}</span>}
          </span>
        </label>
      ))}
    </div>
  );
}

/** `onChanged` fires after a successful provision so App can reload its repo
 *  list -- a project configured here used to reach the planner's Repo
 *  dropdown only after a full page reload. */
/* What a merge will actually do, said out loud. Three outcomes, and the one
   that used to be silent -- "merge only" -- is the one somebody needs told. */
function deployOutcome(apps: Record<string, boolean>, restarts: Record<string, boolean>): string {
  const pm2 = Object.entries(apps).filter(([, on]) => on).map(([v]) => v);
  const cmds = Object.entries(restarts).filter(([, on]) => on).map(([v]) => v);
  if (!pm2.length && !cmds.length) {
    return "merge only — nothing on this machine is restarted";
  }
  const parts = [];
  if (pm2.length) parts.push(`restart ${pm2.join(", ")}`);
  if (cmds.length) parts.push(cmds.join(", "));
  return `merge, then ${parts.join(" and ")}`;
}

export function ProjectsPanel({ onChanged }: { onChanged?: () => void | Promise<void> } = {}) {
  const [existing, setExisting] = useState<Record<string, { live: string; sandbox: string }>>({});
  const [stage, setStage] = useState<Stage>("idle");
  const [path, setPath] = useState("");
  const [report, setReport] = useState<DetectionReport | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [steps, setSteps] = useState<ProvisionStep[]>([]);
  const [doneMsg, setDoneMsg] = useState<string | null>(null);
  // Which configured project has its push-access panel open.
  const [expanded, setExpanded] = useState<string | null>(null);
  /* Which project's removal panel is open. Held here because the Remove
     button that opens it lives on the row, outside that panel. */
  const [removing, setRemoving] = useState<string | null>(null);
  /* What the last removal did. Rendered here rather than in RemoveProject:
     the project leaves the list the moment it is removed, taking that panel
     with it, so a summary inside it is unmounted before anybody reads it. */
  const [removed, setRemoved] = useState<RemoveProjectResult | null>(null);
  // Change path: which project's box is in use, what is typed, and the outcome.
  const [moving, setMoving] = useState<string | null>(null);
  const [moveTo, setMoveTo] = useState("");
  const [moveError, setMoveError] = useState<string | null>(null);
  const [moveNote, setMoveNote] = useState<string | null>(null);

  // Per-candidate operator choices, keyed by list then value. Seeded from the
  // report's recommendation on first render of the review step.
  const [secrets, setSecrets] = useState<Record<string, boolean>>({});
  const [mounts, setMounts] = useState<Record<string, boolean>>({});
  const [apps, setApps] = useState<Record<string, boolean>>({});
  const [restarts, setRestarts] = useState<Record<string, boolean>>({});
  const [risky, setRisky] = useState<Record<string, boolean>>({});

  const [archives, setArchives] = useState<ProjectArchive[]>([]);
  // Which archive (if any) to restore into the project being added. Set
  // from the detection report, which lists archives matching its name.
  const [restoreFrom, setRestoreFrom] = useState<string | null>(null);

  async function load() {
    try {
      setExisting((await listProjectsConfig()).projects);
    } catch (e) {
      setError(e instanceof Error ? e.message : "could not load projects");
    }
  }
  async function loadArchives() {
    try {
      setArchives((await listProjectArchives()).archives);
    } catch {
      // A missing archive list must not stop the panel rendering the
      // projects, which is what anyone came here for.
    }
  }
  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- the load it starts is async; state lands after the await, not in the effect
    void load(); void loadArchives();
  }, []);

  async function handleDetect() {
    setStage("detecting");
    setError(null);
    setReport(null);
    try {
      /* A GitHub URL is not a path on this machine. Clone it first, then the
         rest of the wizard is identical -- it never learns how the directory
         got there. */
      const input = path.trim();
      const r = looksLikeGitHubUrl(input)
        ? await cloneProject(input)
        : await detectProject(input);
      setReport(r);
      setSecrets(Object.fromEntries(r.secret_files.map((c) => [c.value, c.enabled])));
      setMounts(Object.fromEntries(r.read_only_mounts.map((c) => [c.value, c.enabled])));
      setApps(Object.fromEntries(r.pm2_apps.map((c) => [c.value, c.enabled])));
      setRestarts(Object.fromEntries((r.restart_commands ?? []).map((c) => [c.value, c.enabled])));
      setRisky(Object.fromEntries(r.risky_scripts.map((c) => [c.value, c.enabled])));
      setStage("review");
    } catch (e) {
      setError(e instanceof Error ? e.message : "detection failed");
      setStage("idle");
    }
  }

  async function handleProvision() {
    if (!report) return;
    setStage("provisioning");
    setError(null);
    const on = (m: Record<string, boolean>) => Object.entries(m).filter(([, v]) => v).map(([k]) => k);
    // An enabled risky script is named, not authored: the server substitutes
    // its own detected command for that name, so the client cannot smuggle
    // arbitrary arguments into something the review service will execute.
    const extraChecks = on(risky).map((name) => ({ name }));
    try {
      const res = await provisionProject({
        path: report.live,
        choices: {
          secret_files: on(secrets),
          read_only_mounts: on(mounts),
          pm2_apps: on(apps),
          restart_commands: on(restarts),
          node_modules_dirs: report.node_modules_dirs,
          dependency_dirs: report.dependency_dirs,
          checks: [...report.checks, ...extraChecks],
          build_steps: report.build_steps,
          db_env_file: report.db_env_file,
          /* How it arrived decides how it ships: a repository the agent
             cloned opens pull requests rather than writing to a base branch
             nobody asked it to own. */
          cloned_from_github: report.cloned_from_github ?? false,
        },
        grant_access: true,
        restore_archive: restoreFrom,
      });
      setSteps(res.steps);
      setDoneMsg(res.message || (res.ok ? "Project configured." : "Provisioning failed."));
      setStage("done");
      if (res.ok) {
        void load();
        void onChanged?.();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "provisioning failed");
      setStage("review");
    }
  }

  function reset() {
    setStage("idle");
    setPath("");
    setReport(null);
    setSteps([]);
    setDoneMsg(null);
    setError(null);
  }

  const blocked = !!report?.blockers.length;

  // Renders a bare card, not its own section: the settings page pairs it with
  // Telegram in one row, and a panel that brought its own heading and grid
  // could only ever stack full-width.
  return (
        <section className="settings-card wiz-card">
          <div className="settings-card-head">
            <Icon name="gitBranch" />
            <div>
              <h4>Configured projects</h4>
              <p className="settings-card-sub">
                Each project is a live repo plus a git worktree the agent builds in.
              </p>
            </div>
          </div>

          {removed && (
            <div className="wiz-removed" role="status">
              <p>
                <strong>{removed.name}</strong> is no longer configured.
                {removed.archive
                  ? <> Its memory is archived as <code>{removed.archive}</code>.</>
                  : <> Its memory was deleted.</>}
                {removed.live_removed
                  ? <> <code>{removed.live_removed}</code> was deleted; everything in it was on its remote.</>
                  : <> <code>{removed.live_untouched}</code> was not touched — no files, no branches, no remote.</>}
              </p>
              <StepList steps={removed.steps} />
              <button type="button" className="wiz-btn" onClick={() => setRemoved(null)}>Dismiss</button>
            </div>
          )}

          <ul className="wiz-existing">
            {Object.entries(existing).map(([name, cfg]) => (
              <li key={name} className="wiz-existing-item">
                {/* Remove sits on the row, not inside it. It used to be
                    reachable only by expanding the project first, with
                    nothing on the row saying it was in there -- so the only
                    way anyone found it was by already knowing. */}
                <div className="wiz-existing-head">
                  <button
                    type="button"
                    className="wiz-existing-row"
                    aria-expanded={expanded === name}
                    onClick={() => setExpanded(expanded === name ? null : name)}
                  >
                    <Icon name={expanded === name ? "chevronDown" : "chevronRight"} />
                    <strong>{name}</strong>
                    <code>{cfg.live}</code>
                  </button>
                  <button
                    type="button"
                    className="wiz-existing-remove"
                    aria-label={`Remove ${name} from Persei`}
                    onClick={() => { setExpanded(name); setRemoving(name); }}
                  >
                    Remove
                  </button>
                </div>
                {expanded === name && (
                  <>
                    <form
                      className="wiz-move"
                      onSubmit={async (e) => {
                        e.preventDefault();
                        const next = moveTo.trim();
                        if (!next) return;
                        setMoveError(null);
                        try {
                          const r = await moveProject(name, next);
                          setMoveTo("");
                          setMoveNote(r.unchanged ? "That is already its path." : `${name} now works in ${r.live}.`);
                          await load();
                          await onChanged?.();
                        } catch (err) {
                          setMoveError(err instanceof Error ? err.message : "could not change the path");
                        }
                      }}
                    >
                      <label className="wiz-move-label" htmlFor={`move-${name}`}>Change path</label>
                      <input
                        id={`move-${name}`}
                        className="wiz-move-input"
                        spellCheck={false}
                        placeholder={cfg.live}
                        value={moving === name ? moveTo : ""}
                        onFocus={() => { setMoving(name); setMoveNote(null); }}
                        onChange={(e) => { setMoving(name); setMoveTo(e.target.value); }}
                      />
                      <button type="submit" className="btn btn--sm" disabled={moving !== name || !moveTo.trim()}>Move</button>
                      <span className="wiz-move-hint">Another checkout of the same repository. Nothing on disk moves.</span>
                      {moving === name && moveError && <span className="wiz-move-error">{moveError}</span>}
                      {moving === name && moveNote && <span className="wiz-move-note">{moveNote}</span>}
                    </form>
                    <DeployKeyCard project={name} />
                    <RemoveProject
                      name={name}
                      live={cfg.live}
                      open={removing === name}
                      onOpenChange={(v) => setRemoving(v ? name : null)}
                      onRemoved={async (res) => {
                        setRemoved(res);
                        // Both: this panel's own table, and App's repo list,
                        // which feeds the planner and task composer dropdowns.
                        // Refreshing only the first leaves a removed project
                        // selectable everywhere else until a page reload.
                        setExpanded(null);
                        setRemoving(null);
                        await load();
                        await loadArchives();
                        await onChanged?.();
                      }}
                    />
                  </>
                )}
              </li>
            ))}
            {!Object.keys(existing).length && <li className="wiz-empty">No projects configured yet.</li>}
          </ul>

          {archives.length > 0 && (
            <div className="wiz-archives">
              <h4>Archived memory</h4>
              <p className="settings-card-sub">
                What removed projects knew. Adding a project of the same name again offers to
                restore the matching archive.
              </p>
              <ul className="wiz-archive-list">
                {archives.map((a) => (
                  <li key={a.file}>
                    <div className="wiz-archive-body">
                      <strong>{a.project}</strong>
                      <code>{a.file}</code>
                      <span className="wiz-archive-meta">
                        {a.item_count} item{a.item_count === 1 ? "" : "s"} · {a.archived_at}
                      </span>
                    </div>
                    <button
                      type="button"
                      className="wiz-btn"
                      onClick={async () => {
                        try {
                          await deleteProjectArchive(a.file);
                          await loadArchives();
                        } catch (e) {
                          setError(e instanceof Error ? e.message : "could not delete the archive");
                        }
                      }}
                    >
                      Delete
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {stage === "idle" && (
            <div className="wiz-row">
              <input
                className="wiz-input"
                placeholder="/absolute/path/to/your/repo  or  https://github.com/owner/repo"
                value={path}
                onChange={(e) => setPath(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter" && path.trim()) void handleDetect(); }}
              />
              {/* The label says which of the two things is about to happen.
                  Inspect promises to create nothing; cloning plainly does. */}
              <button className="wiz-btn wiz-btn--primary" disabled={!path.trim()} onClick={() => void handleDetect()}>
                {looksLikeGitHubUrl(path) ? "Clone" : "Inspect"}
              </button>
            </div>
          )}

          {stage === "detecting" && (
            <p className="wiz-status">
              {looksLikeGitHubUrl(path) ? `Cloning ${path}…` : `Inspecting ${path}…`}
            </p>
          )}
          {error && <p className="wiz-error">{error}</p>}

          {stage === "review" && report && (
            <div className="wiz-review">
              <div className="wiz-summary">
                <span><strong>{report.name}</strong></span>
                <span>{report.languages.join(", ") || "unknown stack"}
                  {report.package_manager ? ` · ${report.package_manager}` : ""}</span>
                <span className="wiz-path">worktree → {report.sandbox}</span>
              </div>

              {(report.archives?.length ?? 0) > 0 && (
                <div className="wiz-restore">
                  <p className="wiz-restore-head">
                    A project called <strong>{report.name}</strong> was removed earlier and its
                    memory was kept.
                  </p>
                  <label className="wiz-restore-opt">
                    <input
                      type="radio"
                      name="restore"
                      checked={restoreFrom === null}
                      onChange={() => setRestoreFrom(null)}
                    />
                    <span>Start fresh — leave the archive alone</span>
                  </label>
                  {report.archives?.map((a) => (
                    <label key={a.file} className="wiz-restore-opt">
                      <input
                        type="radio"
                        name="restore"
                        checked={restoreFrom === a.file}
                        onChange={() => setRestoreFrom(a.file)}
                      />
                      <span>
                        Restore {a.item_count} item{a.item_count === 1 ? "" : "s"} from{" "}
                        <code>{a.file}</code>
                        <em> ({a.archived_at})</em>
                      </span>
                    </label>
                  ))}
                </div>
              )}

              {report.blockers.map((b) => <p key={b} className="wiz-blocker">{b}</p>)}
              {report.warnings.map((w) => <p key={w} className="wiz-warn">{w}</p>)}

              {!blocked && (
                <>
                  <div className="wiz-group">
                    <div className="wiz-group-head">
                      <span className="wiz-group-title">Checks the review gate will run</span>
                      <span className="wiz-group-help">Taken from the repo's own scripts and manifests.</span>
                    </div>
                    {report.checks.length ? report.checks.map((c) => (
                      <div key={c.name} className="wiz-cmd">
                        <code>{c.name}</code>
                        <span>{c.cmd} {c.args.join(" ")}</span>
                      </div>
                    )) : <p className="wiz-empty">None detected — changes will ship on human review alone.</p>}
                  </div>

                  <CandidateList
                    title="Test commands that make network calls"
                    help="Enable only if you know they don't touch production. A whole suite is listed when the stack has no per-suite script names (Go, Rust, Ruby, pytest)."
                    items={report.risky_scripts} chosen={risky}
                    onToggle={(v, on) => setRisky((s) => ({ ...s, [v]: on }))}
                  />
                  <CandidateList
                    title="Secret files to copy into review checkouts"
                    help="Gitignored, so they never arrive with the code."
                    items={report.secret_files} chosen={secrets}
                    onToggle={(v, on) => setSecrets((s) => ({ ...s, [v]: on }))}
                  />
                  <CandidateList
                    title="Read-only mounts"
                    help="Gitignored fixture directories some suites need."
                    items={report.read_only_mounts} chosen={mounts}
                    onToggle={(v, on) => setMounts((s) => ({ ...s, [v]: on }))}
                  />
                  <CandidateList
                    title="Restart on deploy"
                    help="pm2 apps serving this path."
                    items={report.pm2_apps} chosen={apps}
                    onToggle={(v, on) => setApps((s) => ({ ...s, [v]: on }))}
                  />
                  {(report.restart_commands ?? []).length > 0 && (
                    <CandidateList
                      title="Or run this to restart it"
                      help="Found on this machine. Only offered for a project this box actually runs."
                      items={report.restart_commands} chosen={restarts}
                      onToggle={(v, on) => setRestarts((s) => ({ ...s, [v]: on }))}
                    />
                  )}
                  {/* Onboarding used to end without ever saying what a merge
                      would do. Somebody whose project is not a pm2 app got a
                      merge and nothing else, and no sentence told them. */}
                  <p className="wiz-outcome">
                    After a passing review: <b>{deployOutcome(apps, restarts)}</b>
                  </p>
                </>
              )}

              <div className="wiz-row wiz-row--end">
                <button className="wiz-btn" onClick={reset}>Cancel</button>
                <button className="wiz-btn wiz-btn--primary" disabled={blocked} onClick={() => void handleProvision()}>
                  Create project
                </button>
              </div>
            </div>
          )}

          {stage === "provisioning" && <p className="wiz-status">Provisioning {report?.name}…</p>}

          {stage === "done" && (
            <div className="wiz-review">
              <StepList steps={steps} />
              {doneMsg && <p className="wiz-status">{doneMsg}</p>}
              {report && steps.some((s) => s.step === "config" && s.ok) && (
                <>
                  <p className="wiz-status">
                    Last step: give this project push access, so approved merges reach your
                    remote. Merges work without it — they just stay local.
                  </p>
                  <DeployKeyCard project={report.name} />
                </>
              )}
              <div className="wiz-row wiz-row--end">
                <button className="wiz-btn wiz-btn--primary" onClick={reset}>Done</button>
              </div>
            </div>
          )}
        </section>
  );
}
