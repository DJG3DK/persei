/* The control panel. Plain script on purpose: no bundler, no framework.
   Everything that does work lives in Rust (src-tauri/src); this page asks
   for it and shows what comes back. `window.__TAURI__` is on because
   tauri.conf.json sets withGlobalTauri. */
const { invoke } = window.__TAURI__.core;
const { listen } = window.__TAURI__.event;
const dialog = window.__TAURI__.dialog;
const opener = window.__TAURI__.opener;

const $ = (id) => document.getElementById(id);
const show = (id) => { for (const p of document.querySelectorAll(".page")) p.classList.add("hidden"); $(id).classList.remove("hidden"); };
const log = $("log");
let following = false;
let pendingStackUpdate = null;
let appUpdate = null;

function append(stream, line) {
  const span = document.createElement("span");
  span.className = stream;
  span.textContent = line + "\n";
  log.appendChild(span);
  if (log.childNodes.length > 2000) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}
listen("stack-log", (e) => append(e.payload.stream, e.payload.line));

function say(line) { append("app", line); }
function fail(e) { append("err", String(e)); }

for (const a of document.querySelectorAll("a[data-open]")) {
  a.addEventListener("click", (ev) => { ev.preventDefault(); opener.openUrl(a.dataset.open); });
}

// ── Docker ───────────────────────────────────────────────────────────────────
// The Docker screen in each platform's words. Linux (2026-10-04): Docker
// Engine, installed and started through the system's own password prompt.
const LINUX = /Linux/.test(navigator.platform || navigator.userAgent) && !/Android/.test(navigator.userAgent);
const DOCKER_TEXT = {
  windows: {
    missing: { title: "Docker Desktop is not installed", button: "install", label: "Install Docker Desktop",
      body: "Persei runs in containers. This installs WSL 2 if it is missing and then Docker Desktop, from Docker's own site. Windows will ask for permission, and a restart may be needed." },
    stopped: { title: "Docker Desktop is not running", button: "start", label: "Start Docker Desktop",
      body: "Start it and it comes up in a minute. If Docker Desktop itself says \"Virtualization support not detected\", the CPU's virtualization is switched off in the firmware (BIOS/UEFI: Intel VT-x or AMD SVM); no installer can turn that on." },
    denied: { title: "Docker refused this account", body: "Open Docker Desktop once, then check again." },
    "no-compose": { title: "This Docker has no compose command", body: "Update Docker Desktop and check again." },
  },
  linux: {
    missing: { title: "Docker is not installed", button: "install", label: "Install Docker",
      body: "Persei runs in containers. This installs Docker Engine (with Docker's own install script, or from your distribution's packages on Arch-based systems like CachyOS), starts its service, and lets your account use it. Your system asks for your password once, and Persei restarts itself when it is done." },
    stopped: { title: "Docker is not running", button: "start", label: "Start Docker",
      body: "The Docker service is installed but stopped. Starting it asks for your password (or starts Docker Desktop, if that is what you use)." },
    denied: { title: "Your account cannot use Docker yet", button: "install", label: "Give my account access",
      body: "Docker is running, but only members of the docker group may use it. This adds you (your system asks for your password once), and Persei restarts itself with access." },
    "no-compose": { title: "This Docker has no compose command", body: "Install the compose plugin (the docker-compose-plugin package) and check again." },
  },
};

async function checkDocker() {
  show("page-docker");
  $("docker-install").classList.add("hidden");
  $("docker-start").classList.add("hidden");
  $("docker-title").textContent = "Checking Docker…";
  $("docker-text").textContent = "";
  const state = await invoke("docker_state");
  if (state === "ready") return afterDocker();
  // Linux: already in the docker group, from before this login. The app
  // restarts itself into the group; there is nothing to ask (2026-10-06).
  if (state === "denied" && LINUX && await invoke("docker_can_restart")) {
    $("docker-title").textContent = "Getting Docker access…";
    $("docker-text").textContent = "Your account is in the docker group already. Persei restarts itself with it in a moment.";
    try { say(await invoke("docker_install")); } catch (e) { fail(e); }
    return;
  }
  const text = DOCKER_TEXT[LINUX ? "linux" : "windows"][state] || DOCKER_TEXT.windows["no-compose"];
  $("docker-title").textContent = text.title;
  $("docker-text").textContent = text.body;
  if (text.button) {
    const b = $(text.button === "start" ? "docker-start" : "docker-install");
    b.textContent = text.label;
    b.classList.remove("hidden");
  }
}
$("docker-recheck").onclick = checkDocker;
if (LINUX) $("setup-dir").placeholder = "/home/you/code";
$("docker-start").onclick = async () => {
  $("docker-start").disabled = true;
  try { await invoke("docker_start"); await afterDocker(); } catch (e) { fail(e); } finally { $("docker-start").disabled = false; }
};
$("docker-install").onclick = async () => {
  $("docker-install").disabled = true;
  try {
    const next = await invoke("docker_install");
    // Done: the heading said "not installed" over a message saying it was
    // (2026-10-04). What is left is the person's step, so no button.
    if (!/^Nothing to install/.test(next)) {
      $("docker-title").textContent = /Restarting/.test(next) ? "Docker is ready" : "Docker is set up";
      $("docker-install").classList.add("hidden");
    }
    $("docker-text").textContent = next;
    say(next);
  } catch (e) { fail(e); } finally { $("docker-install").disabled = false; }
};

// ── Setup ────────────────────────────────────────────────────────────────────
let settings = null;

// The agent's password rules (agent/auth.py validate_password_strength), in
// its words. The Rust side checks them again before the one-time password
// is spent; this is so the form says so before the stack starts.
function passwordProblem(pw) {
  if (pw.length < 12) return "password must be at least 12 characters";
  if (!/\p{Ll}/u.test(pw)) return "password must include a lowercase letter";
  if (!/\p{Lu}/u.test(pw)) return "password must include an uppercase letter";
  if (!/[0-9]/.test(pw)) return "password must include a digit";
  return null;
}
async function afterDocker() {
  settings = await invoke("settings_get");
  $("stack-version").textContent = "";
  const installed = await invoke("installed_version");
  if (installed) $("stack-version").textContent = "stack " + installed;
  if (!settings.openrouter_api_key_set || !settings.projects_dir) return showSetup(false);
  await showStack();
}

async function showSetup(cancellable) {
  show("page-setup");
  $("setup-key").value = "";
  $("setup-key-hint").textContent = settings && settings.openrouter_api_key_set ? `Set (${settings.openrouter_key_hint}). Leave blank to keep it.` : "";
  $("setup-dir").value = settings ? settings.projects_dir : "";
  $("setup-email").value = settings ? settings.admin_email : "";
  let gitName = settings ? settings.git_name : "";
  let gitEmail = settings ? settings.git_email : "";
  if (!gitName && !gitEmail) {
    try { [gitName, gitEmail] = await invoke("machine_git_identity"); } catch (e) { /* no git here; fine */ }
  }
  $("setup-git-name").value = gitName || "";
  $("setup-git-email").value = gitEmail || "";
  try {
    const prefs = await invoke("prefs_get");
    $("setup-auto-update").checked = !!prefs.auto_update;
    $("setup-prereleases").checked = !!prefs.include_prereleases;
    $("setup-prereleases").disabled = !!prefs.prereleases_forced;
    $("setup-prereleases-note").textContent = prefs.prereleases_forced ? FORCED_NOTE : "";
  } catch (e) { /* defaults stay */ }
  $("setup-cancel").classList.toggle("hidden", !cancellable);
  $("setup-password-block").classList.toggle("hidden", cancellable);   // only the first setup sets it
  $("setup-error").classList.add("hidden");
}
$("setup-pick").onclick = async () => {
  const dir = await dialog.open({ directory: true, multiple: false, title: "Projects folder" });
  if (dir) $("setup-dir").value = dir;
};
$("setup-cancel").onclick = () => showStack();
$("setup-form").onsubmit = async (ev) => {
  ev.preventDefault();
  $("setup-save").disabled = true;
  $("setup-error").classList.add("hidden");
  try {
    const pw = $("setup-password").value;
    if (pw && pw !== $("setup-password-2").value) throw new Error("the two passwords differ");
    const problem = pw && passwordProblem(pw);
    if (problem) throw new Error(problem);
    const key = $("setup-key").value.trim();
    settings = await invoke("settings_save", {
      key: key || null, projectsDir: $("setup-dir").value, adminEmail: $("setup-email").value,
      gitName: $("setup-git-name").value, gitEmail: $("setup-git-email").value,
    });
    await invoke("prefs_set", { autoUpdate: $("setup-auto-update").checked, includePrereleases: $("setup-prereleases").checked });
    void showAutoStatus();
    if (lastRunning) say("Saved. The stack is running on its old settings; they apply when it restarts (Stop, then Start).");
    await showStack();
    if (!(await invoke("installed_version"))) {
      const set = await runStack("stack_install", { password: pw || null });
      if (set) say("Password set. Open the console and sign in with it.");
      else if (pw && set === false) say("The account already has a password (an earlier install's data is still here), so the one typed here was not used. Sign in with the earlier one, or Show first password if it was never shown.");
    }
    $("setup-password").value = ""; $("setup-password-2").value = "";
  } catch (e) {
    $("setup-error").textContent = String(e);
    $("setup-error").classList.remove("hidden");
  } finally {
    $("setup-save").disabled = false;
  }
};

// ── Stack ────────────────────────────────────────────────────────────────────
let busy = false;
let lastRunning = false;   // what the last status poll saw; Settings says so when a restart is needed
const FORCED_NOTE = "This app is a release candidate, so pre-releases are always included.";
async function showStack() {
  show("page-stack");
  await refreshStatus();
  void showAutoStatus();
}

function setState(text, tone, lead) {
  const pill = $("stack-state");
  pill.textContent = text;
  pill.className = "pill " + tone;
  $("stack-text").textContent = lead || "";
}

async function refreshStatus() {
  let rows = [];
  try { rows = await invoke("stack_status"); } catch (e) { /* not up yet */ }
  const tbody = $("containers").querySelector("tbody");
  tbody.textContent = "";
  for (const r of rows) {
    const tr = document.createElement("tr");
    const service = document.createElement("td");
    service.textContent = r.service;
    const state = document.createElement("td");
    state.className = `state-${r.state}`;
    state.textContent = r.health ? `${r.state} (${r.health})` : r.state;
    tr.append(service, state);
    tbody.appendChild(tr);
  }
  const agent = rows.find((r) => r.service === "agent");
  const running = agent && agent.state === "running";
  lastRunning = !!running;
  if (busy) return;
  if (running) setState("running", agent.health === "healthy" || !agent.health ? "good" : "warn", "Open console shows it in this window; the tray icon brings this panel back.");
  else if (rows.length) setState("stopped", "warn", "The stack is installed and stopped.");
  else setState("not started", "warn", "Nothing is running yet.");
  $("btn-open").disabled = !running;
  $("btn-password").disabled = !running;
  $("btn-stop").disabled = !running;
  $("btn-start").disabled = !!running;
}
setInterval(() => { if (!$("page-stack").classList.contains("hidden")) refreshStatus(); }, 10000);

async function runStack(command, args) {
  busy = true;
  setState("working", "warn", "Docker's own progress is in the log below. The first start pulls about 3 GB.");
  for (const id of ["btn-start", "btn-stop", "btn-update-stack"]) $(id).disabled = true;
  let result;
  try {
    result = await invoke(command, args || {});
    say("Done.");
  } catch (e) {
    fail(e);
    setState("error", "bad", String(e));
  } finally {
    busy = false;
    await refreshStatus();
    // The first install changes the stack's version line ("stack not
    // installed" stayed up after it, 2026-10-04).
    void showAutoStatus();
  }
  return result;
}
$("btn-start").onclick = () => runStack("stack_up");
$("btn-stop").onclick = () => runStack("stack_down");
// Quit stops the stack (and Docker Desktop, when this app started it) and
// closes the app; the app side asks first when a task is running.
$("btn-quit").onclick = () => invoke("quit_app").catch(fail);
$("close-action").onchange = async () => {
  try { await invoke("prefs_set_close", { action: $("close-action").value }); } catch (e) { fail(e); }
};
$("btn-open").onclick = () => invoke("open_console").catch(fail);
$("btn-settings").onclick = () => { void showSetup(true); };
$("btn-folder").onclick = () => invoke("open_projects_dir").catch(fail);
$("btn-password").onclick = async () => {
  try {
    const pw = await invoke("stack_password");
    $("password-value").textContent = pw;
    $("password-box").classList.remove("hidden");
  } catch (e) {
    fail("No one-time password to show: it was shown already, or the agent has not started yet. " + e);
  }
};

// ── Updates ──────────────────────────────────────────────────────────────────
async function showAutoStatus() {
  try { await showVersions(null); } catch (e) { /* fine */ }
  try {
    const prefs = await invoke("prefs_get");
    $("pre-inline").checked = !!prefs.include_prereleases;
    $("pre-inline").disabled = !!prefs.prereleases_forced;
    $("close-action").value = prefs.close_action || "ask";
    $("pre-inline").parentElement.title = prefs.prereleases_forced ? FORCED_NOTE : "";
    $("update-auto").textContent = prefs.auto_update
      ? `Automatic: on start and every six hours, when the agent is idle${prefs.include_prereleases ? ", pre-releases included" : ""}. The stack follows this app's release.`
      : "Automatic updates are off (Settings). The stack still follows this app's release.";
  } catch (e) { /* fine */ }
}
async function showVersions(info) {
  const appV = info ? info.app_version : await invoke("app_version");
  const stack = info ? info.installed : await invoke("installed_version");
  const verified = info ? info.stack_verified : true;
  const stackText = stack ? `stack ${stack}${verified ? "" : " (unverified)"}` : "stack not installed";
  $("versions").textContent = `This app: ${appV} · ${stackText}`;
  $("stack-version").textContent = stack ? "stack " + stack : "";
}
$("btn-check-stack").onclick = async () => {
  $("update-text").textContent = "Checking…";
  $("btn-update-stack").disabled = true;
  $("btn-update-app").disabled = true;
  try {
    const info = await invoke("stack_check_update");
    await showVersions(info);
    if (info.available) {
      pendingStackUpdate = info.latest;
      $("update-text").textContent = info.stack_verified
        ? `Persei ${info.latest} is out; the stack is on ${info.installed}.`
        : `The stack's release cannot be verified; Update Persei puts it on ${info.latest}.`;
      $("btn-update-stack").disabled = false;
    } else {
      $("update-text").textContent = `The stack is up to date (${info.installed}).`;
    }
  } catch (e) { $("update-text").textContent = String(e); }
  try {
    appUpdate = await invoke("app_update_check");
    if (appUpdate.available) {
      $("update-text").textContent += ` This app has a new version too (${appUpdate.version}).`;
      $("btn-update-app").disabled = false;
    } else {
      $("update-text").textContent += " This app is the newest.";
    }
  } catch (e) { $("update-text").textContent += ` (App update check failed: ${String(e)})`; }
};
$("pre-inline").onchange = async () => {
  try {
    const prefs = await invoke("prefs_get");
    await invoke("prefs_set", { autoUpdate: prefs.auto_update, includePrereleases: $("pre-inline").checked });
    void showAutoStatus();
    $("btn-check-stack").onclick();
  } catch (e) { fail(e); }
};
$("btn-update-stack").onclick = async () => {
  if (!pendingStackUpdate) return;
  $("btn-update-stack").disabled = true;
  await runStack("stack_update", { tag: pendingStackUpdate });
  await showVersions(null);
  pendingStackUpdate = null;
};
$("btn-update-app").onclick = async () => {
  $("btn-update-app").disabled = true;
  try {
    await invoke("app_update_install");   // downloads, installs and restarts into the new app
  } catch (e) { fail(e); $("btn-update-app").disabled = false; }
};

// ── Logs ─────────────────────────────────────────────────────────────────────
$("btn-logs").onclick = async () => {
  try { await invoke("logs_follow", { service: $("log-service").value }); following = true; say(`Following ${$("log-service").value}…`); } catch (e) { fail(e); }
};
$("btn-logs-stop").onclick = async () => { await invoke("logs_stop"); if (following) say("Stopped following."); following = false; };
$("btn-logs-clear").onclick = () => { log.textContent = ""; };

// ── Window controls (frameless) ──────────────────────────────────────────────
try {
  const win = window.__TAURI__.window.getCurrentWindow();
  $("win-min").onclick = () => win.minimize();
  $("win-max").onclick = () => win.toggleMaximize();
  $("win-close").onclick = () => win.close();   // hides to the tray; Quit is in the tray menu
} catch (e) { /* not inside the app */ }

// ── Boot ─────────────────────────────────────────────────────────────────────
(async () => {
  try {
    const v = await window.__TAURI__.app.getVersion();
    $("app-version").textContent = "app v" + v;
  } catch (e) { /* fine */ }
  await checkDocker();
})();
