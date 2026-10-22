# Security

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue.

Use GitHub's private reporting: **Security → Report a vulnerability** on
[github.com/DJG3DK/persei](https://github.com/DJG3DK/persei/security/advisories/new).
That opens a channel only the maintainer can see.

Include what you'd want to receive: what you did, what happened, what you
expected, and the smallest reproduction you have. If it involves the agent
executing something it shouldn't, the exact prompt or task text matters —
that *is* the payload.

This is a single-maintainer project, so expect a first response in days, not
hours. You'll get an acknowledgement, a fix or an explanation of why it isn't
one, and credit in the release notes unless you'd rather not be named.

## What this project is, threat-model-wise

Persei runs an LLM that writes and executes code against your repositories.
That is the product, not a bug — so it's worth being precise about which
capabilities are intended and which would be real vulnerabilities.

**Working as designed** (please don't report these as vulnerabilities):

- The agent runs arbitrary shell commands. It does so inside a Docker
  container with only that project's worktree mounted, `--cap-drop ALL` and
  `--security-opt no-new-privileges` — but it is genuinely running model-chosen
  commands, on purpose.
- The agent can modify any file in a project you onboarded. That's the job.
- An admin can point onboarding at any directory inside `AGENT_PROJECT_ROOTS`.
  Narrowing that root is the operator's control.
- An admin can create a directory under `AGENT_PROJECT_ROOTS` from the
  dashboard, and a private GitHub repository with it, using a token the
  same admin stored. The name is validated as a single directory name, a
  path that already exists or resolves outside the roots is refused, and
  the request is audited.
- An admin can remove a project from the dashboard. That deletes the agent's
  workspace, its deploy key and — if they choose delete rather than archive —
  everything the agent learned about it. It does **not** touch the live
  repository, and that boundary is what `tests/test_project_removal.py`
  exists to hold. The action is audited and refused while work is in flight.
- Web push sends alert text through a push service run by Google, Mozilla or
  Apple. The payload is end-to-end encrypted to the browser's own key, so the
  service cannot read it, but it does learn that a message was sent, when, and
  roughly how big — so push bodies carry the same short summaries the Telegram
  alerts do and never a diff. The signing key (`keys/vapid.json`) never leaves
  the box; only its public half goes to the browser.
- Model output is not trusted-but-verified so much as *gated*: an independent
  review service and (optionally) a human approve every merge.
- The two review services listen on loopback and are reached only through
  the agent's own `/_review/` proxy, which requires an admin session and
  injects the control secret itself. No reverse proxy may add that header.

**Genuine vulnerabilities** — please do report:

- Escaping the sandbox container, or reaching host paths outside the mounted
  worktree from an agent tool call.
- Reading or exfiltrating a secret the agent shouldn't see: `.env` contents,
  another project's credentials, a deploy key, `AUTH_SECRET_KEY`, session
  tokens.
- Authentication or authorization bypass: acting as another user, reaching an
  admin endpoint without an admin session, accessing a repo outside your
  `allowed_repos`, defeating TOTP.
- Onboarding a path outside `AGENT_PROJECT_ROOTS`, or getting a command into
  a project's check/build config that the server didn't itself propose.
- Prompt injection that escalates *privilege* — content in a repo, a web page
  or a task description that causes the agent to take an action the operator's
  settings should have prevented (bypassing the approval gate, pushing without
  review, disabling a control).

  `tests/test_prompt_injection.py` is the fixture for this claim: a repo file
  that tells the agent, in as many words, to disable merge review, read the
  deploy key and force-push. It pins the containments that make the text
  inert — the tool surface has nothing that reaches a control, the endpoints
  refuse an unauthenticated call, the container mounts only the workspace and
  carries no credential, and the approval gate is decided from the account's
  own setting before any file is read. It is a regression test, not a proof:
  injection is contained here by what the text cannot reach, never by the
  model declining to follow it.
- Anything that lets an unauthenticated request reach a code-execution path.

## Where agent-authored code runs

The agent's shell is sandboxed. Its `write`/`edit` tools are not — they put
files directly in the project's worktree, which is the job. That means a file
the agent wrote is code, and the question that matters is *where* anything
runs it.

**The reviewer runs it.** `commit-reviewer` executes each project's configured
checks — `npm test`, a lint, a build — against that worktree. On a host
install this service runs as root, so until 2026-09-21 a test file the agent
wrote was arbitrary code executing as root outside any container: a complete
path from "model in a container" to "root on the machine". `sealedEnv()`
already stopped secrets reaching those commands; it did nothing about what
they could do once running.

**What it does now**, decided per deployment because the right answer differs:

| where | what runs checks | why |
|---|---|---|
| host install | the sandbox container | the reviewer is root on the real machine |
| compose bundle | the same sandbox container, started by `agent` on the reviewer's behalf | the reviewer's container holds the merge secret; only `agent` has the socket |
| neither available | **nothing — the review refuses** | see below |

Until 2026-09-26 the bundle ran checks **in the reviewer's own process**, on
the theory that its container was containment enough. It was not: that
container reads `review_control_secret` from the shared data volume and can
reach `agent-review`'s merge endpoint, so a test file the agent wrote could
read the secret and POST `{"force": true}` — merging its own branch past the
gate. Now the reviewer sends a structured request to
`POST /api/internal/review-sandbox/run` on the agent (authenticated with the
same secret), and `agent/review_sandbox.py` starts the container. The agent
does not take the request's word for what the container can see: the worktree
must be this project's review worktree under `REVIEW_WORKTREE_ROOT`, every
extra mount must come from the live checkout and is forced read-only, the
image comes from server-owned config, and the hardening flags are fixed.
Before each review the reviewer asks the agent's probe whether docker and
the image are there, and a missing image starts a build on the agent's side.
Without `AGENT_SANDBOX_URL` the bundle's reviewer refuses, like any other
deployment that cannot contain the code. The database checks below run in
the bundle too, against throwaway services of their own.

Handing the bundle's reviewer the Docker socket instead would give that
container host-root equivalent, which is why the agent — already holding the
socket for its own sandboxes — starts the container rather than the reviewer.

**It fails closed.** If Docker or the sandbox image is missing on a host
install, the check does not run on the host instead — it returns a refusal
saying so. "Fall back when the sandbox is unavailable" is the path an attacker
would engineer, and it is also the change someone makes later to stop a red
build. This costs nothing in practice: the agent's own shell already requires
Docker, so a box without it is not producing work to review.

**Containment mirrors the agent's own**, deliberately, rather than being a
second policy: same image, `--cap-drop ALL`, `--security-opt
no-new-privileges`, memory/CPU/pid limits, `--network none`, argv rather than
a shell. Two hardening policies drift, and the weaker one is the one that
decides.

### The network opt-in, and what you are agreeing to

Checks run with no network, because dependencies are installed by the time
they run. One real check needs egress anyway — `pnpm audit` queries an
advisory database — so a check may set `network: "bridge"` in `projects.json`.

That file lives outside the worktree, so **the agent cannot grant its own code
network access**. What an operator is agreeing to, per check, is that this one
command runs agent-authored code *with* egress. Grant it to a package
manager's own audit path; think harder before granting it to a test suite.

The dependency installs (`composer install --no-scripts --no-plugins`, `mix
deps.get`) run with network for the same reason — fetching is the point. They
were previously argued safe as "the non-executing kind", which does less work
than it looks: `mix deps.get` evaluates `mix.exs`, and `mix.exs` is Elixir the
agent could have written. They are contained now too.

### A project that is not Node or Python

The agent's sandbox image carries Node and Python. `agent/provisioning.py`
detects and configures checks for Go, Rust, Ruby, Elixir, Java, PHP and .NET
as well, so containing checks in that one image would have turned every
review red for anyone whose project is not JavaScript.

So the image is chosen per CHECK, from `docker/stack-images.json` -- the same
list `scripts/verify_stack_checks.py` already proves in CI. Per check rather
than per project, because a Go backend with a React frontend is an ordinary
repository and its two sets of checks belong in two different images.
Detection stamps the stack when a project is onboarded, which is the only
moment it is known: by the time `projects.json` is written, `go vet` is just
a command.

A stack with no entry falls back to the default image rather than failing --
the map can gain entries after a config was written, and refusing a check
because its label is new is worse than running it where it probably works.

`node scripts/check_sandbox_tools.js` probes every configured check's command
against its own image and says which are missing, which images are not pulled
yet, and who needs them. It runs inside `scripts/doctor.py`, so an existing
install finds out before its next review rather than during it. A missing
toolchain is reported as a **setup** error naming the tool, never as a failing
check -- an environment error read as a code failure gets handed back to the
agent, which then tries to debug an environment it cannot see, and a correct
commit is rejected round after round.

### The database checks: contained in the bundle, on the host still not

Three commands run outside the sandbox on a host install: a project's
`db:drift`, `db:seed` and `test:e2e`. They are agent-authored code, and they
are the exception to everything above. **In the compose bundle they are
contained** (since 2026-09-27): the bundle carries a throwaway
`checks-postgres` and `checks-redis` on an internal network with no gateway,
and the three commands run in the same hardened container as every other
check, joined to that network alone. The only other members of that network
are the two throwaway services: the agent is not on it. It sets each run up
with `docker exec` into the two containers -- a plain role owning a database
created from `template0`, and one of the Redis databases, flushed -- and
the checks get that role's DSN and secrets built for the run. The server's
superuser password is replaced at start with one nobody holds
(`docker/checks-postgres/init.sh`), so a check that reads the bootstrap
value in the compose file gets nothing with it. Runs in flight each have
their own database on both servers. The database and the role are dropped
whatever happened. A bundle without those services gets the refusal it
always did.

They talk to Postgres and Redis on this machine's loopback. Inside a
container `localhost` is the container, so containing them means one of two
things: `--network host`, which is not containment; or rewriting each
project's DSN to the bridge gateway and exposing those services to it. Both
trade a working review for a boundary weaker than the one they would buy.

What bounds them instead is real but partial. The commands come from
`projects.json`, which lives outside the worktree and the agent cannot write.
The database is a throwaway, created before and dropped after. The
environment is constructed for the run rather than inherited: `PATH`, `HOME`,
`LANG`, `CI` and `DEBIAN_FRONTEND`, plus the throwaway `DATABASE_URL`, a
`REDIS_URL` on a scratch database, and freshly generated JWT and encryption
secrets — nothing of the reviewer's own, so its router key and control secret
are not in it. (Until 2026-09-23 this sentence was true of the checks and
false of these three commands, which inherited the reviewer's full
environment; `tests/test_db_check_env.py` now pins it.) What is **not** bounded is the code
those commands execute — that is the repository under review, and it runs as
root on the host.

Everything else that was in this position has moved: the checks, the build,
the package-manager installs, schema generation, and the build assertions all
go through the sandbox now. This is the residue, and it is written down
rather than left to be discovered.

Closing it on a host install means the same thing the bundle now does: a
throwaway Postgres and Redis the check container can reach by name on a
private network. The bundle has them; a host install still points at the
machine's own loopback services and so still runs these three on the host.

### What this does not fix

- **The Docker socket in the bundle.** `agent` is given
  `/var/run/docker.sock` so it can start sibling sandboxes. Anything that can
  reach that socket can ask for a privileged container, so this is host-root
  equivalent for that container. `sandbox.py`'s mount allow-list is a guard in
  the *client*; a socket proxy enforcing it server-side is the fix, and is not
  built.
- **Egress from the sandbox itself.** The agent's own shell runs on the
  default bridge. SSRF is guarded host-side (`agent/tools/url_guard.py`), not
  at the network layer.
- **A project with no checks** is reviewed without running anything, so none
  of this applies to it.

## The review services

The review dashboard (`:4100`) and the commit reviewer's control port
(`:4101`) hold the only write path into a live repository, and publish no
port. On a host install they bind loopback; in the container bundle they bind
the compose network, because the agent is a different container. That network
also reaches the agent's sandboxes, which have ordinary network access — so
binding is not the boundary there. The shared secret is: every route on both
services except `/health` requires `X-Review-Secret`, reads included (until
2026-09-23 the dashboard's reads — every project's diff among them — were
open). `/health` stays unauthenticated for monitoring and names nothing: the
reviewer reports how many projects are under review, and names them only to a
caller holding the secret. `tests/test_review_services_gated.py` starts both
and asks from outside.

## Sessions, links and second factors

### Approve links

A GitHub inbox alert on Telegram or email carries a link that can start a task
with nobody signed in — the person is on a phone with no session. The token in
it is the credential: HMAC-signed, bound to one inbox item, valid for 48 hours,
and single-use. Opening it (`GET`) only shows what would happen and a button;
messengers fetch links for previews, so a `GET` never acts. The button posts
the token in the form body, and that `POST` is refused cross-site (the same
`Sec-Fetch-Site` rule as the review dashboard) and rate-limited per address.
Both pages are `no-store`, and every response is `Referrer-Policy: no-referrer`.

What stays is that the token is in the link's URL, because a link is what an
alert can carry. Anywhere the URL is recorded before it is used — a proxy log,
a chat backup, a screenshot — someone holding it can start that one task. The
task still goes through the review gate and, with final merge review on, stops
for a person before anything merges; the budget is the project's inbox budget.
Treat an alert link like the alert: it is for the person it was sent to.

### Signing in

- **Two-factor is required for admins and optional for everyone else.** An
  admin cannot reach the API past a forced setup screen until it is on, and
  cannot turn it off. A restricted user may turn it on in Settings → Account;
  if they do not, their password alone opens every repository they can see.
  That is a reasonable default for a household box and a weak one the moment
  a second account belongs to someone outside it.
- **The desktop app is the one exception, and only while its premise holds.**
  The app writes `TEKTONIX_DESKTOP=1` into its own `.env`; with it, the
  admin's second factor is optional and a session lasts ninety days, on the
  premise that the machine's own login is the boundary. The agent checks
  that premise rather than trusting the flag (`agent/auth.py`,
  `desktop_install`): the dashboard must be bound to loopback
  (`BIND_ADDRESS`), and no account beyond the first may be licensed
  (`TEKTONIX_FEATURES=multi-user`). Change either and the full sign-in is
  back for everyone.
- **A passkey replaces password and code, admins included.** A passkey
  sign-in requires the device's own unlock, so it stands in for both factors.
  It answers only on the console's own address: `WEBAUTHN_ORIGINS` when set,
  otherwise the host the passkey was made on (and on `localhost`, the port
  the console answers on). Naming a parent domain in `WEBAUTHN_RP_ID`
  requires `WEBAUTHN_ORIGINS` too; without it, a page on a sibling subdomain
  could use the same passkeys. A password change or reset removes every
  passkey on the account.
- **CSRF protection is the session cookie's `SameSite=strict`.** There are no
  CSRF tokens, because a strict cookie is never sent on a request another site
  starts. Loosening it — for an embed, an OAuth return, a subdomain — makes
  tokens or a `Sec-Fetch-Site` check on every mutating route required in the
  same change.
- **Login rate limits are counted per account.** Five misses a minute on one
  account from one address locks that account's sign-in from there for five
  minutes; a success clears only its own account's misses. One address also
  has a looser limit across every account.
- **Login rate limits live in the process's memory.** They reset on restart,
  and would multiply by the worker count if the API ran more than one
  (`agent/rate_limit.py` says what raising that requires). If the limiter
  itself fails, it lets the request through and logs that it did.

## Deploying this safely

- **Never expose the dashboard directly to the internet.** It is an operator
  console. Put it behind a VPN, an SSH tunnel, or a reverse proxy with its own
  authentication — [INSTALL.md §3a](INSTALL.md#3a-reaching-it-from-another-machine)
  covers all three. The app has its own login and TOTP, but it was designed to
  sit behind something, not to be the perimeter.
- **Keep `AGENT_PROJECT_ROOTS` narrow.** Onboarding grants an agent write
  access to whatever it points at.
- **Treat the review gate as load-bearing.** `Final merge review` and the
  independent reviewer exist because a model can be confidently wrong; turning
  both off means unattended merges to your live repos.
- **Deploy keys, not account keys.** Each project gets an SSH key scoped to one
  repository (Settings → Projects → Push access) rather than a credential that
  can reach everything you own. A repository created from the dashboard is
  handled the same way: the GitHub token is used for two API calls — create
  the private repository, register the project's freshly minted deploy key on
  it — and the first push of `main` goes over that key from the API process
  on the host, on an admin's request. No agent ever holds the token or the
  key, and `git push` stays on the agent's blocked-command list.
- **Budget ceilings are a safety control too.** `DEFAULT_BUDGET_USD` bounds a
  runaway loop's cost.
- **On Linux, the docker group is root.** The desktop app adds your account
  to it when Docker refuses you, and every program you run can then start a
  container that mounts `/` and edit anything on the machine. The panel says
  so before it asks. If that is too much, use
  [rootless Docker](https://docs.docker.com/engine/security/rootless/) and
  leave the group alone.

## Supported versions

Pre-1.0. Fixes land on `main`; there are no backported release branches yet.
Run the latest commit.
