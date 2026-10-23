import { useEffect, useState } from "react";
import { SetupTotpPage } from "./SetupTotpPage";
import { RuntimeLimitsPanel } from "./RuntimeLimitsPanel";
import { SettingsSaveBar, SettingsSaveProvider } from "./SettingsSaveBar";
import { PasskeysCard } from "./PasskeysCard";
import { changePassword, disable2FA, setAutoApprove, setMergeReview, getTelegramSettings, setTelegramSettings, sendTelegramTest, listProjectsConfig } from "../api";
import type { CurrentUser } from "../types";
import "./SettingsPage.css";
import { ApiKeysPanel } from "./ApiKeysPanel";
import { AppearancePanel } from "./AppearancePanel";
import { PushPanel } from "./PushPanel";
import { ProjectsPanel } from "./ProjectsPanel";
import { GitHubSettingsCard } from "./GitHubSettingsCard";
import { AuditLogCard } from "./AuditLogCard";

function sameRepos(a: string[], b: string[]): boolean {
  return a.length === b.length && [...a].sort().every((v, i) => v === [...b].sort()[i]);
}

type SectionId =
  | "account" | "appearance" | "agent" | "notifications" | "projects"
  | "github" | "limits" | "environment" | "audit";

/** Where the operator was last time. Settings is a place people come back to
 *  mid-task -- landing on "Account" every time is a small tax on every visit.
 *  Per browser, and a stale/unknown value simply falls back to the first
 *  section the account can see. */
const LAST_SECTION_KEY = "settings.section";

function rememberedSection(): SectionId {
  try {
    return (localStorage.getItem(LAST_SECTION_KEY) as SectionId) || "account";
  } catch {
    return "account";
  }
}

interface Props {
  user: CurrentUser;
  onUserChanged: (user: CurrentUser) => void;
  /** A project was provisioned in the Projects wizard; App reloads its repos. */
  onProjectsChanged?: () => void | Promise<void>;
}

export function SettingsPage({ user, onUserChanged, onProjectsChanged }: Props) {
  const [section, setSectionState] = useState<SectionId>(rememberedSection);

  function setSection(id: SectionId) {
    setSectionState(id);
    try {
      localStorage.setItem(LAST_SECTION_KEY, id);
    } catch {
      /* a browser with site data blocked still navigates, it just forgets */
    }
  }

  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [pwSaving, setPwSaving] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);
  const [pwDone, setPwDone] = useState(false);
  const [pwPasskeysRemoved, setPwPasskeysRemoved] = useState(0);

  // Two-factor. Required (and forced at sign-in) for admins; optional for
  // everyone else, who can turn it on here and off again with their
  // password -- the same password /2fa/disable demands, because dropping a
  // second factor is exactly what a stolen session would want to do.
  const [enrolling2fa, setEnrolling2fa] = useState(false);
  const [tfaPassword, setTfaPassword] = useState("");
  const [tfaSaving, setTfaSaving] = useState(false);
  const [tfaError, setTfaError] = useState<string | null>(null);

  async function handleDisable2fa(e: React.FormEvent) {
    e.preventDefault();
    setTfaSaving(true);
    setTfaError(null);
    try {
      await disable2FA(tfaPassword);
      setTfaPassword("");
      onUserChanged({ ...user, totp_enabled: false });
    } catch (err) {
      setTfaError(err instanceof Error ? err.message : "could not turn off 2FA");
    } finally {
      setTfaSaving(false);
    }
  }

  const [autoSaving, setAutoSaving] = useState(false);
  const [autoError, setAutoError] = useState<string | null>(null);
  // Turning auto mode ON asks for a second, explicit confirmation; turning it
  // back OFF is always allowed immediately. Friction belongs on the side that
  // removes a safety prompt, never on the side that restores one.
  const [confirmingAuto, setConfirmingAuto] = useState(false);
  // Auto mode is per project. The picker starts from whatever the account
  // already covers, so turning it off and on again does not mean re-choosing
  // every project.
  const [projects, setProjects] = useState<string[]>([]);
  const [autoRepos, setAutoRepos] = useState<string[]>(user.auto_approve_repos ?? []);

  useEffect(() => {
    listProjectsConfig()
      .then((r) => setProjects(Object.keys(r.projects)))
      .catch(() => setProjects([]));
  }, []);

  function toggleRepo(name: string) {
    setAutoRepos((prev) => (prev.includes(name) ? prev.filter((r) => r !== name) : [...prev, name]));
  }

  async function handlePasswordSave(e: React.FormEvent) {
    e.preventDefault();
    setPwError(null);
    setPwDone(false);
    if (next !== confirm) {
      setPwError("The two new-password fields don't match.");
      return;
    }
    setPwSaving(true);
    try {
      const { passkeysRemoved } = await changePassword(current, next);
      setPwPasskeysRemoved(passkeysRemoved);
      setCurrent("");
      setNext("");
      setConfirm("");
      setPwDone(true);
    } catch (err) {
      setPwError(err instanceof Error ? err.message : "change password failed");
    } finally {
      setPwSaving(false);
    }
  }

  const [mergeSaving, setMergeSaving] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);

  async function applyMergeReview(value: boolean) {
    setMergeError(null);
    setMergeSaving(true);
    try {
      await setMergeReview(value);
      onUserChanged({ ...user, require_merge_review: value });
    } catch (err) {
      setMergeError(err instanceof Error ? err.message : "saving merge review failed");
    } finally {
      setMergeSaving(false);
    }
  }

  async function applyAuto(value: boolean, repos?: string[]) {
    setAutoError(null);
    setAutoSaving(true);
    try {
      const res = await setAutoApprove(value, repos);
      onUserChanged({
        ...user,
        auto_approve_commands: value,
        auto_approve_repos: res.auto_approve_repos ?? user.auto_approve_repos,
      });
      setConfirmingAuto(false);
    } catch (err) {
      setAutoError(err instanceof Error ? err.message : "saving auto mode failed");
    } finally {
      setAutoSaving(false);
    }
  }

  // One section at a time, chosen from the rail on the left.
  //
  // This page used to be every card stacked in one 4300px scroll: eleven cards
  // at three different widths, two-up rows whose heights never matched (a
  // 735px "Auto mode" beside a 332px "Final merge review" left 400px of dead
  // space), and two unrelated cards both titled "GitHub". Nothing told you
  // where you were or what else there was.
  //
  // The rail is the table of contents the page never had, and showing one
  // section at a time means each one gets a single readable column instead of
  // a grid that has to look right at every combination of card heights.
  // `wide` opts a section out of the reading-width cap. Prose and form rows
  // want ~760px; a table does not -- capping the GitHub per-project grid at a
  // reading width clipped its Budget column behind a horizontal scrollbar
  // while 300px of screen sat empty to the right of it.
  const sections: { id: SectionId; label: string; blurb: string; admin?: boolean; wide?: boolean }[] = [
    { id: "account", label: "Account", blurb: "Who you are signed in as, your password, and two-factor." },
    { id: "appearance", label: "Appearance", blurb: "The console's colour scheme, for this account." },
    { id: "agent", label: "Agent behavior", blurb: "How much the agent does without stopping to ask." },
    { id: "notifications", label: "Notifications", blurb: "Where the agent reaches you." },
    { id: "projects", label: "Projects", blurb: "The repositories this deployment can work on.", admin: true },
    { id: "github", label: "GitHub", blurb: "Tokens, the inbox, and what it is allowed to start.", admin: true, wide: true },
    { id: "limits", label: "Runtime limits", blurb: "How hard the agent tries before giving up.", admin: true, wide: true },
    { id: "environment", label: "Environment", blurb: "API keys and integration settings.", admin: true, wide: true },
    { id: "audit", label: "Audit log", blurb: "Who moved a control, and when.", admin: true, wide: true },
  ];
  const visible = sections.filter((s) => !s.admin || user.role === "admin");
  const active = visible.find((s) => s.id === section) ?? visible[0];

  if (enrolling2fa) {
    // The same full-screen flow an admin is forced through at sign-in:
    // QR code, confirm a code, then the recovery codes shown once.
    return (
      <SetupTotpPage
        onDone={() => {
          setEnrolling2fa(false);
          onUserChanged({ ...user, totp_enabled: true });
        }}
      />
    );
  }

  return (
    <SettingsSaveProvider>
    <div className="settings-page">
      <nav className="settings-nav" aria-label="Settings sections">
        <h1 className="settings-title">Settings</h1>
        <p className="settings-account">
          <strong>{user.email}</strong>
          <span className="settings-role">{user.role}</span>
        </p>
        <ul className="settings-nav-list">
          {visible.map((s) => (
            <li key={s.id}>
              <button
                type="button"
                className={`settings-nav-item ${s.id === active.id ? "is-current" : ""}`}
                aria-current={s.id === active.id ? "page" : undefined}
                onClick={() => setSection(s.id)}
              >
                {s.label}
              </button>
            </li>
          ))}
        </ul>
      </nav>

      <div className={`settings-content ${active.wide ? "settings-content--wide" : ""}`}>
        <header className="settings-content-head">
          <h2 className="settings-content-title">{active.label}</h2>
          <p className="settings-content-blurb">{active.blurb}</p>
        </header>

        {active.id === "account" && (
          <div className="settings-stack">
            <section className="settings-card">
              <h2>Change password</h2>
              <form onSubmit={handlePasswordSave} className="settings-form">
                <label className="field">
                  <span>Current password</span>
                  <input
                    type="password"
                    autoComplete="current-password"
                    value={current}
                    onChange={(e) => setCurrent(e.target.value)}
                    required
                  />
                </label>
                <label className="field">
                  <span>New password</span>
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={next}
                    onChange={(e) => setNext(e.target.value)}
                    required
                  />
                </label>
                <label className="field">
                  <span>Confirm new password</span>
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={confirm}
                    onChange={(e) => setConfirm(e.target.value)}
                    required
                  />
                </label>
                <p className="settings-hint">
                  At least 12 characters, with an uppercase letter, a lowercase letter, and a digit.
                </p>
                {pwError && <div className="settings-error">{pwError}</div>}
                {pwDone && (
                  <div className="settings-ok" role="status">
                    Password updated.
                    {pwPasskeysRemoved > 0 && ` Your ${pwPasskeysRemoved === 1 ? "passkey was" : `${pwPasskeysRemoved} passkeys were`} removed with it; add a new one under Passkeys.`}
                  </div>
                )}
                <button className="submit-btn" type="submit" disabled={pwSaving || !current || !next || !confirm}>
                  {pwSaving ? "Saving…" : "Update password"}
                </button>
              </form>
            </section>

            <section className="settings-card">
              <h2>
                Two-factor authentication{" "}
                <span className={`settings-pill ${user.totp_enabled ? "settings-pill--on" : ""}`}>
                  {user.totp_enabled ? "ON" : "OFF"}
                </span>
              </h2>
              {user.role === "admin" ? (
                <p className="settings-card-sub">
                  Required on admin accounts, so it cannot be turned off here. Lost your authenticator? Sign in
                  with one of your recovery codes. With none left, it is a reset on the server — see{" "}
                  <code>docs/runbooks/two-factor.md</code>.
                </p>
              ) : user.totp_enabled ? (
                <form onSubmit={handleDisable2fa} className="settings-form">
                  <p className="settings-card-sub">
                    A code from your authenticator app is asked for at sign-in. Turning it off needs your
                    password, and deletes your recovery codes.
                  </p>
                  <label className="field">
                    <span>Current password</span>
                    <input
                      type="password"
                      autoComplete="current-password"
                      value={tfaPassword}
                      onChange={(e) => setTfaPassword(e.target.value)}
                      required
                    />
                  </label>
                  {tfaError && <div className="settings-error">{tfaError}</div>}
                  <button className="submit-btn" type="submit" disabled={tfaSaving || !tfaPassword}>
                    {tfaSaving ? "Turning off…" : "Turn off 2FA"}
                  </button>
                </form>
              ) : (
                <>
                  <p className="settings-card-sub">
                    Optional for your account: a code from an authenticator app at sign-in, on top of your
                    password.
                  </p>
                  <button className="submit-btn" type="button" onClick={() => setEnrolling2fa(true)}>
                    Turn on 2FA
                  </button>
                </>
              )}
            </section>

            <PasskeysCard />
          </div>
        )}

        {active.id === "agent" && (
          <div className="settings-stack">
            <section className={`settings-card ${user.auto_approve_commands ? "settings-card--armed" : ""}`}>
              <h2>
                Auto mode
                <span className={`settings-pill ${user.auto_approve_commands ? "settings-pill--on" : ""}`}>
                  {user.auto_approve_commands ? "ON" : "OFF"}
                </span>
              </h2>
              <p className="settings-body">
                Normally the agent pauses and asks before it touches something sensitive — a config file,
                a <code>.env</code>, anything under <code>.git/</code> or <code>.github/workflows</code>.
                That prompt is what makes a long task need babysitting. Auto mode runs those without
                asking, so a task can finish unattended.
              </p>
              <div className="settings-note settings-note--keep">
                <strong>Still always asks, even with auto mode on:</strong>
                <ul>
                  <li>
                    Deletions that lose work — anything git tracks in the checkout, the checkout itself,
                    anything under <code>.git</code>, a path outside the sandbox, <code>git clean -f</code>,
                    or a target it cannot read (a shell variable). Deleting its own scratch — a probe
                    script it just wrote, <code>/tmp</code>, a build folder like <code>dist</code> or{" "}
                    <code>node_modules</code> — runs without asking: git never heard of it, so nothing
                    reaches your diff.
                  </li>
                  <li>Questions the agent asks you directly, so it gets your real answer</li>
                </ul>
              </div>
              <div className="settings-note settings-note--warn">
                <strong>What you give up:</strong> the agent edits config, secrets-adjacent files, CI
                workflows and deploy config with no prompt, and runs shell commands that strict mode
                stops for (<code>sudo</code>, a force push, <code>chmod -R</code>). Every command runs in
                a throwaway container with no credentials, no privileges and only the task's own checkout
                mounted, so those cannot reach the host, another project or a remote — but you won't see
                them until you read the log. The review gate and the task budget are unchanged.
              </div>
              <p className="settings-body settings-body--dim">
                Applies to tasks you start from now on. A task already running keeps the setting it began
                with, so flipping this can't loosen the gate on something already in flight.
              </p>
              {autoError && <div className="settings-error">{autoError}</div>}
              {user.auto_approve_commands ? (
                <>
                  <div className="settings-scope">
                    <strong>Covers:</strong>{" "}
                    {user.auto_approve_repos?.length
                      ? user.auto_approve_repos.join(", ")
                      : "no projects — auto mode is on but applies nowhere"}
                    <p className="settings-body settings-body--dim">
                      Every other project still asks. Tick a project to change what auto mode covers.
                    </p>
                    <div className="settings-projects">
                      {projects.map((name) => (
                        <label key={name} className="settings-project">
                          <input
                            type="checkbox"
                            checked={autoRepos.includes(name)}
                            disabled={autoSaving}
                            onChange={() => toggleRepo(name)}
                          />
                          {name}
                        </label>
                      ))}
                    </div>
                    <button
                      className="settings-btn"
                      disabled={autoSaving || sameRepos(autoRepos, user.auto_approve_repos ?? [])}
                      onClick={() => applyAuto(true, autoRepos)}
                    >
                      {autoSaving ? "Saving…" : "Save projects"}
                    </button>
                  </div>
                  <button className="settings-btn settings-btn--off" disabled={autoSaving} onClick={() => applyAuto(false)}>
                    {autoSaving ? "Saving…" : "Turn auto mode off"}
                  </button>
                </>
              ) : confirmingAuto ? (
                <div className="settings-confirm">
                  <span>Which projects may run sensitive file and shell operations without asking?</span>
                  <div className="settings-projects">
                    {projects.map((name) => (
                      <label key={name} className="settings-project">
                        <input
                          type="checkbox"
                          checked={autoRepos.includes(name)}
                          disabled={autoSaving}
                          onChange={() => toggleRepo(name)}
                        />
                        {name}
                      </label>
                    ))}
                  </div>
                  <p className="settings-body settings-body--dim">
                    Anything not ticked keeps asking. There is no "all projects" option on purpose:
                    a switch that covers everything is one nobody chose the reach of.
                  </p>
                  <button
                    className="settings-btn settings-btn--danger"
                    disabled={autoSaving || autoRepos.length === 0}
                    onClick={() => applyAuto(true, autoRepos)}
                  >
                    {autoSaving ? "Saving…" : `Turn it on for ${autoRepos.length || "no"} project${autoRepos.length === 1 ? "" : "s"}`}
                  </button>
                  <button className="settings-btn" disabled={autoSaving} onClick={() => setConfirmingAuto(false)}>
                    Cancel
                  </button>
                </div>
              ) : (
                <button className="settings-btn" onClick={() => setConfirmingAuto(true)}>
                  Turn auto mode on…
                </button>
              )}
            </section>
            <section className={`settings-card ${user.require_merge_review ? "" : "settings-card--armed"}`}>
              <h2>
                Final merge review
                <span className={`settings-pill ${user.require_merge_review ? "settings-pill--on" : ""}`}>
                  {user.require_merge_review ? "ON" : "OFF"}
                </span>
              </h2>
              <p className="settings-body">
                With this on, a task that passes the independent review service <em>parks</em> instead of
                merging: the full diff slides out from the right, you read it, and nothing ships until you
                hit <strong>Approve &amp; merge</strong> — or send it back with notes for another round.
              </p>
              <div className="settings-note settings-note--warn">
                <strong>Turning it off</strong> restores fully hands-free shipping: review-approved
                commits merge and deploy on their own. The review service still gates every merge either
                way — this toggle is only about <em>your</em> final look.
              </div>
              <p className="settings-body settings-body--dim">
                Applies to tasks you start from now on. A task already running keeps the setting it began
                with.
              </p>
              {mergeError && <div className="settings-error">{mergeError}</div>}
              {user.require_merge_review ? (
                <button className="settings-btn settings-btn--danger" disabled={mergeSaving} onClick={() => applyMergeReview(false)}>
                  {mergeSaving ? "Saving…" : "Turn final review off — merge without me"}
                </button>
              ) : (
                <button className="settings-btn" disabled={mergeSaving} onClick={() => applyMergeReview(true)}>
                  {mergeSaving ? "Saving…" : "Turn final review on"}
                </button>
              )}
            </section>
          </div>
        )}

        {active.id === "appearance" && (
          <div className="settings-stack">
            <AppearancePanel user={user} onUserChanged={onUserChanged} />
          </div>
        )}

        {active.id === "notifications" && (
          <div className="settings-stack">
            <section className="settings-card">
              <h2>Push notifications</h2>
              <p className="settings-card-sub">
                Alerts on your phone or desktop, from the installed app.
              </p>
              <PushPanel />
            </section>
            <TelegramCard />
          </div>
        )}

        {active.id === "projects" && user.role === "admin" && (
          <div className="settings-stack">
            <ProjectsPanel onChanged={onProjectsChanged} />
          </div>
        )}

        {active.id === "github" && user.role === "admin" && (
          <div className="settings-stack">
            <GitHubSettingsCard />
          </div>
        )}

        {active.id === "limits" && user.role === "admin" && (
          <div className="settings-stack">
            <RuntimeLimitsPanel />
          </div>
        )}

        {active.id === "environment" && user.role === "admin" && <ApiKeysPanel />}

        {active.id === "audit" && user.role === "admin" && (
          <div className="settings-stack">
            <AuditLogCard />
          </div>
        )}
      </div>

      {/* One save control for the whole page, pinned bottom-right. It shows
          itself only when a panel above reports a pending edit. */}
      <SettingsSaveBar />
    </div>
    </SettingsSaveProvider>
  );
}


/** Telegram alert settings — bot token + chat id, saved per user. The token
 * is write-only: the backend never returns it (masked endpoint), so an
 * already-configured card shows a placeholder and sends the __unchanged__
 * sentinel unless the operator types a new one. */
function TelegramCard() {
  const [loaded, setLoaded] = useState(false);
  const [configured, setConfigured] = useState(false);
  const [token, setToken] = useState("");
  const [chatId, setChatId] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getTelegramSettings()
      .then((s) => {
        setConfigured(s.configured);
        setChatId(s.chat_id ?? "");
        setLoaded(true);
      })
      .catch((e) => {
        setError(e instanceof Error ? e.message : "failed to load telegram settings");
        setLoaded(true);
      });
  }, []);

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setMsg(null);
    setSaving(true);
    try {
      const sendToken = token.trim() === "" && configured ? "__unchanged__" : token.trim();
      const s = await setTelegramSettings(sendToken, chatId.trim());
      setConfigured(s.configured);
      setToken("");
      setMsg(s.configured ? "Saved — use Send test to verify delivery." : "Cleared — alerts are off.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "save failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleTest() {
    setError(null);
    setMsg(null);
    setTesting(true);
    try {
      await sendTelegramTest();
      setMsg("Test message sent — check Telegram.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "test send failed");
    } finally {
      setTesting(false);
    }
  }

  return (
    <section className={`settings-card ${configured ? "settings-card--armed" : ""}`}>
      <h2>
        Telegram alerts
        <span className={`settings-pill ${configured ? "settings-pill--on" : ""}`}>
          {configured ? "ON" : "OFF"}
        </span>
      </h2>
      <p className="settings-hint">
        Pushes the moments that need you to Telegram: task escalations, approval and merge-review
        stops, completions, and failures — each with details and cost so far. Create a bot with
        @BotFather, message it once, then get your chat id from @userinfobot.
      </p>
      {loaded && (
        <form onSubmit={handleSave} className="settings-form">
          <label className="field">
            <span>Bot token</span>
            <input
              type="password"
              autoComplete="off"
              placeholder={configured ? "•••••• (saved — leave blank to keep)" : "123456:ABC-DEF…"}
              value={token}
              onChange={(e) => setToken(e.target.value)}
            />
          </label>
          <label className="field">
            <span>Chat ID</span>
            <input
              type="text"
              autoComplete="off"
              placeholder="e.g. 123456789"
              value={chatId}
              onChange={(e) => setChatId(e.target.value)}
            />
          </label>
          <div className="settings-actions">
            <button type="submit" disabled={saving}>{saving ? "Saving…" : "Save"}</button>
            <button type="button" disabled={testing || !configured} onClick={handleTest}>
              {testing ? "Sending…" : "Send test"}
            </button>
          </div>
          {msg && <p className="settings-done">{msg}</p>}
          {error && <p className="settings-error">{error}</p>}
        </form>
      )}
    </section>
  );
}
