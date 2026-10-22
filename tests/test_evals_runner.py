"""The harness: fixtures, the run loop, the budget stop and the report.

No money is spent here. The graph is a fake that returns a chosen final state,
which is enough to test everything the runner actually decides -- how it reads
an outcome, when it stops, what it scores -- without a model call. The one
thing a fake graph cannot cover is that the real graph parks on
`require_merge_review` instead of merging; that is asserted directly against
the real routing function at the bottom.
"""
import json
import subprocess

import pytest

from agent.evals import fixtures as fx
from agent.evals import report as ev_report
from agent.evals import runner
from agent.evals.spec import Assertion, TaskSpec, load_fixture


# --- fixtures --------------------------------------------------------------

@pytest.fixture
def demo_spec(tmp_path):
    root = tmp_path / "fixtures" / "demo"
    (root / "files" / "src").mkdir(parents=True)
    (root / "files" / "src" / "a.py").write_text("x = 1\n")
    (root / "files" / "package.json").write_text('{"scripts": {"test": "true"}}\n')
    (root / "fixture.yaml").write_text(
        "description: a demo\nstack: python\n"
        "review:\n  checks:\n    - name: test\n      dir: '.'\n      cmd: 'true'\n      args: []\n")
    return load_fixture("demo", tmp_path / "fixtures")


def test_materialize_builds_a_live_repo_and_a_worktree(demo_spec, tmp_path):
    mf = fx.materialize(demo_spec, tmp_path / "work")
    assert (mf.live / ".git").is_dir()
    # A worktree's .git is a FILE pointing at the live repo, not a directory.
    # That shape is what the sandbox's git mount depends on, so it is the
    # thing worth asserting rather than mere existence.
    assert (mf.sandbox / ".git").is_file()
    assert (mf.sandbox / "src" / "a.py").read_text() == "x = 1\n"
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=mf.sandbox,
                            capture_output=True, text=True).stdout.strip()
    assert branch == fx.BASE_BRANCH


def test_materialize_is_idempotent_so_a_rerun_starts_clean(demo_spec, tmp_path):
    """Rebuilt, not reset. A reset that misses a stray file does not fail --
    it contaminates the next task, and the number that comes out is wrong in
    a way nobody can see."""
    mf = fx.materialize(demo_spec, tmp_path / "work")
    (mf.sandbox / "leftover.txt").write_text("from the last task")
    again = fx.materialize(demo_spec, tmp_path / "work")
    assert not (again.sandbox / "leftover.txt").exists()


def test_the_fixture_commit_does_not_need_the_operators_git_identity(demo_spec, tmp_path, monkeypatch):
    """A suite that only runs for people with a configured git user is a suite
    that does not run in CI."""
    monkeypatch.delenv("GIT_AUTHOR_NAME", raising=False)
    mf = fx.materialize(demo_spec, tmp_path / "work")
    log = subprocess.run(["git", "log", "-1", "--format=%an"], cwd=mf.live,
                         capture_output=True, text=True).stdout.strip()
    assert log == "tektonix-evals"


def test_projects_json_contains_only_the_fixtures(demo_spec, tmp_path):
    """The live projects.json must never learn about a fixture, and an eval
    must never see a real project. One file, written fresh, is what makes both
    true at once."""
    mf = fx.materialize(demo_spec, tmp_path / "work")
    path = tmp_path / "projects.json"
    fx.write_projects_json(path, [mf])
    data = json.loads(path.read_text())
    assert list(data["projects"]) == ["demo"]
    assert data["projects"]["demo"]["sandbox"] == str(mf.sandbox)
    # The review block is what the reviewer reads its check commands from --
    # the fixture goes through it as an ordinary project.
    assert data["projects"]["demo"]["review"]["checks"][0]["name"] == "test"


def test_changed_paths_reads_the_committed_branch(demo_spec, tmp_path):
    mf = fx.materialize(demo_spec, tmp_path / "work")
    branch = fx.task_branch_name("abc-123")
    fx._git(["checkout", "-q", "-b", branch], mf.sandbox)
    (mf.sandbox / "src" / "a.py").write_text("x = 2\n")
    fx._git(["add", "-A"], mf.sandbox)
    fx._git(["commit", "-q", "-m", "change"], mf.sandbox)
    assert fx.changed_paths(mf, branch) == ("src/a.py",)


def test_changed_paths_falls_back_to_a_dirty_worktree(demo_spec, tmp_path):
    """An escalated task never commits. Without this the diff assertions on
    every failed task would report 'changed nothing' and quietly PASS a
    diff_excludes."""
    mf = fx.materialize(demo_spec, tmp_path / "work")
    (mf.sandbox / "src" / "b.py").write_text("new file\n")
    (mf.sandbox / "src" / "a.py").write_text("edited\n")
    assert fx.changed_paths(mf, fx.task_branch_name("never-committed")) == ("src/a.py", "src/b.py")


def test_the_task_branch_name_matches_what_the_reviewer_will_accept(demo_spec):
    """The reviewer only reviews `agent/<uuid>`. A harness guessing a
    different shape would look for a diff that is there under another name."""
    import re
    task_id = "0b5f2c1a-1111-2222-3333-444455556666"
    assert re.match(r"^agent/[0-9a-f-]{36}$", fx.task_branch_name(task_id))


# --- the run loop ----------------------------------------------------------

def task(id="demo-task", fixture="demo", budget=2.0, assertions=None, skip=""):
    from pathlib import Path
    return TaskSpec(id=id, fixture=fixture, category="bug-fix", goal="do it",
                    budget_usd=budget,
                    assertions=tuple(assertions or [Assertion("checks_pass", True)]),
                    path=Path(f"{id}.yaml"), skip=skip)


class FakeGraph:
    """Returns a chosen final state, and records what it was asked to run."""

    def __init__(self, final, per_task=None):
        self.final = final
        self.per_task = per_task or {}
        self.invocations = []

    async def ainvoke(self, state, config):
        self.invocations.append((state, config))
        return {}

    async def aget_state(self, config):
        tid = config["configurable"]["thread_id"]
        values = self.per_task.get(tid, self.final)

        class Snapshot:
            pass
        s = Snapshot()
        s.values = values
        return s


@pytest.fixture
def run_kwargs(tmp_path, monkeypatch, demo_spec):
    """Point the runner's fixture lookup at the temp fixture."""
    import agent.evals.spec as spec_mod
    monkeypatch.setattr(spec_mod, "FIXTURES_DIR", demo_spec.root.parent)
    monkeypatch.setattr("agent.config.reload_projects", lambda: {})
    return {"config": None, "eval_root": tmp_path / "evalroot"}


SHIPPED = {"review_gate_result": {"verdict": "READY"}, "iteration_count": 0,
           "cost_so_far": 1.25, "escalated": False}
ESCALATED = {"review_gate_result": None, "escalated": True, "iteration_count": 3,
             "escalation_reason": "the check suite kept failing", "cost_so_far": 0.8}


@pytest.mark.asyncio
async def test_a_shipped_task_is_read_the_way_the_episode_writer_reads_it(run_kwargs):
    run = await runner.run_task(task(), graph=FakeGraph(SHIPPED), **run_kwargs)
    assert run.outcome == "shipped"
    assert run.review_verdict == "READY"
    assert run.iteration_count == 0
    assert run.cost_usd == 1.25
    # A verdict can only exist downstream of a green suite, so this is known
    # rather than guessed.
    assert run.checks_pass is True
    assert run.passed


@pytest.mark.asyncio
async def test_an_escalated_task_is_not_a_pass(run_kwargs):
    run = await runner.run_task(task(), graph=FakeGraph(ESCALATED), **run_kwargs)
    assert run.outcome == "escalated"
    assert run.checks_pass is False   # the reason names the checks
    assert not run.passed


@pytest.mark.asyncio
async def test_a_task_that_ships_but_misses_its_assertions_fails(run_kwargs):
    """The whole reason a golden task is not scored on its outcome: this task
    cleared every gate the system has and did not do the thing."""
    t = task(assertions=[Assertion("checks_pass", True),
                         Assertion("file_matches", {"path": "src/a.py", "pattern": "never-here"})])
    run = await runner.run_task(t, graph=FakeGraph(SHIPPED), **run_kwargs)
    assert run.outcome == "shipped" and not run.passed
    assert len(run.assertions.failures) == 1


@pytest.mark.asyncio
async def test_a_graph_that_raises_is_an_error_not_a_crash(run_kwargs):
    class Boom(FakeGraph):
        async def ainvoke(self, state, config):
            raise RuntimeError("router down")

    run = await runner.run_task(task(), graph=Boom({}), **run_kwargs)
    assert run.outcome == "error" and "router down" in run.error
    assert not run.passed


@pytest.mark.asyncio
async def test_the_task_runs_with_merge_review_on_which_is_what_stops_the_merge(run_kwargs):
    graph = FakeGraph(SHIPPED)
    await runner.run_task(task(), graph=graph, **run_kwargs)
    state, config = graph.invocations[0]
    assert state["require_merge_review"] is True
    assert config["metadata"]["eval"] == "demo-task"


@pytest.mark.asyncio
async def test_the_suite_stops_before_it_crosses_the_ceiling(run_kwargs):
    """Before, not after. Stopping once the overspend has happened reports a
    ceiling that was already breached, which is not a ceiling."""
    tasks = [task(id=f"task-{i}", budget=2.0) for i in range(5)]
    suite = await runner.run_suite(tasks, graph=FakeGraph(SHIPPED),
                                   cost_ceiling_usd=5.0, **run_kwargs)
    # The bound is ACTUAL spend so far plus this task's cap, not the sum of
    # the caps. Both are hard bounds -- a task can never spend more than its
    # budget, so the ceiling cannot be crossed either way -- but tasks
    # routinely spend well under their cap, and budgeting on caps would leave
    # most of a run's ceiling unused. Each task here costs 1.25 against a 2.00
    # cap: three start (0 + 2, 1.25 + 2, 2.50 + 2 all within 5.00) and the
    # fourth is refused at 3.75 + 2.
    assert len(suite.attempted) == 3
    assert suite.total_cost == pytest.approx(3.75)
    assert suite.total_cost <= 5.0
    assert suite.stopped_early and "ceiling" in suite.stopped_early
    assert "task-3" in suite.stopped_early


@pytest.mark.asyncio
async def test_a_skipped_task_is_recorded_and_costs_nothing(run_kwargs):
    suite = await runner.run_suite([task(skip="reproduces a bug that is now fixed")],
                                   graph=FakeGraph(SHIPPED), cost_ceiling_usd=25.0, **run_kwargs)
    assert suite.runs[0].outcome == "skipped"
    assert suite.attempted == []
    assert suite.total_cost == 0.0


@pytest.mark.asyncio
async def test_every_task_gets_a_freshly_rebuilt_fixture(run_kwargs):
    """Two tasks in one run must not see each other's leftovers."""
    graph = FakeGraph(SHIPPED)
    t = task(assertions=[Assertion("file_absent", ["leftover.txt"])])
    first = await runner.run_task(t, graph=graph, **run_kwargs)
    # Simulate the first task having left something behind.
    (run_kwargs["eval_root"] / "work" / "sandbox" / "demo" / "leftover.txt").write_text("x")
    second = await runner.run_task(t, graph=graph, **run_kwargs)
    assert first.passed and second.passed


# --- the report ------------------------------------------------------------

def make_suite(runs, cost=0.0, stopped=""):
    s = runner.SuiteRun(window_label="2026-09-22T12:00:00Z")
    s.runs = runs
    s.total_cost = cost
    s.stopped_early = stopped
    return s


def make_run(id, passed, outcome="shipped", verdict="READY", iters=0, cost=1.0):
    r = runner.TaskRun(task=task(id=id), task_id=f"tid-{id}", outcome=outcome,
                       review_verdict=verdict, iteration_count=iters, cost_usd=cost)
    from agent.evals.assertions import AssertionResult, TaskAssertionReport
    r.assertions = TaskAssertionReport(results=[
        AssertionResult(Assertion("checks_pass", True), passed, "checks passed" if passed else "nope")])
    return r


def test_the_report_carries_the_same_six_numbers_as_the_analytics_panel():
    """An eval that scored itself on private metrics would answer a question
    the dashboard cannot be compared against."""
    suite = make_suite([make_run("a", True), make_run("b", False, iters=2)], cost=2.0)
    report = ev_report.build(suite, cost_ceiling_usd=25.0)
    bench = report["benchmarks"]
    assert bench["tasks"] == 2
    assert bench["first_pass"] == 1        # only the one with 0 redos
    assert bench["first_pass_rate"] == 50.0
    assert bench["iterations_median"] == 1


def test_the_headline_is_assertions_not_outcomes():
    suite = make_suite([make_run("a", True), make_run("b", False)])
    report = ev_report.build(suite, cost_ceiling_usd=25.0)
    assert report["tasks_passed"] == 1
    assert report["pass_rate"] == 50.0
    # Both SHIPPED. Scored on outcome this run would read as 100%.
    assert all(t["outcome"] == "shipped" for t in report["tasks"])


def test_errored_tasks_are_kept_out_of_the_aggregate_but_shown():
    """A task that blew up in the harness says nothing about the agent, and
    counting it as an escalation would blame the agent for the harness."""
    err = runner.TaskRun(task=task(id="c"), task_id="x", outcome="error", error="router down")
    report = ev_report.build(make_suite([make_run("a", True), err]), cost_ceiling_usd=25.0)
    assert report["benchmarks"]["tasks"] == 1
    assert any(t["outcome"] == "error" for t in report["tasks"])


def test_a_run_with_nothing_scored_has_no_benchmarks_rather_than_fake_ones():
    err = runner.TaskRun(task=task(id="c"), task_id="x", outcome="error", error="boom")
    assert ev_report.build(make_suite([err]), cost_ceiling_usd=25.0)["benchmarks"] is None


def test_render_shows_the_reason_a_task_failed(tmp_path):
    report = ev_report.build(make_suite([make_run("b", False)]), cost_ceiling_usd=25.0)
    out = ev_report.render(report)
    assert "FAIL" in out and "b" in out
    # The point of a failing golden task is WHY, and a table of crosses sends
    # the reader to the JSON.
    assert "nope" in out


def test_the_diff_names_what_regressed():
    previous = ev_report.build(make_suite([make_run("a", True), make_run("b", True)]),
                               cost_ceiling_usd=25.0)
    now = ev_report.build(make_suite([make_run("a", True), make_run("b", False)]),
                          cost_ceiling_usd=25.0)
    out = ev_report.diff_against(now, previous)
    assert "REGRESSED: b" in out
    assert "now passing" not in out


def test_the_diff_says_so_when_nothing_moved():
    r = ev_report.build(make_suite([make_run("a", True)]), cost_ceiling_usd=25.0)
    assert "same tasks passing" in ev_report.diff_against(r, r)


def test_a_report_round_trips_through_disk(tmp_path):
    report = ev_report.build(make_suite([make_run("a", True)], cost=1.0), cost_ceiling_usd=25.0)
    path = ev_report.write(report, tmp_path)
    assert json.loads(path.read_text())["tasks_passed"] == 1
    assert ev_report.latest_report(tmp_path)["tasks_passed"] == 1


def test_a_truncated_report_does_not_lose_the_comparison(tmp_path):
    (tmp_path / "2026-01-01T00-00-00Z.json").write_text("{not json")
    good = ev_report.build(make_suite([make_run("a", True)]), cost_ceiling_usd=25.0)
    ev_report.write(good, tmp_path)
    assert ev_report.latest_report(tmp_path) is not None


# --- the claim the fake graph cannot make ---------------------------------

def test_a_ready_verdict_awaiting_merge_approval_ends_the_graph():
    """The entire ship-depth decision rests on this: with require_merge_review
    on, a READY verdict parks the task instead of merging, and the harness is
    simply an operator who never approves. If this ever routed onward, an eval
    would start merging into fixture repos -- and, with a real project
    registered, somewhere worse."""
    from langgraph.graph import END

    from agent.outer_graph import _route_after_verify
    parked = {"pending_merge_approval": {"sha": "abc", "repo": "demo"},
              "review_gate_result": {"verdict": "READY"}, "committed_sha": "abc"}
    assert _route_after_verify(parked) == END


def test_the_runner_isolates_the_store_from_production(tmp_path):
    """An eval writes an episode per task, and episodes are what the Analytics
    panel is computed from. A shared store would inject synthetic tasks into
    the production numbers the eval exists to explain."""
    import dataclasses

    @dataclasses.dataclass(frozen=True)
    class Cfg:
        dsn: str

    cfg = runner.eval_config(Cfg(dsn="postgresql://live/agent"), tmp_path)
    assert cfg.dsn.startswith("sqlite:///")
    assert "postgres" not in cfg.dsn


# --- the resting states that are not outcomes -----------------------------

BLOCKED = {"escalated": False, "review_gate_result": None, "cost_so_far": 0.0004,
           "pending_approval": {"action_requests": [{"name": "bash"}]}}
PARKED_ON_MERGE = {"escalated": False, "cost_so_far": 1.1, "iteration_count": 0,
                   "review_gate_result": {"verdict": "READY"},
                   "pending_merge_approval": {"sha": "abc", "repo": "demo"}}


@pytest.mark.asyncio
async def test_a_task_parked_on_a_command_approval_is_blocked_not_finished(run_kwargs):
    """What the first end-to-end run got wrong. verify_and_ship._is_terminal
    returns False for pending_approval, so no episode is written -- but the
    final state has no review_gate_result and is not escalated, so reading it
    as an outcome reported a task that had not started as one that had
    finished and correctly decided there was nothing to do."""
    run = await runner.run_task(task(), graph=FakeGraph(BLOCKED), **run_kwargs)
    assert run.outcome == "blocked"
    assert "bash" in run.escalation_reason
    assert not run.passed
    # Nothing was verified, so nothing is known -- and an unknown must not be
    # allowed to read as a pass.
    assert run.checks_pass is None


ASKED = {"escalated": False, "review_gate_result": None, "cost_so_far": 0.007,
         "pending_approval": {"action_requests": [{"name": "ask_user", "args": {
             "question": "Should an invalid jitter throw, or be ignored? Either could check out."}}]}}


@pytest.mark.asyncio
async def test_a_question_to_the_operator_is_an_escalation_carrying_the_question(run_kwargs):
    """The agent chose to ask for a human. As "blocked" it left the aggregate
    and the scorecard said nobody was asked; the question is also the only
    useful thing to show for the task."""
    run = await runner.run_task(task(), graph=FakeGraph(ASKED), **run_kwargs)
    assert run.outcome == "escalated"
    assert "invalid jitter throw" in run.escalation_reason
    assert not run.passed
    assert run.checks_pass is None      # "check" in the question says nothing about checks
    report = ev_report.build(make_suite([make_run("a", True), run]), cost_ceiling_usd=25.0)
    assert report["benchmarks"]["escalated"] == 1


@pytest.mark.asyncio
async def test_the_resting_state_the_harness_aims_for_is_a_ship(run_kwargs):
    """A READY verdict parked on the operator's final look. This is the
    intended terminal state of every eval task -- nothing merged."""
    run = await runner.run_task(task(), graph=FakeGraph(PARKED_ON_MERGE), **run_kwargs)
    assert run.outcome == "shipped"
    assert run.review_verdict == "READY"
    assert run.checks_pass is True


@pytest.mark.asyncio
async def test_commands_are_auto_approved_so_a_task_measures_coding(run_kwargs):
    """With approval required, every task parked on its first bash call and
    the suite measured the operator's responsiveness."""
    graph = FakeGraph(SHIPPED)
    await runner.run_task(task(), graph=graph, **run_kwargs)
    assert graph.invocations[0][0]["auto_approve_commands"] is True


@pytest.mark.asyncio
async def test_a_blocked_task_says_which_command_it_stopped_on(run_kwargs):
    """2026-10-22: "awaiting approval for: bash" could not be explained once
    the run's store was cleaned up. The report carries the command itself."""
    state = dict(BLOCKED, pending_approval={"action_requests": [
        {"name": "bash", "args": {"command": 'rm -rf "$d"'}}]})
    run = await runner.run_task(task(), graph=FakeGraph(state), **run_kwargs)
    assert run.outcome == "blocked"
    assert run.escalation_reason == 'parked awaiting approval for: bash: rm -rf "$d"'


def test_a_blocked_task_is_kept_out_of_the_aggregate():
    """It says nothing about the agent, and counting it as an escalation would
    blame the agent for an operator gate that never opened."""
    blocked = runner.TaskRun(task=task(id="c"), task_id="x", outcome="blocked",
                             escalation_reason="parked awaiting approval for: bash")
    report = ev_report.build(make_suite([make_run("a", True), blocked]), cost_ceiling_usd=25.0)
    assert report["benchmarks"]["tasks"] == 1
    assert report["benchmarks"]["escalated"] == 0
    assert any(t["outcome"] == "blocked" for t in report["tasks"])


# --- keeping the diff of a failing task ------------------------------------

def test_changed_diff_reads_the_committed_branch(demo_spec, tmp_path):
    mf = fx.materialize(demo_spec, tmp_path / "work")
    branch = fx.task_branch_name("abc-123")
    fx._git(["checkout", "-q", "-b", branch], mf.sandbox)
    (mf.sandbox / "src" / "a.py").write_text("x = 2\n")
    fx._git(["add", "-A"], mf.sandbox)
    fx._git(["commit", "-q", "-m", "change"], mf.sandbox)
    diff = fx.changed_diff(mf, branch)
    assert "-x = 1" in diff and "+x = 2" in diff


def test_changed_diff_falls_back_to_uncommitted_work(demo_spec, tmp_path):
    """An escalated task never commits, and its diff is the thing most worth
    reading."""
    mf = fx.materialize(demo_spec, tmp_path / "work")
    (mf.sandbox / "src" / "a.py").write_text("broken\n")
    assert "+broken" in fx.changed_diff(mf, fx.task_branch_name("never-committed"))


def test_a_huge_diff_is_truncated_rather_than_swallowing_the_report(demo_spec, tmp_path):
    mf = fx.materialize(demo_spec, tmp_path / "work")
    (mf.sandbox / "src" / "a.py").write_text("line\n" * 20_000)
    assert len(fx.changed_diff(mf, None)) <= fx.MAX_DIFF_CHARS


@pytest.mark.asyncio
async def test_a_failing_task_keeps_its_diff_and_a_passing_one_does_not(run_kwargs):
    """The first full run is why. py-top-n-heap failed a guard saying the test
    file must not change; the report recorded only that it was among the
    changed paths, so there was no telling whether the agent had ADDED a test
    or WEAKENED one -- opposite findings -- and the fixture was already gone."""
    failing = task(assertions=[Assertion("file_matches", {"path": "src/a.py", "pattern": "never"})])
    run = await runner.run_task(failing, graph=FakeGraph(SHIPPED), **run_kwargs)
    assert not run.passed
    assert isinstance(run.diff, str)          # captured, even when empty

    passing = task()
    ok = await runner.run_task(passing, graph=FakeGraph(SHIPPED), **run_kwargs)
    assert ok.passed and ok.diff == "", "a passing task's diff is noise"


def test_the_report_carries_the_diff_of_a_failing_task():
    r = make_run("b", False)
    r.diff = "--- a/tests/x.py\n+++ b/tests/x.py\n-    assert total == 60\n"
    row = ev_report.task_row(r)
    assert "assert total == 60" in row["diff"]
