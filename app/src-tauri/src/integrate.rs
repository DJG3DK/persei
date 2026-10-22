//! Linux: an AppImage that installs itself (2026-10-06).
//!
//! A downloaded AppImage runs from wherever the browser put it and appears
//! in no menu. On its first run this moves it to a fixed place in the app's
//! own data directory, writes a menu entry and an icon into the user's own
//! XDG directories (no root), and runs again from there. The updater
//! replaces the AppImage at that same path, so an update keeps the menu
//! entry pointing at the current version. Nothing here touches a copy
//! installed from the .deb (no APPIMAGE variable), and
//! TEKTONIX_NO_INSTALL=1 turns it off.

use std::path::{Path, PathBuf};

/// The app's identifier, its data directory's name (tauri.conf.json).
const IDENTIFIER: &str = "io.tektonix.desktop";
/// The window class the menu entry is matched to (the .deb's own entry
/// says the same), so a running window groups under the menu's icon. It is
/// the binary's name (tauri.conf.json mainBinaryName).
const WM_CLASS: &str = "persei";
/// The entry and icon name, the same as the binary's.
const NAME: &str = "persei";
/// What versions before the rename to Persei (0.9.4) wrote. An update
/// replaces the AppImage in place, so the first run of the new one removes
/// these, or the menu would show the app twice.
const OLD_NAME: &str = "tektonix";

/// `$XDG_DATA_HOME`, else `~/.local/share`.
pub fn data_home(xdg_data_home: Option<&str>, home: &Path) -> PathBuf {
    match xdg_data_home.filter(|d| Path::new(d).is_absolute()) {
        Some(d) => PathBuf::from(d),
        None => home.join(".local/share"),
    }
}

/// Where the installed AppImage lives: one name, whatever the version.
pub fn installed_path(data_home: &Path) -> PathBuf {
    data_home.join(IDENTIFIER).join("Tektonix.AppImage")
}

/// A path as a desktop-entry Exec argument: double-quoted, with the four
/// characters the specification reserves inside quotes escaped, and `%`
/// doubled so it is not read as a field code. The value is a string first,
/// whose own escaping turns `\\` into `\`, so every quoting backslash is
/// written twice: `\\$` for `$`, four for one backslash (the
/// specification's own examples).
pub fn exec_arg(path: &Path) -> String {
    let mut out = String::from("\"");
    for c in path.display().to_string().chars() {
        match c {
            '"' | '`' | '$' => {
                out.push_str("\\\\");
                out.push(c);
            }
            '\\' => out.push_str("\\\\\\\\"),
            '%' => out.push_str("%%"),
            _ => out.push(c),
        }
    }
    out.push('"');
    out
}

/// The menu entry, the same as the .deb's but for where it runs from.
pub fn desktop_entry(exec: &Path) -> String {
    format!(
        "[Desktop Entry]\nType=Application\nName=Persei\nComment=A self-hosted coding agent\n\
         Exec={} %U\nIcon={NAME}\nTerminal=false\nCategories=Development;\n\
         StartupWMClass={WM_CLASS}\n",
        exec_arg(exec)
    )
}

/// What to do for an AppImage at `running`, given where it belongs.
#[derive(Debug, PartialEq, Eq)]
pub enum Step {
    /// Not an AppImage, or turned off: nothing.
    Nothing,
    /// Already in place: make sure the menu entry is there.
    Entry,
    /// Elsewhere (Downloads, say): move it, write the entry, run again.
    Move,
}

pub fn step(running: Option<&Path>, installed: &Path, opted_out: bool) -> Step {
    match running {
        _ if opted_out => Step::Nothing,
        None => Step::Nothing,
        Some(p) if p == installed => Step::Entry,
        Some(_) => Step::Move,
    }
}

const ICONS: [(&str, &[u8]); 3] = [
    ("32x32", include_bytes!("../icons/32x32.png")),
    ("128x128", include_bytes!("../icons/128x128.png")),
    ("256x256", include_bytes!("../icons/128x128@2x.png")),
];

fn write_entry(data_home: &Path, exec: &Path) -> std::io::Result<()> {
    let apps = data_home.join("applications");
    std::fs::create_dir_all(&apps)?;
    let entry = apps.join(format!("{NAME}.desktop"));
    let text = desktop_entry(exec);
    if std::fs::read_to_string(&entry).ok().as_deref() != Some(text.as_str()) {
        std::fs::write(&entry, text)?;
    }
    for (size, bytes) in ICONS {
        let dir = data_home.join("icons/hicolor").join(size).join("apps");
        std::fs::create_dir_all(&dir)?;
        let icon = dir.join(format!("{NAME}.png"));
        if std::fs::metadata(&icon).map(|m| m.len()).ok() != Some(bytes.len() as u64) {
            std::fs::write(&icon, bytes)?;
        }
        let _ = std::fs::remove_file(dir.join(format!("{OLD_NAME}.png")));
    }
    let _ = std::fs::remove_file(apps.join(format!("{OLD_NAME}.desktop")));
    // Menus that cache (some do) pick the entry up now rather than at the
    // next login. Best effort: not every desktop has the tool.
    let _ = std::process::Command::new("update-desktop-database")
        .arg(&apps)
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status();
    Ok(())
}

/// Move `from` to `to`: a rename where they share a filesystem, else a copy
/// and then the original removed.
fn move_file(from: &Path, to: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::PermissionsExt;
    if let Some(dir) = to.parent() {
        std::fs::create_dir_all(dir)?;
    }
    if std::fs::rename(from, to).is_err() {
        let tmp = to.with_extension("AppImage.part");
        std::fs::copy(from, &tmp)?;
        std::fs::rename(&tmp, to)?;
        let _ = std::fs::remove_file(from);
    }
    std::fs::set_permissions(to, std::fs::Permissions::from_mode(0o755))
}

/// `--integrate`: the menu entry and icons for the AppImage this is, moving
/// it into place first when it is elsewhere, and nothing else.
pub fn integrate_only() -> Result<(), String> {
    let home = std::env::var_os("HOME")
        .map(PathBuf::from)
        .ok_or("HOME is not set")?;
    let data = data_home(std::env::var("XDG_DATA_HOME").ok().as_deref(), &home);
    let installed = installed_path(&data);
    let running = std::env::var_os("APPIMAGE")
        .map(PathBuf::from)
        .ok_or("--integrate is for the AppImage; this copy is not one")?;
    if running != installed {
        move_file(&running, &installed).map_err(|e| format!("could not install: {e}"))?;
    }
    write_entry(&data, &installed).map_err(|e| format!("could not write the menu entry: {e}"))
}

/// Run at startup, before anything else. Returns the installed path when
/// the app should run again from there; any failure leaves it running from
/// where it is, which works, just without the menu entry.
pub fn install_appimage() -> Option<PathBuf> {
    let home = std::env::var_os("HOME").map(PathBuf::from)?;
    let data = data_home(std::env::var("XDG_DATA_HOME").ok().as_deref(), &home);
    let installed = installed_path(&data);
    let running = std::env::var_os("APPIMAGE").map(PathBuf::from);
    let opted_out = std::env::var("TEKTONIX_NO_INSTALL").is_ok_and(|v| v == "1");
    match step(running.as_deref(), &installed, opted_out) {
        Step::Nothing => None,
        Step::Entry => {
            let _ = write_entry(&data, &installed);
            None
        }
        Step::Move => {
            let from = running?;
            move_file(&from, &installed).ok()?;
            write_entry(&data, &installed).ok()?;
            Some(installed)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn it_installs_under_the_users_own_data_directory() {
        let home = Path::new("/home/danny");
        assert_eq!(data_home(None, home), Path::new("/home/danny/.local/share"));
        assert_eq!(data_home(Some("/srv/xdg"), home), Path::new("/srv/xdg"));
        assert_eq!(
            data_home(Some("relative"), home),
            Path::new("/home/danny/.local/share")
        );
        assert_eq!(
            installed_path(Path::new("/home/danny/.local/share")),
            Path::new("/home/danny/.local/share/io.tektonix.desktop/Tektonix.AppImage")
        );
    }

    #[test]
    fn a_download_moves_an_installed_copy_stays_and_a_deb_is_left_alone() {
        let installed = Path::new("/h/.local/share/io.tektonix.desktop/Tektonix.AppImage");
        let download = Path::new("/h/Downloads/Persei_0.9.2_amd64.AppImage");
        assert_eq!(step(Some(download), installed, false), Step::Move);
        assert_eq!(step(Some(installed), installed, false), Step::Entry);
        assert_eq!(
            step(None, installed, false),
            Step::Nothing,
            "a .deb install"
        );
        assert_eq!(
            step(Some(download), installed, true),
            Step::Nothing,
            "opted out"
        );
    }

    #[test]
    fn the_menu_entry_matches_the_debs_and_quotes_its_path() {
        let e = desktop_entry(Path::new("/home/o\"b $x/Tektonix.AppImage"));
        assert!(e.contains("Exec=\"/home/o\\\\\"b \\\\$x/Tektonix.AppImage\" %U\n"));
        assert_eq!(
            exec_arg(Path::new("/h/a\\b/100%/`c`")),
            r#""/h/a\\\\b/100%%/\\`c\\`""#,
            "a backslash is four, a percent sign two"
        );
        assert!(e.contains("StartupWMClass=persei\n") && e.contains("Icon=persei\n"));
        assert!(e.contains("Name=Persei\n"));
        assert!(e.contains("Categories=Development;\n"));
    }
}
