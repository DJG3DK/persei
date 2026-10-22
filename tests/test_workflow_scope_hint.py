"""GitHub's refusal to let a token write .github/workflows/, made actionable.

Live 2026-09-22: a task wrote a good `.github/workflows/ci.yml`, the review
gate passed, and the ship step reported a raw git error. Nothing in it said
the fix was one permission, which of the two token types the operator had, or
that the work was safe. These pin the answer to each of those.
"""
import pytest

from agent.tools import review_gate as rg

FINE = "github_pat_11ABCDEF0123456789"
CLASSIC = "ghp_0123456789abcdef"

# What git actually printed, abridged. Matching on the real string matters:
# a hint keyed to a paraphrase would stop firing the day GitHub reworded
# nothing at all and someone reworded this test's fixture instead.
REAL_REFUSAL = (
    "To https://github.com/OWNER/REPO.git\n"
    " ! [remote rejected] agent/abc -> agent/abc (refusing to allow a Personal "
    "Access Token to create or update workflow `.github/workflows/ci.yml` "
    "without `workflow` scope)\nerror: failed to push some refs"
)


def test_the_real_refusal_is_recognised():
    assert rg._WORKFLOW_SCOPE_REFUSAL in REAL_REFUSAL


def test_an_unrelated_push_failure_is_not_mistaken_for_it():
    for other in ("! [rejected] main -> main (non-fast-forward)",
                  "fatal: Authentication failed",
                  "error: failed to push some refs"):
        assert rg._WORKFLOW_SCOPE_REFUSAL not in other


def test_a_fine_grained_token_is_sent_to_the_permission_it_actually_has():
    """GitHub's own message says "workflow scope", which is the CLASSIC token's
    wording. Telling someone holding a fine-grained token to tick that sends
    them looking for a checkbox that is not on their screen."""
    hint = rg._workflow_scope_hint(FINE, "OWNER/REPO", "agent/abc")
    assert "Fine-grained" in hint
    assert "Workflows -> Read and write" in hint
    assert "Tokens (classic)" not in hint


def test_a_classic_token_is_sent_to_the_scope_checkbox():
    hint = rg._workflow_scope_hint(CLASSIC, "OWNER/REPO", "agent/abc")
    assert "Tokens (classic)" in hint
    assert "`workflow` scope" in hint
    assert "Fine-grained" not in hint


def test_the_hint_says_the_work_is_safe():
    """The first question an operator has is whether they lost the task."""
    hint = rg._workflow_scope_hint(FINE, "OWNER/REPO", "agent/abc")
    assert "committed locally" in hint and "nothing is lost" in hint
    assert "resuming the task" in hint


def test_the_hint_names_the_branch_and_the_org_approval_case():
    hint = rg._workflow_scope_hint(FINE, "MyOrg/REPO", "agent/79910c4f")
    assert "agent/79910c4f" in hint
    assert "MyOrg" in hint and "org approval" in hint


def test_the_hint_offers_the_deploy_key_route():
    """A deploy-key push is not subject to the restriction at all, and an
    operator who would rather not widen a token's permissions deserves to
    know that here rather than after searching."""
    assert "deploy key" in rg._workflow_scope_hint(FINE, "OWNER/REPO", "b")


def test_a_missing_token_still_produces_a_usable_hint():
    hint = rg._workflow_scope_hint(None, "OWNER/REPO", "agent/abc")
    assert "Tokens (classic)" in hint   # the safe default wording


def test_the_hint_never_contains_the_token():
    for tok in (FINE, CLASSIC):
        assert tok not in rg._workflow_scope_hint(tok, "OWNER/REPO", "agent/abc")


@pytest.mark.asyncio
async def test_the_ship_step_returns_the_hint_rather_than_the_git_error(monkeypatch, tmp_path):
    """End to end through ship_as_pull_request: a refused push comes back
    tagged, hinted, and with the raw git output kept separately for anyone who
    wants it."""
    from agent import github_settings
    from agent.config import PROJECTS

    monkeypatch.setitem(PROJECTS, "demo", {"live": str(tmp_path), "sandbox": str(tmp_path),
                                           "ship": "pr"})
    monkeypatch.setattr(github_settings, "token_for", lambda *a, **k: FINE)

    async def fake_git(cmd, cwd, timeout=None, extra_env=None):
        if cmd.startswith("rev-parse --verify"):
            return {"ok": True, "output": "deadbeef" * 5}
        if "remote get-url" in cmd or "config --local" in cmd:
            return {"ok": True, "output": "https://github.com/OWNER/REPO.git"}
        if " push " in f" {cmd} ":
            return {"ok": False, "output": REAL_REFUSAL}
        return {"ok": True, "output": ""}

    monkeypatch.setattr("agent.tools.git._git", fake_git)
    result = await rg.ship_as_pull_request("demo", "agent/abc", "deadbeef" * 5, "t")

    assert result["ok"] is False
    assert result["reason"] == "workflow_scope"
    assert "Workflows -> Read and write" in result["error"]
    assert "nothing is lost" in result["error"]
    # The raw output is kept, just not as the headline.
    assert "remote rejected" in result["git"]
    assert FINE not in result["error"] and FINE not in result["git"]


# ---------------------------------------------------------------------------
# ...and the message has to survive the trip to the operator
# ---------------------------------------------------------------------------

def test_the_escalation_carries_the_message_not_the_repr_of_the_dict():
    """What the operator actually read on 2026-09-22:

        merge/deploy failed: {'ok': False, 'stage': 'ship', 'error': 'could
        not push agent/7991...'}

    A stringified dict, with the one useful sentence inside it. A better
    `error` string is worth nothing if it is re-wrapped in punctuation on the
    way out.
    """
    import re

    from agent.nodes import verify_and_ship as vs
    source = vs.__file__
    text = open(source).read()
    assert 'f"merge/deploy failed: {deployed}"' not in text, (
        "the escalation still stringifies the whole result dict")
    assert re.search(r'why = str\(deployed\.get\("error"\)', text)


def test_a_result_with_no_error_string_still_says_something():
    """Not every failure path sets `error`; falling back to the dict is worse
    than a sentence and better than an empty escalation."""
    deployed = {"ok": False, "stage": "restart"}
    why = str(deployed.get("error") or "").strip() or str(deployed)
    assert why == str(deployed)
