# Running the bundle

One command, any host with Docker: Windows, macOS or Linux. See
`docs/roadmap-packaging.md` for where this fits and what it does not cover yet.

```
cp docker/.env.example .env      # set OPENROUTER_API_KEY and PROJECTS_DIR
docker compose up -d
```

**On Windows, the desktop app (`app/`, an installer on each release) does all of this in a window. The script below still works:** double-click `Install Persei.bat`. It asks for the two
values, starts Docker Desktop if it is installed but not running, waits for
it, brings the stack up and offers to open the console. The window stays open
at the end, so a failure is readable rather than a flash of text.

From a terminal, or on macOS and Linux with PowerShell 7:

```
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

The `.bat` exists because neither half works without it. A `.ps1` cannot be
double-clicked into running -- Explorer opens it in Notepad -- and Windows
client editions default to an execution policy of Restricted, so even from a
terminal the bare path fails with "running scripts is disabled on this
system". The flag applies to that one process and changes nothing about your
machine.

It does exactly the two steps above: checks Docker is
installed AND running, asks for the two values with no sensible default,
writes the `.env`, and brings the stack up. `-DryRun` prints every action
without performing one; `-Yes` never prompts and fails naming whatever is
missing. Re-running is safe, keeps every value already in your `.env`, and is
also the upgrade path.

(The `cp` above is the reason it exists: Windows has no such command, so the
documented first step could not be followed as written.)

Then open <http://localhost:8100> and sign in as `ADMIN_EMAIL` from your
`.env` (`admin@example.com` unless you changed it). The one-time password is
shown by

```bash
docker compose exec agent python scripts/show_initial_password.py
```

It is shown once, and you change it on first login.

## Upgrading

```bash
git pull
docker compose up -d
```

`up -d` builds whatever changed, from cache, so it is seconds when nothing
did and a few minutes after a release. No `--build` needed.

**Installed before 2026-09-28?** The compose project was called
`three-d-agent` then, and its volumes carry that prefix. Without the two
lines below, `up -d` starts a second, empty stack named `tektonix` beside
your data:

```
COMPOSE_PROJECT_NAME=three-d-agent
POSTGRES_DB=three_d_agent
```

Put them in `.env` before the first `up -d` after the pull.

## What you need

Docker Desktop (Windows/macOS) or Docker Engine (Linux). Nothing else —
Python, Node, Postgres and ripgrep all live in the images.

## The two settings that matter

**`PROJECTS_DIR`** is the folder the agent works in, *on the host*. A
repository already there appears to the agent as `/projects/<name>`, and one
it clones from GitHub for you lands there too: on the Projects page, paste
the repository URL instead of a path and press Clone. An empty folder is fine
if everything you have is on GitHub; a private repository needs a token
(`GITHUB_TOKEN` in `.env`, or one added under Settings, GitHub).

```
Linux/macOS   PROJECTS_DIR=/home/you/code
Windows       PROJECTS_DIR=C:\Users\you\code
```

**`BIND_ADDRESS`** defaults to `127.0.0.1`, so the dashboard is reachable only
from the machine running it. That is the intended shape for a local install:
no domain, no TLS, nothing exposed. Change it deliberately, and put something
in front of it if you do. In the desktop app, changing it also ends the
relaxed sign-in the app gets on its own machine: two-factor is required for
the admin again, and sessions last seven days (SECURITY.md, "Signing in").

## Why the Docker socket is mounted

The agent runs every command the model issues inside a throwaway container. In
the bundle it asks the *host's* Docker daemon to start those, so they are
siblings of the agent container rather than nested inside it — no privileged
mode, no second storage driver.

The consequence is the one thing to understand here: **bind mounts are resolved
by the host daemon, not by the agent container.** The agent sees a project at
`/projects/foo`; the daemon must be told `C:\Users\you\code\foo`. That is what
`AGENT_HOST_PATH_MAP` does (compose sets it from `PROJECTS_DIR`), and it is why
a wrong `PROJECTS_DIR` produces an agent that reads an *empty* repo rather than
an error.

## First run

Compose builds the sandbox image (the `sandbox-image` service) before the
agent starts. The agent's entrypoint then generates `AUTH_SECRET_KEY` into
the data volume and `REVIEW_CONTROL_SECRET` into the volume it shares with
the review services, writes an empty `projects.json` beside it, and waits
for Postgres. All of it is idempotent: `down` and `up` again changes
nothing.

On Linux, set `PUID` and `PGID` in `.env` to your own ids (`id -u`, `id -g`)
so the files the agent and the review services create in `PROJECTS_DIR` --
worktrees, commits, merges -- are yours rather than root's. Each container
chowns its own volumes and drops to that user at start. Unset, they run as
root, which is what Docker Desktop's bind mounts want and what every install
before 2026-09-29 did.

## The review gate is in the bundle

`agent-review` and `commit-reviewer` come up with everything else. A task's
branch is reviewed by a second model and only a READY verdict merges, which is
the whole point of the thing and used to be missing from the easiest install.

They mount, read-only, the one volume the agent shares with them
(`reviewshared`): the control secret it generates on first run, so all three
already agree, and the same `projects.json` a project onboarded from the
dashboard writes. Nothing else of the agent's -- not its signing key, not
the database password -- is reachable from either of them.

The agent does not become ready until the router answers, and the reviewers
do not become ready until the agent does. A first task that used to start
while the router was still booting died mid-call with "peer closed
connection". `depends_on: service_healthy` is what closes that.

## What is not in the bundle yet

* **Deploying after a merge.** The review services can merge, but not restart
  your app: pm2 runs on the host and a container cannot reach it. That endpoint
  reports itself unavailable rather than failing, so a task ends at a real
  merge. Restart it yourself, or use the host install if you want that
  automated.
* **A project that lives only on GitHub.** The dashboard clones it here and
  ships as a pull request (M2). The clone is still on this machine; there is
  no remote execution.
* **Checks run in a sandbox the agent starts, not in the reviewer.** The
  reviewer is deliberately NOT given `/var/run/docker.sock` (the socket would
  make it host-root equivalent), and it holds the merge secret, so it runs no
  agent-written code itself. It asks the agent, which has the socket, to
  start the same hardened container for each check, after a probe that the
  image is there (the agent builds it on demand). A project's `db:drift`,
  `db:seed` and `test:e2e` run the same way against the bundle's own
  throwaway `checks-postgres` and `checks-redis`, on an internal network the
  check container shares with those two services and nothing else -- not
  the agent, which sets a run up with `docker exec` into them and hands
  the checks a plain role of their own. See `SECURITY.md`.
* **The daily jobs.** Memory consolidation and the codebase map are scheduled
  by the agent itself, once a day at the first quiet moment, with a Run now
  button on the memory panel. There is no cron in the bundle and none is
  needed.
* **The logo tools.** `logo_render`, `logo_export_brand_kit` and the rest call
  LogoLoom's Node modules, and the agent image is Python-only — no Node, and
  60MB of image libraries for a feature most installs never touch. The agent
  detects their absence and simply has no logo tools, which is the intended
  outcome rather than a failure. The host install picks them up from
  `services/logoloom` (see `install.sh`).

## Data

Ten named volumes. `docker compose down` keeps them; `down -v` destroys them.

| Volume | Holds | Back it up? |
|---|---|---|
| `pgdata` | tasks, memory, users, sessions | **yes** — `scripts/backup.sh --bundle` |
| `routerconfig` | the model pins the Models page writes | **yes** — it is the only copy |
| `agentdata` | `AUTH_SECRET_KEY`, the encrypted first password, the daily jobs' markers | **yes** — the database is unreadable without the key |
| `pgsecret` | the generated database password | **yes** — lost with it, the agent cannot open a surviving `pgdata` |
| `routerkey` | the generated router key | yes, or regenerate: delete it and restart |
| `reviewshared` | `projects.json` and the review-control secret, for the review services | `projects.json` is worth a copy |
| `reviewstate` | verdicts and their history | optional |
| `bundlesecrets` | where the two secrets lived before 2026-09-29; postgres reads it once on upgrade | no |
| `agentlogs`, `routerlogs` | the agent's logs and the router's per-call ledger | no |

The projects themselves are in `PROJECTS_DIR` on the host, not in a volume.
Restore is in `docs/backup.md`.

## Model pins

The router's live config is `config.yaml` in the `routerconfig` volume: the
Models page writes pins there and the router re-reads it on its next call.
On the **first boot only**, the volume is empty and the router seeds it from
`ROUTER_CONFIG` in `.env` when set, else the committed
`services/model-router/config.example.yaml` (a working default, not a
stub). After that the volume's file is yours: a restart, an upgrade or a
changed `ROUTER_CONFIG` never overwrites it, so pins set from the dashboard
survive. To reseed from a file, empty the volume first:

```bash
docker compose down router
docker volume rm tektonix_routerconfig
docker compose up -d
```
