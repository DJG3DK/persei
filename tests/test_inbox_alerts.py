"""A task made for Dependabot alerts concludes when another change has
closed them (2026-09-29: one merge closed eleven, ten tasks shipped nothing)."""

import pytest

from agent import inbox_alerts
from agent.github_inbox import Item, build_goal


def _item(number, summary="", url="https://github.com/o/proj/security/dependabot/9"):
    return Item(key="x", kind="security_alerts", repo="proj", number=number, title="[HIGH] undici: bug",
                url=url, fingerprint="f", summary=summary)


def test_the_alerts_a_goal_names_are_read_back():
    single = build_goal(_item(9))
    assert inbox_alerts.alerts_referenced(single) == ("o/proj", [9])
    group = build_goal(_item(None, summary="undici in f: bump\n#6 [LOW] a: x\n#8 [HIGH] b: y\n#9 [HIGH] c: z",
                             url="https://github.com/o/proj/security/dependabot?q=is%3Aopen+package%3Aundici"))
    assert inbox_alerts.alerts_referenced(group) == ("o/proj", [6, 8, 9])
    assert inbox_alerts.alerts_referenced("Add a dark mode toggle") == (None, [])
    assert inbox_alerts.alerts_referenced("") == (None, [])


class _Client:
    states = {}
    fail = False

    def __init__(self, token):
        self.token = token

    async def get(self, path, params=None):
        if _Client.fail:
            raise PermissionError("no")
        n = int(path.rsplit("/", 1)[1])
        return {"number": n, "state": _Client.states.get(n, "open")}


@pytest.mark.asyncio
async def test_all_closed_asks_github_about_each_alert(monkeypatch):
    from agent import github_inbox
    monkeypatch.setattr(github_inbox, "GitHubClient", _Client)
    _Client.states, _Client.fail = {6: "fixed", 7: "auto_dismissed", 8: "dismissed"}, False
    assert await inbox_alerts.all_closed("tok", "o/proj", [6, 7]) is True
    assert await inbox_alerts.all_closed("tok", "o/proj", [6, 9]) is False, "#9 is still open"
    assert await inbox_alerts.all_closed("tok", "o/proj", [6, 8]) is False, \
        "B40: dismissed is a person choosing not to fix it, not a fix"
    assert await inbox_alerts.all_closed("tok", "o/proj", []) is False
    _Client.fail = True
    assert await inbox_alerts.all_closed("tok", "o/proj", [6]) is None, "unknown, not closed"


@pytest.mark.asyncio
async def test_already_fixed_says_so_only_with_a_token_and_every_alert_closed(monkeypatch):
    from agent import github_inbox, github_settings
    monkeypatch.setattr(github_inbox, "GitHubClient", _Client)
    goal = build_goal(_item(None, summary="undici in f: bump\n#6 [LOW] a: x\n#9 [HIGH] c: z",
                            url="https://github.com/o/proj/security/dependabot?q=x"))
    monkeypatch.setattr(github_settings, "token_for", lambda settings, config, repo: None)
    assert await inbox_alerts.already_fixed("proj", goal, None) is None, "no token: nothing is known"
    monkeypatch.setattr(github_settings, "token_for", lambda settings, config, repo: "tok")
    _Client.states, _Client.fail = {6: "fixed", 9: "fixed"}, False
    note = await inbox_alerts.already_fixed("proj", goal, None)
    assert note and "#6, #9 on o/proj are no longer open" in note
    _Client.states = {6: "fixed"}
    assert await inbox_alerts.already_fixed("proj", goal, None) is None, "one still open"
    assert await inbox_alerts.already_fixed("proj", "Add a toggle", None) is None
