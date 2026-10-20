"""SWE-bench, run exactly as SWE-bench (agent/evals/swebench.py, evals/SWEBENCH.md).

The rules that make a result advertisable as a SWE-bench result are pinned here:
the official image name, the issue and nothing else in the goal, no network and
no outside tools in a benchmark project, and a prediction that is the agent's
source change -- not its tests, not the image's build output.
"""
import os
import subprocess

import pytest

from agent.config import PROJECTS
from agent.evals import swebench as sb


def test_the_image_is_the_harness_s_own_name_for_the_task():
    assert sb.image_name("psf__requests-1142") == "swebench/sweb.eval.x86_64.psf_1776_requests-1142:latest"
    assert sb.image_name("Django__Django-10097").islower()


def test_the_goal_is_the_issue_and_nothing_that_grades_it():
    inst = {"repo": "psf/requests", "problem_statement": "  Something is broken.\n",
            "hints_text": "THE HINT", "FAIL_TO_PASS": '["tests/test_x.py::test_secret"]',
            "patch": "THE FIX", "test_patch": "THE TESTS"}
    goal = sb.goal_for(inst)
    assert "<issue>\nSomething is broken.\n</issue>" in goal
    for leak in ("THE HINT", "test_secret", "THE FIX", "THE TESTS"):
        assert leak not in goal


def test_a_sample_is_seeded_and_ids_must_exist():
    rows = [{"instance_id": f"r__r-{i}"} for i in range(40)]
    assert sb.select(rows, sample=5, seed=3) == sb.select(rows, sample=5, seed=3)
    assert sb.select(rows, sample=5, seed=3) != sb.select(rows, sample=5, seed=4)
    with pytest.raises(sb.SetupError):
        sb.select(rows, ids=["nope__nope-1"])


@pytest.mark.parametrize("path,is_test", [
    ("tests/test_models.py", True), ("testing/python/raises.py", True), ("requests/test_utils.py", True),
    ("sklearn/utils/_testing.py", False), ("django/test/client.py", True), ("conftest.py", True),
    ("astropy/io/fits/card.py", False), ("src/_pytest/python.py", False), ("sympy/core/basic.py", False),
])
def test_what_counts_as_a_test_file(path, is_test):
    assert sb.is_test_path(path) is is_test


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True).stdout


def test_the_prediction_is_the_source_change_only(tmp_path, monkeypatch):
    """Not test files (the harness applies the task's own test patch after it,
    and a clash fails the task), not the image's build output, not caches."""
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null"}.items():
        monkeypatch.setenv(k, v)
    live = tmp_path / "live"
    (live / "pkg").mkdir(parents=True)
    (live / "tests").mkdir()
    (live / "pkg" / "core.py").write_text("x = 1\n")
    (live / "tests" / "test_core.py").write_text("def test(): pass\n")
    _git(["init", "-q", "-b", "main"], live)
    _git(["add", "-A"], live)
    _git(["commit", "-qm", "SWE-bench"], live)
    (live / "build").mkdir()
    (live / "build" / "lib.txt").write_text("built\n")                 # present before the task
    base = _git(["rev-parse", "HEAD"], live).strip()
    ws = tmp_path / "ws"
    _git(["worktree", "add", "-q", "--detach", str(ws), base], live)
    (ws / "build").mkdir()
    (ws / "build" / "lib.txt").write_text("built\n")
    (ws / "pkg" / "core.py").write_text("x = 2\n")                    # committed below
    _git(["commit", "-qam", "fix"], ws)
    (ws / "pkg" / "new.py").write_text("y = 1\n")                     # created, uncommitted
    (ws / "tests" / "test_core.py").write_text("def test(): assert 0\n")
    (ws / ".probe_out.txt").write_text("x" * 5000 + "\n")                # the agent's scratch
    (ws / "debug_m2m.py").write_text("print(1)\n")
    (ws / "pkg" / "repro_issue.py").write_text("print(2)\n")
    (ws / "pkg" / "__pycache__").mkdir()
    (ws / "pkg" / "__pycache__" / "core.cpython-39.pyc").write_bytes(b"\0")
    patch = sb.prediction_patch(ws, base, live)
    assert "pkg/core.py" in patch and "+x = 2" in patch
    assert "pkg/new.py" in patch and "+y = 1" in patch
    for absent in ("tests/test_core.py", "build/lib.txt", "__pycache__", ".probe_out.txt", "debug_m2m.py",
                   "repro_issue.py"):
        assert absent not in patch


def test_a_benchmark_project_runs_offline_in_its_image_with_its_own_gate(monkeypatch):
    from agent.tools.sandbox import sandbox_environment_for, sandbox_layout_for
    inst = {"repo": "sympy/sympy"}
    entry = sb.project_entry(inst, {"live": "/l", "sandbox": "/tmp/sbx-proj", "image": "swebench/x:latest",
                                    "base": "abc123"})
    monkeypatch.setitem(PROJECTS, "sbx-proj", entry)
    assert sandbox_environment_for("/tmp/sbx-proj")[0] == "swebench/x:latest"
    layout = sandbox_layout_for("/tmp/sbx-proj")
    assert layout == {"mounts": ["/testbed"], "init": sb.CONDA_INIT, "network": "none", "readonly": []}, \
        "no /baseline here: the template path does not exist in this test"
    names = [c["name"] for c in entry["checks"]]
    assert names == ["compile", "import"] and "import sympy" in entry["checks"][1]["cmd"]
    assert entry["benchmark"] is True and entry["review"] == {"checks": []}


def test_a_project_cannot_mount_over_the_container_s_own_system():
    from agent.tools import sandbox as sbx
    PROJECTS["sbx-bad"] = {"sandbox": "/tmp/sbx-bad", "sandbox_mounts": ["/etc", "/usr", "../x", "/testbed", "relative"],
                           "sandbox_network": "host"}
    try:
        layout = sbx.sandbox_layout_for("/tmp/sbx-bad")
        assert layout["mounts"] == ["/testbed"]
        assert layout["network"] is None, "only none or bridge; never the host's network"
    finally:
        PROJECTS.pop("sbx-bad", None)


async def test_a_project_s_declared_checks_replace_the_npm_scripts(monkeypatch, tmp_path):
    from agent.tools import checks
    ran = []

    async def fake(cmd, cwd, timeout=None, network=None, **k):
        ran.append((cmd, network))
        return {"ok": "fail" not in cmd, "exit_code": 0, "output": "out"}

    monkeypatch.setattr(checks, "run_shell_sandboxed", fake)
    monkeypatch.setitem(PROJECTS, "declared", {"sandbox": str(tmp_path), "checks": [
        {"name": "compile", "cmd": "python -m py_compile x.py"}, {"name": "import", "cmd": "fail please"}]})
    out = await checks.run_all_checks(str(tmp_path), "declared")
    assert ran == [("python -m py_compile x.py", "none"), ("fail please", "none")]
    assert out["all_ok"] is False and set(out["checks"]) == {"compile", "import"}


def test_a_benchmark_seat_has_no_way_out_of_its_repository():
    """The published fix is on GitHub. No GitHub tools, no web pages, no other
    projects -- and (above) no network in its shell."""
    import inspect

    from agent import deep_agent
    src = inspect.getsource(deep_agent.build_deep_agent)
    for guarded in ("github_tools = [] if benchmark", "visual_tools = [] if benchmark",
                    "reference_tools = [] if benchmark"):
        assert guarded in src


# --- batches: a full run never fills the disk ---------------------------------

def _runner():
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location("run_swebench", pathlib.Path("scripts/run_swebench.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_each_batch_is_graded_then_its_images_deleted_and_the_disk_stops_the_run(monkeypatch, tmp_path):
    """500 images are over a terabyte. A batch is run, graded while its images
    are still here, then its images go; a disk past the limit stops the run
    before the next batch rather than filling it. One official report covers
    every batch that ran, and what did not run says why."""
    import json
    mod = _runner()
    rows = [{"instance_id": f"r__r-{i}", "repo": "r/r", "image": f"img-{i}"} for i in range(5)]
    monkeypatch.setattr(mod, "RUNS", tmp_path)
    monkeypatch.setattr(mod.sb, "load_instances", lambda: rows)
    events, spent_seen = [], []
    # Two batches' worth of images fill the disk past the limit.
    monkeypatch.setattr(mod, "disk_used_pct", lambda path="/": 95 if len(spent_seen) >= 2 else 10)

    async def fake_run(args, batch, root, run_dir, spent_before=0.0, on_result=None):
        spent_seen.append(spent_before)
        events.append(("run", [i["instance_id"] for i in batch]))
        for i in batch:
            on_result(i["instance_id"], {"outcome": "done", "cost_usd": 1.0})
            live = json.loads((tmp_path / "t" / "summary.json").read_text())
            assert live["state"] == "running" and live["instances"][i["instance_id"]]["outcome"] == "done", \
                "the dashboard sees each task as it finishes"
        out = {i["instance_id"]: {"outcome": "done", "cost_usd": 1.0} for i in batch}
        if "r__r-1" in out:       # its image could not be set up
            out["r__r-1"] = {"outcome": "setup_error", "reason": "refusing to run"}
        return {"results": out, "runtime_settings": {"x": 1}}

    def fake_grade(pred, ids, run_id, report_dir, max_workers=4, rewrite=False, **k):
        events.append(("rewrite" if rewrite else "grade", list(ids)))
        return {"resolved_ids": [i for i in ids if i.endswith(("-0", "-3"))],
                "failure_reasons": {i: "no_tests_collected" for i in ids if i.endswith("-2")}}

    def fake_subprocess_run(cmd, **k):
        if cmd[:3] == ["docker", "image", "rm"]:
            events.append(("rmi", cmd[-1]))
    monkeypatch.setattr(mod, "_run", fake_run)
    monkeypatch.setattr(mod.sb, "grade", fake_grade)
    monkeypatch.setattr(mod.subprocess, "run", fake_subprocess_run)

    assert mod.main(["--all", "--batch-size", "2", "--run-id", "t", "--max-disk-pct", "80"]) == 0
    assert events == [
        ("run", ["r__r-0", "r__r-1"]), ("grade", ["r__r-0"]), ("rmi", "img-0"), ("rmi", "img-1"),
        ("run", ["r__r-2", "r__r-3"]), ("grade", ["r__r-2", "r__r-3"]), ("rmi", "img-2"), ("rmi", "img-3"),
        ("rewrite", ["r__r-0", "r__r-2", "r__r-3"]),
    ]
    assert spent_seen == [0.0, 1.0], "the ceiling counts the whole run, not one batch"
    summary = json.loads((tmp_path / "t" / "summary.json").read_text())
    assert summary["resolved"] == 2 and summary["total"] == 5 and summary["resolved_rate"] == 40.0
    assert "95%" in summary["stopped_early"]
    assert summary["instances"]["r__r-4"] == {"outcome": "not_run", "resolved": False}
    assert summary["state"] == "stopped" and summary["runtime_settings"] == {"x": 1}
    assert summary["instances"]["r__r-1"]["outcome"] == "setup_error" and summary["instances"]["r__r-1"]["resolved"] is False, \
        "a task that could not be set up is never graded, and never dropped from the total"
    assert summary["instances"]["r__r-2"]["harness_note"] == "no_tests_collected", "the harness's own note is kept"
    assert "harness_note" not in summary["instances"]["r__r-0"]


def test_the_reviewer_s_record_and_files_outlive_the_run_s_temporary_root(tmp_path):
    """Only `review_verdict` used to survive, and the pair's history went with
    the temporary root: "what did the reviewer say" had no answer."""
    import json
    mod = _runner()
    review = {"verdict": "NEEDS_FIXES", "summary": "misses a case", "findings": [{"severity": "blocking", "issue": "x"}],
              "agentMessage": "fix x", "baseline": {"abc": {"pytest": False}}}
    kept = mod.review_record(review)
    assert kept["summary"] == "misses a case" and kept["findings"] and kept["agentMessage"] == "fix x"
    assert "baseline" not in kept, "a cache of which checks fail on base is not a review"
    assert mod.review_record(None) is None and mod.review_record({}) is None

    src, dst = tmp_path / "tmp" / "reviewer", tmp_path / "run" / "reviewer"
    for n, (state, line) in enumerate([({"p1": {"verdict": "READY"}}, '{"project": "p1"}'),
                                        ({"p2": {"verdict": "NEEDS_FIXES"}}, '{"project": "p2"}')], 1):
        src.mkdir(parents=True, exist_ok=True)
        (src / "state.json").write_text(json.dumps(state))
        (src / "history.jsonl").write_text(line + "\n")
        (src / "reviewer.log").write_text(f"batch {n}\n")
        mod.keep_reviewer_files(src, dst)      # once per batch, into one run folder
    assert json.loads((dst / "state.json").read_text()) == {"p1": {"verdict": "READY"}, "p2": {"verdict": "NEEDS_FIXES"}}
    assert (dst / "history.jsonl").read_text() == '{"project": "p1"}\n{"project": "p2"}\n'
    assert (dst / "reviewer.log").read_text() == "batch 1\nbatch 2\n"
    mod.keep_reviewer_files(tmp_path / "nowhere", dst)     # a batch whose pair never started


def test_a_trajectory_is_every_message_each_conversation_held():
    """The agent's `messages` is a DeltaChannel: its checkpoint holds only a
    marker, and the first runs saved an empty trajectory for every task. The
    conversation is rebuilt from the writes, including what summarization
    later removed, each subagent's separately."""
    from types import SimpleNamespace

    from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, ToolMessage
    mod = _runner()

    def tup(cid, ns, writes):
        return SimpleNamespace(config={"configurable": {"checkpoint_id": cid, "checkpoint_ns": ns}},
                               checkpoint={"channel_values": {}}, pending_writes=writes)
    goal, call = HumanMessage("fix it", id="h"), AIMessage("", id="a", tool_calls=[{"name": "read", "args": {}, "id": "c"}])
    out = ToolMessage("file", tool_call_id="c", id="t")
    sub = AIMessage("delegated work", id="s")
    tuples = [
        tup("3", "", [("x", "messages", [RemoveMessage(id="h")]), ("x", "todos", [])]),   # summarization
        tup("1", "", [("x", "messages", [goal])]),
        tup("2", "", [("x", "messages", [call, out]), ("x", "messages", [call])]),
        tup("2", "tools:abc", [("y", "messages", sub)]),
    ]
    convs = mod.conversations(tuples)
    assert [m.id for m in convs[""]] == ["h", "a", "t"]
    assert [m.id for m in convs["tools:abc"]] == ["s"]


def test_old_release_tags_are_history_but_anything_after_the_task_is_refused(tmp_path, monkeypatch):
    """Several images carry old release tags on maintenance branches (the
    matplotlib ones reach back to 0.91): history the task's authors had. A
    later commit, on any branch or tag, is the future -- and the fix is in it."""
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null"}.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "r"
    repo.mkdir()

    def commit(msg, when, *extra):
        monkeypatch.setenv("GIT_COMMITTER_DATE", when)
        monkeypatch.setenv("GIT_AUTHOR_DATE", when)
        _git(["commit", "-q", "--allow-empty", "-m", msg, *extra], repo)
        return _git(["rev-parse", "HEAD"], repo).strip()

    _git(["init", "-q", "-b", "main"], repo)
    root = commit("root", "2019-01-01T00:00:00")
    _git(["checkout", "-q", "-b", "v3.0.x"], repo)
    commit("REL: v3.0.3", "2019-02-25T00:00:00")            # an old maintenance release
    _git(["tag", "v3.0.3"], repo)
    _git(["checkout", "-q", "main"], repo)
    base = commit("the task's base", "2019-04-18T00:00:00")
    _git(["checkout", "-q", "-b", "later"], repo)
    fix = commit("the fix", "2019-05-01T00:00:00")          # a descendant of the base
    _git(["checkout", "-q", "main"], repo)
    commit("SWE-bench", "2026-08-13T00:00:00")               # the image's own commit, HEAD
    assert sb.future_commits(repo, base) == [fix]

    _git(["branch", "-D", "later"], repo)
    assert sb.future_commits(repo, base) == [], "old release tags are history, not the future"

    _git(["checkout", "-q", "--orphan", "stray"], repo)
    stray = commit("an unrelated newer commit", "2020-01-01T00:00:00")
    _git(["checkout", "-q", "main"], repo)
    assert sb.future_commits(repo, base) == [stray], "newer than the base, however it is connected"
    assert root


def test_only_a_benchmark_project_runs_with_no_approvals(monkeypatch):
    """A benchmark task has nobody to ask, so nothing may stop for a person --
    it parked forever on deleting a generated file. Every other project keeps
    its gates, auto-approve or not."""
    from agent.deep_agent import INTERRUPT_ON, approval_gates
    monkeypatch.setitem(PROJECTS, "bench", {"sandbox": "/tmp/bench", "benchmark": True})
    monkeypatch.setitem(PROJECTS, "real", {"sandbox": "/tmp/real"})
    assert approval_gates("bench", auto_approve_commands=False) == {}
    assert approval_gates("bench", auto_approve_commands=True, repo_root="/tmp/bench") == {}
    assert approval_gates("real", auto_approve_commands=False) is INTERRUPT_ON
    auto = approval_gates("real", auto_approve_commands=True, repo_root="/tmp/real")
    assert "bash" in auto and "ask_user" in auto, "auto-approve on a real project still asks before losing work"


def test_every_batch_s_projects_are_the_ones_the_agent_sees(tmp_path, monkeypatch):
    """The second batch reloaded a projects file deleted with the first,
    fell back to the example projects, and the run crashed 20 tasks in."""
    import agent.config as cfg
    mod = _runner()
    path = tmp_path / "run" / "projects.json"
    path.parent.mkdir()
    monkeypatch.setattr(cfg, "_PROJECTS_CONFIG_PATH", path)
    saved = dict(cfg.PROJECTS)
    try:
        mod.publish_projects(path, {"batch1-task": {"sandbox": str(tmp_path / "s1")}})
        assert "batch1-task" in cfg.PROJECTS
        mod.publish_projects(path, {"batch2-task": {"sandbox": str(tmp_path / "s2")}})
        assert "batch2-task" in cfg.PROJECTS and "batch1-task" not in cfg.PROJECTS
        from agent import workspaces
        assert workspaces.task_workspace_path("batch2-task", "t1").startswith(str(tmp_path))
        with pytest.raises(RuntimeError, match="reads projects from"):
            mod.publish_projects(tmp_path / "elsewhere.json", {"x": {"sandbox": "/tmp/x"}})
    finally:
        cfg.PROJECTS.clear()
        cfg.PROJECTS.update(saved)


def test_every_batch_s_tasks_call_that_batch_s_reviewer():
    """The review gate reads its ports once, at import: the second batch's
    tasks called the first batch's reviewer, long gone, and waited."""
    from agent.tools import review_gate
    mod = _runner()
    before = (review_gate.REVIEW_SERVICE_PORT, review_gate.REVIEW_CONTROL_PORT)
    # point_agent_at_reviewer also writes the ports into os.environ; left
    # there, a later reload of the gate in another test file read them.
    env_before = {v: os.environ.get(v) for v in ("REVIEW_SERVICE_PORT", "REVIEW_CONTROL_PORT")}
    try:
        review_gate._CHECKS_CACHE["all"] = {"stale": True}
        mod.point_agent_at_reviewer({"REVIEW_SERVICE_PORT": "45001", "REVIEW_CONTROL_PORT": "45002"})
        assert (review_gate.REVIEW_SERVICE_PORT, review_gate.REVIEW_CONTROL_PORT) == (45001, 45002)
        assert "all" not in review_gate._CHECKS_CACHE
        mod.point_agent_at_reviewer({"REVIEW_SERVICE_PORT": "46001", "REVIEW_CONTROL_PORT": "46002"})
        assert review_gate.REVIEW_SERVICE_PORT == 46001, "the next batch moves it again"
    finally:
        review_gate.REVIEW_SERVICE_PORT, review_gate.REVIEW_CONTROL_PORT = before
        for var, val in env_before.items():
            os.environ.pop(var, None) if val is None else os.environ.__setitem__(var, val)


@pytest.mark.parametrize("cmd", [
    "cd /workspace && git cat-file --batch-all-objects --batch-check='%(objecttype)'",
    "cd /workspace && git fsck --lost-found 2>&1 | head",
    "cd /workspace && git log --all --oneline --grep=context",
    "cd /workspace && pip download astropy==5.2 2>&1 | tail -2",
    'curl -s "https://api.github.com/repos/astropy/astropy/pulls/14580/files"',
    'cd / && grep -rl "division_of_units" --include=*.py / 2>/dev/null | head',
    'find / -maxdepth 4 -iname "*eval*"',
    "cat /tmp/tektonix-swebench-abc/work/live/x/.git/packed-refs",
    # 2026-09-25: what walked past the first patterns
    "strings sphinx/domains/__pycache__/cpp.cpython-39.pyc | grep -i userdefined",
    "python -c \"import marshal; c = marshal.loads(open('tests/__pycache__/test_domain_py.cpython-39.pyc',"
    "'rb').read()[16:]); print(c.co_names)\"",
    "python -m dis sphinx/__pycache__/x.cpython-39.pyc",
    "xxd tests/__pycache__/test_x.cpython-39.pyc | head",
    "git branch -a && git tag | tail -5",
    "git branch -r", "git branch --all", "git tag", "git tag --contains abc",
    "git for-each-ref --sort=-creatordate | head",
    "git ls-remote origin",
    "gh api repos/django/django/pulls/13033/files",
    "gh pr view 13033",
    "ls ~/.cache/pip", "ls $HOME/.cache", "find /root/.cache/pip -name '*.whl'",
    # a loop cannot be split, so it is judged whole
    "for f in a b; do curl $f; done",
])
def test_searching_for_the_published_fix_is_refused_on_a_benchmark(cmd):
    """Answer-hunting was 15% of all shell commands in the first samples and
    most of the budget of the tasks that failed; one curled GitHub for the
    fix's own pull request."""
    from agent.tools.benchmark_guard import refusal, screen
    msg = refusal(cmd)
    assert msg and msg.startswith("[Persei harness] REFUSED on a benchmark task") and "does not exist anywhere" in msg
    assert screen(cmd) == (None, msg), "nothing of it runs"


@pytest.mark.parametrize("cmd", [
    "cd /workspace && python -m pytest astropy/timeseries/tests/test_sampled.py -q",
    "cd /workspace && git log --oneline -5 -- astropy/io/fits/card.py",
    "cd /workspace && git diff && git status --short",
    'cd /workspace && grep -rn "is invalid - expected" astropy/',
    "cd /workspace && find . -name '*.py' -path '*timeseries*'",
    "ls /opt/miniconda3/envs/testbed/lib/python3.9/site-packages/numpy/core",
    "cd /workspace && python .scratch/probe.py",
    # ordinary work that shares words with the hunts
    "pip list | grep -i sphinx", "pip show django",
    "git log --oneline -3", "git log -p -- django/utils/x.py", "git blame -L 10,20 a.py",
    "git show HEAD:a.py", "git describe --tags", "git branch", "git branch -vv",
    'python -c "import sphinx; print(sphinx.__file__)"',
    # cleaning stale bytecode is fine; only reading it is a hunt
    "find . -name __pycache__ -exec rm -rf {} +",
    "find . -name '*.pyc' -delete",
    'grep -rn "foo" --exclude-dir=__pycache__ .',
    "git log --oneline --grep=tags",
])
def test_ordinary_work_is_never_refused(cmd):
    from agent.tools.benchmark_guard import refusal, screen
    assert refusal(cmd) is None
    assert screen(cmd) == (cmd, None)


@pytest.mark.parametrize("cmd,remaining,dropped", [
    ("git log --all --oneline | head -5 && git status --short", "git status --short", "git log --all --oneline | head -5"),
    ('grep -rn "has_key" django/ ; find / -name json.py', 'grep -rn "has_key" django/', "find / -name json.py"),
    ("cd /workspace && curl -s http://x || echo offline", "cd /workspace || echo offline", "curl -s http://x"),
    ("cd /workspace && git log --all --oneline\ngit status", "cd /workspace \ngit status", "git log --all --oneline"),
])
def test_only_the_hunting_half_of_a_compound_command_is_refused(cmd, remaining, dropped):
    """62 refusals over 50 tasks on 2026-09-25, most of them compounds whose
    legitimate half was thrown away with the hunt; in one task that half was
    the `grep` that would have found the regression the task failed on."""
    from agent.tools.benchmark_guard import screen
    run, note = screen(cmd)
    assert run == remaining
    assert note.startswith("[Persei harness] REFUSED part of that command on a benchmark task: `" + dropped + "`")
    assert "does not exist anywhere" in note and "The rest of the command ran" in note


def test_the_task_statement_says_the_fix_is_not_in_the_environment():
    goal = sb.goal_for({"repo": "a/b", "problem_statement": "broken"})
    assert "does not exist anywhere in this environment" in goal and "Do not search for them" in goal


def test_the_task_statement_forbids_a_remembered_patch_and_says_how_to_run_baseline():
    """2026-09-25: a verified change was swapped for the upstream fix from
    memory, and an editable install kept importing /workspace at /baseline."""
    goal = sb.goal_for({"repo": "a/b", "problem_statement": "broken"})
    assert "Do not reproduce a remembered patch" in goal
    assert "never replace a change you have verified with a remembered one" in goal
    assert "PYTHONPATH=/baseline" in goal and "cd /baseline" in goal
    assert goal.index("/baseline") < goal.index("<issue>"), "framing before the issue, never inside it"


def _bash(tmp_path, monkeypatch, ran: list, benchmark: bool):
    """The real bash tool with the container replaced by a recorder."""
    from agent.tools import agent_tools

    async def _fake_sandbox(cmd, cwd, timeout=None, extra_env=None, network=None):
        ran.append(cmd)
        return {"ok": True, "exit_code": 0, "output": "ok\n"}

    monkeypatch.setattr(agent_tools, "run_shell_sandboxed", _fake_sandbox)
    tools, _ = agent_tools.make_agent_tools(str(tmp_path), benchmark=benchmark)
    return {t.name: t for t in tools}["bash"]


def test_only_a_benchmark_s_bash_carries_the_guard(tmp_path, monkeypatch):
    import asyncio
    import inspect

    from agent import deep_agent
    from agent.tools import agent_tools, bash_advice
    assert "if benchmark:" in inspect.getsource(agent_tools.make_agent_tools)
    assert 'benchmark=bool((PROJECTS.get(repo) or {}).get("benchmark"))' in inspect.getsource(deep_agent.build_deep_agent)

    hunt = 'grep -rn "has_key" django/ ; find / -name json.py'
    ran: list = []
    out = asyncio.run(_bash(tmp_path / "plain", monkeypatch, ran, benchmark=False).ainvoke({"command": hunt}))
    assert ran == [hunt] and out.startswith("exit_code=0"), "an ordinary project runs it all, unremarked"

    ran.clear()
    out = asyncio.run(_bash(tmp_path / "bench", monkeypatch, ran, benchmark=True).ainvoke({"command": hunt}))
    assert ran == ['grep -rn "has_key" django/'], "the legitimate half ran"
    assert out.startswith("[Persei harness] REFUSED part of that command") and "\nexit_code=0\nok" in out
    assert bash_advice.kind_of_result(out) == "benchmark-refused", "the work node's telemetry can see it"

    ran.clear()
    out = asyncio.run(_bash(tmp_path / "bench", monkeypatch, ran, benchmark=True).ainvoke({"command": "git fsck"}))
    assert ran == [] and out.startswith("[Persei harness] REFUSED on a benchmark task")
    assert bash_advice.kind_of_result(out) == "benchmark-refused"


@pytest.mark.parametrize("cmd,prefixed", [
    ("cd /baseline/tests && python runtests.py utils_tests -v 1", True),
    ("python /baseline/tests/runtests.py utils_tests", True),
    ('cd "/baseline" && python -m pytest tests/test_x.py -q', True),
    ("diff -u /baseline/django/utils/x.py django/utils/x.py", False),
    ("cd /baseline/tests && PYTHONPATH=/baseline python runtests.py x", False),
    ("cd /workspace && python -m pytest tests -q", False),
])
def test_a_run_at_baseline_imports_the_baseline_not_the_patched_tree(tmp_path, monkeypatch, cmd, prefixed):
    """2026-09-25: `cd /baseline/tests && python runtests.py` was the agent's
    baseline check and it tested the PATCHED code -- the editable install
    resolves to the workspace whatever the cwd. The task had passed in two
    earlier runs."""
    import asyncio

    from agent.tools.agent_tools import BASELINE_PYTHONPATH_NOTE
    ran: list = []
    out = asyncio.run(_bash(tmp_path, monkeypatch, ran, benchmark=True).ainvoke({"command": cmd}))
    if prefixed:
        assert ran == ["export PYTHONPATH=/baseline${PYTHONPATH:+:$PYTHONPATH}; " + cmd]
        assert out.startswith(BASELINE_PYTHONPATH_NOTE + "\nexit_code=0")
    else:
        assert ran == [cmd] and out.startswith("exit_code=0")
    ran.clear()
    asyncio.run(_bash(tmp_path, monkeypatch, ran, benchmark=False).ainvoke({"command": cmd}))
    assert ran == [cmd], "only a benchmark project has a /baseline"


def test_a_benchmark_sees_its_untouched_tree_at_baseline_read_only(tmp_path, monkeypatch):
    """Agents kept retrying `git stash` on the sandbox's read-only .git to run
    the code as it was before their change."""
    from agent.tools.sandbox import sandbox_layout_for
    template = tmp_path / "sbx-proj"
    template.mkdir()
    entry = sb.project_entry({"repo": "sympy/sympy"}, {"live": "/l", "sandbox": str(template),
                                                        "image": "swebench/x:latest", "base": "abc123"})
    assert entry["sandbox_readonly_mounts"] == {"/baseline": str(template)}
    monkeypatch.setitem(PROJECTS, "sbx-proj", entry)
    assert sandbox_layout_for(str(template))["readonly"] == [(str(template), "/baseline")]
    monkeypatch.setitem(PROJECTS, "bad", {"sandbox": str(tmp_path / "bad"),
                                          "sandbox_readonly_mounts": {"/etc": str(template), "/b": "relative",
                                                                      "/c": "/no/such/dir"}})
    (tmp_path / "bad").mkdir()
    assert sandbox_layout_for(str(tmp_path / "bad"))["readonly"] == []


def test_a_sandbox_cannot_swap_and_is_told_its_own_size():
    """31 containers were killed after thrashing the host's swap: Django's test
    runner sized itself from the host's 32 cores inside a 2-CPU container."""
    import inspect

    from agent.tools import sandbox
    src = inspect.getsource(sandbox.run_shell_sandboxed)
    assert '"--memory-swap", SANDBOX_MEMORY_SWAP' in src and "SANDBOX_TEST_ENV" in src
    assert sandbox.SANDBOX_MEMORY_SWAP == sandbox.SANDBOX_MEMORY_LIMIT
    assert sandbox.SANDBOX_TEST_ENV == {"DJANGO_TEST_PROCESSES": sandbox.SANDBOX_CPU_LIMIT}


def test_shards_split_one_selection_without_overlap():
    mod = _runner()
    rows = [{"instance_id": str(i)} for i in range(11)]
    parts = [mod.shard(rows, f"{k}/3") for k in (1, 2, 3)]
    assert sorted(r["instance_id"] for p in parts for r in p) == sorted(r["instance_id"] for r in rows)
    assert mod.shard(rows, None) == rows
    with pytest.raises(SystemExit):
        mod.shard(rows, "4/3")


@pytest.mark.parametrize("cmd,kind", [
    ("cd /workspace && git stash", "git-write"),
    ("git -C /workspace checkout -- a.py", "git-write"),
    ("git stash pop", "git-write"),
    ("git add -A && git commit -m x", "git-write"),
    ("cat > /workspace/.scratch/p.py <<'EOF'\nprint(1)\nEOF", None),
    ("cd /workspace && git show HEAD:a.py | head", None),
    ("git diff HEAD", None),
    # 2026-09-25: read-only git that the old \\b matched
    ("git merge-base --is-ancestor abc HEAD", None),
    ("git stash list", None),
    ("git stash show -p", None),
    ("git cat-file -t HEAD", None),
    ("git status --short && git stash list", None),
])
def test_git_writes_are_explained_and_scratch_probes_are_not_nagged(cmd, kind):
    from agent.tools import bash_advice
    assert bash_advice.kind(cmd) == kind


def test_the_git_write_note_does_not_point_at_a_head_that_moves():
    """The ship gate commits mid-task, after which HEAD includes the agent's
    own change; `git show HEAD:<path>` then showed it its own code as the
    original, and one task lost six calls to that."""
    from agent.tools.bash_advice import GIT_WRITE_NOTE
    assert "git show HEAD:" not in GIT_WRITE_NOTE
    assert "/baseline" in GIT_WRITE_NOTE and "PYTHONPATH=/baseline" in GIT_WRITE_NOTE
    assert "`git show <base>:<path>`" in GIT_WRITE_NOTE and "git log --oneline -3" in GIT_WRITE_NOTE


async def test_an_edit_that_changes_nothing_is_refused(tmp_path):
    from agent.tools.agent_tools import make_agent_tools
    (tmp_path / "a.py").write_text("x = 1\n")
    tools, _ = make_agent_tools(str(tmp_path))
    edit = {t.name: t for t in tools}["edit"]
    out = await edit.ainvoke({"path": "a.py", "old_string": "x = 1", "new_string": "x = 1"})
    assert out.startswith("[Persei harness] ERROR: old_string and new_string are identical")


def test_grading_retries_the_harness_s_image_listing_race(tmp_path, monkeypatch):
    """With two shards side by side, the harness's report step listed an image
    the other shard removed a moment later, and the whole shard crashed."""
    import json as _json
    monkeypatch.setattr(sb, "GRADE_RETRY_S", 0)
    calls = []

    def fake_run(cmd, cwd=None, **k):
        calls.append(cmd)
        if len(calls) == 1:
            return subprocess.CompletedProcess(cmd, 1, "", "docker.errors.ImageNotFound: 404 No such image")
        (tmp_path / "tektonix.r1.json").write_text(_json.dumps({"resolved_ids": ["a"]}))
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(sb.subprocess, "run", fake_run)
    assert sb.grade(tmp_path / "p.jsonl", ["a"], "r1", tmp_path, log=lambda m: None)["resolved_ids"] == ["a"]
    assert len(calls) == 2

    calls.clear()
    monkeypatch.setattr(sb.subprocess, "run", lambda cmd, **k: (calls.append(cmd),
                        subprocess.CompletedProcess(cmd, 1, "", "some other failure"))[1])
    with pytest.raises(sb.SetupError):
        sb.grade(tmp_path / "p.jsonl", ["a"], "r2", tmp_path, log=lambda m: None)
    assert len(calls) == 1, "only that race is retried"
