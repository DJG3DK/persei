"""Create a brand-new project from the command line -- the "New project"
button's headless twin.

    .venv/bin/python scripts/new_project.py my-service
    .venv/bin/python scripts/new_project.py my-service --description "Orders API"
    .venv/bin/python scripts/new_project.py my-service --github            # PRIVATE repo, pushed
    .venv/bin/python scripts/new_project.py my-service --parent /home/apps

What it does, in order, is exactly what POST /api/projects/create does:
a directory under an allowed root with `git init` and one commit on main;
optionally a private GitHub repository with its own deploy key, set as the
SSH origin and pushed; then detection, the recommended answers (the same
rule as `add_project.py --yes`), the worktree and the projects.json entry.
Same modules underneath (agent/provisioning.py, agent/github_repos.py), so
a headless install cannot drift from what the dashboard does.

--github reads the token from the environment variable named by --token-env
(default GITHUB_TOKEN); a stored dashboard token is not reachable from a
script, and a token must never be a command-line argument (it would land in
the shell history and `ps`).

The knowledge seeding the dashboard does (project memory, codebase map)
needs the store and a model; like add_project.py, this script leaves those
to their own scripts and says so at the end.
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config import _PROJECTS_CONFIG_PATH, PROJECTS  # noqa: E402
from agent import provisioning as prov  # noqa: E402


def _step(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  {label:<11} {'ok' if ok else 'FAILED'}{': ' + detail if detail else ''}")
    return ok


def _github(live: str, name: str, description: str, token: str) -> bool:
    """Create the private repo, mint and register its deploy key, push. Any
    failure is reported and the LOCAL project continues -- the repo on disk
    is real; the remote can be connected later from the deploy-key panel."""
    from agent import deploy_keys, github_repos  # noqa: PLC0415

    try:
        created = asyncio.run(github_repos.create_private_repo(token, name, description))
        public_key = github_repos.connect_origin(live, created["ssh_url"], name)
        asyncio.run(github_repos.add_deploy_key(token, created["full_name"],
                                                f"persei-{name}", public_key))
        ok, detail = github_repos.push_initial(live, name)
        return _step("github", ok, f"{created['html_url']}: {detail}" if ok else detail)
    except (PermissionError, LookupError, ValueError, deploy_keys.DeployKeyError) as e:
        return _step("github", False, str(e)[:400])
    except Exception as e:  # noqa: BLE001 -- an httpx error must not hide the local result
        return _step("github", False, f"GitHub request failed ({type(e).__name__})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Create and onboard a new project")
    ap.add_argument("name", help="project name (one directory name; also the projects.json key)")
    ap.add_argument("--parent", default=None,
                    help="directory to create the project under (default: the first AGENT_PROJECT_ROOTS entry)")
    ap.add_argument("--description", default="", help="README first paragraph and GitHub description")
    ap.add_argument("--github", action="store_true",
                    help="also create a PRIVATE GitHub repository, add a deploy key and push main")
    ap.add_argument("--token-env", default="GITHUB_TOKEN",
                    help="environment variable holding the GitHub token used with --github")
    ap.add_argument("--sandbox-root", default=None, help="override AGENT_SANDBOX_ROOT for this run")
    args = ap.parse_args()

    try:
        name = prov.validate_project_name(args.name, list(PROJECTS))
    except prov.ProvisioningError as e:
        print(f"error: {e.detail}", file=sys.stderr)
        return 2

    token = None
    if args.github:
        token = (os.environ.get(args.token_env) or "").strip()
        if not token:
            print(f"error: --github needs a token in ${args.token_env}", file=sys.stderr)
            return 2

    try:
        live = prov.create_repository(args.parent, name, description=args.description,
                                     existing_names=list(PROJECTS))
    except prov.ProvisioningError as e:
        print(f"error: {e.detail}", file=sys.stderr)
        return 2
    print(f"\n{name}")
    _step("repository", True, f"{live} (one commit on main)")

    if args.github and token:
        _github(live, name, args.description, token)

    try:
        report = prov.detect_project(live, sandbox_root=args.sandbox_root,
                                     existing_names=list(PROJECTS))
    except prov.ProvisioningError as e:
        _step("detect", False, e.detail)
        return 1
    if report.blockers:
        _step("detect", False, "; ".join(report.blockers))
        return 1
    for w in report.warnings:
        print(f"  warning     {w}")
    try:
        choices = prov.validate_choices(report, prov.recommended_choices(report))
    except prov.ProvisioningError as e:
        _step("detect", False, e.detail)
        return 1
    _step("detect", True, f"{len(report.checks)} check(s)")

    ok, detail = prov.create_worktree(report.live, report.sandbox)
    if not _step("worktree", ok, detail):
        return 1

    entry = prov.config_from_choices(report.live, report.sandbox, choices)
    try:
        prov.write_project_entry(_PROJECTS_CONFIG_PATH, name, entry)
    except prov.ProvisioningError as e:
        _step("config", False, e.detail)
        return 1
    _step("config", True, f"wrote {name} to {_PROJECTS_CONFIG_PATH}")

    print(f"\n  live      {report.live}")
    print(f"  sandbox   {report.sandbox}")
    print("\nNext:")
    print(f"  .venv/bin/python scripts/run_cartographer.py {name}   # build its codebase map")
    print("  .venv/bin/python scripts/seed_memory.py               # seed project memory")
    # Only the dashboard endpoint reloads the RUNNING process in place; a
    # file written from here is invisible to it until a restart.
    print("  restart the agent so the running process picks it up")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
