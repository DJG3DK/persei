// --- Auth (agent/auth.py) ----------------------------------------------
export interface CurrentUser {
  id: number;
  email: string;
  role: "admin" | "user";
  /** Licensed features this deployment has. Missing means none. */
  features?: { multi_user: boolean };
  allowed_repos: string[] | null; // null == every repo (always true for admin)
  totp_enabled: boolean;
  must_change_password: boolean;
  require_totp_setup: boolean; // admin-only forced enrollment, see agent/server.py's require_full_auth
  /** Auto mode: skips the approval prompt for sensitive-path bash/write/edit.
   *  Destructive commands (rm -rf, force push, sudo...) stay gated regardless
   *  -- see agent/deep_agent.py's interrupt_on_for. */
  auto_approve_commands: boolean;
  /** Which projects auto mode covers. Both halves must agree before a task
   *  runs unattended -- see User.auto_approves in agent/auth.py. */
  auto_approve_repos: string[];
  /** The account's colour scheme (src/themes.ts). The server substitutes the
   *  default for an account that has never chosen, so this is never null. */
  theme: string;
  require_merge_review: boolean;
}

export type TaskStatus =
  | "running" | "done" | "escalated" | "error" | "stopped"
  /** Waiting for a slot on its project: every slot "Tasks at once per
   *  project" allows is held by another task (project_slot in agent/graph.py,
   *  one advisory lock per slot). This is what that wait looks like from
   *  outside, instead of a task that claims to be running and never moves. */
  | "queued"
  | "awaiting_approval"
  /** Review READY; merge parked on the operator's final look at the diff. */
  | "awaiting_merge";

export interface TaskDiffFile {
  path: string;
  additions: number | null;
  deletions: number | null;
  binary: boolean;
  untracked: boolean;
  patch: string;
  truncated: boolean;
}

export interface TaskDiff {
  repo: string;
  base: string;
  head: string | null;
  branch: string | null;
  files: TaskDiffFile[];
  total_additions: number;
  total_deletions: number;
}

export interface TaskMeta {
  task_id: string;
  goal: string;
  /** The list sends a preview; the whole goal is on the task's own record. */
  goal_truncated?: boolean;
  repo: string;
  budget_usd: number;
  status: TaskStatus;
  created_at: number;
  cost_so_far?: number;
  /** The planning session's spend carried onto this task at Build Now. The
   * whole cost of a task is cost_so_far + planning_cost_usd; the budget bar
   * measures the build alone. */
  planning_cost_usd?: number;
  planning_session_id?: string | null;
  escalation_reason?: string | null;
  /** Set when the project ships as a pull request instead of merging. The
   * task is finished and the work is waiting for a person. */
  pull_request_url?: string | null;
  error?: string;
  /** Fixed taxonomy from agent/classify.py, set once at creation -- absent
   * on tasks created before the classifier existed. */
  category?: string;
  /** Which coder seat this task runs on and why (agent/frontend_route.py). */
  route?: "frontend" | "general";
  route_reason?: string | null;
}

export interface PlanStep {
  id: string;
  description: string;
  status: "pending" | "in_progress" | "done" | "failed" | "skipped";
  result: string | null;
  verified: boolean;
}

// "work"/"verify_and_ship" are the new deepagents-based graph's own two
// nodes; `work:${name}` tags a subagent's own turns (e.g. "work:investigator")
// so they're visually distinguishable from the coordinator's. "operator" is
// unchanged (a human-sent message). The old "plan"/"execute"/"reflect"/
// "review"/"deploy" node names belonged to the legacy plan->execute->reflect
// graph and no longer appear once server.py is on the new outer graph.
// "supervisor" is agent/supervisor.py healing or concluding a parked task.
export interface LogEntry {
  node: "work" | "verify_and_ship" | "operator" | "supervisor" | `work:${string}`;
  step_id: string | null;
  summary: string;
  detail: string;
  cost_usd: number;
  timestamp: string;
  /** Real underlying model that produced this turn (from the router's
   * return_raw_model_name), e.g. "deepseek/deepseek-v4-pro-0813" — absent
   * on tool results, gate events, and entries from before this field. */
  model?: string | null;
  /** Which agent ROLE made the call (coder, test-writer, ...) — two roles can pin the same model, so the model badge alone is ambiguous. */
  role?: string | null;
  /** A phase that is silent by nature: "checks" while the suite runs,
   *  "review" while the review service does. The idle banner needs it to tell
   *  expected quiet from a wedged task — see components/activityPhase.ts. */
  phase?: "checks" | "review" | "deploy" | null;
  /** This PROJECT's own median for that phase, when there is history to go on
   *  (agent/check_timing.py). Null on the first run of a new project, which
   *  the banner has to handle rather than invent a number for. */
  expected_seconds?: number | null;
}

export interface ReviewGateResult {
  verdict: "READY" | "NEEDS_FIXES";
  summary: string;
  findings: { severity: "blocking" | "minor"; file?: string; issue: string }[];
}

// A pending human-in-the-loop approval request -- deep_agent.py's
// INTERRUPT_ON paused the agent mid-turn on one or more risky bash/write/
// edit calls (a sensitive-looking path or a recognizably dangerous
// command), and it's waiting on POST /tasks/{id}/approve before continuing.
// Mirrors LangChain's own HITLRequest shape verbatim (langgraph.types.
// Interrupt.value, forwarded as-is by work.py) -- no server-side reshaping,
// so this type is the authoritative contract for what the dashboard renders.
interface PendingApprovalActionRequest {
  name: string;
  args: Record<string, unknown>;
  description?: string;
}

export interface PendingApproval {
  action_requests: PendingApprovalActionRequest[];
  review_configs: { action_name: string; allowed_decisions: string[] }[];
}

export interface TaskState {
  goal: string;
  repo: string;
  budget_usd: number;
  plan: PlanStep[];
  execution_log: LogEntry[];
  cost_so_far: number;
  escalated: boolean;
  escalation_reason: string | null;
  review_gate_result: ReviewGateResult | null;
  pending_approval: PendingApproval | null;
  committed_sha?: string | null;
  pull_request_url?: string | null;
}


export interface RouterBalance {
  totalCredits: number;
  totalUsage: number;
  remaining: number;
}

export interface StreamEvent {
  type: "status" | "node_update" | "closed" | "ping";
  node?: string;
  execution_log?: LogEntry[];
  plan?: PlanStep[] | null;
  cost_so_far?: number;
  escalated?: boolean;
  escalation_reason?: string | null;
  review_gate_result?: ReviewGateResult | null;
  pending_approval?: PendingApproval | null;
  status?: TaskStatus;
  error?: string;
  committed_sha?: string | null;
}

interface AnalyticsDaily {
  date: string;
  cost: number;
  tasks: number;
}

interface AnalyticsTask {
  task_id: string;
  repo: string;
  goal: string;
  category: string;
  cost: number;
  budget: number;
  status: string;
  created_at: number | null;
}

interface AnalyticsEpisode {
  task_id: string;
  repo: string;
  iterations: number;
  outcome: string;
  cost: number | null;
  review_verdict: string | null;
  timestamp: string | null;
}

// The fixed taxonomy a task's goal gets sorted into at creation time (see
// agent/classify.py) -- "other" is also the fallback for any task created
// before this classifier existed at all.
interface AnalyticsCategory {
  category: string;
  tasks: number;
  cost: number;
}

/** The commit reviewer's own model spend.
 *
 *  Separate from the agent's totals on purpose: they are different budgets, and
 *  folding them together would silently change what every other number on the
 *  Analytics page means. Until 2026-08-25 this was not measured at all — the
 *  reviewer called OpenRouter directly and never read the response's usage. */
interface ReviewerUsage {
  reviews: number;
  cost: number;
  tokens_in: number;
  tokens_out: number;
  model: string | null;
  /** false when some reviews reported no cost — the dollar figure is then a floor. */
  cost_known: boolean;
  reviews_missing_cost?: number;
  per_repo: { repo: string; reviews: number; cost: number }[];
  daily: { date: string; cost: number; reviews: number }[];
}

export interface Analytics {
  daily: AnalyticsDaily[];
  per_task: AnalyticsTask[];
  by_category: AnalyticsCategory[];
  outcomes: Record<string, number>;
  per_repo: Record<string, { tasks: number; cost: number }>;
  episodes: AnalyticsEpisode[];
  total_cost: number;
  total_tasks: number;
  reviewer?: ReviewerUsage;
}

export interface AgentModelUsage {
  role: string;
  model: string;
  calls: number;
  tokens_in: number;
  tokens_out: number;
  avg_latency_s: number | null;
  /** What the router was billed. From its own ledger, not an estimate. */
  cost_usd?: number;
  cached_tokens?: number;
  /** Share of prompt tokens the provider served from cache; null when the
   *  provider reports nothing, which is different from a real zero. */
  cache_hit_rate?: number | null;
  errors?: number;
}

interface ToolReliabilityEntry {
  tool: string;
  calls: number;
  errors: number;
  error_rate: number;
  /** Calls the harness pointed at a cheaper tool. Not errors -- the command
   * ran, it was just the expensive way to get the answer. */
  nudged?: number;
}

interface ToolNudge {
  /** "read" | "write" | "memory-read" | "memory-write" */
  kind: string;
  count: number;
}

interface ToolReliabilityDaily {
  date: string;
  errors: number;
}

export interface ToolReliability {
  tools: ToolReliabilityEntry[];
  daily: ToolReliabilityDaily[];
  nudges?: ToolNudge[];
}

// Top-level trace health from LangSmith -- one entry per root run (a
// work/verify pass, planning turn, or subagent invocation), not per
// individual llm/tool call the way AgentModelUsage/ToolReliability are.
export interface TraceSummary {
  trace_count: number;
  avg_latency_s: number | null;
  error_rate: number;
  total_input_tokens: number;
  total_output_tokens: number;
}

// Benchmarks: whether a change to the agent made it better -- see
// agent/benchmarks.py for what each number means and why it is a comparison
// between two windows rather than a single figure.
//
// Every rate is nullable on purpose: null is "nothing to divide by", and
// rendering it as 0 would show a regression that did not happen.
export interface BenchmarkWindow {
  tasks: number;
  shipped: number;
  escalated: number;
  outcomes: Record<string, number>;
  ship_rate: number | null;
  escalation_rate: number | null;
  first_pass_rate: number | null;
  reviewed: number;
  first_pass: number;
  iterations_median: number | null;
  iterations_p90: number | null;
  cost_median: number | null;
  cost_p90: number | null;
  cost_per_shipped_median: number | null;
  total_cost: number;
  memory_prompts: number;
  sections_offered: number;
  sections_read: number;
  section_reads_per_prompt: number | null;
  history_queries: number;
  history_used: number;
  history_follow_rate: number | null;
}

export interface Benchmarks {
  window_days: number;
  current: BenchmarkWindow;
  previous: BenchmarkWindow;
  /** Only the metrics where BOTH windows had a value -- a missing key means
   *  "unknown", which is not the same as "no change". */
  delta: Partial<Record<keyof BenchmarkWindow, number>>;
  /** Set when either window is too thin for the comparison to mean anything. */
  sample_warning: string | null;
}

// The roles this agent pins -- see agent/model_config.py's MANAGED_ROLES.
// That set INCLUDES agent-reviewer: the independent review service resolves
// its model through this API (services/commit-reviewer/reviewer.js reads the
// alias, not a model name).
//
// Aliases that do not begin agent- are deliberately not exposed here: this
// page only edits the seats this agent owns. Fallback targets
// (deepseek-v4-pro, claude-haiku-4.5, gpt-4o-mini) stay in the router config
// and are not listed.
export interface ModelPin {
  label: string;
  model: string;
  input_cost_per_token: number | null;
  output_cost_per_token: number | null;
  /** This role hands the model callable tools. */
  tools?: boolean;
  /** This role constrains the shape of the output. */
  structured?: boolean;
  /** Tools AND structured output in the SAME request — the combination that
   *  actually breaks models. Only the Consolidator needs it. */
  strict?: boolean;
  /** Plain-language description of what the role asks a model to do. */
  note?: string;
  /** Models probed against the real strict shape. */
  strict_ok?: string[];
  strict_bad?: string[];
  provider?: string | null;
}

export interface ModelCatalogEntry {
  id: string;
  name: string;
  context_length: number | null;
  input_cost_per_token: number;
  output_cost_per_token: number;
  knowledge_cutoff?: string | null;
  arena?: { category: string; elo: number; rank: number; win_rate: number } | null;
}

// --- Planning chat (agent/planning_chat.py) ---------------------------------
// A conversational research/design-consulting session, distinct from a
// TaskMeta/task: no plan/execute/verify graph, no budget, no write/edit/bash
// access to the repo. Its entire job is to converse and produce a
// plan_markdown document -- "Build Now" in the frontend hands that off to a
// real task via the ordinary POST /api/tasks, not a dedicated endpoint here.

/** What the planner's create_project tool recorded: a project the agent
 *  proposes to create for a NEW application the operator described
 *  mid-conversation. Nothing exists yet -- an admin answers it from the
 *  confirm card (POST .../new-project), which creates the project and moves
 *  the session onto it. */
export interface NewProjectProposal {
  name: string;
  description: string;
  github: boolean;
  proposed_by?: string | null;
}

export interface PlanningSessionMeta {
  session_id: string;
  repo: string;
  route?: "frontend" | "general";
  route_reason?: string | null;
  created_at: number;
  updated_at: number;
  title: string | null;
  plan_markdown: string | null;
  // No budget/cap for planning chat (unlike a task) -- this is purely
  // informational, computed the same way BudgetGuardMiddleware computes a
  // task's real spend, just against an uncapped tracker.
  cost_usd: number;
  // Set via "New Plan" -- closes out this conversation without deleting it
  // (still fully reachable), and drops it out of the sidebar's default
  // active list. Absent (not just false) on any session created before this
  // field existed.
  archived?: boolean;
  /** Same fixed taxonomy as TaskMeta.category (agent/classify.py), set once
   * on the session's first real message. Absent on a session with no
   * messages yet, or one created before this classifier existed. */
  category?: string | null;
  /** Why the last turn ended, persisted rather than only streamed. Before
   *  this, the reason existed solely as a live WebSocket event: refresh, or
   *  simply not be watching, and a turn that failed was indistinguishable
   *  from one that stopped for no reason. Absent on sessions predating it. */
  last_outcome?: "completed" | "stopped" | "stalled" | "budget" | "error" | null;
  last_outcome_detail?: string | null;
  last_outcome_at?: number | null;
  /** An unanswered create_project proposal. Absent or null once it has been
   *  confirmed (the session then lives under the new repo) or dismissed. */
  new_project?: NewProjectProposal | null;
}

export interface PlanningLogEntry {
  /** Content-derived, from the server (agent/log_stream.py): the same entry
   *  arriving by snapshot and by socket is recognisably one entry. Absent on
   *  the operator's own message until the server echoes it back. */
  id?: string;
  kind: "agent" | "tool-result" | "user";
  summary: string;
  detail: string;
  timestamp: string;
  model?: string | null;
  /** Which agent ROLE made the call (coder, test-writer, ...) — two roles can pin the same model, so the model badge alone is ambiguous. */
  role?: string | null;
}

export interface PlanningStreamEvent {
  /** "stopped" is emitted when the operator cancels a turn — distinct from
   *  "error" (a turn that failed) and from "closed" (which always follows and
   *  is what actually clears the running flag). */
  type: "log_entry" | "turn_complete" | "error" | "stopped" | "closed" | "ping" | "cost";
  /** Per-session position, on every content event (never on a ping). The
   *  snapshot reports where it sits in the same numbering. */
  seq?: number;
  entry?: PlanningLogEntry;
  plan_markdown?: string | null;
  cost_usd?: number;
  message?: string;
  /** turn_complete only: the proposal the turn persisted, so the confirm
   *  card appears without waiting for the sidebar's next poll. */
  new_project?: NewProjectProposal | null;
}

/* The golden eval suite (agent/routers/evals.py, evals/README.md). */
export interface EvalRunSummary {
  name: string;
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  notes: string;
  tasks_total: number | null;
  tasks_attempted: number | null;
  tasks_passed: number | null;
  pass_rate: number | null;
  total_cost_usd: number | null;
  stopped_early: boolean;
  full: boolean;
  /** Tasks run at once, each in its own workspace. */
  parallel?: number;
  by_category: Record<string, { tasks: number; passed: number }>;
  benchmarks: Partial<BenchmarkWindow> | null;
  failed: string[];
  results: Record<string, boolean>;
}

interface EvalStatus {
  running: boolean;
  pid: number | null;
  started_at: number | null;
  finished_at: number | null;
  notes: string;
  only: string[];
  tasks_total: number;
  done: number;
  passed: number;
  spent_usd: number;
  results: { id: string; passed: boolean; cost_usd: number; outcome: string }[];
  exit_code: number | null;
  report: string | null;
  stopped_by?: string;
}

export interface EvalsOverview {
  runs: EvalRunSummary[];
  status: EvalStatus | null;
  suite: { tasks: number; by_category: Record<string, number>; ids: string[]; error?: string };
  estimate: { cost_usd: number | null; duration_s: number | null; from_run: string | null };
}

interface EvalAssertionRow {
  kind: string;
  describe: string;
  ok: boolean;
  undetermined: boolean;
  detail: string;
}

interface EvalTaskRow {
  id: string;
  fixture: string;
  category: string;
  passed: boolean;
  outcome: string;
  escalation_reason: string | null;
  review_verdict: string | null;
  iterations: number;
  cost_usd: number;
  duration_s: number;
  changed_paths: string[];
  diff?: string;
  assertions: EvalAssertionRow[];
  error: string;
}

export interface EvalReport {
  tasks: EvalTaskRow[];
  summary: EvalRunSummary;
}

/* SWE-bench Verified runs (agent/routers/swebench.py, evals/SWEBENCH.md). */
export interface SwebenchRunSummary {
  name: string;
  /** full: all 500, the only advertisable number. sample: a seeded sample.
   *  selected: chosen tasks. diagnostic: an experiment, never a score. */
  kind: "full" | "sample" | "selected" | "diagnostic";
  state: "running" | "done" | "stopped";
  notes: string;
  started_at: string | null;
  finished_at: string | null;
  duration_s: number | null;
  total: number;
  done: number;
  graded: boolean;
  /** Final when graded; while running, resolved among the batches graded so far. */
  resolved: number | null;
  graded_count: number;
  resolved_rate: number | null;
  total_cost_usd: number | null;
  stopped_early: string | null;
  parallel: number | null;
  budget_usd: number | null;
  models: Record<string, number>;
  /** Set when the run was split across runner processes (--shard K/N): its shards' run names. */
  shards?: string[];
}

/** What the Start control sends (POST /swebench/run). sample 500 = the full run. */
export interface SwebenchStartOptions {
  sample: number;
  seed: number;
  parallel: number;
  budget_usd: number;
  notes?: string;
}

export interface SwebenchOverview {
  runs: SwebenchRunSummary[];
  dataset_size: number;
  gold_check: { checked: number; reference_fails: string[] };
}

interface SwebenchTests {
  patch_applied: boolean | null;
  fail_to_pass_failed: string[];
  fail_to_pass_passed: number;
  pass_to_pass_failed: string[];
  pass_to_pass_passed: number;
}

export interface SwebenchTaskRow {
  id: string;
  repo: string;
  outcome: string | null;
  reason: string | null;
  /** Absent until the official harness has graded it. */
  resolved?: boolean | null;
  cost_usd: number | null;
  duration_s: number | null;
  patch_bytes: number | null;
  review_verdict: string | null;
  models: Record<string, number>;
  started: boolean;
  reference_fails: boolean;
  tests: SwebenchTests | null;
  /** The harness's own reading of an unresolved task (its `failure_reasons`),
   *  e.g. "no_tests_collected" -- its note, not our failure. */
  harness_note?: string | null;
  has_trajectory: boolean;
  /** The run holding this task's files -- a shard, for a split run. */
  run?: string;
}

/** The reviewer's record of a task's last review (services/commit-reviewer),
 *  as the runner kept it. A run from before it was kept has only the verdict. */
export interface SwebenchReview {
  verdict: string | null;
  summary?: string | null;
  findings?: { severity: string; file?: string | null; issue: string }[];
  agentMessage?: string | null;
  escalated?: boolean;
  [key: string]: unknown;
}

/** One figure the runner samples once a minute while a run goes
 *  (agent/evals/host_metrics.py). Every one is host-wide. */
export type SwebenchHostField =
  | "mem_pct" | "mem_avail_gb" | "cpu_pct" | "load1" | "disk_pct" | "containers" | "oom_kills"
  | "router_calls" | "router_inflight" | "router_p50_s" | "router_p90_s" | "router_errors";

/** A sample: unix seconds plus whichever fields were read that minute. */
type SwebenchHostSample = { t: number } & { [k in SwebenchHostField]?: number };

export interface SwebenchHost {
  interval_s: number;
  samples: number;
  /** Max of each field over the run so far (over every sample, not only the thinned series). */
  peaks: { [k in SwebenchHostField]?: number };
  /** Thinned to <= 300 points, ascending t. */
  series: SwebenchHostSample[];
}

export interface SwebenchRun {
  summary: SwebenchRunSummary;
  tasks: SwebenchTaskRow[];
  /** The box and the model router while the run went; null for a run from before it was recorded. */
  host?: SwebenchHost | null;
}

interface SwebenchMessage {
  role: string;
  name: string | null;
  text: string;
  tool_calls: { name: string; args: string }[];
}

export interface SwebenchTaskDetail {
  id: string;
  patch: string | null;
  review?: SwebenchReview | null;
  conversation: { generation: number; namespace: string; messages: SwebenchMessage[] }[];
}
