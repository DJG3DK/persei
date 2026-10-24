import { useEffect, useMemo, useRef, useState } from "react";
import { getModelCatalog, getModelConfig, getModelEndpoints, getPrivacy, probeForcedToolCall, restartLlmRouter, savePrivacy, saveModelConfig, saveProviderPins } from "../api";
import type { PrivacyMode, ProviderEndpoint } from "../api";

const PRIVACY_LABELS: Record<PrivacyMode, string> = {
  zdr: "Zero retention: only hosts that keep nothing",
  deny: "No training: only hosts that neither store nor train on it",
  off: "Off: whatever your OpenRouter account allows",
};

/** The router-wide rule on which hosts may see a call. Saved on change: the
 *  router applies it on its next call, no restart, so it does not wait for
 *  the pins' Save bar. */
function PrivacyControl() {
  const [mode, setMode] = useState<PrivacyMode | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  useEffect(() => { getPrivacy().then(setMode).catch((e: Error) => setLoadError(e.message)); }, []);
  async function change(next: PrivacyMode) {
    setMsg(null);
    try {
      setMode(await savePrivacy(next));
      setMsg("Saved. The router applies it on its next call.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "saving failed");
    }
  }
  return (
    <div className="model-config-privacy">
      <label htmlFor="router-privacy" className="model-config-privacy-label">Provider privacy</label>
      <select id="router-privacy" value={mode ?? "zdr"} disabled={mode === null}
              onChange={(e) => void change(e.target.value as PrivacyMode)}>
        {(Object.keys(PRIVACY_LABELS) as PrivacyMode[]).map((m) => (
          <option key={m} value={m}>{PRIVACY_LABELS[m]}</option>
        ))}
      </select>
      <span className="model-config-privacy-note">
        Applies to every call through the router, the chat app and the reviewers included. A provider pin below
        still has to pass it.
      </span>
      {loadError && <span className="model-config-privacy-msg">{loadError}</span>}
      {msg && <span className="model-config-privacy-msg" role="status">{msg}</span>}
    </div>
  );
}

function providerRecord(e: ProviderEndpoint): string {
  const r = e.record;
  if (!r || r.calls === 0) return "not used by this router this week";
  const parts = [`${r.calls.toLocaleString()} calls this week`];
  if (r.errors) parts.push(`${r.errors} errors`);
  if (r.runaways) parts.push(`${r.runaways} runaways`);
  return parts.join(", ");
}
import type { ModelCatalogEntry, ModelPin } from "../types";
import { SettingsSaveBar, SettingsSaveProvider, useSettingsSave } from "./SettingsSaveBar";
import "./ModelConfigPanel.css";

// Display ORDER for this agent's pinned roles (agent/model_config.py's
// MANAGED_ROLES). Deliberately not a filter: any role the backend returns
// that isn't listed here still renders, appended after these. It used to be
// both, so adding agent-demo-chat to MANAGED_ROLES made it appear in the API
// response and stay invisible in the UI -- a new role silently missing from
// the page is a worse failure than one in an unexpected position.
const ROLE_ORDER = [
  "agent-planner",
  "agent-coder",
  "agent-coder-frontend",
  "agent-coder-fallback",
  "agent-investigator",
  "agent-test-writer",
  "agent-verifier",
  "agent-summarizer",
  "agent-vision",
  "agent-consolidator",
  "agent-cartographer",
  "agent-classifier",
  "agent-planning-chat",
  "agent-planning-chat-hard",
  "agent-planning-chat-frontend",
  "agent-demo-chat",
  "agent-reviewer",
];

// Themed sections (2026-08-28 reorganization): the flat list stopped scaling
// once provider pinning doubled each row's controls. Any role missing from
// every group still renders, in a trailing "Other" group -- same
// never-hide-a-role rule as ROLE_ORDER.
const ROLE_GROUPS: [string, string[]][] = [
  ["Build pipeline", ["agent-planner", "agent-coder", "agent-coder-frontend", "agent-investigator", "agent-test-writer", "agent-verifier", "agent-reviewer"]],
  ["Planning chat", ["agent-planning-chat", "agent-planning-chat-hard", "agent-planning-chat-frontend", "agent-classifier"]],
  ["Support", ["agent-summarizer", "agent-vision", "agent-cartographer", "agent-consolidator", "agent-demo-chat"]],
];

/** Provider pin dropdown for one role. Lazy: the provider list is fetched
 * from OpenRouter's endpoints API the first time the picker is opened for
 * the role's (possibly pending) model. "Auto" = OpenRouter's own routing. */
function ProviderPicker({
  modelId,
  value,
  onChange,
}: {
  modelId: string;
  value: string | null;
  onChange: (provider: string | null) => void;
}) {
  const [endpoints, setEndpoints] = useState<ProviderEndpoint[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // model changed underneath us -> stale list. Adjusted during render so
  // the old model's endpoints never get a frame under the new one.
  const [listedFor, setListedFor] = useState(modelId);
  if (listedFor !== modelId) {
    setListedFor(modelId);
    setEndpoints(null);
    setLoadError(null);
  }

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  async function ensureLoaded() {
    if (endpoints || loadError) return;
    try {
      setEndpoints(await getModelEndpoints(modelId));
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "failed to load providers");
    }
  }

  return (
    <div className="provider-picker" ref={rootRef}>
      <button
        type="button"
        className={`provider-picker-trigger ${value ? "provider-picker-trigger--pinned" : ""}`}
        title={value ? `Served only by ${value} (no pool fallback)` : "OpenRouter picks the provider per call"}
        onClick={() => {
          setOpen((o) => !o);
          void ensureLoaded();
        }}
      >
        {value ? `📌 ${value}` : "provider: auto"}
        <span className="model-picker-caret">&#9662;</span>
      </button>
      {open && (
        <div className="provider-picker-dropdown">
          <button
            type="button"
            className={`provider-picker-option ${value === null ? "active" : ""}`}
            onClick={() => { onChange(null); setOpen(false); }}
          >
            <span>Auto — OpenRouter routes per call</span>
          </button>
          <div className="provider-picker-note">
            Hosts outside the router's privacy rule (top of this page) are skipped on each call, even when pinned.
          </div>
          {loadError && <div className="provider-picker-error">{loadError}</div>}
          {!endpoints && !loadError && <div className="provider-picker-loading">Loading providers…</div>}
          {endpoints?.map((e) => (
            <button
              key={e.provider}
              type="button"
              className={`provider-picker-option ${value === e.provider ? "active" : ""}`}
              onClick={() => { onChange(e.provider); setOpen(false); }}
            >
              <span>{e.provider}</span>
              <span className="provider-picker-price">
                {formatPrice(e.input_cost_per_token)} in · {formatPrice(e.output_cost_per_token)} out
                {/* quantization matters for open-weight hosts (fp8 vs fp4 is
                    a real quality difference); closed first-party APIs report
                    the literal string "unknown" -- noise, not information. */}
                {e.quantization && e.quantization !== "unknown" ? ` · ${e.quantization}` : ""}
                <span className="provider-picker-metrics">
                  {e.latency_s != null ? `${e.latency_s.toFixed(1)}s latency` : ""}
                  {e.throughput_tps != null ? ` · ${Math.round(e.throughput_tps)} tok/s` : ""}
                  {e.uptime != null ? ` · ${e.uptime.toFixed(1)}% up` : ""}
                  {e.implicit_caching ? " · cache ✓" : ""}
                </span>
                <span className="provider-picker-facts">
                  {e.headquarters ? `Based in ${e.headquarters}` : ""}
                  {e.datacenters && e.datacenters.length ? ` · servers ${e.datacenters.join(", ")}` : ""}
                  {e.headquarters || (e.datacenters && e.datacenters.length) ? " · " : ""}
                  {providerRecord(e)}
                  {e.privacy_policy_url && (
                    <>
                      {" · "}
                      <span
                        role="link"
                        tabIndex={0}
                        className="provider-picker-policy"
                        onClick={(ev) => { ev.stopPropagation(); window.open(e.privacy_policy_url!, "_blank", "noopener"); }}
                        onKeyDown={(ev) => {
                          if (ev.key === "Enter") { ev.stopPropagation(); window.open(e.privacy_policy_url!, "_blank", "noopener"); }
                        }}
                      >
                        privacy policy ↗
                      </span>
                    </>
                  )}
                </span>
              </span>
            </button>
          ))}
          {endpoints?.length === 0 && <div className="provider-picker-error">no providers listed for this model</div>}
        </div>
      )}
    </div>
  );
}

function formatPrice(perToken: number | null | undefined): string {
  if (perToken == null) return "—";
  const per1M = perToken * 1_000_000;
  return `$${per1M < 1 ? per1M.toFixed(3) : per1M.toFixed(2)}/1M`;
}

function ModelPicker({
  value,
  catalog,
  onChange,
  allowedIds,
  restrictionLabel,
}: {
  value: string;
  catalog: ModelCatalogEntry[];
  onChange: (id: string) => void;
  /** When set, the list is restricted to these ids — the models a real probe
   *  proved can do what this role needs. Undefined = no restriction. */
  allowedIds?: Set<string>;
  restrictionLabel?: string;
}) {
  const [query, setQuery] = useState("");
  const [open, setOpen] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    return () => document.removeEventListener("mousedown", onDocClick);
  }, [open]);

  const filtered = useMemo(() => {
    // Restrict to what the role can actually use, unless the operator has
    // explicitly asked to see everything. The currently-pinned model is always
    // kept, even if it fails the restriction — hiding the current value makes
    // the picker look broken and loses the chance to see WHY it is flagged.
    const restricted = allowedIds && !showAll
      ? catalog.filter((m) => allowedIds.has(m.id) || m.id === value)
      : catalog;
    const q = query.trim().toLowerCase();
    // No slice. The list was capped at 50, so with 417 models in the catalog
    // most of it was simply unreachable unless you happened to type the right
    // substring — the list is scrollable, so render what matches.
    return q
      ? restricted.filter((m) => m.id.toLowerCase().includes(q) || m.name.toLowerCase().includes(q))
      : restricted;
  }, [query, catalog, allowedIds, showAll, value]);

  const current = catalog.find((m) => m.id === value);

  return (
    <div className="model-picker" ref={rootRef}>
      <button
        type="button"
        className="model-picker-trigger"
        onClick={() => {
          setOpen((o) => !o);
          setQuery("");
        }}
      >
        <span className="model-picker-current">{current?.name ?? value}</span>
        <span className="model-picker-caret">&#9662;</span>
      </button>
      {open && (
        <div className="model-picker-dropdown">
          <input
            autoFocus
            className="model-picker-search"
            placeholder="Search OpenRouter models…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <div className="model-picker-meta">
            <span>
              {filtered.length} model{filtered.length === 1 ? "" : "s"}
              {allowedIds && !showAll && restrictionLabel ? ` · ${restrictionLabel}` : ""}
            </span>
            {allowedIds && (
              // An escape hatch, not a wall. The probe is evidence, and evidence
              // goes stale: a model that failed last month may pass today. The
              // operator can always look at the rest — they just have to ask.
              <button type="button" className="model-picker-showall" onClick={() => setShowAll((v) => !v)}>
                {showAll ? "show only verified" : `show all ${catalog.length}`}
              </button>
            )}
          </div>
          <div className="model-picker-list">
            {filtered.map((m) => (
              <button
                type="button"
                key={m.id}
                className={`model-picker-option ${m.id === value ? "selected" : ""}`}
                onClick={() => {
                  onChange(m.id);
                  setOpen(false);
                }}
              >
                <span className="model-picker-option-name">
                  {m.name}
                  {/* The three numbers this stack picks models by: agentic
                      arena standing (closest public proxy for tool-calling
                      build work -- a compass, not a code-correctness
                      verdict), price, and knowledge cutoff. */}
                  <span className="model-picker-option-metrics">
                    {m.arena ? `#${m.arena.rank} agents (${m.arena.category}) · ELO ${m.arena.elo}` : "no arena data"}
                    {m.knowledge_cutoff ? ` · cutoff ${String(m.knowledge_cutoff).slice(0, 7)}` : ""}
                  </span>
                </span>
                <span className="model-picker-option-price">
                  {formatPrice(m.input_cost_per_token)} in / {formatPrice(m.output_cost_per_token)} out
                </span>
              </button>
            ))}
            {filtered.length === 0 && <div className="model-picker-empty">No matches</div>}
          </div>
        </div>
      )}
    </div>
  );
}

/* The same save flow as the settings page: one sticky bar, pinned
 * bottom-right, that appears only when a pin is genuinely dirty. The static
 * "Save N changes" button lived at the very bottom of a page that is three
 * groups of rows long, so a model changed in the top group was easy to
 * scroll away from and abandon. The provider carries the registry the bar
 * reads; the panel below registers its pending count into it. */
export function ModelConfigPanel() {
  return (
    <SettingsSaveProvider>
      <ModelConfigPins />
      <SettingsSaveBar />
    </SettingsSaveProvider>
  );
}

function ModelConfigPins() {
  const [pins, setPins] = useState<Record<string, ModelPin> | null>(null);
  const [catalog, setCatalog] = useState<ModelCatalogEntry[]>([]);
  // Which models actually survive a forced tool call, from real probes rather
  // than OpenRouter's catalog (which cannot express the constraint).
  // Probe results, refreshed by the button below. Kept as state (rather than
  // read only from the pin) so a re-probe updates the panel without a reload.
  const [probeOk, setProbeOk] = useState<string[]>([]);
  const probeOkSet = useMemo(() => new Set(probeOk), [probeOk]);
  const [probeAttempted, setProbeAttempted] = useState<number>(0);
  const [probedAt, setProbedAt] = useState<string | null>(null);
  const [probing, setProbing] = useState(false);
  const [probeMsg, setProbeMsg] = useState<string | null>(null);

  // Re-run the real probe. Worth a button rather than a static list: OpenRouter's
  // roster changes constantly, and compliance is per-provider, so a cached
  // verdict decays. Reads the catalog cannot answer this — only a real request can.
  async function handleProbe() {
    setProbing(true);
    setProbeMsg(null);
    try {
      const r = await probeForcedToolCall();
      setProbeOk(r.compliant);
      setProbedAt(r.probed_at);
      setProbeAttempted(r.attempted ?? 0);
      // Report every verdict. "122 passed · 34 failed" hid 60 unavailable and
      // 2 rate-limited models, so the probe looked like it had only covered 156
      // of the catalogue and 122 read as the catalogue's total size.
      const parts = [`${r.compliant.length} passed`, `${r.non_compliant.length} failed`];
      if (r.unavailable?.length) parts.push(`${r.unavailable.length} unavailable`);
      if (r.transient?.length) parts.push(`${r.transient.length} rate-limited (re-probe)`);
      const scope = r.attempted && r.catalog_size
        ? ` — ${r.attempted} eligible of ${r.catalog_size} catalogue models`
        : "";
      setProbeMsg(parts.join(" · ") + scope);
    } catch (err) {
      setProbeMsg(err instanceof Error ? err.message : "probe failed");
    } finally {
      setProbing(false);
    }
  }
  const [pending, setPending] = useState<Record<string, string>>({});
  // Provider pins pending save. undefined = untouched; null = clear to auto.
  const [pendingProviders, setPendingProviders] = useState<Record<string, string | null>>({});
  const [restarting, setRestarting] = useState(false);
  // The restart's own error, kept separate from the panel's: the panel renders
  // its error BEHIND the modal backdrop, so a failed restart showed a dialog
  // with nothing in it but Cancel -- indistinguishable from a dead button.
  const [restartError, setRestartError] = useState<string | null>(null);
  const [restartDone, setRestartDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [justSaved, setJustSaved] = useState(false);
  const [confirmingRestart, setConfirmingRestart] = useState(false);
  // In the compose bundle the router re-reads its config on the next call and
  // there is no pm2 to restart; the page gets a note instead of a button.
  const [restartNote, setRestartNote] = useState<string | null>(null);
  // audit M-24: focus management for the restart confirmation dialog -- it
  // restarts a shared service that interrupts in-flight calls across two apps,
  // so a keyboard user must be able to reach Cancel/Restart, dismiss with
  // Escape, and land back where they were.
  const cancelBtnRef = useRef<HTMLButtonElement>(null);
  const restartTriggerRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!confirmingRestart) return;
    restartTriggerRef.current = document.activeElement as HTMLElement | null;
    cancelBtnRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setConfirmingRestart(false);
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      restartTriggerRef.current?.focus?.();
    };
  }, [confirmingRestart]);

  async function load() {
    setError(null);
    try {
      const [cfg, cat] = await Promise.all([getModelConfig(), getModelCatalog()]);
      setPins(cfg.roles);
      setRestartNote(cfg.router_restart && !cfg.router_restart.available ? (cfg.router_restart.note ?? "") : null);
      setCatalog(cat.models);
      setProbeOk(cat.forced_tool_call?.compliant ?? []);
      setProbedAt(cat.forced_tool_call?.probed_at ?? null);
      setProbeAttempted(cat.forced_tool_call?.attempted ?? 0);
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to load model config");
    }
  }

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- the load it starts is async; state lands after the await, not in the effect
    load();
  }, []);

  function currentModelFor(role: string): string {
    return pending[role] ?? pins?.[role]?.model ?? "";
  }

  function handleChange(role: string, modelId: string) {
    setJustSaved(false);
    setPending((p) => {
      const next = { ...p };
      if (pins?.[role]?.model === modelId) delete next[role];
      else next[role] = modelId;
      return next;
    });
  }

  function currentProviderFor(role: string): string | null {
    return role in pendingProviders ? pendingProviders[role] : (pins?.[role]?.provider ?? null);
  }

  function handleProviderChange(role: string, provider: string | null) {
    setJustSaved(false);
    setPendingProviders((p) => {
      const next = { ...p };
      if ((pins?.[role]?.provider ?? null) === provider) delete next[role];
      else next[role] = provider;
      return next;
    });
  }

  const changedCount = Object.keys(pending).length + Object.keys(pendingProviders).length;

  // Committed by the page's save bar, which owns the saving state and shows
  // any error next to its own button -- so this throws rather than catching.
  async function commitPins() {
    // Models first: a model repin RESETS that role's provider pin on the
    // backend (a provider chosen for the old model is meaningless), so the
    // provider save must land second to apply against the new model.
    let roles = pins!;
    if (Object.keys(pending).length) {
      roles = (await saveModelConfig(pending)).roles;
    }
    if (Object.keys(pendingProviders).length) {
      roles = (await saveProviderPins(pendingProviders)).roles;
    }
    setPins(roles);
    setPending({});
    setPendingProviders({});
    setJustSaved(true);
  }

  function discardPins() {
    setPending({});
    setPendingProviders({});
    setJustSaved(false);
  }

  useSettingsSave("model-pins", changedCount, commitPins, discardPins);

  async function handleRestart() {
    setRestarting(true);
    setRestartError(null);
    setRestartDone(null);
    try {
      const res = await restartLlmRouter();
      setConfirmingRestart(false);
      setJustSaved(false);
      // Say it plainly. The only confirmation before this was a Telegram
      // alert, which is not the same thing as the page telling you.
      setRestartDone(
        res.healthy === false
          ? "Restart command accepted, but the router is not answering yet — check pm2."
          : `Router restarted and answering${res.waited_s ? ` after ${res.waited_s}s` : ""}. The new pins are live.`,
      );
    } catch (err) {
      setRestartError(err instanceof Error ? err.message : "restart failed");
    } finally {
      setRestarting(false);
    }
  }

  if (!pins) {
    return (
      <div className="model-config-view">
        <h1 className="model-config-title">Model Configuration</h1>
        {error ? <div className="model-config-error">{error}</div> : <div className="model-config-loading">Loading…</div>}
      </div>
    );
  }

  return (
    <div className="model-config-view">
      <h1 className="model-config-title">Model Configuration</h1>
      <p className="model-config-sub">
        Every role this agent pins, including <code>agent-reviewer</code> — the independent
        review service resolves its model through this page like everything else. Aliases
        that do not begin <code>agent-</code> are left alone: they are not this agent&apos;s
        to set.
      </p>

      <PrivacyControl />

      {error && <div className="model-config-error">{error}</div>}

      {(() => {
        const grouped = new Set(ROLE_GROUPS.flatMap(([, rs]) => rs));
        const leftovers = [
          ...ROLE_ORDER.filter((r) => r in pins && !grouped.has(r)),
          ...Object.keys(pins).filter((r) => !ROLE_ORDER.includes(r) && !grouped.has(r)),
        ];
        const sections: [string, string[]][] = [
          ...ROLE_GROUPS.map(([t, rs]) => [t, rs.filter((r) => r in pins)] as [string, string[]]),
          ...(leftovers.length ? ([["Other", leftovers]] as [string, string[]][]) : []),
        ];
        return sections.map(([title, roles]) => (
          <div key={title} className="model-config-group">
            <h2 className="model-config-group-title">{title}</h2>
            <div className="model-config-list">
        {roles.map((role) => {
          const pin = pins[role];
          if (!pin) return null;
          const isChanged = role in pending;
          return (
            <div key={role} className={`model-config-row ${isChanged ? "changed" : ""}`}>
              <div className="model-config-role">
                <span className="model-config-role-label">{pin.label}</span>
                <span className="model-config-role-alias">{role}</span>
                {/* What the role needs from a model. A role that forces a tool
                    call cannot use every model — some providers reject it in
                    thinking mode — so surface that before a model is picked,
                    not after the nightly job quietly stops working. */}
                <span className="model-config-role-caps">
                  {/* `strict` first and amber: it is the only axis that actually
                      constrains which models you can pick. */}
                  {pin.strict && (
                    <span className="model-cap model-cap--strict" title="Tools AND structured output in one request. Most models fail this — check the probe before changing the pin.">
                      strict
                    </span>
                  )}
                  {pin.tools && (
                    <span className="model-cap" title="Hands the model callable tools.">tools</span>
                  )}
                  {pin.structured && (
                    <span className="model-cap" title="Constrains the shape of the output.">structured</span>
                  )}
                  {!pin.tools && !pin.structured && (
                    <span className="model-cap model-cap--none" title="Plain completion — no tools, no output constraint.">
                      plain
                    </span>
                  )}
                </span>
                {pin.note && <span className="model-config-role-note">{pin.note}</span>}
                {/* Only the strict role gets a hard warning — everywhere else a
                    probe failure is irrelevant, so saying nothing is correct. */}
                {pin.strict && pin.strict_bad?.includes(pin.model) && (
                  <span className="model-config-role-warn">
                    ⚠ {pin.model} is verified FAILING for this role — it does not reliably
                    call tools while returning structured output.
                  </span>
                )}
                {pin.strict && pin.strict_ok?.length ? (
                  <span className="model-config-role-note model-config-role-note--dim">
                    verified working: {pin.strict_ok.join(", ")}
                    {probedAt ? ` · last probe ${probedAt.slice(0, 10)}` : ""}
                    {probeOk.length ? ` · ${probeOk.length} of ${probeAttempted || probeOk.length} probed models pass` : ""}
                  </span>
                ) : null}
              </div>
              {/* Tailored per role. A role that sends tools AND structured
                  output in the same request cannot use every model, and the
                  catalog cannot tell you which — supported_parameters lists
                  tool_choice and reasoning separately while some providers
                  refuse the COMBINATION. Only a real request answers that,
                  which is what scripts/probe_forced_tool_call.py makes. So a
                  strict role is restricted to models the probe actually
                  PASSED; every other role gets the full catalog, because
                  nothing narrows it usefully. The restriction is a default,
                  not a lock — see the show-all toggle in the dropdown. */}
              <div className="model-config-pickers">
                <ModelPicker
                  value={currentModelFor(role)}
                  catalog={catalog}
                  onChange={(id) => handleChange(role, id)}
                  allowedIds={pin.strict ? probeOkSet : undefined}
                  restrictionLabel={pin.strict ? "passed the tool probe" : undefined}
                />
                <ProviderPicker
                  modelId={currentModelFor(role)}
                  value={role in pending ? (pendingProviders[role] ?? null) : currentProviderFor(role)}
                  onChange={(prov) => handleProviderChange(role, prov)}
                />
              </div>
              <div className="model-config-price">
                {formatPrice(pin.input_cost_per_token)} in · {formatPrice(pin.output_cost_per_token)} out
              </div>
              {(isChanged || role in pendingProviders) && <span className="model-config-pending-badge">pending</span>}
            </div>
          );
        })}
            </div>
          </div>
        ));
      })()}

      {/* Saving lives in the sticky bar (see ModelConfigPanel above); this
          row holds the two actions that are not edits. */}
      <div className="model-config-actions">
        {restartNote === null ? (
          <button className="model-config-restart-btn" disabled={restarting} onClick={() => setConfirmingRestart(true)}>
            {restarting ? "Restarting…" : "Restart Router"}
          </button>
        ) : (
          <p className="settings-hint model-config-restart-note" data-testid="router-restart-note">{restartNote}</p>
        )}
        {/* Re-runs the real forced-tool-call probe across the eligible catalog.
            Takes a few minutes — it makes one live request per model, which is
            the only thing that actually answers the question. */}
        <button
          className="model-config-probe-btn"
          disabled={probing}
          onClick={handleProbe}
          title="Re-test every eligible model with a real forced tool call and refresh the allow-list"
        >
          {probing ? "Probing…" : "Refresh model list"}
        </button>
        {probeMsg && <span className="model-config-probe-note">{probeMsg}</span>}
        {justSaved && (
          <span className="model-config-saved-note">
            Saved to config.yaml. The router reads it on its next call; no restart needed.
          </span>
        )}
        {restartDone && (
          <span className="model-config-saved-hint" role="status">{restartDone}</span>
        )}
      </div>

      {confirmingRestart && (
        <div className="model-config-modal-backdrop" onClick={() => setConfirmingRestart(false)}>
          <div className="model-config-modal" role="dialog" aria-modal="true"
               aria-labelledby="restart-modal-title" onClick={(e) => e.stopPropagation()}>
            <h2 id="restart-modal-title">Restart the model router?</h2>
            <p>
              This restarts the shared model router. It will briefly interrupt any in-flight request from
              every service that depends on it — the review service and this agent — not just
              the role you changed.
            </p>
            {restartError && (
              <div className="model-config-error model-config-modal-error" role="alert">
                {restartError}
              </div>
            )}
            <div className="model-config-modal-actions">
              <button ref={cancelBtnRef} className="model-config-modal-cancel-btn"
                      onClick={() => { setConfirmingRestart(false); setRestartError(null); }}>
                {restartError ? "Close" : "Cancel"}
              </button>
              <button className="model-config-restart-confirm-btn" onClick={handleRestart} disabled={restarting}>
                {restarting ? "Restarting…" : restartError ? "Try again" : "Restart now"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
