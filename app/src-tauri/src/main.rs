// Prevents an extra console window on Windows in release builds.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    // Linux: WebKitGTK's DMA-BUF renderer needs libGLESv2, which the
    // AppImage cannot carry (it is the system's GL dispatcher) and a desktop
    // without libgles2 does not have. There the app aborted before drawing
    // a window (2026-10-04, an Ubuntu 24.04 XFCE install); it is also the
    // renderer behind the blank windows NVIDIA drivers give WebKitGTK apps.
    // Set before anything starts a thread, and only when the person has not
    // chosen otherwise.
    #[cfg(target_os = "linux")]
    if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
        std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1");
    }
    // Linux: a downloaded AppImage installs itself into the app menu on its
    // first run, then runs again from there (integrate.rs). Before any
    // window, so nothing flashes up and is closed.
    // `--integrate`: only that, and no window (the one-line installer runs
    // it, possibly over ssh with no display).
    #[cfg(target_os = "linux")]
    if std::env::args_os()
        .nth(1)
        .is_some_and(|a| a == "--integrate")
    {
        std::process::exit(match tektonix_lib::integrate::integrate_only() {
            Ok(()) => 0,
            Err(e) => {
                eprintln!("persei: {e}");
                1
            }
        });
    }
    #[cfg(target_os = "linux")]
    if let Some(installed) = tektonix_lib::integrate::install_appimage() {
        use std::os::unix::process::CommandExt;
        let err = std::process::Command::new(&installed)
            .args(std::env::args_os().skip(1))
            .exec();
        eprintln!("could not run the installed copy ({err}); running from here");
    }
    tektonix_lib::run()
}
