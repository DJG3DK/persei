import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import { createTask, getGitHubSettings, getMe, listPlanningSessions, listRepos, listTasks, logout, setAuthFailureHandler, uploadFiles } from "./api";
import { ChangePasswordPage } from "./components/ChangePasswordPage";
import { LoginPage } from "./components/LoginPage";
import { ModelConfigPanel } from "./components/ModelConfigPanel";
import { ConsolidationStatusPanel } from "./components/ConsolidationStatusPanel";
import { MobileNav } from "./components/MobileNav";
import { Icon } from "./components/Icon";
import { NewTaskPanel } from "./components/NewTaskPanel";
import { PlanningView } from "./components/PlanningView";
import { SettingsPage } from "./components/SettingsPage";
import { GitHubInboxView } from "./components/GitHubInboxView";
import { SetupTotpPage } from "./components/SetupTotpPage";
import { Sidebar } from "./components/Sidebar";
import { TaskView } from "./components/TaskView";
import { UsersPanel } from "./components/UsersPanel";
import type { CurrentUser, PlanningSessionMeta, TaskMeta } from "./types";
import { applyTheme, isThemeId, storedTheme } from "./themes";
// audit M-23: recharts (~the bulk of the bundle) now ships in its own chunk,
// fetched only when an admin opens Analytics.
const AnalyticsView = lazy(() =>
  import("./components/AnalyticsView").then((m) => ({ default: m.AnalyticsView })),
);
// Benchmarks is its own page (2026-09-26): the golden suite and SWE-bench
// used to sit under Analytics, below the fold of a page about spend.
const BenchmarksView = lazy(() =>
  import("./components/BenchmarksView").then((m) => ({ default: m.BenchmarksView })),
);
import { useTaskStream } from "./useTaskStream";
import { parseRoute, routePath, sameRoute, type Route, type View } from "./route";
import { TitleBar } from "./components/TitleBar";
import "./App.css";

function AuthenticatedApp({ user, onLogout, onUserChanged }: { user: CurrentUser; onLogout: () => void; onUserChanged: (u: CurrentUser) => void }) {
  const [repos, setRepos] = useState<string[]>([]);
  const [tasks, setTasks] = useState<TaskMeta[]>([]);
  const [selected, setSelected] = useState<TaskMeta | null>(null);
  // `selected` is the record as it was when clicked. The list behind it
  // refreshes every few seconds, and a resume with a budget top-up changes
  // the record: the page showed "$5.15 of $5" for a task whose budget was
  // $7 (2026-09-29). The task page reads the live record when there is one.
  const liveSelected = selected ? (tasks.find((t) => t.task_id === selected.task_id) ?? selected) : null;
  const [planningSessions, setPlanningSessions] = useState<PlanningSessionMeta[]>([]);
  const [selectedPlanningSession, setSelectedPlanningSession] = useState<PlanningSessionMeta | null>(null);
  // The URL says where to start (see route.ts). With no path it is Planning:
  // plan-first (2026-08-28), a fresh session panel rather than the raw task
  // composer -- planning chat always happens first; Build Now makes tasks.
  const [initialRoute] = useState<Route>(() => parseRoute(window.location.pathname));
  const [view, setView] = useState<View>(initialRoute.view);
  // A task or session named by the URL, waiting for the lists to load so it
  // can be selected. Cleared once found; `missing` if the list came back
  // without it (deleted, or older than the recent-tasks window).
  const [pending, setPending] = useState<{ taskId?: string; sessionId?: string } | null>(
    initialRoute.taskId || initialRoute.sessionId
      ? { taskId: initialRoute.taskId, sessionId: initialRoute.sessionId } : null,
  );
  const [missing, setMissing] = useState<string | null>(null);
  const [tasksLoaded, setTasksLoaded] = useState(false);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);
  // Bumping this forces useTaskStream to open a fresh WS connection after a
  // resume, without wiping the log history the way switching to a different
  // task does — see the hook's own doc comment for why a resume needs this
  // (the server-side connection genuinely closes when a run finishes).
  const [generation, setGeneration] = useState(0);
  // Called unconditionally (not just while view === "task") so switching to
  // Analytics/Planning/etc. and back never drops the WS connection or the
  // log entries already streamed in -- see TaskView.tsx's own comment on why
  // that used to happen.
  const taskStream = useTaskStream(selected?.task_id ?? null, selected?.repo ?? null, generation);
  // Mobile only (<=768px, see App.css): which pane is visible. On desktop
  // both panes always render side by side and this class has no effect.
  // A link to something specific opens straight onto it.
  const [mobilePane, setMobilePane] = useState<"list" | "main">(
    window.location.pathname.replace(import.meta.env.BASE_URL || "/", "") ? "main" : "list",
  );

  const refreshTasks = useCallback(async () => {
    try {
      setTasks(await listTasks());
      setTasksLoaded(true);
    } catch {
      // Backend not reachable yet — sidebar just stays on its last known list.
    }
  }, []);

  const refreshPlanningSessions = useCallback(async () => {
    try {
      setPlanningSessions(await listPlanningSessions());
      setSessionsLoaded(true);
    } catch {
      // Same tolerance as refreshTasks -- sidebar just stays on its last known list.
    }
  }, []);

  // Repos come back already scoped to this user's own access (see
  // GET /api/repos) -- a restricted account simply never sees a project
  // it can't touch, no separate frontend filtering needed anywhere below.
  // Not on the 8s poll: the list only changes when a project is provisioned
  // (Settings -> Projects, or the planner's "New project…"), and both call
  // this directly, so a page reload is no longer the only way to see it.
  const refreshRepos = useCallback(async () => {
    try {
      setRepos(await listRepos());
    } catch {
      // Same tolerance as refreshTasks -- the dropdowns keep their last list.
    }
  }, []);

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- the load it starts is async; state lands after the await, not in the effect
    refreshRepos();
    refreshTasks();
    refreshPlanningSessions();
    const interval = setInterval(() => {
      refreshTasks();
      refreshPlanningSessions();
    }, 8000); // catches status/cost/title changes for anything other than the selected item
    return () => clearInterval(interval);
  }, [refreshRepos, refreshTasks, refreshPlanningSessions]);

  // URL -> selection: once the lists are in, select what the URL named.
  useEffect(() => {
    if (!pending) return;
    if (pending.taskId && tasksLoaded) {
      const t = tasks.find((x) => x.task_id === pending.taskId);
      // oxlint-disable-next-line react/set-state-in-effect -- resolves the deep link once the list it names has loaded; that load is the external event
      if (t) setSelected(t);
      else setMissing(`Task ${pending.taskId.slice(0, 8)} is not in your task list — it may have been deleted, or be older than the list goes back.`);
      setPending(null);
    } else if (pending.sessionId && sessionsLoaded) {
      const s = planningSessions.find((x) => x.session_id === pending.sessionId);
      if (s) setSelectedPlanningSession(s);
      else setMissing(`Planning session ${pending.sessionId.slice(0, 8)} is not in your list — it may have been deleted or archived.`);
      setPending(null);
    }
  }, [pending, tasks, planningSessions, tasksLoaded, sessionsLoaded]);

  // Selection -> URL. A real change pushes a history entry. A URL that
  // already MEANS this route in another spelling -- `/planning` for `/`, an
  // unknown path that lands on Planning -- is REPLACED with the canonical
  // one instead: pushing there made Back return to `/planning`, which pushed
  // `/` again, so Back could never get past it (2026-09-23 follow-up, F7).
  useEffect(() => {
    if (pending) return;   // still resolving what the URL asked for; leave it be
    const route: Route = {
      view,
      taskId: view === "task" ? selected?.task_id : undefined,
      sessionId: view === "planning" ? selectedPlanningSession?.session_id : undefined,
    };
    const path = routePath(route);
    if (path === window.location.pathname) return;
    if (sameRoute(parseRoute(window.location.pathname), route)) window.history.replaceState(null, "", path);
    else window.history.pushState(null, "", path);
  }, [view, selected?.task_id, selectedPlanningSession?.session_id, pending]);

  // Back / forward: apply the URL the browser moved to.
  const listsRef = useRef({ tasks, planningSessions });
  useEffect(() => {
    listsRef.current = { tasks, planningSessions };
  });
  useEffect(() => {
    const onPop = () => {
      const r = parseRoute(window.location.pathname);
      setMissing(null);
      // `selected` is deliberately KEPT when Back leaves the task view: the
      // task stream is held open across views (see useTaskStream above), and
      // every view but "task" ignores it. A view that starts reading it while
      // view !== "task" must clear it here first.
      setView(r.view);
      if (r.taskId) {
        const t = listsRef.current.tasks.find((x) => x.task_id === r.taskId);
        if (t) setSelected(t);
        else setPending({ taskId: r.taskId });
      }
      if (r.view === "planning") {
        if (r.sessionId) {
          const s = listsRef.current.planningSessions.find((x) => x.session_id === r.sessionId);
          if (s) setSelectedPlanningSession(s);
          else setPending({ sessionId: r.sessionId });
        } else {
          setSelectedPlanningSession(null);
        }
      }
      setMobilePane("main");
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  // Whether "create a private GitHub repo" can work at all: admins only,
  // asked once after sign-in. The endpoint is admin-only, so a restricted
  // account never calls it -- it would be a guaranteed 403 for a checkbox
  // that account cannot see anyway. Any failure just hides the checkbox.
  const isAdmin = user.role === "admin";
  const [githubReady, setGithubReady] = useState(false);
  useEffect(() => {
    if (!isAdmin) return;
    getGitHubSettings()
      .then((g) => setGithubReady(Boolean(g.env_token) || Object.keys(g.settings?.tokens ?? {}).length > 0))
      .catch(() => setGithubReady(false));
  }, [isAdmin]);

  async function handleCreate(goal: string, repo: string, budgetUsd: number, files: File[], route: "auto" | "frontend" | "general" = "auto", planningSessionId?: string) {
    setSubmitting(true);
    setCreateError(null);
    try {
      const attachments = files.length ? await uploadFiles(repo, files) : undefined;
      const created = await createTask(goal, repo, budgetUsd, attachments, route, planningSessionId);
      const { task_id } = created;
      const meta: TaskMeta = {
        task_id, goal, repo, budget_usd: budgetUsd, status: "running", created_at: Date.now() / 1000,
        route: created.route === "frontend" || created.route === "general" ? created.route : undefined,
        route_reason: created.route_reason ?? null,
      };
      setTasks((t) => [meta, ...t]);
      setSelected(meta);
      setView("task");
      setMobilePane("main");
    } catch (err) {
      // audit M-20: surface the failure instead of just flickering the button.
      // A 413 on a large attachment (or any create/upload error) now tells the
      // operator what happened rather than silently doing nothing.
      setCreateError(err instanceof Error ? err.message : "Failed to start the task. Please try again.");
    } finally {
      setSubmitting(false);
    }
  }

  const viewTitle =
    view === "task" ? "Task"
    : view === "analytics" ? "Analytics"
    : view === "benchmarks" ? "Benchmarks"
    : view === "models" ? "Models"
    : view === "planning" ? "Planning"
    : view === "users" ? "Users"
    : view === "settings" ? "Settings"
    : view === "github" ? "GitHub inbox"
    : "New task";

  return (
    <div className={`app-shell ${mobilePane === "main" ? "show-main" : "show-list"}`}>
      <Sidebar
        tasks={tasks}
        selectedTaskId={selected?.task_id ?? null}
        planningSessions={planningSessions}
        selectedPlanningSessionId={selectedPlanningSession?.session_id ?? null}
        view={view}
        user={user}
        onSelect={(t) => {
          setSelected(t);
          setView("task");
          setMobilePane("main");
        }}
        onNewTask={() => {
          setView("new-task");
          setMobilePane("main");
        }}
        onAnalytics={() => {
          setView("analytics");
          setMobilePane("main");
        }}
        onBenchmarks={() => {
          setView("benchmarks");
          setMobilePane("main");
        }}
        onModels={() => {
          setView("models");
          setMobilePane("main");
        }}
        onUsers={() => {
          setView("users");
          setMobilePane("main");
        }}
        onSettings={() => {
          setView("settings");
          setMobilePane("main");
        }}
        onGitHub={() => {
          setView("github");
          setMobilePane("main");
        }}
        onSelectPlanning={(s) => {
          setSelectedPlanningSession(s);
          setView("planning");
          setMobilePane("main");
        }}
        onNewPlanning={() => {
          setSelectedPlanningSession(null);
          setView("planning");
          setMobilePane("main");
        }}
        onDeleted={(taskId) => {
          setTasks((t) => t.filter((task) => task.task_id !== taskId));
          if (selected?.task_id === taskId) {
            setSelected(null);
            setView("planning");
            setMobilePane("list");
          }
        }}
        onPlanningDeleted={(sessionId) => {
          setPlanningSessions((list) => list.filter((s) => s.session_id !== sessionId));
          if (selectedPlanningSession?.session_id === sessionId) {
            // The open conversation just went away; drop the selection rather
            // than leaving the pane bound to a session the server no longer has.
            setSelectedPlanningSession(null);
            setMobilePane("list");
          }
        }}
        onLogout={onLogout}
      />
      <div className="main-pane">
        <div className="mobile-topbar">
          <button className="mobile-back" onClick={() => setMobilePane("list")} aria-label="Back to tasks">
            <Icon name="chevronLeft" size={18} />
            <span>Tasks</span>
          </button>
          <span className="mobile-topbar-title">{viewTitle}</span>
        </div>
        {missing && (
          <div className="route-missing" role="status">
            {missing}
            <button type="button" onClick={() => setMissing(null)} aria-label="Dismiss">✕</button>
          </div>
        )}
        {/* Keyed by task: the composer draft, the resume panel and the diff
            panel are per-task state, and without the key they carried over
            from the task the operator just left (2026-09-29 audit, U3). The
            stream is not affected -- it lives above, keyed by its own
            arguments. */}
        {view === "task" && selected && <TaskView key={selected.task_id} task={liveSelected ?? selected} stream={taskStream} setGeneration={setGeneration} />}
        {view === "new-task" && <NewTaskPanel repos={repos} onSubmit={handleCreate} submitting={submitting} error={createError} onClearError={() => setCreateError(null)} />}
        {view === "analytics" && user.role === "admin" && (
          <Suspense fallback={<div style={{ padding: "2rem", color: "var(--text-muted, #888)" }}>Loading analytics...</div>}>
            <AnalyticsView />
          </Suspense>
        )}
        {view === "benchmarks" && user.role === "admin" && (
          <Suspense fallback={<div style={{ padding: "2rem", color: "var(--text-muted, #888)" }}>Loading benchmarks...</div>}>
            <BenchmarksView />
          </Suspense>
        )}
        {view === "models" && user.role === "admin" && (
          /* Single wrapper on purpose: .main-pane gives `flex: 1` to EVERY direct
             child, so returning two siblings here split the pane 50/50 and blew
             the status card up to half the screen. The wrapper takes that flex
             slot; inside it the card sizes to its content and the model list
             takes the remaining height. */
          <div className="models-view">
            <ConsolidationStatusPanel />
            <ModelConfigPanel />
          </div>
        )}
        {/* Every admin gets the page: existing accounts keep working whether
            or not the deployment is licensed for more, and this is the only
            place to disable or delete one. The licence gates CREATING an
            account (agent/features.py), so that is the one part the panel
            withholds (2026-09-29 audit, U5). */}
        {view === "users" && user.role === "admin" && <UsersPanel repos={repos} canCreate={Boolean(user.features?.multi_user)} />}
        {(view === "analytics" || view === "benchmarks" || view === "models" || view === "users") && user.role !== "admin" && (
          /* An admin URL opened by a restricted account (a shared link, a
             bookmark). The server is the boundary -- every API behind these
             views answers 403 -- so this is only so the pane says why it is
             empty rather than being blank (2026-09-23 follow-up, F12). */
          <div className="admin-only" role="status">
            <h2>Admins only</h2>
            <p>This view is for admin accounts. Planning, your tasks and Settings are all yours.</p>
          </div>
        )}
        {view === "settings" && <SettingsPage user={user} onUserChanged={onUserChanged} onProjectsChanged={refreshRepos} />}
        {view === "github" && (
          <GitHubInboxView
            isAdmin={user.role === "admin"}
            onOpenTask={(taskId) => {
              const t = tasks.find((x) => x.task_id === taskId);
              if (t) {
                setSelected(t);
                setView("task");
                setMobilePane("main");
              }
            }}
          />
        )}
        {view === "planning" && (
          <PlanningView
            key={selectedPlanningSession?.session_id ?? "new"}
            repos={repos}
            isAdmin={isAdmin}
            githubReady={githubReady}
            onProjectCreated={refreshRepos}
            session={selectedPlanningSession}
            onBuildNow={(goal, repo, budgetUsd, route, sessionId) => handleCreate(goal, repo, budgetUsd, [], route, sessionId)}
            buildError={createError}
            onClearBuildError={() => setCreateError(null)}
            onSessionCreated={(s) => {
              setPlanningSessions((list) => [s, ...list]);
              setSelectedPlanningSession(s);
            }}
            onSessionUpdated={(s) => {
              // A confirmed new project moves the session onto the new repo:
              // the sidebar row and the open view must both follow, or the
              // header and Build Now would still name the old project until
              // the next poll.
              setPlanningSessions((list) => list.map((x) => (x.session_id === s.session_id ? s : x)));
              setSelectedPlanningSession((cur) => (cur?.session_id === s.session_id ? s : cur));
            }}
          />
        )}
      </div>
      {/* Bottom tab bar, mobile only. Navigation used to live solely in the
          sidebar — which IS the list pane on a phone — so reaching Analytics
          took three gestures. These sit in the thumb zone instead. */}
      <MobileNav
        view={view}
        pane={mobilePane}
        isAdmin={user.role === "admin"}
        canManageUsers={user.role === "admin"}
        onTasks={() => setMobilePane("list")}
        onNewPlan={() => { setSelectedPlanningSession(null); setView("planning"); setMobilePane("main"); }}
        onAnalytics={() => { setView("analytics"); setMobilePane("main"); }}
        onBenchmarks={() => { setView("benchmarks"); setMobilePane("main"); }}
        onModels={() => { setView("models"); setMobilePane("main"); }}
        onUsers={() => { setView("users"); setMobilePane("main"); }}
        onSettings={() => { setView("settings"); setMobilePane("main"); }}
        onGitHub={() => { setView("github"); setMobilePane("main"); }}
      />
    </div>
  );
}

export default function App() {
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [checked, setChecked] = useState(false);
  // No landing page on this host any more, so there is nothing to decide:
  // signed out is the sign-in form, whether the visitor never had a session
  // or just had one expire. The ref that used to tell those two apart went
  // with it.

  useEffect(() => {
    getMe()
      .then((me) => {
        // The account is the source of truth for the scheme; main.tsx painted
        // the last one THIS browser saw so there was no flash. Reconcile now:
        // a scheme changed on another device, or a first sign-in on a new
        // browser, would otherwise stay wrong until the next save.
        if (isThemeId(me.theme) && me.theme !== storedTheme()) applyTheme(me.theme);
        setUser(me);
      })
      .catch(() => setUser(null))
      .finally(() => setChecked(true));
  }, []);

  // audit H-14: any authenticated request that comes back 401 (expired/invalid
  // cookie) clears the user here, dropping the app straight back to the login
  // screen instead of degrading into stale data and opaque "... failed: 401".
  useEffect(() => {
    // Clearing the user is the whole job now: signed out on this host IS the
    // login form. It used to also have to push past a landing page that sat
    // in front of it, which is gone with the split to perseiagent.ai.
    setAuthFailureHandler(() => setUser(null));
    return () => setAuthFailureHandler(null);
  }, []);

  async function handleLogout() {
    await logout();
    setUser(null);
  }

  // audit M-20: a visible loading state during the initial getMe(), so a slow
  // auth check is distinguishable from a crashed render (the ErrorBoundary now
  // catches the latter and shows its own fallback).
  let page;
  if (!checked) {
    page = (
      <div style={{
        display: "flex", alignItems: "center", justifyContent: "center",
        minHeight: "100vh", color: "var(--text-muted, #888)",
        fontFamily: "system-ui, sans-serif", fontSize: "0.9rem",
      }}>
        Loading...
      </div>
    );
  } else if (!user) {
    // The console is its own host now (agent.perseiagent.ai), so signed out means
    // the sign-in form -- there is no public page in front of it to go back
    // to. perseiagent.ai serves the landing page, built separately from site/.
    page = <LoginPage onLoggedIn={setUser} />;
  } else if (user.must_change_password) {
    page = <ChangePasswordPage onDone={() => setUser({ ...user, must_change_password: false })} />;
  } else if (user.require_totp_setup) {
    page = <SetupTotpPage onDone={() => setUser({ ...user, require_totp_setup: false, totp_enabled: true })} />;
  } else {
    page = <AuthenticatedApp user={user} onLogout={handleLogout} onUserChanged={setUser} />;
  }
  // The title bar sits ABOVE the auth gate. The desktop window is frameless,
  // so this strip is its only drag handle and its only close button; it used
  // to render inside AuthenticatedApp, which left the sign-in, change-password
  // and 2FA-setup screens with no way to move or close the window (2026-09-29
  // audit, U2). Outside the app it renders nothing.
  return (
    <>
      <TitleBar />
      {page}
    </>
  );
}
