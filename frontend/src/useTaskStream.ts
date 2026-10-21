import { useEffect, useRef, useState } from "react";
import { AuthError, getMe, getTask, taskStreamUrl } from "./api";
import type { LogEntry, PendingApproval, PlanStep, ReviewGateResult, StreamEvent, TaskStatus } from "./types";

interface StreamState {
  log: LogEntry[];
  plan: PlanStep[];
  costSoFar: number;
  escalated: boolean;
  escalationReason: string | null;
  reviewGateResult: ReviewGateResult | null;
  pendingApproval: PendingApproval | null;
  committedSha: string | null;
  status: TaskStatus | "connecting";
  connected: boolean;
  hydrateError: string | null;
  /** Seconds since anything of substance arrived (a log entry, a plan change,
   *  a status), while the task is running. A live socket is not the same as a
   *  working agent: a task wedged on a model call that never returns keeps
   *  receiving pings, so the liveness watchdog stays happy and the page keeps
   *  saying "running". This is the number that tells the two apart. 0 when the
   *  task is not running. */
  idleSeconds: number;
  // True when the Store says "running" but nothing is actually driving the
  // task (a backend restart mid-run — same condition resume_task's own
  // eligibility check already accepts). Only ever known from the REST
  // hydrate snapshot, not the WS stream — a dead task's WS endpoint still
  // accepts the connection and just sits there forever, so there's no
  // WS-side signal to detect this from.
  orphaned: boolean;
  /** The task's whole goal, from its own record. The task list carries only
   *  a preview of it (agent/routers/tasks.py GOAL_PREVIEW_CHARS). Null until
   *  the first snapshot arrives. */
  goal: string | null;
}

// audit H-16: the log renders unvirtualized, so beyond React.memo (which
// removes the O(N^2) regex cost) a very long autonomous run still piles up
// live DOM nodes. Cap retained entries to a generous ceiling; slicing keeps
// the surviving entries' object references intact, so ChatMessage's memo
// still short-circuits. cleanText-heavy history beyond this is dropped from
// the view (the full record lives server-side).
const MAX_LOG_ENTRIES = 3000;

// Liveness watchdog, the same one planning chat has had since 2026-08-31 and
// build tasks did not (2026-09-11).
//
// onclose is not a reliable death signal. A half-open TCP -- laptop sleep, a
// NAT idle-kill, a proxy dropping the connection without a FIN -- leaves the
// browser holding a socket it will never hear from again, and no event ever
// fires. The task then finishes, escalates or asks for an approval
// server-side, and every one of those events goes to a socket nobody is
// listening on: the page sits on "running" forever, showing a step that
// completed twenty minutes ago. It looks exactly like a stuck agent, which is
// the one thing this dashboard exists to make visible.
//
// The server pings every 20s, so silence past three pings is death. The
// recovery is just close(): that fires onclose, which reconnects and
// re-hydrates, and hydration reads status from the server -- the authority on
// whether the task is actually running.
const SOCKET_SILENCE_LIMIT_MS = 70_000;
const SOCKET_WATCHDOG_POLL_MS = 15_000;

// A live socket is not the same as a working agent. Pings keep arriving from a
// server whose task is wedged on a model call that will never return, so the
// watchdog above sees a healthy connection and the page keeps saying
// "running". This tracks the last time anything of SUBSTANCE arrived -- a log
// entry, a plan change, a status, an approval -- and the view reports it, so a
// stall is visible as a stall rather than as ordinary work.
const STALL_TICK_MS = 10_000;

/** One log from the snapshot and what the socket has delivered since.
 *
 * Server-side twin: agent/log_stream.py's merge(). Entries carry a
 * content-derived `id`, so the same entry arriving by both routes is one
 * entry. An entry with no id (an older server) falls back to its own content,
 * which is what the id is anyway.
 */
export function mergeLog(durable: LogEntry[] | undefined | null, live: LogEntry[]): LogEntry[] {
  // The fallback must use the SAME fields as agent/log_stream.py's
  // _IDENTITY_FIELDS, and all of them. It only runs against a server too old
  // to stamp ids -- where neither source is stamped, so both go through this
  // -- and the danger there is a collision, not a mismatch: two entries that
  // differ only in step_id, cost, or past the first 120 characters of detail
  // would be treated as one and the second silently dropped.
  const idOf = (e: LogEntry): string =>
    (e as { id?: string }).id ??
    [e.timestamp, e.node, e.step_id, e.summary, e.detail, e.cost_usd]
      .map((v) => (v === undefined || v === null ? "" : String(v)))
      // \u0000 as an escape, never the raw byte: one NUL in the file makes
      // git treat the whole module as binary, so every diff of it reads
      // "Binary files differ" and no review ever sees the change.
      .join("\u0000");
  const snapshot = durable ?? [];
  const seen = new Set(snapshot.map(idOf));
  const merged = [...snapshot];
  for (const entry of live) {
    const id = idOf(entry);
    if (!seen.has(id)) {
      seen.add(id);
      merged.push(entry);
    }
  }
  return merged.length > MAX_LOG_ENTRIES ? merged.slice(-MAX_LOG_ENTRIES) : merged;
}

const EMPTY_STATE: StreamState = {
  log: [],
  plan: [],
  costSoFar: 0,
  escalated: false,
  escalationReason: null,
  reviewGateResult: null,
  pendingApproval: null,
  committedSha: null,
  status: "connecting",
  connected: false,
  hydrateError: null,
  idleSeconds: 0,
  orphaned: false,
  goal: null,
};

const HYDRATE_MAX_RETRIES = 5;

/**
 * Auto-reconnects with backoff on an unexpected drop — a silent disconnect
 * that looks like the agent just stopped is worse than a visible retry. A
 * deliberate server-sent "closed" event (task actually finished) stops
 * reconnecting; anything else (network blip, server restart) retries.
 *
 * `generation` forces a fresh connection without treating it as a new task
 * (used after resuming an escalated task — the server-side WS handler
 * genuinely closes when a run finishes, by design, so picking the same task
 * back up needs a new connection, but should keep the existing log history
 * rather than wiping it like switching tasks does).
 *
 * The WS only carries live events from the moment it connects — it never
 * replays what already happened. `getTask` (a plain REST snapshot of the
 * current checkpointed state) fills that gap on every connect/reconnect, so
 * the UI reflects reality immediately regardless of when the viewer showed
 * up. This snapshot fetch retries with backoff and surfaces a visible error
 * via `hydrateError` if it keeps failing, rather than leaving the view
 * stuck on a blank/"connecting" state with no indication anything is wrong.
 */
export function useTaskStream(taskId: string | null, repo: string | null, generation: number = 0): StreamState {
  const [state, setState] = useState<StreamState>(EMPTY_STATE);
  const closedIntentionally = useRef(false);
  const prevTaskId = useRef<string | null>(null);
  // The live socket and when it last said anything, for the watchdog below.
  // A ref because the watchdog runs in its own effect and must be able to
  // close the socket the connect() closure owns.
  const wsRef = useRef<WebSocket | null>(null);
  const lastMessageAt = useRef(Date.now());
  // Socket-first hydrate. The socket is opened before the REST snapshot is
  // fetched, so nothing published during the fetch is missed; frames that
  // arrive while `hydrating` is true wait in `pending` and are applied after
  // the snapshot, in order, skipping anything the snapshot already contains
  // (`lastAppliedSeq`).
  const hydrating = useRef(false);
  const pending = useRef<StreamEvent[]>([]);
  const lastAppliedSeq = useRef(0);
  // Last time something of substance arrived -- not a ping. A wedged agent
  // keeps the socket healthy, so this is the only thing that can tell the
  // difference between working and stuck.
  const lastProgressAt = useRef(Date.now());

  useEffect(() => {
    if (!taskId || !repo) return;
    closedIntentionally.current = false;
    const isNewTask = prevTaskId.current !== taskId;
    prevTaskId.current = taskId;
    let cancelled = false;

    async function hydrate(): Promise<boolean> {
      for (let attempt = 0; attempt <= HYDRATE_MAX_RETRIES; attempt++) {
        if (cancelled) return false;
        try {
          const { meta, state: graphState, orphaned, seq } = await getTask(taskId!, repo!);
          if (cancelled) return false;
          // Where this snapshot sits in the stream: anything at or below it
          // is already folded in, so the buffer replay skips it.
          //
          // ADOPTED, not raised. The server's counter lives in the process
          // that hands it out, so a backend restart begins again at 1. A
          // browser holding, say, 500 from the old process would then drop
          // every live frame from the new one until it climbed past 500 --
          // silently, on exactly the reconnect-after-restart path this was
          // meant to make safe. The snapshot is the authority on where the
          // stream is; if it moves backwards, so does this.
          if (typeof seq === "number") lastAppliedSeq.current = seq;
          if (graphState) {
            setState((s) => ({
              ...s,
              // audit M-18, finished 2026-09-11. This was "keep whichever
              // list is longer", a proxy for freshness that is wrong both
              // ways: a snapshot that is longer but older replaced newer
              // live entries, and a shorter one was discarded even when it
              // held history this browser never saw. Entries now carry a
              // content-derived id from the server (agent/log_stream.py), so
              // the two sources MERGE: the snapshot keeps its order, and
              // anything the socket delivered since is appended.
              log: mergeLog(graphState.execution_log, s.log),
              plan: graphState.plan ?? s.plan,
              costSoFar: graphState.cost_so_far ?? s.costSoFar,
              escalated: graphState.escalated ?? s.escalated,
              escalationReason: graphState.escalation_reason ?? s.escalationReason,
              reviewGateResult: graphState.review_gate_result ?? s.reviewGateResult,
              committedSha: graphState.committed_sha ?? s.committedSha,
              // Not `?? s.pendingApproval`: this field must be able to go
              // from a real object back to null on reconnect (the approval
              // was resolved while disconnected) -- `??` would treat that
              // legitimate null as "missing" and incorrectly keep showing a
              // stale approval card. The REST snapshot always includes this
              // key (outer_state.py's AgentState always has it), so it's
              // safe to take verbatim rather than fall back.
              pendingApproval: graphState.pending_approval,
              status: meta.status,
              hydrateError: null,
              orphaned,
              goal: meta.goal ?? s.goal,
            }));
          } else {
            setState((s) => ({ ...s, status: meta.status, hydrateError: null, orphaned, goal: meta.goal ?? s.goal }));
          }
          return true;
        } catch (err) {
          console.error(`getTask attempt ${attempt + 1}/${HYDRATE_MAX_RETRIES + 1} failed:`, err);
          if (attempt === HYDRATE_MAX_RETRIES) {
            setState((s) => ({
              ...s,
              hydrateError: "Couldn't load this task's current state after several attempts. Reload the page to retry.",
            }));
            return false;
          }
          await new Promise((r) => setTimeout(r, Math.min(1000 * 2 ** attempt, 8000)));
        }
      }
      return false;
    }

    // audit H6: every reconnect path used to call connect() directly, so only
    // the FIRST connection ever fetched a snapshot. A network blip or a backend
    // restart therefore dropped whatever the socket missed while it was down --
    // permanently, since nothing re-read it afterwards. The graceful
    // closed-frame path re-hydrated; the three unclean paths did not, which is
    // exactly backwards: an unclean drop is when a gap is most likely.
    function applyEvent(event: StreamEvent) {
      // Ordering and replay protection: the server numbers every content
      // event per task (agent/log_stream.py). A frame already folded into
      // the snapshot, or one replayed twice from the buffer, is dropped
      // here rather than double-counted.
      const seq = (event as { seq?: number }).seq;
      if (typeof seq === "number") {
        if (seq <= lastAppliedSeq.current) return;
        lastAppliedSeq.current = seq;
      }
      if (event.type === "closed") {
        closedIntentionally.current = true;
        watchForResumption();
        return;
      }
      // Something of substance arrived, which is what "the agent is
      // working" actually means -- a ping only means the socket is open.
      lastProgressAt.current = Date.now();
      setState((s) => ({
        // mergeLog rather than a blind append: a replayed frame (the
        // buffer flushed after a hydrate, or a reconnect that re-sends)
        // must not double an entry.
        log: event.execution_log ? mergeLog(s.log, event.execution_log) : s.log,
        goal: s.goal,
        plan: event.plan ?? s.plan,
        costSoFar: event.cost_so_far ?? s.costSoFar,
        committedSha: event.committed_sha ?? s.committedSha,
        escalated: event.escalated ?? s.escalated,
        escalationReason: event.escalation_reason ?? s.escalationReason,
        reviewGateResult: event.review_gate_result ?? s.reviewGateResult,
        // Same reasoning as the hydrate path above: `pending_approval` is
        // only included by server.py on a "work" node_update or a
        // "status" event, but is ALWAYS included (possibly as an explicit
        // null) whenever it is -- work_node's own return dict always has
        // this key. `"pending_approval" in event` distinguishes "this
        // event doesn't speak to approval state at all" (a todos/
        // log_entry custom event) from "the approval state is definitely
        // X now, even if X is null" -- `??` alone can't tell those apart
        // since it treats an explicit null the same as absent.
        pendingApproval: "pending_approval" in event ? (event.pending_approval ?? null) : s.pendingApproval,
        status: event.status ?? s.status,
        connected: true,
        hydrateError: null,
        // A live event is direct proof the task is being driven right
        // now, regardless of what the last hydrate snapshot said.
        orphaned: false,
        // An event IS progress, so the stall counter restarts here rather
        // than waiting for the next tick.
        idleSeconds: 0,
      }));
    }

    /** Open the socket, then hydrate, then replay whatever arrived meanwhile.
     *
     * The order matters and used to be the other way round. Hydrating first
     * leaves a window between the snapshot being taken and the socket being
     * open: anything the task published in that window reached nobody, and
     * nothing ever went back for it. Opening first closes the window; the
     * buffer is what makes opening first safe, and the per-event seq plus the
     * per-entry id are what make the replay idempotent.
     */
    async function connectThenHydrate() {
      hydrating.current = true;
      pending.current = [];
      connect();
      const ok = await hydrate();
      if (cancelled) return;
      hydrating.current = false;
      lastProgressAt.current = Date.now();
      const buffered = pending.current;
      pending.current = [];
      for (const event of buffered) applyEvent(event);
      return ok;
    }

    async function reconnect() {
      if (cancelled || closedIntentionally.current) return;
      await connectThenHydrate();
    }

    async function hydrateAndConnect() {
      if (isNewTask) {
        setState({ ...EMPTY_STATE, status: "connecting" });
        lastAppliedSeq.current = 0;
      } else {
        setState((s) => ({ ...s, status: "connecting", connected: false, hydrateError: null }));
      }
      await connectThenHydrate();
    }

    let ws: WebSocket;
    let retryDelay = 1000;
    let retryTimer: ReturnType<typeof setTimeout> | undefined;
    let watchTimer: ReturnType<typeof setTimeout> | undefined;

    // A server "closed" event means this run ended -- it does not mean the
    // task is over forever. A task paused at awaiting_approval (or stopped
    // for a backend fix) sends "closed"; if it's later resumed from outside
    // this page (an operator API call), the page must not permanently stop
    // reconnecting -- the approval request and every subsequent status
    // change would otherwise be invisible until a manual refresh. After
    // "closed", keep polling the REST snapshot (status/approvals/log stay
    // current within seconds either way) and re-open the live stream the
    // moment the task is running again.
    // audit M-19: a "closed" event does not always mean the task can still
    // change. done/error are settled -- a resume from THIS page bumps generation
    // and re-runs the effect, so there is nothing to poll for. Only the states
    // that can transition WITHOUT this page's involvement (an external approval
    // or an orphaned task being resumed elsewhere) are worth watching. The old
    // code polled every 6s forever for done/error/stopped too, and kept doing
    // it while the user sat on Analytics or a hidden tab.
    const _TERMINAL_SETTLED = new Set(["done", "error"]);
    const _WATCH_MIN_MS = 6000;
    const _WATCH_MAX_MS = 60000;
    let watchDelay = _WATCH_MIN_MS;

    function watchForResumption() {
      clearTimeout(watchTimer);
      const tick = async () => {
        if (cancelled) {
          clearTimeout(watchTimer);
          return;
        }
        // Pause polling while the tab is hidden -- resume on the next tick once
        // it's visible again (don't advance backoff on a skipped tick).
        if (typeof document !== "undefined" && document.hidden) {
          watchTimer = setTimeout(tick, watchDelay);
          return;
        }
        try {
          const { meta, state: graphState, orphaned, seq } = await getTask(taskId!, repo!);
          if (cancelled) return;
          // Adopt the snapshot's position, exactly as hydrate does -- the
          // server's counter restarts with its process.
          if (typeof seq === "number") lastAppliedSeq.current = seq;
          setState((s) => ({
            ...s,
            status: meta.status,
            orphaned,
            costSoFar: graphState?.cost_so_far ?? s.costSoFar,
            committedSha: graphState?.committed_sha ?? s.committedSha,
            // Same merge as the hydrate path: this poll is a snapshot like
            // any other, and it used to carry the old "keep whichever list is
            // longer" rule -- so a task parked on an approval could still
            // lose or duplicate entries here while the socket was closed.
            log: mergeLog(graphState?.execution_log, s.log),
            plan: graphState?.plan ?? s.plan,
            escalated: graphState?.escalated ?? s.escalated,
            escalationReason: graphState?.escalation_reason ?? s.escalationReason,
            reviewGateResult: graphState?.review_gate_result ?? s.reviewGateResult,
            pendingApproval: graphState ? graphState.pending_approval : s.pendingApproval,
          }));
          if (meta.status === "running") {
            clearTimeout(watchTimer);
            closedIntentionally.current = false;
            // connectThenHydrate, not connect: this is the resume-from-
            // outside path (an operator approving from another device, an
            // orphan resumed elsewhere), and it is a reconnect like any
            // other. Calling connect() alone left the same hydrate-window
            // hole the socket-first order exists to close -- and skipped the
            // snapshot that re-adopts the server's seq after a restart.
            void connectThenHydrate();
            return;
          }
          // Nothing left to watch for on a settled task -- stop the loop.
          if (_TERMINAL_SETTLED.has(meta.status)) {
            clearTimeout(watchTimer);
            return;
          }
        } catch {
          // Transient poll failure -- keep watching.
        }
        watchDelay = Math.min(watchDelay * 1.5, _WATCH_MAX_MS);
        watchTimer = setTimeout(tick, watchDelay);
      };
      watchDelay = _WATCH_MIN_MS;
      watchTimer = setTimeout(tick, watchDelay);
    }

    function connect() {
      ws = new WebSocket(taskStreamUrl(taskId!));
      wsRef.current = ws;
      lastMessageAt.current = Date.now();
      // audit H-14: a rejected upgrade (expired/invalid session) fires onclose
      // WITHOUT ever firing onopen, and the old code just reconnected on the
      // same backoff forever against a server that will never accept it.
      let openedThisAttempt = false;

      ws.onopen = () => {
        openedThisAttempt = true;
        retryDelay = 1000;
        lastMessageAt.current = Date.now();
        setState((s) => ({ ...s, connected: true }));
      };

      ws.onmessage = (ev) => {
        // Any frame at all, ping or content, parseable or not, proves the
        // socket is alive -- that is the only question the watchdog asks.
        lastMessageAt.current = Date.now();
        // A frame that does not parse drops just that frame. Unguarded, the
        // throw escapes into the event loop as a bare SyntaxError with no
        // indication of which socket produced it -- the stream survives
        // either way, but the log line should say what actually happened.
        let event: StreamEvent;
        try {
          event = JSON.parse(ev.data);
        } catch {
          console.error("task stream: discarding unparseable frame");
          return;
        }
        if (event.type === "ping") return; // server heartbeat, not content
        // Socket-first hydrate: this connection was opened BEFORE the
        // snapshot was fetched, so that nothing published in between is
        // lost. Until the snapshot lands, frames are buffered rather than
        // applied -- applying them first and then hydrating is what used to
        // let an older snapshot overwrite newer state.
        if (hydrating.current) {
          pending.current.push(event);
          return;
        }
        applyEvent(event);
      };


      ws.onclose = () => {
        // `cancelled` is per-effect-run; `closedIntentionally` is a ref shared
        // across runs, and checking only the ref produced DUPLICATED stream
        // output. On a generation bump (a resume), cleanup sets the ref true
        // and tears down the old socket -- but the new effect run has already
        // reset the same ref to false by the time the old socket's onclose
        // actually fires, so the OLD connection saw "not intentional" and
        // reconnected itself. Two live sockets for one task, both appending
        // to the same log, so every entry rendered twice. Checking `cancelled`
        // first is what distinguishes "this effect run is over, a newer one
        // owns the connection now" from "the server dropped us, reconnect".
        if (cancelled) return;
        setState((s) => ({ ...s, connected: false }));
        if (closedIntentionally.current) return;
        // audit H-14: if the socket closed without ever opening, re-validate the
        // session before scheduling another retry. A 401 from getMe fires the
        // central auth-failure handler (App clears the user -> login screen) and
        // we stop, instead of hammering a server that keeps rejecting the upgrade.
        if (!openedThisAttempt) {
          getMe()
            .then(() => {
              if (cancelled || closedIntentionally.current) return;
              retryTimer = setTimeout(reconnect, retryDelay);
              retryDelay = Math.min(retryDelay * 2, 15000);
            })
            .catch((err) => {
              // AuthError -> the central handler already logged the user out;
              // do NOT reschedule (there is nothing to reconnect to). Any other
              // error means the API is unreachable too, so back off and retry.
              if (err instanceof AuthError) return;
              if (cancelled || closedIntentionally.current) return;
              retryTimer = setTimeout(reconnect, retryDelay);
              retryDelay = Math.min(retryDelay * 2, 15000);
            });
          return;
        }
        retryTimer = setTimeout(reconnect, retryDelay);
        retryDelay = Math.min(retryDelay * 2, 15000);
      };
    }

    hydrateAndConnect();
    return () => {
      cancelled = true;
      closedIntentionally.current = true;
      clearTimeout(retryTimer);
      clearTimeout(watchTimer);
      ws?.close();
      wsRef.current = null;
    };
  }, [taskId, repo, generation]);

  // How long the agent has been quiet, recomputed on a timer because the
  // absence of events is exactly what has to be noticed. Only while running:
  // a settled or paused task is quiet by definition and reporting that would
  // be noise.
  useEffect(() => {
    if (state.status !== "running") {
      setState((s) => (s.idleSeconds === 0 ? s : { ...s, idleSeconds: 0 }));
      return;
    }
    const tick = () => {
      const seconds = Math.floor((Date.now() - lastProgressAt.current) / 1000);
      setState((s) => (s.idleSeconds === seconds ? s : { ...s, idleSeconds: seconds }));
    };
    tick();
    const id = window.setInterval(tick, STALL_TICK_MS);
    return () => window.clearInterval(id);
  }, [state.status]);

  // Armed only while the task is running: a settled task has nothing to
  // recover, and the REST watcher above already covers the paused states.
  useEffect(() => {
    if (state.status !== "running") return;
    const id = window.setInterval(() => {
      if (Date.now() - lastMessageAt.current < SOCKET_SILENCE_LIMIT_MS) return;
      lastMessageAt.current = Date.now(); // don't re-fire while the retry runs
      const ws = wsRef.current;
      if (ws && ws.readyState !== WebSocket.CLOSED) {
        // close() fires onclose, which re-hydrates and reconnects. Going
        // through that path rather than calling connect() directly keeps the
        // backoff and the auth re-validation in one place.
        try { ws.close(); } catch { /* already closing */ }
      } else {
        // No socket at all and the server still says running: nothing is
        // coming to reconnect us, so mark the view disconnected and let the
        // REST watcher pick the task back up.
        setState((s) => (s.connected ? { ...s, connected: false } : s));
      }
    }, SOCKET_WATCHDOG_POLL_MS);
    return () => window.clearInterval(id);
  }, [state.status]);

  return state;
}
