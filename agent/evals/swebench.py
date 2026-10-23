"""SWE-bench Verified, run through the real Persei pipeline and graded by the
official harness.

What makes the number a SWE-bench number, and so what this must not change:

* The tasks are the published dataset, SWE-bench/SWE-bench_Verified, as
  published: the agent gets the issue text (`problem_statement`) and the
  repository at the task's commit. Not the hints, not the tests that grade it.
* The environment is the task's own official image
  (`swebench/sweb.eval.x86_64.<id>`), with the code where the harness has it,
  /testbed, and its conda env active. The repository is copied OUT of that
  image rather than cloned, because several (astropy, scikit-learn) are built
  in place there, and because the image's history already ends at the task:
  the published fix is not in it.
* One attempt per task (pass@1). Each task is its own project, so nothing the
  agent learns or remembers on one reaches another.
* No internet: the published fix is on GitHub. A benchmark project has no
  GitHub, web or cross-project tools, and its sandbox has no network.
* Graded by `swebench.harness.run_evaluation`, never by us.

What is Persei's own and is part of the system being measured: the agent,
its gate (`checks` below: the changed files compile, the package imports), the
independent reviewer, and how the final diff becomes a prediction
(`prediction_patch`).
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import time
from pathlib import Path

from agent.harness_voice import HARNESS

# The dataset's current official home. The older princeton-nlp copy has the
# same 500 tasks, issue text, commits and patches; this one adds the `image`
# field the harness now requires, and drops six unreliable PASS_TO_PASS tests
# from two tasks (checked 2026-09-24).
DATASET = "SWE-bench/SWE-bench_Verified"
# Exported once from the dataset by the official package's own environment.
DATASET_JSON = Path(os.environ.get("SWEBENCH_DATASET_JSON", "/opt/swebench/verified.json"))
HARNESS_PYTHON = os.environ.get("SWEBENCH_PYTHON", "/opt/swebench/venv/bin/python")
MODEL_NAME = "tektonix"
GRADE_ATTEMPTS = 4
GRADE_RETRY_S = 15
CONDA_INIT = "source /opt/miniconda3/bin/activate testbed"

# The package each repository installs, for the gate's import check.
IMPORT_NAME = {
    "astropy/astropy": "astropy", "django/django": "django", "matplotlib/matplotlib": "matplotlib",
    "mwaskom/seaborn": "seaborn", "pallets/flask": "flask", "psf/requests": "requests",
    "pydata/xarray": "xarray", "pylint-dev/pylint": "pylint", "pytest-dev/pytest": "pytest",
    "scikit-learn/scikit-learn": "sklearn", "sphinx-doc/sphinx": "sphinx", "sympy/sympy": "sympy",
}


class SetupError(RuntimeError):
    pass


def _run(args: list[str], cwd: Path | None = None, timeout: int = 600) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    r = subprocess.run(args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                       timeout=timeout, env=env)
    if r.returncode != 0:
        raise SetupError(f"{' '.join(args[:4])} failed: {(r.stderr or r.stdout).strip()[:600]}")
    return r.stdout


# --- the tasks ------------------------------------------------------------------

def load_instances(path: Path = DATASET_JSON) -> list[dict]:
    if not path.is_file():
        raise SetupError(f"{path} is missing -- export the dataset first (evals/SWEBENCH.md)")
    return json.loads(path.read_text())


def select(instances: list[dict], ids: list[str] | None = None, sample: int | None = None,
           seed: int = 0) -> list[dict]:
    """By id, or a seeded random sample, or everything -- in dataset order."""
    if ids:
        by_id = {i["instance_id"]: i for i in instances}
        unknown = [i for i in ids if i not in by_id]
        if unknown:
            raise SetupError(f"not in {DATASET}: {', '.join(unknown)}")
        return [by_id[i] for i in ids]
    if sample:
        chosen = set(i["instance_id"] for i in random.Random(seed).sample(instances, sample))
        return [i for i in instances if i["instance_id"] in chosen]
    return list(instances)


def image_name(instance_id: str) -> str:
    """The harness's own name for a task's image (swebench.task.checks.expected_image)."""
    return f"swebench/sweb.eval.x86_64.{instance_id}:latest".lower().replace("__", "_1776_")


def project_name(instance_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", instance_id)


def goal_for(instance: dict) -> str:
    """The issue, and only the issue. The framing is ours and the same for every task."""
    return (
        f"{HARNESS} Resolve the following GitHub issue in the {instance['repo']} repository, which is "
        f"checked out in your workspace. Change the library's source code so the problem the "
        f"issue describes is fixed. Do not modify the existing tests; you may add new ones. "
        f"You have no internet access. The fix does not exist anywhere in this environment: the "
        f"repository has no commits after this point, there is no newer package, and the tests that "
        f"will grade the fix are not here. Do not search for them; work from the issue and the code. "
        f"You may recognise this issue and its upstream fix from memory. Do not reproduce a remembered "
        f"patch: work from the issue and the code, verify by running, and never replace a change you "
        f"have verified with a remembered one. To run the code as it was before your change use "
        f"/baseline; for an editable install run test commands there with PYTHONPATH=/baseline (the "
        f"shell sets it for you when the command starts with cd /baseline), otherwise the package "
        f"still imports from /workspace.\n\n"
        f"<issue>\n{instance['problem_statement'].strip()}\n</issue>"
    )


# --- the repository, out of its official image ----------------------------------

def ensure_image(instance_id: str, image: str | None = None) -> str:
    image = image or image_name(instance_id)
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
    if probe.returncode != 0:
        _run(["docker", "pull", "-q", image], timeout=1800)
    return image


_JUNK = re.compile(r"(^|/)(__pycache__|\.pytest_cache|\.mypy_cache|\.tox)(/|$)|\.py[co]$")
# A file the agent CREATED that is its own scratch, not part of the fix: a
# hidden file (`.probe_out.txt`), anything at the top of the repository
# (`debug_m2m.py` -- a fix lives in the package, not beside setup.py), and
# probe/repro scripts anywhere. 2026-09-24: one prediction carried 870 KB of
# these alongside a 2 KB fix.
_SCRATCH = re.compile(r"(^|/)\.|^[^/]+$|(^|/)(probe|repro|reproduce|debug|scratch|tmp)[^/]*$|_tmp\.[a-z]+$")


def materialize(instance: dict, root: Path) -> dict:
    """<root>/live/<p>: the image's /testbed, a git repo at the task's commit.
    <root>/sandbox/<p>: the project workspace, a worktree of it carrying the
    same untracked build output (compiled extensions, egg-info), which task
    workspaces are filled from."""
    name = project_name(instance["instance_id"])
    image = ensure_image(instance["instance_id"], instance.get("image"))
    live, sandbox = root / "live" / name, root / "sandbox" / name
    for p in (live, sandbox):
        if p.exists():
            shutil.rmtree(p)
    live.parent.mkdir(parents=True, exist_ok=True)
    sandbox.parent.mkdir(parents=True, exist_ok=True)
    cid = _run(["docker", "create", image]).strip()
    try:
        _run(["docker", "cp", f"{cid}:/testbed", str(live)], timeout=1800)
    finally:
        subprocess.run(["docker", "rm", "-f", cid], capture_output=True)
    if not (live / ".git").is_dir():
        raise SetupError(f"{image}: /testbed is not a git repository")
    head = _run(["git", "rev-parse", "HEAD"], live).strip()
    # Nothing from after the task may be reachable: the fix is in it.
    # Checked, not assumed.
    future = future_commits(live, instance.get("base_commit") or f"{head}~1")
    if future:
        raise SetupError(f"{image}: {len(future)} commit(s) from after the task are reachable "
                         f"(first: {future[0][:12]}) -- refusing to run")
    for key, value in (("user.name", "Persei"), ("user.email", "agent@persei.local")):
        _run(["git", "config", key, value], live)
    if _run(["git", "branch", "--show-current"], live).strip() != "main":
        _run(["git", "branch", "-f", "main", head], live)
    _run(["git", "worktree", "add", "-q", "--detach", str(sandbox), head], live)
    # What the image built in place, into the project workspace. Some of it
    # is untracked but not ignored (requests leaves a build/), and the commit
    # gate refuses build output -- so it is excluded locally, in .git/info,
    # which every worktree shares and no diff contains.
    status = _run(["git", "status", "--porcelain", "--ignored", "--untracked-files=normal"], live)
    untracked = [ln[3:].rstrip("/") for ln in status.splitlines() if ln.startswith("?? ")]
    if untracked:
        with (live / ".git" / "info" / "exclude").open("a") as fh:
            fh.write("\n# present in the SWE-bench image before the task began\n")
            fh.writelines(f"/{rel}\n" for rel in untracked)
    for line in status.splitlines():
        if line[:3] not in ("?? ", "!! "):
            continue
        rel = line[3:].rstrip("/")
        if _JUNK.search(rel):
            continue
        src, dst = live / rel, sandbox / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        _run(["cp", "-a", str(src), str(dst)])
    return {"name": name, "live": live, "sandbox": sandbox, "image": image, "base": head}


def future_commits(repo: Path, base: str) -> list[str]:
    """Commits reachable in `repo` that come after the task: descendants of
    its base commit other than HEAD (the image's own empty "SWE-bench"
    commit), or anything committed later than the base.

    Not simply "anything beyond HEAD": several images carry old release tags
    on maintenance branches (matplotlib's reach back to 0.91), which are
    history the task's authors had, not its future. A tag on a LATER release
    is a descendant of the base, and is caught."""
    base_sha = _run(["git", "rev-parse", f"{base}^{{commit}}"], repo).strip()
    base_time = int(_run(["git", "log", "-1", "--format=%ct", base_sha], repo).strip())
    head = _run(["git", "rev-parse", "HEAD"], repo).strip()
    found: list[str] = []
    # Descendants of the base, from every ref, except HEAD itself.
    for sha in _run(["git", "rev-list", "--all", "--ancestry-path", f"^{base_sha}"], repo).split():
        if sha != head:
            found.append(sha)
    # Anything else newer than the base, however it is connected.
    for line in _run(["git", "rev-list", "--all", "--not", "HEAD", "--format=%H %ct",
                      "--no-commit-header"], repo).splitlines():
        sha, _, ts = line.partition(" ")
        if ts.strip().isdigit() and int(ts) > base_time and sha not in found:
            found.append(sha)
    return found


def checks_for(instance: dict, base: str) -> list[dict]:
    """The gate: every Python file the change touches compiles, and the
    package still imports. Deliberately light -- the tests that grade the
    task are hidden, and a repository's full suite (Django's) is hours."""
    pkg = IMPORT_NAME.get(instance["repo"], "")
    compile_changed = (f"git --no-optional-locks diff --name-only --diff-filter=AMR {base} -- '*.py' "
                       f"| xargs -r python -m py_compile")
    return [{"name": "compile", "cmd": compile_changed, "timeout_s": 300},
            *([{"name": "import", "cmd": f"python -c 'import {pkg}'", "timeout_s": 300}] if pkg else [])]


def project_entry(instance: dict, mf: dict) -> dict:
    return {
        "live": str(mf["live"]), "sandbox": str(mf["sandbox"]),
        "sandbox_image": mf["image"], "sandbox_mounts": ["/testbed"],
        "sandbox_shell_init": CONDA_INIT, "sandbox_network": "none",
        # The untouched tree, read-only: how the agent runs the code as it was
        # before its change. It had been trying git stash, which the
        # read-only .git refuses, in loops (2026-09-24).
        "sandbox_readonly_mounts": {"/baseline": str(mf["sandbox"])},
        "benchmark": True,
        "checks": checks_for(instance, mf["base"]),
        # The reviewer reviews the diff; it runs no checks of its own here --
        # its sandbox is not the task's image.
        "review": {"checks": []},
    }


# --- the prediction ----------------------------------------------------------------

_TEST_PATH = re.compile(r"(^|/)(tests?|testing)(/|$)|(^|/)test_[^/]*\.py$|_tests?\.py$|(^|/)conftest\.py$")


def is_test_path(path: str) -> bool:
    return bool(_TEST_PATH.search(path))


def prediction_patch(workspace: Path, base: str, live: Path) -> str:
    """What the agent changed, as a patch the harness applies to /testbed.

    Committed and uncommitted work, and files it created -- but not tests:
    the harness applies the task's own test patch after this one, and a
    prediction touching the same test file makes that fail to apply. And
    not build output or caches, which were never the agent's."""
    created = []
    for line in _run(["git", "status", "--porcelain", "--untracked-files=all"], workspace).splitlines():
        if line.startswith("?? "):
            rel = line[3:]
            if not _JUNK.search(rel) and not _SCRATCH.search(rel) and not (live / rel).exists():
                created.append(rel)
    if created:
        _run(["git", "add", "-N", "--", *created], workspace)
    names = _run(["git", "diff", "--name-only", base], workspace).split("\n")
    keep = [n for n in names if n and not is_test_path(n) and not _JUNK.search(n)]
    if not keep:
        return ""
    return _run(["git", "diff", "--no-color", base, "--", *keep], workspace)


# --- grading -----------------------------------------------------------------------

def grade(predictions: Path, instance_ids: list[str], run_id: str, report_dir: Path,
          max_workers: int = 4, log=print, predictions_arg: str | None = None, rewrite: bool = False) -> dict:
    """The official harness, in its own environment. Returns its report.
    `predictions_arg="gold"` grades the dataset's reference fixes instead."""
    report_dir = Path(report_dir).resolve()   # it runs from report_dir
    pred = predictions_arg or str(Path(predictions).resolve())
    cmd = [HARNESS_PYTHON, "-m", "swebench.harness.run_evaluation",
           "--dataset_name", DATASET, "--predictions_path", pred,
           "--instance_ids", *instance_ids, "--max_workers", str(max_workers),
           "--run_id", run_id, "--report_dir", str(report_dir),
           # Re-reads the harness's own logs of this run_id -- every batch --
           # into one report, without running anything again.
           *(["--rewrite_reports", "true"] if rewrite else [])]
    report = report_dir / f"{'gold' if predictions_arg == 'gold' else MODEL_NAME}.{run_id}.json"
    for attempt in range(1, GRADE_ATTEMPTS + 1):
        log("grading: " + " ".join(cmd[:3]) + " ..." + (f" (attempt {attempt})" if attempt > 1 else ""))
        r = subprocess.run(cmd, cwd=str(report_dir), capture_output=True, text=True, timeout=6 * 3600)
        (report_dir / "grading.log").write_text(r.stdout + "\n" + r.stderr)
        if report.is_file():
            return json.loads(report.read_text())
        # The harness's report step lists every image on the host and then
        # fetches each by id; another run pulling or deleting images in
        # between makes one vanish (2026-09-25, two shards side by side).
        # Repeating is safe: tasks already graded are skipped, and only the
        # report is rebuilt.
        if "ImageNotFound" not in (r.stdout + r.stderr) or attempt == GRADE_ATTEMPTS:
            break
        time.sleep(GRADE_RETRY_S)
    raise SetupError(f"the harness wrote no report (exit {r.returncode}); see {report_dir / 'grading.log'}")
