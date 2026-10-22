"""Is the Dependabot alert a task was created for still open?

A task from the GitHub inbox names its alerts in its goal ("Fix Dependabot
security alerts #6, #8 and #9 ...") and links the repository's alert page.
When another change closes those alerts first -- on 2026-09-29 one bump
closed eleven, and ten tasks then rebased, re-reviewed and merged a
lockfile change that fixed nothing -- the task has nothing left to ship
and says so instead of taking the project's merge slot. Never raises: a
question GitHub cannot answer is "unknown", and unknown ships as before.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("tektonix")

_HEAD = re.compile(r"\AFix Dependabot security alerts? ((?:#\d+(?:, | and )?)+)")
_SLUG = re.compile(r"https://github\.com/([^/\s]+/[^/\s?#]+)/security/dependabot")


def alerts_referenced(goal: str) -> tuple[str | None, list[int]]:
    """(owner/repo, alert numbers) named by an inbox goal; (None, []) for
    any other goal."""
    m = _HEAD.match(goal or "")
    if not m:
        return None, []
    numbers = [int(n) for n in re.findall(r"#(\d+)", m.group(1))]
    slug = _SLUG.search(goal or "")
    return (slug.group(1) if slug else None), numbers


# The states that mean the vulnerability is gone. `dismissed` is a person
# deciding not to fix it -- the code is unchanged, so the task's work is not
# already done (audit B40, 2026-10-22). `auto_dismissed` is Dependabot's own
# finding that the alert no longer applies.
_FIXED_STATES = ("fixed", "auto_dismissed")


async def all_closed(token: str, slug: str, numbers: list[int]) -> bool | None:
    """True when GitHub says every alert is fixed, False when one is not,
    None when it cannot say."""
    from agent.github_inbox import GitHubClient  # noqa: PLC0415 -- the inbox imports this module's caller

    client = GitHubClient(token)
    try:
        for n in numbers:
            alert = await client.get(f"/repos/{slug}/dependabot/alerts/{n}")
            if (alert or {}).get("state", "open") not in _FIXED_STATES:
                return False
    except Exception as e:  # noqa: BLE001 -- unknown, not closed
        logger.info("inbox alerts: could not read %s alerts %s: %s", slug, numbers, e)
        return None
    return bool(numbers)


async def already_fixed(repo: str, goal: str, config) -> str | None:
    """A sentence when every alert this task was created for is closed, else
    None. Reads the project's own token from the GitHub settings."""
    slug, numbers = alerts_referenced(goal)
    if not slug or not numbers:
        return None
    try:
        from agent import github_settings  # noqa: PLC0415
        token = github_settings.token_for(github_settings.current(), config, repo)
    except Exception as e:  # noqa: BLE001
        logger.info("inbox alerts: no token for %s: %s", repo, e)
        return None
    if not token:
        return None
    closed = await all_closed(token, slug, numbers)
    if not closed:
        return None
    refs = ", ".join(f"#{n}" for n in numbers)
    return (f"Dependabot alert{'s' if len(numbers) > 1 else ''} {refs} on {slug} "
            f"{'are' if len(numbers) > 1 else 'is'} no longer open: another change already fixed "
            f"{'them' if len(numbers) > 1 else 'it'}, so there is nothing left to ship.")
