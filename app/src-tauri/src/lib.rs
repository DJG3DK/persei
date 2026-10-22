//! Persei desktop: the dashboard in a window, the compose stack under it,
//! and a tray icon to reach both. One window, two pages: the window's own
//! page (src/) is a small control panel; the dashboard itself is the agent's
//! web app, shown in the same window once the stack answers (open_console,
//! open_panel). See stack.rs for the layout.

mod docker;
#[cfg(target_os = "linux")]
pub mod integrate;
mod proc;
mod quit;
mod stack;
mod update;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tauri::menu::{Menu, MenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{AppHandle, Emitter, Manager};
use tokio::sync::Mutex;

struct LogFollow(Mutex<Option<proc::Streaming>>);

#[tauri::command]
async fn docker_state() -> docker::DockerState {
    docker::state().await
}

#[tauri::command]
async fn docker_start(app: AppHandle) -> Result<(), String> {
    docker::start_and_wait(&app).await
}

/// Linux: the account is in the docker group but this session is older
/// than that; the panel restarts the app into the group without asking.
#[tauri::command]
async fn docker_can_restart() -> bool {
    docker::can_restart_into_group().await
}

#[tauri::command]
async fn docker_install(app: AppHandle) -> Result<String, String> {
    docker::install(&app).await
}

#[tauri::command]
fn settings_get(app: AppHandle) -> Result<stack::Settings, String> {
    stack::prepare(&app)?;
    stack::read_settings(&app)
}

#[tauri::command]
fn settings_save(
    app: AppHandle,
    key: Option<String>,
    projects_dir: String,
    admin_email: String,
    git_name: String,
    git_email: String,
) -> Result<stack::Settings, String> {
    stack::prepare(&app)?;
    stack::save_settings(&app, key, projects_dir, admin_email, git_name, git_email)
}

#[tauri::command]
async fn machine_git_identity() -> (String, String) {
    stack::machine_git_identity().await
}

#[tauri::command]
fn installed_version(app: AppHandle) -> Option<String> {
    stack::installed_version(&app)
}

/// First start: pull the release this app was built for, up, and set the
/// password chosen on the form (nothing if the account already has one).
#[tauri::command]
async fn stack_install(
    app: AppHandle,
    password: Option<String>,
    lock: tauri::State<'_, stack::StackLock>,
) -> Result<bool, String> {
    let _one_at_a_time = lock.0.lock().await;
    stack::prepare(&app)?;
    stack::pull(&app, &stack::release_tag(&app)).await?;
    stack::up(&app).await?;
    let email = stack::read_settings(&app)?.admin_email;
    match password {
        Some(p) => stack::set_first_password(&app, &email, &p).await,
        None => Ok(false),
    }
}

#[tauri::command]
async fn stack_up(app: AppHandle, lock: tauri::State<'_, stack::StackLock>) -> Result<(), String> {
    let _one_at_a_time = lock.0.lock().await;
    stack::prepare(&app)?;
    stack::start(&app).await
}

#[tauri::command]
async fn stack_down(
    app: AppHandle,
    lock: tauri::State<'_, stack::StackLock>,
) -> Result<(), String> {
    let _one_at_a_time = lock.0.lock().await;
    stack::down(&app).await
}

#[tauri::command]
async fn stack_status(app: AppHandle) -> Result<Vec<stack::Container>, String> {
    stack::status(&app).await
}

#[tauri::command]
async fn stack_password(app: AppHandle) -> Result<String, String> {
    stack::initial_password(&app).await
}

#[tauri::command]
async fn stack_check_update(app: AppHandle) -> Result<stack::UpdateInfo, String> {
    stack::check_update(&app).await
}

#[tauri::command]
async fn stack_update(
    app: AppHandle,
    tag: String,
    lock: tauri::State<'_, stack::StackLock>,
) -> Result<(), String> {
    let _one_at_a_time = lock.0.lock().await;
    stack::update_to(&app, &tag).await
}

#[tauri::command]
fn prefs_get(app: AppHandle) -> stack::PrefsView {
    stack::read_prefs_view(&app)
}

/// A candidate app keeps the operator's own pre-release choice untouched:
/// the box is shown ticked and disabled, and that is not a choice.
#[tauri::command]
fn prefs_set(
    app: AppHandle,
    auto_update: bool,
    include_prereleases: bool,
) -> Result<stack::PrefsView, String> {
    let mut prefs = stack::read_prefs(&app);
    prefs.auto_update = auto_update;
    if !stack::read_prefs_view(&app).prereleases_forced {
        prefs.include_prereleases = include_prereleases;
    }
    stack::write_prefs(&app, &prefs)?;
    Ok(stack::read_prefs_view(&app))
}

/// The close button's choice, from the control panel.
#[tauri::command]
fn prefs_set_close(app: AppHandle, action: String) -> Result<stack::PrefsView, String> {
    let action = quit::CloseAction::parse(&action).ok_or("unknown close action")?;
    let mut prefs = stack::read_prefs(&app);
    prefs.close_action = action;
    stack::write_prefs(&app, &prefs)?;
    Ok(stack::read_prefs_view(&app))
}

/// Quit from the control panel: the same as the tray's Quit.
#[tauri::command]
fn quit_app(app: AppHandle) {
    shutdown(app);
}

/// A two-button question; true for the first button. Esc or the window's
/// own close counts as the second.
async fn ask(app: &AppHandle, title: &str, text: &str, yes: &str, no: &str) -> bool {
    use tauri_plugin_dialog::{DialogExt, MessageDialogButtons, MessageDialogKind};
    let (tx, rx) = tokio::sync::oneshot::channel();
    app.dialog()
        .message(text)
        .title(title)
        .kind(MessageDialogKind::Info)
        .buttons(MessageDialogButtons::OkCancelCustom(yes.into(), no.into()))
        .show(move |first| {
            let _ = tx.send(first);
        });
    rx.await.unwrap_or(false)
}

fn log_line(app: &AppHandle, line: impl Into<String>) {
    let _ = app.emit(
        proc::LOG_EVENT,
        proc::LogLine {
            stream: "app".into(),
            line: line.into(),
        },
    );
}

static QUITTING: AtomicBool = AtomicBool::new(false);

/// Quit Persei (quit.rs): ask first when a task is running, stop the
/// stack, stop Docker Desktop when this app started it, then exit. A
/// second Quit while one is under way does nothing.
fn shutdown(app: AppHandle) {
    if QUITTING.swap(true, Ordering::SeqCst) {
        return;
    }
    tauri::async_runtime::spawn(async move {
        let started_docker = app
            .try_state::<docker::StartedHere>()
            .is_some_and(|s| s.0.load(Ordering::SeqCst));
        let plan = quit::plan(
            stack::agent_state().await,
            stack::installed_version(&app).is_some(),
            started_docker,
        );
        if plan.confirm
            && !ask(
                &app,
                "A task is running",
                "Quitting stops Persei, and the running task stops with it.",
                "Quit anyway",
                "Keep running",
            )
            .await
        {
            QUITTING.store(false, Ordering::SeqCst);
            return;
        }
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.hide();
        }
        if let Some(tray) = app.tray_by_id("tektonix") {
            let _ = tray.set_tooltip(Some("Persei: stopping..."));
        }
        if plan.stop_stack {
            // A Start or an update under way gets two minutes to finish; the
            // stop goes ahead either way.
            let lock = app.state::<stack::StackLock>();
            let _one_at_a_time =
                tokio::time::timeout(std::time::Duration::from_secs(120), lock.0.lock()).await;
            if let Err(e) = stack::down(&app).await {
                log_line(&app, format!("Stopping the stack failed: {e}"));
            }
        }
        if plan.stop_docker {
            docker::stop_desktop(&app).await;
        }
        stop_follow_on_exit(&app);
        app.exit(0);
    });
}

/// The window's close button, by the person's choice: hide to the tray, or
/// quit. Asked the first time and remembered; the panel changes it.
fn on_close(window: &tauri::Window) {
    let app = window.app_handle().clone();
    match stack::read_prefs(&app).close_action {
        quit::CloseAction::Tray => {
            let _ = window.hide();
        }
        quit::CloseAction::Quit => shutdown(app),
        quit::CloseAction::Ask => {
            tauri::async_runtime::spawn(async move {
                let quit = ask(
                    &app,
                    "Close Persei",
                    "Quit stops the agent and its containers and frees their memory. \
                     Keep running leaves them working in the tray, where Quit stops everything later.\n\n\
                     This is remembered; change it in the control panel.",
                    "Quit and stop",
                    "Keep running",
                )
                .await;
                let mut prefs = stack::read_prefs(&app);
                prefs.close_action = if quit {
                    quit::CloseAction::Quit
                } else {
                    quit::CloseAction::Tray
                };
                let _ = stack::write_prefs(&app, &prefs);
                if quit {
                    shutdown(app);
                } else if let Some(w) = app.get_webview_window("main") {
                    let _ = w.hide();
                }
            });
        }
    }
}

#[tauri::command]
fn app_version(app: AppHandle) -> String {
    stack::release_tag(&app)
}

#[tauri::command]
async fn app_update_check(app: AppHandle) -> Result<stack::AppUpdate, String> {
    stack::check_app_update(&app).await
}

#[tauri::command]
async fn app_update_install(app: AppHandle) -> Result<(), String> {
    stack::install_app_update(&app).await
}

/// Automatic updates: two minutes after start, then every six hours. Each
/// pass is one line in the log pane; a failure is a line too, never a dialog.
/// A pass that finds a stack operation under way (the first install, a
/// Start from the panel) skips rather than joining it.
fn spawn_auto_updater(app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(std::time::Duration::from_secs(120)).await;
        loop {
            let lock = app.state::<stack::StackLock>();
            match stack::auto_update_pass(&app, &lock).await {
                Ok(what) => {
                    let _ = app.emit(
                        proc::LOG_EVENT,
                        proc::LogLine {
                            stream: "app".into(),
                            line: format!("Update check: {what}."),
                        },
                    );
                }
                Err(e) => {
                    let _ = app.emit(
                        proc::LOG_EVENT,
                        proc::LogLine {
                            stream: "app".into(),
                            line: format!("Update check failed: {e}"),
                        },
                    );
                }
            }
            tokio::time::sleep(std::time::Duration::from_secs(6 * 3600)).await;
        }
    });
}

#[tauri::command]
async fn logs_follow(
    app: AppHandle,
    service: String,
    state: tauri::State<'_, Arc<LogFollow>>,
) -> Result<(), String> {
    // One follow at a time: the lock is held from stopping the old one to
    // storing the new, so two quick clicks cannot leave an orphan.
    let mut slot = state.0.lock().await;
    if let Some(old) = slot.take() {
        old.stop().await;
    }
    let dir = stack::dir(&app)?;
    let f = dir.join("docker-compose.yml").display().to_string();
    let d = dir.display().to_string();
    let args = [
        "compose",
        "--project-directory",
        d.as_str(),
        "-f",
        f.as_str(),
        "logs",
        "-f",
        "--tail",
        "200",
        service.as_str(),
    ];
    let child = proc::spawn_streaming(&app, "logs", "docker", &args, Some(&dir))
        .map_err(|e| e.to_string())?;
    *slot = Some(child);
    Ok(())
}

/// The app is going: the log follow goes with it. Quit from the tray and
/// the restart after a Linux update come through RunEvent::Exit; a Windows
/// update does not: the plugin starts the installer and calls
/// process::exit, so no destructor or exit event runs, and the
/// `docker compose logs -f` child outlived the app (2026-10-22). The
/// updater's on_before_exit calls this for that one (stack::updater).
pub(crate) fn stop_follow_on_exit(app: &AppHandle) {
    if let Some(follow) = app.try_state::<Arc<LogFollow>>() {
        if let Ok(mut slot) = follow.0.try_lock() {
            if let Some(child) = slot.take() {
                child.stop_now();
            }
        }
    }
}

#[tauri::command]
async fn logs_stop(state: tauri::State<'_, Arc<LogFollow>>) -> Result<(), String> {
    if let Some(child) = state.0.lock().await.take() {
        child.stop().await;
    }
    Ok(())
}

/// One window, two pages. The console (the agent's own web page) and the
/// control panel (this app's page) take turns in the main window: the
/// console when the stack answers, the panel before that and whenever it
/// is asked for. Navigating the window, rather than framing the console
/// in the panel, keeps the console's sign-in cookie first-party.
#[tauri::command]
async fn open_console(app: AppHandle) -> Result<(), String> {
    show_console(&app)
}

#[tauri::command]
async fn open_panel(app: AppHandle) -> Result<(), String> {
    show_panel_page(&app)
}

/// The projects folder from the settings, in the file manager. A command
/// rather than the opener plugin's open-path from the page, so no page is
/// allowed to open an arbitrary path.
#[tauri::command]
fn open_projects_dir(app: AppHandle) -> Result<(), String> {
    let dir = stack::read_settings(&app)?.projects_dir;
    if dir.is_empty() {
        return Err("no projects folder is set yet".into());
    }
    tauri_plugin_opener::open_path(dir, None::<&str>).map_err(|e| e.to_string())
}

fn show_console(app: &AppHandle) -> Result<(), String> {
    let w = app.get_webview_window("main").ok_or("no window")?;
    let url: tauri::Url = stack::DASHBOARD
        .parse()
        .map_err(|e: url::ParseError| e.to_string())?;
    w.navigate(url).map_err(|e| e.to_string())?;
    let _ = w.show();
    let _ = w.set_focus();
    stack::remember_page(app, "console");
    Ok(())
}

fn show_panel_page(app: &AppHandle) -> Result<(), String> {
    let w = app.get_webview_window("main").ok_or("no window")?;
    // On Windows the app's own pages are served from http://tauri.localhost.
    let page = if cfg!(windows) {
        "http://tauri.localhost/index.html"
    } else {
        "tauri://localhost/index.html"
    };
    let url: tauri::Url = page.parse().map_err(|e: url::ParseError| e.to_string())?;
    w.navigate(url).map_err(|e| e.to_string())?;
    let _ = w.show();
    let _ = w.set_focus();
    stack::remember_page(app, "panel");
    Ok(())
}

fn show_panel(app: &AppHandle) {
    if show_panel_page(app).is_err() {
        focus_window(app);
    }
}

/// Bring the window forward as it is: whatever page it shows stays.
fn focus_window(app: &AppHandle) {
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.show();
        let _ = w.set_focus();
    }
}

/// After a restart the window comes back to the page it showed: the
/// console, once the agent answers (up to a minute), else the panel.
fn restore_last_page(app: AppHandle) {
    if stack::read_prefs(&app).last_page != "console" {
        return;
    }
    tauri::async_runtime::spawn(async move {
        for _ in 0..60 {
            if stack::agent_state().await != update::Agent::Down {
                let _ = show_console(&app);
                return;
            }
            tokio::time::sleep(std::time::Duration::from_secs(1)).await;
        }
    });
}

fn build_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open console", true, None::<&str>)?;
    let panel = MenuItem::with_id(app, "panel", "Control panel", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit (stops Persei)", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &panel, &quit])?;
    // The gem tile, not the window icon: the app icon is the constellation,
    // which at tray size (16-24px) is a smudge (scripts/brand/build_assets.py).
    let icon = tauri::image::Image::from_bytes(include_bytes!("../icons/tray.png"))?;
    TrayIconBuilder::with_id("tektonix")
        .icon(icon)
        .tooltip("Persei")
        .menu(&menu)
        .show_menu_on_left_click(true)
        .on_menu_event(|app, event| match event.id.as_ref() {
            "open" => {
                let _ = show_console(app);
            }
            "panel" => show_panel(app),
            "quit" => shutdown(app.clone()),
            _ => {}
        })
        .build(app)?;
    Ok(())
}

/// Runs on every page the window shows, the app's own and the console's.
/// A frameless window is only usable through a strip drawn by the page it
/// shows. The app's page and a current console draw one; an older console
/// draws nothing, and the window could then neither be moved nor closed
/// (2026-09-29). So: when the page has drawn no strip, draw one here.
const STRIP_FALLBACK: &str = r#"
(function () {
  function ensure() {
    if (document.querySelector('.titlebar, .top[data-tauri-drag-region]')) return;
    var w = window.__TAURI__ && window.__TAURI__.window && window.__TAURI__.window.getCurrentWindow && window.__TAURI__.window.getCurrentWindow();
    if (!w) return;
    var bar = document.createElement('div');
    bar.setAttribute('data-tauri-drag-region', '');
    bar.style.cssText = 'position:fixed;top:0;left:0;right:0;height:30px;z-index:2147483647;display:flex;align-items:center;justify-content:space-between;padding-left:12px;background:#171b21;color:#e6e9ee;border-bottom:1px solid #2a313b;font:12px system-ui,sans-serif;user-select:none;-webkit-user-select:none;';
    var name = document.createElement('span'); name.textContent = 'Persei'; name.setAttribute('data-tauri-drag-region', ''); name.style.fontWeight = '600';
    var ctl = document.createElement('span'); ctl.style.cssText = 'display:flex;height:100%;';
    function btn(label, title, fn, close) {
      var b = document.createElement('button'); b.textContent = label; b.title = title;
      b.style.cssText = 'width:40px;height:100%;border:0;background:transparent;color:inherit;font:inherit;cursor:pointer;';
      b.onmouseenter = function () { b.style.background = close ? '#d9534f' : 'rgba(255,255,255,.08)'; };
      b.onmouseleave = function () { b.style.background = 'transparent'; };
      b.onclick = fn; return b;
    }
    var panel = btn('Control panel', 'Back to the control panel', function () { window.__TAURI__.core.invoke('open_panel'); });
    panel.style.width = 'auto'; panel.style.padding = '0 10px'; panel.style.opacity = '.75';
    ctl.appendChild(panel);
    ctl.appendChild(btn('\u2500', 'Minimise', function () { w.minimize(); }));
    ctl.appendChild(btn('\u25A1', 'Maximise', function () { w.toggleMaximize(); }));
    ctl.appendChild(btn('\u2715', 'Close', function () { w.close(); }, true));
    bar.appendChild(name); bar.appendChild(ctl);
    document.documentElement.appendChild(bar);
    document.documentElement.style.setProperty('--tektonix-strip', '30px');
    document.body && (document.body.style.paddingTop = '30px');
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', function () { setTimeout(ensure, 300); });
  else setTimeout(ensure, 300);
})();
"#;

/// Where the window may go: the app's own pages and the console. Anything
/// else (the login page's link to the project site, a link in a task's
/// answer) opens in the system browser. Followed in the window, an outside
/// page drew no title strip, and the frameless window could then neither
/// be moved nor closed (2026-09-29).
pub fn navigation_allowed(url: &tauri::Url) -> bool {
    match url.scheme() {
        "tauri" | "about" | "blob" | "data" => true,
        "http" | "https" => {
            let host = url.host_str().unwrap_or("");
            host == "tauri.localhost"
                || ((host == "localhost" || host == "127.0.0.1") && url.port() == Some(8100))
        }
        _ => false,
    }
}

fn build_main_window(app: &AppHandle) -> tauri::Result<()> {
    tauri::WebviewWindowBuilder::new(app, "main", tauri::WebviewUrl::App("index.html".into()))
        .title("Persei")
        .inner_size(1360.0, 860.0)
        .min_inner_size(900.0, 600.0)
        .decorations(false)
        .initialization_script(STRIP_FALLBACK)
        .on_navigation(|url| {
            if navigation_allowed(url) {
                return true;
            }
            let _ = tauri_plugin_opener::open_url(url.as_str(), None::<&str>);
            false
        })
        .build()?;
    Ok(())
}

pub fn run() {
    tauri::Builder::default()
        // One running copy. A second launch (the installer's "run when
        // finished" plus the Start menu, or a launch while the first sits in
        // the tray) brings the running window forward, on whatever page it
        // shows, instead of starting another app with a dead taskbar button
        // of its own (2026-09-29).
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            focus_window(app)
        }))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .manage(Arc::new(LogFollow(Mutex::new(None))))
        .manage(stack::StackLock::default())
        .manage(docker::StartedHere::default())
        .setup(|app| {
            build_main_window(app.handle())?;
            build_tray(app.handle())?;
            restore_last_page(app.handle().clone());
            spawn_auto_updater(app.handle().clone());
            Ok(())
        })
        .on_window_event(|window, event| {
            // The close button hides to the tray or quits, as the person
            // chose (on_close). The window itself is never destroyed here.
            if let tauri::WindowEvent::CloseRequested { api, .. } = event {
                if window.label() == "main" {
                    api.prevent_close();
                    on_close(window);
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            docker_state,
            docker_start,
            docker_install,
            docker_can_restart,
            settings_get,
            settings_save,
            machine_git_identity,
            installed_version,
            stack_install,
            stack_up,
            stack_down,
            stack_status,
            stack_password,
            stack_check_update,
            stack_update,
            prefs_get,
            prefs_set,
            prefs_set_close,
            quit_app,
            app_version,
            app_update_check,
            app_update_install,
            logs_follow,
            logs_stop,
            open_console,
            open_panel,
            open_projects_dir,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tektonix")
        .run(|app, event| {
            if let tauri::RunEvent::Exit = event {
                stop_follow_on_exit(app);
            }
        });
}

#[cfg(test)]
mod tests {
    use tauri::ipc::{CallbackFn, InvokeBody, InvokeResponseBody};
    use tauri::test::{get_ipc_response, mock_builder, MockRuntime, INVOKE_KEY};
    use tauri::webview::InvokeRequest;

    // Stand-ins under the real commands' names. The ACL is resolved by name
    // before anything is called, and the real commands take the Wry
    // runtime's AppHandle, which the mock runtime cannot supply.
    #[tauri::command(rename = "open_panel")]
    fn fake_open_panel() -> &'static str {
        "panel"
    }
    #[tauri::command(rename = "settings_save")]
    fn fake_settings_save() -> &'static str {
        "saved"
    }
    #[tauri::command(rename = "app_version")]
    fn fake_app_version() -> &'static str {
        "v0"
    }

    /// The app on the mock runtime with the real capabilities and ACL
    /// (generate_context! carries them).
    fn app() -> tauri::App<MockRuntime> {
        mock_builder()
            .invoke_handler(tauri::generate_handler![
                fake_open_panel,
                fake_settings_save,
                fake_app_version
            ])
            .build(tauri::generate_context!())
            .expect("the app builds on the mock runtime")
    }

    fn call(
        webview: &tauri::WebviewWindow<MockRuntime>,
        cmd: &str,
        origin: &str,
    ) -> Result<InvokeResponseBody, serde_json::Value> {
        get_ipc_response(
            webview,
            InvokeRequest {
                cmd: cmd.into(),
                callback: CallbackFn(0),
                error: CallbackFn(1),
                url: origin.parse().unwrap(),
                body: InvokeBody::default(),
                headers: Default::default(),
                invoke_key: INVOKE_KEY.to_string(),
            },
        )
    }

    #[test]
    fn the_window_stays_on_the_app_and_the_console() {
        let url = |s: &str| tauri::Url::parse(s).unwrap();
        for ok in [
            "tauri://localhost/index.html",
            "http://tauri.localhost/index.html",
            "http://localhost:8100/",
            "http://localhost:8100/tasks/1",
            "http://127.0.0.1:8100/login",
            "about:blank",
        ] {
            assert!(super::navigation_allowed(&url(ok)), "{ok}");
        }
        for out in [
            "https://perseiagent.ai/",
            "https://github.com/DJG3DK/persei",
            "http://localhost:8000/",
            "http://localhost/",
            "http://localhost.evil.example:8100/",
            "http://tauri.localhost.evil.example/",
            "file:///C:/Windows/",
        ] {
            assert!(
                !super::navigation_allowed(&url(out)),
                "{out} belongs in the system browser"
            );
        }
    }

    #[test]
    fn the_console_origin_reaches_the_panel_switch_and_nothing_else() {
        let app = app();
        let webview = tauri::WebviewWindowBuilder::new(&app, "main", Default::default())
            .build()
            .unwrap();
        let console = "http://localhost:8100/tasks";
        assert!(
            call(&webview, "open_panel", console).is_ok(),
            "2026-09-29: with no app manifest the console's Control panel button was refused"
        );
        for cmd in ["settings_save", "app_version", "plugin:process|exit"] {
            let refused = call(&webview, cmd, console).expect_err(cmd);
            assert!(
                refused.to_string().contains("not allowed"),
                "{cmd} from the console: {refused}"
            );
        }
        let local = if cfg!(windows) {
            "http://tauri.localhost/index.html"
        } else {
            "tauri://localhost/index.html"
        };
        assert!(
            call(&webview, "app_version", local).is_ok(),
            "the panel's own page keeps every command"
        );
        assert!(call(&webview, "open_panel", local).is_ok());
    }
}
