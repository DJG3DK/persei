# Persei desktop

The dashboard in a window, the compose stack under it, and a tray icon to
reach both. A [Tauri 2](https://tauri.app) shell with one window and two
pages: the window's own page (`src/`) is a small control panel, and the
dashboard, the agent's web app at `localhost:8100`, takes the same window
over once the stack answers (its title bar has a button back to the panel,
and so does the tray). Everything that does work is Rust (`src-tauri/src/`).

Windows (an NSIS installer) and Linux (an AppImage, which updates itself,
and a .deb). What it does that the console installer did not:

* installs WSL 2 and Docker Desktop when they are missing (Windows), through
  Windows' own permission prompt; on Linux, Docker Engine through Docker's
  install script, and the docker group for an account the daemon refuses,
  through polkit's password prompt (pkexec), and it runs the stack as your
  account (PUID/PGID) so the projects folder stays yours;
* asks for the settings in a form, keeps them in its own `.env`;
* pulls the release's images from GHCR instead of building from source, with
  Docker's progress in the window, and starts the stack with `--no-build`;
* sets the first account's password from the form at the first start (and
  can show the one-time password if it was never used);
* keeps the stack on the app's own release, updates the stack in one click
  when the operator asks for a newer one, and updates itself through Tauri's
  signed updater;
* shuts down when you quit: Quit (tray or panel) stops the stack's
  containers, and Docker Desktop too when the app started it. The window's
  close button asks once whether to quit or keep running in the tray, and
  the panel changes that later. An update's restart leaves the stack alone.

## Layout on the machine

Under the app's local data directory (`%LOCALAPPDATA%\io.tektonix.desktop`
on Windows, `~/.local/share/io.tektonix.desktop` on Linux; the identifier in
`tauri.conf.json`):

```
stack/docker-compose.yml           copied from the app on every launch
stack/docker/…                     (so an app update carries the stack's
stack/services/model-router/…       own files with it)
stack/.env                         the operator's: written by the settings form; the app
                                   adds TEKTONIX_DESKTOP and the SANDBOX_* defaults when
                                   they are missing and leaves everything else alone
stack/version.json                 which release's images are installed, the image id that
                                   proves it, and which compose file they were started under
prefs.json                         automatic updates, pre-releases, the page last shown
```

Images come from `ghcr.io/djg3dk/tektonix-<name>:<tag>` and are tagged with
the local names the compose file uses (`tektonix-<name>:latest`), so one
compose file serves both this app and a source checkout.

The updater endpoint in `tauri.conf.json` is the plugin's required
placeholder; at run time the app points the updater at the chosen release's
own `latest.json` (`stack.rs`, `updater_endpoint`), so a release candidate
can update to the next candidate.

## Building

The Windows installer is built by `.github/workflows/release.yml` on a
version tag, signed for the updater with the key in the repository's
secrets, and attached to the GitHub release with `latest.json`.

Locally, without a Rust toolchain on the machine, the backend type-checks
and its tests run in Tauri's own container:

```bash
docker run --rm -v "$PWD:/repo" -w /repo/app/src-tauri ivangabriele/tauri:debian-bookworm-20 \
  sh -c "cargo check && cargo test"
```

A full Windows build needs Windows (or the workflow). `npm ci && npm run
build` in `app/` does it there.
