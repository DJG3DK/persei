//! Docker: is it there, is it running, and installing it when not.
//!
//! Windows: Docker's own installer, from Docker's own URL, run with its
//! documented silent flags and an elevation prompt the user sees. WSL 2
//! first when it is missing, because Docker Desktop's installer assumes it.
//! Virtualization switched off in the firmware is the one thing nothing
//! here can fix, so it is named, with the remedy, instead of guessed at.
//!
//! Linux (2026-10-04): Docker Engine through Docker's own install script,
//! run as root through polkit's password prompt (pkexec), the system
//! service enabled, and this account added to the docker group. A daemon
//! this account may not use is a state of its own (Denied), with the same
//! one-prompt remedy. Docker Desktop for Linux is used when it is there.

use crate::proc;
use serde::Serialize;
use tauri::{AppHandle, Emitter};

#[derive(Serialize, Clone, Copy, PartialEq, Eq, Debug)]
#[serde(rename_all = "kebab-case")]
pub enum DockerState {
    Missing,
    Stopped,
    /// The daemon runs, but this account may not use its socket (Linux:
    /// not in the docker group yet, or not since the last login).
    Denied,
    NoCompose,
    Ready,
}

/// `docker info`'s complaint when the socket is there and refuses us.
pub fn is_denied(text: &str) -> bool {
    let t = text.to_ascii_lowercase();
    t.contains("permission denied") && t.contains("docker")
}

pub async fn state() -> DockerState {
    match proc::capture("docker", &["--version"], None).await {
        Err(proc::ProcError::Missing(_)) => return DockerState::Missing,
        Err(_) => return DockerState::Missing,
        Ok(_) => {}
    }
    match proc::capture("docker", &["info"], None).await {
        Ok(_) => {}
        Err(proc::ProcError::Failed { tail, .. }) if is_denied(&tail) => {
            return DockerState::Denied
        }
        Err(_) => return DockerState::Stopped,
    }
    if !proc::succeeds("docker", &["compose", "version"]).await {
        return DockerState::NoCompose;
    }
    DockerState::Ready
}

fn note(app: &AppHandle, line: impl Into<String>) {
    let _ = app.emit(
        proc::LOG_EVENT,
        proc::LogLine {
            stream: "setup".into(),
            line: line.into(),
        },
    );
}

/// Set when this app started Docker Desktop, so Quit stops it again
/// (quit.rs). Docker Desktop the person started, or that starts with
/// Windows, is theirs and is left running.
#[derive(Default)]
pub struct StartedHere(pub std::sync::atomic::AtomicBool);

/// Stop Docker Desktop: `docker desktop stop`, Docker Desktop 4.37 and
/// later. An older one has no such command, and stays running.
pub async fn stop_desktop(app: &AppHandle) {
    note(app, "Stopping Docker Desktop...");
    // Linux's Docker Desktop is a user service; stopping it is plain systemd.
    if cfg!(target_os = "linux")
        && proc::succeeds("systemctl", &["--user", "stop", "docker-desktop"]).await
    {
        return;
    }
    if !proc::succeeds("docker", &["desktop", "stop"]).await {
        note(app, "Docker Desktop did not stop (it needs version 4.37 or later); quit it from its own tray icon.");
    }
}

/// Start Docker Desktop and wait for the daemon, up to two minutes.
pub async fn start_and_wait(app: &AppHandle) -> Result<(), String> {
    #[cfg(windows)]
    {
        let exe = ["C:\\Program Files\\Docker\\Docker\\Docker Desktop.exe",
                   "C:\\Program Files (x86)\\Docker\\Docker\\Docker Desktop.exe"]
            .into_iter()
            .find(|p| std::path::Path::new(p).exists())
            .ok_or("Docker Desktop is installed but its program was not found; start it yourself and try again")?;
        note(app, "Starting Docker Desktop...");
        mark_started(app);
        proc::capture(
            "powershell",
            &[
                "-NoProfile",
                "-Command",
                &format!("Start-Process -FilePath {}", ps_quote(exe)),
            ],
            None,
        )
        .await
        .map_err(|e| e.to_string())?;
    }
    #[cfg(target_os = "linux")]
    {
        if proc::succeeds("systemctl", &["--user", "cat", "docker-desktop"]).await {
            note(app, "Starting Docker Desktop...");
            mark_started(app);
            proc::capture("systemctl", &["--user", "start", "docker-desktop"], None)
                .await
                .map_err(|e| e.to_string())?;
        } else {
            note(
                app,
                "Starting the Docker service (your system asks for your password)...",
            );
            proc::capture("pkexec", &["systemctl", "start", "docker"], None)
                .await
                .map_err(|e| pkexec_failure(&e, "start the Docker service"))?;
        }
    }
    #[cfg(not(any(windows, target_os = "linux")))]
    {
        note(
            app,
            "Docker is not running. Start Docker Desktop and try again.",
        );
        return Err("Docker is not running".into());
    }
    #[allow(unreachable_code)]
    for i in 0..60 {
        if proc::succeeds("docker", &["info"]).await {
            note(app, "Docker is running.");
            return Ok(());
        }
        if i % 10 == 0 {
            note(app, "Waiting for Docker to come up...");
        }
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
    if cfg!(windows) {
        Err("Docker Desktop did not come up within two minutes. Open it and look at what it says; \
            \"Virtualization support not detected\" means the CPU's virtualization is off in the firmware."
            .into())
    } else {
        Err("Docker did not come up within two minutes; `systemctl status docker` says why.".into())
    }
}

/// Docker Desktop was started by this app (StartedHere).
#[cfg_attr(not(any(windows, target_os = "linux")), allow(dead_code))]
fn mark_started(app: &AppHandle) {
    if let Some(flag) = tauri::Manager::try_state::<StartedHere>(app) {
        flag.0.store(true, std::sync::atomic::Ordering::SeqCst);
    }
}

/// A POSIX shell single-quoted literal.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn sh_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "'\\''"))
}

/// A login name usermod will take, and nothing that could be read as more
/// than one: the name goes into a script that runs as root.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn plain_account(name: &str) -> bool {
    let mut chars = name.chars();
    matches!(chars.next(), Some(c) if c.is_ascii_lowercase() || c == '_')
        && name.len() <= 32
        && chars.all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_' || c == '-')
}

/// What runs as root to install Docker Engine on Linux: Docker's own
/// install script (it adds Docker's package repository and its signing
/// key, then installs the engine and the compose plugin from it), the
/// service enabled and started, and this account given the socket.
///
/// The downloaded file sits where this account can write, and the prompt
/// that follows makes it root (2026-10-22): anything running as this
/// account could swap it in between. So root runs a copy in a directory of
/// its own, after checking it against the hash taken of the bytes as they
/// arrived.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn linux_install_script(script: &str, sha256: &str, account: &str) -> String {
    assert!(
        sha256.len() == 64 && sha256.bytes().all(|b| b.is_ascii_hexdigit()),
        "a sha256 in hex"
    );
    format!(
        "set -e; d=$(mktemp -d); trap 'rm -rf \"$d\"' EXIT; cp {} \"$d/get-docker.sh\"; \
         echo \"{sha256}  $d/get-docker.sh\" | sha256sum -c --quiet -; sh \"$d/get-docker.sh\"; \
         systemctl enable --now docker; usermod -aG docker {}",
        sh_quote(script),
        sh_quote(account)
    )
}

/// Arch Linux or a distribution built on it, from /etc/os-release: its ID,
/// or Arch among the IDs it says it is like.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn arch_family(os_release: &str) -> bool {
    os_release.lines().any(|line| {
        let Some((key, value)) = line.split_once('=') else {
            return false;
        };
        let value = value.trim().trim_matches('"');
        match key.trim() {
            "ID" => value == "arch",
            "ID_LIKE" => value.split_whitespace().any(|v| v == "arch"),
            _ => false,
        }
    })
}

/// What runs as root to install Docker on an Arch-family system: the
/// engine and compose from the distribution's own repositories (no -y: a
/// sync without the upgrade is a partial upgrade, which Arch does not
/// support), the service enabled, and this account given the socket.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn pacman_install_script(account: &str) -> String {
    format!(
        "set -e; pacman -S --needed --noconfirm docker docker-compose; \
         systemctl enable --now docker; usermod -aG docker {}",
        sh_quote(account)
    )
}

/// pkexec's own exit codes, in words: 126 is the prompt dismissed, 127 is
/// not authorised (or pkexec not there to ask).
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn pkexec_failure(e: &proc::ProcError, what: &str) -> String {
    match e {
        proc::ProcError::Missing(_) => format!(
            "could not {what}: this system has no pkexec to ask for your password. \
             Run the commands in a terminal with sudo instead (Persei's install guide lists them)."
        ),
        proc::ProcError::Failed { code: 126, .. } => {
            format!("could not {what}: the password prompt was cancelled")
        }
        proc::ProcError::Failed { code: 127, .. } => {
            format!("could not {what}: your account is not allowed to administer this system")
        }
        other => format!("could not {what}: {other}"),
    }
}

/// This account's login name.
#[cfg(target_os = "linux")]
async fn account() -> Result<String, String> {
    let name = match std::env::var("USER") {
        Ok(u) if !u.is_empty() => u,
        _ => proc::capture("id", &["-un"], None)
            .await
            .map_err(|e| e.to_string())?
            .trim()
            .to_string(),
    };
    if plain_account(&name) {
        Ok(name)
    } else {
        Err(format!("the account name {name:?} is not one this installer will pass to usermod; add it to the docker group yourself"))
    }
}

#[cfg(windows)]
const DOCKER_INSTALLER_URL: &str =
    "https://desktop.docker.com/win/main/amd64/Docker%20Desktop%20Installer.exe";

/// A PowerShell single-quoted literal: the one character that needs
/// anything is the quote itself, doubled. A path holding an apostrophe
/// (a user called O'Brien) ended the literal early.
#[cfg_attr(not(windows), allow(dead_code))]
pub fn ps_quote(s: &str) -> String {
    format!("'{}'", s.replace('\'', "''"))
}

/// The script that runs Docker's installer elevated. Before the elevation
/// prompt it checks what was downloaded: a valid Authenticode signature,
/// and Docker's own name on it; an installer that fails that is not run.
/// The installer's exit code is the script's (`-PassThru`; `-Wait` alone
/// threw it away, and a failed install read as done).
#[cfg_attr(not(windows), allow(dead_code))]
pub fn installer_script(path: &str) -> String {
    let p = ps_quote(path);
    format!(
        "$sig = Get-AuthenticodeSignature -FilePath {p}; \
         if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notlike '*Docker Inc*') {{ \
           [Console]::Error.WriteLine('the downloaded installer is not signed by Docker Inc (' + $sig.Status + '); not running it'); exit 3 }}; \
         $p = Start-Process -FilePath {p} -ArgumentList 'install','--accept-license','--quiet' -Verb RunAs -PassThru -Wait; \
         exit $p.ExitCode"
    )
}

/// Install what is missing: WSL 2, then Docker Desktop. Each step elevates
/// through Windows' own prompt. Returns a note on what the user must do
/// next (a reboot, typically) or an error in plain words.
#[cfg(windows)]
pub async fn install(app: &AppHandle) -> Result<String, String> {
    let mut next = String::new();
    if !proc::succeeds("wsl", &["--status"]).await {
        note(
            app,
            "WSL 2 is not set up. Installing it (Windows will ask for permission)...",
        );
        let r = proc::capture("powershell", &["-NoProfile", "-Command",
            "Start-Process -FilePath wsl -ArgumentList '--install','--no-distribution' -Verb RunAs -Wait"], None).await;
        match r {
            Ok(_) => {
                next.push_str(
                    "WSL 2 was installed. Windows needs a restart before Docker can use it. ",
                );
            }
            Err(e) => return Err(format!("could not install WSL 2: {e}")),
        }
    }
    if state().await == DockerState::Missing {
        let dir = std::env::temp_dir();
        let path = dir.join("Docker Desktop Installer.exe");
        note(
            app,
            "Downloading Docker Desktop from desktop.docker.com (about 600 MB)...",
        );
        download(app, DOCKER_INSTALLER_URL, &path).await?;
        note(
            app,
            "Running the Docker Desktop installer (Windows will ask for permission)...",
        );
        let ps = installer_script(&path.display().to_string());
        proc::capture("powershell", &["-NoProfile", "-Command", &ps], None)
            .await
            .map_err(|e| format!("the Docker Desktop installer did not finish: {e}"))?;
        next.push_str("Docker Desktop is installed. Sign out and back in (or restart), open Docker Desktop once, then come back here.");
    }
    if next.is_empty() {
        next.push_str("Nothing to install.");
    }
    Ok(next)
}

/// Linux: Docker Engine when it is missing, the docker group when the
/// daemon refuses this account. One password prompt either way, and no
/// logging out: a new group reaches only new logins, so the app restarts
/// itself under `sg docker`, which starts it with the group already active
/// (a listed member needs no password for it). The next login has the group
/// for everything (2026-10-06; before, the app said to log out and back in).
#[cfg(target_os = "linux")]
pub async fn install(app: &AppHandle) -> Result<String, String> {
    let user = account().await?;
    match state().await {
        DockerState::Denied if listed_member(&docker_group_line().await, &user) => {
            restart_with_docker_group(app)
        }
        DockerState::Denied => {
            note(
                app,
                format!(
                    "Adding {user} to the docker group (your system asks for your password)..."
                ),
            );
            proc::capture("pkexec", &["usermod", "-aG", "docker", &user], None)
                .await
                .map_err(|e| pkexec_failure(&e, "add you to the docker group"))?;
            restart_with_docker_group(app)
        }
        DockerState::Missing
            if arch_family(&std::fs::read_to_string("/etc/os-release").unwrap_or_default()) =>
        {
            // Docker's install script refuses Arch and its derivatives
            // (CachyOS, Manjaro, EndeavourOS: "Unsupported distribution",
            // 2026-10-05); their own repositories carry Docker.
            note(app, "Installing Docker from your distribution's packages (your system asks for your password)...");
            let script = pacman_install_script(&user);
            proc::capture("pkexec", &["sh", "-c", &script], None)
                .await
                .map_err(|e| pkexec_failure(&e, "install Docker"))?;
            restart_with_docker_group(app)
        }
        DockerState::Missing => {
            // In the app's own data directory, not /tmp: the file runs as
            // root, and only this account can write here.
            let dir = crate::stack::dir(app)?;
            std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
            let path = dir.join("get-docker.sh");
            note(
                app,
                "Downloading Docker's install script from get.docker.com...",
            );
            let sha256 = download(app, DOCKER_SCRIPT_URL, &path).await?;
            note(app, "Installing Docker Engine (your system asks for your password; this takes a few minutes)...");
            let script = linux_install_script(&path.display().to_string(), &sha256, &user);
            let r = proc::capture("pkexec", &["sh", "-c", &script], None).await;
            let _ = std::fs::remove_file(&path);
            r.map_err(|e| pkexec_failure(&e, "install Docker"))?;
            restart_with_docker_group(app)
        }
        _ => Ok("Nothing to install.".into()),
    }
}

/// Set on the copy `sg docker` starts, so a daemon that still refuses it
/// is reported rather than restarted into again.
#[cfg(target_os = "linux")]
const RESTARTED: &str = "TEKTONIX_DOCKER_RESTARTED";

/// `getent group docker`: `docker:x:988:sam,bob`, or empty.
#[cfg(target_os = "linux")]
async fn docker_group_line() -> String {
    proc::capture("getent", &["group", "docker"], None)
        .await
        .unwrap_or_default()
}

/// Whether `user` is listed in a group line from getent.
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn listed_member(group_line: &str, user: &str) -> bool {
    group_line
        .trim()
        .rsplit(':')
        .next()
        .is_some_and(|members| members.split(',').any(|m| m == user))
}

/// Whether the panel may restart the app into the docker group without
/// asking anything: the account is a member already (added earlier, not
/// logged in since), sg is there, and this copy is not already the restart.
#[cfg(target_os = "linux")]
pub async fn can_restart_into_group() -> bool {
    std::env::var_os(RESTARTED).is_none()
        && which("sg")
        && match account().await {
            Ok(user) => listed_member(&docker_group_line().await, &user),
            Err(_) => false,
        }
}

#[cfg(not(target_os = "linux"))]
pub async fn can_restart_into_group() -> bool {
    false
}

#[cfg(target_os = "linux")]
fn which(program: &str) -> bool {
    std::env::var_os("PATH")
        .is_some_and(|paths| std::env::split_paths(&paths).any(|dir| dir.join(program).is_file()))
}

/// Start this app again under `sg docker` and close this copy. The new copy
/// waits a second, so this one has let go of the single-instance lock.
#[cfg(target_os = "linux")]
fn restart_with_docker_group(app: &AppHandle) -> Result<String, String> {
    if std::env::var_os(RESTARTED).is_some() || !which("sg") {
        return Ok("Docker is ready for your account. Log out and back in so it applies, then open Persei again.".into());
    }
    let exe = std::env::var_os("APPIMAGE")
        .map(std::path::PathBuf::from)
        .or_else(|| std::env::current_exe().ok())
        .ok_or("could not tell where this app is running from")?;
    let inner = sh_quote(&exe.display().to_string());
    std::process::Command::new("sh")
        .args(["-c", "sleep 1; exec sg docker -c \"$0\"", &inner])
        .env(RESTARTED, "1")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map_err(|e| format!("could not restart Persei: {e}"))?;
    let handle = app.clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_millis(400)).await;
        handle.exit(0);
    });
    note(
        app,
        "Docker is ready. Restarting Persei so it can use it...",
    );
    Ok("Docker is ready. Restarting Persei so it can use it...".into())
}

#[cfg(target_os = "linux")]
const DOCKER_SCRIPT_URL: &str = "https://get.docker.com";

#[cfg(not(any(windows, target_os = "linux")))]
pub async fn install(_app: &AppHandle) -> Result<String, String> {
    Err("Install Docker Desktop yourself, then come back here.".into())
}

/// Says the sha256 of the bytes as they arrived, in hex.
#[cfg(any(windows, target_os = "linux"))]
async fn download(app: &AppHandle, url: &str, path: &std::path::Path) -> Result<String, String> {
    use sha2::{Digest, Sha256};
    use tokio::io::AsyncWriteExt;
    let mut hash = Sha256::new();
    let resp = reqwest::get(url)
        .await
        .map_err(|e| format!("download failed: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("download failed: HTTP {}", resp.status()));
    }
    let total = resp.content_length().unwrap_or(0);
    let mut file = tokio::fs::File::create(path)
        .await
        .map_err(|e| format!("could not write {}: {e}", path.display()))?;
    let mut got: u64 = 0;
    let mut last_pct = 0;
    let mut stream = resp.bytes_stream();
    use futures_util::StreamExt;
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("download failed: {e}"))?;
        hash.update(&chunk);
        file.write_all(&chunk).await.map_err(|e| e.to_string())?;
        got += chunk.len() as u64;
        if let Some(pct) = (got * 100).checked_div(total).map(|p| p as u32) {
            if pct >= last_pct + 10 {
                last_pct = pct;
                note(app, format!("Downloaded {pct}%"));
            }
        }
    }
    file.flush().await.map_err(|e| e.to_string())?;
    Ok(hash.finalize().iter().map(|b| format!("{b:02x}")).collect())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_refused_socket_is_denied_and_a_stopped_daemon_is_not() {
        assert!(is_denied(
            "permission denied while trying to connect to the Docker daemon socket at unix:///var/run/docker.sock"
        ));
        assert!(!is_denied(
            "Cannot connect to the Docker daemon at unix:///var/run/docker.sock. Is the docker daemon running?"
        ));
    }

    #[test]
    fn only_a_plain_login_name_reaches_the_root_script() {
        for ok in ["danny", "_svc", "dev-1", "a_b"] {
            assert!(plain_account(ok), "{ok}");
        }
        for bad in ["", "Danny", "1abc", "a b", "x;reboot", "x'y", "$(id)"] {
            assert!(!plain_account(bad), "{bad}");
        }
    }

    #[test]
    fn the_linux_install_script_quotes_what_it_is_given() {
        let hex = "ab".repeat(32);
        let s = linux_install_script("/home/o'brien/.local/share/x/get-docker.sh", &hex, "danny");
        assert!(
            s.contains("cp '/home/o'\\''brien/.local/share/x/get-docker.sh' \"$d/get-docker.sh\";")
        );
        assert!(s.contains("systemctl enable --now docker"));
        assert!(s.ends_with("usermod -aG docker 'danny'"));
    }

    #[test]
    fn root_runs_its_own_copy_of_the_docker_script_after_checking_its_hash() {
        let hex = "0f".repeat(32);
        let s = linux_install_script("/u/get-docker.sh", &hex, "danny");
        let copy = s.find("cp '/u/get-docker.sh' \"$d/").unwrap();
        let check = s
            .find(&format!("{hex}  $d/get-docker.sh\" | sha256sum -c"))
            .unwrap();
        let run = s.find("sh \"$d/get-docker.sh\"").unwrap();
        assert!(s.starts_with("set -e; d=$(mktemp -d);") && copy < check && check < run);
        assert!(
            !s.contains("sh '/u/"),
            "never the file this account can write"
        );
        assert!(std::panic::catch_unwind(|| linux_install_script(
            "/u/x",
            "abc\"; reboot",
            "danny"
        ))
        .is_err());
    }

    #[test]
    fn arch_and_its_derivatives_use_pacman() {
        let cachyos = "NAME=\"CachyOS Linux\"\nID=cachyos\nID_LIKE=\"arch\"\n";
        let manjaro = "ID=manjaro\nID_LIKE=arch\n";
        let endeavour = "ID=endeavouros\nID_LIKE=\"arch\"\n";
        assert!(arch_family("ID=arch\n") && arch_family(cachyos));
        assert!(arch_family(manjaro) && arch_family(endeavour));
        for other in [
            "ID=ubuntu\nID_LIKE=debian\n",
            "ID=fedora\n",
            "ID=archery\n",
            "",
        ] {
            assert!(!arch_family(other), "{other}");
        }
        let s = pacman_install_script("danny");
        assert!(s.contains("pacman -S --needed --noconfirm docker docker-compose"));
        assert!(!s.contains("-Sy"), "no partial upgrade");
        assert!(s.ends_with("usermod -aG docker 'danny'"));
    }

    #[test]
    fn membership_is_read_from_the_group_line() {
        assert!(listed_member("docker:x:988:sam,bob\n", "sam"));
        assert!(listed_member("docker:x:988:bob,sam", "sam"));
        assert!(!listed_member("docker:x:988:samuel,bob", "sam"));
        assert!(!listed_member("docker:x:988:", "sam"));
        assert!(!listed_member("", "sam"));
    }

    #[test]
    fn pkexecs_own_codes_read_as_words() {
        let failed = |code| proc::ProcError::Failed {
            program: "pkexec".into(),
            code,
            tail: String::new(),
        };
        assert!(pkexec_failure(&failed(126), "x").contains("cancelled"));
        assert!(pkexec_failure(&failed(127), "x").contains("not allowed"));
        assert!(
            pkexec_failure(&proc::ProcError::Missing("pkexec".into()), "x").contains("no pkexec")
        );
    }

    #[test]
    fn a_path_with_an_apostrophe_survives_powershell() {
        assert_eq!(
            ps_quote("C:\\Users\\O'Brien\\Docker Desktop Installer.exe"),
            "'C:\\Users\\O''Brien\\Docker Desktop Installer.exe'"
        );
        assert_eq!(ps_quote("plain"), "'plain'");
    }

    #[test]
    fn the_installer_is_checked_before_elevation_and_its_exit_code_is_kept() {
        let script = installer_script("C:\\Temp\\Docker Desktop Installer.exe");
        let check = script
            .find("Get-AuthenticodeSignature")
            .expect("signature check");
        let run = script.find("Start-Process").expect("the run");
        assert!(check < run, "the check comes before the elevation prompt");
        assert!(
            script.contains("-notlike '*Docker Inc*'"),
            "Docker's own name on the signature"
        );
        assert!(
            script.contains("-PassThru -Wait"),
            "-Wait alone threw the exit code away"
        );
        assert!(script.ends_with("exit $p.ExitCode"));
        assert!(script.contains("-FilePath 'C:\\Temp\\Docker Desktop Installer.exe'"));
    }
}
