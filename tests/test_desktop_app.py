"""The desktop app (app/) and the release pipeline that feeds it.

The app pulls released images rather than building from source, so three
lists must agree: the images the release workflow publishes, the local
names the compose file gives them, and the names the app's Rust backend
pulls and tags. And the stack files the app bundles must be the ones the
compose file references at runtime, or the stack starts without them."""
import json
import re

import yaml

from agent import paths

REPO = paths.REPO_ROOT
IMAGES = {"agent", "router", "reviewer", "sandbox", "launcher"}


def test_the_release_workflow_publishes_every_image_the_compose_file_names():
    wf = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())
    matrix = {row["image"] for row in wf["jobs"]["images"]["strategy"]["matrix"]["include"]}
    assert matrix == IMAGES
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    named = {svc.get("image") for svc in compose["services"].values() if "build" in svc}
    assert named == {f"tektonix-{n}:latest" for n in IMAGES}
    # packages: write belongs to the job that pushes, not to the workflow:
    # the installer job holds the updater's signing key and needs no
    # registry access.
    assert "packages" not in wf["permissions"]
    assert wf["jobs"]["images"]["permissions"]["packages"] == "write"
    assert "app-windows" in wf["jobs"] and "images" in wf["jobs"]["app-windows"]["needs"], "the installer is built after its images"


def test_images_are_published_once_by_digest_and_signed():
    """A re-run for an existing tag repushed its images under an immutable
    release, and every stable tag moved a :latest nothing consumed. Now the
    published check gates the images job, no :latest is pushed, and the
    digest each image was pushed as is signed with the updater's key and
    attached to the release before it goes live, for the app to pull by
    (update.rs verified_digests)."""
    wf = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())
    images, app = wf["jobs"]["images"], wf["jobs"]["app-windows"]
    assert set(images["needs"]) == {"gate", "check"} and "published" in images["if"]
    assert set(app["needs"]) == {"check", "images"} and "published" in app["if"]
    build = next(s for s in images["steps"] if s.get("uses", "").startswith("docker/build-push-action"))
    assert ":latest" not in build["with"]["tags"] and "\n" not in build["with"]["tags"].strip()
    assert build["id"] == "build" and any("steps.build.outputs.digest" in json.dumps(s) for s in images["steps"])
    names = [s.get("name", s.get("uses", "")) for s in app["steps"]]
    sign = next(i for i, n in enumerate(names) if n == "Write and sign the image manifest")
    attach = next(i for i, n in enumerate(names) if n == "Attach the image manifest")
    assert sign < attach, "signed, then attached"
    assert "Publish the release" not in names, "the publish job publishes, after both apps"
    publish = wf["jobs"]["publish"]
    # windows-upgrade installs the built installer over Tektonix first (D28).
    assert set(publish["needs"]) == {"app-windows", "app-linux", "windows-upgrade"}, "attached while the release is still a draft"
    assert "--draft=false" in publish["steps"][-1]["run"]
    assert "tauri signer sign" in app["steps"][sign]["run"] and "digests.json.sig" in app["steps"][attach]["run"]
    rust = (REPO / "app/src-tauri/src/stack.rs").read_text()
    assert "releases/download/{tag}/digests.json" in rust, "the app reads the manifest from the tag's own release"


def test_the_app_pulls_the_same_images_and_tags_them_with_the_compose_names():
    rust = (REPO / "app/src-tauri/src/stack.rs").read_text()
    m = re.search(r'pub const IMAGES: \[&str; \d\] = \[(.*?)\];', rust)
    assert m and set(re.findall(r'"([a-z]+)"', m.group(1))) == IMAGES
    assert 'pub const REGISTRY: &str = "ghcr.io/djg3dk";' in rust
    assert 'format!("tektonix-{name}:latest")' in rust


def test_the_app_bundles_every_stack_file_the_compose_file_binds():
    conf = json.loads((REPO / "app/src-tauri/tauri.conf.json").read_text())
    resources = conf["bundle"]["resources"]
    for src in resources:
        assert (REPO / "app/src-tauri" / src).exists(), f"{src} is bundled but missing"
    shipped = {v.removeprefix("stack/") for v in resources.values()}
    rust = (REPO / "app/src-tauri/src/stack.rs").read_text()
    m = re.search(r"const SHIPPED: \[&str; \d\] = \[(.*?)\];", rust, re.S)
    assert m and set(re.findall(r'"([^"]+)"', m.group(1))) == shipped, "stack.rs copies what tauri.conf.json bundles"
    compose_text = (REPO / "docker-compose.yml").read_text()
    for path in re.findall(r"- \./(docker/[\w./-]+\.sh):", compose_text):
        assert path in shipped, f"the compose file binds {path}; the app must ship it"
    assert "services/model-router/config.example.yaml" in shipped, "the router's seed"
    assert conf["plugins"]["updater"]["pubkey"] != "REPLACED_AT_KEY_GENERATION"
    assert conf["bundle"]["createUpdaterArtifacts"] is True


def test_the_app_checks_the_agents_password_rules_before_spending_the_one_time_password():
    """2026-09-29: the panel checked only the length. A long all-lowercase
    passphrase passed it, the agent refused it, and by then the one-time
    password had been read (which deletes it): no way in. The app and the
    panel now carry the agent's rules, in the agent's words, so a new rule
    in auth.py fails here until both copies know it."""
    from agent.auth import validate_password_strength

    tripping = ["Short1a", "correct horse battery staple", "CORRECT HORSE BATTERY 1", "Correct Horse Battery"]
    messages = {validate_password_strength(p) for p in tripping}
    assert None not in messages and len(messages) == 4, "one input per rule"
    assert validate_password_strength("Correct Horse Battery 1") is None
    rust = (REPO / "app/src-tauri/src/stack.rs").read_text()
    panel = (REPO / "app/src/main.js").read_text()
    for msg in messages:
        assert f'"{msg}"' in rust, f"stack.rs password_problem lacks: {msg}"
        assert f'"{msg}"' in panel, f"main.js passwordProblem lacks: {msg}"
    body = rust.split("pub async fn set_first_password", 1)[1]
    assert body.index("password_problem(password)") < body.index("initial_password(app)"), "the rules run before the one-time password is read"
    assert "with_one_time_password(" in body, "a failure after the read hands the one-time password back"


def _registered_commands() -> list[str]:
    lib = (REPO / "app/src-tauri/src/lib.rs").read_text()
    handler = re.search(r"invoke_handler\(tauri::generate_handler!\[(.*?)\]\)", lib, re.S)
    return re.findall(r"\w+", handler.group(1))


def test_the_console_origin_gets_window_controls_and_the_panel_switch_only():
    """One capability used to cover the panel and the console page at :8100
    alike, so model-rendered console text sat one CSP away from the updater,
    the process plugin and the opener. And with no app manifest, nothing
    could grant open_panel to the console: its "Control panel" button was
    silently refused (2026-09-29). Now: build.rs lists every command, the
    panel gets all of them, and the console gets exactly this list."""
    caps = {p.stem: json.loads(p.read_text()) for p in (REPO / "app/src-tauri/capabilities").glob("*.json")}
    assert set(caps) == {"panel", "console"}
    console, panel = caps["console"], caps["panel"]
    assert console["local"] is False
    assert set(console["remote"]["urls"]) == {"http://localhost:8100/*", "http://127.0.0.1:8100/*"}, "the origins stay"
    assert set(console["permissions"]) == {
        "core:window:allow-start-dragging",
        "core:window:allow-minimize",
        "core:window:allow-toggle-maximize",
        "core:window:allow-close",
        "core:window:allow-is-maximized",
        "allow-open-panel",
    }
    assert "remote" not in panel and panel.get("local", True) is True
    commands = _registered_commands()
    build = (REPO / "app/src-tauri/build.rs").read_text()
    assert set(re.findall(r'"(\w+)"', build)) == set(commands), "build.rs's manifest is lib.rs's handler list"
    assert {f"allow-{c.replace('_', '-')}" for c in commands} <= set(panel["permissions"]), "the panel keeps every command"


def test_the_projects_folder_opens_through_a_command_not_the_opener_plugin():
    """opener.openPath from the page needed opener:allow-open-path, which the
    capability never granted, so "Open projects folder" was denied
    (2026-09-29). A command opens the saved setting instead; no page gets to
    open an arbitrary path."""
    panel_js = (REPO / "app/src/main.js").read_text()
    assert "openPath" not in panel_js and 'invoke("open_projects_dir")' in panel_js
    assert "open_projects_dir" in _registered_commands()
    for cap in (REPO / "app/src-tauri/capabilities").glob("*.json"):
        assert "opener:allow-open-path" not in json.loads(cap.read_text())["permissions"]


def test_the_desktop_docs_describe_the_app_as_it_is():
    """One window since 3b3e365, a data path under the bundle identifier, a
    form that sets the first password, and an installer Windows users are
    pointed at (2026-09-29: all four were stale)."""
    app_readme = (REPO / "app/README.md").read_text()
    lib = (REPO / "app/src-tauri/src/lib.rs").read_text()
    stack = (REPO / "app/src-tauri/src/stack.rs").read_text()
    conf = json.loads((REPO / "app/src-tauri/tauri.conf.json").read_text())
    for text, where in ((app_readme, "app/README.md"), (lib, "lib.rs")):
        assert "second window" not in text, f"{where}: one window, two pages"
    assert "never overwritten" not in app_readme, "prepare adds the app's own lines to .env"
    for text, where in ((app_readme, "app/README.md"), (stack, "stack.rs")):
        assert f"%LOCALAPPDATA%\\{conf['identifier']}" in text, f"{where}: the data path is under the bundle identifier"
        assert "%LOCALAPPDATA%\\Persei`" not in text and "%LOCALAPPDATA%\\Persei\n" not in text, where
    assert "dashboard.json" not in (REPO / "frontend/src/components/TitleBar.tsx").read_text()
    for doc in ("README.md", "INSTALL.md"):
        assert "x64-setup.exe" in (REPO / doc).read_text(), f"{doc} points Windows users at the desktop installer"


def test_no_dead_commands_and_a_policy_on_the_panel_page():
    """stack_dir was never called, and auto_update_now could self-install
    from any page that reached it: unused privileged surface. The panel
    page builds its rows from text and carries a CSP; the release build
    sets up no emulator for an amd64-only image."""
    commands = _registered_commands()
    assert "stack_dir" not in commands and "auto_update_now" not in commands
    conf = json.loads((REPO / "app/src-tauri/tauri.conf.json").read_text())
    assert conf["app"]["security"]["csp"].startswith("default-src 'self'")
    assert "innerHTML" not in (REPO / "app/src/main.js").read_text()
    assert "setup-qemu" not in (REPO / ".github/workflows/release.yml").read_text()
    stack = (REPO / "app/src-tauri/src/stack.rs").read_text()
    assert stack.count("use tauri::Manager") == 0 and "use tauri::{AppHandle, Emitter, Manager};" in stack
    assert re.search(r"^(pub )?(async )?fn ", stack[stack.index("#[cfg(test)]\nmod tests"):], re.M) is None, "nothing after mod tests"


def test_the_app_has_one_tray_icon():
    """app.trayIcon in the config made Tauri build a tray of its own, with
    no menu and no handler, beside the one lib.rs builds (2026-09-29)."""
    conf = json.loads((REPO / "app/src-tauri/tauri.conf.json").read_text())
    assert "trayIcon" not in conf["app"]
    lib = (REPO / "app/src-tauri/src/lib.rs").read_text()
    assert lib.count("TrayIconBuilder::with_id(") == 1


def test_the_app_version_is_one_number_in_three_places():
    conf = json.loads((REPO / "app/src-tauri/tauri.conf.json").read_text())
    pkg = json.loads((REPO / "app/package.json").read_text())
    cargo = (REPO / "app/src-tauri/Cargo.toml").read_text()
    m = re.search(r'^version = "([^"]+)"', cargo, re.M)
    assert conf["version"] == pkg["version"] == m.group(1)



def test_code_signing_happens_inside_the_build_and_is_verified():
    """Authenticode through Azure Artifact Signing (2026-09-30). The sign
    command goes into the Tauri config before the build, so the updater
    signature is taken over the signed bytes; a configured signing that
    yields an unsigned installer fails the release."""
    wf = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())
    steps = wf["jobs"]["app-windows"]["steps"]
    names = [s.get("name") or s.get("uses", "") for s in steps]
    sign = names.index("Code signing, when configured")
    build = names.index("Build")
    verify = names.index("The installer is signed by the expected publisher")
    updater = names.index("Sign the installer for the updater")
    upload = names.index("Attach the installer and the update manifest to the draft")
    assert sign < build < verify < updater < upload
    assert "signCommand" in steps[sign]["run"] and "trusted-signing-cli" in steps[sign]["run"]
    assert steps[verify]["if"] == "steps.codesign.outputs.enabled == 'true'"
    assert "Get-AuthenticodeSignature" in steps[verify]["run"]
    env = steps[build]["env"]
    assert {"AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID"} <= set(env)



def test_the_published_installer_is_verified_from_outside_windows():
    """The file GitHub serves is checked after publishing, against the
    Microsoft root pinned in scripts/verify_windows_installer.sh."""
    wf = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())
    app, job = wf["jobs"]["app-windows"], wf["jobs"]["verify-published"]
    assert app["outputs"]["signed"] == "${{ steps.codesign.outputs.enabled }}"
    assert set(job["needs"]) == {"app-windows", "publish"} and "signed == 'true'" in job["if"]
    assert "verify_windows_installer.sh --release" in job["steps"][-1]["run"]
    script = (REPO / "scripts/verify_windows_installer.sh").read_text()
    assert "ROOT_SHA256=\"53:67:F2:0C" in script and "-TSA-CAfile" in script



def test_the_installer_carries_the_tektonix_branding():
    """The NSIS defaults (a blue sidebar with a computer, a globe in the
    title bar) until 2026-09-30. Tauri takes a 164x314 sidebar, a 150x57
    header and an .ico; NSIS reads only 24-bit BMPs for the two images."""
    from PIL import Image
    conf = json.loads((REPO / "app/src-tauri/tauri.conf.json").read_text())
    nsis = conf["bundle"]["windows"]["nsis"]
    base = REPO / "app/src-tauri"
    assert (base / nsis["installerIcon"]).is_file()
    for key, size in (("sidebarImage", (164, 314)), ("headerImage", (150, 57))):
        with Image.open(base / nsis[key]) as im:
            assert im.format == "BMP" and im.size == size and im.mode == "RGB", key


def test_the_linux_app_is_built_signed_and_joins_the_release_before_it_goes_public():
    """2026-10-04: an AppImage (self-updating) and a .deb on every release.
    The Linux job only builds; the publish job attaches its files to the
    Windows job's draft and adds the AppImage to latest.json, so the two
    jobs never race to create the release or to write the manifest."""
    wf = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())
    linux, publish = wf["jobs"]["app-linux"], wf["jobs"]["publish"]
    assert set(linux["needs"]) == {"check", "images"} and "published" in linux["if"]
    assert linux["environment"] == "release", "the updater key, behind the same reviewer"
    assert linux["permissions"] == {"contents": "read"}, "it writes nothing to the release itself"
    steps = [s.get("name", "") for s in linux["steps"]]
    build = linux["steps"][steps.index("Build")]
    assert build["run"].startswith("npx tauri build --bundles appimage,deb ")
    assert "TEKTONIX_RELEASE_TAG" in build["env"]
    assert steps.index("Build") < steps.index("Sign the AppImage for the updater") < steps.index("Collect the files")
    attach = next(s for s in publish["steps"] if s.get("name", "").startswith("Attach the Linux app"))
    assert "add_linux_to_updater_manifest.mjs" in attach["run"]
    assert attach["run"].index("gh release upload") < attach["run"].index("add_linux_to_updater_manifest")
    names = [s.get("name", "") for s in publish["steps"]]
    assert names.index(attach["name"]) < names.index("Publish the release")
    conf = json.loads((REPO / "app/src-tauri/tauri.conf.json").read_text())
    assert conf["bundle"]["createUpdaterArtifacts"] is True, "the AppImage's .sig"
    # The tray's libayatana-appindicator3-1 is added to the .deb by Tauri
    # itself (tray-icon feature); listing it again doubled it.
    assert "depends" not in conf["bundle"]["linux"]["deb"]


def test_a_windows_app_update_stops_the_log_follow_before_the_plugin_exits():
    """D30, 2026-10-22: on Windows the updater plugin starts the installer
    and calls process::exit, so RunEvent::Exit never comes and a
    `docker compose logs -f` child outlived the app. Its on_before_exit
    hook replaces the plugin's own, so that one (cleanup_before_exit) runs
    from ours."""
    src = (REPO / "app/src-tauri/src/stack.rs").read_text()
    body = src[src.index("fn updater(app: &AppHandle"):]
    body = body[: body.index("\n}\n")]
    hook = body[body.index(".on_before_exit("):]
    assert hook.index("crate::stop_follow_on_exit(") < hook.index(".cleanup_before_exit()")
    assert body.index(".on_before_exit(") < body.index(".build()")


def test_the_panel_says_the_docker_group_is_root_before_it_asks():
    """D32, 2026-10-22: the panel offered to add the account to the docker
    group without saying what that grants."""
    js = (REPO / "app/src/main.js").read_text()
    linux = js[js.index("  linux: {"):]
    for state in ("missing", "denied"):
        body = linux[linux.index(f"{state}: {{"):]
        body = body[: body.index("},")]
        assert "as good as root" in body, state
    assert "the docker group is root" in (REPO / "SECURITY.md").read_text()


def test_the_updater_key_is_never_in_a_build_and_signing_tools_are_pinned():
    """I24, 2026-10-22: the updater's minisign key was in the environment of
    the Tauri build, and so of every tool it runs: an unpinned
    trusted-signing-cli once per signed file on Windows, linuxdeploy
    downloaded at build time on Linux. Any of them could sign an update
    every installed copy accepts. The builds run without it; steps that
    run only `tauri signer sign` sign the finished files."""
    for path in (REPO / ".github/workflows").glob("*.yml"):
        wf = yaml.safe_load(path.read_text())
        for name, job in wf["jobs"].items():
            for step in job.get("steps", []):
                run, env = str(step.get("run", "")), step.get("env", {})
                for line in re.findall(r"cargo install .*", run):
                    assert re.search(r"--version \d+\.\d+\.\d+\b|--rev [0-9a-f]{40}\b", line), f"{path.name} {name}: {line}"
                if "tauri build" in run or "tauri-action" in step.get("uses", ""):
                    assert "TAURI_SIGNING_PRIVATE_KEY" not in env, f"{path.name} {name}"
                    assert '"createUpdaterArtifacts":false' in run, "or the build demands the key"
                if "TAURI_SIGNING_PRIVATE_KEY" in env:
                    assert "tauri signer sign" in run, f"{path.name} {name}: {step.get('name')}"
    wf = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())
    for job in ("app-windows", "app-linux"):
        signs = [s.get("name") for s in wf["jobs"][job]["steps"] if "TAURI_SIGNING_PRIVATE_KEY" in s.get("env", {})]
        assert signs, f"{job} signs nothing for the updater"


def test_ci_builds_the_app_in_a_container_pinned_by_digest():
    wf = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    assert re.fullmatch(r"ivangabriele/tauri:[\w.-]+@sha256:[0-9a-f]{64}", wf["jobs"]["app"]["container"])
    runs = [s.get("run", "") for s in wf["jobs"]["app"]["steps"]]
    assert "cargo test --locked" in runs and any(r.startswith("cargo clippy --locked") for r in runs)
