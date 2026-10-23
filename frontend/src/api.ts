import type { RouterBalance, TaskMeta, TaskState, Analytics, AgentModelUsage, ModelPin, ModelCatalogEntry, ToolReliability, TraceSummary, Benchmarks, PlanningSessionMeta, PlanningLogEntry, CurrentUser } from "./types";

// import.meta.env.BASE_URL is Vite's own `base` config value: "/" in dev and
// in production alike now that the console has its own host (see
// vite.config.ts). Building API_BASE from it keeps working if a subpath ever
// comes back: dev's proxy and prod's nginx both forward {BASE_URL}api to the
// same backend.
const API_BASE = `${import.meta.env.BASE_URL.replace(/\/$/, "")}/api`;

// audit H-14: a typed error for an expired/invalid session, so callers and the
// central handler can distinguish "you are logged out" from any other failure.
// Previously every wrapper threw a generic Error with the status in a string,
// so an expired cookie degraded into stale data and opaque "listTasks failed:
// 401" messages with no path back to the login screen.
export class AuthError extends Error {
  constructor(message = "session expired") {
    super(message);
    this.name = "AuthError";
  }
}

// App registers a handler here so a 401 on ANY authenticated request clears the
// user and drops back to the login screen, from one place instead of ~50.
let onAuthFailure: (() => void) | null = null;
export function setAuthFailureHandler(fn: (() => void) | null): void {
  onAuthFailure = fn;
}

// Drop-in replacement for fetch on every AUTHENTICATED endpoint. A 401 fires the
// global handler and throws AuthError; everything else is returned unchanged for
// each caller's own res.ok handling. Pre-auth endpoints (login, 2FA verify,
// logout, forgot/reset password) deliberately stay on raw fetch -- a 401 there
// means "bad credentials", not "session expired", and must not trigger logout.
// audit M-20: a default request timeout so a hung request (an intermediary
// silently holding the connection) fails with a clear error instead of spinning
// forever. Callers that legitimately run long (probeForcedToolCall) pass a
// larger `timeoutMs`; a caller supplying its own AbortSignal opts out.
const DEFAULT_TIMEOUT_MS = 60_000;

async function apiFetch(
  input: RequestInfo | URL,
  init?: RequestInit,
  timeoutMs: number = DEFAULT_TIMEOUT_MS,
): Promise<Response> {
  const signal = init?.signal ?? AbortSignal.timeout(timeoutMs);
  let res: Response;
  try {
    // NOTE: this MUST call the global fetch, not apiFetch -- an earlier edit
    // accidentally made it recurse into itself, stack-overflowing every
    // authenticated call.
    res = await fetch(input, { ...init, signal });
  } catch (err) {
    if (err instanceof DOMException && err.name === "TimeoutError") {
      throw new Error(`request timed out after ${Math.round(timeoutMs / 1000)}s`);
    }
    throw err;
  }
  if (res.status === 401) {
    onAuthFailure?.();
    throw new AuthError();
  }
  return res;
}

// --- Auth --------------------------------------------------------------

export async function login(email: string, password: string): Promise<{ requires_2fa: boolean; temp_token?: string; user?: CurrentUser }> {
  const res = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `login failed: ${res.status}`);
  }
  return res.json();
}

export async function verify2FA(tempToken: string, code: string): Promise<{ user: CurrentUser }> {
  const res = await fetch(`${API_BASE}/auth/2fa/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ temp_token: tempToken, code }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `2FA verify failed: ${res.status}`);
  }
  return res.json();
}

// --- passkeys (agent/routers/passkeys.py) -------------------------------------

export interface Passkey {
  id: number;
  name: string;
  site: string;
  synced: boolean;
  created_at: string;
  last_used_at: string | null;
}

interface PasskeyChallenge {
  challenge_id: string;
  options: Record<string, unknown>;
}

async function passkeyCall<T>(path: string, init: RequestInit, fallback: string): Promise<T> {
  // Only /login/* is pre-auth. Everything else here is a signed-in call and
  // goes through apiFetch, so an expired session lands on the sign-in screen
  // and a hung request times out; until 2026-10-22 (audit U26) all of them
  // used raw fetch and showed "could not load passkeys: 401". A wrong
  // password on /register/options is a 403, so this never logs anyone out.
  const call = path.startsWith("/login/") ? fetch : apiFetch;
  const res = await call(`${API_BASE}/auth/passkeys${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers || {}) },
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${fallback}: ${res.status}`);
  }
  return res.json();
}

export function passkeyLoginOptions(): Promise<PasskeyChallenge> {
  return passkeyCall("/login/options", { method: "POST" }, "passkey sign-in failed");
}

export function passkeyLoginVerify(challengeId: string, credential: Record<string, unknown>): Promise<{ user: CurrentUser }> {
  return passkeyCall("/login/verify", { method: "POST", body: JSON.stringify({ challenge_id: challengeId, credential }) },
    "passkey sign-in failed");
}

export async function listPasskeys(): Promise<Passkey[]> {
  return (await passkeyCall<{ passkeys: Passkey[] }>("", { method: "GET" }, "could not load passkeys")).passkeys;
}

export function passkeyRegisterOptions(password: string): Promise<PasskeyChallenge> {
  return passkeyCall("/register/options", { method: "POST", body: JSON.stringify({ password }) }, "could not start");
}

export function passkeyRegisterVerify(challengeId: string, credential: Record<string, unknown>, name: string): Promise<Passkey> {
  return passkeyCall("/register/verify", { method: "POST", body: JSON.stringify({ challenge_id: challengeId, credential, name }) },
    "could not save the passkey");
}

export function renamePasskey(id: number, name: string): Promise<{ id: number; name: string }> {
  return passkeyCall(`/${id}`, { method: "PATCH", body: JSON.stringify({ name }) }, "could not rename");
}

export function deletePasskey(id: number): Promise<{ ok: boolean }> {
  return passkeyCall(`/${id}`, { method: "DELETE" }, "could not remove");
}

export async function logout(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, { method: "POST" });
}

export async function getMe(): Promise<CurrentUser> {
  const res = await apiFetch(`${API_BASE}/auth/me`);
  if (!res.ok) throw new Error(`getMe failed: ${res.status}`);
  return res.json();
}

export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/change-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `change password failed: ${res.status}`);
  }
}

/** Auto mode and the projects it covers. `repos` is required by the server
 *  when turning it ON: a switch whose blast radius nobody chose should not be
 *  the widest one (agent/routers/auth.py's _validated_auto_repos). */
export async function setAutoApprove(
  autoApproveCommands: boolean, repos?: string[],
): Promise<{ auto_approve_repos: string[] }> {
  const res = await apiFetch(`${API_BASE}/auth/me/auto-approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ auto_approve_commands: autoApproveCommands, repos }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `saving auto mode failed: ${res.status}`);
  }
  return res.json();
}

export interface AuditEntry {
  ts: number;
  actor: string;
  action: string;
  label: string;
  target: string | null;
  detail: string | null;
}

/** Who moved a control, newest first. Admin-only (agent/audit.py). */
export async function getAuditLog(limit = 50): Promise<AuditEntry[]> {
  const res = await apiFetch(`${API_BASE}/audit?limit=${limit}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `loading the audit log failed: ${res.status}`);
  }
  return (await res.json()).entries;
}

export async function forgotPassword(email: string): Promise<void> {
  await fetch(`${API_BASE}/auth/forgot-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email }),
  });
  // Always "ok" from the backend's own side (anti-enumeration) -- nothing
  // meaningful to throw on for a non-2xx. A network error still rejects, and
  // LoginPage shows it (audit U27: it was swallowed until 2026-10-22).
}

export async function resetPassword(email: string, code: string, newPassword: string): Promise<void> {
  const res = await fetch(`${API_BASE}/auth/reset-password`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, code, new_password: newPassword }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `reset password failed: ${res.status}`);
  }
}

export async function setup2FA(): Promise<{ secret: string; uri: string }> {
  const res = await apiFetch(`${API_BASE}/auth/2fa/setup`, { method: "POST" });
  if (!res.ok) throw new Error(`setup2FA failed: ${res.status}`);
  return res.json();
}

export async function confirm2FA(code: string): Promise<{ recovery_codes: string[] }> {
  const res = await apiFetch(`${API_BASE}/auth/2fa/confirm`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ code }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `confirm2FA failed: ${res.status}`);
  }
  return res.json();
}

// Requires the current password: removing a second factor is precisely what a
// stolen session would want to do, so the session alone must not authorise it.
export async function disable2FA(password: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/2fa/disable`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `disable2FA failed: ${res.status}`);
  }
}

export async function listUsers(): Promise<CurrentUser[]> {
  const res = await apiFetch(`${API_BASE}/auth/users`);
  if (!res.ok) throw new Error(`listUsers failed: ${res.status}`);
  return res.json();
}

export async function createUser(email: string, password: string, role: string, allowedRepos: string[] | null): Promise<CurrentUser> {
  const res = await apiFetch(`${API_BASE}/auth/users`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email, password, role, allowed_repos: allowedRepos }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `createUser failed: ${res.status}`);
  }
  return res.json();
}

export async function updateUserAccess(userId: number, allowedRepos: string[] | null): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/users/${userId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ allowed_repos: allowedRepos }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `updateUserAccess failed: ${res.status}`);
  }
}

export async function deleteUser(userId: number): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/users/${userId}`, { method: "DELETE" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `deleteUser failed: ${res.status}`);
  }
}

export async function listRepos(): Promise<string[]> {
  const res = await apiFetch(`${API_BASE}/repos`);
  if (!res.ok) throw new Error(`listRepos failed: ${res.status}`);
  return res.json();
}

export async function listTasks(repo?: string): Promise<TaskMeta[]> {
  const url = repo ? `${API_BASE}/tasks?repo=${encodeURIComponent(repo)}` : `${API_BASE}/tasks`;
  const res = await apiFetch(url);
  if (!res.ok) throw new Error(`listTasks failed: ${res.status}`);
  return res.json();
}

/** `seq` is where this snapshot sits in the task's event stream: the browser
 *  opens its socket before hydrating, buffers what arrives meanwhile, and on
 *  replay drops anything at or below this number (agent/log_stream.py). */
export async function getTask(taskId: string, repo: string): Promise<{ meta: TaskMeta; state: TaskState | null; orphaned: boolean; seq?: number }> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}?repo=${encodeURIComponent(repo)}`);
  if (!res.ok) throw new Error(`getTask failed: ${res.status}`);
  return res.json();
}

export interface AttachmentEntry {
  path: string;
  kind: string;
  bytes: number;
  extracted_text?: string | null;
  pages?: number;
  note?: string;
}

export async function uploadFiles(repo: string, files: File[]): Promise<AttachmentEntry[]> {
  const form = new FormData();
  for (const f of files) form.append("files", f);
  const res = await apiFetch(`${API_BASE}/uploads?repo=${encodeURIComponent(repo)}`, {
    method: "POST",
    body: form,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `upload failed: ${res.status}`);
  }
  return (await res.json()).files;
}

export async function createTask(
  goal: string, repo: string, budgetUsd?: number, attachments?: AttachmentEntry[], route: "auto" | "frontend" | "general" = "auto",
  planningSessionId?: string,
): Promise<{ task_id: string; route?: string; route_reason?: string }> {
  const res = await apiFetch(`${API_BASE}/tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      goal, repo, budget_usd: budgetUsd, attachments: attachments?.length ? attachments : null, route,
      planning_session_id: planningSessionId ?? null,
    }),
  });
  if (!res.ok) {
    // Say WHY: a 422 on Build Now (plan longer than the goal limit, 2026-09-09)
    // used to surface as a dead button.
    const body = await res.json().catch(() => ({}));
    const detail = typeof body.detail === "string" ? body.detail : Array.isArray(body.detail) ? body.detail.map((d: { msg?: string }) => d.msg).filter(Boolean).join("; ") : "";
    throw new Error(`createTask failed: ${res.status}${detail ? ` — ${detail}` : ""}`);
  }
  return res.json();
}

export async function sendTaskMessage(taskId: string, text: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
  });
  if (!res.ok) throw new Error(`sendTaskMessage failed: ${res.status}`);
}

export async function stopTask(taskId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/stop`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `stopTask failed: ${res.status}`);
  }
}

export async function getTaskDiff(taskId: string): Promise<import("./types").TaskDiff> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/diff`);
  if (!res.ok) throw new Error(`getTaskDiff failed: ${res.status}`);
  return res.json();
}

export interface TaskFile {
  path: string;
  /** Before the task touched it ("" for a file the task created). */
  original: string;
  /** As the task's branch has it now. */
  modified: string;
  /** The branch commit this was read from -- sent back on save, so an edit
   * can never land on a newer commit than the one it was made against. */
  sha: string;
  base: string;
}

export async function getTaskFile(taskId: string, path: string): Promise<TaskFile> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/file?path=${encodeURIComponent(path)}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `could not open ${path}: ${res.status}`);
  }
  return res.json();
}

export async function saveOperatorEdits(
  taskId: string,
  baseSha: string,
  files: { path: string; content: string }[],
  note?: string,
): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/edits`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ base_sha: baseSha, files, note }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `saving the edit failed: ${res.status}`);
  }
}

export async function submitMergeDecision(
  taskId: string,
  decision: "approve" | "request_changes",
  message?: string,
  /** The commit the operator was looking at. The server refuses (409) when
   * it is no longer the one parked for approval, so a decision made on one
   * diff can never land on a later commit (2026-09-29 audit, U3). */
  sha?: string | null,
): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/merge-decision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, message, sha: sha ?? undefined }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `merge decision failed: ${res.status}`);
  }
}

interface TelegramSettings {
  configured: boolean;
  chat_id: string | null;
}

export async function getTelegramSettings(): Promise<TelegramSettings> {
  const res = await apiFetch(`${API_BASE}/auth/me/telegram`);
  if (!res.ok) throw new Error(`getTelegramSettings failed: ${res.status}`);
  return res.json();
}

export async function setTelegramSettings(botToken: string, chatId: string): Promise<TelegramSettings> {
  const res = await apiFetch(`${API_BASE}/auth/me/telegram`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bot_token: botToken, chat_id: chatId }),
  });
  if (!res.ok) throw new Error(`setTelegramSettings failed: ${res.status}`);
  return res.json();
}

export async function sendTelegramTest(): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/me/telegram/test`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `test send failed: ${res.status}`);
  }
}

export interface ProviderEndpoint {
  provider: string;
  context_length: number | null;
  input_cost_per_token: number;
  output_cost_per_token: number;
  quantization: string | null;
  uptime: number | null;
  latency_s: number | null;
  throughput_tps: number | null;
  implicit_caching: boolean;
}

export async function getModelEndpoints(modelId: string): Promise<ProviderEndpoint[]> {
  const res = await apiFetch(`${API_BASE}/model-config/endpoints?model=${encodeURIComponent(modelId)}`);
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `provider list failed: ${res.status}`);
  }
  return (await res.json()).endpoints;
}

export async function saveProviderPins(pins: Record<string, string | null>): Promise<{ roles: Record<string, ModelPin> }> {
  const res = await apiFetch(`${API_BASE}/model-config/providers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pins }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => null);
    throw new Error(body?.detail ?? `provider save failed: ${res.status}`);
  }
  return res.json();
}

export async function setTheme(theme: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/me/theme`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ theme }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `saving the theme failed: ${res.status}`);
  }
}

// ── web push ───────────────────────────────────────────────────────────────

interface PushKeyResponse {
  public_key: string;
  subscriptions: number;
}

export async function getPushKey(): Promise<PushKeyResponse> {
  const res = await apiFetch(`${API_BASE}/push/key`);
  if (!res.ok) throw new Error(`push key unavailable: ${res.status}`);
  return res.json();
}

export async function subscribePush(body: {
  endpoint: string; p256dh: string; auth: string; label?: string;
}): Promise<{ subscriptions: number }> {
  const res = await apiFetch(`${API_BASE}/push/subscribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const b = await res.json().catch(() => ({}));
    throw new Error(b.detail || `subscribing failed: ${res.status}`);
  }
  return res.json();
}

// `auth` is the subscription's own secret: it lets this browser drop its
// endpoint even when the row belongs to the account it was signed into before.
export async function unsubscribePush(endpoint: string, auth?: string): Promise<{ subscriptions: number }> {
  const res = await apiFetch(`${API_BASE}/push/unsubscribe`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ endpoint, auth }),
  });
  if (!res.ok) throw new Error(`unsubscribing failed: ${res.status}`);
  return res.json();
}

export async function testPush(): Promise<{ sent: number; devices: number }> {
  const res = await apiFetch(`${API_BASE}/push/test`, { method: "POST" });
  if (!res.ok) {
    const b = await res.json().catch(() => ({}));
    throw new Error(b.detail || `test push failed: ${res.status}`);
  }
  return res.json();
}

export async function setMergeReview(requireMergeReview: boolean): Promise<void> {
  const res = await apiFetch(`${API_BASE}/auth/me/merge-review`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ require_merge_review: requireMergeReview }),
  });
  if (!res.ok) throw new Error(`setMergeReview failed: ${res.status}`);
}

export async function stopPlanningTurn(sessionId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/planning/sessions/${sessionId}/stop`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `stopPlanningTurn failed: ${res.status}`);
  }
}

export async function resumeTask(taskId: string, additionalBudgetUsd: number, message?: string): Promise<{ new_budget_usd: number }> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/resume`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ additional_budget_usd: additionalBudgetUsd, message: message || null }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `resumeTask failed: ${res.status}`);
  }
  return res.json();
}

export async function approveTask(taskId: string, decision: "approve" | "reject" | "respond", message?: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}/approve`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ decision, message: message || null }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `approveTask failed: ${res.status}`);
  }
}

export async function deleteTask(taskId: string, repo: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/tasks/${taskId}?repo=${encodeURIComponent(repo)}`, { method: "DELETE" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `deleteTask failed: ${res.status}`);
  }
}

export async function deletePlanningSession(sessionId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/planning/sessions/${encodeURIComponent(sessionId)}`, {
    method: "DELETE",
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `deletePlanningSession failed: ${res.status}`);
  }
}


export function taskStreamUrl(taskId: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${API_BASE}/tasks/${taskId}/stream`;
}

export async function getAgentModelUsage(): Promise<{ models: AgentModelUsage[] }> {
  const res = await apiFetch(`${API_BASE}/analytics/models`);
  if (!res.ok) throw new Error(`getAgentModelUsage failed: ${res.status}`);
  return res.json();
}

export async function getToolReliability(): Promise<ToolReliability> {
  const res = await apiFetch(`${API_BASE}/analytics/tool-reliability`);
  if (!res.ok) throw new Error(`getToolReliability failed: ${res.status}`);
  return res.json();
}

export async function getTraceSummary(): Promise<TraceSummary> {
  const res = await apiFetch(`${API_BASE}/analytics/trace-summary`);
  if (!res.ok) throw new Error(`getTraceSummary failed: ${res.status}`);
  return res.json();
}

/** Benchmarks, with one failure the others do not have.
 *
 *  The backend serves the SPA from a catch-all, so a route the running
 *  process does not have comes back as 200 index.html rather than 404 --
 *  which is exactly what a frontend deployed ahead of its backend sees. A
 *  bare res.json() turns that into "Unexpected token '<'". The content type
 *  is what actually distinguishes the two, so it is what is checked. */
export async function getBenchmarks(windowDays = 14): Promise<Benchmarks> {
  const res = await apiFetch(`${API_BASE}/analytics/benchmarks?window_days=${windowDays}`);
  if (res.status === 404 || !(res.headers.get("content-type") ?? "").includes("json")) {
    throw new Error("route-missing");
  }
  if (!res.ok) throw new Error(`getBenchmarks failed: ${res.status}`);
  return res.json();
}

export async function getAnalytics(): Promise<Analytics> {
  const res = await apiFetch(`${API_BASE}/analytics`);
  if (!res.ok) throw new Error(`getAnalytics failed: ${res.status}`);
  return res.json();
}

// Proxied through this app's own backend (GET /api/router-balance) rather
// than calling the review service's own endpoint directly. A deployment may
// sit the review service behind a separate reverse-proxy auth that this
// app's users have no session for; calling it directly then silently 401s
// and the balance vanishes from the sidebar with no visible error.
export async function getRouterBalance(): Promise<RouterBalance> {
  const res = await apiFetch(`${API_BASE}/router-balance`);
  if (!res.ok) throw new Error(`getRouterBalance failed: ${res.status}`);
  return res.json();
}

// This agent's own seven pinned roles (agent/model_config.py's
// MANAGED_ROLES) -- distinct from the two above, which read the shared
// review-service router dashboard.
export interface RouterRestart {
  /** false in the compose bundle: the router re-reads its config; there is no restart to offer. */
  available: boolean;
  note: string | null;
}

export async function getModelConfig(): Promise<{ roles: Record<string, ModelPin>; router_restart?: RouterRestart }> {
  const res = await apiFetch(`${API_BASE}/model-config`);
  if (!res.ok) throw new Error(`getModelConfig failed: ${res.status}`);
  return res.json();
}

interface ForcedToolCallInfo {
  compliant: string[];
  non_compliant: string[];
  /** Cannot be probed at all: batch-only endpoints, or no provider serving
   *  them under this account's data policy. Never becomes a pass. */
  unavailable?: string[];
  /** The probe itself was rate-limited (HTTP 429) — worth re-running. */
  transient?: string[];
  attempted?: number;
  catalog_size?: number;
  probed_at: string | null;
}

export interface ConsolidationStatus {
  ran_at: string | null;
  ok: boolean | null;
  exit_code: number | null;
  stale: boolean;
  age_hours?: number;
  tail: string;
  /** From the agent's own scheduler (agent/jobs.py). */
  due_at?: string | null;
  due?: boolean;
  running?: boolean;
  waiting?: string | null;
  trigger?: string | null;
  error?: string | null;
}

export interface JobStatus {
  name: string;
  title: string;
  ran_at: string | null;
  ok: boolean | null;
  exit_code: number | null;
  trigger: string | null;
  error: string | null;
  duration_s: number | null;
  due_at: string | null;
  due: boolean;
  running: boolean;
  waiting: string | null;
}

export async function getJobs(): Promise<{ jobs: JobStatus[] }> {
  const res = await apiFetch(`${API_BASE}/jobs`);
  if (!res.ok) throw new Error(`getJobs failed: ${res.status}`);
  return res.json();
}

export async function runJob(name: string): Promise<{ ok: boolean; started: string }> {
  const res = await apiFetch(`${API_BASE}/jobs/${encodeURIComponent(name)}/run`, { method: "POST" });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `runJob failed: ${res.status}`);
  return res.json();
}

export interface EnvKey {
  /** The row's identity when saving. Equals `key` unless the same variable
   *  lives in two .env files (MODEL_ROUTER_KEY is in the router's and the
   *  agent's); then each row has its own id. */
  id: string;
  key: string;
  label: string;
  help: string;
  group: string;
  secret: boolean;
  is_set: boolean;
  /** Masked hint for secrets (last 4 chars), full value for non-secrets.
   *  There is no endpoint that returns a secret's real value. */
  display: string;
  restarts: string[];
  file: string;
}

export interface EnvConfig {
  keys: EnvKey[];
  /** "compose": the bundle; values come from the host's .env and are read-only here. */
  managed_by: "compose" | "files";
  note: string | null;
}

export async function getEnvConfig(): Promise<EnvConfig> {
  const res = await apiFetch(`${API_BASE}/env-config`);
  if (!res.ok) throw new Error(`getEnvConfig failed: ${res.status}`);
  return res.json();
}

export async function saveEnvConfig(
  updates: Record<string, string>,
): Promise<{ updated: string[]; restart_required: string[] }> {
  const res = await apiFetch(`${API_BASE}/env-config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `save failed: ${res.status}`);
  return res.json();
}

export async function restartServices(services: string[]): Promise<{ restarted: Record<string, string> }> {
  const res = await apiFetch(`${API_BASE}/env-config/restart`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ updates: { services: services.join(",") } }),
  });
  if (!res.ok) throw new Error(`restart failed: ${res.status}`);
  return res.json();
}

export async function getConsolidationStatus(): Promise<ConsolidationStatus> {
  const res = await apiFetch(`${API_BASE}/consolidation/status`);
  if (!res.ok) throw new Error(`getConsolidationStatus failed: ${res.status}`);
  return res.json();
}

export async function probeForcedToolCall(): Promise<{
  ok: boolean;
  compliant: string[];
  non_compliant: string[];
  /** Cannot be probed at all: batch-only endpoints, or no provider serving
   *  them under this account's data policy. Never becomes a pass. */
  unavailable?: string[];
  /** The probe itself was rate-limited (HTTP 429) — worth re-running. */
  transient?: string[];
  attempted?: number;
  catalog_size?: number;
  probed_at: string | null;
  tail: string;
}> {
  const res = await apiFetch(`${API_BASE}/model-config/probe-forced-tool-call`, { method: "POST" }, 300_000);
  if (!res.ok) throw new Error(`probeForcedToolCall failed: ${res.status}`);
  return res.json();
}

export async function getModelCatalog(
  refresh = false,
): Promise<{ models: ModelCatalogEntry[]; forced_tool_call?: ForcedToolCallInfo }> {
  const res = await apiFetch(`${API_BASE}/model-config/catalog${refresh ? "?refresh=true" : ""}`);
  if (!res.ok) throw new Error(`getModelCatalog failed: ${res.status}`);
  return res.json();
}

export async function saveModelConfig(pins: Record<string, string>): Promise<{ roles: Record<string, ModelPin> }> {
  const res = await apiFetch(`${API_BASE}/model-config`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ pins }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `saveModelConfig failed: ${res.status}`);
  }
  return res.json();
}

export async function restartLlmRouter(): Promise<{
  ok: boolean;
  output: string;
  /** Whether the router answered its liveness route again. null when this
   *  deployment has no base URL to check. */
  healthy?: boolean | null;
  /** Whether pm2 accepted the restart command, which is NOT the same question. */
  restarted?: boolean;
  waited_s?: number;
}> {
  const res = await apiFetch(`${API_BASE}/model-config/restart-router`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `restartLlmRouter failed: ${res.status}`);
  }
  return res.json();
}

export async function createPlanningSession(repo: string, route: "auto" | "frontend" | "general" = "auto"): Promise<{ session_id: string; repo: string }> {
  const res = await apiFetch(`${API_BASE}/planning/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ repo, route }),
  });
  if (!res.ok) throw new Error(`createPlanningSession failed: ${res.status}`);
  return res.json();
}

export async function listPlanningSessions(): Promise<PlanningSessionMeta[]> {
  const res = await apiFetch(`${API_BASE}/planning/sessions`);
  if (!res.ok) throw new Error(`listPlanningSessions failed: ${res.status}`);
  return res.json();
}

export async function getPlanningSession(
  sessionId: string,
): Promise<{ meta: PlanningSessionMeta; log: PlanningLogEntry[]; running: boolean; seq?: number }> {
  const res = await apiFetch(`${API_BASE}/planning/sessions/${sessionId}`);
  if (!res.ok) throw new Error(`getPlanningSession failed: ${res.status}`);
  return res.json();
}

export async function archivePlanningSession(sessionId: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/planning/sessions/${sessionId}/archive`, { method: "POST" });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `archivePlanningSession failed: ${res.status}`);
  }
}

export interface NewProjectDecisionResult {
  /** The create result on confirm (ok:false carries the step list); null on dismiss. */
  project: CreateProjectResult | null;
  /** The session as it now stands: under the new repo after a successful
   *  confirm, unchanged (proposal still attached) after a failed one, and
   *  with the proposal cleared after a dismiss. */
  session: PlanningSessionMeta;
}

/** Answer the planner's create_project proposal. Admin-only. Confirm runs
 *  the same server path as createProject and then moves the session onto
 *  the new repo, so the conversation continues there. */
export async function decideNewProject(
  sessionId: string,
  body: { decision: "confirm" | "dismiss"; github?: boolean | null; token_name?: string | null },
): Promise<NewProjectDecisionResult> {
  // Same ceiling as createProject: a confirm is a create (git init, an
  // optional push, the cartographer's first pass) plus the row move.
  const res = await apiFetch(`${API_BASE}/planning/sessions/${sessionId}/new-project`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, 180_000);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function sendPlanningMessage(sessionId: string, text: string, attachments?: AttachmentEntry[]): Promise<void> {
  const res = await apiFetch(`${API_BASE}/planning/sessions/${sessionId}/message`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text, attachments: attachments?.length ? attachments : null }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `sendPlanningMessage failed: ${res.status}`);
  }
}

export function planningStreamUrl(sessionId: string): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${API_BASE}/planning/sessions/${sessionId}/stream`;
}

// --- Project onboarding wizard (agent/provisioning.py) ----------------------

// Same shape the rest of this file uses inline: prefer FastAPI's `detail`,
// fall back to the status code.
async function errText(res: Response): Promise<string> {
  const body = await res.json().catch(() => ({}));
  return (body as { detail?: string }).detail || `request failed: ${res.status}`;
}

export interface ProvisionCandidate {
  value: string;
  reason: string;
  enabled: boolean;
  warning: string | null;
}

interface CheckStep {
  name: string;
  dir: string;
  cmd: string;
  args: string[];
  timeoutMs?: number;
}

export interface DetectionReport {
  name: string;
  live: string;
  sandbox: string;
  is_git_repo: boolean;
  /* Set only by the clone route. Carried into provisioning so the project's
     `ship` default reflects how it arrived. */
  cloned_from_github?: boolean;
  cloned_to?: string;
  package_manager: string | null;
  languages: string[];
  node_modules_dirs: string[];
  /** Dependency trees the review checkout needs but git does not carry
   *  (PHP's vendor/, Elixir's deps/, a bundled Ruby project's vendor/bundle).
   *  The reviewer binds these read-only from the live checkout. */
  dependency_dirs: string[];
  checks: CheckStep[];
  build_steps: CheckStep[];
  pm2_apps: ProvisionCandidate[];
  /** How this machine could restart the project after a merge. Empty for a
   *  project this box does not run, which is most of them. */
  restart_commands: ProvisionCandidate[];
  secret_files: ProvisionCandidate[];
  read_only_mounts: ProvisionCandidate[];
  risky_scripts: ProvisionCandidate[];
  db_env_file: string | null;
  warnings: string[];
  blockers: string[];
  /** Memory kept from a project of this name that was removed earlier. The
   *  wizard offers to restore one; see agent/project_removal.py. */
  archives?: ProjectArchive[];
}

export interface ProvisionStep {
  step: string;
  ok: boolean;
  detail: string;
}

interface ProvisionResult {
  ok: boolean;
  steps: ProvisionStep[];
  message?: string;
}

export interface ProjectArchive {
  file: string;
  project: string;
  archived_at: string;
  item_count: number;
}

export interface RemoveProjectResult {
  ok: boolean;
  name: string;
  steps: ProvisionStep[];
  archive: string | null;
  /** The checkout that was left alone. Null when it was deleted instead. */
  live_untouched: string | null;
  /** The checkout that was deleted. Null when it was left where it was. */
  live_removed: string | null;
}

/** Whether the live checkout could be deleted along with the project, and the
 *  reason either way. The reason is shown to the operator verbatim: "you may
 *  not delete this" is only useful with the because. */
export interface CheckoutRemovable {
  live: string;
  removable: boolean;
  reason: string;
}

export async function checkoutRemovable(name: string): Promise<CheckoutRemovable> {
  const res = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(name)}/checkout`);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function moveProject(name: string, live: string): Promise<{ ok: boolean; live: string; sandbox: string | null; unchanged: boolean }> {
  const res = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(name)}/move`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ live }),
  });
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `moveProject failed: ${res.status}`);
  return res.json();
}

export async function removeProject(
  name: string, memory: "archive" | "delete", files: "keep" | "delete" = "keep",
): Promise<RemoveProjectResult> {
  const res = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(name)}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ memory, files }),
  }, 120_000);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function listProjectArchives(): Promise<{ archives: ProjectArchive[] }> {
  const res = await apiFetch(`${API_BASE}/projects/archives`);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function deleteProjectArchive(file: string): Promise<void> {
  const res = await apiFetch(`${API_BASE}/projects/archives/${encodeURIComponent(file)}`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await errText(res));
}

export async function listProjectsConfig(): Promise<{
  projects: Record<string, { live: string; sandbox: string }>;
  config_path: string;
}> {
  const res = await apiFetch(`${API_BASE}/projects`);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

/* Everything the wizard sends is a path today. `owner/repo` and
   `relative/path` are the same string, so only an unmistakable URL switches
   the button to Clone -- guessing wrong here means cloning a directory name. */
export function looksLikeGitHubUrl(text: string): boolean {
  const t = text.trim();
  return /^https:\/\/(www\.)?github\.com\/[\w.-]+\/[\w.-]+(\.git)?\/?$/.test(t)
    || /^(ssh:\/\/)?git@github\.com[:/][\w.-]+\/[\w.-]+(\.git)?\/?$/.test(t);
}

export interface GitHubRepo {
  slug: string;
  name: string;
  owner: string;
  private: boolean;
  archived: boolean;
  default_branch: string;
  push: boolean;
  pushed_at: string | null;
  /** The project name it is already onboarded as, matched on the remote each
   *  checkout actually has -- not on how it was added. */
  onboarded_as: string | null;
}

/* Every repository a token can reach. The token carries its own grant, so
   this goes stale when that changes in GitHub rather than when somebody
   remembers to update a list here. */
export async function listGitHubRepos(body: { name?: string; token?: string }): Promise<{
  repos: GitHubRepo[];
  onboarded: string[];
}> {
  const res = await apiFetch(`${API_BASE}/github/repos`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

/* Clone and provision in one step, with the answers the wizard would have
   pre-ticked. The long way round still exists for a project with unusual
   checks. */
export async function onboardFromGitHub(body: {
  slug: string;
  token_name?: string;
  ship?: "push" | "pr";
}): Promise<{ ok: boolean; name: string; path: string; ship: string; steps: ProvisionStep[] }> {
  // A clone of a real repository plus the wizard's own steps runs past the
  // default 60s. The map is built in the background server-side, so this is
  // the clone and nothing slower.
  const res = await apiFetch(`${API_BASE}/projects/onboard-github`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, 180_000);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

/* Clones first, then detects, and says so in its name: /detect promises to
   create nothing and an operator pasting a URL is entitled to that promise. */
export async function cloneProject(source: string): Promise<DetectionReport> {
  // Same ceiling as the one-step add: this is a clone over the network.
  const res = await apiFetch(`${API_BASE}/projects/clone`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path: source }),
  }, 180_000);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function detectProject(path: string): Promise<DetectionReport> {
  const res = await apiFetch(`${API_BASE}/projects/detect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function provisionProject(body: {
  // The server re-detects from `path` and derives the live/worktree
  // locations itself -- the client cannot name them.
  path: string;
  choices: Record<string, unknown>;
  grant_access: boolean;
  /** An archive FILENAME from the detection report, never a path. */
  restore_archive?: string | null;
}): Promise<ProvisionResult> {
  // The worktree and the starter memory, not the map -- that is started in
  // the background -- but a worktree on a large repo is still not instant.
  const res = await apiFetch(`${API_BASE}/projects/provision`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, 180_000);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export interface CreateProjectResult {
  ok: boolean;
  name: string;
  live: string;
  steps: ProvisionStep[];
  github: { full_name: string; html_url: string } | null;
  message?: string;
}

/** Create a brand-new project: a directory under an allowed root with an
 *  initial commit, optionally a private GitHub repo, then registered exactly
 *  as the onboarding wizard would. Admin-only. */
export async function createProject(body: {
  name: string;
  description?: string;
  github?: boolean;
  token_name?: string | null;
}): Promise<CreateProjectResult> {
  // A git push plus the cartographer's first pass over the new checkout can
  // run past the default 60s; 3 minutes is the server's own ceiling for it.
  const res = await apiFetch(`${API_BASE}/projects/create`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  }, 180_000);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

// --- Per-project deploy keys (agent/deploy_keys.py) -------------------------

export interface DeployKeyStatus {
  project: string;
  installed: boolean;
  fingerprint: string | null;
  public_key: string | null;
  remote: string | null;
  remote_kind: "ssh" | "https" | null;
  configured: boolean;
  detail: string | null;
}

export async function getDeployKey(project: string): Promise<DeployKeyStatus> {
  const res = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(project)}/deploy-key`);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function installDeployKey(project: string, privateKey: string): Promise<DeployKeyStatus> {
  const res = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(project)}/deploy-key`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ private_key: privateKey }),
  });
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function generateDeployKey(project: string): Promise<DeployKeyStatus> {
  const res = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(project)}/deploy-key/generate`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function testDeployKey(project: string): Promise<{ ok: boolean; detail: string }> {
  const res = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(project)}/deploy-key/test`, {
    method: "POST",
  });
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function deleteDeployKey(project: string): Promise<DeployKeyStatus> {
  const res = await apiFetch(`${API_BASE}/projects/${encodeURIComponent(project)}/deploy-key`, {
    method: "DELETE",
  });
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

// --- Runtime limits (admin) ------------------------------------------

export interface RuntimeKnob {
  label: string;
  help: string;
  unit: string;
  default: number;
  min: number;
  max: number;
  group: string;
}

export async function getRuntimeSettings(): Promise<{ knobs: Record<string, RuntimeKnob>; values: Record<string, number> }> {
  const res = await apiFetch(`${API_BASE}/settings/runtime`);
  if (!res.ok) throw new Error(`getRuntimeSettings failed: ${res.status}`);
  return res.json();
}

/** Open to every account, unlike getRuntimeSettings (admin-only). */
export async function getTaskDefaults(): Promise<{ default_task_budget_usd: number }> {
  const res = await apiFetch(`${API_BASE}/settings/task-defaults`);
  if (!res.ok) throw new Error(`getTaskDefaults failed: ${res.status}`);
  return res.json();
}

export async function saveRuntimeSettings(values: Record<string, number>): Promise<{ values: Record<string, number> }> {
  const res = await apiFetch(`${API_BASE}/settings/runtime`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ values }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `saveRuntimeSettings failed: ${res.status}`);
  }
  return res.json();
}

// --- GitHub integration (agent/github_settings.py, agent/github_inbox.py) ---

export type GitHubMode = "off" | "propose" | "auto";
export type GitHubSource = "dependabot_prs" | "security_alerts" | "review_requests" | "ci_failures" | "code_scanning";

export interface GitHubProjectSettings {
  token: string | null;
  policies: Record<GitHubSource, GitHubMode>;
  budget_usd: number;
  max_open_auto: number;
  authors: "dependabot" | "bots" | "anyone";
  route: "auto" | "frontend" | "general";
}

export interface GitHubSettings {
  poll_interval_min: number;
  public_url: string;
  notify: { telegram: boolean; email: boolean; email_to: string };
  /** name -> hint/date only; the token value never leaves the server. */
  tokens: Record<string, { hint: string; created_at: number | null }>;
  projects: Record<string, GitHubProjectSettings>;
}

export interface GitHubSettingsResponse {
  settings: GitHubSettings;
  sources: Record<GitHubSource, { label: string; help: string }>;
  modes: GitHubMode[];
  author_filters: string[];
  env_token: boolean;
  projects: string[];
}

export interface GitHubSettingsPatch {
  poll_interval_min?: number;
  public_url?: string;
  notify?: { telegram: boolean; email: boolean; email_to: string };
  add_tokens?: Record<string, string>;
  remove_tokens?: string[];
  /** {old: new}. Projects point at a token by name, so the server moves them
   *  with it -- otherwise every one silently falls back to the env token. */
  rename_tokens?: Record<string, string>;
  projects?: Record<string, Partial<GitHubProjectSettings> & { policies?: Partial<Record<GitHubSource, GitHubMode>> }>;
}

export async function getGitHubSettings(): Promise<GitHubSettingsResponse> {
  const res = await apiFetch(`${API_BASE}/settings/github`);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function saveGitHubSettings(patch: GitHubSettingsPatch): Promise<{ settings: GitHubSettings }> {
  const res = await apiFetch(`${API_BASE}/settings/github`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export interface GitHubTokenProbe {
  ok: boolean;
  error?: string;
  warning?: string;
  login?: string | null;
  repos: { slug: string; project: string | null; push: boolean; pull: boolean }[];
  matched: { slug: string; project: string | null; push: boolean; pull: boolean; dependabot_alerts?: boolean | null; checks?: boolean | null }[];
  /** Configured projects the token could not reach, each with the reason. */
  unreached?: { slug: string | null; project: string; error: string }[];
}

/** Test a stored token by name, or a pasted one before it is saved. */
export async function testGitHubToken(arg: { name?: string; token?: string }): Promise<GitHubTokenProbe> {
  const res = await apiFetch(`${API_BASE}/settings/github/test`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(arg),
  }, 30_000);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export type GitHubItemState = "seen" | "proposed" | "task_created" | "awaiting_rescan" | "dismissed" | "snoozed" | "resolved";

/** An earlier task that merged a fix GitHub did not accept. */
export interface GitHubItemAttempt {
  task_id: string;
  finished_at: number;
  still_open: number[];
}

export interface GitHubInboxItem {
  key: string;
  kind: GitHubSource;
  repo: string;
  title: string;
  url: string;
  number: number | null;
  author: string | null;
  summary: string;
  state: GitHubItemState;
  mode: GitHubMode;
  reason: string;
  task_id: string | null;
  created_at: number;
  updated_at: number;
  snoozed_until: number | null;
  attempts?: GitHubItemAttempt[];
}

interface GitHubInboxResponse {
  items: GitHubInboxItem[];
  last_poll: { at: number; results: { repo: string; found?: number; proposed?: number; created?: number; resolved?: number; skipped?: string; error?: string }[] } | null;
}

export async function getGitHubInbox(): Promise<GitHubInboxResponse> {
  const res = await apiFetch(`${API_BASE}/github/inbox`);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function actOnGitHubItem(repo: string, key: string, action: "approve" | "dismiss" | "snooze", days?: number): Promise<{ item: GitHubInboxItem; task_id: string | null }> {
  const res = await apiFetch(`${API_BASE}/github/inbox/${encodeURIComponent(repo)}/${encodeURIComponent(key)}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(days !== undefined ? { days } : {}),
  });
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

export async function pollGitHubNow(): Promise<{ results: GitHubInboxResponse["last_poll"] extends infer T ? (T extends { results: infer R } ? R : never) : never }> {
  const res = await apiFetch(`${API_BASE}/github/poll`, { method: "POST" }, 120_000);
  if (!res.ok) throw new Error(await errText(res));
  return res.json();
}

// ---------------------------------------------------------------------------
// The golden eval suite (admin). A run is a detached process on the server;
// these read its history and progress and start or stop one.
// ---------------------------------------------------------------------------

async function evalsJson<T>(res: Response, what: string): Promise<T> {
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${what} failed: ${res.status}`);
  }
  return res.json();
}

export async function getEvals(): Promise<import("./types").EvalsOverview> {
  return evalsJson(await apiFetch(`${API_BASE}/evals`), "loading evals");
}

export async function getEvalRun(name: string): Promise<import("./types").EvalReport> {
  return evalsJson(await apiFetch(`${API_BASE}/evals/runs/${encodeURIComponent(name)}`), "loading the run");
}

export async function startEvalRun(
  notes: string, only?: string[], parallel = 1,
): Promise<{ ok: boolean; tasks: number }> {
  return evalsJson(await apiFetch(`${API_BASE}/evals/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ notes, only: only && only.length ? only : null, parallel }),
  }), "starting the run");
}

export async function stopEvalRun(): Promise<void> {
  await evalsJson(await apiFetch(`${API_BASE}/evals/stop`, { method: "POST" }), "stopping the run");
}

export async function getSwebench(): Promise<import("./types").SwebenchOverview> {
  return evalsJson(await apiFetch(`${API_BASE}/swebench`), "loading SWE-bench runs");
}

export async function getSwebenchRun(name: string): Promise<import("./types").SwebenchRun> {
  return evalsJson(await apiFetch(`${API_BASE}/swebench/runs/${encodeURIComponent(name)}`), "loading the run");
}

/** Start a SWE-bench run on the server (POST /swebench/run, 202). `sample`
 *  of 500 is the full run. 409 when a run is already in progress, 400 on bad
 *  input -- both carry a `detail` that evalsJson turns into the message. */
export async function startSwebenchRun(
  opts: import("./types").SwebenchStartOptions,
): Promise<{ ok: true; name: string; shards: string[] }> {
  return evalsJson(await apiFetch(`${API_BASE}/swebench/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      sample: opts.sample, seed: opts.seed, parallel: opts.parallel, budget_usd: opts.budget_usd, notes: opts.notes,
    }),
  }), "starting the run");
}

/** Stop a run by its name from the runs list; a combined run's name stops
 *  all of its shards. 409 when nothing is running. */
export async function stopSwebenchRun(name: string): Promise<{ ok: true; stopped: string[] }> {
  return evalsJson(
    await apiFetch(`${API_BASE}/swebench/runs/${encodeURIComponent(name)}/stop`, { method: "POST" }),
    "stopping the run",
  );
}

/** The last `lines` lines of the runner's log for a run. */
export async function getSwebenchRunLog(name: string, lines = 80): Promise<{ lines: string[] }> {
  return evalsJson(
    await apiFetch(`${API_BASE}/swebench/runs/${encodeURIComponent(name)}/log?lines=${lines}`),
    "loading the runner log",
  );
}

export async function getSwebenchTask(run: string, id: string): Promise<import("./types").SwebenchTaskDetail> {
  return evalsJson(
    await apiFetch(`${API_BASE}/swebench/runs/${encodeURIComponent(run)}/tasks/${encodeURIComponent(id)}`),
    "loading the task",
  );
}
