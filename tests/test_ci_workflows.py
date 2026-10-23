"""The two workflows, read as the claims they make.

A CI step that cannot fail is a step that lies, and the 2026-09-29 audit
found three: a bundle job whose service comparison counted the one-shot
image build as a crash (so the job never passed and nobody ran it), a
doctor step whose `grep -qv Traceback` passed on every traceback, and two
docker-backed suites that skipped on every run because nothing built the
image they wait for. These read the workflow files the way the runner
would, and run the one shell step whose logic was wrong.
"""
from __future__ import annotations

import os
import pathlib
import re
import stat
import subprocess

import pytest
import yaml

from agent import paths

REPO = paths.REPO_ROOT
CI = REPO / ".github/workflows/ci.yml"
RELEASE = REPO / ".github/workflows/release.yml"
WINDOWS_UPGRADE = REPO / ".github/workflows/windows-upgrade.yml"


def _jobs(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())["jobs"]


def _step(job: dict, name_part: str) -> dict:
    steps = [s for s in job["steps"] if name_part in (s.get("name") or "")]
    assert len(steps) == 1, f"{name_part!r}: {len(steps)} steps match"
    return steps[0]


# ---------------------------------------------------------------------------
# The docker jobs run on pushes, and the bundle job can pass
# ---------------------------------------------------------------------------

def test_the_bundle_and_sandbox_jobs_run_on_a_push_to_main_that_touches_the_bundle():
    jobs = _jobs(CI)
    gate = jobs["bundle-paths"]
    assert "push" in gate["if"] and "refs/heads/main" in gate["if"] and "workflow_dispatch" in gate["if"]
    decide = gate["steps"][-1]["run"]
    for path in ("docker", "docker-compose.yml", "agent"):
        assert re.search(rf"--[^\n]*\b{re.escape(path)}\b", decide), f"{path} does not trigger the docker jobs"
    for name in ("bundle", "sandbox"):
        assert jobs[name]["needs"] == "bundle-paths", name
        assert "bundle-paths.outputs.run" in jobs[name]["if"], name


def test_the_bundle_job_expects_a_one_shot_to_have_exited_rather_than_to_be_running():
    """sandbox-image has entrypoint true and restart "no": it exits at once
    by design. Diffing `config --services` against the running services
    listed it as missing on every run, so the job failed by construction."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    one_shots = {n for n, s in compose["services"].items() if str(s.get("restart")) == "no"}
    assert one_shots, "no one-shot service; this test guards the comparison against one"
    run = _step(_jobs(CI)["bundle"], "still running")["run"]
    assert "config --services" not in run, "the comparison counts one-shots as crashed again"
    assert 'restart != "no"' in run and 'restart == "no"' in run
    assert "exited 0" in run


def test_the_sandbox_job_builds_the_image_the_docker_suites_wait_for():
    job = _jobs(CI)["sandbox"]
    runs = "\n".join(s.get("run") or "" for s in job["steps"])
    assert "docker build -t tektonix-sandbox:latest docker/agent-sandbox" in runs
    for suite in ("tests/test_review_db_check_e2e.py", "tests/test_sandbox_gh.py"):
        assert suite in runs, suite
        assert (REPO / suite).exists(), suite
    assert "skipped" in runs, "a skipped suite must fail the job, or the gap is back"


# ---------------------------------------------------------------------------
# The doctor step, run for real
# ---------------------------------------------------------------------------

def _doctor_step_script() -> str:
    return _step(_jobs(CI)["shell"], "Doctor runs")["run"]


def _run_doctor_step(tmp_path: pathlib.Path, fake_doctor: str) -> int:
    """The step's shell, under the runner's own flags (bash -eo pipefail),
    with `python3` replaced by a stub that behaves like a given doctor."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "python3"
    stub.write_text("#!/usr/bin/env bash\n" + fake_doctor + "\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    script = _doctor_step_script().replace("/tmp/doctor.out", str(tmp_path / "doctor.out"))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return subprocess.run(["bash", "-eo", "pipefail", "-c", script], env=env, cwd=REPO,
                          capture_output=True, text=True).returncode


def test_the_doctor_step_passes_on_findings_and_fails_on_a_traceback(tmp_path):
    """`| grep -qv Traceback` succeeds whenever ANY output line lacks the
    word, which is true of every traceback. The step was green on a doctor
    that crashed. Now: findings (exit 1) pass, a traceback fails, and so
    does any exit code the doctor does not document."""
    assert "grep -qv" not in _doctor_step_script()
    findings = "echo '2 ok, 1 failure(s)'; exit 1"
    clean = "echo 'all ok'; exit 0"
    crash = "echo 'Traceback (most recent call last):' >&2; echo 'RuntimeError: x' >&2; exit 1"
    odd_exit = "echo 'usage'; exit 2"
    assert _run_doctor_step(tmp_path / "a", findings) == 0
    assert _run_doctor_step(tmp_path / "b", clean) == 0
    assert _run_doctor_step(tmp_path / "c", crash) != 0
    assert _run_doctor_step(tmp_path / "d", odd_exit) != 0


# ---------------------------------------------------------------------------
# The release workflow: pinned actions, a gated build, a validated tag
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workflow", [RELEASE, CI, WINDOWS_UPGRADE])
def test_every_action_is_pinned_to_a_commit_with_its_release_beside_it(workflow):
    """release.yml holds the updater's signing key, and auto-update is on
    by default: whoever could move a tag like `v7` on an action used there
    could sign an update for every desktop install. A commit cannot be
    moved. ci.yml is pinned the same way so the two never drift apart. A
    workflow of this repository (./.github/workflows/...) is the commit
    being run, so it needs no pin."""
    uses = re.findall(r"^\s*-?\s*uses:\s*(\S+)(.*)$", workflow.read_text(), re.M)
    assert uses, workflow
    for ref, rest in uses:
        if ref.startswith("./.github/workflows/"):
            assert (REPO / ref).is_file(), ref
            continue
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", ref), f"{workflow.name}: {ref} is not pinned to a commit"
        assert re.search(r"#\s*v\d", rest), f"{workflow.name}: {ref} has no release comment for Dependabot"


def test_the_signing_job_holds_no_registry_permission_and_names_the_protected_environment():
    wf = yaml.safe_load(RELEASE.read_text())
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["images"]["permissions"]["packages"] == "write"
    app = wf["jobs"]["app-windows"]
    assert "packages" not in (app.get("permissions") or {})
    assert app["environment"] == "release"


def _gate_script() -> str:
    return _step(_jobs(RELEASE)["gate"], "CI passed")["run"]


def _run_gate(tmp_path: pathlib.Path, tag: str, *, on_main: bool = True, green: int = 1) -> subprocess.CompletedProcess:
    """The gate step with git and gh stubbed: git says whether the tag's
    commit is on main, gh says how many green CI runs the commit has."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    git = bin_dir / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        'case "$1" in\n'
        '  rev-parse) echo 0123456789abcdef0123456789abcdef01234567 ;;\n'
        '  fetch) ;;\n'
        f'  merge-base) exit {0 if on_main else 1} ;;\n'
        '  *) echo "unexpected git $*" >&2; exit 99 ;;\n'
        "esac\n")
    gh = bin_dir / "gh"
    gh.write_text(f"#!/usr/bin/env bash\necho {green}\n")
    for f in (git, gh):
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "TAG": tag,
           "GH_TOKEN": "x", "GITHUB_REPOSITORY": "o/r"}
    return subprocess.run(["bash", "-eo", "pipefail", "-c", _gate_script()], env=env, cwd=REPO,
                          capture_output=True, text=True)


@pytest.mark.parametrize("tag", ["v1.2.3", "v0.9.0-rc13", "v10.0.0-beta.2"])
def test_the_gate_passes_a_version_tag_on_main_with_a_green_run(tmp_path, tag):
    proc = _run_gate(tmp_path, tag)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("tag", ["v1.2", "1.2.3", "v1.2.3;rm -rf /", "v1.2.3 x", "vlatest", "v1.2.3-", "main"])
def test_the_gate_refuses_a_tag_that_is_not_a_version(tmp_path, tag):
    """The tag used to go straight into a sed on Cargo.toml."""
    proc = _run_gate(tmp_path, tag)
    assert proc.returncode != 0 and "not a version tag" in proc.stderr, tag


def test_the_gate_refuses_a_tag_off_main_or_without_a_green_ci_run(tmp_path):
    off_main = _run_gate(tmp_path / "a", "v1.2.3", on_main=False)
    assert off_main.returncode != 0 and "not on main" in off_main.stderr
    red = _run_gate(tmp_path / "b", "v1.2.3", green=0)
    assert red.returncode != 0 and "no successful CI run" in red.stderr


def test_nothing_builds_or_signs_before_the_gate():
    jobs = _jobs(RELEASE)
    assert "gate" in jobs["images"]["needs"]
    assert "images" in jobs["app-windows"]["needs"]


def test_the_release_is_published_only_after_the_windows_upgrade_test_passed():
    """D28 shipped because only a compile of the hooks covered them. The
    upgrade job installs the built installer over the published Tektonix
    0.9.3 on a real Windows, and publish waits for it."""
    jobs = _jobs(RELEASE)
    upload = [s for s in jobs["app-windows"]["steps"] if "upload-artifact" in (s.get("uses") or "")]
    assert len(upload) == 1 and upload[0]["with"]["name"] == "windows-installer"
    upgrade = jobs["windows-upgrade"]
    assert upgrade["uses"] == "./.github/workflows/windows-upgrade.yml"
    assert upgrade["with"]["installer-artifact"] == "windows-installer"
    assert "app-windows" in upgrade["needs"]
    assert "windows-upgrade" in jobs["publish"]["needs"]


def test_the_windows_upgrade_test_reads_no_secret_and_checks_what_the_user_sees():
    """It runs on pull requests too, which never see the signing secrets,
    and it executes a downloaded installer, so that one is pinned by digest."""
    text = WINDOWS_UPGRADE.read_text()
    assert "${{ secrets." not in text
    wf = yaml.safe_load(text)
    on = wf[True]  # YAML 1.1 reads the bare key `on` as a boolean
    assert {"workflow_call", "pull_request"} <= set(on)
    assert "app/src-tauri/windows/**" in on["pull_request"]["paths"]
    job = wf["jobs"]["upgrade"]
    modes = {m["mode"]: m["args"] for m in job["strategy"]["matrix"]["include"]}
    assert modes == {"update": "/S /UPDATE", "install": "/S"}
    fetch = _step(job, "Tektonix 0.9.3 installer")["run"]
    assert re.search(r"'[0-9a-f]{64}'", fetch), "the old installer is not pinned by digest"
    check = _step(job, "What the user sees")["run"]
    for claim in ("Persei\\persei.exe", "'Programs'", "'Desktop'", "Persei.lnk", "Tektonix.lnk",
                  "$uninstall\\Tektonix", "$uninstall\\Persei"):
        assert claim in check, claim


# ---------------------------------------------------------------------------
# Dependabot sees every manifest, and the router's requirements are pinned
# ---------------------------------------------------------------------------

def test_dependabot_watches_every_manifest_in_the_tree():
    """Three manifests had no entry (the review dashboard's npm, the router's
    pip, the images' base tags), so the router's requirements floated and
    nothing watched them. Fixtures under evals/ are test data, not ours to
    update; services/logoloom is vendored."""
    doc = yaml.safe_load((REPO / ".github/dependabot.yml").read_text())
    watched: set[tuple[str, str]] = set()
    for entry in doc["updates"]:
        for d in entry.get("directories") or [entry["directory"]]:
            watched.add((entry["package-ecosystem"], d))
    # Tracked files only: a development box also holds agent worktrees under
    # .claude, review worktrees under the reviewer, and installed packages,
    # each a manifest Dependabot has no business watching.
    tracked = subprocess.run(["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, check=True).stdout
    skip = {"evals", "logoloom"}
    expected: set[tuple[str, str]] = set()
    for raw in tracked.split(b"\0"):
        if not raw:
            continue
        p = REPO / raw.decode()
        if any(part in skip for part in p.relative_to(REPO).parts):
            continue
        rel = "/" if p.parent == REPO else "/" + p.parent.relative_to(REPO).as_posix()
        if p.name == "package.json":
            expected.add(("npm", rel))
        elif p.name == "requirements.txt":
            expected.add(("pip", rel))
        elif p.name == "Cargo.toml":
            expected.add(("cargo", rel))
        elif p.name == "Dockerfile":
            expected.add(("docker", rel))
    missing = set()
    for eco, d in expected:
        if (eco, d) in watched:
            continue
        # a glob entry like /docker/* covers /docker/agent
        if any(w_eco == eco and w_dir.endswith("/*") and d.startswith(w_dir[:-1]) for w_eco, w_dir in watched):
            continue
        missing.add((eco, d))
    assert not missing, f"manifests Dependabot does not watch: {sorted(missing)}"
    assert ("docker-compose", "/") in watched, "the compose file's postgres and redis tags float otherwise"


def test_the_router_s_requirements_are_pinned_and_ci_installs_them_in_their_own_venv():
    reqs = [ln.strip() for ln in (REPO / "services/model-router/requirements.txt").read_text().splitlines()
            if ln.strip() and not ln.startswith("#")]
    assert reqs and all("==" in r for r in reqs), reqs
    step = _step(_jobs(CI)["python"], "model router, its own requirements")["run"]
    assert "services/model-router/.venv/bin/pip install" in step and "services/model-router/requirements.txt" in step
    assert "services/model-router/.venv/bin/python -m pytest -q services/model-router/tests" in step
    assert "pip install ruff\n" not in "\n".join(_lint_lines()), "ruff comes pinned from requirements.txt"


def _lint_lines() -> list[str]:
    return [s.get("run") or "" for s in _jobs(CI)["python"]["steps"]]


def test_the_real_doctor_survives_the_step_on_this_tree(tmp_path):
    """The step against the real script: whatever this checkout is missing,
    the answer is findings, never a traceback."""
    script = _doctor_step_script().replace("/tmp/doctor.out", str(tmp_path / "doctor.out"))
    proc = subprocess.run(["bash", "-eo", "pipefail", "-c", script], cwd=REPO, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_no_workflow_repeats_a_key():
    """2026-09-29: a merge left `needs:` twice in one job. yaml.safe_load
    keeps the last value and says nothing; GitHub refuses the file, and
    every push then shows a release run that failed at start-up."""
    class _Strict(yaml.SafeLoader):
        pass

    def _mapping(loader, node):
        seen = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node)
            assert key not in seen, f"{key!r} repeated at line {key_node.start_mark.line + 1}"
            seen.add(key)
        return loader.construct_mapping(node, deep=True)

    _Strict.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)
    for wf in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        yaml.load(wf.read_text(), Loader=_Strict)
