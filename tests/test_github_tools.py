"""Read-only GitHub pull-request tools (agent/tools/github_tools.py):
slug resolution from the checkout's remote, formatting of what the model
sees, the no-token case, and wiring into both agents."""

import pytest

import agent.tools.github_tools as gh
from agent.tools.github_tools import (
    format_pull_request, format_pull_request_list, make_github_tools, repo_slug_from_remote,
)


def test_slug_from_ssh_and_https_remotes():
    assert repo_slug_from_remote("git@github.com:owner/webapp.git") == "owner/webapp"
    assert repo_slug_from_remote("https://github.com/DJG3DK/persei") == "DJG3DK/persei"
    assert repo_slug_from_remote("https://github.com/DJG3DK/persei.git\n") == "DJG3DK/persei"
    # HTTPS with userinfo (a PAT in the URL, or an insteadOf rewrite):
    assert repo_slug_from_remote("https://x-access-token:secret@github.com/DJG3DK/persei.git") == "DJG3DK/persei"
    # A deploy key per project means an SSH host alias per project (2026-09-10:
    # two of three live projects resolved to no slug at all until this).
    assert repo_slug_from_remote("git@github-storefront:owner/storefront.com.git") == "owner/storefront.com"
    assert repo_slug_from_remote("ssh://git@github.com-work/owner/repo.git") == "owner/repo"
    assert repo_slug_from_remote("git@gitlab.com:x/y.git") is None
    assert repo_slug_from_remote("") is None


def test_resolve_slug_uses_the_configured_remote_not_an_insteadof_rewrite(tmp_path, monkeypatch):
    """Same class of insteadOf rewrite as deploy_keys.status: the slug must
    still resolve when git remote get-url would return a token HTTPS URL."""
    import subprocess

    live = tmp_path / "proj"
    live.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=live, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:owner/proj.git"],
                   cwd=live, check=True)
    cfg = tmp_path / "gitconfig"
    cfg.write_text(
        '[url "https://x-access-token:super-secret-token@github.com/"]\n'
        "\tinsteadOf = git@github.com:\n"
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(gh, "PROJECTS", {"proj": {"live": str(live), "sandbox": str(live)}})
    monkeypatch.setattr(gh, "_slug_cache", {})
    assert gh.resolve_slug("proj") == "owner/proj"


def _data():
    return {
        "pr": {"number": 12, "title": "Audit fixes", "state": "open", "draft": False, "user": {"login": "reviewer"},
               "head": {"ref": "audit/fixes", "sha": "abcdef1234567890"}, "base": {"ref": "main"},
               "additions": 10, "deletions": 3, "changed_files": 2, "html_url": "https://github.com/o/r/pull/12",
               "body": "Addresses the S1-S3 audit findings."},
        "diff": "diff --git a/src/a.js b/src/a.js\n+const x = 1;\n",
        "review_comments": [{"path": "src/a.js", "line": 42, "user": {"login": "auditor"}, "body": "S1: this can divide by zero"}],
        "issue_comments": [{"user": {"login": "danny"}, "body": "please also fix S3"}],
        "reviews": [{"state": "CHANGES_REQUESTED", "user": {"login": "auditor"}, "body": "three findings"}],
        "checks": [{"name": "CI", "status": "completed", "conclusion": "failure"}],
    }


def test_full_format_carries_every_section_the_model_needs():
    out = format_pull_request(_data(), "all")
    assert out.startswith("PR #12: Audit fixes")
    assert "audit/fixes -> main" in out and "+10 -3 in 2 files" in out
    assert "DESCRIPTION:\nAddresses the S1-S3 audit findings." in out
    assert "- CI: completed / failure" in out
    assert "[CHANGES_REQUESTED] auditor: three findings" in out
    assert "- src/a.js:42 (auditor): S1: this can divide by zero" in out
    assert "- danny: please also fix S3" in out
    assert "DIFF:\ndiff --git a/src/a.js" in out


def test_parts_narrow_the_output():
    assert "DIFF:" not in format_pull_request(_data(), "summary")
    assert "src/a.js:42" in format_pull_request(_data(), "summary")
    d = format_pull_request(_data(), "diff")
    assert "DIFF:" in d and "REVIEW COMMENTS" not in d
    assert "COMMENTS: none" in format_pull_request({**_data(), "review_comments": [], "issue_comments": [], "reviews": []}, "comments")


def test_a_huge_diff_is_truncated_with_a_way_forward():
    data = {**_data(), "diff": "x" * 100_000}
    out = format_pull_request(data, "diff")
    assert "diff truncated at 60000 chars of 100000" in out


def test_list_format():
    prs = [{"number": 16, "state": "open", "draft": False, "title": "chore: prettier", "user": {"login": "d"}, "head": {"ref": "chore/prettier"}, "base": {"ref": "main"}}]
    assert "- #16 [open] chore: prettier -- d, chore/prettier -> main" in format_pull_request_list("o/r", prs, "open")
    assert format_pull_request_list("o/r", [], "closed") == "No closed pull requests on o/r."


def test_no_token_means_no_tools():
    assert make_github_tools(None) == [] and make_github_tools("") == []


def test_availability_counts_a_token_stored_from_the_dashboard(monkeypatch):
    """There are two places a token can live and the enablement rule has to
    know both. agent/capabilities.py briefly kept its own copy that checked
    the environment variable alone, so `doctor` reported the tools missing
    on a box where a per-project token from Settings -> GitHub had them
    live. One rule, read from here."""
    from agent import github_settings

    config = type("C", (), {"github_token": None})()
    monkeypatch.setattr(github_settings, "_cache", github_settings.normalize(None))
    assert gh.available(config) is False

    with_token = github_settings.normalize(None)
    with_token["tokens"]["work"] = {"enc": "ciphertext"}
    monkeypatch.setattr(github_settings, "_cache", with_token)
    assert gh.available(config) is True


def test_the_capability_line_asks_the_tools_rather_than_re_deciding(monkeypatch):
    """A second copy of "are the GitHub tools on" is how an operator gets
    told the wrong thing about a subsystem that is working."""
    import agent.capabilities as caps

    monkeypatch.setattr(gh, "available", lambda config: True)
    assert caps._github_tools() is True
    monkeypatch.setattr(gh, "available", lambda config: False)
    assert caps._github_tools() is False


def test_tools_resolve_repo_via_remote_and_report_errors_as_text(monkeypatch):
    monkeypatch.setattr(gh, "PROJECTS", {"demo": {"sandbox": "/nowhere"}})
    monkeypatch.setattr(gh, "_slug_cache", {})
    monkeypatch.setattr(gh, "resolve_slug", lambda repo: "o/r")
    calls = []

    def fake_fetch(token, slug, number):
        calls.append((token, slug, number))
        return _data()

    monkeypatch.setattr(gh, "fetch_pull_request", fake_fetch)
    tools = {t.name: t for t in make_github_tools("tok")}
    out = tools["github_pull_request"].invoke({"repo": "demo", "number": 12, "part": "summary"})
    assert calls == [("tok", "o/r", 12)] and out.startswith("PR #12")
    assert tools["github_pull_request"].invoke({"repo": "unknown", "number": 1}).startswith("ERROR: unknown or inaccessible repo")
    assert tools["github_pull_request"].invoke({"repo": "demo", "number": 1, "part": "bogus"}).startswith("PR #12"), "an unknown part falls back to all"


def test_not_found_and_auth_failures_are_text_not_exceptions(monkeypatch):
    monkeypatch.setattr(gh, "PROJECTS", {"demo": {"sandbox": "/nowhere"}})
    monkeypatch.setattr(gh, "resolve_slug", lambda repo: "o/r")

    def not_found(token, slug, number):
        raise LookupError("not found (or the token has no access to this repository)")

    monkeypatch.setattr(gh, "fetch_pull_request", not_found)
    tools = {t.name: t for t in make_github_tools("tok")}
    assert tools["github_pull_request"].invoke({"repo": "demo", "number": 999}).startswith("ERROR: not found")


def test_allowed_repos_scope_is_enforced(monkeypatch):
    monkeypatch.setattr(gh, "PROJECTS", {"a": {"sandbox": "/x"}, "b": {"sandbox": "/y"}})
    monkeypatch.setattr(gh, "resolve_slug", lambda repo: "o/r")
    monkeypatch.setattr(gh, "fetch_pull_request", lambda t, s, n: _data())
    tools = {t.name: t for t in make_github_tools("tok", allowed_repos=["a"])}
    assert tools["github_pull_request"].invoke({"repo": "b", "number": 1}).startswith("ERROR")
    assert tools["github_pull_request"].invoke({"repo": "a", "number": 1}).startswith("PR #12")


def test_planner_and_coder_get_the_tools_only_with_a_token():
    import inspect
    import agent.deep_agent as da
    import agent.planning_chat as pc
    # Both seats resolve the token per project through Settings -> GitHub,
    # with GITHUB_TOKEN as the fallback (agent/tools/github_tools.token_source).
    assert "make_github_tools(token_source(config))" in inspect.getsource(da)
    assert "make_github_tools(token_source(config), allowed_repos)" in inspect.getsource(pc)


class _FakeStore:
    def __init__(self, items):
        self._items = items

    async def asearch(self, ns, limit=100):
        from types import SimpleNamespace
        return [SimpleNamespace(key=k, value=v) for k, v in self._items.items()] if ns == ("github_inbox", "a") else []


@pytest.mark.asyncio
async def test_the_inbox_tool_lists_open_items_with_the_patched_version(monkeypatch):
    from agent.tools.github_tools import make_github_inbox_tool
    monkeypatch.setattr("agent.tools.github_tools.PROJECTS", {"a": {"live": "/x", "sandbox": "/y"}}, raising=False)
    store = _FakeStore({
        "alert:9": {"key": "alert:9", "kind": "security_alerts", "repo": "a", "number": 9, "state": "proposed", "updated_at": 2,
                    "title": "[HIGH] sharp: libheif", "summary": "sharp < 0.35.4 -> patched in 0.35.4 (pnpm-lock.yaml)", "url": "https://gh/a/9"},
        "pr:7": {"key": "pr:7", "kind": "dependabot_prs", "repo": "a", "number": 7, "state": "task_created", "task_id": "abcdef123", "updated_at": 1,
                 "title": "bump sharp", "summary": "dependabot[bot]: dep -> main", "url": "https://gh/pr/7"},
        "pr:3": {"key": "pr:3", "kind": "dependabot_prs", "repo": "a", "number": 3, "state": "dismissed", "updated_at": 3, "title": "old", "summary": "", "url": ""},
    })
    tool_ = make_github_inbox_tool(store, ["a"])
    out = await tool_.ainvoke({"repo": "a"})
    assert out.startswith("2 open GitHub inbox item(s) for a:")
    assert "[security alert] #9 [HIGH] sharp: libheif -- state: proposed" in out
    assert "patched in 0.35.4" in out
    assert "task: abcdef12" in out
    assert "old" not in out                                   # dismissed is not open
    assert "old" in await tool_.ainvoke({"repo": "a", "state": "all"})
    assert (await tool_.ainvoke({"repo": "b"})).startswith("ERROR")
    empty = await tool_.ainvoke({"repo": "a", "state": "snoozed"})
    assert "no snoozed items" in empty



# ---------------------------------------------------------------------------
# 2026-09-30: every inbox task's first GitHub call failed. The goal carries the
# PR's link (github.com/<owner>/<repo>/pull/<n>); the model passed "<owner>/<repo>";
# the tools only knew the project's own name.
# ---------------------------------------------------------------------------

def _two_projects(monkeypatch, allowed=None):
    monkeypatch.setattr(gh, "PROJECTS", {"storefront": {"live": "/a"}, "internal": {"live": "/b"}, "twin": {"live": "/c"}})
    slugs = {"storefront": "Acme/Shop", "internal": "acme/internal-tools", "twin": None}
    monkeypatch.setattr(gh, "resolve_slug", lambda repo: slugs.get(repo))


def test_a_project_is_found_by_name_or_by_its_github_owner_repo(monkeypatch):
    _two_projects(monkeypatch)
    assert gh.project_for("storefront") == ("storefront", None)
    assert gh.project_for("Acme/Shop") == ("storefront", None)
    assert gh.project_for("acme/shop") == ("storefront", None), "GitHub names are case-insensitive"
    assert gh.project_for("https://github.com/acme/shop") == ("storefront", None)
    assert gh.project_for("https://github.com/Acme/Shop.git") == ("storefront", None)


def test_access_is_the_callers_in_either_form(monkeypatch):
    _two_projects(monkeypatch)
    def usable(problem):
        return problem.split("can use:", 1)[1]

    project, problem = gh.project_for("acme/internal-tools", allowed_repos=["storefront"])
    assert project is None and "inaccessible" in problem
    assert usable(problem).startswith(" 'storefront')"), "the list names only what the user can see"
    project, problem = gh.project_for("internal", allowed_repos=["storefront"])
    assert project is None and usable(problem).strip() == "'storefront'."


def test_an_unknown_name_lists_the_valid_ones(monkeypatch):
    _two_projects(monkeypatch)
    project, problem = gh.project_for("someone/else")
    assert project is None
    assert "'internal', 'storefront', 'twin'" in problem and "owner/repo" in problem


def test_two_projects_on_one_repository_is_ambiguous_not_a_guess(monkeypatch):
    monkeypatch.setattr(gh, "PROJECTS", {"shop-a": {}, "shop-b": {}})
    monkeypatch.setattr(gh, "resolve_slug", lambda repo: "acme/shop")
    project, problem = gh.project_for("acme/shop")
    assert project is None and "'shop-a', 'shop-b'" in problem


def test_the_pull_request_tool_accepts_the_owner_repo_and_uses_the_projects_token(monkeypatch):
    _two_projects(monkeypatch)
    seen = {}

    def fetch(token, slug, number):
        seen.update(token=token, slug=slug, number=number)
        return _data()

    monkeypatch.setattr(gh, "fetch_pull_request", fetch)
    tools = {t.name: t for t in make_github_tools(lambda repo: f"token-for-{repo}")}
    out = tools["github_pull_request"].invoke({"repo": "Acme/Shop", "number": 12})
    assert "Audit fixes" in out
    assert seen == {"token": "token-for-storefront", "slug": "Acme/Shop", "number": 12}
