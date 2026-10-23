"""The compose bundle, rendered the way `docker compose up` would read it.

`docker compose config` is the offline half of the bundle job: it
interpolates the file and resolves names without touching the daemon.
What it renders is what the agent, which holds container and network
names as plain environment strings, has to agree with -- and what each
container can reach, which is the bundle's whole privilege story."""
from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from agent import paths

REPO = paths.REPO_ROOT

pytestmark = pytest.mark.skipif(
    shutil.which("docker") is None
    or subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0,
    reason="needs the docker compose plugin",
)


def render(**env: str) -> dict:
    """The bundle as compose resolves it, under the given .env values."""
    base = {"PROJECTS_DIR": "/tmp", "OPENROUTER_API_KEY": "placeholder"}
    base.update(env)
    out = subprocess.run(
        ["docker", "compose", "--project-directory", str(REPO), "config", "--format", "json"],
        capture_output=True, text=True, check=True, cwd=REPO,
        env={**os.environ, **base, "COMPOSE_ENV_FILES": ""},  # never this box's .env
    ).stdout
    return json.loads(out)


def _volumes(service: dict) -> dict[str, str]:
    """named volume -> mount target, for the service's volume mounts."""
    return {v["source"]: v["target"] for v in service.get("volumes", []) if v["type"] == "volume"}


@pytest.mark.parametrize("project_name", [None, "three-d-agent"])
def test_the_checks_network_the_agent_attaches_to_exists_under_any_project_name(project_name):
    """The agent attaches each check container by the fixed name in
    REVIEW_CHECKS_NETWORK. compose's default name carries the project name,
    so an install kept on the pre-rename project name had a
    three-d-agent_checks network and no tektonix_checks: every database
    check failed to attach."""
    env = {"COMPOSE_PROJECT_NAME": project_name} if project_name else {}
    rendered = render(**env)
    wanted = rendered["services"]["agent"]["environment"]["REVIEW_CHECKS_NETWORK"]
    names = {net.get("name") for net in rendered["networks"].values()}
    assert wanted in names, f"the agent attaches to {wanted!r}; compose creates {sorted(names)}"
    assert rendered["networks"]["checks"]["internal"] is True


def test_each_secret_is_mounted_only_where_it_is_read():
    """One volume per secret. Before 2026-09-29 the database password and
    the router key shared a volume that the router and the reviewer
    mounted whole, and the review services mounted the agent's entire data
    volume -- AUTH_SECRET_KEY and the encrypted first password included --
    to read projects.json and one secret."""
    services = render()["services"]
    mounts = {name: _volumes(svc) for name, svc in services.items()}

    # The database password: written by postgres, read by the agent.
    assert set(n for n, m in mounts.items() if "pgsecret" in m) == {"postgres", "agent"}
    assert services["agent"]["environment"]["POSTGRES_PASSWORD_FILE"].startswith(mounts["agent"]["pgsecret"])
    # The router key: written by postgres, read by the three that call the router.
    assert set(n for n, m in mounts.items() if "routerkey" in m) == {"postgres", "router", "agent", "commit-reviewer"}
    for name in ("router", "agent", "commit-reviewer"):
        assert services[name]["environment"]["MODEL_ROUTER_KEY_FILE"].startswith(mounts[name]["routerkey"]), name
    # The agent's own data: the agent alone.
    assert set(n for n, m in mounts.items() if "agentdata" in m) == {"agent"}
    # What the review services get from the agent: the shared volume, read-only.
    for name in ("agent-review", "commit-reviewer"):
        shared = mounts[name]["reviewshared"]
        env = services[name]["environment"]
        assert env["AGENT_PROJECTS_JSON"].startswith(shared) and env["REVIEW_CONTROL_SECRET_FILE"].startswith(shared), name
        ro = [v for v in services[name]["volumes"] if v["type"] == "volume" and v["source"] == "reviewshared"][0]
        assert ro.get("read_only") is True, f"{name} can write the shared volume"
    agent_env = services["agent"]["environment"]
    assert agent_env["AGENT_PROJECTS_JSON"].startswith(mounts["agent"]["reviewshared"])
    assert agent_env["TEKTONIX_SHARED_DIR"] == mounts["agent"]["reviewshared"]
    # The old shared volume stays mounted by postgres only, for the carry-over.
    assert set(n for n, m in mounts.items() if "bundlesecrets" in m) == {"postgres"}


def test_the_review_dashboard_can_read_the_router_s_ledger_and_key_in_the_bundle():
    """Its Router tab read services/model-router/logs/routing.jsonl and the
    router's .env: host paths the container does not have. The ledger is
    the routerlogs volume, named by the same variable the agent uses, and
    the key comes from the environment."""
    services = render()["services"]
    review = services["agent-review"]
    mounts = _volumes(review)
    assert review["environment"]["MODEL_ROUTER_LEDGER"].startswith(mounts["routerlogs"])
    assert review["environment"]["MODEL_ROUTER_LEDGER"] == services["agent"]["environment"]["MODEL_ROUTER_LEDGER"]
    ro = [v for v in review["volumes"] if v["type"] == "volume" and v["source"] == "routerlogs"][0]
    assert ro.get("read_only") is True
    assert review["environment"]["OPENROUTER_API_KEY"] == "placeholder"


def test_the_containers_that_write_into_the_operator_s_folder_take_puid_and_pgid():
    """Set from .env, applied by each entrypoint (tests/test_bundle_entrypoints.sh).
    Unset they default to 0, which is what every install before had."""
    services = render(PUID="1234", PGID="1235")["services"]
    for name in ("agent", "agent-review", "commit-reviewer"):
        env = services[name]["environment"]
        assert (env["PUID"], env["PGID"]) == ("1234", "1235"), name
    services = render()["services"]
    assert services["agent"]["environment"]["PUID"] == "0"


def test_every_long_lived_service_caps_its_logs():
    """json-file logs grow without bound; a busy agent writes gigabytes a
    month. The one-shot image build has nothing to say."""
    services = render()["services"]
    for name, svc in services.items():
        if str(svc.get("restart")) == "no":
            continue
        opts = (svc.get("logging") or {}).get("options") or {}
        assert opts.get("max-size") and opts.get("max-file"), f"{name} has no log limit"


def test_the_readme_lists_exactly_the_volumes_compose_declares():
    """docker/README.md said "four named volumes" while compose declared
    seven; the two an operator most needs to back up (the model pins and
    the generated secrets) were the ones missing."""
    import re
    readme = (REPO / "docker/README.md").read_text()
    section = readme.split("## Data", 1)[1].split("\n## ", 1)[0]
    listed = set(re.findall(r"`([a-z]+)`", section))
    declared = set(render()["volumes"])
    assert listed == declared, f"README lists {sorted(listed)}; compose declares {sorted(declared)}"


def test_only_the_sandbox_launcher_holds_the_docker_socket():
    """The agent held /var/run/docker.sock until 2026-10-23, which made a
    compromise of the agent process root on the host. The socket is the
    launcher's alone, the launcher is reachable from the agent alone, and
    its key is mounted by the two of them and the service that writes it."""
    rendered = render(PROJECTS_DIR="/srv/code")
    services = rendered["services"]
    holders = {name for name, svc in services.items()
               for v in svc.get("volumes", []) if "docker.sock" in str(v.get("source"))}
    assert holders == {"sandbox-launcher"}, holders

    on_launcher_net = {name for name, svc in services.items() if "launcher" in (svc.get("networks") or {})}
    assert on_launcher_net == {"agent", "sandbox-launcher"}, on_launcher_net
    assert rendered["networks"]["launcher"]["internal"] is True
    launcher = services["sandbox-launcher"]
    assert set(launcher["networks"]) == {"launcher"} and not launcher.get("ports")

    mounts = {name: _volumes(svc) for name, svc in services.items()}
    assert {n for n, m in mounts.items() if "launcherkey" in m} == {"postgres", "sandbox-launcher", "agent"}
    for name in ("sandbox-launcher", "agent"):
        env = services[name]["environment"]
        assert env["SANDBOX_LAUNCHER_KEY_FILE"].startswith(mounts[name]["launcherkey"]), name
        ro = [v for v in services[name]["volumes"] if v["type"] == "volume" and v["source"] == "launcherkey"][0]
        assert ro.get("read_only") is True, f"{name} can write the launcher key"

    # The agent hands the launcher paths under /projects; the launcher sees
    # the same folder there, read-only, and maps it to the host's own path.
    binds = {v["target"]: v for v in launcher["volumes"] if v["type"] == "bind"}
    assert binds["/projects"]["source"] == "/srv/code" and binds["/projects"].get("read_only") is True
    assert launcher["environment"]["SANDBOX_LAUNCHER_ROOT"] == "/projects"
    assert launcher["environment"]["SANDBOX_HOST_ROOT"] == "/srv/code"
    agent_env = services["agent"]["environment"]
    assert "AGENT_HOST_PATH_MAP" not in agent_env, "the launcher maps paths now"
    assert agent_env["SANDBOX_LAUNCHER_URL"] == "http://sandbox-launcher:8300"
    assert services["agent"]["depends_on"]["sandbox-launcher"]["condition"] == "service_healthy"
    # The database checks name the same network and containers on both sides.
    for key in ("REVIEW_CHECKS_NETWORK", "REVIEW_CHECKS_POSTGRES_CONTAINER", "REVIEW_CHECKS_REDIS_CONTAINER"):
        assert launcher["environment"][key] == agent_env[key], key
