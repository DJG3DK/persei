import { useMemo, useState } from "react";
import { relativeTime } from "../format";
import { deletePlanningSession, deleteTask } from "../api";
import type { CurrentUser, PlanningSessionMeta, TaskMeta } from "../types";
import { CATEGORY_LABELS, CATEGORY_ORDER } from "../categories";
import { BalanceStrip } from "./BalanceStrip";
import { RepoBadge } from "./RepoBadge";
import { StatusBadge } from "./StatusBadge";
import "./Sidebar.css";
import logoUrl from "../assets/persei-logo.png";
import { Icon } from "./Icon";
import { useGitHubInboxCount } from "../useGitHubInboxCount";

function categoryOf(t: TaskMeta): string {
  return t.category && CATEGORY_ORDER.includes(t.category) ? t.category : "other";
}

function planningCategoryOf(s: PlanningSessionMeta): string {
  return s.category && CATEGORY_ORDER.includes(s.category) ? s.category : "other";
}

interface Props {
  tasks: TaskMeta[];
  selectedTaskId: string | null;
  planningSessions: PlanningSessionMeta[];
  selectedPlanningSessionId: string | null;
  view: "new-task" | "task" | "analytics" | "benchmarks" | "models" | "planning" | "users" | "settings" | "github";
  user: CurrentUser;
  onSelect: (task: TaskMeta) => void;
  onNewTask: () => void;
  onAnalytics: () => void;
  onBenchmarks: () => void;
  onModels: () => void;
  onUsers: () => void;
  onSettings: () => void;
  onGitHub: () => void;
  onSelectPlanning: (session: PlanningSessionMeta) => void;
  onNewPlanning: () => void;
  onDeleted: (taskId: string) => void;
  onPlanningDeleted: (sessionId: string) => void;
  onLogout: () => void;
}

function TaskRow({
  t,
  selected,
  onSelect,
  onDelete,
  deleting,
}: {
  t: TaskMeta;
  selected: boolean;
  onSelect: () => void;
  onDelete: (e: React.MouseEvent) => void;
  deleting: boolean;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      className={`sidebar-item ${selected ? "selected" : ""}`}
      onClick={onSelect}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect()}
    >
      <div className="sidebar-item-top">
        <RepoBadge repo={t.repo} />
        <span className="sidebar-item-top-right">
          <span className="sidebar-item-time">{relativeTime(t.created_at)}</span>
          {t.status !== "running" && (
            <button className="sidebar-item-delete" title="Delete task" disabled={deleting} onClick={onDelete}>
              {deleting ? "…" : "×"}
            </button>
          )}
        </span>
      </div>
      <div className="sidebar-item-goal">{t.goal}</div>
      <div className="sidebar-item-bottom">
        <StatusBadge status={t.status} />
        {t.cost_so_far !== undefined && (
          <span
            className="sidebar-item-cost"
            title={t.planning_cost_usd ? `build $${t.cost_so_far.toFixed(2)} + planning $${t.planning_cost_usd.toFixed(2)}` : undefined}
          >
            ${(t.cost_so_far + (t.planning_cost_usd ?? 0)).toFixed(2)}
          </span>
        )}
      </div>
    </div>
  );
}

function PlanningRow({ s, selected, onSelect, onDelete, deleting }: {
  s: PlanningSessionMeta;
  selected: boolean;
  onSelect: () => void;
  onDelete: (e: React.MouseEvent) => void;
  deleting: boolean;
}) {
  return (
    <div
      role="button"
      tabIndex={0}
      className={`sidebar-item ${selected ? "selected" : ""}`}
      onClick={onSelect}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect()}
    >
      <div className="sidebar-item-top">
        <RepoBadge repo={s.repo} />
        <span className="sidebar-item-top-right">
          <span className="sidebar-item-time">{relativeTime(s.updated_at)}</span>
          <button className="sidebar-item-delete" title="Delete plan" disabled={deleting} onClick={onDelete}>
            {deleting ? "…" : "×"}
          </button>
        </span>
      </div>
      <div className="sidebar-item-goal">{s.title || "New session"}</div>
      <div className="sidebar-item-bottom">
        {s.plan_markdown && <span className="sidebar-plan-dot" title="Has a saved plan" />}
        <span className="sidebar-item-cost">${s.cost_usd.toFixed(3)}</span>
      </div>
    </div>
  );
}

/** Waiting on the operator: an escalation to read, or a diff to approve. */
function needsOperator(status: TaskMeta["status"]): boolean {
  return status === "escalated" || status === "awaiting_merge" || status === "awaiting_approval";
}

function SectionHeader({
  label,
  count,
  open,
  onToggle,
  action,
}: {
  label: string;
  count: number;
  open: boolean;
  onToggle: () => void;
  action?: React.ReactNode;
}) {
  return (
    <div className="sidebar-section-head" role="button" tabIndex={0} onClick={onToggle}
         onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), onToggle())}>
      <span className={`sidebar-chevron ${open ? "open" : ""}`}>▸</span>
      <span className="sidebar-section-label">{label}</span>
      <span className="sidebar-section-count">{count}</span>
      {action && (
        <span
          className="sidebar-section-action"
          onClick={(e) => {
            e.stopPropagation();
          }}
        >
          {action}
        </span>
      )}
    </div>
  );
}

export function Sidebar({
  tasks,
  selectedTaskId,
  planningSessions,
  selectedPlanningSessionId,
  view,
  user,
  onSelect,
  onNewTask,
  onAnalytics,
  onBenchmarks,
  onModels,
  onUsers,
  onSettings,
  onGitHub,
  onSelectPlanning,
  onNewPlanning,
  onDeleted,
  onPlanningDeleted,
  onLogout,
}: Props) {
  // Re-read when the view changes: approving an item and going back to the
  // task list should clear the badge then, not up to 45 seconds later.
  const inboxCount = useGitHubInboxCount(view);
  const [deletingId, setDeletingId] = useState<string | null>(null);
  const [planningOpen, setPlanningOpen] = useState(true);
  const [buildingOpen, setBuildingOpen] = useState(true);
  const [planningQuery, setPlanningQuery] = useState("");
  const [buildingQuery, setBuildingQuery] = useState("");
  // Explicit user intent per category, not a set of "additionally expanded"
  // ones. The old shape could only ever ADD expansion, so any auto-expand
  // rule OR-ed alongside it could not be switched off -- clicking to collapse
  // did nothing at all. An entry here always wins; absent means "no opinion
  // yet, use the default".
  const [categoryOverride, setCategoryOverride] = useState<Record<string, boolean>>({});
  const [planningCategoryOverride, setPlanningCategoryOverride] = useState<Record<string, boolean>>({});
  const [archivedPlanningOpen, setArchivedPlanningOpen] = useState(false);
  // Project separation (operator request 2026-08-27): one filter, applied to
  // BOTH lists, rather than a second level of nesting inside the category
  // groups -- the categories stay (they were their own request), and "show
  // me one project" is a click instead of a fold hierarchy.
  const [repoFilter, setRepoFilter] = useState<string | null>(null);
  const knownRepos = useMemo(() => {
    const rs = new Set<string>();
    for (const t of tasks) rs.add(t.repo);
    for (const ps of planningSessions) rs.add(ps.repo);
    return [...rs].sort();
  }, [tasks, planningSessions]);

  async function handlePlanningDelete(e: React.MouseEvent, s: PlanningSessionMeta) {
    e.stopPropagation();
    const label = s.title || "New session";
    if (!confirm(
      `Delete this plan?\n\n"${label.slice(0, 80)}${label.length > 80 ? "…" : ""}"\n\n` +
      `This removes the conversation and any saved plan. Archive instead if you might come back to it.`,
    )) {
      return;
    }
    setDeletingId(s.session_id);
    try {
      await deletePlanningSession(s.session_id);
      onPlanningDeleted(s.session_id);
    } catch (err) {
      alert(err instanceof Error ? err.message : "delete failed");
    } finally {
      setDeletingId(null);
    }
  }

  async function handleDelete(e: React.MouseEvent, t: TaskMeta) {
    e.stopPropagation();
    if (t.status === "running") return; // backend refuses this anyway — button is disabled below
    if (!confirm(`Delete this task from the list?\n\n"${t.goal.slice(0, 80)}${t.goal.length > 80 ? "…" : ""}"\n\nThis removes its full history — it can't be resumed after.`)) {
      return;
    }
    setDeletingId(t.task_id);
    try {
      await deleteTask(t.task_id, t.repo);
      onDeleted(t.task_id);
    } catch (err) {
      alert(err instanceof Error ? err.message : "delete failed");
    } finally {
      setDeletingId(null);
    }
  }

  const filteredSessions = useMemo(() => {
    const q = planningQuery.trim().toLowerCase();
    const matches = (s: PlanningSessionMeta) =>
      (!repoFilter || s.repo === repoFilter) &&
      (!q || (s.title ?? "").toLowerCase().includes(q) || s.repo.toLowerCase().includes(q));
    const active = planningSessions.filter((s) => !s.archived && matches(s));
    const archived = planningSessions.filter((s) => s.archived && matches(s));
    return { active, archived };
  }, [planningSessions, planningQuery, repoFilter]);

  // A session stays in this ungrouped list until it has a SAVED PLAN, then
  // settles into its category group.
  //
  // It used to move as soon as it got a title, i.e. after the very first
  // reply -- which meant the sidebar reshuffled at the exact moment the
  // operator was reading that reply, dropping the session next to an older
  // one on the same subject that already had a "Build now" button. The flick
  // plus the neighbouring button read as "your plan is ready" when the agent
  // had actually just asked a question (operator report, 2026-08-29).
  //
  // Now the move happens once, when the session becomes buildable, so it
  // means something. `plan_markdown` is the same field that gates the Build
  // button, so the two can never disagree.
  const inProgressPlanningSessions = useMemo(
    () => filteredSessions.active.filter((s) => !s.plan_markdown),
    [filteredSessions.active],
  );

  const planningByCategory = useMemo(() => {
    const groups: Record<string, PlanningSessionMeta[]> = {};
    for (const cat of CATEGORY_ORDER) groups[cat] = [];
    for (const s of filteredSessions.active) {
      // No saved plan -> it belongs to the in-progress list above, never both
      // places (see inProgressPlanningSessions).
      if (!s.plan_markdown) continue;
      groups[planningCategoryOf(s)].push(s);
    }
    return groups;
  }, [filteredSessions.active]);

  // Takes what is currently on screen and inverts it, so one click always
  // does the visible thing regardless of which default produced that state.
  function togglePlanningCategory(cat: string, isExpanded: boolean) {
    setPlanningCategoryOverride((prev) => ({ ...prev, [cat]: !isExpanded }));
  }

  // Running tasks get their own always-visible group instead of sitting
  // inside a (possibly collapsed) category -- a page refresh resets which
  // category was auto-expanded (that tracked the *selected* task, which
  // resets too), so a running task could otherwise take real hunting to
  // find again after nothing more than a reload.
  // Queued tasks sit here too. They are in-flight from the operator's point
  // of view -- started, not finished, nothing to do but wait -- and burying
  // one in a collapsed category is how you end up wondering why the project
  // seems idle.
  const runningTasks = useMemo(() => {
    const q = buildingQuery.trim().toLowerCase();
    return tasks.filter((t) =>
      (t.status === "running" || t.status === "queued") &&
      (!repoFilter || t.repo === repoFilter) &&
      (!q || t.goal.toLowerCase().includes(q) || t.repo.toLowerCase().includes(q)));
  }, [tasks, buildingQuery, repoFilter]);

  // Tasks waiting on the operator -- an escalation to read, a diff to
  // approve -- get their own always-visible group too. Filed into a
  // category they were as good as lost: the category is collapsed, the
  // badge is small, and the one task that needs a decision looks like the
  // finished ones around it. A task moves into a category only when it is
  // over (2026-09-29).
  const needsYouTasks = useMemo(() => {
    const q = buildingQuery.trim().toLowerCase();
    return tasks.filter((t) =>
      needsOperator(t.status) &&
      (!repoFilter || t.repo === repoFilter) &&
      (!q || t.goal.toLowerCase().includes(q) || t.repo.toLowerCase().includes(q)));
  }, [tasks, buildingQuery, repoFilter]);

  const byCategory = useMemo(() => {
    const q = buildingQuery.trim().toLowerCase();
    const groups: Record<string, TaskMeta[]> = {};
    for (const cat of CATEGORY_ORDER) groups[cat] = [];
    for (const t of tasks) {
      if (t.status === "running" || t.status === "queued") continue; // shown in the Running group instead, never both places
      if (needsOperator(t.status)) continue;                          // shown in the Needs you group instead
      if (repoFilter && t.repo !== repoFilter) continue;
      if (q && !t.goal.toLowerCase().includes(q) && !t.repo.toLowerCase().includes(q)) continue;
      groups[categoryOf(t)].push(t);
    }
    return groups;
  }, [tasks, buildingQuery, repoFilter]);

  function toggleCategory(cat: string, isExpanded: boolean) {
    setCategoryOverride((prev) => ({ ...prev, [cat]: !isExpanded }));
  }

  const searching = buildingQuery.trim().length > 0;
  // Deliberately null for a RUNNING task: running tasks are filtered out of
  // byCategory and shown in the Running group instead, so opening "their"
  // category revealed a list they are not even in -- and, before the override
  // fix above, one that could not then be closed.
  const selectedTaskMeta =
    view === "task" && selectedTaskId ? tasks.find((t) => t.task_id === selectedTaskId) : undefined;
  const selectedCategory =
    selectedTaskMeta && selectedTaskMeta.status !== "running" && !needsOperator(selectedTaskMeta.status)
      ? categoryOf(selectedTaskMeta) : null;

  const planningSearching = planningQuery.trim().length > 0;
  const selectedPlanningCategory =
    view === "planning" && selectedPlanningSessionId
      ? planningCategoryOf(planningSessions.find((s) => s.session_id === selectedPlanningSessionId) ?? ({} as PlanningSessionMeta))
      : null;

  return (
    <aside className="sidebar">
      <div className="sidebar-header">
        <div className="sidebar-logo">
          {/* The Persei lockup -- the gold diamond P plus the wordmark,
              replacing the placeholder diamond glyph. */}
          {/* Imported through vite (not public/): the backend only mounts
              /assets/*, so a root-level file fell through to the SPA
              catch-all and served HTML as the image (seen live). Bundling
              gives it a hashed /assets URL the existing mount serves. */}
          <img className="sidebar-logo-img" src={logoUrl} alt="Persei" />
        </div>
        {/* Plan-first (operator decision 2026-08-28): "we do not go directly
            to tasks... Planning chat should always happen first." The primary
            action starts a planning session; Build Now hands the finished
            plan to the build pipeline. The raw task composer stays reachable
            from the Building section header for the rare direct case. */}
        <button className="new-task-btn" onClick={onNewPlanning}>
          <Icon name="plus" size={16} />
          <span>New Plan</span>
        </button>
        {user.role === "admin" && (
          <>
            <button className={`analytics-nav-btn ${view === "analytics" ? "active" : ""}`} onClick={onAnalytics}>
              <Icon name="chart" size={15} />
              <span>Analytics</span>
            </button>
            <button className={`analytics-nav-btn ${view === "benchmarks" ? "active" : ""}`} onClick={onBenchmarks}>
              <Icon name="check" size={15} />
              <span>Benchmarks</span>
            </button>
            <button className={`analytics-nav-btn ${view === "models" ? "active" : ""}`} onClick={onModels}>
              <Icon name="cpu" size={15} />
              <span>Models</span>
            </button>
            {/* Not gated on the multi-user licence: the accounts that exist
                are managed here whatever the licence says (App.tsx). */}
            <button className={`analytics-nav-btn ${view === "users" ? "active" : ""}`} onClick={onUsers}>
              <Icon name="users" size={15} />
              <span>Users</span>
            </button>
          </>
        )}
        <button className={`analytics-nav-btn ${view === "github" ? "active" : ""}`} onClick={onGitHub}>
          <Icon name="github" size={15} />
          <span>GitHub</span>
          {/* Items waiting on a decision, and only those -- see
              useGitHubInboxCount for why `seen` and `snoozed` are not counted.
              Rendered only when there is something, so the button looks
              exactly as it did before on a quiet day. */}
          {inboxCount != null && inboxCount > 0 && (
            <span className="nav-badge" aria-label={`${inboxCount} GitHub item(s) awaiting a decision`}>
              {inboxCount > 99 ? "99+" : inboxCount}
            </span>
          )}
        </button>
        {/* Was the same ⚙ glyph as Models — two nav items with identical icons.
            A cpu for the model pins, a cog for settings. */}
        <button className={`analytics-nav-btn ${view === "settings" ? "active" : ""}`} onClick={onSettings}>
          <Icon name="settings" size={15} />
          <span>Settings</span>
        </button>
      </div>

      <div className="sidebar-list">
        {knownRepos.length > 1 && (
          <div className="sidebar-repo-filter">
            <button
              className={`sidebar-repo-chip ${repoFilter === null ? "active" : ""}`}
              onClick={() => setRepoFilter(null)}
            >
              All
            </button>
            {knownRepos.map((r) => (
              <button
                key={r}
                className={`sidebar-repo-chip ${repoFilter === r ? "active" : ""}`}
                onClick={() => setRepoFilter(repoFilter === r ? null : r)}
              >
                {r}
              </button>
            ))}
          </div>
        )}
        <div className="sidebar-section">
          <SectionHeader
            label="Planning"
            count={filteredSessions.active.length}
            open={planningOpen}
            onToggle={() => setPlanningOpen((o) => !o)}
            action={
              <button className="sidebar-section-new" onClick={onNewPlanning} title="New planning session">
                +
              </button>
            }
          />
          {planningOpen && (
            <div className="sidebar-section-body">
              {planningSessions.length > 3 && (
                <input
                  className="sidebar-search"
                  placeholder="Search planning..."
                  value={planningQuery}
                  onChange={(e) => setPlanningQuery(e.target.value)}
                />
              )}
              <div className="sidebar-scroll">
                {filteredSessions.active.length === 0 && <div className="sidebar-empty">No planning sessions yet</div>}
                {inProgressPlanningSessions.length > 0 && (
                  <div className="sidebar-section-head sidebar-section-head--plain">
                    <span className="sidebar-category-label">In progress</span>
                    <span className="sidebar-section-count">{inProgressPlanningSessions.length}</span>
                  </div>
                )}
                {inProgressPlanningSessions.map((s) => (
                  <PlanningRow
                    key={s.session_id}
                    s={s}
                    selected={s.session_id === selectedPlanningSessionId && view === "planning"}
                    onSelect={() => onSelectPlanning(s)}
                    onDelete={(e) => handlePlanningDelete(e, s)}
                    deleting={deletingId === s.session_id}
                  />
                ))}
                {CATEGORY_ORDER.map((cat) => {
                  const items = planningByCategory[cat];
                  if (items.length === 0 && !planningSearching) return null;
                  const expanded =
                    planningSearching || (planningCategoryOverride[cat] ?? selectedPlanningCategory === cat);
                  return (
                    <div key={cat} className="sidebar-category">
                      <div className="sidebar-category-head" role="button" tabIndex={0} onClick={() => togglePlanningCategory(cat, expanded)}
                           onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), togglePlanningCategory(cat, expanded))}>
                        <span className={`sidebar-chevron ${expanded ? "open" : ""}`}>▸</span>
                        <span className="sidebar-category-label">{CATEGORY_LABELS[cat] ?? cat}</span>
                        <span className="sidebar-section-count">{items.length}</span>
                      </div>
                      {expanded && (
                        <div className="sidebar-category-body">
                          {items.length === 0 && <div className="sidebar-empty sidebar-empty--small">no matches</div>}
                          {items.map((s) => (
                            <PlanningRow
                              key={s.session_id}
                              s={s}
                              selected={s.session_id === selectedPlanningSessionId && view === "planning"}
                              onSelect={() => onSelectPlanning(s)}
                              onDelete={(e) => handlePlanningDelete(e, s)}
                              deleting={deletingId === s.session_id}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
                {filteredSessions.archived.length > 0 && (
                  <div className="sidebar-category">
                    <div className="sidebar-category-head" role="button" tabIndex={0} onClick={() => setArchivedPlanningOpen((o) => !o)}
                         onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), setArchivedPlanningOpen((o) => !o))}>
                      <span className={`sidebar-chevron ${archivedPlanningOpen ? "open" : ""}`}>▸</span>
                      <span className="sidebar-category-label">Archived</span>
                      <span className="sidebar-section-count">{filteredSessions.archived.length}</span>
                    </div>
                    {archivedPlanningOpen && (
                      <div className="sidebar-category-body">
                        {filteredSessions.archived.map((s) => (
                          <PlanningRow
                            key={s.session_id}
                            s={s}
                            selected={s.session_id === selectedPlanningSessionId && view === "planning"}
                            onSelect={() => onSelectPlanning(s)}
                            onDelete={(e) => handlePlanningDelete(e, s)}
                            deleting={deletingId === s.session_id}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <div className="sidebar-section">
          <SectionHeader
            label="Building"
            count={tasks.length}
            open={buildingOpen}
            onToggle={() => setBuildingOpen((o) => !o)}
            action={
              <button className="sidebar-section-new" onClick={onNewTask} title="Start a build task directly (skips planning)">
                +
              </button>
            }
          />
          {buildingOpen && (
            <div className="sidebar-section-body">
              {tasks.length > 5 && (
                <input
                  className="sidebar-search"
                  placeholder="Search tasks..."
                  value={buildingQuery}
                  onChange={(e) => setBuildingQuery(e.target.value)}
                />
              )}
              <div className="sidebar-scroll">
                {tasks.length === 0 && <div className="sidebar-empty">No tasks yet</div>}
                {runningTasks.length > 0 && (
                  <div className="sidebar-category sidebar-running-group">
                    <div className="sidebar-category-head sidebar-running-head">
                      <span className="sidebar-running-dot" />
                      <span className="sidebar-category-label">Running</span>
                      <span className="sidebar-section-count">{runningTasks.length}</span>
                    </div>
                    <div className="sidebar-category-body">
                      {runningTasks.map((t) => (
                        <TaskRow
                          key={t.task_id}
                          t={t}
                          selected={t.task_id === selectedTaskId && view === "task"}
                          onSelect={() => onSelect(t)}
                          onDelete={(e) => handleDelete(e, t)}
                          deleting={deletingId === t.task_id}
                        />
                      ))}
                    </div>
                  </div>
                )}
                {needsYouTasks.length > 0 && (
                  <div className="sidebar-category sidebar-running-group sidebar-needs-you-group">
                    <div className="sidebar-category-head sidebar-running-head">
                      <span className="sidebar-running-dot sidebar-needs-you-dot" />
                      <span className="sidebar-category-label">Needs you</span>
                      <span className="sidebar-section-count">{needsYouTasks.length}</span>
                    </div>
                    <div className="sidebar-category-body">
                      {needsYouTasks.map((t) => (
                        <TaskRow
                          key={t.task_id}
                          t={t}
                          selected={t.task_id === selectedTaskId && view === "task"}
                          onSelect={() => onSelect(t)}
                          onDelete={(e) => handleDelete(e, t)}
                          deleting={deletingId === t.task_id}
                        />
                      ))}
                    </div>
                  </div>
                )}
                {CATEGORY_ORDER.map((cat) => {
                  const items = byCategory[cat];
                  // Zero-filled taxonomy is useful in Analytics (a category is
                  // a fixed concept worth always showing); here it would just
                  // be dead weight in an already-long list, so an empty
                  // category collapses away entirely unless a search is
                  // active (searching shows the "0 matches" state instead).
                  if (items.length === 0 && !searching) return null;
                  // Search wins outright -- it is a transient view whose whole
                  // job is to show matches. Otherwise the user's own choice
                  // wins, falling back to "open the selected task's category"
                  // only until they express one.
                  const expanded = searching || (categoryOverride[cat] ?? selectedCategory === cat);
                  return (
                    <div key={cat} className="sidebar-category">
                      <div className="sidebar-category-head" role="button" tabIndex={0} onClick={() => toggleCategory(cat, expanded)}
                           onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && (e.preventDefault(), toggleCategory(cat, expanded))}>
                        <span className={`sidebar-chevron ${expanded ? "open" : ""}`}>▸</span>
                        <span className="sidebar-category-label">{CATEGORY_LABELS[cat] ?? cat}</span>
                        <span className="sidebar-section-count">{items.length}</span>
                      </div>
                      {expanded && (
                        <div className="sidebar-category-body">
                          {items.length === 0 && <div className="sidebar-empty sidebar-empty--small">no matches</div>}
                          {items.map((t) => (
                            <TaskRow
                              key={t.task_id}
                              t={t}
                              selected={t.task_id === selectedTaskId && view === "task"}
                              onSelect={() => onSelect(t)}
                              onDelete={(e) => handleDelete(e, t)}
                              deleting={deletingId === t.task_id}
                            />
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      </div>

      <div className="sidebar-user-footer">
        <span className="sidebar-user-email" title={user.email}>{user.email}</span>
        <button className="sidebar-logout-btn" onClick={onLogout}>
          Log out
        </button>
      </div>
      <BalanceStrip isAdmin={user.role === "admin"} />
    </aside>
  );
}
