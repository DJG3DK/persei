"""The GitHub inbox: discovery from a fake API, policy decisions, approve
links, and a full poll pass against a fake store."""
import base64
import secrets
import time
from types import SimpleNamespace

import pytest

from agent import github_inbox as gi
from agent import github_settings as gs


def _config():
    return SimpleNamespace(auth_secret_key=base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(), github_token="env-token-0123456789abcdef")


class FakeGitHub:
    """Just enough of the API surface discover() touches."""
    def __init__(self, prs=(), reviews=None, alerts=(), checks=(), default_branch="main", fail=(), workflow_runs=(), code_alerts=()):
        self.prs, self.reviews_by, self.alerts, self.checks = list(prs), reviews or {}, list(alerts), list(checks)
        self.default_branch, self.fail, self.workflow_runs_list = default_branch, set(fail), list(workflow_runs)
        self.code_alerts = list(code_alerts)

    async def code_scanning_alerts(self, slug):
        if "code" in self.fail:
            raise PermissionError("Code scanning alerts: read is missing")
        if "code-404" in self.fail:
            raise LookupError("no analysis found")
        return self.code_alerts

    async def open_prs(self, slug):
        if "prs" in self.fail:
            raise PermissionError("no")
        return self.prs

    async def reviews(self, slug, number):
        return self.reviews_by.get(number, [])

    async def dependabot_alerts(self, slug):
        if "alerts" in self.fail:
            raise PermissionError("Dependabot alerts: read is missing")
        return self.alerts

    async def repo(self, slug):
        return {"default_branch": self.default_branch}

    async def check_runs(self, slug, ref):
        if "checks" in self.fail:
            raise PermissionError("Checks: read is missing")
        return self.checks

    async def workflow_runs(self, slug, branch):
        if "actions" in self.fail:
            raise PermissionError("Actions: read is missing")
        return self.workflow_runs_list


def _pr(n, login, title="bump", sha="abc123def456789"):
    return {"number": n, "title": title, "html_url": f"https://gh/pr/{n}", "user": {"login": login},
            "head": {"sha": sha, "ref": f"dep/{n}"}, "base": {"ref": "main"}}


def _proj(**policies):
    p = dict(gs.DEFAULT_PROJECT)
    p["policies"] = {**gs.DEFAULT_PROJECT["policies"], **policies}
    return p


@pytest.mark.asyncio
async def test_discover_reads_every_switched_on_source_and_one_failure_hides_nothing():
    gh = FakeGitHub(
        prs=[_pr(1, "dependabot[bot]"), _pr(2, "renovate[bot]"), {**_pr(3, "danny"), "author_association": "OWNER"}],
        reviews={3: [{"id": 5, "state": "CHANGES_REQUESTED", "user": {"login": "reviewer"}},
                     {"id": 6, "state": "APPROVED", "user": {"login": "other"}}],
                 1: [{"id": 7, "state": "CHANGES_REQUESTED", "user": {"login": "r"}},
                     {"id": 8, "state": "APPROVED", "user": {"login": "r"}}]},   # later approval cancels it
        alerts=[{"number": 9, "state": "open", "html_url": "https://gh/alert/9",
                 "security_advisory": {"severity": "high", "summary": "RCE", "ghsa_id": "GHSA-1"},
                 "dependency": {"package": {"name": "multer"}, "manifest_path": "apps/api/package.json"},
                 "security_vulnerability": {"vulnerable_version_range": "< 2.0", "first_patched_version": {"identifier": "2.0.0"}}}],
        checks=[{"id": 11, "name": "ci", "conclusion": "failure", "head_sha": "feedfacefeed1234", "html_url": "https://gh/run/11", "output": {"title": "3 tests failed"}},
                {"id": 12, "name": "lint", "conclusion": "success", "head_sha": "feedfacefeed1234"}],
    )
    proj = _proj(dependabot_prs="propose", review_requests="propose", security_alerts="auto", ci_failures="propose")
    items = await gi.discover(gh, "proj", "o/proj", proj)
    keys = sorted(i.key for i in items)
    assert keys == ["alerts:multer@apps.api.package.json", "ci:feedfacefeed:ci", "pr:1", "review:3"]
    by = {i.key: i for i in items}
    assert by["alerts:multer@apps.api.package.json"].title.startswith("[HIGH] multer") and by["alerts:multer@apps.api.package.json"].number == 9
    assert "reviewer" in by["review:3"].summary
    assert by["ci:feedfacefeed:ci"].summary == "3 tests failed"

    # author filter widens to any bot, then anyone
    proj["authors"] = "bots"
    assert sorted(i.key for i in await gi.discover(gh, "proj", "o/proj", _proj(dependabot_prs="propose") | {"authors": "bots"})) == ["pr:1", "pr:2"]
    assert len([i for i in await gi.discover(gh, "proj", "o/proj", _proj(dependabot_prs="propose") | {"authors": "anyone"}) if i.kind == "dependabot_prs"]) == 3

    # the alerts endpoint refusing (missing permission) does not hide the PRs
    gh.fail.add("alerts")
    items = await gi.discover(gh, "proj", "o/proj", proj)
    assert "pr:1" in {i.key for i in items} and "alerts:multer@apps.api.package.json" not in {i.key for i in items}


def _item(key, kind="dependabot_prs", fp="a"):
    return gi.Item(key=key, kind=kind, repo="proj", title=key, url="u", fingerprint=fp, number=1)


def test_decide_new_items_follow_policy_and_the_auto_cap():
    proj = _proj(dependabot_prs="auto", security_alerts="propose", ci_failures="off") | {"max_open_auto": 2}
    found = [_item("pr:1"), _item("pr:2"), _item("pr:3"), _item("alert:1", "security_alerts"), _item("ci:x", "ci_failures")]
    decisions, gone = gi.decide({}, found, proj, open_auto=1)
    actions = {d.item.key: (d.action, d.item.state) for d in decisions}
    assert actions["pr:1"] == ("create", "task_created")
    assert actions["pr:2"] == ("propose", "proposed")          # cap: 2 max, 1 already open
    assert actions["pr:3"] == ("propose", "proposed")
    assert actions["alert:1"] == ("propose", "proposed")
    assert actions["ci:x"] == ("none", "seen")
    assert gone == []


def test_decide_unchanged_items_are_left_alone_and_vanished_ones_resolved():
    proj = _proj(dependabot_prs="propose")
    existing = {
        "pr:1": {"key": "pr:1", "fingerprint": "a", "state": "dismissed", "created_at": 1},
        "pr:2": {"key": "pr:2", "fingerprint": "a", "state": "proposed", "created_at": 1},
        "pr:9": {"key": "pr:9", "fingerprint": "a", "state": "proposed", "created_at": 1},
    }
    decisions, gone = gi.decide(existing, [_item("pr:1"), _item("pr:2")], proj, open_auto=0)
    assert decisions == []                      # nothing changed: no re-proposal, no spam
    assert gone == ["pr:9"]                     # merged/closed on GitHub

    # A new head commit is a new fingerprint: a dismissed PR comes back.
    decisions, _ = gi.decide(existing, [_item("pr:1", fp="b")], proj, open_auto=0)
    assert [(d.item.key, d.action) for d in decisions] == [("pr:1", "propose")]
    assert decisions[0].item.created_at == 1    # history kept

    # ...but one with a task on it keeps the task instead of stacking another.
    existing["pr:2"].update({"state": "task_created", "task_id": "t-1"})
    decisions, _ = gi.decide(existing, [_item("pr:2", fp="b")], proj, open_auto=0)
    assert decisions[0].action == "none" and decisions[0].item.task_id == "t-1"


def test_decide_reproposes_an_expired_snooze():
    proj = _proj(dependabot_prs="propose")
    existing = {"pr:1": {"key": "pr:1", "fingerprint": "a", "state": "snoozed", "snoozed_until": time.time() - 1, "created_at": 1}}
    decisions, _ = gi.decide(existing, [_item("pr:1")], proj, open_auto=0)
    assert [(d.action, d.reason) for d in decisions] == [("propose", "snooze expired")]
    existing["pr:1"]["snoozed_until"] = time.time() + 3600
    assert gi.decide(existing, [_item("pr:1")], proj, open_auto=0)[0] == []


def test_approve_links_are_signed_expiring_and_bound_to_one_item():
    # verify_approval raises reason CODES; the approve page maps them to wording.
    cfg = _config()
    tok = gi.sign_approval(cfg, "proj", "pr:1", "nonce1", "approve")
    data = gi.verify_approval(cfg, tok)
    assert (data["r"], data["k"], data["n"], data["a"]) == ("proj", "pr:1", "nonce1", "approve")

    with pytest.raises(ValueError, match="^expired$"):
        gi.verify_approval(cfg, gi.sign_approval(cfg, "proj", "pr:1", "n", "approve", ttl_s=-1))
    with pytest.raises(ValueError, match="^invalid$"):
        gi.verify_approval(_config(), tok)                       # another deployment's key
    with pytest.raises(ValueError, match="^invalid$"):
        body, mac = tok.split(".")
        gi.verify_approval(cfg, body[:-2] + "AA." + mac)          # tampered payload
    with pytest.raises(ValueError, match="^malformed$"):
        gi.verify_approval(cfg, "garbage")

    settings = gs.normalize({"public_url": "https://agent.example.com/v2"})
    assert gi.approval_url(settings, tok) == f"https://agent.example.com/v2/api/github/approve?t={tok}"
    assert gi.approval_url(gs.normalize(None), tok) is None


@pytest.mark.parametrize("missing", [None, ""])
def test_no_secret_key_signs_nothing_and_accepts_nothing(missing):
    # An empty key made an HMAC anyone can compute, so a link minted with it
    # would pass; an absent one raised TypeError, which the page served as a 500.
    cfg = SimpleNamespace(auth_secret_key=missing, github_token=None)
    with pytest.raises(ValueError, match="^invalid$"):
        gi.sign_approval(cfg, "proj", "pr:1", "n", "approve")
    with pytest.raises(ValueError, match="^invalid$"):
        gi.verify_approval(cfg, gi.sign_approval(_config(), "proj", "pr:1", "n", "approve"))


def test_goal_and_notification_text_name_the_gate():
    item = _item("pr:12")
    item.title, item.url, item.number, item.summary = "bump lodash", "https://gh/pr/12", 12, "dependabot: dep/12 -> main"
    goal = gi.build_goal(item, "PR #12: bump lodash\nstate: open")
    assert "pull request #12" in goal and "github_pull_request" in goal and "review gate" in goal
    assert "PR #12: bump lodash" in goal
    text = gi.proposal_text(item, "https://x/approve", "https://x/dismiss", 3.0)
    assert "Approve → https://x/approve" in text and "$3.00" in text and "Dismiss → https://x/dismiss" in text
    assert "dashboard" in gi.proposal_text(item, None, None, 3.0)


class FakeStore:
    def __init__(self):
        self.data = {}

    async def asearch(self, ns, limit=100):
        return [SimpleNamespace(key=k, value=v) for (n, k), v in self.data.items() if n == ns]

    async def aput(self, ns, key, value):
        self.data[(ns, key)] = dict(value)

    async def aget(self, ns, key):
        v = self.data.get((ns, key))
        return SimpleNamespace(key=key, value=v) if v is not None else None


@pytest.mark.asyncio
async def test_poll_project_creates_proposes_notifies_and_resolves(monkeypatch):
    cfg = _config()
    store = FakeStore()
    settings = gs.apply_patch(cfg, gs.normalize({"public_url": "https://agent.example.com/v2"}), {
        "projects": {"proj": {"policies": {"dependabot_prs": "auto", "security_alerts": "propose"}, "max_open_auto": 1, "budget_usd": 2.5}},
    })
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    monkeypatch.setattr(gi, "pr_text_for", _async_none)
    _with_checks(monkeypatch)
    created, notes = [], []

    async def create_task(repo, goal, budget, route):
        created.append((repo, goal, budget, route))
        return f"task-{len(created)}"

    async def notify(text, repo):
        notes.append(text)

    async def open_auto(repo):
        return 0

    gh = FakeGitHub(prs=[_pr(1, "dependabot[bot]", "bump a"), _pr(2, "dependabot[bot]", "bump b")],
                    alerts=[{"number": 9, "state": "open", "html_url": "u", "security_advisory": {"severity": "low", "summary": "x", "ghsa_id": "G"},
                             "dependency": {"package": {"name": "p"}}, "security_vulnerability": {}}])
    summary = await gi.poll_project(store, cfg, settings, "proj", create_task=create_task, notify=notify, open_auto_count=open_auto, client=gh)
    assert summary == {"repo": "proj", "found": 3, "proposed": 2, "created": 1, "resolved": 0}
    assert len(created) == 1 and created[0][2] == 2.5 and "pull request #1" in created[0][1]
    items = await gi.list_items(store, "proj")
    assert items["pr:1"]["state"] == "task_created" and items["pr:1"]["task_id"] == "task-1"
    assert items["pr:2"]["state"] == "proposed" and "cap" in items["pr:2"]["reason"]
    assert items["alerts:p@-"]["state"] == "proposed" and items["alerts:p@-"]["approval_nonce"]
    approve_notes = [n for n in notes if "Approve →" in n]
    assert len(approve_notes) == 2 and "/api/github/approve?t=" in approve_notes[0]
    assert any("started automatically" in n for n in notes)

    # The one audit entry with no person behind it. A task appeared, nobody
    # clicked anything, and without this the log shows the policy change and
    # then silence.
    from agent import audit
    entries = await audit.recent(store)
    auto = [e for e in entries if e["action"] == "inbox.auto_start"]
    assert len(auto) == 1, "an auto-started task must be recorded"
    assert auto[0]["actor"] == "github-inbox", "the policy acted, not an account"
    assert auto[0]["target"] == "proj/pr:1"
    assert auto[0]["task_id"] == "task-1"
    assert not [e for e in entries if e["action"] == "inbox.approve"], \
        "nobody approved anything here"

    # Second pass: nothing new, nothing re-sent; PR 2 merged -> resolved.
    notes.clear()
    gh.prs = [_pr(1, "dependabot[bot]", "bump a")]
    summary = await gi.poll_project(store, cfg, settings, "proj", create_task=create_task, notify=notify, open_auto_count=open_auto, client=gh)
    assert summary["proposed"] == 0 and summary["created"] == 0 and summary["resolved"] == 1
    assert notes == [] and len(created) == 1
    assert (await gi.list_items(store, "proj"))["pr:2"]["state"] == "resolved"


@pytest.mark.asyncio
async def test_poll_project_falls_back_to_a_proposal_when_auto_start_fails(monkeypatch):
    cfg = _config()
    store = FakeStore()
    settings = gs.apply_patch(cfg, gs.normalize(None), {"projects": {"proj": {"policies": {"dependabot_prs": "auto"}}}})
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    monkeypatch.setattr(gi, "pr_text_for", _async_none)
    _with_checks(monkeypatch)
    notes = []

    async def create_task(repo, goal, budget, route):
        raise RuntimeError("classifier down")

    async def notify(text, repo):
        notes.append(text)

    async def open_auto(repo):
        return 0

    gh = FakeGitHub(prs=[_pr(1, "dependabot[bot]")])
    summary = await gi.poll_project(store, cfg, settings, "proj", create_task=create_task, notify=notify, open_auto_count=open_auto, client=gh)
    assert summary["created"] == 0 and summary["proposed"] == 1
    item = (await gi.list_items(store, "proj"))["pr:1"]
    assert item["state"] == "proposed" and "auto start failed" in item["reason"]
    assert len(notes) == 1


@pytest.mark.asyncio
async def test_poll_project_skips_without_a_token_or_a_remote(monkeypatch):
    cfg = SimpleNamespace(auth_secret_key=_config().auth_secret_key, github_token=None)
    settings = gs.apply_patch(cfg, gs.normalize(None), {"projects": {"proj": {"policies": {"dependabot_prs": "auto"}}}})
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    out = await gi.poll_project(FakeStore(), cfg, settings, "proj", create_task=None, notify=None, open_auto_count=None)
    assert out["skipped"] == "no token"


def _with_checks(monkeypatch, value: bool | None = True):
    """The poll asks the review service whether the project verifies anything
    before it auto-starts work. In a test there is no review service, so say
    what the project is: with checks unless the test is about their absence."""
    import agent.tools.review_gate as rg

    async def _has_checks(repo):
        return value

    monkeypatch.setattr(rg, "project_has_checks", _has_checks)


async def _async_none(*a, **k):
    return None


@pytest.mark.asyncio
async def test_ci_failures_fall_back_to_actions_runs_when_check_runs_are_refused():
    tip, older = "aaaaaaaaaaaa1234", "bbbbbbbbbbbb5678"
    gh = FakeGitHub(fail={"checks"}, workflow_runs=[
        {"id": 1, "name": "CI", "conclusion": "failure", "head_sha": tip, "html_url": "https://gh/run/1", "display_title": "fix: thing", "event": "push", "run_number": 40},
        {"id": 2, "name": "Deploy", "conclusion": "success", "head_sha": tip},
        {"id": 3, "name": "CI", "conclusion": "failure", "head_sha": older},   # not the tip: history, not work
        {"id": 4, "name": "npm_and_yarn in /. for sharp - Update #1", "conclusion": "failure", "head_sha": tip, "event": "dynamic"},  # Dependabot's own job
    ])
    items = await gi.discover(gh, "proj", "o/proj", _proj(ci_failures="propose"))
    assert [i.key for i in items] == [f"ci:{tip[:12]}:CI"]
    assert "fix: thing" in items[0].summary and "#40" in items[0].summary

    # Neither permission: the source is empty and the others are untouched.
    gh = FakeGitHub(fail={"checks", "actions"}, prs=[_pr(1, "dependabot[bot]")])
    items = await gi.discover(gh, "proj", "o/proj", _proj(ci_failures="propose", dependabot_prs="propose"))
    assert [i.key for i in items] == ["pr:1"]


def _code_alert(n, rule_id, sev, path, line, msg="tainted", sha="cafebabe0000", tool="CodeQL"):
    return {"number": n, "state": "open", "html_url": f"https://gh/code/{n}",
            "rule": {"id": rule_id, "security_severity_level": sev, "description": f"{rule_id} description"},
            "tool": {"name": tool},
            "most_recent_instance": {"commit_sha": sha, "message": {"text": msg},
                                     "location": {"path": path, "start_line": line}}}


@pytest.mark.asyncio
async def test_code_scanning_alerts_group_by_rule_and_scope_to_the_repo():
    gh = FakeGitHub(code_alerts=[
        _code_alert(1, "js/double-escaping", "high", "apps/a/x.ts", 10),
        _code_alert(2, "js/double-escaping", "high", "apps/b/y.ts", 20),
        _code_alert(3, "js/request-forgery", "critical", "apps/api/z.ts", 30, msg="URL from supplier"),
        {**_code_alert(4, "js/request-forgery", "critical", "old.ts", 1), "state": "fixed"},   # not open: ignored
    ])
    items = await gi.discover(gh, "proj", "o/proj", _proj(code_scanning="propose"))
    assert [i.key for i in items] == ["code:js.request-forgery", "code:js.double-escaping"]   # critical first
    by = {i.key: i for i in items}
    assert by["code:js.double-escaping"].title == "[HIGH] js/double-escaping: js/double-escaping description (2 locations)"
    assert by["code:js.double-escaping"].number is None
    assert "#1 apps/a/x.ts:10" in by["code:js.double-escaping"].summary and "#2 apps/b/y.ts:20" in by["code:js.double-escaping"].summary
    assert "URL from supplier" in by["code:js.request-forgery"].summary
    assert by["code:js.request-forgery"].url == "https://github.com/o/proj/security/code-scanning?query=is%3Aopen+rule%3Ajs/request-forgery"
    assert all(i.repo == "proj" for i in items)
    assert "/" not in by["code:js.double-escaping"].key.split(":", 1)[1]   # key is one URL path segment

    # fixing one of two locations changes the fingerprint; fixing both removes the item
    before = by["code:js.double-escaping"].fingerprint
    gh.code_alerts = gh.code_alerts[1:]
    after = {i.key: i for i in await gi.discover(gh, "proj", "o/proj", _proj(code_scanning="propose"))}
    assert after["code:js.double-escaping"].fingerprint != before
    gh.code_alerts = [gh.code_alerts[1]]
    assert "code:js.double-escaping" not in {i.key for i in await gi.discover(gh, "proj", "o/proj", _proj(code_scanning="propose"))}

    # a repository that never ran code scanning (404) is quiet; a missing permission hides only this source
    gh.fail.add("code-404")
    assert await gi.discover(gh, "proj", "o/proj", _proj(code_scanning="propose")) == []
    gh.fail = {"code"}
    assert [i.key for i in await gi.discover(gh, "proj", "o/proj", _proj(code_scanning="propose", dependabot_prs="propose"))] == []
    assert await gi.discover(gh, "proj", "o/proj", _proj()) == []   # off: never fetched


def test_code_scanning_goal_scopes_the_task_to_this_repository_and_forbids_suppression():
    item = gi.code_scanning_items([_code_alert(7, "js/path-injection", "high", "apps/api/c.ts", 189)], "proj", "o/proj")[0]
    goal = gi.build_goal(item)
    assert "THIS repository" in goal and "do not look for or touch other projects" in goal
    assert "no dismissing alerts on GitHub" in goal and "#7 apps/api/c.ts:189" in goal
    assert "security/code-scanning?query=is%3Aopen+rule%3Ajs/path-injection" in goal
    assert gi.proposal_text(item, None, None, 3.0).startswith("🐙 GitHub: Code scanning alert on proj")


# ---------------------------------------------------------------------------
# Auto needs a gate with something in it (2026-09-11)
# ---------------------------------------------------------------------------

def test_auto_degrades_to_propose_when_a_project_has_no_checks():
    """A project whose review gate runs nothing mechanical has no automated
    verification behind it: a model's opinion would be the only thing between
    a GitHub alert and a diff waiting for a merge click. The operator loses
    one click and keeps the review that click is for."""
    proj = _proj(security_alerts="auto", dependabot_prs="auto")
    decisions, _ = gi.decide({}, [_item("alert:1", "security_alerts"), _item("pr:1")],
                             proj, open_auto=0, has_checks=False)
    assert [d.action for d in decisions] == ["propose", "propose"]
    assert all(d.item.state == "proposed" for d in decisions)
    assert all("no checks" in d.reason or "auto needs checks" in d.reason for d in decisions)


def test_auto_is_held_back_when_the_reviewer_cannot_be_reached():
    """Unconfirmed is not the same as none, and both fail the same way: the
    item is proposed, and the reason says which it was."""
    decisions, _ = gi.decide({}, [_item("pr:1")], _proj(dependabot_prs="auto"),
                             open_auto=0, has_checks=None)
    assert decisions[0].action == "propose"
    assert "could not confirm" in decisions[0].reason


def test_auto_still_works_for_a_project_with_checks():
    decisions, _ = gi.decide({}, [_item("pr:1")], _proj(dependabot_prs="auto"),
                             open_auto=0, has_checks=True)
    assert decisions[0].action == "create"


def test_propose_and_off_are_unaffected_by_the_checks_rule():
    """The rule is about starting work unattended, not about listing it."""
    decisions, _ = gi.decide({}, [_item("pr:1"), _item("alert:1", "security_alerts")],
                             _proj(dependabot_prs="propose", security_alerts="off"),
                             open_auto=0, has_checks=False)
    actions = {d.item.key: d.action for d in decisions}
    assert actions["pr:1"] == "propose"
    assert actions["alert:1"] == "none"


@pytest.mark.asyncio
async def test_a_full_poll_starts_nothing_on_a_project_without_checks(monkeypatch):
    """End to end through poll_project: Auto is configured, the project has
    no checks, and the pass must propose rather than create."""
    cfg = _config()
    store = FakeStore()
    settings = gs.apply_patch(cfg, gs.normalize(None),
                              {"projects": {"proj": {"policies": {"dependabot_prs": "auto"}}}})
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    monkeypatch.setattr(gi, "pr_text_for", _async_none)
    _with_checks(monkeypatch, value=False)
    created, notes = [], []

    async def create_task(repo, goal, budget, route):
        created.append(repo)
        return "task-1"

    async def notify(text, repo):
        notes.append(text)

    async def open_auto(repo):
        return 0

    gh = FakeGitHub(prs=[_pr(1, "dependabot[bot]")])
    summary = await gi.poll_project(store, cfg, settings, "proj", create_task=create_task,
                                    notify=notify, open_auto_count=open_auto, client=gh)

    assert created == [], "auto started work on a project that verifies nothing"
    assert summary["proposed"] == 1 and summary["created"] == 0
    item = (await gi.list_items(store, "proj"))["pr:1"]
    assert item["state"] == "proposed" and "checks" in item["reason"]
    assert notes and "Approve" in notes[0]


# ---------------------------------------------------------------------------
# The registry is the contract (docs/playbooks/add-an-inbox-source.md)
#
# A name in SOURCES switches on a card in the settings UI. If discover() does
# not look for it, that switch is a lie: the operator turns the source to
# Auto, nothing is ever found, and the absence looks exactly like "no items
# right now". These are the tests that fail first when a source is added.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_every_source_in_the_registry_is_one_discover_looks_for():
    gh = FakeGitHub(
        prs=[_pr(1, "dependabot[bot]")],
        reviews={1: [{"id": 5, "state": "CHANGES_REQUESTED", "user": {"login": "reviewer"}}]},
        alerts=[{"number": 9, "state": "open", "html_url": "https://gh/alert/9",
                 "security_advisory": {"severity": "high", "summary": "RCE", "ghsa_id": "GHSA-1"},
                 "dependency": {"package": {"name": "multer"}},
                 "security_vulnerability": {"vulnerable_version_range": "< 2", "first_patched_version": {"identifier": "2.0.0"}}}],
        checks=[{"id": 11, "name": "ci", "conclusion": "failure", "head_sha": "feedfacefeed1234",
                 "html_url": "https://gh/run/11", "output": {"title": "3 tests failed"}}],
        code_alerts=[{"number": 21, "state": "open", "html_url": "https://gh/code/21",
                      "rule": {"id": "js/sql-injection", "security_severity_level": "high",
                               "description": "SQL injection"},
                      "most_recent_instance": {"location": {"path": "api/db.js", "start_line": 12}}}],
    )
    proj = _proj(**{name: "propose" for name in gs.SOURCES})
    found = {i.kind for i in await gi.discover(gh, "proj", "o/proj", proj)}
    missing = set(gs.SOURCES) - found
    assert not missing, (
        f"these sources are switchable in the settings card and discover() never "
        f"produces them, so switching them on finds nothing forever: {sorted(missing)}")


@pytest.mark.parametrize("source", sorted(gs.SOURCES))
def test_every_source_can_become_a_task_and_a_notification(source):
    """A discovered item with no goal template raises a KeyError inside the
    poller, and one with no label notifies the operator about a blank."""
    assert source in gi._GOAL_TEMPLATES, "build_goal would raise KeyError on this kind"
    assert source in gi._KIND_LABEL, "the proposal notification would name nothing"
    goal = gi.build_goal(_item(f"{source}:1", kind=source))
    assert "review gate" in goal, "every inbox goal states that the gate still applies"


# ---------------------------------------------------------------------------
# an item whose task is no longer running comes back to the queue
# ---------------------------------------------------------------------------
#
# `task_created` was a one-way door. Whatever became of the task -- stopped,
# errored, finished without fixing anything -- the item kept that state, and
# the dashboard offers no action on it (actionable is proposed/snoozed/seen).
# So the alert sat in the list with no button while it was still open on
# GitHub, and the only way back was editing the store by hand.
#
# Observed 2026-09-22: a task on a js/sql-injection alert went down a rabbit
# hole, was stopped, and took its alert out of reach with it. With 59 open
# alerts on one project that is not an edge case, it is a Tuesday.

def _created(key, task_id="t1", fp="a"):
    return {"key": key, "fingerprint": fp, "state": "task_created",
            "task_id": task_id, "created_at": 1}


def test_an_item_whose_task_was_stopped_is_proposed_again():
    """The stranded case: the ALERT did not change, the TASK died -- so this
    is the unchanged-fingerprint branch, not the changed one."""
    proj = _proj(dependabot_prs="propose")
    decisions, _ = gi.decide({"pr:1": _created("pr:1")}, [_item("pr:1")], proj,
                             open_auto=0, live_tasks=set())
    assert [(d.action, d.item.state) for d in decisions] == [("propose", "proposed")]
    assert "without fixing it" in decisions[0].reason


def test_an_item_whose_task_is_still_running_is_left_alone():
    proj = _proj(dependabot_prs="propose")
    decisions, _ = gi.decide({"pr:1": _created("pr:1")}, [_item("pr:1")], proj,
                             open_auto=0, live_tasks={"t1"})
    assert decisions == []


def test_a_changed_item_whose_task_died_goes_through_policy_again():
    """The other branch: the alert moved on AND nobody is working it."""
    proj = _proj(dependabot_prs="auto") | {"max_open_auto": 2}
    decisions, _ = gi.decide({"pr:1": _created("pr:1", fp="old")}, [_item("pr:1", fp="new")],
                             proj, open_auto=0, live_tasks=set(), has_checks=True)
    assert [(d.action, d.item.state) for d in decisions] == [("create", "task_created")]


def test_a_changed_item_whose_task_lives_still_does_not_stack_a_second():
    proj = _proj(dependabot_prs="auto") | {"max_open_auto": 2}
    decisions, _ = gi.decide({"pr:1": _created("pr:1", fp="old")}, [_item("pr:1", fp="new")],
                             proj, open_auto=0, live_tasks={"t1"}, has_checks=True)
    assert [(d.action, d.item.state) for d in decisions] == [("none", "task_created")]


def test_without_a_lookup_nothing_changes():
    """live_tasks=None means "we could not ask". Re-proposing on a guess
    would duplicate work; the old behaviour is the safe default."""
    proj = _proj(dependabot_prs="propose")
    assert gi.decide({"pr:1": _created("pr:1")}, [_item("pr:1")], proj, open_auto=0)[0] == []


def test_an_empty_set_is_a_real_answer_and_not_the_same_as_none():
    """The distinction the whole fix rests on: nothing running is a fact,
    could-not-ask is not."""
    proj = _proj(dependabot_prs="propose")
    none_says = gi.decide({"pr:1": _created("pr:1")}, [_item("pr:1")], proj, open_auto=0,
                          live_tasks=None)[0]
    empty_says = gi.decide({"pr:1": _created("pr:1")}, [_item("pr:1")], proj, open_auto=0,
                           live_tasks=set())[0]
    assert none_says == [] and len(empty_says) == 1


def test_an_off_policy_item_is_not_dragged_back_into_the_queue():
    """A source the operator turned off stays off, however its task ended."""
    proj = _proj(dependabot_prs="off")
    assert gi.decide({"pr:1": _created("pr:1")}, [_item("pr:1")], proj, open_auto=0,
                     live_tasks=set())[0] == []


def test_a_dismissed_item_is_not_resurrected():
    """Only task_created comes back. Dismissed was a decision."""
    proj = _proj(dependabot_prs="propose")
    existing = {"pr:1": {"key": "pr:1", "fingerprint": "a", "state": "dismissed", "created_at": 1}}
    assert gi.decide(existing, [_item("pr:1")], proj, open_auto=0, live_tasks=set())[0] == []


def test_the_original_creation_time_survives_the_round_trip():
    """The list sorts on it; resetting it would shuffle an old alert to the
    top as though it were new."""
    proj = _proj(dependabot_prs="propose")
    decisions, _ = gi.decide({"pr:1": _created("pr:1")}, [_item("pr:1")], proj,
                             open_auto=0, live_tasks=set())
    assert decisions[0].item.created_at == 1


def test_a_finished_task_does_not_put_its_item_back_in_the_queue():
    """The bug this rule shipped with, caught live the same evening.

    A task fixed the finding, committed, pushed and opened a pull request --
    status `done`. The first version of this rule asked "is a task running
    right now", got no, and re-proposed the item: the operator saw finished
    work sitting in the inbox asking to be done again, labelled "its task is
    no longer running".

    `done` means the fix is merged or waiting in a PR. The alert stays open on
    GitHub until the scanner re-runs, and that delay is not a reason to ask
    for the work a second time.
    """
    from agent.server import _TASK_HANDLED_STATUSES
    assert "done" in _TASK_HANDLED_STATUSES
    assert set(_TASK_HANDLED_STATUSES) == {"running", "queued", "awaiting_approval",
                                           "awaiting_merge", "escalated", "done"}
    # Only a task that ended WITHOUT delivering releases its item.
    for ended_empty in ("stopped", "error"):
        assert ended_empty not in _TASK_HANDLED_STATUSES


def test_a_done_task_keeps_its_item_out_of_the_queue():
    """End to end through decide(), which is where it actually mattered."""
    proj = _proj(dependabot_prs="propose")
    # "handled" is what the server passes: the done task's id is in the set.
    decisions, _ = gi.decide({"pr:1": _created("pr:1", task_id="finished")},
                             [_item("pr:1")], proj, open_auto=0, live_tasks={"finished"})
    assert decisions == []


def test_a_stopped_tasks_item_still_comes_back():
    """The case the rule was written for, unchanged."""
    proj = _proj(dependabot_prs="propose")
    decisions, _ = gi.decide({"pr:1": _created("pr:1", task_id="stopped-one")},
                             [_item("pr:1")], proj, open_auto=0, live_tasks=set())
    assert [(d.action, d.item.state) for d in decisions] == [("propose", "proposed")]
    assert "without fixing it" in decisions[0].reason


# ---------------------------------------------------------------------------
# a code-scanning goal carries the code, not just its coordinates
# ---------------------------------------------------------------------------
#
# The goal used to carry the rule id, a list of file:line, and one sentence
# per location -- everything except the thing the task is about. Three
# attempts on 2026-09-22 took the rule id as the subject, went and read the
# analyser's own query source to work out what it meant, and never opened the
# controller they had been handed the line number for. Prompt guidance telling
# them not to held for about six minutes.
#
# The alert already knows the file and the line, and reading them is a file
# read rather than a judgement. Deterministic on purpose: no model call, no
# drift between runs, and an unreadable location is skipped rather than
# guessed at.

SUMMARY = (
    "CodeQL · high · 3 open alerts for rule js/sql-injection\n"
    "#5 src/a.js:3 — This query object depends on a user-provided value.\n"
    "#6 src/a.js:9 — Also user-provided.\n"
    "#7 src/b.js:2 — And here.\n"
)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text("".join(f"line{n}\n" for n in range(1, 41)))
    (tmp_path / "src" / "b.js").write_text("bee1\nbee2\nbee3\n")
    return tmp_path


def test_locations_are_parsed_out_of_the_rendered_summary():
    assert gi.parse_locations(SUMMARY) == [
        ("src/a.js", 3, "This query object depends on a user-provided value."),
        ("src/a.js", 9, "Also user-provided."),
        ("src/b.js", 2, "And here."),
    ]


def test_a_summary_with_no_locations_yields_nothing(repo):
    assert gi.parse_locations("CodeQL · high · 0 alerts") == []
    assert gi.code_for_locations(str(repo), "no locations here") == ""


def test_the_flagged_line_is_marked_and_surrounded_by_context(repo):
    out = gi.code_for_locations(str(repo), "#1 src/b.js:2 — And here.")
    assert "bee2  <-- flagged" in out
    assert "bee1" in out and "bee3" in out          # context either side
    assert "And here." in out                        # the alert's own message


def test_two_alerts_close_together_become_one_excerpt(repo):
    """Lines 3 and 9 are six apart, so their ±6 windows overlap. Printing two
    blocks would repeat the same four lines and read as two problems."""
    out = gi.code_for_locations(str(repo), SUMMARY)
    assert out.count("--- src/a.js") == 1
    assert out.count("<-- flagged") == 3             # both in a.js, one in b.js


def test_a_location_that_cannot_be_read_is_skipped_not_guessed(repo):
    out = gi.code_for_locations(str(repo), "#1 src/gone.js:5 — vanished.\n#2 src/b.js:2 — here.")
    assert "src/gone.js" not in out
    assert "bee2" in out


def test_a_path_cannot_escape_the_repository(repo):
    """The path comes from GitHub. A goal builder is not a place to open an
    arbitrary absolute path."""
    out = gi.code_for_locations(str(repo), "#1 ../../etc/passwd:1 — nice try.")
    assert out == ""


def test_the_excerpt_is_bounded(repo):
    big = "\n".join(f"#{n} src/a.js:{n} — hit." for n in range(1, 40))
    assert len(gi.code_for_locations(str(repo), big)) <= gi._MAX_SNIPPET_CHARS + 500


def test_the_goal_gains_the_code_and_says_not_to_research_the_analyser(repo):
    item = {"kind": "code_scanning", "repo": "proj", "number": None,
            "title": "[HIGH] js/sql-injection: x", "url": "u", "summary": SUMMARY}
    plain = gi.build_goal(item)
    withcode = gi.build_goal(item, repo_root=str(repo))
    assert "bee2  <-- flagged" in withcode and "bee2" not in plain
    assert "the flagged code, read from this repository" in withcode
    # The instruction lands whether or not the code could be read -- it is in
    # the template, not the appendix.
    for g in (plain, withcode):
        assert "Do not download, read or reason about the" in g
        assert ".qll" in g


def test_a_repo_that_cannot_be_read_produces_exactly_the_old_goal(tmp_path):
    """No repo_root, or an empty one, and nothing changes. The enrichment is
    additive or it is absent."""
    item = {"kind": "code_scanning", "repo": "proj", "number": None,
            "title": "t", "url": "u", "summary": SUMMARY}
    assert gi.build_goal(item) == gi.build_goal(item, repo_root=str(tmp_path / "nope"))


def test_other_kinds_are_untouched(repo):
    """Only code_scanning names lines. A dependabot goal must not grow a
    code appendix from a summary that never had locations in it."""
    item = {"kind": "security_alerts", "repo": "proj", "number": 7,
            "title": "t", "url": "u", "summary": "#5 src/a.js:3 — not a code-scanning item"}
    assert gi.build_goal(item) == gi.build_goal(item, repo_root=str(repo))


def _dep_alert(n, pkg, manifest, sev, rng, patched, state="open"):
    return {"number": n, "state": state, "html_url": f"https://github.com/o/proj/security/dependabot/{n}",
            "security_advisory": {"severity": sev, "summary": f"{pkg} bug {n}", "ghsa_id": f"GHSA-{n}"},
            "dependency": {"package": {"name": pkg}, "manifest_path": manifest},
            "security_vulnerability": {"vulnerable_version_range": rng, "first_patched_version": {"identifier": patched}}}


async def test_security_alerts_group_by_package_and_manifest():
    """2026-09-29: eleven undici advisories on one lockfile became eleven
    tasks; one merge closed all eleven and the other ten rebased and
    re-reviewed for nothing. One package in one manifest is one item."""
    gh = FakeGitHub(alerts=[
        _dep_alert(6, "undici", "frontend/package-lock.json", "medium", "< 8.10.2", "8.10.2"),
        _dep_alert(9, "undici", "frontend/package-lock.json", "high", ">= 8.10.0, < 8.11.2", "8.11.2"),
        _dep_alert(7, "multer", "apps/api/package.json", "high", "< 2.0", "2.0.0"),
        _dep_alert(8, "undici", "package-lock.json", "low", "< 8.10.2", "8.10.2"),          # another manifest
        _dep_alert(5, "undici", "frontend/package-lock.json", "low", "< 8.1", "8.1.0", state="fixed"),  # closed: ignored
    ])
    items = await gi.discover(gh, "proj", "o/proj", _proj(security_alerts="propose"))
    by = {i.key: i for i in items}
    assert sorted(by) == ["alerts:multer@apps.api.package.json", "alerts:undici@frontend.package-lock.json", "alerts:undici@package-lock.json"]
    group = by["alerts:undici@frontend.package-lock.json"]
    assert group.number is None and group.kind == "security_alerts"
    assert group.title == "[HIGH] undici: 2 open alerts in frontend/package-lock.json, patched in 8.11.2"
    assert group.summary.splitlines()[0].startswith("undici in frontend/package-lock.json: bump to at least 8.11.2")
    assert "#6 [MEDIUM] undici bug 6" in group.summary and "#9 [HIGH] undici bug 9" in group.summary
    assert "/" not in group.key.split(":", 1)[1], "the key is one URL path segment"
    assert gi.alert_numbers(group.to_dict()) == [6, 9]
    assert gi.alert_reference(group.to_dict()) == "alerts #6 and #9"
    # A lone alert keeps its own title, number and page, under the same kind of key (B36).
    assert by["alerts:multer@apps.api.package.json"].number == 7 and by["alerts:multer@apps.api.package.json"].title.startswith("[HIGH] multer")
    assert gi.alert_reference(by["alerts:multer@apps.api.package.json"].to_dict()) == "alert #7"
    goal = gi.build_goal(group)
    assert goal.startswith("Fix Dependabot security alerts #6 and #9: [HIGH] undici: 2 open alerts")
    assert "bump to at least 8.11.2" in goal and "https://github.com/o/proj/security/dependabot" in goal
    assert gi.build_goal(by["alerts:multer@apps.api.package.json"]).startswith("Fix Dependabot security alert #7: [HIGH] multer")
    # Closing one of the two changes the fingerprint; the key stays, so the
    # item keeps its task and attempts (B36).
    before = group.fingerprint
    gh.alerts = [a for a in gh.alerts if a["number"] != 6]
    after = {i.key: i for i in await gi.discover(gh, "proj", "o/proj", _proj(security_alerts="propose"))}
    lone = after["alerts:undici@frontend.package-lock.json"]
    assert lone.number == 9 and lone.fingerprint != before



@pytest.mark.asyncio
async def test_tektonix_never_offers_its_own_pull_requests_back_to_itself():
    """2026-09-30, with authors set to "anyone": every task's own PR came back
    as a new inbox task, which opened another PR for the next poll."""
    own = {**_pr(43, "operator"), "head": {"ref": "agent/b426d85c-132b-4ed6-95ba-286c24b94617", "sha": "a" * 40},
           "title": "Land the dependency update proposed in GitHub pull request #42"}
    backup = {**_pr(44, "operator"), "head": {"ref": "agent/b426d85c-132b-4ed6-95ba-286c24b94617-pre-rebase-backup", "sha": "b" * 40}}
    person = {**_pr(45, "operator"), "head": {"ref": "agent/add-dark-mode", "sha": "c" * 40}}
    bot = _pr(46, "dependabot[bot]")
    gh = FakeGitHub(prs=[own, backup, person, bot])
    for authors in ("anyone", "bots", "dependabot"):
        keys = {i.key for i in await gi.discover(gh, "proj", "o/proj", _proj(dependabot_prs="propose") | {"authors": authors})}
        assert "pr:43" not in keys and "pr:44" not in keys, authors
    keys = {i.key for i in await gi.discover(gh, "proj", "o/proj", _proj(dependabot_prs="propose") | {"authors": "anyone"})}
    assert keys == {"pr:45", "pr:46"}, "a person's own branch named agent/... is still theirs"


# --- a finished task whose fix GitHub did not accept (2026-10-16) ----------

def _code_item(fp="a"):
    item = gi.code_scanning_items([_code_alert(201, "js/loop-bound-injection", "high", "src/w.js", 85)],
                                  "proj", "o/r")[0]
    item.fingerprint = fp
    return item


def _done(key, task_id="t1", fp=None):
    return {"key": key, "fingerprint": fp or _code_item().fingerprint, "state": "task_created",
            "task_id": task_id, "created_at": 1}


def test_a_finished_tasks_item_waits_for_a_scan_newer_than_the_task():
    """The fix merged at 1000; code scanning last ran at 900 (or not at all):
    the item says it is waiting, keeps its task, and offers no button yet."""
    item = _code_item()
    proj = _proj(code_scanning="propose")
    for scanned in (None, 900.0, 1000.0 + gi.SCAN_MARGIN_S - 1):
        decisions, gone = gi.decide({item.key: _done(item.key)}, [_code_item()], proj, open_auto=0,
                                    live_tasks={"t1"}, finished_tasks={"t1": 1000.0},
                                    scanned_at=scanned, now=5000.0)
        assert gone == []
        assert [(d.action, d.item.state, d.item.task_id) for d in decisions] == [("none", "awaiting_rescan", "t1")]
        assert "waiting for GitHub to re-scan" in decisions[0].reason


def test_after_the_scan_a_still_open_alert_comes_back_with_the_attempt_recorded():
    item = _code_item()
    decisions, _ = gi.decide({item.key: _done(item.key)}, [_code_item()], _proj(code_scanning="propose"),
                             open_auto=0, live_tasks={"t1"}, finished_tasks={"t1": 1000.0},
                             scanned_at=2000.0, now=5000.0)
    (d,) = decisions
    assert (d.action, d.item.state, d.item.task_id) == ("propose", "proposed", None)
    assert d.item.attempts == [{"task_id": "t1", "finished_at": 1000.0, "still_open": [201]}]
    assert "task t1 merged its fix, but GitHub still reports 1 alert after re-scanning" in d.reason


def test_an_alert_gone_after_the_fix_resolves_as_before():
    item = _code_item()
    _, gone = gi.decide({item.key: {**_done(item.key), "state": "awaiting_rescan"}}, [], _proj(code_scanning="propose"),
                        open_auto=0, live_tasks={"t1"}, finished_tasks={"t1": 1000.0}, scanned_at=2000.0, now=5000.0)
    assert gone == [item.key]


def test_waiting_unchanged_writes_nothing():
    item = _code_item()
    existing = {item.key: {**_done(item.key), "state": "awaiting_rescan"}}
    decisions, _ = gi.decide(existing, [_code_item()], _proj(code_scanning="propose"), open_auto=0,
                             live_tasks={"t1"}, finished_tasks={"t1": 1000.0}, scanned_at=None, now=5000.0)
    assert decisions == []


def test_other_kinds_wait_a_fixed_time_instead_of_a_scan():
    proj = _proj(security_alerts="propose")
    existing = {"alert:7": {"key": "alert:7", "fingerprint": "a", "state": "task_created", "task_id": "t1", "created_at": 1}}
    found = lambda: [_item("alert:7", "security_alerts")]  # noqa: E731
    early, _ = gi.decide(existing, found(), proj, open_auto=0, live_tasks={"t1"},
                         finished_tasks={"t1": 1000.0}, now=1000.0 + gi.SETTLE_S - 1)
    late, _ = gi.decide(existing, found(), proj, open_auto=0, live_tasks={"t1"},
                        finished_tasks={"t1": 1000.0}, now=1000.0 + gi.SETTLE_S)
    assert early[0].item.state == "awaiting_rescan" and late[0].item.state == "proposed"


def test_an_item_with_a_rejected_fix_is_never_auto_started_again(monkeypatch):
    """Auto with room to spare: a fix GitHub keeps rejecting must not start
    task after task by itself."""
    proj = _proj(code_scanning="auto") | {"max_open_auto": 5}
    item = _code_item()
    settled, _ = gi.decide({item.key: _done(item.key)}, [_code_item()], proj, open_auto=0, has_checks=True,
                           live_tasks={"t1"}, finished_tasks={"t1": 1000.0}, scanned_at=2000.0, now=5000.0)
    assert settled[0].action == "propose"
    # ...and later, when the alert itself changes, still a proposal.
    stored = settled[0].item.to_dict()
    again, _ = gi.decide({item.key: stored}, [_code_item(fp="changed")], proj, open_auto=0, has_checks=True,
                         live_tasks=set(), finished_tasks={"t1": 1000.0}, scanned_at=2000.0, now=6000.0)
    assert [(d.action, d.item.state) for d in again] == [("propose", "proposed")]
    assert again[0].item.attempts and "earlier fix did not clear it" in again[0].reason


def test_a_running_task_is_still_left_alone():
    item = _code_item()
    decisions, _ = gi.decide({item.key: _done(item.key, task_id="t2")}, [_code_item()], _proj(code_scanning="propose"),
                             open_auto=0, live_tasks={"t2"}, finished_tasks={"t1": 1000.0}, scanned_at=9000.0, now=9000.0)
    assert decisions == []


def test_the_next_tasks_goal_carries_the_earlier_attempt():
    item = _code_item().to_dict() | {"attempts": [{"task_id": "3320a0ee-x", "finished_at": 1000.0, "still_open": [201]}]}
    goal = gi.build_goal(item)
    assert "earlier attempts that GitHub did not accept" in goal
    assert "Task 3320a0ee finished and merged its fix" in goal and "#201" in goal
    assert "does not introduce a new finding" in goal
    assert "earlier attempts" not in gi.build_goal(_code_item())


@pytest.mark.asyncio
async def test_the_last_scan_time_is_read_for_the_default_branch():
    class GH(gi.GitHubClient):
        def __init__(self):
            self.asked = None

        async def repo(self, slug):
            return {"default_branch": "trunk"}

        async def get(self, path, params=None):
            self.asked = (path, params)
            return [{"created_at": "2026-10-16T18:35:00Z"}]
    gh = GH()
    assert await gh.last_scan_at("o/r") == 1792175700.0
    assert gh.asked == ("/repos/o/r/code-scanning/analyses", {"ref": "refs/heads/trunk", "per_page": 1})



# ---------------------------------------------------------------------------
# audit 2026-10-22: B35 (every item), B36 (stable Dependabot keys), B37
# (settle per kind)
# ---------------------------------------------------------------------------

class PagedStore(FakeStore):
    """Pages like Postgres: newest updated first, at most `cap` rows a call,
    honouring offset. `asearch(limit=200)` saw only the newest 200."""

    def __init__(self, cap=100):
        super().__init__()
        self.cap = cap

    async def asearch(self, ns, limit=10, offset=0):
        rows = sorted(((k, v) for (n, k), v in self.data.items() if n == ns),
                      key=lambda kv: kv[1].get("updated_at", 0), reverse=True)
        return [SimpleNamespace(key=k, value=v) for k, v in rows[offset:offset + min(limit, self.cap)]]

    async def adelete(self, ns, key):
        self.data.pop((ns, key), None)


async def _poll(store, gh, proj_patch, *, live=(), finished=None):
    cfg = _config()
    settings = gs.apply_patch(cfg, gs.normalize({}), {"projects": {"proj": proj_patch}})
    created = []

    async def create_task(repo, goal, budget, route):
        created.append(goal)
        return f"task-{len(created)}"

    async def notify(text, repo):
        return None

    async def open_auto(repo):
        return 0

    async def live_tasks(repo):
        return set(live)

    async def finished_tasks(repo):
        return dict(finished or {})

    summary = await gi.poll_project(store, cfg, settings, "proj", create_task=create_task, notify=notify,
                                    open_auto_count=open_auto, client=gh, live_tasks=live_tasks,
                                    finished_tasks=finished_tasks)
    return summary, created


@pytest.mark.asyncio
async def test_the_oldest_of_201_items_is_listed_actionable_and_resolved(monkeypatch):
    """B35: list_items read the 200 most recently updated rows. The 201st
    vanished from the list, approve/dismiss/snooze answered 404, and the
    poll never resolved it."""
    from agent.routers import github as github_routes

    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    _with_checks(monkeypatch)
    store = PagedStore()
    ns = (gi.NAMESPACE, "proj")
    for n in range(201):
        store.data[(ns, f"ci:{n:012d}:ci")] = {"key": f"ci:{n:012d}:ci", "kind": "ci_failures", "repo": "proj",
                                               "title": f"ci {n}", "state": "proposed", "fingerprint": "f",
                                               "updated_at": 1000.0 + n, "created_at": 1.0}
    oldest = "ci:000000000000:ci"
    assert len(await gi.list_items(store, "proj")) == 201
    app = SimpleNamespace(state=SimpleNamespace(store=store))
    out = await github_routes._github_act(app, "proj", oldest, "snooze", days=1, actor="me")
    assert out["item"]["state"] == "snoozed"

    summary, _ = await _poll(store, FakeGitHub(checks=[]), {"policies": {"ci_failures": "propose"}})
    assert summary["resolved"] == 201
    assert store.data[(ns, oldest)]["state"] == "resolved"


@pytest.mark.asyncio
async def test_resolved_items_are_pruned_after_a_month(monkeypatch):
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    _with_checks(monkeypatch)
    store = PagedStore()
    ns = (gi.NAMESPACE, "proj")
    old = time.time() - gi.PRUNE_RESOLVED_AFTER_S - 60
    store.data[(ns, "ci:old:ci")] = {"key": "ci:old:ci", "kind": "ci_failures", "state": "resolved", "updated_at": old}
    store.data[(ns, "ci:new:ci")] = {"key": "ci:new:ci", "kind": "ci_failures", "state": "resolved",
                                     "updated_at": time.time()}
    store.data[(ns, "pr:1")] = {"key": "pr:1", "kind": "dependabot_prs", "state": "dismissed", "updated_at": old,
                                "fingerprint": "abc123def456"}
    await _poll(store, FakeGitHub(prs=[_pr(1, "dependabot[bot]")]), {"policies": {"dependabot_prs": "propose"}})
    keys = {k for (_, k) in store.data}
    assert "ci:old:ci" not in keys and "ci:new:ci" in keys
    assert store.data[(ns, "pr:1")]["state"] == "dismissed", "dismissed and still open on GitHub stays dismissed"


@pytest.mark.asyncio
async def test_a_second_advisory_on_the_package_keeps_the_task_already_on_it(monkeypatch):
    """B36 (grow): a task ran on `alert:5`; a second advisory arrived and the
    new group item started a second task while `alert:5` was resolved as
    no longer open."""
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    _with_checks(monkeypatch)
    store = PagedStore()
    ns = (gi.NAMESPACE, "proj")
    store.data[(ns, "alert:5")] = {"key": "alert:5", "kind": "security_alerts", "repo": "proj", "number": 5,
                                   "title": "t", "state": "task_created", "task_id": "t1", "fingerprint": "open:GHSA-5",
                                   "approval_nonce": None, "attempts": [], "updated_at": 1000.0, "created_at": 900.0}
    gh = FakeGitHub(alerts=[_dep_alert(5, "undici", "package-lock.json", "high", "< 8", "8.0.0"),
                            _dep_alert(6, "undici", "package-lock.json", "low", "< 8.1", "8.1.0")])
    summary, created = await _poll(store, gh, {"policies": {"security_alerts": "auto"}, "max_open_auto": 5},
                                   live={"t1"})
    assert created == [] and summary["resolved"] == 0
    items = await gi.list_items(store, "proj")
    assert set(items) == {"alerts:undici@package-lock.json"}
    item = items["alerts:undici@package-lock.json"]
    assert item["state"] == "task_created" and item["task_id"] == "t1" and item["created_at"] == 900.0


@pytest.mark.asyncio
async def test_a_group_shrinking_to_one_alert_waits_for_its_finished_task(monkeypatch):
    """B36 (shrink): a group's task finished with one alert left, and
    `alert:9` started at once with no settle wait and no attempts."""
    monkeypatch.setattr(gi, "resolve_slug", lambda repo: "o/proj")
    _with_checks(monkeypatch)
    store = PagedStore()
    ns = (gi.NAMESPACE, "proj")
    key = "alerts:undici@package-lock.json"
    store.data[(ns, key)] = {"key": key, "kind": "security_alerts", "repo": "proj", "number": None, "title": "t",
                             "summary": "undici\n#6 [LOW] a\n#9 [HIGH] b", "state": "task_created", "task_id": "t1",
                             "fingerprint": "old", "attempts": [], "updated_at": 1000.0, "created_at": 900.0}
    gh = FakeGitHub(alerts=[_dep_alert(9, "undici", "package-lock.json", "high", "< 8", "8.0.0")])
    _, created = await _poll(store, gh, {"policies": {"security_alerts": "auto"}, "max_open_auto": 5},
                             live={"t1"}, finished={"t1": time.time() - 60})
    assert created == []
    assert store.data[(ns, key)]["state"] == "awaiting_rescan" and store.data[(ns, key)]["task_id"] == "t1"


@pytest.mark.asyncio
async def test_an_approve_link_naming_the_old_alert_key_finds_the_item():
    store = PagedStore()
    ns = (gi.NAMESPACE, "proj")
    store.data[(ns, "alerts:undici@x")] = {"key": "alerts:undici@x", "kind": "security_alerts", "number": 5}
    assert (await gi.get_item(store, "proj", "alert:5"))["key"] == "alerts:undici@x"
    assert await gi.get_item(store, "proj", "alert:6") is None


def _review_item(review_id=5, head="abc123def456"):
    return gi.Item(key="review:3", kind="review_requests", repo="proj", title="t", url="u", number=3,
                   fingerprint=f"{head}:{review_id}")


def test_a_review_item_stays_with_its_finished_task_until_a_new_review():
    """B37: a merged fix never clears a CHANGES_REQUESTED review, so the
    item came back 45 minutes later as "GitHub still reports it"."""
    proj = _proj(review_requests="propose")
    existing = {"review:3": {**_review_item().to_dict(), "state": "task_created", "task_id": "t1"}}
    later = 1000.0 + gi.SETTLE_S * 10
    # The task pushed to the branch: a new head, the same review.
    same, _ = gi.decide(existing, [_review_item(head="fffffffff000")], proj, open_auto=0, live_tasks={"t1"},
                        finished_tasks={"t1": 1000.0}, now=later)
    assert [(d.action, d.item.state) for d in same] == [("none", "awaiting_rescan")]
    stored = {"review:3": same[0].item.to_dict()}
    quiet, _ = gi.decide(stored, [_review_item(head="fffffffff000")], proj, open_auto=0, live_tasks={"t1"},
                         finished_tasks={"t1": 1000.0}, now=later + 1)
    assert quiet == []
    again, _ = gi.decide(stored, [_review_item(review_id=9, head="fffffffff000")], proj, open_auto=0,
                         live_tasks={"t1"}, finished_tasks={"t1": 1000.0}, now=later + 2)
    assert [(d.action, d.item.state) for d in again] == [("propose", "proposed")]
    assert "new review" in again[0].reason and again[0].item.attempts[0]["task_id"] == "t1"
    goal = gi.build_goal(again[0].item)
    assert "asked for changes again" in goal and "scanner" not in goal


def test_a_dependabot_pr_item_stays_with_its_task_until_the_pr_closes():
    """B37: Dependabot is often slower than 45 minutes to close a PR the fix
    superseded, and it may rebase it first."""
    proj = _proj(dependabot_prs="propose")
    existing = {"pr:1": {**_item("pr:1", fp="a").to_dict(), "state": "task_created", "task_id": "t1"}}
    late, _ = gi.decide(existing, [_item("pr:1", fp="rebased")], proj, open_auto=0, live_tasks={"t1"},
                        finished_tasks={"t1": 1000.0}, now=1000.0 + gi.SETTLE_S * 20)
    assert [(d.action, d.item.state) for d in late] == [("none", "awaiting_rescan")]
    assert "pull request to close" in late[0].reason
    closed, gone = gi.decide({"pr:1": late[0].item.to_dict()}, [], proj, open_auto=0, live_tasks={"t1"},
                             finished_tasks={"t1": 1000.0}, now=1000.0 + gi.SETTLE_S * 21)
    assert closed == [] and gone == ["pr:1"]


def test_a_returning_dependabot_alert_gets_dependabot_advice_not_a_scanners():
    item = {"kind": "security_alerts", "title": "[HIGH] undici", "url": "u", "number": 9, "summary": "s",
            "attempts": [{"task_id": "3320a0ee-x", "finished_at": 1000.0, "still_open": [9]}]}
    goal = gi.build_goal(item)
    assert "GitHub still reports #9." in goal and "lockfile" in goal
    assert "scanner" not in goal and "re-scanning" not in goal
    ci = {"kind": "ci_failures", "title": "ci failed", "url": "u", "summary": "s",
          "attempts": [{"task_id": "3320a0ee-x", "finished_at": 1000.0, "still_open": []}]}
    assert "still reports the check failing" in gi.build_goal(ci)



@pytest.mark.asyncio
async def test_an_outsiders_pull_request_is_not_a_review_item():
    """A21 (2026-10-22): review requests had no author filter, so any
    outsider's PR with a changes-requested review became work, and its body
    reached the goal and the memory consolidation prompt."""
    review = [{"id": 5, "state": "CHANGES_REQUESTED", "user": {"login": "maintainer"}}]
    prs = [{**_pr(1, "stranger"), "author_association": "FIRST_TIME_CONTRIBUTOR"},
           {**_pr(2, "colleague"), "author_association": "COLLABORATOR"},
           {**_pr(3, "dependabot[bot]"), "author_association": "NONE"},
           {**_pr(4, "someone"), "author_association": "CONTRIBUTOR"}]
    gh = FakeGitHub(prs=prs, reviews={n: review for n in (1, 2, 3, 4)})
    keys = {i.key for i in await gi.discover(gh, "proj", "o/proj", _proj(review_requests="propose"))}
    assert keys == {"review:2", "review:3"}
    keys = {i.key for i in await gi.discover(gh, "proj", "o/proj",
                                             _proj(review_requests="propose") | {"authors": "anyone"})}
    assert keys == {"review:1", "review:2", "review:3", "review:4"}, "the operator can widen it"
