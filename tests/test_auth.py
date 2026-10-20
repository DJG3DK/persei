"""Unit tests for the DB-independent pieces of agent/auth.py (password
hashing/strength, TOTP secret encryption, per-user repo access). Matches
this repo's existing convention of never touching the real Postgres DB in
automated tests -- the DB-dependent flows (session create/resolve/revoke,
TOTP setup/confirm/verify with its replay guard, recovery codes, admin
seeding) were verified live against the real deployment during development
(argon2 hash/verify round trip, a full login+2FA-enrollment+recovery-code
cycle, and the enrollment-code replay fix all confirmed working end to end).
"""

import pyotp

from agent.auth import (
    User,
    check_repo_access,
    hash_password,
    new_totp_secret,
    totp_provisioning_uri,
    validate_password_strength,
    verify_password,
)
from fastapi import HTTPException
import pytest


# ---------------------------------------------------------------------------
# password hashing
# ---------------------------------------------------------------------------


def test_hash_and_verify_round_trip():
    h = hash_password("SuperSecret123")
    assert verify_password("SuperSecret123", h)


def test_verify_rejects_wrong_password():
    h = hash_password("SuperSecret123")
    assert not verify_password("wrong-password", h)


def test_hash_is_never_the_plaintext_or_reversible_trivially():
    h = hash_password("SuperSecret123")
    assert "SuperSecret123" not in h
    assert h.startswith("$argon2")


def test_same_password_hashes_differently_each_time():
    # argon2 salts per-hash -- two hashes of the same password must differ,
    # or a database leak would let two matching hashes reveal shared passwords.
    assert hash_password("SuperSecret123") != hash_password("SuperSecret123")


# ---------------------------------------------------------------------------
# password strength
# ---------------------------------------------------------------------------


def test_rejects_too_short():
    assert validate_password_strength("Ab1defghi") is not None  # 9 chars


def test_rejects_missing_uppercase():
    assert validate_password_strength("alllowercase123") is not None


def test_rejects_missing_lowercase():
    assert validate_password_strength("ALLUPPERCASE123") is not None


def test_rejects_missing_digit():
    assert validate_password_strength("NoDigitsHereAtAll") is not None


def test_accepts_a_strong_password():
    assert validate_password_strength("SuperSecret123") is None


# ---------------------------------------------------------------------------
# TOTP secret / provisioning URI
# ---------------------------------------------------------------------------


def test_new_totp_secret_is_a_valid_base32_secret_usable_by_pyotp():
    secret = new_totp_secret()
    totp = pyotp.TOTP(secret)
    code = totp.now()
    assert len(code) == 6
    assert totp.verify(code)


def test_provisioning_uri_names_the_issuer_and_account():
    secret = new_totp_secret()
    uri = totp_provisioning_uri(secret, "admin@example.com")
    assert uri.startswith("otpauth://totp/")
    assert "Persei" in uri
    assert "admin%40example.com" in uri or "admin@example.com" in uri


# ---------------------------------------------------------------------------
# per-user repo access (the actual per-project ACL feature)
# ---------------------------------------------------------------------------


def _user(role="user", allowed_repos=None):
    return User(id=1, email="x@example.com", role=role, allowed_repos=allowed_repos, totp_enabled=False, must_change_password=False)


def test_admin_can_access_every_repo_regardless_of_allowed_repos_field():
    admin = _user(role="admin", allowed_repos=[])
    assert admin.can_access("my-service")
    assert admin.can_access("shop-api")
    assert admin.can_access("anything-not-even-configured")


def test_restricted_user_can_only_access_their_allowed_repos():
    user = _user(role="user", allowed_repos=["shop-api"])
    assert user.can_access("shop-api")
    assert not user.can_access("my-service")
    assert not user.can_access("web-app")


def test_user_with_allowed_repos_none_can_access_everything():
    # None means "no restriction" -- distinct from an empty list (which
    # means "restricted to nothing"). Only meaningful for a non-admin
    # account if an operator deliberately leaves it unset; admin already
    # covers the common "sees everything" case via role alone.
    user = _user(role="user", allowed_repos=None)
    assert user.can_access("my-service")
    assert user.can_access("shop-api")


def test_check_repo_access_raises_403_for_a_denied_repo():
    user = _user(role="user", allowed_repos=["shop-api"])
    with pytest.raises(HTTPException) as exc_info:
        check_repo_access(user, "my-service")
    assert exc_info.value.status_code == 403


def test_check_repo_access_is_silent_for_an_allowed_repo():
    user = _user(role="user", allowed_repos=["shop-api"])
    check_repo_access(user, "shop-api")  # must not raise


# audit M-2: the replay ratchet keys on the step a code was MINTED for, not the
# wall-clock step at verification. _matched_totp_step is the piece that recovers
# the minted step; the DB UPDATE then rejects any step <= the last consumed one.
def test_matched_totp_step_recovers_the_minting_step_across_the_window():
    import time
    from agent.auth import _matched_totp_step

    secret = pyotp.random_base32()
    totp = pyotp.TOTP(secret)
    now = int(time.time())
    now_step = now // totp.interval

    # A code minted for the CURRENT step is matched as the current step.
    code_now = totp.at(now)
    assert _matched_totp_step(totp, code_now) == now_step

    # A code minted for the PREVIOUS step (still accepted by valid_window=1) is
    # attributed to that previous step, not to `now` -- this is exactly what
    # stops the replay: a login at S stores S, and this same S-code replayed
    # later still resolves to S, which the `< matched_step` guard rejects.
    code_prev = totp.at(now - totp.interval)
    assert _matched_totp_step(totp, code_prev) == now_step - 1


def test_matched_totp_step_returns_none_for_a_code_outside_the_window():
    import time
    from agent.auth import _matched_totp_step

    totp = pyotp.TOTP(pyotp.random_base32())
    now = int(time.time())
    valid = {totp.at(now - totp.interval), totp.at(now), totp.at(now + totp.interval)}
    # pick any 6-digit string that isn't one of the three in-window codes
    bogus = next(f"{n:06d}" for n in range(1000000) if f"{n:06d}" not in valid)
    assert _matched_totp_step(totp, bogus) is None


# --- audit H7: fail-open user creation --------------------------------------

def test_can_access_treats_none_as_unrestricted_for_any_role():
    """The behaviour the create endpoint has to defend against: allowed_repos
    of None means EVERY repo, regardless of role. That is intentional for
    admins and a fail-open for anyone else."""
    from agent.auth import User
    unrestricted = User(id=1, email="u@x", role="user", allowed_repos=None,
                        totp_enabled=False, must_change_password=False,
                        auto_approve_commands=False, require_merge_review=True)
    assert unrestricted.can_access("any-repo-at-all") is True

    scoped = User(id=2, email="s@x", role="user", allowed_repos=["one"],
                  totp_enabled=False, must_change_password=False,
                  auto_approve_commands=False, require_merge_review=True)
    assert scoped.can_access("one") is True
    assert scoped.can_access("two") is False
