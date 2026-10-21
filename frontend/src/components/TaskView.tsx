import { useRef, useState } from "react";
import { RouteBadge } from "./RouteSelect";
import type { TaskMeta } from "../types";
import type { useTaskStream } from "../useTaskStream";
import { ApprovalCard } from "./ApprovalCard";
import { ChatMessage } from "./ChatMessage";
import { MessageInput } from "./MessageInput";
import { PlanTracker } from "./PlanTracker";
import { RepoBadge } from "./RepoBadge";
import { ResumePanel } from "./ResumePanel";
import { JumpToBottom } from "./JumpToBottom";
import { useStickToBottom } from "../useStickToBottom";
import { ReviewGatePanel } from "./ReviewGatePanel";
import { StatusBadge } from "./StatusBadge";
import { StopButton } from "./StopButton";
import { DiffPanel } from "./DiffPanel";
import "./TaskView.css";
import { idleMessage } from "./activityPhase";
import { safeHref } from "../safeHref";

// Rows mounted at once, and how many more a click reveals. 200 covers more
// than a viewport at any realistic row height, so the common case -- watching
// a task run -- never sees the control at all.
const LOG_WINDOW = 200;
const LOG_WINDOW_STEP = 400;

interface Props {
  task: TaskMeta;
  // Lifted up to AuthenticatedApp (App.tsx) so the WS connection and
  // accumulated log survive switching to another view and back -- this
  // component used to own the useTaskStream() call itself, which meant
  // navigating to Analytics and back fully unmounted/remounted it, dropping
  // every entry that streamed in but hadn't been checkpointed yet (a
  // work_node pass only persists execution_log at the END of a pass, so the
  // REST hydrate snapshot on remount could be well behind the live stream
  // the operator had actually been watching). Bumping `generation` forces a
  // fresh WS connection after a resume without wiping log history the way
  // switching to a different task does — see the hook's own doc comment.
  stream: ReturnType<typeof useTaskStream>;
  setGeneration: (updater: (g: number) => number) => void;
}

export function TaskView({ task, stream, setGeneration }: Props) {
  const logContainerRef = useRef<HTMLDivElement>(null);
  const [diffOpen, setDiffOpen] = useState(false);

  // How many of the log's entries are actually in the DOM, counted from the
  // end. useTaskStream caps the array at 3,000 and ChatMessage is memoized
  // (audit H-16), so appending does not re-render the rows above -- but every
  // row is still MOUNTED, and a multi-hour task makes the tab slow well
  // before it reaches that cap. A log is read from its end, so the window is
  // anchored there and grows upward on request rather than guessing at row
  // heights: these are chat bubbles, a rendered diff and a wall of build
  // output are two orders of magnitude apart, and a spacer sized from an
  // average of those two is a scrollbar that lies.
  // Starts over for every task: App keys this view by task id, so another
  // task is a fresh mount, and a window someone expanded on the last one
  // cannot carry over to mount rows nobody asked for.
  const [windowSize, setWindowSize] = useState(LOG_WINDOW);

  // Follows the newest entry while the reader is at the bottom, and stops
  // the moment they scroll up (audit H-16) -- see useStickToBottom for why
  // this is tracked from their scrolling rather than measured per entry.
  useStickToBottom(logContainerRef, stream.log.length, task.task_id);

  const firstShown = Math.max(0, stream.log.length - windowSize);
  const shownLog = firstShown === 0 ? stream.log : stream.log.slice(firstShown);
  const hiddenAbove = firstShown;

  const status = stream.status === "connecting" ? task.status : stream.status;
  // Store-status "running" is a lie for an orphaned task (backend restarted
  // mid-run, nothing actually driving it) — badge and input both need to
  // reflect that, not just the panel below.
  const displayStatus = stream.orphaned ? "stalled" : status;

  // The final look announces itself: the moment the task parks on
  // awaiting_merge the panel slides out, rather than relying on the operator
  // to notice a badge and find a button. Closing it is fine -- the Changes
  // button reopens it, and nothing merges until a decision is made.
  const awaitingMerge = displayStatus === "awaiting_merge";
  // On the transition INTO awaiting_merge, not while in it: closing the
  // panel must stick until the next parking. Adjusted during render rather
  // than in an effect, so the panel is open on the same frame.
  const [wasAwaiting, setWasAwaiting] = useState(awaitingMerge);
  if (wasAwaiting !== awaitingMerge) {
    setWasAwaiting(awaitingMerge);
    if (awaitingMerge) setDiffOpen(true);
  }
  const budgetPct = Math.min(100, (stream.costSoFar / Math.max(task.budget_usd, 0.01)) * 100);

  // Phase-aware: silence during a check run or a review wait is expected and
  // says so; silence during ordinary model work is still the old warning.
  const idleBanner = idleMessage(stream.log, stream.idleSeconds);

  return (
    <div className="task-view">
      <div className="task-view-header">
        <div className="task-view-title-row">
          <RepoBadge repo={task.repo} />
          <StatusBadge status={displayStatus} />
          <RouteBadge route={task.route} reason={task.route_reason} />
          {/* Identity chips: enough to name THIS task unambiguously when
              talking about it elsewhere (chat, logs, git). Click copies the
              full value; the commit chip appears once a commit exists. */}
          <code
            className="task-view-id"
            title={`task id: ${task.task_id} (click to copy)`}
            onClick={() => navigator.clipboard?.writeText(task.task_id)}
          >
            id:{task.task_id.slice(0, 8)}
          </code>
          {stream.committedSha && (
            <code
              className="task-view-id"
              title={`commit: ${stream.committedSha} (click to copy)`}
              onClick={() => navigator.clipboard?.writeText(stream.committedSha!)}
            >
              commit:{stream.committedSha.slice(0, 8)}
            </code>
          )}
          {status === "running" && !stream.orphaned && (
            <StopButton taskId={task.task_id} onStopped={() => setGeneration((g) => g + 1)} />
          )}
          <button className="task-view-changes-btn" onClick={() => setDiffOpen(true)}
                  title={status === "running" ? "Watch the agent's edits live" : "View this task's diff"}>
            Changes
          </button>
          <div
            className="task-view-budget"
            title={`build $${stream.costSoFar.toFixed(3)} of $${task.budget_usd.toFixed(2)} budget`
              + (task.planning_cost_usd ? ` + planning $${task.planning_cost_usd.toFixed(3)} = $${(stream.costSoFar + task.planning_cost_usd).toFixed(3)} in all` : "")}
          >
            <span className="task-view-cost">${(stream.costSoFar + (task.planning_cost_usd ?? 0)).toFixed(2)}</span>
            <div className="task-view-budget-track">
              <div
                className={`task-view-budget-fill ${budgetPct > 85 ? "task-view-budget-fill--hot" : ""}`}
                style={{ width: `${budgetPct}%` }}
              />
            </div>
            <span className="task-view-budget-cap">${task.budget_usd.toFixed(0)}</span>
          </div>
        </div>
        {/* The goal is not repeated here: it already opens the log below as
            the first message, and on a phone the clamped copy of it pushed
            the live stream down to a third of the screen. */}
        <PlanTracker plan={stream.plan} />
      </div>

      <div className="task-view-log-wrap">
      <JumpToBottom containerRef={logContainerRef} />
      <div className="task-view-log" ref={logContainerRef}>
        <div className="chat-thread">
          {stream.hydrateError && (
            <div className="hydrate-error">
              {stream.hydrateError}
              <button onClick={() => setGeneration((g) => g + 1)}>Retry</button>
            </div>
          )}
          {/* The task goal itself opens the conversation, chat-style */}
          <div className="chat-row chat-row--user">
            <div className="chat-bubble chat-bubble--user">
              <div className="chat-text">{stream.goal ?? task.goal}</div>
            </div>
          </div>
          {hiddenAbove > 0 && (
            <button
              type="button"
              className="chat-show-earlier"
              onClick={() => {
                // Revealing rows above pushes the rest down, and the browser
                // keeps scrollTop rather than what was under it. Hold the
                // reading position by adding back exactly what grew.
                const c = logContainerRef.current;
                const before = c ? c.scrollHeight - c.scrollTop : 0;
                setWindowSize((n) => n + LOG_WINDOW_STEP);
                requestAnimationFrame(() => {
                  if (c) c.scrollTop = c.scrollHeight - before;
                });
              }}
            >
              Show earlier — {hiddenAbove} more {hiddenAbove === 1 ? "entry" : "entries"}
            </button>
          )}
          {shownLog.map((entry, i) => {
            // The absolute index, so growing the window does not renumber
            // every key and remount the rows already on screen -- and so
            // prevEntry is the real previous entry rather than undefined at
            // the top of the window, which would break the continuation
            // styling on the first visible row.
            const abs = firstShown + i;
            return (
              <ChatMessage
                key={abs}
                entry={entry}
                prevEntry={abs > 0 ? stream.log[abs - 1] : undefined}
              />
            );
          })}
          {status === "running" && !stream.orphaned && (
            <div className="chat-typing">
              <span /><span /><span />
            </div>
          )}
          {/* Thinking bubbles look the same after four seconds and after forty
              minutes. A task wedged on a model call that never returns still
              receives the server's pings, so the socket is healthy and the
              page keeps animating — the one thing this dashboard exists to
              make visible is exactly what it hid.

              What it says now depends on what the task is actually doing. A
              check run announces itself and then emits nothing for minutes,
              and this banner used to answer that silence with "either on a
              long model call or stuck" — directly under the line saying
              several minutes of quiet was normal. Two contradicting sentences,
              and the operator reasonably believed the alarming one
              (2026-09-13). See activityPhase.ts. */}
          {status === "running" && !stream.orphaned && idleBanner && (
            <div className="chat-stalled" role="status">{idleBanner}</div>
          )}
          {stream.reviewGateResult && <ReviewGatePanel result={stream.reviewGateResult} minimized={status === "running"} />}
          {stream.pendingApproval && (
            <ApprovalCard
              taskId={task.task_id}
              pendingApproval={stream.pendingApproval}
              onDecided={() => setGeneration((g) => g + 1)}
            />
          )}
          {/* A project that ships as a pull request finishes with the work
              waiting for a person. The link is the whole outcome, so it sits
              above the resume panel rather than in the step log. */}
          {task.pull_request_url && (
            <div className="task-pr">
              <span className="task-pr-label">Opened a pull request</span>
              <a
                className="task-pr-link"
                href={safeHref(task.pull_request_url)}
                target="_blank"
                rel="noopener noreferrer"
              >
                {task.pull_request_url.replace(/^https:\/\/github\.com\//, "")}
              </a>
              <span className="task-pr-note">
                Nothing was merged &mdash; review and merge it on GitHub.
              </span>
            </div>
          )}
          {(status === "escalated" || status === "stopped" || status === "done" || stream.orphaned) && (
            <ResumePanel
              taskId={task.task_id}
              currentBudget={task.budget_usd}
              budgetUsedFraction={stream.costSoFar / Math.max(task.budget_usd, 0.01)}
              escalationReason={
                stream.orphaned
                  ? "the server restarted mid-run — nothing is actively driving this task anymore, resume to pick it back up"
                  : status === "stopped"
                    ? "stopped by operator — resume to pick it back up from exactly where it left off"
                    : status === "done"
                      ? "not actually finished? tell it what's still missing and it'll pick back up on the same thread"
                      : stream.escalationReason
              }
              requireMessage={status === "done"}
              onResumed={() => setGeneration((g) => g + 1)}
            />
          )}
        </div>
      </div>

      </div>

      <MessageInput taskId={task.task_id} running={status === "running" && !stream.orphaned} />

      <DiffPanel
        taskId={task.task_id}
        open={diffOpen}
        onClose={() => setDiffOpen(false)}
        live={status === "running" && !stream.orphaned}
        awaitingMerge={awaitingMerge}
        onDecided={() => setGeneration((g) => g + 1)}
      />
    </div>
  );
}
