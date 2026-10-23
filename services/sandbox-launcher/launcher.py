"""The bundle's sandbox launcher: the one container that holds the Docker socket.

The agent runs every command the model writes in a throwaway container. In
the bundle it used to start those itself, over /var/run/docker.sock mounted
into the agent container -- and anything that can reach that socket can ask
for a privileged container with / mounted, so whoever compromised the agent
process (a prompt injection that found a way out of the tool layer, a bad
dependency) had root on the host.

This service holds the socket instead, and offers the agent a handful of
operations with everything that decides what a container can do fixed here:

  * a run names an image, and the image must be on the allow-list (the
    sandbox image, the toolchain images in docker/stack-images.json, and
    SANDBOX_EXTRA_IMAGES from the operator's .env);
  * cpu, memory, swap and pids limits are this service's, never the
    request's; every capability is dropped, at most CHOWN, DAC_OVERRIDE and
    FOWNER are given back (the PUID hand-back, agent/tools/sandbox.py), and
    no-new-privileges is always set. There is no field for privileged mode,
    a user, devices, the pid or ipc namespace or a security option, and a
    request carrying any field this module does not know is refused whole;
  * every bind mount must resolve, symlinks followed, strictly inside one
    fixed root (SANDBOX_LAUNCHER_ROOT, the projects folder) and exist;
  * the network is none, the default bridge or the checks network, by name;
  * a published port is bound to 127.0.0.1 only;
  * every container it starts carries its label, and exec, kill, logs and
    state answer for labelled containers only;
  * two fixed operations set the database checks up (psql and a FLUSHDB in
    the two throwaway services), in containers named by this service's own
    environment.

Every call carries the shared secret generated at install
(docker/postgres/init-secrets.sh, the `launcherkey` volume), which only the
agent and this service mount. The standard library only, and the docker CLI:
nothing here is worth a dependency.
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

logger = logging.getLogger("sandbox-launcher")

LABEL = "tektonix.sandbox-launcher"
PIDS_LIMIT = "512"
ALLOWED_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER")
MAX_BODY = 1024 * 1024
MAX_OUTPUT_CHARS = 4 * 1024 * 1024
MAX_TIMEOUT_S = 4 * 60 * 60
MAX_ARGS = 512
MAX_ENV = 256
MAX_MOUNTS = 64

_NAME = re.compile(r"^[a-z][a-z0-9-]{2,62}$")
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_CONTAINER_PATH = re.compile(r"^/[^\x00-\x1f:,]{0,1023}$")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
# Mount targets the container's own system needs; the workspace laid over
# them is no host escape, but there is no reason to allow it either.
_FORBIDDEN_TARGETS = ("/proc", "/sys", "/dev")

RUN_FIELDS = {"name", "image", "argv", "entrypoint", "workdir", "env", "mounts", "network",
              "cap_add", "publish", "detach", "timeout_s"}
MOUNT_FIELDS = {"source", "target", "readonly"}


class Refused(ValueError):
    """The request asks for something this service will not do."""

    status = 400


class NotOurs(Refused):
    status = 403


class NotFound(Refused):
    status = 404


def _cap(value: str, default: str, pattern: str) -> str:
    value = (value or "").strip()
    return value if re.fullmatch(pattern, value) else default


class Config:
    """Everything that decides what a container may do, from this service's
    own environment. Read once at start."""

    def __init__(self, env: dict | None = None):
        env = dict(os.environ if env is None else env)
        self.root = os.path.realpath(env.get("SANDBOX_LAUNCHER_ROOT") or "/projects")
        # Where the daemon finds that root: the host's own path to the
        # projects folder (PROJECTS_DIR). Unset means the same path, which is
        # a launcher run directly on a host.
        self.host_root = (env.get("SANDBOX_HOST_ROOT") or "").strip().rstrip("/\\") or self.root
        self.memory = _cap(env.get("SANDBOX_MEMORY", ""), "2g", r"[1-9]\d*[gmGM]")
        self.cpus = _cap(env.get("SANDBOX_CPUS", ""), "2", r"[1-9]\d?(\.\d+)?")
        self.default_image = env.get("SANDBOX_DEFAULT_IMAGE") or "tektonix-sandbox:latest"
        self.build_context = env.get("SANDBOX_BUILD_CONTEXT") or "/app/docker/agent-sandbox"
        self.images = self._images(env)
        self.checks_network = env.get("REVIEW_CHECKS_NETWORK") or None
        self.checks_postgres = env.get("REVIEW_CHECKS_POSTGRES_CONTAINER") or None
        self.checks_redis = env.get("REVIEW_CHECKS_REDIS_CONTAINER") or None
        self.docker = env.get("SANDBOX_DOCKER") or "docker"

    def _images(self, env: dict) -> set[str]:
        images = {self.default_image}
        path = env.get("SANDBOX_STACK_IMAGES") or "/app/docker/stack-images.json"
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data.get("default"), str):
                images.add(data["default"])
            for entry in (data.get("stacks") or {}).values():
                if isinstance(entry, dict) and isinstance(entry.get("image"), str):
                    images.add(entry["image"])
        except (OSError, ValueError, AttributeError):
            logger.warning("no stack image list at %s; the sandbox image alone is allowed", path)
        images.update(i.strip() for i in (env.get("SANDBOX_EXTRA_IMAGES") or "").split(",") if i.strip())
        return images

    def networks(self) -> set[str]:
        return {"none", "bridge", *([self.checks_network] if self.checks_network else [])}

    def host_path(self, real: str) -> str:
        """The daemon's path for `real`, which is inside `root`."""
        if real == self.root:
            return self.host_root
        rest = real[len(self.root) + 1:]
        windows = "\\" in self.host_root or self.host_root[1:2] == ":"
        sep = "\\" if windows else "/"
        return f"{self.host_root}{sep}{rest.replace('/', sep)}"


# --- validation ---------------------------------------------------------------

def _string(value, what: str, limit: int = 65536) -> str:
    if not isinstance(value, str) or "\x00" in value or len(value) > limit:
        raise Refused(f"{what} must be a string without NUL bytes")
    return value


def _strings(value, what: str, limit: int) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise Refused(f"{what} must be a list of at most {limit} strings")
    return [_string(v, what) for v in value]


def checked_name(value) -> str:
    if not isinstance(value, str) or not _NAME.match(value):
        raise Refused("name must be lower-case letters, digits and dashes")
    return value


def _container_path(value, what: str) -> str:
    path = _string(value, what, 1024)
    if not _CONTAINER_PATH.match(path) or ".." in path.split("/"):
        raise Refused(f"{what} {path!r} is not a plain absolute path")
    return path.rstrip("/") or "/"


def checked_source(source, cfg: Config) -> str:
    """The real path of a mount source, which must exist strictly inside the
    fixed root. Symlinks are followed HERE, against the same tree the daemon
    sees, and the daemon is handed the resolved path: a symlink to / in a
    workspace names /, and is refused."""
    path = _string(source, "mount source", 4096)
    if not os.path.isabs(path):
        raise Refused("mount source must be absolute")
    real = os.path.realpath(path)
    if real == cfg.root or not real.startswith(cfg.root + os.sep):
        raise Refused(f"mount source {path!r} is not inside {cfg.root}")
    if not os.path.exists(real):
        # The daemon would create it on the host, as root.
        raise Refused(f"mount source {path!r} does not exist")
    if any(c in real for c in (":", ",")):
        raise Refused("mount source may not contain ':' or ','")
    return real


def _mount_args(mounts, cfg: Config) -> list[str]:
    if not isinstance(mounts, list) or len(mounts) > MAX_MOUNTS:
        raise Refused(f"mounts must be a list of at most {MAX_MOUNTS}")
    args: list[str] = []
    for m in mounts:
        if not isinstance(m, dict) or set(m) - MOUNT_FIELDS or not {"source", "target"} <= set(m):
            raise Refused("a mount is {source, target, readonly} and nothing else")
        real = checked_source(m["source"], cfg)
        target = _container_path(m["target"], "mount target")
        if target == "/" or any(target == f or target.startswith(f + "/") for f in _FORBIDDEN_TARGETS):
            raise Refused(f"mount target {target!r} is a system path")
        if not isinstance(m.get("readonly", False), bool):
            raise Refused("readonly must be true or false")
        args += ["-v", f"{cfg.host_path(real)}:{target}" + (":ro" if m.get("readonly") else "")]
    return args


def build_run_argv(spec: dict, cfg: Config) -> tuple[list[str], str]:
    """The `docker run` argv for one request, and the container's name.

    Pure apart from resolving mount sources, so every refusal is tested
    without a daemon."""
    if not isinstance(spec, dict):
        raise Refused("the body must be a JSON object")
    unknown = sorted(set(spec) - RUN_FIELDS)
    if unknown:
        raise Refused(f"unknown field(s) {', '.join(unknown)}: nothing beyond {sorted(RUN_FIELDS)} is accepted")
    name = checked_name(spec["name"]) if spec.get("name") is not None else f"sbx-{uuid.uuid4().hex[:12]}"

    image = _string(spec.get("image"), "image", 256)
    if image not in cfg.images:
        raise Refused(f"image {image!r} is not on the allow-list")
    argv = _strings(spec.get("argv") or [], "argv", MAX_ARGS)

    env = spec.get("env") or {}
    if not isinstance(env, dict) or len(env) > MAX_ENV:
        raise Refused(f"env must be an object of at most {MAX_ENV} entries")
    env_args: list[str] = []
    for k, v in env.items():
        if not isinstance(k, str) or not _ENV_KEY.match(k):
            raise Refused(f"env name {k!r} is not a plain identifier")
        env_args += ["-e", f"{k}={_string(v, f'env {k}')}"]

    network = spec.get("network")
    if network is not None and network not in cfg.networks():
        raise Refused(f"network {network!r} is not one of {sorted(cfg.networks())}")

    caps = _strings(spec.get("cap_add") or [], "cap_add", len(ALLOWED_CAPS))
    for cap in caps:
        if cap not in ALLOWED_CAPS:
            raise Refused(f"capability {cap!r} is not one of {list(ALLOWED_CAPS)}")

    detach = spec.get("detach", False)
    if not isinstance(detach, bool):
        raise Refused("detach must be true or false")
    publish_args: list[str] = []
    publish = spec.get("publish")
    if publish is not None:
        if not isinstance(publish, dict) or set(publish) != {"host_port", "container_port"}:
            raise Refused("publish is {host_port, container_port}")
        hp, cp = publish["host_port"], publish["container_port"]
        if not (isinstance(hp, int) and 1024 <= hp <= 65535 and isinstance(cp, int) and 1 <= cp <= 65535):
            raise Refused("publish ports are out of range")
        publish_args = ["-p", f"127.0.0.1:{hp}:{cp}"]

    entry: list[str] = []
    if spec.get("entrypoint") is not None:
        ep = _string(spec["entrypoint"], "entrypoint", 256)
        if not ep or ep.startswith("-"):
            raise Refused("entrypoint must be a program name")
        entry = ["--entrypoint", ep]

    workdir = _container_path(spec.get("workdir") or "/workspace", "workdir")
    mount_args = _mount_args(spec.get("mounts") or [], cfg)

    out = [
        cfg.docker, "run", "--rm", *(["-d"] if detach else []), "--name", name,
        "--label", f"{LABEL}=1",
        *mount_args,
        *(["--network", network] if network else []),
        *publish_args,
        "-w", workdir,
        "--memory", cfg.memory,
        "--memory-swap", cfg.memory,
        "--cpus", cfg.cpus,
        "--pids-limit", PIDS_LIMIT,
        "--cap-drop", "ALL",
        *[a for cap in caps for a in ("--cap-add", cap)],
        "--security-opt", "no-new-privileges",
        *entry,
        *env_args,
        image,
        *argv,
    ]
    return out, name


def checked_timeout(value, default: int = 300) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise Refused("timeout_s must be a positive whole number")
    return min(value, MAX_TIMEOUT_S)


# --- docker -----------------------------------------------------------------

def _text(raw: bytes | None, limit: int = MAX_OUTPUT_CHARS) -> str:
    return (raw or b"").decode("utf-8", errors="replace")[-limit:]


class Launcher:
    """The operations, each over the docker CLI. `run_docker` is the one
    place a process starts, so tests replace it."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._build_lock = threading.Lock()
        self._building = False

    def run_docker(self, args: list[str], timeout: float, stdin: bytes | None = None) -> tuple[int, str]:
        """(exit code, combined output); raises subprocess.TimeoutExpired."""
        proc = subprocess.run(args, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              stdin=None if stdin is not None else subprocess.DEVNULL, timeout=timeout)
        return proc.returncode, _text(proc.stdout)

    def _docker(self, *args: str, timeout: float = 30, stdin: bytes | None = None) -> tuple[int, str]:
        try:
            return self.run_docker([self.cfg.docker, *args], timeout, stdin)
        except subprocess.TimeoutExpired:
            return 124, f"docker {args[0]} timed out after {int(timeout)}s"
        except OSError as e:
            return 125, f"docker could not be started: {e}"

    # runs
    def run(self, spec: dict) -> dict:
        argv, name = build_run_argv(spec, self.cfg)
        if spec.get("detach"):
            code, out = self._docker(*argv[1:], timeout=120)
            return {"name": name, "exit_code": code, "output": out[-4000:]}
        timeout = checked_timeout(spec.get("timeout_s"))
        try:
            code, out = self.run_docker(argv, timeout)
        except subprocess.TimeoutExpired as e:
            # `docker run --rm` cleans up on a normal exit only: killing the
            # CLI leaves the container running. Kill the container itself.
            self._docker("kill", name)
            return {"name": name, "exit_code": 124, "timed_out": True, "output": _text(e.output)}
        except OSError as e:
            return {"name": name, "exit_code": 125, "output": f"docker could not be started: {e}"}
        return {"name": name, "exit_code": code, "output": out}

    # containers this service started, and no others
    def owned(self, name) -> str:
        name = checked_name(name)
        code, out = self._docker("inspect", "--format", "{{index .Config.Labels \"%s\"}}" % LABEL, name)
        if code != 0:
            raise NotFound(f"no container {name}")
        if out.strip() != "1":
            raise NotOurs(f"{name} was not started by the sandbox launcher")
        return name

    def kill(self, body: dict) -> dict:
        name = self.owned(body.get("name"))
        code, out = self._docker("kill", name)
        return {"ok": code == 0, "output": out[-2000:]}

    def remove(self, body: dict) -> dict:
        name = self.owned(body.get("name"))
        code, out = self._docker("rm", "-f", name)
        return {"ok": code == 0, "output": out[-2000:]}

    def state(self, body: dict) -> dict:
        try:
            name = self.owned(body.get("name"))
        except NotFound:
            return {"running": False, "exists": False}
        code, out = self._docker("inspect", "--format", "{{.State.Running}}", name)
        return {"running": code == 0 and out.strip() == "true", "exists": code == 0}

    def logs(self, body: dict) -> dict:
        name = self.owned(body.get("name"))
        tail = body.get("tail", 60)
        if not isinstance(tail, int) or isinstance(tail, bool) or not 1 <= tail <= 5000:
            raise Refused("tail must be 1..5000")
        code, out = self._docker("logs", "--tail", str(tail), name, timeout=20)
        return {"ok": code == 0, "output": out[-200_000:]}

    def exec(self, body: dict) -> dict:
        name = self.owned(body.get("name"))
        argv = _strings(body.get("argv"), "argv", MAX_ARGS)
        if not argv:
            raise Refused("argv must name a program")
        code, out = self._docker("exec", name, *argv, timeout=checked_timeout(body.get("timeout_s"), 60))
        return {"exit_code": code, "output": out}

    # images: the allow-listed ones only
    def status(self) -> dict:
        code, out = self._docker("version", "--format", "{{.Server.Version}}")
        return {"ok": code == 0, "docker": out.strip() if code == 0 else None,
                "error": None if code == 0 else out[-300:], "building": self._building}

    def image_present(self, body: dict) -> dict:
        image = _string(body.get("image"), "image", 256)
        if image not in self.cfg.images:
            raise Refused(f"image {image!r} is not on the allow-list")
        code, _ = self._docker("image", "inspect", image, "--format", "{{.Id}}")
        return {"present": code == 0, "building": self._building and image == self.cfg.default_image}

    def build_default(self, _body: dict | None = None) -> dict:
        """Build the sandbox image from the context baked into this image, in
        the background. Only that image, only from that context."""
        if not os.path.isfile(os.path.join(self.cfg.build_context, "Dockerfile")):
            return {"building": False, "error": f"no build context at {self.cfg.build_context}"}
        with self._build_lock:
            if self._building:
                return {"building": True}
            self._building = True
        threading.Thread(target=self._build, daemon=True).start()
        return {"building": True}

    def _build(self) -> None:
        try:
            code, out = self._docker("build", "-q", "-t", self.cfg.default_image, self.cfg.build_context,
                                     timeout=1800)
            if code == 0:
                logger.info("built %s", self.cfg.default_image)
            else:
                logger.error("building %s failed: %s", self.cfg.default_image, out[-500:])
        finally:
            self._building = False

    def ensure_default_image(self) -> None:
        """At start: the desktop app runs pulled images with --no-build, and
        a stack without the sandbox image fails on its first command."""
        if self._docker("image", "inspect", self.cfg.default_image)[0] != 0:
            logger.info("%s is missing; building it", self.cfg.default_image)
            self.build_default()

    # the database checks' two throwaway services, by this service's names
    def checks_psql(self, body: dict) -> dict:
        if not self.cfg.checks_postgres:
            raise Refused("REVIEW_CHECKS_POSTGRES_CONTAINER is not set on the launcher")
        sql = _string(body.get("sql"), "sql")
        user, db = body.get("user"), body.get("db")
        if not (isinstance(user, str) and _IDENT.match(user) and isinstance(db, str) and _IDENT.match(db)):
            raise Refused("user and db must be plain identifiers")
        mode = "-At" if body.get("rows") else "-q"
        code, out = self._docker("exec", "-i", self.cfg.checks_postgres, "psql", "-v", "ON_ERROR_STOP=1",
                                 mode, "-U", user, "-d", db, timeout=60, stdin=sql.encode())
        return {"ok": code == 0, "output": out[-200_000:]}

    def checks_redis_flush(self, body: dict) -> dict:
        if not self.cfg.checks_redis:
            raise Refused("REVIEW_CHECKS_REDIS_CONTAINER is not set on the launcher")
        db = body.get("db")
        if not isinstance(db, int) or isinstance(db, bool) or not 0 <= db <= 15:
            raise Refused("db must be 0..15")
        code, out = self._docker("exec", self.cfg.checks_redis, "redis-cli", "-n", str(db), "FLUSHDB",
                                 timeout=30)
        return {"ok": code == 0, "output": out[-2000:]}


# --- HTTP ----------------------------------------------------------------------

ROUTES = {
    "/v1/run": "run",
    "/v1/containers/kill": "kill",
    "/v1/containers/remove": "remove",
    "/v1/containers/state": "state",
    "/v1/containers/logs": "logs",
    "/v1/containers/exec": "exec",
    "/v1/images/present": "image_present",
    "/v1/images/build": "build_default",
    "/v1/checks/psql": "checks_psql",
    "/v1/checks/redis-flush": "checks_redis_flush",
}


def make_handler(launcher: Launcher, key: str):
    if not key:
        raise SystemExit("the sandbox launcher has no key; refusing to start")
    expected = f"Bearer {key}".encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "tektonix-sandbox-launcher"

        def log_message(self, fmt, *args):  # noqa: D401 -- one line per call, into our logger
            if self.path != "/health":       # the healthcheck, every few seconds
                logger.info("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _authorised(self) -> bool:
            given = (self.headers.get("Authorization") or "").encode()
            if hmac.compare_digest(given, expected):
                return True
            self._send(401, {"error": "missing or wrong launcher key"})
            return False

        def do_GET(self):  # noqa: N802 -- http.server's name
            if self.path == "/health":
                return self._send(200, {"ok": True})
            if not self._authorised():
                return None
            if self.path == "/v1/status":
                return self._send(200, launcher.status())
            return self._send(404, {"error": "no such route"})

        def do_POST(self):  # noqa: N802
            if not self._authorised():
                return None
            op = ROUTES.get(self.path)
            if not op:
                return self._send(404, {"error": "no such route"})
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                return self._send(413, {"error": f"the body must be at most {MAX_BODY} bytes"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._send(400, {"error": "the body is not JSON"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "the body must be a JSON object"})
            try:
                return self._send(200, getattr(launcher, op)(body))
            except Refused as e:
                logger.warning("refused %s: %s", self.path, e)
                return self._send(e.status, {"error": str(e)})
            except Exception as e:  # noqa: BLE001 -- the caller gets a reason, the log the trace
                logger.exception("%s failed", self.path)
                return self._send(500, {"error": f"{type(e).__name__}: {e}"[:500]})

    return Handler


def read_key(env: dict | None = None) -> str:
    env = os.environ if env is None else env
    if env.get("SANDBOX_LAUNCHER_KEY"):
        return env["SANDBOX_LAUNCHER_KEY"].strip()
    path = env.get("SANDBOX_LAUNCHER_KEY_FILE")
    if path:
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read().strip()
        except OSError:
            return ""
    return ""


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="[sandbox-launcher] %(levelname)s %(message)s")
    cfg = Config()
    launcher = Launcher(cfg)
    handler = make_handler(launcher, read_key())
    port = int(os.environ.get("SANDBOX_LAUNCHER_PORT") or 8300)
    logger.info("allowing %d image(s); mounts under %s (the daemon's %s)", len(cfg.images), cfg.root, cfg.host_root)
    launcher.ensure_default_image()
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()


if __name__ == "__main__":
    main()
