//! The stack under the app: the compose files it ships, the .env it keeps,
//! the released images it pulls, and `docker compose` around them.
//!
//! Layout, under the app's local data directory
//! (`%LOCALAPPDATA%\io.tektonix.desktop` on Windows, the bundle identifier):
//!
//!   stack/docker-compose.yml        copied from the app's resources on every
//!   stack/docker/...                launch, so an app update carries the
//!   stack/services/model-router/... stack's own files with it
//!   stack/.env                      the operator's: written by the settings
//!                                   form; prepare adds the app's own lines
//!                                   when missing and leaves the rest alone
//!   stack/version.json              which release's images are installed,
//!                                   the image id that proves it, and which
//!                                   compose file they were started under
//!   prefs.json                      the app's own preferences
//!
//! Images come from GHCR as ghcr.io/djg3dk/tektonix-<name>:<tag> and are
//! tagged with the local name the compose file uses (tektonix-<name>:latest),
//! so `docker compose up -d --no-build` starts a release the same way a
//! source checkout's `--build` would.

use crate::proc;
use crate::quit::CloseAction;
use crate::update::{self, Agent, StackMove};
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};
use tauri::{AppHandle, Emitter, Manager};

pub const IMAGES: [&str; 4] = ["agent", "router", "reviewer", "sandbox"];
pub const REGISTRY: &str = "ghcr.io/djg3dk";
pub const REPO: &str = "DJG3DK/persei";
pub const DASHBOARD: &str = "http://localhost:8100";
const HEALTH: &str = "http://127.0.0.1:8100/api/health";

/// The stack's files the app carries, relative to both the resource dir's
/// `stack/` and the data dir's `stack/`.
const SHIPPED: [&str; 5] = [
    "docker-compose.yml",
    ".env.example",
    "docker/checks-postgres/init.sh",
    "docker/postgres/init-secrets.sh",
    "services/model-router/config.example.yaml",
];

#[derive(Serialize, Deserialize, Clone, Default)]
pub struct Settings {
    pub openrouter_api_key_set: bool,
    pub openrouter_key_hint: String,
    pub projects_dir: String,
    pub admin_email: String,
    /// Whose commits these are; blank falls back to "Persei" and the
    /// sign-in address (docker-compose.yml).
    pub git_name: String,
    pub git_email: String,
}

#[derive(Serialize, Clone)]
pub struct Container {
    pub name: String,
    pub service: String,
    pub state: String,
    pub health: String,
}

/// What the stack runs: the release tag, and the id of the agent image
/// that tag was pulled as. The id is the proof; a record without one (an
/// early candidate wrote the requested tag whether or not the pull got it)
/// or whose image is no longer the local one is not trusted. The compose
/// fingerprint says which app's compose file the images were last started
/// under (update::StackMove::Recompose).
#[derive(Serialize, Deserialize, Clone, Default)]
pub struct Version {
    pub tag: String,
    #[serde(default)]
    pub image_id: Option<String>,
    #[serde(default)]
    pub compose: Option<String>,
}

/// Stack operations one at a time: install, Start, Stop, an update from
/// the panel, and the automatic pass, which skips rather than waits.
#[derive(Default)]
pub struct StackLock(pub tokio::sync::Mutex<()>);

/// The app's own preferences, in the data directory beside the stack.
#[derive(Serialize, Deserialize, Clone)]
pub struct Prefs {
    /// Check on startup and every few hours; update the app when a newer
    /// release is out and nothing is going on. The stack follows the app's
    /// release (auto_update_pass), preference or not.
    pub auto_update: bool,
    /// Count pre-releases (a dash in the tag) as releases.
    pub include_prereleases: bool,
    /// "console" or "panel": what the window showed last, so a restart
    /// (an update's, most of all) comes back to it.
    #[serde(default)]
    pub last_page: String,
    /// What the window's close button does (quit.rs). Absent in a
    /// preferences file from before 2026-10-03: the first close asks.
    #[serde(default)]
    pub close_action: CloseAction,
}

impl Default for Prefs {
    fn default() -> Self {
        Prefs {
            auto_update: true,
            include_prereleases: false,
            last_page: String::new(),
            close_action: CloseAction::Ask,
        }
    }
}

/// Record what the window shows; nothing else in the preferences moves.
pub fn remember_page(app: &AppHandle, page: &str) {
    let mut prefs = read_prefs(app);
    if prefs.last_page != page {
        prefs.last_page = page.to_string();
        let _ = write_prefs(app, &prefs);
    }
}

/// A release-candidate app counts release candidates as releases whatever
/// the preference says: its next update IS one. 2026-09-29: with the box
/// unticked, an rc app compared itself to the last stable release and said
/// it was up to date, forever.
pub fn wants_prereleases(app: &AppHandle) -> bool {
    prefs_view(&read_prefs(app), &release_tag(app)).include_prereleases
}

/// The preferences as the panel shows them. A release-candidate app has
/// pre-releases on whatever the box says, and the box now says so instead
/// of showing unticked beside a candidate update.
#[derive(Serialize, Clone, Debug, PartialEq)]
pub struct PrefsView {
    pub auto_update: bool,
    pub include_prereleases: bool,
    pub prereleases_forced: bool,
    pub last_page: String,
    pub close_action: CloseAction,
}

pub fn prefs_view(prefs: &Prefs, app_tag: &str) -> PrefsView {
    let forced = app_tag.contains('-');
    PrefsView {
        auto_update: prefs.auto_update,
        include_prereleases: prefs.include_prereleases || forced,
        prereleases_forced: forced,
        last_page: prefs.last_page.clone(),
        close_action: prefs.close_action,
    }
}

pub fn read_prefs_view(app: &AppHandle) -> PrefsView {
    prefs_view(&read_prefs(app), &release_tag(app))
}

/// Whether someone is using the window right now: it is shown and has the
/// focus. An update never restarts the app under a person's hands.
pub fn window_in_use(app: &AppHandle) -> bool {
    match app.get_webview_window("main") {
        Some(w) => w.is_visible().unwrap_or(false) && w.is_focused().unwrap_or(false),
        None => false,
    }
}

fn prefs_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(dir(app)?.join("prefs.json"))
}

pub fn read_prefs(app: &AppHandle) -> Prefs {
    prefs_path(app)
        .ok()
        .and_then(|p| std::fs::read_to_string(p).ok())
        .and_then(|t| serde_json::from_str(&t).ok())
        .unwrap_or_default()
}

pub fn write_prefs(app: &AppHandle, prefs: &Prefs) -> Result<Prefs, String> {
    let path = prefs_path(app)?;
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    }
    write_atomic(
        &path,
        serde_json::to_string_pretty(prefs)
            .map_err(|e| e.to_string())?
            .as_bytes(),
    )?;
    Ok(prefs.clone())
}

/// Every file this module keeps goes through here: written whole to a
/// sibling and renamed over the old one, so a crash mid-write leaves the
/// old file, not a torn one. A torn prefs.json read as the defaults, which
/// turned automatic updates back on. Says whether anything was written; an
/// unchanged file is left alone.
pub fn write_atomic(path: &Path, content: &[u8]) -> Result<bool, String> {
    if std::fs::read(path).is_ok_and(|old| old == content) {
        return Ok(false);
    }
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let tmp = path.with_file_name(format!("{name}.tmp"));
    std::fs::write(&tmp, content).map_err(|e| format!("could not write {}: {e}", tmp.display()))?;
    std::fs::rename(&tmp, path)
        .map_err(|e| format!("could not replace {}: {e}", path.display()))?;
    Ok(true)
}

#[derive(Serialize, Clone)]
pub struct UpdateInfo {
    /// The stack's recorded release; empty when nothing was recorded.
    pub installed: String,
    /// Whether that record is proven by the local image (stack.rs Version).
    pub stack_verified: bool,
    /// This app's own release.
    pub app_version: String,
    /// The release the stack should be on: the newer of GitHub's latest
    /// and this app's own.
    pub latest: String,
    pub available: bool,
    pub url: String,
    pub notes: String,
}

pub fn dir(app: &AppHandle) -> Result<PathBuf, String> {
    let base = app.path().app_local_data_dir().map_err(|e| e.to_string())?;
    Ok(base.join("stack"))
}

fn resources(app: &AppHandle) -> Result<PathBuf, String> {
    let base = app.path().resource_dir().map_err(|e| e.to_string())?;
    Ok(base.join("stack"))
}

fn note(app: &AppHandle, line: impl Into<String>) {
    let _ = app.emit(
        proc::LOG_EVENT,
        proc::LogLine {
            stream: "app".into(),
            line: line.into(),
        },
    );
}

/// Copy the shipped stack files into the data directory. Every launch, so
/// an app update updates the compose file with it; never .env.
pub fn prepare(app: &AppHandle) -> Result<PathBuf, String> {
    let src = resources(app)?;
    let dst = dir(app)?;
    for rel in SHIPPED {
        let from = src.join(rel);
        let to = dst.join(rel);
        if let Some(parent) = to.parent() {
            std::fs::create_dir_all(parent)
                .map_err(|e| format!("could not create {}: {e}", parent.display()))?;
        }
        std::fs::copy(&from, &to).map_err(|e| format!("could not copy {}: {e}", from.display()))?;
    }
    // The compose file binds this directory into the agent; an absent source
    // becomes an empty directory docker creates as root. Make it ourselves.
    std::fs::create_dir_all(dst.join("docker/agent-sandbox")).map_err(|e| e.to_string())?;
    let env = dst.join(".env");
    let fresh = !env.exists();
    if fresh {
        std::fs::copy(dst.join(".env.example"), &env).map_err(|e| e.to_string())?;
    }
    // This is the desktop app: the single-user sign-in rules (agent/auth.py,
    // desktop_install). Only the app writes this line.
    let mut content = std::fs::read_to_string(&env).unwrap_or_default();
    if get_env_value(&content, "TEKTONIX_DESKTOP").as_deref() != Some("1") {
        content = set_env_line(&content, "TEKTONIX_DESKTOP", "1");
    }
    // A machine of its own: checks get half its cores and four gigabytes,
    // written once so the operator can change them in the file. The example
    // file carries the small-server values (2 and 2g), so "absent" never
    // happened on a fresh install and every desktop ran its checks at 2 and
    // 2g (found 2026-10-04 in an Ubuntu VM): a new .env is sized here too.
    if fresh || get_env_value(&content, "SANDBOX_CPUS").is_none() {
        content = set_env_line(
            &content,
            "SANDBOX_CPUS",
            &sandbox_cpus(machine_cores()).to_string(),
        );
    }
    if fresh || get_env_value(&content, "SANDBOX_MEMORY").is_none() {
        content = set_env_line(&content, "SANDBOX_MEMORY", "4g");
    }
    // Linux: the stack runs as this account (docker/agent/entrypoint.sh,
    // step 5), so the projects folder stays the operator's own; Docker
    // Desktop ignores ownership and leaves these empty. The data directory
    // was just made by this process, so its owner is this account.
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if let Ok(meta) = std::fs::metadata(&dst) {
            content = fill_owner(&content, meta.uid(), meta.gid());
        }
    }
    write_atomic(&env, content.as_bytes())?;
    Ok(dst)
}

/// PUID and PGID when the file has none (an empty `PUID=` from the example
/// counts as none); an operator's own values stay.
#[cfg_attr(not(unix), allow(dead_code))]
pub fn fill_owner(content: &str, uid: u32, gid: u32) -> String {
    let unset = |k| get_env_value(content, k).map_or(true, |v| v.is_empty());
    if !unset("PUID") {
        return content.to_string();
    }
    let c = set_env_line(content, "PUID", &uid.to_string());
    set_env_line(&c, "PGID", &gid.to_string())
}

fn machine_cores() -> usize {
    std::thread::available_parallelism()
        .map(|n| n.get())
        .unwrap_or(4)
}

/// Half the machine, never fewer than two, never more than eight.
pub fn sandbox_cpus(cores: usize) -> usize {
    (cores / 2).clamp(2, 8)
}

// ── .env ─────────────────────────────────────────────────────────────────────

/// Replace `KEY=` in place, or append it. A commented-out line is left as
/// documentation. Same rules as install.ps1's Set-EnvLine, which replaces
/// every line of the key: compose takes the last one, so a duplicate left
/// behind would shadow the value written.
pub fn set_env_line(content: &str, key: &str, value: &str) -> String {
    let mut out = Vec::new();
    let mut done = false;
    for line in content.lines() {
        let trimmed = line.trim_start();
        if trimmed.starts_with(key)
            && trimmed[key.len()..].trim_start().starts_with('=')
            && !trimmed.starts_with('#')
        {
            if !done {
                out.push(format!("{key}={}", env_literal(value)));
                done = true;
            }
        } else {
            out.push(line.to_string());
        }
    }
    if !done {
        out.push(format!("{key}={}", env_literal(value)));
    }
    let mut s = out.join("\n");
    s.push('\n');
    s
}

pub fn get_env_value(content: &str, key: &str) -> Option<String> {
    content.lines().find_map(|line| {
        let t = line.trim();
        if t.starts_with('#') {
            return None;
        }
        let (k, v) = t.split_once('=')?;
        if k.trim() != key {
            return None;
        }
        Some(env_value(v.trim()))
    })
}

/// A value as compose reads it (env_literal writes it): single quotes are
/// literal, double quotes unescape `\\`, `\"` and `\$`, bare is bare.
fn env_value(raw: &str) -> String {
    if raw.len() >= 2 && raw.starts_with('\'') && raw.ends_with('\'') {
        return raw[1..raw.len() - 1].to_string();
    }
    if raw.len() >= 2 && raw.starts_with('"') && raw.ends_with('"') {
        let mut out = String::new();
        let mut chars = raw[1..raw.len() - 1].chars();
        while let Some(c) = chars.next() {
            match (c, chars.clone().next()) {
                ('\\', Some(n @ ('\\' | '"' | '$'))) => {
                    out.push(n);
                    chars.next();
                }
                _ => out.push(c),
            }
        }
        return out;
    }
    raw.to_string()
}

/// A value as compose must see it. Bare when nothing in it means anything
/// to compose; else single-quoted, where everything is literal; else, when
/// it holds a single quote, double-quoted with compose's escapes. Written
/// bare, `Dan #2` lost `#2` to the comment rule and `$` was interpolated.
pub fn env_literal(value: &str) -> String {
    let plain = !value
        .chars()
        .any(|c| c.is_whitespace() || matches!(c, '#' | '$' | '"' | '\''));
    if plain {
        return value.to_string();
    }
    if !value.contains('\'') {
        return format!("'{value}'");
    }
    let escaped = value
        .replace('\\', "\\\\")
        .replace('"', "\\\"")
        .replace('$', "\\$");
    format!("\"{escaped}\"")
}

fn env_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(dir(app)?.join(".env"))
}

pub fn read_settings(app: &AppHandle) -> Result<Settings, String> {
    let content = std::fs::read_to_string(env_path(app)?).unwrap_or_default();
    let key = get_env_value(&content, "OPENROUTER_API_KEY").unwrap_or_default();
    Ok(Settings {
        openrouter_api_key_set: !key.is_empty(),
        openrouter_key_hint: hint(&key),
        projects_dir: get_env_value(&content, "PROJECTS_DIR").unwrap_or_default(),
        admin_email: get_env_value(&content, "ADMIN_EMAIL")
            .unwrap_or_else(|| "admin@example.com".into()),
        git_name: get_env_value(&content, "GIT_USER_NAME").unwrap_or_default(),
        git_email: get_env_value(&content, "GIT_USER_EMAIL").unwrap_or_default(),
    })
}

/// This machine's own git identity, to prefill the form: most people who
/// have git have set it once.
pub async fn machine_git_identity() -> (String, String) {
    let name = proc::capture("git", &["config", "--global", "user.name"], None)
        .await
        .unwrap_or_default();
    let email = proc::capture("git", &["config", "--global", "user.email"], None)
        .await
        .unwrap_or_default();
    (name.trim().to_string(), email.trim().to_string())
}

/// The last four characters behind eight dots. Characters, not bytes: this
/// runs from settings_get at every launch, and a byte slice that split a
/// multibyte character at the end of the key aborted the app until .env
/// was edited by hand (panic = "abort").
fn hint(secret: &str) -> String {
    let count = secret.chars().count();
    if count <= 8 {
        return "•".repeat(count);
    }
    let tail: String = secret.chars().skip(count - 4).collect();
    format!("••••••••{tail}")
}

/// A path as typed for the projects folder: the quotes a person pastes
/// along with it stripped, and the trailing separator trimmed, since
/// compose joins onto the value. Not on a drive root: `D:\` minus its
/// separator is `D:`, the current directory on that drive. install.ps1's
/// Format-ProjectsDir keeps three characters or fewer as they are.
pub fn normalize_projects_dir(raw: &str) -> String {
    let v = raw.trim().trim_matches(['"', '\'']).trim();
    if v.chars().count() > 3 {
        v.trim_end_matches(['\\', '/']).to_string()
    } else {
        v.to_string()
    }
}

pub fn save_settings(
    app: &AppHandle,
    key: Option<String>,
    projects_dir: String,
    admin_email: String,
    git_name: String,
    git_email: String,
) -> Result<Settings, String> {
    let projects_dir = normalize_projects_dir(&projects_dir);
    if projects_dir.is_empty() || !Path::new(&projects_dir).is_absolute() {
        return Err(if cfg!(windows) {
            "the projects folder must be a full path, for example C:\\Users\\you\\code".into()
        } else {
            "the projects folder must be a full path, for example /home/you/code".into()
        });
    }
    std::fs::create_dir_all(&projects_dir)
        .map_err(|e| format!("could not create {projects_dir}: {e}"))?;
    let path = env_path(app)?;
    let mut content = std::fs::read_to_string(&path).unwrap_or_default();
    if let Some(k) = key.map(|k| k.trim().to_string()).filter(|k| !k.is_empty()) {
        content = set_env_line(&content, "OPENROUTER_API_KEY", &k);
    }
    content = set_env_line(&content, "PROJECTS_DIR", &projects_dir);
    let email = admin_email.trim();
    if !email.is_empty() {
        content = set_env_line(&content, "ADMIN_EMAIL", email);
    }
    content = set_env_line(&content, "GIT_USER_NAME", git_name.trim());
    content = set_env_line(&content, "GIT_USER_EMAIL", git_email.trim());
    write_atomic(&path, content.as_bytes())?;
    read_settings(app)
}

// ── versions and images ─────────────────────────────────────────────────────

fn version_path(app: &AppHandle) -> Result<PathBuf, String> {
    Ok(dir(app)?.join("version.json"))
}

pub fn installed_version(app: &AppHandle) -> Option<String> {
    let text = std::fs::read_to_string(version_path(app).ok()?).ok()?;
    serde_json::from_str::<Version>(&text)
        .ok()
        .map(|v| v.tag)
        .filter(|t| !t.is_empty())
}

fn read_version(app: &AppHandle) -> Option<Version> {
    let text = std::fs::read_to_string(version_path(app).ok()?).ok()?;
    serde_json::from_str::<Version>(&text)
        .ok()
        .filter(|v| !v.tag.is_empty())
}

fn write_version(app: &AppHandle, record: &Version) -> Result<(), String> {
    let text = serde_json::to_string(record).map_err(|e| e.to_string())?;
    write_atomic(&version_path(app)?, text.as_bytes()).map(|_| ())
}

/// Record that the stack was (or is next) started under this app's compose
/// file. After every `up`, and when a stopped stack's next Start will use it.
fn stamp_compose(app: &AppHandle) -> Result<(), String> {
    let Some(mut record) = read_version(app) else {
        return Ok(());
    };
    let now = Some(compose_fingerprint(app)?);
    if record.compose != now {
        record.compose = now;
        write_version(app, &record)?;
    }
    Ok(())
}

/// The shipped compose file's fingerprint: FNV-1a over its bytes, hex.
/// Change detection only, so no hashing crate for it.
fn compose_fingerprint(app: &AppHandle) -> Result<String, String> {
    let bytes = std::fs::read(dir(app)?.join("docker-compose.yml")).map_err(|e| e.to_string())?;
    Ok(fingerprint(&bytes))
}

pub fn fingerprint(bytes: &[u8]) -> String {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for b in bytes {
        h ^= u64::from(*b);
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    format!("{h:016x}")
}

/// The id of the agent image the compose file runs, as Docker has it now.
async fn local_agent_image_id() -> Option<String> {
    let out = proc::capture(
        "docker",
        &[
            "image",
            "inspect",
            &local_name("agent"),
            "--format",
            "{{.Id}}",
        ],
        None,
    )
    .await
    .ok()?;
    let id = out.trim().to_string();
    (!id.is_empty()).then_some(id)
}

/// Does the record say what the local images are? True only when it names
/// an image id and that is the image Docker has under the local name.
pub fn record_is_proven(record: &Version, local_id: Option<&str>) -> bool {
    matches!((record.image_id.as_deref(), local_id), (Some(a), Some(b)) if a == b)
}

/// The release this app was built from: the tag the release workflow bakes
/// in, else the package version. 2026-09-29: an rc app asked for `v0.9.0`
/// images, no such release existed, and the pull fell back to `latest`, so
/// every release candidate ran the previous stable release's stack.
pub fn release_tag(app: &AppHandle) -> String {
    match option_env!("TEKTONIX_RELEASE_TAG")
        .map(str::trim)
        .filter(|t| !t.is_empty())
    {
        Some(t) => t.to_string(),
        None => format!("v{}", app.package_info().version),
    }
}

/// `vMAJOR.MINOR.PATCH`, with an optional `-rcN`: a pre-release sorts below
/// the release it leads to, and among pre-releases the label comes first
/// (alpha, then beta, then rc, as the words sort) and its number second,
/// as a number. Anything unparseable sorts lowest.
fn version_key(tag: &str) -> (u64, u64, u64, bool, String, u64) {
    let raw = tag.trim().trim_start_matches('v');
    let (core, pre) = match raw.split_once('-') {
        Some((c, p)) => (c, Some(p)),
        None => (raw, None),
    };
    let mut nums = core.split('.').map(|n| n.parse::<u64>().unwrap_or(0));
    let (maj, min, pat) = (
        nums.next().unwrap_or(0),
        nums.next().unwrap_or(0),
        nums.next().unwrap_or(0),
    );
    match pre {
        None => (maj, min, pat, true, String::new(), 0),
        Some(p) => {
            let number = p.trim_start_matches(|c: char| !c.is_ascii_digit());
            let label = p[..p.len() - number.len()].trim_end_matches('.');
            (
                maj,
                min,
                pat,
                false,
                label.to_ascii_lowercase(),
                number.parse().unwrap_or(0),
            )
        }
    }
}

pub fn newer_than(candidate: &str, installed: &str) -> bool {
    version_key(candidate) > version_key(installed)
}

pub fn image_ref(name: &str, tag: &str) -> String {
    format!("{REGISTRY}/tektonix-{name}:{tag}")
}

pub fn local_name(name: &str) -> String {
    format!("tektonix-{name}:latest")
}

/// Pull one release's images and tag them with the local names: every pull
/// first, then every tag (update::pull_commands), so a pull that fails
/// leaves the local names on one release. The images are published before
/// the installer (release.yml), so a release the app knows always has
/// them; a missing image is an error, never a quiet swap for some other
/// release's code.
pub async fn pull(app: &AppHandle, tag: &str) -> Result<(), String> {
    let digests = release_digests(app, tag).await?;
    match &digests {
        Some(_) => note(app, format!("The image manifest for {tag} is signed by this app's key; pulling by digest.")),
        None => note(app, format!("{tag} has no signed image manifest (releases before it was introduced); pulling by tag.")),
    }
    for cmd in update::pull_commands(tag, digests.as_ref())? {
        let args: Vec<&str> = cmd.iter().map(String::as_str).collect();
        if args[0] == "pull" {
            let remote = args[1];
            note(app, format!("Pulling {remote}"));
            let code = proc::stream(app, "docker", "docker", &args, None)
                .await
                .map_err(|e| e.to_string())?;
            if code != 0 {
                return Err(format!("could not pull {remote} (exit {code}). Is this machine online, and is {tag} a published release?"));
            }
        } else {
            proc::capture("docker", &args, None)
                .await
                .map_err(|e| e.to_string())?;
        }
    }
    write_version(
        app,
        &Version {
            tag: tag.into(),
            image_id: local_agent_image_id().await,
            compose: Some(compose_fingerprint(app)?),
        },
    )
}

/// The release's signed image manifest (release.yml publishes digests.json
/// and its .sig beside the installer), checked against the updater key
/// this app was built with. None when the release has neither file, which
/// every release before the manifest existed has; a manifest without its
/// signature, or one this key did not sign, is an error, not a fallback.
async fn release_digests(app: &AppHandle, tag: &str) -> Result<Option<update::Digests>, String> {
    let client = reqwest::Client::builder()
        .user_agent("tektonix-desktop")
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let base = format!("https://github.com/{REPO}/releases/download/{tag}/digests.json");
    let fetch = |url: String| {
        let client = client.clone();
        async move {
            let r = client
                .get(&url)
                .send()
                .await
                .map_err(|e| format!("could not reach GitHub for {url}: {e}"))?;
            if r.status() == reqwest::StatusCode::NOT_FOUND {
                return Ok::<Option<Vec<u8>>, String>(None);
            }
            if !r.status().is_success() {
                return Err(format!("GitHub answered {} for {url}", r.status()));
            }
            Ok(Some(r.bytes().await.map_err(|e| e.to_string())?.to_vec()))
        }
    };
    let manifest = fetch(base.clone()).await?;
    let sig = fetch(format!("{base}.sig")).await?;
    match (manifest, sig) {
        (None, None) => Ok(None),
        (Some(m), Some(s)) => {
            let sig = String::from_utf8(s).map_err(|_| "the manifest signature is not text")?;
            update::verified_digests(&updater_pubkey(app)?, &m, &sig).map(Some)
        }
        (Some(_), None) => Err(format!(
            "{tag} has an image manifest but no signature for it"
        )),
        (None, Some(_)) => Err(format!("{tag} has a manifest signature but no manifest")),
    }
}

/// The updater's public key, as configured: what signs the installer signs
/// the image manifest.
fn updater_pubkey(app: &AppHandle) -> Result<String, String> {
    app.config()
        .plugins
        .0
        .get("updater")
        .and_then(|u| u.get("pubkey"))
        .and_then(|p| p.as_str())
        .map(str::to_string)
        .ok_or_else(|| "no updater key in the app's configuration".to_string())
}

/// The stack this app runs is at least the release it was built from, under
/// this app's compose file. A new app over an older stack (or a stack
/// recorded as `latest` by the old fallback) pulls its own images; a stack
/// moved ahead of the app by hand is left where it is. The decision is
/// update::own_release_move; this carries out the pull half and returns
/// the decision, so the caller knows whether to restart the stack.
pub async fn ensure_own_release(app: &AppHandle) -> Result<StackMove, String> {
    let wanted = release_tag(app);
    let record = read_version(app);
    let local = local_agent_image_id().await;
    let compose = compose_fingerprint(app)?;
    let seen = update::Seen {
        app_tag: &wanted,
        record: record.as_ref(),
        local_id: local.as_deref(),
        compose: &compose,
        agent: agent_state().await,
    };
    let decision = update::own_release_move(&seen);
    match &decision {
        StackMove::Current => {}
        // A running task is not interrupted for this: the automatic pass
        // comes back to it once the agent is idle.
        StackMove::Busy => note(
            app,
            format!("The agent is busy; the stack moves to {wanted} when it is idle."),
        ),
        StackMove::Recompose { .. } => note(
            app,
            format!("The stack is {wanted} but was started under an older app's compose file."),
        ),
        StackMove::Pull { .. } => {
            let proven = record
                .as_ref()
                .is_some_and(|r| record_is_proven(r, local.as_deref()));
            note(
                app,
                match &record {
                    Some(r) if proven => format!(
                        "This app is {wanted}; the stack is {}. Pulling {wanted}.",
                        r.tag
                    ),
                    Some(r) => format!(
                        "The stack record says {} but cannot be verified. Pulling {wanted}.",
                        r.tag
                    ),
                    None => format!("Pulling the {wanted} stack."),
                },
            );
            pull(app, &wanted).await?;
        }
    }
    Ok(decision)
}

/// Start, from the panel: this app's release when the registry answers,
/// else the images already here (update::start_on_local_images), then up.
pub async fn start(app: &AppHandle) -> Result<(), String> {
    if let Err(e) = ensure_own_release(app).await {
        let present = local_agent_image_id().await.is_some();
        note(app, update::start_on_local_images(&e, present)?);
    }
    up(app).await
}

fn compose_args<'a>(dir: &'a Path, rest: &[&'a str]) -> Vec<String> {
    let mut v = vec![
        "compose".to_string(),
        "--project-directory".into(),
        dir.display().to_string(),
        "-f".into(),
        dir.join("docker-compose.yml").display().to_string(),
    ];
    v.extend(rest.iter().map(|s| s.to_string()));
    v
}

async fn compose_stream(app: &AppHandle, rest: &[&str]) -> Result<i32, String> {
    let dir = dir(app)?;
    let args = compose_args(&dir, rest);
    let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    proc::stream(app, "compose", "docker", &refs, Some(&dir))
        .await
        .map_err(|e| e.to_string())
}

async fn compose_capture(app: &AppHandle, rest: &[&str]) -> Result<String, String> {
    let dir = dir(app)?;
    let args = compose_args(&dir, rest);
    let refs: Vec<&str> = args.iter().map(|s| s.as_str()).collect();
    proc::capture("docker", &refs, Some(&dir))
        .await
        .map_err(|e| e.to_string())
}

/// `docker compose up -d --no-build`, then wait for the dashboard.
pub async fn up(app: &AppHandle) -> Result<(), String> {
    let settings = read_settings(app)?;
    if !settings.openrouter_api_key_set || settings.projects_dir.is_empty() {
        return Err("set the OpenRouter key and the projects folder first".into());
    }
    note(app, "Starting the stack...");
    let code = compose_stream(app, &["up", "-d", "--no-build", "--remove-orphans"]).await?;
    if code != 0 {
        return Err(format!(
            "docker compose up failed (exit {code}); the lines above are Docker's own"
        ));
    }
    stamp_compose(app)?;
    wait_healthy(app).await
}

async fn wait_healthy(app: &AppHandle) -> Result<(), String> {
    let client = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(3))
        .build()
        .map_err(|e| e.to_string())?;
    for i in 0..150 {
        if let Ok(r) = client.get(HEALTH).send().await {
            if r.status().is_success() {
                note(app, format!("Persei is running at {DASHBOARD}"));
                return Ok(());
            }
        }
        if i % 15 == 0 {
            note(app, "Waiting for the agent to answer...");
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
    Err("the agent did not answer within five minutes; see the agent log".into())
}

pub async fn down(app: &AppHandle) -> Result<(), String> {
    note(app, "Stopping the stack...");
    let code = compose_stream(app, &["stop"]).await?;
    if code != 0 {
        return Err(format!("docker compose stop failed (exit {code})"));
    }
    note(app, "Stopped.");
    Ok(())
}

pub async fn status(app: &AppHandle) -> Result<Vec<Container>, String> {
    let out = compose_capture(app, &["ps", "-a", "--format", "json"])
        .await
        .unwrap_or_default();
    // compose prints one JSON object per line (older versions: an array).
    let mut rows = Vec::new();
    let mut objects: Vec<serde_json::Value> = Vec::new();
    if let Ok(serde_json::Value::Array(arr)) = serde_json::from_str::<serde_json::Value>(&out) {
        objects = arr;
    } else {
        for line in out.lines() {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(line) {
                objects.push(v);
            }
        }
    }
    for v in objects {
        let s = |k: &str| v.get(k).and_then(|x| x.as_str()).unwrap_or("").to_string();
        rows.push(Container {
            name: s("Name"),
            service: s("Service"),
            state: s("State"),
            health: s("Health"),
        });
    }
    Ok(rows)
}

pub async fn initial_password(app: &AppHandle) -> Result<String, String> {
    let out = compose_capture(
        app,
        &[
            "exec",
            "-T",
            "agent",
            "python",
            "scripts/show_initial_password.py",
        ],
    )
    .await?;
    Ok(out.trim().to_string())
}

// ── updates ──────────────────────────────────────────────────────────────────

#[derive(Deserialize)]
pub struct Release {
    pub tag_name: String,
    pub html_url: String,
    #[serde(default)]
    pub body: String,
    #[serde(default)]
    pub draft: bool,
}

pub async fn latest_release(include_prereleases: bool) -> Result<Release, String> {
    let client = reqwest::Client::builder()
        .user_agent("tektonix-desktop")
        .build()
        .map_err(|e| e.to_string())?;
    if !include_prereleases {
        let url = format!("https://api.github.com/repos/{REPO}/releases/latest");
        let r = client
            .get(&url)
            .send()
            .await
            .map_err(|e| format!("could not reach GitHub: {e}"))?;
        if !r.status().is_success() {
            return Err(format!(
                "GitHub answered {} for the latest release",
                r.status()
            ));
        }
        return r
            .json::<Release>()
            .await
            .map_err(|e| format!("unexpected answer from GitHub: {e}"));
    }
    // Drafts never count, pre-releases do. The listing is neither version-
    // ordered nor short: 22 candidates by 2026-09-29, and a page of ten
    // missed the newest.
    let url = releases_url();
    let r = client
        .get(&url)
        .send()
        .await
        .map_err(|e| format!("could not reach GitHub: {e}"))?;
    if !r.status().is_success() {
        return Err(format!("GitHub answered {} for the releases", r.status()));
    }
    let all = r
        .json::<Vec<Release>>()
        .await
        .map_err(|e| format!("unexpected answer from GitHub: {e}"))?;
    newest_release(all).ok_or_else(|| "no releases yet".to_string())
}

/// GitHub's largest page: every release this repository is likely to have.
pub fn releases_url() -> String {
    format!("https://api.github.com/repos/{REPO}/releases?per_page=100")
}

/// The newest by version among the published ones. GitHub's listing is
/// not newest first: on 2026-09-29 it led with rc9 above rc14, and the app
/// announced rc9 as the release that was out.
pub fn newest_release(all: Vec<Release>) -> Option<Release> {
    all.into_iter()
        .filter(|rel| !rel.draft)
        .max_by_key(|rel| version_key(&rel.tag_name))
}

pub async fn check_update(app: &AppHandle) -> Result<UpdateInfo, String> {
    let latest = latest_release(wants_prereleases(app)).await?;
    let mine = release_tag(app);
    let record = read_version(app);
    let local = local_agent_image_id().await;
    let proven = record
        .as_ref()
        .is_some_and(|r| record_is_proven(r, local.as_deref()));
    let installed = record.map(|r| r.tag).unwrap_or_default();
    // An app built ahead of GitHub's newest listing still wants its own.
    let target = if newer_than(&mine, &latest.tag_name) {
        mine.clone()
    } else {
        latest.tag_name.clone()
    };
    Ok(UpdateInfo {
        available: !proven || newer_than(&target, &installed),
        installed,
        stack_verified: proven,
        app_version: mine,
        latest: target,
        url: latest.html_url,
        notes: latest.body,
    })
}

/// Pull the release's images and restart the stack on them.
pub async fn update_to(app: &AppHandle, tag: &str) -> Result<(), String> {
    pull(app, tag).await?;
    up(app).await
}

/// The agent, from the public health route's `busy` count. Only no answer
/// at all means the stack is down; any answer that does not say counts as
/// busy (update::busy_in), so an automatic update never pulls a running
/// task's containers out from under it.
pub async fn agent_state() -> Agent {
    let Ok(client) = reqwest::Client::builder()
        .timeout(std::time::Duration::from_secs(5))
        .build()
    else {
        return Agent::Busy;
    };
    let Ok(r) = client.get(HEALTH).send().await else {
        return Agent::Down;
    };
    let body = r.text().await.unwrap_or_default();
    if update::busy_in(&body) {
        Agent::Busy
    } else {
        Agent::Idle
    }
}

/// Where the updater's manifest for a release lives: with the installer,
/// on that release. `releases/latest/download` would skip pre-releases.
pub fn updater_endpoint(tag: &str) -> String {
    format!("https://github.com/{REPO}/releases/download/{tag}/latest.json")
}

#[derive(Serialize, Clone)]
pub struct AppUpdate {
    pub available: bool,
    pub version: String,
    pub tag: String,
}

/// The app's updater for one release: its manifest lives on that release
/// (updater_endpoint), and releases are ordered the way the stack orders
/// them (version_key). The plugin's default is semver, which reads a
/// pre-release label as text, so `rc14 < rc9`: an rc9 app saw rc10 to rc22
/// as older and never moved (2026-09-29).
fn updater(app: &AppHandle, tag: &str) -> Result<tauri_plugin_updater::Updater, String> {
    use tauri_plugin_updater::UpdaterExt;
    let endpoint: tauri::Url = updater_endpoint(tag)
        .parse()
        .map_err(|e: url::ParseError| e.to_string())?;
    app.updater_builder()
        .endpoints(vec![endpoint])
        .map_err(|e| e.to_string())?
        .version_comparator(|current, release| {
            app_update_wanted(&current.to_string(), &release.version.to_string())
        })
        .build()
        .map_err(|e| e.to_string())
}

/// Whether a release's app replaces the running one: the stack's ordering,
/// nothing else.
pub fn app_update_wanted(current: &str, release: &str) -> bool {
    newer_than(release, current)
}

/// Is a newer app than this one attached to the newest release?
pub async fn check_app_update(app: &AppHandle) -> Result<AppUpdate, String> {
    let latest = latest_release(wants_prereleases(app)).await?;
    let updater = updater(app, &latest.tag_name)?;
    match updater.check().await {
        Ok(Some(u)) => Ok(AppUpdate {
            available: true,
            version: u.version.clone(),
            tag: latest.tag_name,
        }),
        Ok(None) => Ok(AppUpdate {
            available: false,
            version: String::new(),
            tag: latest.tag_name,
        }),
        Err(e) => Err(format!("could not check the app's own update: {e}")),
    }
}

/// Whether this copy of the app can replace itself. Tauri's updater
/// installs an AppImage on Linux and nothing else; a copy installed from
/// the .deb is the package manager's, and its update is the next .deb.
/// `appimage` is the APPIMAGE variable the AppImage runtime sets.
pub fn self_updating(linux: bool, appimage: Option<&str>) -> bool {
    !linux || appimage.is_some_and(|p| !p.is_empty())
}

/// Download and install the newest app, then restart into it.
pub async fn install_app_update(app: &AppHandle) -> Result<(), String> {
    let latest = latest_release(wants_prereleases(app)).await?;
    if !self_updating(
        cfg!(target_os = "linux"),
        std::env::var("APPIMAGE").ok().as_deref(),
    ) {
        let page = format!("https://github.com/{REPO}/releases/tag/{}", latest.tag_name);
        let _ = tauri_plugin_opener::open_url(&page, None::<&str>);
        return Err(format!(
            "this copy was installed from the .deb package, which updates by installing the new \
             one: the release page is open in your browser ({page})"
        ));
    }
    let updater = updater(app, &latest.tag_name)?;
    let Some(update) = updater.check().await.map_err(|e| e.to_string())? else {
        return Err("this app is already the newest".into());
    };
    note(app, format!("Downloading app {}...", update.version));
    update
        .download_and_install(|_, _| {}, || {})
        .await
        .map_err(|e| format!("app update failed: {e}"))?;
    note(app, "App updated; restarting.");
    app.restart();
}

/// One automatic pass: the stack onto this app's release, then the app
/// itself when a newer release is out. The stack follows the app rather
/// than the newest release, so its images always run under the compose
/// file they were released with; the new app brings the stack along at
/// its next pass. Says what it did and why not.
pub async fn auto_update_pass(app: &AppHandle, lock: &StackLock) -> Result<String, String> {
    let guard = lock.0.try_lock();
    let settings = read_settings(app)?;
    let ready = settings.openrouter_api_key_set && !settings.projects_dir.is_empty();
    if let Some(why) = update::pass_skip_reason(ready, read_version(app).is_some(), guard.is_err())
    {
        return Ok(why.into());
    }
    // Whatever the preference says, the stack runs this app's release: a
    // newly installed app over an older stack corrects it here, once the
    // agent is idle. A stack the operator stopped stays stopped; the new
    // images and compose file are there for the next Start.
    match ensure_own_release(app).await? {
        StackMove::Pull { restart: true } | StackMove::Recompose { restart: true } => {
            note(app, "Restarting the stack on this app's release.");
            up(app).await?;
        }
        StackMove::Pull { restart: false } => {
            note(
                app,
                "The stack is stopped; the new images run from the next Start.",
            );
        }
        StackMove::Recompose { restart: false } => stamp_compose(app)?,
        StackMove::Busy | StackMove::Current => {}
    }
    let prefs = read_prefs(app);
    if !prefs.auto_update {
        return Ok("automatic updates are off".into());
    }
    // The app replaces itself only when nothing is going on: no task in
    // flight, and nobody at the window. 2026-09-29: it reinstalled itself
    // while the operator was typing to a running task, and took the window.
    let mine = check_app_update(app).await?;
    if !mine.available {
        return Ok("up to date".into());
    }
    if agent_state().await == Agent::Busy {
        return Ok(format!(
            "app {} is out; it installs when the agent is idle, or from the panel",
            mine.version
        ));
    }
    if window_in_use(app) {
        return Ok(format!(
            "app {} is out; it installs when this window is not in use, or from the panel",
            mine.version
        ));
    }
    note(
        app,
        format!("A newer app ({}) is out; installing it", mine.version),
    );
    install_app_update(app).await?;
    Ok(format!("installing app {}", mine.version))
}

/// The first account's password, chosen on the setup form instead of read
/// out of a container. The agent seeds the account with a one-time
/// password and requires a change before anything else; this does that
/// change through the same two routes a person would use: sign in with
/// the one-time password, then set the chosen one. Nothing is stored here.
/// Ok(false) when there is no one-time password to use (the account
/// already has its password), which is the normal case after the first
/// start.
pub async fn set_first_password(
    app: &AppHandle,
    email: &str,
    password: &str,
) -> Result<bool, String> {
    if password.trim().is_empty() {
        return Ok(false);
    }
    // Checked here, before the one-time password is read: reading it
    // deletes it, and the agent's refusal of a weak password then left no
    // way to sign in at all (2026-09-29).
    if let Some(problem) = password_problem(password) {
        return Err(problem.into());
    }
    let one_time = match initial_password(app).await {
        Ok(p) if !p.is_empty() => p,
        _ => return Ok(false),
    };
    // From here on the one-time password is spent, so every failure hands
    // it back and the operator finishes by hand.
    if let Err(e) = change_first_password(email, &one_time, password).await {
        return Err(with_one_time_password(&e, &one_time));
    }
    note(app, "Your password is set. Sign in to the console with it.");
    Ok(true)
}

/// The agent's own rules (agent/auth.py validate_password_strength), in its
/// own words, so the panel refuses exactly what the agent would refuse.
pub fn password_problem(password: &str) -> Option<&'static str> {
    if password.chars().count() < 12 {
        return Some("password must be at least 12 characters");
    }
    if !password.chars().any(char::is_lowercase) {
        return Some("password must include a lowercase letter");
    }
    if !password.chars().any(char::is_uppercase) {
        return Some("password must include an uppercase letter");
    }
    if !password.chars().any(|c| c.is_ascii_digit()) {
        return Some("password must include a digit");
    }
    None
}

/// What the panel shows when the chosen password could not be set after
/// the one-time password was read: the one-time password itself, so the
/// sign-in can still happen.
pub fn with_one_time_password(error: &str, one_time: &str) -> String {
    format!(
        "{error}. Sign in to the console with the one-time password {one_time} and choose your password there."
    )
}

async fn change_first_password(email: &str, one_time: &str, password: &str) -> Result<(), String> {
    let client = reqwest::Client::builder()
        .cookie_store(true)
        .timeout(std::time::Duration::from_secs(20))
        .build()
        .map_err(|e| e.to_string())?;
    let base = DASHBOARD;
    let login = client
        .post(format!("{base}/api/auth/login"))
        .json(&serde_json::json!({"email": email, "password": one_time}))
        .send()
        .await
        .map_err(|e| format!("sign-in failed: {e}"))?;
    if !login.status().is_success() {
        return Err(format!(
            "the agent refused the first sign-in ({})",
            login.status()
        ));
    }
    let change = client
        .post(format!("{base}/api/auth/change-password"))
        .json(&serde_json::json!({"current_password": one_time, "new_password": password}))
        .send()
        .await
        .map_err(|e| format!("setting the password failed: {e}"))?;
    if !change.status().is_success() {
        let body = change.text().await.unwrap_or_default();
        return Err(format!(
            "the agent refused that password: {}",
            body.chars().take(200).collect::<String>()
        ));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn set_env_line_replaces_in_place_or_appends() {
        let s = "A=1\n# PROJECTS_DIR=old\nPROJECTS_DIR=\nB=2\n";
        let out = set_env_line(s, "PROJECTS_DIR", "C:\\Users\\me\\code");
        assert_eq!(
            out,
            "A=1\n# PROJECTS_DIR=old\nPROJECTS_DIR=C:\\Users\\me\\code\nB=2\n"
        );
        let out = set_env_line("A=1", "NEW", "x");
        assert_eq!(out, "A=1\nNEW=x\n");
        let out = set_env_line("PROJECTS_DIR_OLD=1\n", "PROJECTS_DIR", "x");
        assert_eq!(
            out, "PROJECTS_DIR_OLD=1\nPROJECTS_DIR=x\n",
            "a different key that starts the same is left alone"
        );
        let out = set_env_line("K=old\nA=1\nK=older\n", "K", "new");
        assert_eq!(
            out, "K=new\nA=1\n",
            "every line of the key goes (install.ps1 does the same); compose would have taken the last"
        );
    }

    #[test]
    fn a_value_compose_would_misread_is_quoted_and_reads_back() {
        assert_eq!(
            env_literal("C:\\Users\\me\\code"),
            "C:\\Users\\me\\code",
            "a backslash alone stays bare, as every install so far wrote it"
        );
        assert_eq!(env_literal("Dan #2"), "'Dan #2'", "bare, #2 was a comment");
        assert_eq!(
            env_literal("pa$$word"),
            "'pa$$word'",
            "bare, $$ was interpolated"
        );
        assert_eq!(
            env_literal("C:\\Users\\Dan Smith\\code"),
            "'C:\\Users\\Dan Smith\\code'"
        );
        assert_eq!(
            env_literal("O'Brien #1"),
            "\"O'Brien #1\"",
            "a single quote forces double quotes"
        );
        assert_eq!(
            env_literal("say \"hi\" $1 \\ 'x'"),
            "\"say \\\"hi\\\" \\$1 \\\\ 'x'\""
        );
        for value in [
            "plain",
            "Dan #2",
            "pa$$word",
            "O'Brien #1",
            "say \"hi\" $1 \\ 'x'",
            "C:\\Users\\Dan Smith\\code",
        ] {
            let content = set_env_line("A=1\n", "K", value);
            assert_eq!(
                get_env_value(&content, "K").as_deref(),
                Some(value),
                "{value} round-trips"
            );
        }
        assert_eq!(
            get_env_value("K='O''s'\n", "K").as_deref(),
            Some("O''s"),
            "single quotes are literal"
        );
    }

    #[test]
    fn a_file_is_replaced_whole_and_only_when_it_changed() {
        let dir = std::env::temp_dir().join(format!("tektonix-write-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let path = dir.join("prefs.json");
        assert!(
            write_atomic(&path, b"one").unwrap(),
            "a new file is written"
        );
        assert_eq!(std::fs::read(&path).unwrap(), b"one");
        assert!(
            !write_atomic(&path, b"one").unwrap(),
            "the same content is not rewritten"
        );
        assert!(write_atomic(&path, b"two").unwrap());
        assert_eq!(std::fs::read(&path).unwrap(), b"two");
        let left: Vec<_> = std::fs::read_dir(&dir)
            .unwrap()
            .map(|e| e.unwrap().file_name().to_string_lossy().into_owned())
            .collect();
        assert_eq!(left, vec!["prefs.json"], "no temp file stays behind");
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn a_projects_dir_is_normalised_like_install_ps1_does_it() {
        // install.ps1's Format-ProjectsDir cases, tests/test_install_ps1.ps1.
        assert_eq!(
            normalize_projects_dir("\"C:\\Users\\you\\code\""),
            "C:\\Users\\you\\code"
        );
        assert_eq!(
            normalize_projects_dir("C:\\Users\\you\\code\\"),
            "C:\\Users\\you\\code"
        );
        assert_eq!(normalize_projects_dir("/home/you/code/"), "/home/you/code");
        assert_eq!(
            normalize_projects_dir("C:\\"),
            "C:\\",
            "a drive root keeps its separator; C: alone is that drive's current directory"
        );
        assert_eq!(normalize_projects_dir("  'D:\\' "), "D:\\");
    }

    #[test]
    fn get_env_value_skips_comments_and_strips_quotes() {
        let s =
            "# OPENROUTER_API_KEY=nope\nOPENROUTER_API_KEY=\"sk-or-1234\"\nPROJECTS_DIR=C:\\code\n";
        assert_eq!(
            get_env_value(s, "OPENROUTER_API_KEY").as_deref(),
            Some("sk-or-1234")
        );
        assert_eq!(
            get_env_value(s, "PROJECTS_DIR").as_deref(),
            Some("C:\\code")
        );
        assert_eq!(get_env_value(s, "MISSING"), None);
    }

    #[test]
    fn image_names_follow_the_release_workflow_and_the_compose_file() {
        assert_eq!(
            image_ref("agent", "v0.9.0"),
            "ghcr.io/djg3dk/tektonix-agent:v0.9.0"
        );
        assert_eq!(local_name("reviewer"), "tektonix-reviewer:latest");
        assert_eq!(IMAGES, ["agent", "router", "reviewer", "sandbox"]);
    }

    #[test]
    fn checks_get_half_the_machine_within_bounds() {
        assert_eq!(sandbox_cpus(2), 2);
        assert_eq!(sandbox_cpus(8), 4);
        assert_eq!(sandbox_cpus(32), 8);
    }

    #[test]
    fn the_newest_release_is_chosen_by_version_not_by_listing_order() {
        let rel = |tag: &str, draft: bool| Release {
            tag_name: tag.into(),
            html_url: String::new(),
            body: String::new(),
            draft,
        };
        let all = vec![
            rel("v0.9.0-rc9", false),
            rel("v0.9.0-rc14", false),
            rel("v0.9.0-rc15", true),
            rel("v0.9.0-rc13", false),
        ];
        assert_eq!(
            newest_release(all).map(|r| r.tag_name).as_deref(),
            Some("v0.9.0-rc14"),
            "a draft never counts"
        );
        assert!(newest_release(vec![]).is_none());
    }

    #[test]
    fn the_release_listing_asks_for_the_largest_page() {
        assert!(releases_url().ends_with("/releases?per_page=100"));
    }

    #[test]
    fn a_record_is_trusted_only_with_the_image_it_names() {
        let legacy = Version {
            tag: "v0.9.0".into(),
            image_id: None,
            compose: None,
        };
        assert!(
            !record_is_proven(&legacy, Some("sha256:abc")),
            "an early candidate's record names no image"
        );
        let mine = Version {
            tag: "v0.9.0-rc13".into(),
            image_id: Some("sha256:abc".into()),
            compose: None,
        };
        assert!(record_is_proven(&mine, Some("sha256:abc")));
        assert!(
            !record_is_proven(&mine, Some("sha256:other")),
            "someone retagged the local image"
        );
        assert!(!record_is_proven(&mine, None), "no local image at all");
        let old: Version = serde_json::from_str(r#"{"tag":"latest"}"#).unwrap();
        assert_eq!(old.image_id, None, "the old record still parses");
    }

    #[test]
    fn a_pre_release_sorts_below_its_release_and_above_the_one_before() {
        assert!(newer_than("v0.9.0-rc11", "v0.8.0"));
        assert!(newer_than("v0.9.0-rc12", "v0.9.0-rc11"));
        assert!(newer_than("v0.9.0", "v0.9.0-rc12"));
        assert!(
            !newer_than("v0.8.0", "v0.9.0-rc11"),
            "a stable release older than the installed rc is not an update"
        );
        assert!(!newer_than("v0.9.0-rc11", "v0.9.0-rc11"));
        assert!(
            !newer_than("latest", "v0.9.0-rc11"),
            "the old fallback's record never wins"
        );
        assert!(
            newer_than("v0.9.0-rc11", "latest"),
            "and is replaced by any real release"
        );
    }

    #[test]
    fn a_pre_release_label_is_compared_before_its_number() {
        assert!(
            newer_than("v0.10.0-rc1", "v0.10.0-beta3"),
            "the number alone said beta3 > rc1"
        );
        assert!(newer_than("v0.10.0-beta1", "v0.10.0-alpha9"));
        assert!(newer_than("v0.10.0-rc2", "v0.10.0-rc1"));
        assert!(newer_than("v0.10.0-rc10", "v0.10.0-rc9"), "still a number");
        assert!(newer_than("v0.10.0", "v0.10.0-rc99"));
        assert!(
            newer_than("v0.10.0-rc.2", "v0.10.0-rc1"),
            "a semver-style dot in the label is the same label"
        );
        assert!(
            !newer_than("v0.10.0-RC1", "v0.10.0-rc1"),
            "case is not a difference"
        );
    }

    #[test]
    fn the_app_updater_orders_release_candidates_like_the_stack() {
        // The comparator the updater is built with, against the versions the
        // plugin hands it (no `v`: Cargo.toml's, and the manifest's).
        for (current, release) in [
            ("0.9.0-rc9", "0.9.0-rc10"),
            ("0.9.0-rc19", "0.9.0-rc20"),
            ("0.9.0-rc22", "0.9.0"),
            ("0.9.0", "0.9.1-rc1"),
        ] {
            assert!(
                app_update_wanted(current, release),
                "{current} -> {release}"
            );
            assert!(
                !app_update_wanted(release, current),
                "{release} -> {current} is a downgrade"
            );
            assert_eq!(
                app_update_wanted(current, release),
                newer_than(release, current),
                "the comparator and the stack agree"
            );
        }
        assert!(!app_update_wanted("0.9.0-rc10", "0.9.0-rc10"));
        // The plugin's default, which the comparator replaces: an rc9 app
        // saw rc10 to rc22 as older (2026-09-29).
        let sem = |v: &str| semver::Version::parse(v).unwrap();
        assert!(
            sem("0.9.0-rc10") < sem("0.9.0-rc9"),
            "semver reads the label as text"
        );
    }

    #[test]
    fn the_updater_manifest_lives_on_the_release_not_on_latest() {
        assert_eq!(
            updater_endpoint("v0.9.0-rc6"),
            "https://github.com/DJG3DK/persei/releases/download/v0.9.0-rc6/latest.json"
        );
    }

    #[test]
    fn preferences_default_to_automatic_without_prereleases() {
        let p = Prefs::default();
        assert!(p.auto_update && !p.include_prereleases);
        let back: Prefs = serde_json::from_str(&serde_json::to_string(&p).unwrap()).unwrap();
        assert!(back.auto_update);
    }

    #[test]
    fn a_candidate_app_shows_prereleases_as_on_and_forced() {
        let p = Prefs::default();
        let rc = prefs_view(&p, "v0.9.0-rc17");
        assert!(
            rc.include_prereleases && rc.prereleases_forced,
            "the box was unticked next to an rc update"
        );
        let stable = prefs_view(&p, "v0.9.0");
        assert!(!stable.include_prereleases && !stable.prereleases_forced);
        let chosen = Prefs {
            include_prereleases: true,
            ..Prefs::default()
        };
        let v = prefs_view(&chosen, "v0.9.0");
        assert!(
            v.include_prereleases && !v.prereleases_forced,
            "the operator's own choice is not forced"
        );
    }

    #[test]
    fn the_owner_is_filled_once_and_an_operators_own_is_kept() {
        let example = "PUID=\nPGID=\nOTHER=1\n";
        let filled = fill_owner(example, 1000, 1001);
        assert_eq!(get_env_value(&filled, "PUID").as_deref(), Some("1000"));
        assert_eq!(get_env_value(&filled, "PGID").as_deref(), Some("1001"));
        assert_eq!(
            fill_owner(&filled, 5, 5),
            filled,
            "a set owner is left alone"
        );
        let absent = fill_owner("OTHER=1\n", 1000, 1000);
        assert_eq!(get_env_value(&absent, "PUID").as_deref(), Some("1000"));
    }

    #[test]
    fn only_an_appimage_replaces_itself_on_linux() {
        assert!(
            self_updating(false, None),
            "Windows and macOS update themselves"
        );
        assert!(self_updating(true, Some("/home/u/Tektonix.AppImage")));
        assert!(!self_updating(true, None), "a .deb install");
        assert!(!self_updating(true, Some("")));
    }

    #[test]
    fn the_password_rules_are_the_agents_and_run_before_anything_is_spent() {
        // agent/auth.py validate_password_strength, rule for rule.
        assert_eq!(
            password_problem("Short1a"),
            Some("password must be at least 12 characters")
        );
        assert_eq!(
            password_problem("correct horse battery staple"),
            Some("password must include an uppercase letter"),
            "2026-09-29: a long all-lowercase passphrase passed the panel, the agent refused it, and the one-time password was gone"
        );
        assert_eq!(
            password_problem("CORRECT HORSE BATTERY 1"),
            Some("password must include a lowercase letter")
        );
        assert_eq!(
            password_problem("Correct Horse Battery"),
            Some("password must include a digit")
        );
        assert_eq!(password_problem("Correct Horse Battery 1"), None);
        assert_eq!(
            password_problem("Ünïcödé pässwörd 1"),
            None,
            "letters outside ASCII are letters"
        );
        let msg = with_one_time_password("the agent refused the first sign-in (401)", "abc-def");
        assert!(msg.contains("abc-def") && msg.starts_with("the agent refused"));
    }

    #[test]
    fn a_secret_is_hinted_never_shown() {
        assert_eq!(hint("sk-or-v1-abcdefgh1234"), "••••••••1234");
        assert_eq!(hint("short"), "•••••");
        assert_eq!(
            hint("sk-or-v1-abcdefgh12é"),
            "••••••••h12é",
            "a multibyte character at the end is a character, not a panic"
        );
        assert_eq!(hint("ééééé"), "•••••");
    }
}
