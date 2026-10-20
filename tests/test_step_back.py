"""A task spending a lot without converging is asked to step back, once at
each checkpoint (agent/middleware/step_back.py), and a bug fix is verified by
an independent seat that can run code but not edit it."""
from __future__ import annotations

import subprocess
from types import SimpleNamespace

from langchain_core.messages import HumanMessage

from agent.middleware.step_back import StepBackMiddleware


class _Req:
    def __init__(self):
        self.messages = [HumanMessage("the task")]

    def override(self, messages):
        r = _Req()
        r.messages = messages
        return r


def _seen(mw):
    return [m.content for m in mw._augment(_Req()).messages[1:]]


def test_checkpoints_fire_once_each_on_spend_and_on_time():
    tracker = SimpleNamespace(budget_usd=3.0, total_cost=0.2)
    now = {"t": 0.0}
    mw = StepBackMiddleware(tracker, clock=lambda: now["t"])
    assert _seen(mw) == []
    tracker.total_cost = 1.05
    first = _seen(mw)
    assert len(first) == 1 and "=== CHECKPOINT ===" in first[0] and "$1.05 of its $3.00" in first[0]
    assert _seen(mw) == [], "each checkpoint fires once"
    now["t"] = 46 * 60
    assert len(_seen(mw)) == 1, "45 minutes into the pass"
    tracker.total_cost = 2.5
    now["t"] = 95 * 60
    assert len(_seen(mw)) == 1, "two due at once still say it once"
    assert _seen(mw) == []


def test_a_resumed_task_is_not_told_again_about_a_checkpoint_it_already_passed():
    tracker = SimpleNamespace(budget_usd=3.0, total_cost=1.5)
    mw = StepBackMiddleware(tracker, clock=lambda: 0.0)
    assert _seen(mw) == []
    tracker.total_cost = 2.1
    assert len(_seen(mw)) == 1


def test_scratch_is_ignored_by_git_in_every_worktree(tmp_path, monkeypatch):
    from agent.deep_agent import exclude_scratch
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x", "GIT_COMMITTER_NAME": "t",
                 "GIT_COMMITTER_EMAIL": "t@x", "GIT_CONFIG_GLOBAL": "/dev/null"}.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*a, cwd=repo):
        return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True, check=True).stdout

    git("init", "-q", "-b", "main")
    git("commit", "-q", "--allow-empty", "-m", "base")
    wt = tmp_path / "task"
    git("worktree", "add", "-q", "--detach", str(wt))
    exclude_scratch(str(wt))
    exclude_scratch(str(wt))
    (wt / ".scratch").mkdir()
    (wt / ".scratch" / "probe.py").write_text("print(1)\n")
    assert git("status", "--porcelain", cwd=wt) == "", "a probe in .scratch is invisible to git"
    assert (repo / ".git" / "info" / "exclude").read_text().count("/.scratch/") == 1
    exclude_scratch(None)
    exclude_scratch(str(tmp_path / "not-a-repo"))


def test_the_coordinator_verifies_a_fix_past_its_example_through_an_independent_seat():
    import inspect

    from agent import deep_agent
    prompt = deep_agent.COORDINATOR_SYSTEM_PROMPT_TEMPLATE
    assert "BUG FIXES -- VERIFY PAST THE EXAMPLE" in prompt and "`verifier`" in prompt
    assert "/workspace/.scratch/" in prompt
    src = inspect.getsource(deep_agent.build_deep_agent)
    assert "subagents=[general_purpose, investigator, test_writer, verifier]" in src
    spec = src[src.index("verifier = {"):src.index("# Explicit general-purpose subagent")]
    assert '"model": verifier_model' in spec, "a different model from the coder that wrote the fix"
    assert '"agent-verifier"' in src, "billed on its own alias, not the test-writer's (2026-09-25)"
    assert 'tool_by_name["write"]' not in spec and 'tool_by_name["edit"]' not in spec, "it runs code, never edits it"
    assert "StepBackMiddleware(tracker)" in src


def test_the_verifier_judges_the_reported_behaviour_not_regressions():
    """On 2026-09-24 the verifier found 232 inputs where the reported bug still
    happened, called them "pre-existing, not a regression", and a half-fix
    shipped."""
    from agent import deep_agent
    p = deep_agent.VERIFIER_SYSTEM_PROMPT
    assert "FAILURE OF THE FIX even if it failed before" in p
    assert "VERDICT: FIX HOLDS" in p and "VERDICT: FIX INCOMPLETE" in p
    assert "FIX INCOMPLETE means the fix is not done" in deep_agent.COORDINATOR_SYSTEM_PROMPT_TEMPLATE


def test_a_benchmark_fix_is_sent_back_once_if_the_verifier_never_saw_it():
    import inspect

    from agent.nodes import verify_and_ship as vs
    src = inspect.getsource(vs)
    gate = src[src.index('"benchmark fix not yet checked by the verifier"') - 400:]
    assert 'not state.get("verifier_runs") and not state.get("verifier_nudged")' in gate
    assert '"verifier_nudged": True' in gate
    assert src.index("benchmark fix not yet checked by the verifier") > src.index("if not checks[\"all_ok\"]")


def test_a_bounded_subagent_is_told_to_report_before_its_cap():
    """The verifier's cap ended one run with only "Tool call limit reached":
    its findings never reached the coordinator."""
    from langchain_core.messages import ToolMessage

    from agent.middleware.wrap_up import WrapUpMiddleware
    mw = WrapUpMiddleware(limit=30)
    outs = [mw.wrap_tool_call(SimpleNamespace(tool_call={"id": str(i)}),
                              lambda r: ToolMessage(content="ok", tool_call_id=r.tool_call["id"])).content
            for i in range(30)]
    noted = [i + 1 for i, c in enumerate(outs) if "[Persei harness]" in c]
    assert noted == [20, 26]
    assert "Send your report NOW, opening with the VERDICT line" in outs[25]
