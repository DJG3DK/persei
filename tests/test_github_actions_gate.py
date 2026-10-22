"""The base branch moves only on a green GitHub Actions run.

After the review gate and the operator pass a commit, a push project pushes the
task branch, opens its pull request, and waits for every workflow run on that
exact commit: green merges, red goes back to the agent, anything else stops
with the commit unmerged.
"""
from __future__ import annotations

import pytest

from agent.tools import github_ci

pytestmark = pytest.mark.asyncio


def _run(id_, name, status="completed", conclusion="success", event="pull_request", workflow_id=None):
    return {"id": id_, "name": name, "status": status, "conclusion": conclusion, "event": event,
            "workflow_id": workflow_id or name, "html_url": f"https://github.com/o/r/actions/runs/{id_}"}


# ── verdict ────────────────────────────────────────────────────────────────

async def test_all_green_passes_and_one_red_fails_without_waiting_for_the_rest():
    assert github_ci.verdict([_run(1, "CI"), _run(2, "CodeQL")]) == ("passed", [])
    state, failed = github_ci.verdict([_run(1, "CI", conclusion="failure"),
                                       _run(2, "CodeQL", status="in_progress", conclusion=None)])
    assert state == "failed" and [r["name"] for r in failed] == ["CI"]
    assert github_ci.verdict([_run(1, "CI", status="queued", conclusion=None)])[0] == "pending"
    assert github_ci.verdict([])[0] == "none"


async def test_a_run_superseded_by_a_newer_one_does_not_count():
    """cancel-in-progress cancels the older run of the same workflow; its
    `cancelled` says nothing about the commit."""
    runs = [_run(1, "CI", conclusion="cancelled"), _run(5, "CI")]
    assert github_ci.verdict(runs) == ("passed", [])


async def test_skipped_and_neutral_do_not_block():
    assert github_ci.verdict([_run(1, "CI", conclusion="skipped"), _run(2, "Lint", conclusion="neutral")])[0] == "passed"


# ── the gate against a fake GitHub ─────────────────────────────────────────

class _Resp:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("x", request=None, response=None)


def _wire(monkeypatch, *, polls, workflows=".github/workflows/ci.yml", origin="https://github.com/o/r.git",
          token="ghp_SECRET", jobs=None, project=None):
    import agent.config as agent_config
    import agent.tools.git as gitmod
    from agent import github_repos, github_settings

    monkeypatch.setitem(agent_config.PROJECTS, "demo", project or {"live": "/tmp/live", "ship": "push"})
    monkeypatch.setattr(agent_config, "load_config", lambda: type("C", (), {"github_token": token})())
    monkeypatch.setattr(github_settings, "current", lambda: {})
    monkeypatch.setattr(github_settings, "token_for", lambda s, c, r: token)

    seen = {"pushes": [], "prs": [], "polls": 0}

    async def fake_git(cmd, root, timeout=30, extra_env=None):
        if cmd.startswith("ls-tree"):
            return {"ok": True, "output": workflows}
        if cmd.startswith("config --local"):
            return {"ok": True, "output": origin}
        if " push " in f" {cmd} ":
            seen["pushes"].append(cmd)
            return {"ok": True, "output": ""}
        return {"ok": True, "output": ""}
    monkeypatch.setattr(gitmod, "_git", fake_git)

    async def fake_pr(tok, slug, head, base, title, body=""):
        seen["prs"].append((slug, head, base))
        return {"number": "3", "url": "https://github.com/o/r/pull/3", "state": "open"}
    monkeypatch.setattr(github_repos, "open_pull_request", fake_pr)

    class Client:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False

        async def get(self, url, **kw):
            if url.endswith("/jobs"):
                return _Resp(200, {"jobs": jobs or []})
            seen["polls"] += 1
            nxt = polls[min(seen["polls"], len(polls)) - 1]
            return nxt if isinstance(nxt, _Resp) else _Resp(200, {"workflow_runs": nxt})
    monkeypatch.setattr(github_ci.httpx, "AsyncClient", Client)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(github_ci.asyncio, "sleep", no_sleep)
    return seen


async def _gate(**kw):
    return await github_ci.gate_on_actions("demo", "agent/t1", "a" * 40, "Do a thing",
                                           timeout=kw.pop("timeout", 3600), **kw)


async def test_green_after_pending_passes_with_the_pull_request(monkeypatch):
    seen = _wire(monkeypatch, polls=[[_run(1, "CI", status="in_progress", conclusion=None)], [_run(1, "CI")]])
    ci = await _gate()
    assert ci == {"ok": True, "passed": ["CI"], "pull_request": "https://github.com/o/r/pull/3"}
    assert seen["prs"] == [("o/r", "agent/t1", "main")]
    assert "+agent/t1:refs/heads/agent/t1" in seen["pushes"][0], "the task branch is force-pushed, not main"
    assert "ghp_SECRET" not in seen["pushes"][0], "the token never reaches the command line"


async def test_a_green_read_is_believed_only_when_the_next_poll_shows_the_same_runs(monkeypatch):
    """Workflows register their runs one at a time. The first poll after the
    push could find one finished workflow and none of the others yet, and
    "everything on this commit passed" was true of a set still growing
    (2026-09-29). Green counts when two polls in a row agree on which runs."""
    seen = _wire(monkeypatch, polls=[
        [_run(1, "CI")],                                                   # Lint has not registered yet
        [_run(1, "CI"), _run(2, "Lint", status="in_progress", conclusion=None)],
        [_run(1, "CI"), _run(2, "Lint")],
        [_run(1, "CI"), _run(2, "Lint")],
    ])
    ci = await _gate()
    assert ci == {"ok": True, "passed": ["CI", "Lint"], "pull_request": "https://github.com/o/r/pull/3"}
    assert seen["polls"] == 4, "one green read was enough to merge"


async def test_a_late_red_run_still_fails_the_gate(monkeypatch):
    _wire(monkeypatch, polls=[[_run(1, "CI")], [_run(1, "CI"), _run(2, "Lint", conclusion="failure")]])
    ci = await _gate()
    assert ci["ok"] is False and ci["reason"] == "failed"


async def test_red_reports_the_failing_job_and_step(monkeypatch):
    jobs = [{"name": "Backend", "status": "completed", "conclusion": "failure",
             "html_url": "https://github.com/o/r/actions/runs/1/job/9",
             "steps": [{"name": "npm ci", "conclusion": "success"}, {"name": "npm run lint", "conclusion": "failure"}]},
            {"name": "Frontend", "status": "completed", "conclusion": "success", "steps": []}]
    _wire(monkeypatch, polls=[[_run(1, "CI", conclusion="failure")]], jobs=jobs)
    ci = await _gate()
    assert ci["ok"] is False and ci["reason"] == "failed"
    assert [(f["job"], f["steps"]) for f in ci["failed"]] == [("Backend", ["npm run lint"])]
    assert "CI / Backend" in github_ci.describe_failures(ci["failed"])


async def test_no_workflows_skips_without_pushing(monkeypatch):
    seen = _wire(monkeypatch, polls=[[]], workflows="")
    ci = await _gate()
    assert ci["ok"] and "no GitHub Actions workflows" in ci["skipped"]
    assert seen["pushes"] == [] and seen["prs"] == []


async def test_ci_gate_false_in_projects_json_skips(monkeypatch):
    seen = _wire(monkeypatch, polls=[[]], project={"live": "/tmp/live", "ci_gate": False})
    assert (await _gate())["skipped"] == "ci_gate is off for this project"
    assert seen["pushes"] == []


async def test_workflows_that_start_no_run_are_let_through_after_the_grace(monkeypatch):
    _wire(monkeypatch, polls=[[]])
    ci = await _gate(start_grace=0)
    assert ci["ok"] and "no GitHub Actions run started" in ci["skipped"]


async def test_a_token_that_cannot_read_actions_stops_with_the_permission_named(monkeypatch):
    _wire(monkeypatch, polls=[_Resp(403, {"message": "Resource not accessible"})])
    ci = await _gate()
    assert ci["ok"] is False and ci["reason"] == "unreadable"
    assert "Actions: read" in ci["error"]


async def test_no_token_skips_and_says_main_is_not_gated(monkeypatch):
    """An SSH project with a deploy key and no token ships as it always did:
    the gate cannot read Actions without a token, and blocking every approved
    commit on that would break an install that never set one up."""
    seen = _wire(monkeypatch, polls=[[]], token=None)
    ci = await _gate()
    assert ci["ok"] and "no GitHub token" in ci["skipped"] and seen["pushes"] == []


async def test_ci_that_never_finishes_times_out(monkeypatch):
    _wire(monkeypatch, polls=[[_run(1, "CI", status="in_progress", conclusion=None)]])
    ci = await _gate(timeout=0)
    assert ci["ok"] is False and ci["reason"] == "timeout"


def _stale(id_, name):
    """A run GitHub still calls in progress, started an hour ago."""
    return dict(_run(id_, name, status="in_progress", conclusion=None), run_started_at="2026-10-22T17:14:03Z")


def _job(conclusion="success", status="completed"):
    return {"name": "analyze", "status": status, "conclusion": conclusion, "steps": [], "html_url": ""}


async def test_a_long_pending_run_whose_jobs_all_passed_counts_as_passed(monkeypatch):
    """2026-10-22: a CodeQL run's jobs were green at 17:16 and the run said
    in_progress until 18:34; the gate waited out its hour and escalated."""
    _wire(monkeypatch, polls=[[_run(1, "CI"), _stale(2, "CodeQL")]], jobs=[_job(), _job("skipped")])
    ci = await _gate()
    assert ci["ok"] is True and ci["passed"] == ["CI", "CodeQL"]


async def test_a_long_pending_run_with_a_red_job_fails(monkeypatch):
    _wire(monkeypatch, polls=[[_run(1, "CI"), _stale(2, "CodeQL")]], jobs=[_job(), _job("failure")])
    ci = await _gate()
    assert ci["ok"] is False and ci["reason"] == "failed"


async def test_a_long_pending_run_with_a_job_still_running_keeps_waiting(monkeypatch):
    _wire(monkeypatch, polls=[[_stale(2, "CodeQL")]], jobs=[_job(), _job(None, status="in_progress")])
    ci = await _gate(timeout=0)
    assert ci["ok"] is False and ci["reason"] == "timeout"


async def test_a_fresh_pending_run_is_not_second_guessed():
    runs = [dict(_run(1, "CI", status="in_progress", conclusion=None), run_started_at="2026-10-22T17:14:03Z")]
    started = github_ci._epoch("2026-10-22T17:14:03Z")

    class NoJobs:
        async def get(self, *a, **k):
            raise AssertionError("a run younger than the threshold is not asked about its jobs")
    out = await github_ci._settle_from_jobs(NoJobs(), "t", "o/r", runs, now=started + 60)
    assert out == runs


# ── the ship gate acting on it ─────────────────────────────────────────────

def _ship(monkeypatch, ci):
    import agent.config as agent_config
    from agent.nodes import verify_and_ship as vs
    from agent.outer_state import initial_state

    async def ret(value):
        return value

    def fake(value):
        async def f(*a, **k):
            return value
        return f

    async def no_branch(*a, **k):
        return {"ok": True, "branch": "agent/t1", "output": ""}

    monkeypatch.setitem(agent_config.PROJECTS, "test-repo", {"live": "/tmp/live", "ship": "push"})
    monkeypatch.setattr(vs, "commits_ahead", fake(0))
    monkeypatch.setattr(vs, "ensure_task_branch", no_branch)
    monkeypatch.setattr(vs, "run_all_checks", fake({"all_ok": True, "summary": ""}))
    monkeypatch.setattr(vs, "git_diff", fake("diff --git a/x b/x\n+1"))
    monkeypatch.setattr(vs, "git_commit", fake({"ok": True}))
    monkeypatch.setattr(vs, "rebase_onto_base", fake({}))
    monkeypatch.setattr(vs, "current_sha", fake("deadbeef"))
    monkeypatch.setattr(vs, "trigger_check", fake(None))
    monkeypatch.setattr(vs, "wait_for_review", fake({"verdict": "READY", "summary": "ok", "findings": []}))
    monkeypatch.setattr(vs, "autodetect_checks_if_none", fake(None))
    monkeypatch.setattr(vs, "gate_on_actions", fake(ci))
    merged = []

    async def merge(repo, branch=None):
        merged.append(branch)
        return {"ok": True, "merge": {"push": {"ok": True}}, "restart": {"built": []}}
    monkeypatch.setattr(vs, "merge_and_deploy", merge)
    state = initial_state(task_id="t1", goal="do the thing", repo="test-repo", budget_usd=1.0)
    state.update(require_merge_review=True, merge_approved_sha="deadbeef")
    return vs, state, merged


async def test_green_ci_merges_and_says_it_pushed(monkeypatch):
    vs, state, merged = _ship(monkeypatch, {"ok": True, "passed": ["CI"], "pull_request": "u"})
    result = await vs._verify_and_ship(state, config=None)
    assert merged == ["agent/t1"]
    summaries = [e["summary"] for e in result["execution_log"]]
    assert "GitHub Actions passed on deadbeef: CI" in summaries
    assert summaries[-1] == "merged and pushed to GitHub"
    assert result["merge_approved_sha"] is None


async def test_red_ci_does_not_merge_and_sends_the_failure_to_the_agent(monkeypatch):
    failed = [{"workflow": "CI", "job": "Backend", "steps": ["npm run lint"], "conclusion": "failure", "url": "u"}]
    vs, state, merged = _ship(monkeypatch, {"ok": False, "reason": "failed", "failed": failed, "pull_request": "u"})
    result = await vs._verify_and_ship(state, config=None)
    assert merged == []
    assert "npm run lint" in result["pending_feedback"]
    assert result["merge_approved_sha"] is None, "the fix is a new commit and needs its own approval"
    assert result["committed_sha"] == "deadbeef" and not result.get("escalated")


async def test_a_stopped_wait_escalates_unmerged_and_keeps_the_approval(monkeypatch):
    vs, state, merged = _ship(monkeypatch, {"ok": False, "reason": "timeout", "error": "did not finish"})
    result = await vs._verify_and_ship(state, config=None)
    assert merged == [] and result["escalated"] is True
    assert "merge_approved_sha" not in result, "a resume goes straight back to waiting"
    assert result["committed_sha"] == "deadbeef"
    assert any("stopped (timeout)" in e["summary"] for e in result["execution_log"])


async def test_a_failed_origin_push_is_on_the_summary_line():
    from agent.nodes import verify_and_ship as vs
    assert vs._merged_summary({"ok": True, "origin_push": {"ok": False, "reason": "no GitHub token"}}) \
        == "merged locally, but GitHub was NOT updated: no GitHub token"
    assert vs._merged_summary({"ok": True, "merge": {"push": {"ok": False, "error": "denied"}}}) \
        .endswith("NOT updated: denied")
    assert vs._merged_summary({"ok": True}) == "merged and deployed"
