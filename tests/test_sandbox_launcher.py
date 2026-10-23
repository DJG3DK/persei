"""The bundle's sandbox launcher: the one service that holds the Docker socket.

The agent used to hold /var/run/docker.sock itself, which made a compromise
of the agent process root on the host. services/sandbox-launcher/launcher.py
holds it now and starts what the agent asks for only within its own rules.
These tests pin those rules -- an image off the allow-list, privileged mode,
a capability beyond the three, a mount outside the projects root, a
container it did not label, a missing or wrong key are all refused -- and
that the argv the agent really builds (agent/tools/sandbox.py,
agent/review_sandbox.py) goes through them unchanged in meaning.
"""
from __future__ import annotations

import importlib.util
import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from agent import paths
from agent.tools import sandbox as sb
from agent.tools import sandbox_launcher as client

_spec = importlib.util.spec_from_file_location(
    "tektonix_sandbox_launcher", paths.REPO_ROOT / "services" / "sandbox-launcher" / "launcher.py")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)

KEY = "k" * 64


@pytest.fixture
def root(tmp_path):
    """The projects folder as the launcher sees it, with one workspace."""
    projects = tmp_path / "projects"
    (projects / ".tektonix-workspaces" / "shop").mkdir(parents=True)
    (projects / "shop" / ".git").mkdir(parents=True)
    return projects


@pytest.fixture
def cfg(root):
    return launcher.Config({
        "SANDBOX_LAUNCHER_ROOT": str(root),
        "SANDBOX_HOST_ROOT": "/srv/code",
        "SANDBOX_CPUS": "3",
        "SANDBOX_MEMORY": "3g",
        "SANDBOX_STACK_IMAGES": str(paths.REPO_ROOT / "docker" / "stack-images.json"),
        "SANDBOX_EXTRA_IMAGES": "example/extra:1",
        "REVIEW_CHECKS_NETWORK": "tektonix_checks",
        "REVIEW_CHECKS_POSTGRES_CONTAINER": "tektonix-checks-postgres",
        "REVIEW_CHECKS_REDIS_CONTAINER": "tektonix-checks-redis",
    })


def _spec_for(root, **kw):
    base = {"name": "lga-0123456789ab", "image": "tektonix-sandbox:latest", "argv": ["sh", "-c", "true"],
            "mounts": [{"source": str(root / ".tektonix-workspaces" / "shop"), "target": "/workspace"}]}
    base.update(kw)
    return base


# --- what a run may ask for ------------------------------------------------------

def test_a_valid_run_gets_the_launchers_own_limits_and_label(cfg, root):
    argv, name = launcher.build_run_argv(_spec_for(root, cap_add=["CHOWN"], network="none",
                                                   env={"CI": "true"}), cfg)
    joined = " ".join(argv)
    assert name == "lga-0123456789ab"
    for flag in ("--rm", "--label tektonix.sandbox-launcher=1", "--memory 3g", "--memory-swap 3g",
                 "--cpus 3", "--pids-limit 512", "--cap-drop ALL", "--cap-add CHOWN",
                 "--security-opt no-new-privileges", "--network none", "-e CI=true", "-w /workspace"):
        assert flag in joined, flag
    # The daemon gets the HOST's path for the workspace.
    assert "/srv/code/.tektonix-workspaces/shop:/workspace" in argv
    assert argv[-4:] == ["tektonix-sandbox:latest", "sh", "-c", "true"]
    assert "--privileged" not in argv


def test_a_windows_host_root_is_translated_with_its_own_separators(root):
    cfg = launcher.Config({"SANDBOX_LAUNCHER_ROOT": str(root), "SANDBOX_HOST_ROOT": "C:\\Users\\me\\code"})
    argv, _ = launcher.build_run_argv(_spec_for(root), cfg)
    assert "C:\\Users\\me\\code\\.tektonix-workspaces\\shop:/workspace" in argv


@pytest.mark.parametrize("image", ["alpine:latest", "tektonix-sandbox", "tektonix-sandbox:latest ",
                                   "docker.io/library/ubuntu", ""])
def test_an_image_off_the_allow_list_is_refused(cfg, root, image):
    with pytest.raises(launcher.Refused, match="allow-list|string"):
        launcher.build_run_argv(_spec_for(root, image=image), cfg)


def test_the_allow_list_is_the_sandbox_image_the_stack_images_and_the_operators_extras(cfg, root):
    stacks = json.loads((paths.REPO_ROOT / "docker" / "stack-images.json").read_text())["stacks"]
    for image in ["tektonix-sandbox:latest", "example/extra:1", *(s["image"] for s in stacks.values())]:
        launcher.build_run_argv(_spec_for(root, image=image), cfg)


@pytest.mark.parametrize("field,value", [
    ("privileged", True), ("user", "0"), ("devices", ["/dev/sda"]), ("pid", "host"),
    ("security_opt", ["seccomp=unconfined"]), ("cap_drop", []), ("volumes_from", ["tektonix-agent-1"]),
])
def test_a_field_the_launcher_does_not_know_is_refused_whole(cfg, root, field, value):
    with pytest.raises(launcher.Refused, match="unknown field"):
        launcher.build_run_argv(_spec_for(root, **{field: value}), cfg)


@pytest.mark.parametrize("cap", ["SYS_ADMIN", "NET_ADMIN", "ALL", "SYS_PTRACE", "chown"])
def test_a_capability_beyond_the_three_is_refused(cfg, root, cap):
    with pytest.raises(launcher.Refused, match="capability"):
        launcher.build_run_argv(_spec_for(root, cap_add=[cap]), cfg)


@pytest.mark.parametrize("network", ["host", "container:tektonix-agent-1", "tektonix_default", "default"])
def test_a_network_other_than_none_bridge_and_checks_is_refused(cfg, root, network):
    with pytest.raises(launcher.Refused, match="network"):
        launcher.build_run_argv(_spec_for(root, network=network), cfg)
    for ok in ("none", "bridge", "tektonix_checks", None):
        launcher.build_run_argv(_spec_for(root, network=ok), cfg)


@pytest.mark.parametrize("source", [
    "/", "/etc", "/var/run/docker.sock", "/root/.ssh",
    "{root}",                         # the root itself: every project at once
    "{root}/../outside",              # climbs out
    "{root}/does-not-exist",          # the daemon would create it on the host, as root
    "relative/path",
])
def test_a_mount_outside_the_projects_root_is_refused(cfg, root, source):
    (root.parent / "outside").mkdir(exist_ok=True)
    with pytest.raises(launcher.Refused, match="mount source"):
        launcher.build_run_argv(_spec_for(root, mounts=[{"source": source.format(root=root), "target": "/x"}]),
                                cfg)


def test_a_symlink_out_of_the_root_is_followed_and_refused(cfg, root):
    ws = root / ".tektonix-workspaces" / "shop"
    (ws / "node_modules").symlink_to("/etc")
    with pytest.raises(launcher.Refused, match="not inside"):
        launcher.build_run_argv(_spec_for(root, mounts=[{"source": str(ws / "node_modules"),
                                                         "target": "/n", "readonly": True}]), cfg)


def test_a_symlink_inside_the_root_is_mounted_by_its_real_path(cfg, root):
    ws = root / ".tektonix-workspaces" / "shop"
    (ws / "git").symlink_to(root / "shop" / ".git")
    argv, _ = launcher.build_run_argv(_spec_for(root, mounts=[
        {"source": str(ws / "git"), "target": str(root / "shop" / ".git"), "readonly": True}]), cfg)
    assert f"/srv/code/shop/.git:{root}/shop/.git:ro" in argv


@pytest.mark.parametrize("target", ["/", "/proc", "/sys/fs", "/dev", "/a:/b", "/a,b", "relative", "/a/../b"])
def test_a_mount_target_on_a_system_path_or_smuggling_a_field_is_refused(cfg, root, target):
    with pytest.raises(launcher.Refused):
        launcher.build_run_argv(_spec_for(root, mounts=[{"source": str(root / "shop"), "target": target}]), cfg)


def test_a_published_port_is_loopback_only(cfg, root):
    argv, _ = launcher.build_run_argv(_spec_for(root, detach=True,
                                                publish={"host_port": 40000, "container_port": 3000}), cfg)
    assert argv[argv.index("-p") + 1] == "127.0.0.1:40000:3000"
    assert "-d" in argv
    for bad in ({"host_port": 80, "container_port": 3000}, {"host_port": 40000, "container_port": 3000, "ip": "0.0.0.0"}):
        with pytest.raises(launcher.Refused):
            launcher.build_run_argv(_spec_for(root, publish=bad), cfg)


@pytest.mark.parametrize("name", ["tektonix_agent", "-rm", "A", "x" * 80, "../x"])
def test_a_container_name_is_plain(cfg, root, name):
    with pytest.raises(launcher.Refused, match="name"):
        launcher.build_run_argv(_spec_for(root, name=name), cfg)


# --- containers it did not start ----------------------------------------------

class FakeDocker(launcher.Launcher):
    """Records every docker call; `labels` is what inspect answers per name."""

    def __init__(self, cfg, labels=None):
        super().__init__(cfg)
        self.labels = labels or {}
        self.calls: list[list[str]] = []

    def run_docker(self, args, timeout, stdin=None):
        self.calls.append(list(args))
        if args[1] == "inspect" and "--format" in args and "Labels" in args[args.index("--format") + 1]:
            name = args[-1]
            if name not in self.labels:
                return 1, "Error: No such object"
            return 0, self.labels[name] + "\n"
        if args[1] == "inspect":
            return 0, "true\n"
        if args[1] == "version":
            return 0, "27.0.0\n"
        return 0, "done"


@pytest.mark.parametrize("op,body", [
    ("kill", {}), ("remove", {}), ("logs", {"tail": 10}), ("exec", {"argv": ["sh", "-c", "id"]}),
])
def test_a_container_without_the_launchers_label_is_not_touched(cfg, op, body):
    fake = FakeDocker(cfg, labels={"tektonix-agent-1": "", "lga-0123456789ab": "1"})
    with pytest.raises(launcher.NotOurs):
        getattr(fake, op)({"name": "tektonix-agent-1", **body})
    assert all(c[1] == "inspect" for c in fake.calls), "only the label was read"
    getattr(fake, op)({"name": "lga-0123456789ab", **body})
    assert fake.calls[-1][1] in ("kill", "rm", "logs", "exec")


def test_an_unknown_container_is_not_found_and_reads_as_not_running(cfg):
    fake = FakeDocker(cfg)
    with pytest.raises(launcher.NotFound):
        fake.kill({"name": "lga-gone0000000"})
    assert fake.state({"name": "lga-gone0000000"}) == {"running": False, "exists": False}


def test_the_database_check_operations_use_the_launchers_own_containers(cfg):
    fake = FakeDocker(cfg)
    fake.checks_psql({"sql": "SELECT 1;", "user": "checks", "db": "postgres", "rows": True})
    assert fake.calls[-1][:4] == ["docker", "exec", "-i", "tektonix-checks-postgres"]
    assert "-At" in fake.calls[-1]
    fake.checks_redis_flush({"db": 3})
    assert fake.calls[-1] == ["docker", "exec", "tektonix-checks-redis", "redis-cli", "-n", "3", "FLUSHDB"]
    for bad in ({"db": 16}, {"db": -1}, {"db": "0"}, {"db": True}):
        with pytest.raises(launcher.Refused):
            fake.checks_redis_flush(bad)
    with pytest.raises(launcher.Refused):
        fake.checks_psql({"sql": "x", "user": "checks; rm -rf /", "db": "postgres"})


def test_an_image_question_is_answered_for_allow_listed_images_only(cfg):
    fake = FakeDocker(cfg)
    assert fake.image_present({"image": "tektonix-sandbox:latest"})["present"] is True
    with pytest.raises(launcher.Refused):
        fake.image_present({"image": "alpine:latest"})


def test_a_run_that_overruns_is_killed_by_the_launcher(cfg, root):
    import subprocess

    class Slow(FakeDocker):
        def run_docker(self, args, timeout, stdin=None):
            if args[1] == "run":
                self.calls.append(list(args))
                raise subprocess.TimeoutExpired(args, timeout, output=b"partial")
            return super().run_docker(args, timeout, stdin)

    slow = Slow(cfg)
    r = slow.run(_spec_for(root, timeout_s=5))
    assert r["timed_out"] is True and r["exit_code"] == 124 and r["output"] == "partial"
    assert slow.calls[-1] == ["docker", "kill", "lga-0123456789ab"]


# --- the key -------------------------------------------------------------------

@pytest.fixture
def server(cfg):
    fake = FakeDocker(cfg, labels={"lga-0123456789ab": "1"})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), launcher.make_handler(fake, KEY))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", fake
    httpd.shutdown()


def _post(url, body, key=KEY):
    headers = {"Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.mark.parametrize("key", [None, "", "wrong", KEY + "x", KEY[:-1], f"Basic {KEY}"])
def test_a_missing_or_wrong_key_is_refused_before_anything_runs(server, root, key):
    url, fake = server
    status, body = _post(url + "/v1/run", _spec_for(root), key=key)
    assert status == 401, body
    status, _ = _post(url + "/v1/containers/kill", {"name": "lga-0123456789ab"}, key=key)
    assert status == 401
    assert fake.calls == [], "nothing reached docker"


def test_with_the_key_a_run_reaches_docker_and_a_refusal_is_a_400(server, root):
    url, fake = server
    status, body = _post(url + "/v1/run", _spec_for(root))
    assert status == 200 and body["exit_code"] == 0, body
    assert fake.calls[-1][:3] == ["docker", "run", "--rm"]
    status, body = _post(url + "/v1/run", _spec_for(root, image="alpine:latest"))
    assert status == 400 and "allow-list" in body["error"]
    status, body = _post(url + "/v1/run", _spec_for(root, privileged=True))
    assert status == 400 and "privileged" in body["error"]
    status, _ = _post(url + "/v1/containers/kill", {"name": "tektonix-agent-1"})
    assert status == 404


def test_health_answers_without_the_key_and_says_nothing_else(server):
    url, _ = server
    with urllib.request.urlopen(url + "/health", timeout=5) as r:
        assert json.loads(r.read()) == {"ok": True}
    req = urllib.request.Request(url + "/v1/status")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=5)
    assert e.value.code == 401


def test_the_launcher_will_not_start_without_a_key(cfg):
    with pytest.raises(SystemExit):
        launcher.make_handler(launcher.Launcher(cfg), "")
    assert launcher.read_key({"SANDBOX_LAUNCHER_KEY_FILE": "/no/such/file"}) == ""


# --- the agent's side ------------------------------------------------------------

def test_the_agents_shell_argv_translates_into_a_request_the_launcher_accepts(cfg, root, monkeypatch):
    """The real argv run_shell_sandboxed builds, in the bundle, through the
    client's translation and the launcher's checks."""
    monkeypatch.setenv("TEKTONIX_BUNDLE", "1")
    monkeypatch.setenv("AGENT_HOST_PATH_MAP", "/projects=/should-not-be-used")
    monkeypatch.setenv("PUID", "1000")
    monkeypatch.setenv("PGID", "1000")
    seen = {}

    async def fake_call(path, body=None, timeout=60):
        seen[path] = body
        return {"exit_code": 0, "output": "hello"}

    monkeypatch.setattr(client, "call", fake_call)
    ws = str(root / ".tektonix-workspaces" / "shop")
    import asyncio
    r = asyncio.run(sb.run_shell_sandboxed("echo hello", ws, timeout=7, network="none"))
    assert r == {"ok": True, "exit_code": 0, "output": "hello"}
    spec = seen["/v1/run"]
    assert spec["timeout_s"] == 7 and spec["network"] == "none"
    assert spec["mounts"][0] == {"source": ws, "target": "/workspace", "readonly": False}, \
        "the agent names the path as it sees it; the launcher maps it"
    assert set(spec["cap_add"]) == {"CHOWN", "DAC_OVERRIDE", "FOWNER"}
    spec.pop("timeout_s")
    argv, _ = launcher.build_run_argv(spec, cfg)
    assert "/srv/code/.tektonix-workspaces/shop:/workspace" in argv


def test_the_review_checks_argv_translates_too(cfg, root):
    argv = ["docker", "run", "--rm", "--name", "rvw-0123456789ab",
            "-v", f"{root}/.tektonix-workspaces/shop:/workspace",
            "-v", f"{root}/shop/.git:{root}/shop/.git:ro",
            "--network", "tektonix_checks", "-w", "/workspace/api",
            "--memory", "9g", "--memory-swap", "9g", "--cpus", "16", "--pids-limit", "99999",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--entrypoint", "npm", "-e", "CI=true", "tektonix-sandbox:latest", "test"]
    spec = client.spec_from_run_argv(argv)
    assert spec["entrypoint"] == "npm" and spec["argv"] == ["test"]
    assert spec["mounts"][1] == {"source": f"{root}/shop/.git", "target": f"{root}/shop/.git", "readonly": True}
    out, _ = launcher.build_run_argv(spec, cfg)
    # What the agent asked for as limits is not what runs.
    assert out[out.index("--memory") + 1] == "3g" and out[out.index("--cpus") + 1] == "3"
    assert out[out.index("--pids-limit") + 1] == "512"


@pytest.mark.parametrize("flag", ["--privileged", "--user", "--pid", "-p"])
def test_the_client_refuses_to_translate_a_flag_it_does_not_know(flag):
    argv = ["docker", "run", "--rm", flag, "x" if flag != "-p" else "0.0.0.0:80:80", "img"]
    if flag == "--privileged":
        argv = ["docker", "run", "--privileged", "img"]
    with pytest.raises(ValueError):
        client.spec_from_run_argv(argv)


def test_outside_the_bundle_the_agent_still_calls_docker_directly(monkeypatch):
    monkeypatch.delenv("TEKTONIX_BUNDLE", raising=False)
    assert not client.enabled()
    monkeypatch.setenv("AGENT_HOST_PATH_MAP", "/projects=/srv/code")
    assert sb.host_path("/projects/shop") == "/srv/code/shop"
    monkeypatch.setenv("TEKTONIX_BUNDLE", "1")
    assert sb.host_path("/projects/shop") == "/projects/shop"


def test_an_unreachable_launcher_is_a_command_that_did_not_run(monkeypatch, tmp_path):
    monkeypatch.setenv("TEKTONIX_BUNDLE", "1")
    monkeypatch.setenv("SANDBOX_LAUNCHER_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("SANDBOX_LAUNCHER_KEY", KEY)
    import asyncio
    r = asyncio.run(sb.run_shell_sandboxed("true", str(tmp_path), timeout=5))
    assert r["ok"] is False and r["exit_code"] == client.UNAVAILABLE
    assert "sandbox launcher" in r["output"]


def test_a_preview_and_its_teardown_go_through_the_launcher(cfg, root, monkeypatch):
    monkeypatch.setenv("TEKTONIX_BUNDLE", "1")
    monkeypatch.setenv("PUID", "1000")
    monkeypatch.setenv("PGID", "1000")
    calls = []

    async def fake_call(path, body=None, timeout=60):
        calls.append((path, body))
        return {"exit_code": 0, "output": "", "running": True}

    monkeypatch.setattr(client, "call", fake_call)
    ws = str(root / ".tektonix-workspaces" / "shop")
    import asyncio

    async def go():
        started = await sb.start_preview_container("npm run dev", ws, 3000)
        await sb.stop_preview_container(started["container"])
        return started

    started = asyncio.run(go())
    assert started["ok"] is True
    spec = calls[0][1]
    assert calls[0][0] == "/v1/run" and spec["detach"] is True
    assert spec["publish"] == {"host_port": started["port"], "container_port": 3000}
    argv, _ = launcher.build_run_argv(spec, cfg)
    assert argv[argv.index("-p") + 1] == f"127.0.0.1:{started['port']}:3000"
    assert [p for p, _ in calls[1:]] == ["/v1/containers/exec", "/v1/containers/kill"]
    assert all(b["name"] == started["container"] for _, b in calls[1:])
