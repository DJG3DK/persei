"""Runs the repo's own real typecheck/lint/test npm scripts -- never a
hand-reconstructed check list, since a mismatch between what's actually
configured and what a check runner assumes is exactly the kind of gap that
can let a real regression through undetected. Shared by the agent-facing
`run_checks` tool (lets the deep agent self-verify cheaply, during its own
work) and the outer `verify_and_ship` gate (the actual hard, code-enforced
gate that never trusts the agent's own self-report), so both call the exact
same logic. Only the outer gate's result has any authority over shipping;
the tool exists purely so the agent gets faster feedback than waiting for
that gate.
"""

import pathlib
from typing import Any, cast

from agent import runtime_settings as _rs

from agent.config import PROJECT_TEST_ENV
from agent.tools.sandbox import run_shell_sandboxed


async def run_check(repo_root: str, script: str, timeout: int,
                    extra_env: dict[str, Any] | None = None) -> dict[str, Any]:
    # audit H-17: read package.json directly and DISTINGUISH "file absent /
    # unreadable" from "file present, no such script". The old `cat ... || true`
    # succeeded with empty output when package.json was missing or the worktree
    # was in a bad state, so every script "skipped" and the gate reported a
    # green run it never performed. A missing manifest is now a FAILED check,
    # not a silent pass.
    import os.path as _osp
    pkg_path = _osp.join(repo_root, "package.json")
    if not _osp.isfile(pkg_path):
        return {"ran": False, "ok": False,
                "output": f"package.json not found at {pkg_path} -- cannot run checks (worktree missing or broken)"}
    try:
        with open(pkg_path) as _fh:
            pkg_text = _fh.read()
    except OSError as e:
        return {"ran": False, "ok": False, "output": f"package.json unreadable: {e}"}
    if f'"{script}"' not in pkg_text:
        return {"ran": False, "ok": True, "output": f"(no {script} script in package.json -- skipped)"}
    # audit C-2: the check suite runs agent-authored code (npm scripts and the
    # test files the test-writer subagent produces), so it must run INSIDE the
    # Docker sandbox, not on the host as uid 1000 with access to every other
    # project's .env and this agent's own AUTH_SECRET_KEY. --network none: the
    # deps are already installed in the worktree, so the checks need no egress,
    # and this also removes the container's reach to the cloud metadata service
    # and host-bridge services during the check phase.
    result = await run_shell_sandboxed(f"npm run {script}", repo_root, timeout=timeout, extra_env=extra_env, network="none")
    output = result["output"][-4000:]
    if not result["ok"]:
        output = _annotate_offline_failure(output)
    return {"ran": True, "ok": result["ok"], "output": output}


# Signatures of "this failed because the sandbox has no network", not because
# the code is wrong. `--network none` above is deliberate, so these failures
# are expected for any test that reaches the internet -- but the raw message
# is a bare `getaddrinfo EAI_AGAIN example.com`, which reads like a code
# regression. Observed live (2026-08-29): a test suite reported 703/704 with
# the single failure being a DNS lookup, and both the agent and the operator
# had to work out for themselves that the sandbox was the cause.
#
# Note this ANNOTATES, never suppresses: the check still fails. Hiding it
# would let a genuinely network-dependent test pass review and then fail in
# CI, which is worse than a confusing message.
_OFFLINE_SIGNATURES = (
    "EAI_AGAIN",
    "ENOTFOUND",
    "getaddrinfo",
    "Network is unreachable",
    "ECONNREFUSED",
    "Temporary failure in name resolution",
    "Could not resolve host",
)

_OFFLINE_NOTE = """
--------------------------------------------------------------------
NOTE FROM THE CHECK RUNNER: this output contains a network/DNS error.

The check suite runs in a container with NO NETWORK (`--network none`),
deliberately -- it executes agent-authored test code, so it gets no
egress. Dependencies are already installed in the workspace, so checks
that only compile and run local code are unaffected.

So a failure like `getaddrinfo EAI_AGAIN <host>` is almost certainly
the sandbox, not the change under review. Confirm by checking whether
the same test fails on an unmodified checkout.

If a test genuinely needs the network, it does not belong in the review
suite: mock at the layer that performs the request. A common miss is
mocking `global.fetch` when the code calls a wrapper that resolves DNS
first (an SSRF guard, for instance) -- the lookup throws before fetch
is ever reached.
--------------------------------------------------------------------
"""


def _annotate_offline_failure(output: str) -> str:
    """Prepend an explanation when a check failed on a network error."""
    if any(sig in output for sig in _OFFLINE_SIGNATURES):
        return _OFFLINE_NOTE + output
    return output


async def run_typecheck(repo_root: str) -> dict[str, Any]:
    return await run_check(repo_root, "typecheck", timeout=_rs.as_int("check_typecheck_timeout_s"))


async def run_lint(repo_root: str) -> dict[str, Any]:
    return await run_check(repo_root, "lint", timeout=_rs.as_int("check_lint_timeout_s"))


async def run_test(repo_root: str, repo_name: str) -> dict[str, Any]:
    # See PROJECT_TEST_ENV's own comment -- some projects' test suites need a
    # path to something outside their own workspace checkout (e.g. a live
    # config file they deliberately target against a single real instance).
    #
    # Prefer `test:review` where the repo defines it. Plain `npm test` chains
    # every test:* script, and for a large project two of those (test:auth, test:routes)
    # drive the RUNNING production bot on a live service's own port -- so they
    # exercise whatever is deployed rather than the agent's own working tree,
    # and they hit live services as a side effect of a self-check.
    # `test:review` is the repo's own statement of what is safe to run against
    # a detached checkout.
    env = PROJECT_TEST_ENV.get(repo_name)
    review = await run_check(repo_root, "test:review", timeout=_rs.as_int("check_review_test_timeout_s"), extra_env=env)
    if review["ran"]:
        return review
    return await run_check(repo_root, "test", timeout=_rs.as_int("check_test_timeout_s"), extra_env=env)


async def run_frontend_build(repo_root: str) -> dict[str, Any]:
    """Real production build of the repo's frontend, when it has one.

    Exists because the deterministic gate passed a commit whose DEPLOY then
    failed: run_all_checks was typecheck+lint+test, but the merge endpoint's
    deploy stage runs `npm run build` (strict `tsc -b`), which rejects things
    check-mode tsc tolerates -- three TS6133 unused-variable errors shipped
    through the whole gate, review, and the operator's approval, then broke
    the build AFTER the merge, leaving live mid-deploy (2026-08-26, the 2FA
    task). Same lesson as this dashboard's own frontend a week earlier: the build
    config is the stricter contract, so the gate must run the build.

    Skips cleanly (ran=False, ok=True) when the repo has no frontend/ with a
    build script -- this is a conditional stage, not a new universal demand.
    """
    import json as _json
    pkg = pathlib.Path(repo_root) / "frontend" / "package.json"
    try:
        scripts: dict[str, Any] = _json.loads(pkg.read_text()).get("scripts") or {}
        has_build = "build" in scripts
    except (OSError, ValueError):
        has_build = False
    if not has_build:
        return {"ran": False, "ok": True, "output": "no frontend build script -- skipped"}
    # audit C-2: sandboxed, same as the check suite above.
    r = await run_shell_sandboxed("npm run --silent build", str(pathlib.Path(repo_root) / "frontend"), timeout=_rs.as_int("frontend_build_timeout_s"), network="none")
    return {"ran": True, "ok": r["ok"], "output": r["output"][-4000:]}


def _declared_checks(repo_name: str) -> list[dict[str, Any]] | None:
    """A project's own gate commands, when projects.json declares them.

    The gate was npm scripts only, so a repository with no package.json
    failed it before a line was checked -- every Python project, and every
    SWE-bench repository. `checks` is a list of {name, cmd, timeout_s}: shell
    commands run in the project's sandbox (its image, its environment, no
    network), from the workspace root."""
    try:
        from agent.config import PROJECTS
    except Exception:  # noqa: BLE001 -- no loadable config means no declared checks; the npm gate still runs
        return None
    declared: Any = (PROJECTS.get(repo_name) or {}).get("checks")
    if not isinstance(declared, list) or not declared:
        return None
    out: list[dict[str, Any]] = []
    for item in cast("list[Any]", declared):
        if not isinstance(item, dict):
            continue
        c = cast("dict[str, Any]", item)
        if isinstance(c.get("cmd"), str) and c["cmd"].strip():
            out.append({"name": str(c.get("name") or f"check {len(out) + 1}")[:60], "cmd": c["cmd"],
                        "timeout_s": int(c.get("timeout_s") or _rs.as_int("check_test_timeout_s"))})
    return out or None


async def _run_declared(repo_root: str, declared: list[dict[str, Any]]) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}
    for c in declared:
        r = await run_shell_sandboxed(c["cmd"], repo_root, timeout=c["timeout_s"], network="none")
        output = r["output"][-4000:]
        checks[c["name"]] = {"ran": True, "ok": r["ok"],
                             "output": output if r["ok"] else _annotate_offline_failure(output)}
    all_ok = all(c["ok"] for c in checks.values())
    summary = "\n".join(f"{name}: {'PASSED' if c['ok'] else 'FAILED'}\n{c['output']}" for name, c in checks.items())
    return {"all_ok": all_ok, "checks": checks, "summary": summary}


async def run_all_checks(repo_root: str, repo_name: str) -> dict[str, Any]:
    """The full gate: typecheck + lint + test. Used by both the agent-facing
    tool and the outer verify_and_ship node so there's exactly one
    definition of "all checks passed" in this system.

    A project that declares its own `checks` in projects.json runs those
    instead (_declared_checks).
    """
    declared = _declared_checks(repo_name)
    if declared:
        return await _run_declared(repo_root, declared)
    typecheck = await run_typecheck(repo_root)
    lint = await run_lint(repo_root)
    test = await run_test(repo_root, repo_name)
    frontend_build = await run_frontend_build(repo_root)
    checks = {"typecheck": typecheck, "lint": lint, "test": test, "frontend_build": frontend_build}
    all_ok = all(c["ok"] for c in checks.values())
    summary = "\n".join(
        f"{name}: {'PASSED' if c['ok'] else 'FAILED'}\n{c['output']}" for name, c in checks.items()
    )
    return {"all_ok": all_ok, "checks": checks, "summary": summary}
