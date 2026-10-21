# Changelog

## v0.9.4 — Tektonix is now Persei

Same agent, new name. Tektonix was one letter away from a company that got
there first, so it's Persei now, after the stars of Perseus.

- Find it at [perseiagent.ai](https://perseiagent.ai) and the code at
  [DJG3DK/persei](https://github.com/DJG3DK/persei). The old addresses
  redirect.
- A new logo: the Perseus constellation with a gold diamond P in it.
- Updating from Tektonix replaces it. On Windows the old install is removed
  first, on Linux the .deb and the AppImage take its place. Your settings,
  projects and history stay where they were.
- Passkeys belong to the address they were made on. Sign in with your
  password on the new one and add a passkey again.

Also in this release:

- After a failed attempt the agent stops and writes down what went wrong
  and what to try next, before it tries again. Fewer rounds of the same fix.
- A test that was already failing before the agent touched anything no
  longer blocks its work. The reviewer says what's wrong with it, and the
  agent fixes it when the fix is small.
- A GitHub alert whose task finished but didn't actually fix it comes back
  to the inbox, ready for another go.
- The task list loads quickly again on a slow connection. It had grown to
  almost a megabyte per refresh; it's a few dozen kilobytes now.

## v0.9.3 — a clean release

A small one. Nothing changes in the apps.

- The release's last check, the one that verifies the Windows installer's
  signature, couldn't read the newer timestamps Microsoft now issues and
  failed v0.9.2 even though its installer was signed correctly. It checks
  them with OpenSSL now.
- `/api/auth/admin-check` answers yes only for a signed-in admin, so your own
  pages can sit behind the dashboard's sign-in through nginx.

## v0.9.2 — Tektonix on Linux

The desktop app runs on Linux now, and a few things got easier everywhere.

### The Linux app

- One line installs it: `curl -fsSL https://tektonix.io/install-linux.sh | sh`.
  It checks the download against GitHub's checksum, adds Tektonix to your app
  menu and starts it. There's also an AppImage and a .deb on the release.
- No Docker? It installs it for you, Arch-based distros included. You type
  your password once and carry on. No logging out.
- It runs as you, so your projects folder doesn't fill up with root-owned
  files.
- The AppImage updates itself like the Windows app. The .deb updates when you
  install the next one.

### Everywhere

- Sign in with a passkey. Your phone, laptop or security key is enough on its
  own, and your password and code still work as a backup.
- Quit actually quits. It stops the containers so they're not sitting on your
  memory, and asks first if a task is running.
- New installs size their sandboxes to your machine instead of 2 CPUs and 2 GB.
- Dropdowns look right on every platform.

### Fixes

- Web search in planning works again, and the agent stops guessing at web
  addresses when a page won't load.
- The GitHub inbox stopped offering Tektonix its own pull requests, and its
  tools understand repository links.
- The Windows installer looks like Tektonix instead of a generic setup wizard.

### Upgrading

The apps update themselves. Host installs: pull, run
`pip install -r requirements.txt` (passkeys need two new packages), rebuild
the dashboard and restart the agent.

## v0.9.1 — the Windows installer is signed

### Windows names the publisher

The installer, the app inside it and its uninstaller are signed with an
Authenticode certificate issued to the author through Azure Artifact
Signing, and timestamped, so Windows shows a verified publisher instead of
"Unknown publisher". SmartScreen may still ask once or twice while the
certificate builds reputation. The signing runs inside the build, before the
updater signature is taken, and a release whose installer comes out
unsigned fails instead of publishing.

## v0.9.0 — a Windows app that updates itself, a review gate you can trust, and a release you approve

The first full release since v0.8.0. Three things changed shape.

**The desktop app.** Tektonix installs on Windows as one app with its own
window and a desktop sign-in. It runs the release it came from, proven by
image id; it updates itself and the stack when the agent is idle, never
while a task runs and never by taking the window from you; and it pulls
each image by a digest the release signed. rc1 to rc12 testers reinstall
once by hand, because those builds ordered release candidates as text.

**The review and merge path.** The gate merges the exact commit that was
reviewed; a review that checked nothing, or whose only check was already
red on main, is escalated instead of passed; a harness failure is asked
again instead of handed to the coder; a long suite keeps the connection
alive; a task queued behind other reviews keeps waiting; an approval
carries over a rebase that changed nothing but the parent; and main moves
only on a green GitHub Actions run. Tasks that need you sit in their own
sidebar group until they are done.

**Releases and the box.** A release builds only from a version tag on main
with green CI, pushes its images once by digest, and waits at the
installer for your approval on the `release` environment. The 2026-09-29
audit's 124 findings are resolved on main (docs/review-2026-09-29.md), the
code scanning backlog is at zero, and the LangChain family is current.

### Upgrading from v0.8.0

- **Host installs:** pull, then `pip install -r requirements.txt` and
  `.venv/bin/playwright install chromium` (the Playwright upgrade wants a
  newer browser; web search in planning fails with "Executable doesn't
  exist" until it is installed), rebuild the dashboard, and restart the
  agent, both review services and the router.
- **If your nginx vhost has a `location /_review/` block,** delete it,
  reload nginx and rotate `REVIEW_CONTROL_SECRET` in both `.env` and
  `services/shared/.env`. Restart with the new value exported
  (`set -a; . ./.env; set +a; pm2 restart … --update-env`): pm2 keeps a
  process's old environment otherwise. `scripts/doctor.py` checks both.
- **Bundle:** `docker compose pull && docker compose up -d`. Secrets move to
  one volume each on first start and are carried over from the old shared
  volume; `init-secrets.sh --rotate` rotates the database password.
- **Forks that publish releases:** protect a `release` environment with a
  required reviewer and a `v*` tag policy, and add the updater signing key
  as `TAURI_SIGNING_PRIVATE_KEY`.

### A failed chain tells the caller the status, not the exception text

When every deployment in a chain failed, the router's 502 carried the last
exception's text to the caller. It names the upstream status and points at
the router ledger now; the full error stays there under the call id. This
closed the last open code scanning alert.

### One task per package, and an approval that survives a rebase

The GitHub inbox made one task per Dependabot alert, so eleven undici
advisories on one lockfile became eleven tasks; one merge closed all
eleven and the other ten rebased, re-reviewed and merged a change that
fixed nothing. The inbox now makes one task per package and manifest,
naming every alert and the version that clears them all, and a task whose
alerts another change has closed concludes with "already fixed on main"
instead of taking the merge slot.

An approval used to be asked again after every merge that landed ahead
of a task, because a rebase makes a new commit. When the rebase changed
nothing but the parent, the approval now carries over; the reviewer still
judges the new commit.

### A task queued at the reviewer keeps waiting

Approving several diffs of one project in a row put every task but the
first behind a rebase and a fresh review, and a request waiting behind
other reviews looked to the gate like one nobody would run, so it timed
out. The reviewer now writes its queue to its state, and the gate waits
for a branch it finds there the way it waits for a review in progress.

### The 2026-09-29 audit, resolved

One hundred and twenty-four findings from a full read of the repository
(docs/review-2026-09-29.md, section 13 has the row for each). The ones
an operator will notice:

- **Release pipeline.** Every action is pinned to a commit; a tag builds
  only if it is a version, sits on main and CI passed on its commit;
  images are pushed once, by digest, without a moving `latest`, and the
  digest manifest is signed with the updater key so the desktop app pulls
  exactly what was released. The signing key is read behind a `release`
  environment. Operator: protect that environment with a required
  reviewer and add a tag ruleset for `v*`.
- **Desktop app.** The stack follows the app's own release and the app
  updates first; the automatic pass never restarts a busy agent, holds a
  lock against the panel, and pulls every image before retagging any.
  The console window keeps only the window controls and the panel
  switch; off-origin links open in the browser. Candidates rc1 to rc12
  reinstall by hand once.
- **Review gate.** The merge ships the reviewed commit itself; the
  reviewer never borrows the agent's workspace tools and installs its own
  in the sandbox, so a Windows install now runs an install per review; a
  committed symlink can no longer reach the live checkout or its secrets;
  each throwaway database is connectable by its owner alone; check output
  is fenced in the reviewer prompt; the only check red on main too is
  escalated instead of passed.
- **Bundle.** One volume per secret, an unprivileged router, PUID/PGID
  for the rest, a `backup.sh --bundle`, log caps on every service, and the
  checks network keeps its name under any compose project name.
- **Agent.** Auto mode asks about every delete it cannot read (wrappers,
  `git reset --hard`, a `cd` inside a subshell, `mv` onto a tracked
  file); a budget refusal survives an empty-reply retry; a stream the
  client abandons is still on the ledger; a used episode carries its real
  rank; the planning-cost carry and the live mirror take one lock; daily
  jobs take a file lock shared with the cron wrappers.
- **Dashboard.** Per-task state resets when you switch tasks, and a merge
  decision names the commit it saw; a planning session opened mid-turn
  connects at once; the desktop title bar sits above the sign-in; every
  admin sees the Users page; notifications open what they are about.

Left open, on the list in docs/todo.md: a warm install cache for
reviews of a foreign live install, the rebase recursion's log merge,
the planner's quiet retry seat, and the bundle's `keys/` directory not
being a volume.

### A stream the client abandons is still on the ledger

A task that hung up mid-stream, or a stall the watchdog cut, left no
ledger line for the tokens it had spent, and a stream whose every
deployment refused came back as an empty success. The line is written
whatever ends the stream, and a total refusal is the same 502 the
buffered path returns.

### A used episode carries its real rank

The recall log remembered only the first five results, so an episode
used from rank six or worse was recorded as never offered, the opposite
of what the rank is for. Every offered result is remembered per task,
a second search no longer erases the first, and "never offered" is its
own field.

### The merge lands the reviewed commit, not whatever the branch holds now

The merge route fast-forwarded to the branch name and pushed the branch,
so a commit that arrived after the verdict would have shipped under it.
It now merges and pushes the reviewed commit itself, and a branch that
moved past it is refused.

### The only check red on main too verifies nothing

A project with one configured check, red on the base commit as well, was
passed as pre-existing: a review that checked nothing. It is escalated
now, in its own words. A failure main already has still counts as
pre-existing while another check verified the commit.

### The database check streams too

The database drift and seed check answered in one piece, so a slow run
hit the reviewer's five-minute cut-off the way the ordinary checks used
to. It keeps the connection alive the same way now; a bad request is
still refused before the first byte.

### The budget ceiling survives an empty-reply retry

The retry of an empty, reasoning-exhausted reply swallowed every error,
including the budget guard's refusal, so a task past its ceiling ran on.
The refusal propagates.

### Recall records where the memory it used was ranked

A used episode carries the rank the search offered it at, and a section
read carries its position in the memory index. The log held offers and
uses as separate events nobody joined, so the question a re-ranker would
answer could not be asked. The vector leg of episode recall is switched on
on the reference deployment.

### Auto mode reads a delete through a variable

The coder kept its scratch path in a shell variable and deleted through
it, and auto mode asked for approval on every cleanup because it could
not read the target. A variable assigned a plain value earlier in the same
command is resolved now; one holding a substitution still asks.

### The LangChain family moves up

langgraph 1.2.12, langchain 1.4.3, langchain-core 1.6.6, langchain-openai
1.6.6, langchain-anthropic 1.7.4, langchain-google-genai 4.4.0 (with
google-genai 2.25.0), langsmith 0.14.1 and deepagents 0.7.19. Twelve
deepagents releases of fixes: the task tool rejects unknown arguments
instead of dropping them, subagent state reaches provided middleware,
compaction recovery is bounded, and clipped tool output says so. The
suite passes unchanged.

### A workspace-internal package resolves inside the check container

The reviewer linked a monorepo's internal package to the worktree by its
host path, which does not exist inside the check container, so every app
importing it failed typecheck with "cannot find module" in any review that
borrowed dependencies instead of installing them. The link is relative
now. A verdict the harness produced is retried on request at once and on
the poll after half an hour, not every tick.

### An approved merge reads the verdict it already has

The rule that makes a re-review wait for a verdict newer than the request
applied to every re-check, including the one after the operator's merge
approval; the reviewer does not review the same commit twice, so two
approved tasks waited out the timeout on a READY that was sitting there.
Only a verdict the harness produced has to be re-judged.

### Tasks that need you have their own group

An escalated task, or one waiting for your look at the diff, was filed
into its category the moment it stopped, collapsed out of sight beside
the finished ones. The sidebar now keeps them in a "Needs you" group under
Running until they are over; only a finished task goes into a category.
The task page reads the live task record, so a budget raised on resume
shows on the bar instead of the number from when the task was opened.

### A review that checked nothing no longer passes

The reviewer links a project's generated code (a Prisma client) to the
live checkout's copy, and the check container never saw that copy, so
every TypeScript check failed with "cannot find module" on the branch and
on main alike; the reviewer called that pre-existing and the review model
wrote it off as unrelated. Found on 2026-09-29 on a project reviewed that
way since 2026-09-23. The generated directory is now mounted into the
check container like node_modules; a baseline on which every compared
check fails is treated as a review environment that cannot run the
project, not as a red project; a harness escalation does not carry into
the next real review; and the gate trusts the reviewer's current record
over the task's copy, so clearing a branch's record does force a review,
and a verdict that differs from the task's copy is taken from the reviewer.

### The review runs in the bundle the way it runs on a host

(rc19: the dependency helper was used in one file without being imported,
and every review of a project with configured node_modules directories
failed with a ReferenceError; a harness escalation now says what to do.)

An audit of the whole path from a passing check to a merged pull request,
as it runs under Docker Desktop, found where each step could fail
silently. In the bundle the reviewer now: borrows the agent's own Linux
node_modules when the operator's checkout has none or a Windows one; never
tries to bind-mount inside its own container, which it cannot; records a
setup failure as a verdict the harness produced instead of leaving the
agent to wait out its timeout; listens on its control port before its
first poll, so a trigger right after a restart is not refused; forgets a
review a crash left in progress; gives a dependency install fifteen
minutes; and logs each check's result with its duration and a line a
minute while one runs. A check that times out is a harness failure, not
a failing check to be re-run on the base commit and waved through. A
re-review waits for a verdict newer than the request. A harness failure
does not count toward the run of failures that escalates a branch. The
final commit's git commands get three minutes on a slow bind mount, a
fresh project's lint and typecheck checks get ten in review, the task
branch is pushed forced before a pull request, and a failed pull-request
call is reported as one.

### A verdict the harness produced is asked about again

When the reviewer's own sandbox call failed, its verdict said "could not
RUN test", and the gate then refused to ask again because the commit had
not changed, telling the coder to fix code that was never judged. Both
sides now treat a verdict blocked only by the harness as no verdict: the
gate asks once more, and the reviewer reviews the commit again.

The agent keeps waiting for a verdict while the reviewer reports it is
still on that commit, up to four times the review wait, whose default is
now thirty minutes: a desktop sandbox runs a long suite past the old wait.

### A long suite fits in the sandbox

A five-minute suite here took over ten in a two-core sandbox under Docker
Desktop, past the shell's ten-minute ceiling. The sandbox's cores and
memory are now set in .env (SANDBOX_CPUS, SANDBOX_MEMORY); the desktop app
writes half the machine's cores and four gigabytes once. The shell's
ceiling follows the suite timeout, whose default is fifteen minutes, and
the coder is told to run the whole suite through run_checks.

Dependency updates arrive weekly as pull requests, the LangChain family
grouped as one.

### An update never takes the window from you

The app installed its own update while a task was running and the
operator was typing to it, and came back on the panel page. It now
replaces itself only when the agent is idle and nobody is at the window,
or from the panel's button; a restart returns to the page it showed; and
a second launch brings the running window forward as it is.

The Updates section always shows both versions and both buttons, the
pre-release switch sits next to Check for updates, a release-candidate app
counts release candidates as releases whether or not that switch is on,
and a stack whose release cannot be verified is offered the app's own.
The newest release is chosen by version: GitHub's listing led with rc9
above rc14, and the app announced rc9 as the release that was out.

### A long check no longer looks like a silent agent

In the bundle the reviewer's checks run through the agent, and the
reviewer's fetch gave up on any answer whose headers took over five
minutes: a project's test suite did, and every review of it said "the
agent's sandbox endpoint did not answer". The agent now answers at once
and keeps the connection alive until the check ends. A failed fetch is
reported with its cause, and the gate's summary says why a check could
not run instead of guessing that the command was missing.

### The app runs the release it came from

A release-candidate app asked for images of its plain version, no such
release existed, and the pull quietly fell back to the previous stable
release: every candidate so far ran the v0.8.0 stack. The app now carries
its release tag, pulls those images whenever the stack is behind it, and a
missing image is an error rather than a swap. The stack record is proven
by the image it names; an early candidate's record, which named a tag it
never pulled, is not trusted. A running task is not interrupted for this:
the automatic pass moves the stack once the agent is idle.

An update is a newer release. With pre-releases off, the stable release
older than the installed candidate was offered as an update.

The installer is named after the tag (`Tektonix_0.9.0-rc13_x64-setup.exe`);
every candidate used to download as 0.9.0.

### The final commit has a one-line subject

A pasted plan became the commit message verbatim, heading and all. The
subject is now the goal's first line, cleaned and cut at a word; a long
goal contributes only its first paragraph.

### The record says what git saw

When the ship gate finds no diff, the task log now carries the branch,
HEAD, the base, what `git status` sees and what is stashed, so a "no diff"
on a workspace that plainly has edits is chased from that line. The
adoption of commits already on the branch uses the project's base branch.

### The window always has a title strip

A frameless window is usable only through a strip drawn by the page it
shows, and an older console drew none, so the window could be neither
moved nor closed. The app now draws a fallback strip on any page that has
not drawn one.

### The throwaway Redis reports healthy

It had no health probe, so the container table said "running" where every
other row said "healthy". It answers `redis-cli ping` now, and the agent
waits for that rather than for the container merely to have started.

### One window, and a desktop sign-in

The app has one window: the console and the control panel take turns in
it, from the panel's Open console, the tray, and a Control panel link on
the console's title strip. Two windows meant two taskbar buttons and one
that did nothing.

On a desktop install the setup form asks for your password, set through
the agent's own sign-in and change-password routes at the first start
instead of a one-time password read out of a container. The first
account's second factor is optional there, and a session lasts ninety
days so the app stays signed in between launches. All of it is switched
on by one line the desktop app writes into its own .env; a host install
and the plain compose bundle keep the full sign-in unchanged, and a test
runs the rules both ways.

### The bundle's commits carry an identity, for real this time

The agent runs git through a shell helper whose minimal environment
drops everything but a few names, so agent-authored commands cannot see
secrets. That scrub also dropped the git identity the compose file sets;
a host install never noticed because the home directory's git config
filled the gap. The four identity variables pass the scrub now, and a
test commits with no git config anywhere to prove it.

### One running copy of the app

A second launch, from the installer's "run when finished" plus the Start
menu or while the first copy sat in the tray, started another app with a
dead taskbar button of its own. It now brings the running panel forward.

### The app's windows are frameless

No native title bars: the control panel and the dashboard window each draw
a slim strip of their own, a drag handle with the three window controls.
The dashboard's strip appears only inside the app; in a browser tab there
is nothing to draw.

### The app updates itself, and the stack, on its own

Two preferences on the app's settings form: update automatically, on by
default, and include pre-releases, off by default. With the first on, the
app checks two minutes after start and every six hours; when a newer
release is out and the agent is idle it updates the stack, then installs
the newer app and restarts into it. The buttons stay for doing it by hand.
The agent's public health route now says how much is in flight, which is
what "idle" reads.

### A failed retry never ends a task

The empty-reply retry re-asks the same model with reasoning switched off.
A model whose endpoint refuses that answered every retry with an error,
and six in a row ended the task. A retry that errors is dropped now: the
next option is tried, then the original reply goes through to the usual
nudge, and a seat that cannot run without reasoning is not asked again.

### Adding a project says where the app runs, not how git moves

"Add · pull requests" and "Add · merge" named the mechanism. They are now
"runs elsewhere" and "runs here", with a line above them saying what each
does: work goes up as pull requests for you to merge, or merges, pushes and
restarts the app on this machine after review.

### Planning spend is part of the task's cost

A planning session banked its turns and Build Now handed the plan over as
a goal; the task then started from $0.00 and the planning spend was
nowhere. The session's spend is now carried onto the task it starts, once,
and the task's cost on the list, its page and Analytics is the whole cost:
the build plus the planning. The budget bar still measures the build. A
plan that never became a task shows on Analytics as its own category.
Reviewer cost stays separate, as before.

### A project's path can change after onboarding

The desktop app's projects folder moved and the only way to follow it was
to remove the project and add it again. Each project row on the Projects
page has a Change path box: the new path is held to onboarding's rules,
must be a checkout of the same repository, and nothing on disk moves.

### A poller deploys a project whose main is merged elsewhere

The agent deploys only its own merges. A pull request merged on GitHub, or
a task shipped from another machine, left the production checkout behind.
`scripts/deploy-poll.sh` pulls main when it moves, installs dependencies
when the package files changed, builds, and restarts the named pm2
processes, one cron line per project. Files the running app rewrites are
named and never reset; a commit that also changes one stops the deploy
rather than guessing. `docs/runbooks/deploy-poll.md`.

### The GitHub token test says which project it cannot reach, and why

"Reaches none of the configured projects" came with nothing to check. The
probe now lists each unreached project with GitHub's answer, and names the
usual cause: a repository owned by an organisation and a fine-grained token
whose resource owner is the person, not the organisation.

### Main moves only on a green GitHub Actions run

After the review gate and the operator's approval, a project that ships by
pushing no longer fast-forwards its base branch at once. It pushes the task
branch, opens a pull request against the base (which starts `on:
pull_request` workflows), and waits for every Actions run on that exact
commit. Green merges and pushes, and GitHub closes the pull request as
merged. Red sends the failing jobs and steps back to the agent, and the fix
needs its own approval. A wait that times out (`ci_wait_timeout_s`, one hour
by default) or a token that cannot read Actions escalates unmerged, and a
resume waits again. Skipped, with a log line, when the commit has no
workflows, the origin is not GitHub, the project has no token, or the
project sets `"ci_gate": false` in projects.json. A merge whose push to
GitHub failed now says so on its summary line instead of "merged and
deployed".

### Staged work is a diff

A final commit that failed after staging left the change in the index,
where the gate's plain `git diff` read empty and ended the task as "no
changes needed". The gate measures against HEAD.

### The agent's commits are yours

`GIT_USER_NAME` and `GIT_USER_EMAIL` name the author and committer of
every commit the agent makes. The app's settings form and the Windows
installer ask for them, prefilled from the machine's git config; the
Environment page shows them; the bundle and a host install both apply
them. Blank keeps "Tektonix" and the sign-in address.

### A committed branch with a clean tree is sent to review, not called "no changes"

The ship gate read the working tree only. When the work was already
committed on the task branch with no record of it in the gate (a final
commit that failed and then landed on a resume, or a coder that committed
itself), it saw an empty diff and nudged toward "no changes needed" with
1,446 lines sitting on the branch. Commits main does not have are the
change: the gate adopts them as the pending commit and reviews them.

### `docker compose up -d` is the whole upgrade

Every built service has `pull_policy: build`: a plain `up -d` builds what
changed and never tries to pull a local image name from Docker Hub, which
printed "pull access denied" on the first Windows install. The desktop
app's `--no-build` start is unaffected.

### The bundle's containers can commit

The first Windows install's first task did everything and then failed its
final commit with "Please tell me who you are": a container has no git
identity, where a host install has the operator's global config. The
agent, both reviewers and every sandbox container now carry one, with the
operator's address from `ADMIN_EMAIL`.

### A Windows app, and released images to feed it

`app/` is a desktop app: the dashboard in a window, the compose stack under
it, a tray icon to reach both. It installs WSL 2 and Docker Desktop when
they are missing, asks for the two settings in a form, pulls the release's
images from GHCR with Docker's progress in the window, shows the first
password, and follows any service's log. One button updates the stack when
a new release is out, and the app updates itself through a signed updater.
The release workflow publishes the four images to
`ghcr.io/djg3dk/tektonix-<name>` on every version tag and builds the
Windows installer. The compose file names every built image, so a pulled
release and a source build land in the same place.

### An empty reply is retried without reasoning first

A reply that is all chain of thought and no answer was retried once on
the fallback seat. On the first Windows install one prompt emptied the
test writer on its own model and on the fallback too, thirty times, and
the task sat there. The blowout is pure reasoning, so the first retry now
re-asks the same model with reasoning switched off, and the fallback seat
is the second try. Every seat gets this.

### The example router config pins the proven models

Seven roles in the committed example still pointed at models the reference
deployment moved away from months ago. The first Windows install's test
writer spent its whole reply budget reasoning and returned nothing, thirty
retries in a row. The example now pins what the reference deployment runs:
the coder on DeepSeek V4.1 Flash, the planner, investigator, test writer,
verifier and both planning-chat seats on GLM 5.3 Flash. An existing install
keeps its own pins; the Models page changes them without a restart.

### The bundle's agent log is quiet again

The service watcher polled pm2 every minute and printed a traceback each
time in the bundle, which has no pm2. It now says so once and stands down;
compose restarts a dead service itself.

### Accounts beyond the first are a licensed feature

The public build is the single-operator product. It no longer offers to
create further accounts: the route refuses with a plain reason and the
Users page is not shown. Nothing changes for an existing account, and a
licensed deployment keeps the page.

### The shipped runtime limits are the proven ones

The defaults were the first week's guesses: 180-second model calls,
two-minute checks, 200 model calls a task, a $2 budget. Every real install
raised them by hand after the first timeouts. A fresh install now starts
from the values the reference deployment runs: five-minute model calls,
ten-minute checks and builds, 400 model and 500 tool calls a task, a $10
task budget and $15 planning turns. Twelve of the twenty-two limits
changed; the Runtime limits page still overrides any of them.

### The daily jobs run inside the agent

Memory consolidation and the codebase map were host crons: the compose
bundle has no cron and a desktop app closed at night never reaches one,
so in both they never ran. The agent schedules them itself now: once a
day, at the first quiet moment after they are due, checked on startup and
every ten minutes. Both are cheap when there is nothing new. The memory
panel shows when each is next due, whether a due run is waiting for the
agent to go idle, and has a Run now button per job. The host crons still
work and count as a run; they are no longer needed.

### The first task in the bundle could not reach the router

The agent's first model call answered "404 Not Found". Compose gave the
agent the router's bare address where a host install gives it the `/v1`
base the OpenAI client appends `/chat/completions` to. Fixed in compose,
and the example env now shows the full value.

### The Models page works in the bundle

It was a 500: the agent read the router's config from a host path the
container does not have, and Analytics read the router's ledger the same
way. Both are shared volumes now. The router seeds its config from the
committed example (or your `ROUTER_CONFIG`) on first boot and keeps the
volume's copy after that, so pins set on the Models page survive restarts
and upgrades and are live on the router's next call. The "restart router"
button is replaced by a note saying so in the bundle.

### The balance card works in the bundle

Analytics showed no API balance although the key worked. The agent asked
the review service, which reads the key from a file only a host install
has. The agent now asks OpenRouter itself with its own key, caches the
answer for a minute, and says "no OpenRouter key" rather than failing
when there is none.

### The Environment page in the bundle shows what compose set

The OpenRouter key showed "not set" although compose had passed it, and
saving answered "could not write the env file": the page read and wrote
`.env` files that exist only on a host install. In the bundle it now shows
the values the process was given, masked, and says the host's `.env` beside
docker-compose.yml is where they change, applied by `docker compose up -d`.

### A Tektonix icon on the Windows desktop

The installer puts a shortcut to the dashboard on the desktop, with the
logo as its icon. The icon builder also kept only one size per `.ico`
until now; the favicon carries its three sizes again.

### The first password can be read from the bundle

`docker compose exec agent python scripts/show_initial_password.py` crashed:
`exec` skips the entrypoint, so the DSN and signing key the entrypoint
derives were not there. The script now reads the key from the data volume
and needs no database. The encrypted password also moves from the repo
root into the data directory, so a rebuild of the container before you
have read it no longer loses the only admin password.

### Everything the bundle creates is called tektonix

Containers were `three-d-agent-agent-1`, the network `three-d-agent_checks`,
the database `three_d_agent`: the working name from before the product had
one. The compose project is `tektonix` now, and so is everything it names.
An install made before this keeps its data with two lines in `.env`:
`COMPOSE_PROJECT_NAME=three-d-agent` and `POSTGRES_DB=three_d_agent`. The
host installer's nginx site and the example DSN follow suit, and the docs
say `/home/tektonix` where they meant "where install.sh put it".

### The sandbox image is built before the agent starts

On the first Windows install the agent was declared unhealthy and the stack
failed. The agent's entrypoint built the sandbox image on first run, inside
the health window; on a slow first download the window closed before the
agent had started serving. Compose builds the image itself now, with its
own progress on screen, and the agent waits for that; the health window is
wider too, for a slow first database start.

### The installer says an empty projects folder is fine

Its question read as if you needed repositories on the machine already. The
folder is where the agent works: repositories there are visible to it, and
one it clones from GitHub lands there too. The prompt, the env example and
the docker README say so.

### Scripts check out with Unix line endings on Windows

Git for Windows converts text files to CRLF by default, and a CRLF copy of
`docker/checks-postgres/init.sh` mounted into the checks database killed
that container, which the agent waits on. A `.gitattributes` keeps every
shell script and Dockerfile at LF on any host. The installer and the docker
README also said the first password is printed in the agent log; it has
been stored encrypted since the code scanning fixes, and both now give the
command that shows it.

## v0.8.0 — every check contained, the gate reads the diff, benchmarks from a page

### Twenty-two code scanning alerts closed

GitHub's scanner read the routes that turn a request value into a file
name as path injection, although each matches the value against a pattern
without a separator first. They go through one helper now that normalises
the joined path and checks it stays under its root, which is the same
guarantee said the way the scanner recognises. Alongside: the SWE-bench
instance-id pattern is linear where two overlapping groups could go
quadratic; an artifact id is compared against stored names instead of
globbed; the eval suite's broken-spec message comes from the loader as data
and any other failure is one sentence and a log line; the review server's
lookups by project name are own-property only, where `__proto__` found
Object.prototype; an artifact image URL is rebuilt from its validated parts
before it reaches an href or src; the trusted-proxies warning no longer
prints the entry; and the prototype-pollution finding in the golden-suite
fixture is dismissed on the alert as the bug that fixture exists to hold.

### The database checks' network holds the throwaway services and nothing else

The agent sat on the `checks` network to create each review's database,
which put its own API, listening on every interface, one hop from every
database check. It is off that network now: it sets a run up with
`docker exec` into the two service containers instead, and holds no
connection and no password. Each run also gets a plain role of its own
owning a database created from `template0`, in place of the server's
superuser, whose bootstrap password an init script replaces at start; and
its own Redis database from a pool, where two reviews at once used to share
database 15 and one's flush emptied the other's keys. The end-to-end test
now proves the check runs as a plain role, that the superuser with the
compose file's password is refused, and that the agent opens no connection
of its own during a run.

### The password re-check on three authenticated routes is rate limited

Changing the password and setting up or disabling 2FA re-check the current
password, and nothing slowed a stolen session guessing it. They now share
the login brake: five wrong tries a minute, then five minutes locked, and a
right password clears the window.

### Stopping a benchmark run removes its containers without a shell

The cleanup ran a `docker ps | xargs docker rm` pipeline through a shell. It
is two argv calls in a background thread now.

### The bundle runs the database checks, contained

`db:drift`, `db:seed` and `test:e2e` were refused in the compose bundle: they
need a Postgres and a Redis, and the only place to run them was the
reviewer's own container, which holds the merge secret. The bundle now
carries a throwaway `checks-postgres` and `checks-redis` on an internal
network with no gateway. The agent creates a database there for each review,
flushes a scratch Redis database, runs the three commands in order in the
same hardened container as every other check, joined to that network alone,
and drops the database whatever happened. The commands and their directory
come from the project's own configuration, never from the request; the DSN
and the secrets are built for the run. A bundle without the two services
gets the refusal it always did, and a host install is unchanged.

### The bundle's reviewer probes the sandbox before it trusts it

In the compose bundle the reviewer asks the agent to run each check in a
sandbox, and that path had no probe: a missing sandbox image became docker's
own error on the check's output, which failed identically on the base commit
and was filed as pre-existing, so the check silently never ran. The reviewer
now asks the agent first, the way a host install asks docker, and remembers
only a yes; the agent refuses a run without the image as a setup problem
rather than a check result, and builds the image on demand from
docker/agent-sandbox, so one failed boot build no longer leaves every review
refused until a restart.

### Three files split at their seams

`agent/server.py` was 3,642 lines; its auth, project, upload and review-proxy
routes are routers now and it is 2,069, with every named seam out (docs/todo.md
has the history). `agent/deep_agent.py` was 2,353; its prompts, the model
client and the project-memory layer have their own modules and it is 1,329,
re-exporting every name so nothing that imports it changed. The commit
reviewer's `reviewer.js` was 2,468; process running, the review checkout, the
mechanical checks and what the model is shown are four modules and it is 864,
with the same exports. Guards, route paths and prompt text are byte-identical;
the route inventories did not change.

### An outside audit, merged

Eighteen commits from a security and correctness audit of the public
repository, reviewed against this deployment and merged. Among them: the rate
limiter believes forwarded-for headers only from a trusted proxy
(`AGENT_TRUSTED_PROXIES`, loopback by default, which is nginx on the same
box); the headless browser reaches the network only through a local proxy
that connects to the address it checked, closing DNS rebinding; file tools
open paths without following a link swapped in after the check; the GitHub
token never appears on a git command line; one-time codes are consumed
atomically and a password change voids reset codes; the two review services
update their shared state under one lock and run git with hooks off; the
compose bundle generates its database password and router key instead of
shipping defaults, and its reviewer asks the agent to start the sandbox for
its checks rather than running agent code itself. On top of the merge: the
audit's two new node suites now run in CI, and a merge whose verdict cannot
be cleared still answers 200 and pushes.

### The reliability chart is a fortnight wide again

The errors-per-day series carried only the days with errors, so one busy day
drew as a single point. It covers every day of the window now, zero where
nothing failed. The busy day was the benchmark: its runner wrote 16,776 tool
events into production's log, which trims itself and so lost every
production day before it. A benchmark run writes its events beside its own
run now, and the benchmark rows were removed from production's log.

### A Benchmarks page, and SWE-bench runs start from it

The Analytics page had grown two whole benchmarks under its numbers. The
golden suite and SWE-bench Verified now have their own admin page,
**Benchmarks**, and Analytics keeps the windowed outcome numbers. SWE-bench
runs start from the page -- sample size, seed, tasks at once, per-task budget
-- as the same runner an operator uses from a shell, in its own session and
split into processes of five, and stop from it; the runner's own log shows
under the run. The golden suite's stop button was already there.

### The gate reads the diff for three shapes of a wrong fix

Two 50-task samples lost the same tasks the same way with the rule against it
in the coder's prompt: an invented error message where the hidden tests
assert the existing template; a grammar rewritten to accept what it used to
reject, with tests for the parsing side only; a fix applied to one of two
functions of the same name. A paragraph in a long system prompt lost to the
decision in front of the model every time. The ship gate now reads the diff
for each shape and sends the fix back once, at the decision point, with the
specific thing to change: keep the message template; open the sibling
definition; add a must-still-reject test. On benchmark projects for now.

The coder seat's chain of thought can be switched off for one runner process
(`--coder-reasoning off`, recorded in the summary): its losses read as
over-thinking and its blowouts were pure reasoning, and with it off the same
model answers the same prompt in a quarter of the time. Measured on the ten
tasks the first sample lost before it decides anything.

### The reviewer reads the agent's answer

A review round is an argument, and only one side was being heard. The
reviewer reads the diff; it cannot run code. When it rejected a fix, the agent
ran the reviewer's own example, the test it asked for, and a probe showing the
reviewer's proposed change re-broke the issue, and put all of it in its closing
message, which went nowhere: the follow-up commit carried the goal and nothing
else, so the reviewer saw the same diff with a comment and repeated itself.
Three rounds later its breaker fired and a task whose deleted line was the
reference fix's own ended as "escalated".

Now the agent's closing message of a round rides in the follow-up commit under
"Response to review round N", the reviewer is given every answer on the branch
before it repeats a finding, and a blocking finding a response has disproved
with a run is withdrawn unless the diff itself shows otherwise; a finding
repeated must name the evidence it disputes. The rejection tells the agent its
message will be read. On a benchmark, where the prediction is the tree and no
human is waiting, a fix the breaker stops is shipped as disputed rather than
escalated. The reviewer's leaked tool-call tags no longer end up in its
summary.

### What the 50-task SWE-bench sample taught the harness

The 2026-09-25 sample resolved 40 of 50. Reading every trajectory found five
mechanical problems, none of them the fix itself; all five are changed.

- **A model that thinks until it has nothing left to say.** The coder returned
  an empty reply with its whole 32,768-token output budget spent on reasoning
  65 times in one run: $2.32 of $18.73 and 2.7 hours of model time, in six of
  the ten failures. It ignores every reasoning cap the router can send. Now an
  empty, length-capped reply is retried once on the fallback seat at low
  reasoning effort, inside the same turn, and every seat's output is capped at
  16k tokens. The old "your last reply was empty" nudge is the last resort, not the
  first.
- **A verifier cut off with nothing to say.** 13 of 48 verifier runs hit their
  tool-call cap and returned "Tool call limit reached" and nothing else; one of
  them had found the exact case the hidden test checks. On its last allowed
  call a bounded subagent now gets no tools and must write its report, and the
  countdown fires when a threshold is crossed, not only when it is hit exactly
  (two calls per turn skipped it). Its counters reset per invocation instead of
  carrying over to the next round. The test-writer gets the same cap; a
  subagent that runs out of model calls ends with a report instead of killing
  the whole pass (one did, with the fix already written). The verifier has its
  own ledger line (`agent-verifier`) instead of being billed as the test-writer.
- **A baseline that tested the patched code.** `/baseline/tests/runtests.py`
  imports the editable install, which is the workspace, so "fails on baseline
  too, pre-existing" was said 39 times about the very code under change, and
  one task that passed in two earlier runs failed on it. The shell now sets
  `PYTHONPATH=/baseline` for a `cd /baseline` command on a benchmark, and the
  prompts say why.
- **A loop the guard could not see.** One test-writer ran a byte-identical
  command 352 times ($1.15) because its output carried a memory address, so no
  two results ever hashed equal. Results are normalised before hashing, and
  eight identical calls in a row are refused whatever they returned.
- **A guard that threw away the good half.** Refusing a compound command for
  one hunting segment (`git log --all ... && git status`) also dropped the
  legitimate one; twice the dropped grep would have led to the failing case.
  Only the hunting segments are refused now, named in the result, and the rest
  runs. Reading `.pyc` bytecode, `git tag`/`git branch -a` and `gh` join the
  list of hunts.

Also: the model cited "the upstream fix" from memory in 23 of 50 tasks and
overwrote its own verified fix to match it in three of the failures; the task
statement and the coder's prompt now say a verified change is never rewritten
to match a remembered one. The reviewer's text is kept with a benchmark run
(only its verdict was), the "read files through `read`" nudge stops after two
per workspace (155 firings, no effect), and read-only git commands (`merge-base`,
`stash list`, `cat-file`) are no longer warned about as writes. The scorecard
shows a sample's percentage beside its fraction, and a run now records the box
it ran on -- memory, CPU, load, disk, containers, OOM kills, and the router's
in-flight calls and latency, once a minute -- and shows it under the run, so
"can we run ten at once" is read off the page instead of three logs.
Ten tasks at once per project is the default now (it was one; the ceiling is
sixteen): six at once used a quarter of the box and the router did not notice.
The benchmark's shards are its own affair -- one runner process and one SQLite
file per few tasks -- and the live agent needs none: one process, Postgres, and
a per-project set of advisory-lock slots that a second worker also honours.

### The agent can show you images, and logo work starts from your logo

A new `show_images` tool puts images in the conversation -- an SVG the agent
wrote, or an image in the repo -- stored per project and served only to
someone who may see that project. Planning and build tasks both have it.
Until now every render went to a vision model and came back as words: asked to
"show me examples of the logo", a planning session drew sixteen versions,
approved its own, and showed the operator none.

Planning is told to do logo work in order: start from the project's existing
logo (trace it, keep its look) unless a new one is asked for, show the options,
let the operator choose, and only then put the agreed SVG in the plan; the
build task exports that SVG rather than redesigning it. `logo_render` renders
on white by default and its `background` now actually sits behind the logo --
it filled only resize padding, so "check it on dark" had been judging a
transparent image.

### A merged dependency fix now reaches the running site

A merge that bumps a lockfile installs nothing by itself, and a deploy only
built, so merged Dependabot fixes never reached what was running: on one
site served from this machine, `sharp`, `nanoid`, `next` and `nodemailer` were
all still at the versions the fixes replaced. A deploy now installs what the
lockfile says (`npm ci`, or pnpm/yarn frozen) before it builds, whenever what
is installed does not match it -- so the next deploy also heals drift that is
already there. The reviewer asks the same question before borrowing live's
install: it had been testing every review against the stale packages, and the
resulting version-test failures showed on the base too and were waved through
as pre-existing. On the project where that happened, the suite went from 11
failures in 306 seconds to none in 5.

### Several tasks per project at once

Each task now has its own workspace — a git worktree on its own branch, filled
from the project's workspace with dependencies hardlinked (instant, no extra
disk) and build output copied — so tasks on the same project no longer queue
behind each other. **Settings → Tasks at once per project** sets how many
(default 10, at most 16 -- it shipped at 1 and was raised the same day, see
above). Tasks code in parallel and take turns only for checks, review and
merge; the second to merge is rebased and reviewed again.

The reviewer keeps a verdict per branch instead of per project, queues a review
asked for while it is busy (it used to drop it, leaving the task to wait out its
whole timeout), and counts fix rounds and churn per branch. A merge clears only
the merged branch's verdict. A finished or deleted task's workspace is removed;
the supervisor sweeps up the rest, and a workspace is never deleted while
anything is mounted in it.

A project can name its own sandbox environment — `stack` from
`docker/stack-images.json` or `sandbox_image` — for the agent as well as the
reviewer. The golden suite can run tasks in parallel (`--parallel`, or "Tasks at
once" on the dashboard) and records how it ran.

### The golden suite on the dashboard, and 30 tasks instead of 12

Analytics has a **Golden suite** panel for admins: the latest full run as a
scorecard (pass rate, first-pass rate, cost and time per task, by category),
what regressed or got fixed since the run before, the run history, and each
task's failing assertions and diff. **Run suite** starts a run from the
browser, detached so a deploy does not kill it, with live progress and a
Stop button; **Copy scorecard** gives a one-line summary to paste.

The suite grew from 12 tasks on 3 fixtures to 30 on 5. The two new back-end
fixtures bring security (SQL injection, path traversal, XSS, prototype
pollution), concurrency, time zones, money rounding, performance and refactor
tasks, plus two test-writing tasks scored by mutation. Each new task was
checked both ways: it fails on the untouched code, and a hand-written correct
fix passes it.

### The review's follow-up, worked through

`docs/review-2026-09-23-followup.md` has a resolution table. The ones that
change behaviour: the supervisor, startup resume and the inbox's "still
handled" check now see every task, not the newest 50 or 100; a push the
remote refuses is no longer "healed"; a parked task gets its own stashed work
back when it resumes; `/planning` no longer traps the Back button; an admin
URL opened by a restricted account explains itself. `server.py` is down to
3,659 lines with the task and planning routes in their own routers.

### The 2026-09-23 review, worked through

All forty findings are answered in `docs/review-2026-09-23.md` §17. The ones
that change behaviour:

- **The reviewer's host database checks no longer inherit its environment** —
  they ran with the router key and control secret in reach while the comment
  and `SECURITY.md` said otherwise.
- **The review services answer nothing but `/health` without the control
  secret**, reads included; `/health` counts projects under review instead of
  naming them. Both console bridges already sent the secret.
- **Every repo-scoped route is pinned with its repo check**
  (`tests/test_repo_scope.py`), so a refactor cannot drop one quietly.
- **The router balance is admin-only**; the approve link refuses cross-site
  posts, is rate-limited and uncached; health failures carry a reason code,
  not exception text; the rate limiter logs when it fails open.
- **Two-factor in Settings** — users can turn it on or off; admins are
  pointed at recovery codes and a runbook. A recovery code could not be typed
  into the login box (13 characters into a 12-character numeric field); now
  it can.
- **Deep links**: a task, a planning session and every view have a URL, with
  back/forward.
- `agent/tasks.py` holds task creation, and the GitHub inbox is its own router
  seam; dead code (`LogEntryCard`, `/api/stats`, `current_step_index`,
  `plan_progress.counts`) is gone; duplicated helpers are shared.

### Tasks heal themselves from infrastructure failures

Sixteen of the last thirty days' nineteen escalations were plumbing, and each
was fixed by clicking Resume once the cause had gone. A **supervisor** now does
that: every minute it closes tasks whose work is already on main, and puts a
task escalated by an infrastructure failure back through the gate once the
cause has cleared — with backoff, a cap (`auto_heal_attempts`, 0 = off), and
never for an escalation older than a day or one that is the task's own
failure. Every heal lands in the task's log and as an alert.

Behind it, the **task lifecycle is one tested table** (`agent/lifecycle.py`):
every action, the states it may start from, and the exact state it writes,
with every state × action pair walked in CI. The ship step also always runs on
the task's own branch now, whoever used the workspace last.

### The reviewer stopped approving checks it never ran

A project that added CI after onboarding had its linters and bundler living in
per-package `node_modules` the reviewer did not know about, so `oxlint`,
`vite` and friends were "not found" on the task branch **and** on its base, and
the baseline filed every one as pre-existing: READY with no check actually run.

- **A check that could not run is never pre-existing.** A missing tool or a
  read-only filesystem (`EROFS`) is infrastructure; the baseline now skips it,
  so it reaches the escalation instead of the approval.
- **`node_modules` directories are detected live**, on every poll, by the
  reviewer and by provisioning with the same rule (parity-tested): any package
  dir up to two levels deep that declares dependencies, plus the root when it
  declares its own or is a workspace root. What is configured is a floor, not
  a ceiling; an explicit `[]` still means none.
- **Per-package `node_modules` are mounted into the check container**, not just
  a root-level symlink, and **build caches are never linked** from live — a
  stale `.vite-temp` made `vite build` fail read-only on every commit.
- **The agent names the branch it wants reviewed** (`/check/<project>?branch=`)
  instead of the reviewer guessing the newest unmerged one.

### Is the agent getting better? Two ways to ask

**A Benchmarks panel** at the top of Analytics. Everything else on that page
answers *what happened*; this answers whether the agent is improving:
first-pass review rate, fix cycles (median and p90), escalation rate, cost per
shipped task, and whether the retrieval subsystems are earning their keep.
Every number carries the previous window of the same length and the delta
between them, because a 60% first-pass rate means nothing until you know it
was 45%. A delta is **omitted, not shown as zero**, when either window had
nothing to divide by, and under ten tasks the panel says the sample is too
thin rather than drawing a confident arrow.

**A golden-task eval suite** (`evals/`, `scripts/run_evals.py`). The panel
above measures production tasks, which move with whatever you happened to need
that fortnight — the right measure for "is it better in practice", the wrong
one for "did that change help". The suite began as twelve fixed goals against
three dependency-free fixture repos (thirty on five since, above), driving the **real** pipeline: real work node,
real check suite, real commit, real reviewer. It stops before the merge with
no special mode, because `require_merge_review` already parks a task after a
READY verdict and the harness is simply an operator who never approves.

Tasks are scored on **assertions, not outcome**. A task can ship, pass its
checks and earn READY having "fixed" the bug by weakening the test, and that
is the one failure every gate here is blind to. Assertions split into goals
(must *become* true) and guards (must *stay* true), and `--verify` refuses a
suite whose goals already pass on the pristine fixture — an assertion true
before the agent runs tests nothing.

A run is isolated by construction: its own SQLite store, its own
`projects.json`, its own reviewer pair on free ports, its own verdict state
and usage log. Measured on the first full run: **$0.28 and 67 minutes for
twelve tasks** — money is not the constraint, wall-clock is.

### A task that is waiting says so

While one task per project was the rule — they shared one worktree — the
status was written *before* the lock was taken, so a queued task was
indistinguishable from a working one: "Running" with a live pulse, no log, no
spend, and the only way to tell was noticing it had been like that a while.
Tasks now show **Queued** (dim, not pulsing) until they actually hold a slot
on the project (one then; up to sixteen now). The orphan-recovery scan and the sidebar's Running group both learned
about the new state; the Telegram alerter learned to ignore it.

### A stopped task no longer strands its alert, or its workspace

Two ways a task that ended badly used to leave damage behind.

**Its inbox item.** Once an item reached `task_created` it was permanently
non-actionable — the dashboard offers no button on that state and the poller
had no transition back. So stopping a task took its GitHub alert out of reach
while the alert was still open. An item whose task is no longer in flight now
returns to `proposed`. "In flight" includes escalated (it is in your list with
a Resume button); stopped, errored and finished-without-fixing-it are not.

**Its workspace.** A stopped task left 2,086 files of downloaded reference
material in the shared worktree. The next task read those instead of the code
it was pointed at, and the base sync then refused *because* of that debris —
the exact failure the sync exists to prevent, reintroduced by its own safety
check, while returning `ok: True`. A task now claims the workspace when it
syncs, so a dirty tree can be told apart: its own uncommitted work is left
alone, and anyone else's is **stashed** (never deleted — `git stash list`
recovers it) before the sync proceeds.

### An episode means the work landed

`_is_terminal` guarded the command-approval pause but not the merge-approval
one, so a task parked on your final look wrote an episode saying
`outcome: shipped` for a commit that had merged nowhere. Consolidation learns
from episodes and reads "shipped" as work that landed, so a parked task taught
one project's memory that a file existed; the operator never merged, and for a
day the memory asserted something that was not on main. A planning session
then believed it, concluded there was nothing to build, and a build task
started against that conclusion.

The episode is now deferred, not lost: approving re-enters the node, the merge
happens, and the terminal write runs then. Note that tasks parked on your
merge decision no longer appear in the Benchmarks panel until you merge — the
numbers count landed work rather than approved work.

### The planner will not hand you a plan that says there is no plan

The unsaved-plan safety net adopts a long, markdown-structured final reply as
the draft when no `save_plan` happened. A turn that investigated a request,
found the work already done and ended *"So there's no plan to save"* cleared
that bar — which armed Build Now, and a task started with a $10 budget whose
goal was an essay explaining that nothing needed doing. The net now checks
whether a reply *disclaims* itself, reading only its conclusion so a plan that
mentions in passing that something already exists is still a plan.

### The agent knows three more things about its own sandbox

All three cost real tool calls and model time to rediscover per task.

- **`gh` is installed**, and deliberately **not** logged in. No GitHub token
  is passed into that container and none should be: the bash tool runs
  LLM-chosen commands, so a token in there is one a prompt-injected
  instruction could push with. It works for public endpoints; this
  deployment's own private repos go through the server-side tools.
- **Every bash call is its own container**, so `/tmp` does not survive to the
  next one. A task curled a file to `/tmp`, got "No such file" on the next
  call, and re-downloaded it — paying a 150-second model call each round.
- **A finding that names a file and a line has already done the hard part.**
  Given a CodeQL alert with sixteen exact locations, a task spent twenty-five
  minutes reading the *analyser's* source and never opened the controller. The
  guidance reaches the coordinator and the investigator both, and is bounded
  on the other side so "stop researching the tool" does not become "stop
  investigating".

### GitHub refusing a workflow file now says what to do

A task wrote a good `.github/workflows/ci.yml`, the review passed, and the
push was refused: GitHub does not let a token write under
`.github/workflows/` without workflow permission. Correct, and unhelpfully
worded — it names a "workflow scope", which is the *classic* token's wording,
while a fine-grained token spells it Repository permissions → Workflows →
Read and write. The ship step now recognises that refusal and writes the path
for the token kind actually in play, says the branch is committed locally and
nothing is lost, and mentions that a deploy-key push is not subject to the
restriction at all. The escalation carries that message instead of the repr of
the result dict it used to print.

### Agent-authored code stops running on the host

`commit-reviewer` ran each project's configured checks against the agent's
worktree with `execFile`, as root, outside any container. The agent's shell is
sandboxed but its write/edit tools are not — that is the job — so a test file
it wrote was arbitrary code running as root: a complete path from "model in a
container" to "root on the machine". `sealedEnv()` stopped secrets reaching
those commands and did nothing about what they could do once running.

Checks, the build, and the `composer`/`mix` dependency installs now go through
one `runAgentCode`. On a host install it is the sandbox container; in the
bundle it stays in-process, because that service is already contained and
`docker-compose.yml` gives the Docker socket to `agent` alone — handing it the
socket so it could start a sandbox would give that container host-root
equivalent. When neither applies the review **refuses** rather than falling
back to the host.

Nine checks across three real projects were run both ways before any of this
was wired in, and two things would otherwise have broken every project: a
symlinked `node_modules` dangles inside a container, and a `mount --bind`
nested in the worktree is not carried in by a plain bind of its parent. The
ninth check, `pnpm audit`, needs egress — so a check may declare
`network: "bridge"` in `projects.json`, a file outside the worktree that the
agent cannot write.

**Checks run in the image their toolchain needs.** The sandbox image carries
Node and Python; detection configures checks for Go, Rust, Ruby, Elixir, Java,
PHP and .NET too, so one image would have turned every review red for anyone
whose project is not JavaScript. `docker/stack-images.json` is a single copy
of the map `scripts/verify_stack_checks.py` already proved in CI, and the
stack is stamped per CHECK at onboarding — a Go backend with a React frontend
is an ordinary repository. `scripts/check_sandbox_tools.js` (also run by
`scripts/doctor.py`) says which tools are missing from which image before a
project's first review, and a missing toolchain is reported as a setup error
naming the tool rather than as a failing check.

See `SECURITY.md` for the threat model and what it deliberately does not fix.

### Project memory is read in sections

A project's `AGENTS.md` was injected whole into every model call and only
grew. It is now split: the preamble plus the rules that fail *silently* stay
resident, everything else is indexed and read on demand with
`read_memory_section`. A build gotcha fails loudly and can be fetched; a
test-wiring rule fails silently, so it stays in front of the model.

Measured on real projects: **10,424 → 2,635 tokens** and **6,671 → 1,655**,
on every model call of every task. Memory under 8,000 characters is not split
at all — below that the index and the round trip cost more than they save.
`read_memory_section("all")` returns the whole file, so a model that cannot
tell from the index has one call that definitely contains the answer.

The nightly consolidator re-splits what it writes. Without that it would have
gone on succeeding every night and quietly stopped reaching any agent, which
is the failure the split exists to prevent arriving from the other side.

### Searching what past tasks ran into

~458,000 tokens of episodes and task history that nothing could query. The
consolidator distils recurring patterns into memory; a one-off from three
months ago was unreachable. `agent_history_fts` holds a weighted tsvector —
error text first — behind a GIN index, with `search_history` and
`read_history` on the coordinator, investigator and planner.

The index is populated **before** the pruner deletes anything, and the prune
runs only if that copy succeeded — the ordering was never the guarantee.
Records are demoted, never deleted, so the index is the archive of what the
pruner removes.

### A SQLite backend, an embedder, and a vector leg that is off

`agent/backends.py` selects the backend by DSN; the SQLite branches are real,
with a parity suite that asserts the **differences** rather than hiding them
(naive vs aware timestamps, tie ordering). Postgres remains the server's
default and is unchanged. `requirements-cli.txt` keeps the dependency out of
the server's own requirements.

The router serves `/v1/embeddings`, billed to `routing.jsonl` like every other
call. Vector search is built, tested on both backends, and **off by default**:
measured across 11 real queries it was actively harmful before a similarity
floor — a nearest-neighbour search cannot say "nothing matched", so it filled
every page and let "least far away" outvote an exact identifier. It is one
switch in Settings, and `doctor.py` prints the three steps to turn it on.

### Smaller things

- The task log mounts a window anchored to its end instead of every row; a
  3,000-entry log no longer mounts 3,000 nodes.
- A `bundle` CI job (manual) actually starts the compose stack, waits for
  `/api/health`, and asserts every service is still running afterwards.
- `agent/routers/push.py` is the first seam out of `server.py`. The route
  inventory now follows included routers — without that, an extracted seam
  counted as zero routes and would have fallen outside the only test that
  checks a route still has a guard.
- Removing a project can delete its checkout, when nothing would be lost and
  nothing this box serves runs from it.
- The planner and the task investigator can run a project and look at it.
- LogoLoom: design a mark, render it, and export a brand kit.


### The bundle waits for the router, and a few leftover review items closed

A first task that started while the router was still booting died mid-call
with "peer closed connection". Compose now waits on health: the agent on
the router, the reviewers on the agent (so the control secret is already
on the shared volume). The file itself is validated in CI with
`docker compose config`; bringing the stack up is still a dispatch job,
written down in `docs/todo.md` with the larger leftovers (split
`server.py`, virtualize the task log, isolate reviewer checks).

The unused `getModelStats` helper is gone. Chat expanders are buttons
(Enter/Space, `aria-expanded`). Newsletter signups are rate-limited per
IP, still as a 303 because the form has no JavaScript. The review
dashboard labels models from the ledger rather than a hardcoded list
that drifted from `config.yaml`.

### Past tasks are on their way to being searchable, and stop being deleted

Every task writes an episode saying how it ended and, when it went wrong,
why. Nothing could query one, and the nightly consolidation deletes them
past a retention window once it has distilled the recurring patterns into
memory -- so a one-off from three months ago was both unreachable and on a
path to being destroyed.

`agent_history_fts` is a table of ours beside langgraph's, holding the text
of every episode, task and build transcript with a weighted `tsvector` and a
GIN index over it. It is created by a versioned migration that runs on every
start and applies nothing on the second, and it touches none of langgraph's
own tables. An episode is indexed as it is written; the rest are reconciled
by the nightly job, which now indexes BEFORE it prunes -- an episode deleted
before it is indexed is gone, and a test asserts the observed call order
rather than the source order, because a refactor that moves one call into a
helper would pass the latter. When the pruner does take a store row, the
index row is demoted rather than deleted and keeps the text.

The order alone was not the guarantee, though: the prune ran whether or not
the copy had worked, so a night when the index was unreachable deleted store
rows anyway -- and an installation that never opened an index at all looked
identical, from the result, to one that has none by design. The prune is now
conditional on the copy having actually happened, the nightly run says so in
its summary and exits non-zero, and the pruner reads the whole namespace
rather than the first page of it. Deleting a task from the dashboard indexes
its row and its transcript first, for the same reason; removing a project
refuses outright rather than archiving a subset when the index it would have
copied from is not open.

`scripts/backfill_history_index.py` fills it from what is already there; it
is dry-run by default, writes only rows whose text actually changed, and
never writes to or locks the store, so it is safe against a live server and
a second run is a genuine no-op.

Removing a project used to leave its build transcripts behind -- the
namespace agent/planning_log.py writes to was missing from the canonical
list of what a project owns. It is in the list now, and the history index,
which is a table rather than a namespace, gets its own archive and removal
step beside it.

### Asking what past tasks ran into, from inside a task

`search_history(query)` searches that index and `read_history(ref)` opens
one record. The split is the economy of it: an episode runs to thousands of
tokens, so eight returned whole would be ~25,000 tokens spent before the
model has decided any of them is relevant. A digest of eight measures ~770
tokens against the real corpus.

Ranking is a two-stage ladder, because one query shape cannot serve both
things this gets asked. An AND of every term is right for a phrase and wrong
for a pasted traceback, where one token that has since moved -- a line
number, a sha, a renamed path -- takes recall to zero; an unbounded OR of
the same words returned 86 of 146 episodes. So the precise stage runs first,
and only if it came back nearly empty does a widened one run beside it, with
its terms filtered by how much of the corpus they appear in and its tail cut
at a fraction of the top score. The precise hits keep the top of the page.

A page of eight is eight pieces of work. One record occupies one slot
whatever it matched in -- decided by a window function in SQL, because
collapsing an over-fetch afterwards let a single long transcript eat the
page and hand back five records out of the ninety-five that matched -- and
the episode, the near-duplicate episode and the task row of one task fold
into one entry naming the others.

A question this system has never seen comes back as a miss rather than as a
confident page: the widened stage will not run on a query that has collapsed
to one ordinary word, and the hits it does return are marked as widened in
the digest. A path is searchable the way a reader types it, relative or bare
filename, not only byte-identically to the absolute one in the corpus. And a
query is capped and every statement runs under a deadline, because the text
is model-authored, the cost is superlinear in its length, and in the server
these run on the same pool that answers logins.

Neither tool queries the index. Both ask agent/episode_recall.py, where this
index is registered as one retrieval leg -- so a second way of looking things
up can arrive later without touching a tool, a seat or a prompt.

The build coordinator, the investigator, the general-purpose seat and the
planner have both tools; the test-writer has neither. Which projects a seat
may search is the same allow-list the reference tools use, through the same
check, with one deliberate inversion: those refuse your own project and
these default to it. A ref is re-checked on the way back in, because it is a
name and not a capability.

Every search records what it offered and every read records what was taken,
so the question of whether this earns its place is answerable in a month
with a number rather than an argument.

### The public tree is this product, not the box it grew on

The example router config that `install.sh` copies onto a fresh install still
carried aliases belonging to two applications that are not this product, with
comments naming their checkouts, and the Models page introduced them by name.
A visitor reading the seed file, or an operator opening Settings → Models on a
brand-new box, was looking at an inventory of somebody else's software.

Those aliases are gone from `config.example.yaml`. Existing `config.yaml`
files are untouched. The Models page now says the property: aliases that do
not begin `agent-` are left alone.

The review-secret fallback the Node services claimed to keep for upgrades
still pointed at `services/llm-router/.env` after the directory was renamed
to `model-router`. Doctor warned about the current path; the services never
read it. They do now, and the pre-rename path remains a last resort.

A handful of docs and one fallback URL still sent people to the LiteLLM
port `:4000`. The router has been on `:4001` since the cutover.

CONTRIBUTING claimed to list the exact CI jobs and then omitted the landing
page, the model-router suite, and `ruff check services/model-router`. The
list is five jobs now, and the hygiene test pins the commands that used to
drift.

The landing page sold the Docker bundle as "agent, database, router and the
review services as one stack". Compose runs three of those. The page now
says the review services are not in the bundle yet, matching
`docker/README.md`.

### The review dashboard could not press its own buttons

`POST /api/review/check/:name` required the control secret, then proxied to
the reviewer **without** it, so "Check now" 401'd even when nginx had
injected the header on the way in. The proxy now forwards the secret it
already checked. A new nginx vhost also gets a `/_review/` location; an
existing one still needs that added by hand (INSTALL.md §7).

`wait_for_review` matched a verdict on the first 12 hex characters of the
sha. It now requires the full string.

A successful 2FA code left the verify-2fa rate-limit window counting, so a
few typos before a good code could lock a legitimate login. Success clears
it, the same way the password step already did.

## v0.7.2 — the gate ships with the bundle, and Windows is a double-click

### The review gate is in `docker compose` now

`docker compose up -d` brings up five containers, not three: the agent, its
database and the model router, plus both review services. A task's branch is
reviewed by a second model and only a pass merges — on Windows, macOS and
Linux, with no Node or process manager on the host.

This was the gap worth closing. The easiest install used to produce an agent
with no gate, which is most of what makes this different from a loop and a
terminal.

The one thing the bundle will not do is restart your application after a
merge: a process manager runs on the host and a container cannot reach it.
That endpoint reports itself unavailable rather than failing, so a task ends at
a real merge. The host install still deploys.

### Windows is a double-click

`Install Tektonix.bat` checks Docker is installed and running, starts it and
waits if it is asleep, asks for the two values that have no default, and opens
the console when it finishes. It exists because a `.ps1` opens in Notepad and
client Windows refuses scripts by default; the launcher passes the bypass for
its own process only.

Failures that are environmental rather than this software's get named, with
the remedy, instead of Docker's own wording. Where the cause is not
recognised, it says so rather than guessing.

Verified on a clean Windows 11 machine: installer through to five running
containers, agent healthy, console answering.

### `docker compose up` works on a fresh copy

It did not. The router bind-mounted a gitignored file, and Docker turns a
missing bind source into a directory, so the container died on a type
mismatch — for everyone who was not already running it. The default is the
committed example now; `ROUTER_CONFIG` points it at your own.

Three more of the same shape, each a one-line cause: the agent image set an
environment variable name nothing read, so onboarding refused every path the
bundle uses; worktrees defaulted outside the host path map, which would have
mounted an empty directory silently; and the review dashboard reached the
reviewer at a hardcoded loopback address that is a different container here.

### Guards, so these do not come back

Tests now fail the build when a bind mount names a source a fresh checkout
lacks, when an environment variable is set and read nowhere, when anything
private appears in the tree, and when the landing page's description of the
bundle stops matching the compose file. The last one has been wrong in both
directions, so it is pinned to the file rather than to a string.

A `.dockerignore` denies by category rather than by filename, and two tests
check both directions: nothing sensitive enters a build context, and nothing
the images need is filtered out.

## v0.7.1 — the public tree is this product, and nothing else

Re-cut of v0.7.0. Its tarball shipped a seed router config describing two
applications that are not part of Tektonix, so that download is withdrawn
rather than corrected in place. Everything in v0.7.0 is in this release.

### Nothing of the maintainer's own software ships here

Aliases for two other applications, comments naming their checkouts and their
incidents, an example projects file built around one of them, and test
fixtures using real strategy filenames. All gone. Fixtures were renamed rather
than deleted: one only has to have the right shape, so the real names were
never doing any work.

`tests/test_repo_hygiene.py` now greps every tracked file for those names and
fails with file and line. Nothing else catches a name that arrives inside a
comment written to explain a real incident, which is how every one of these
got in.

### Fixes that the same audit surfaced

The review secret now follows the router rename, the review dashboard can
press its own buttons, a verdict has to match the full commit sha, and a good
two-factor code clears its own rate-limit window.

## v0.7.0 — a page that is not the product, an installer that installs, and a gate that blames the right thing

### A Windows installer

The Docker bundle already ran on Windows: the bind-mount translation handles
drive letters, and the example env file gives the Windows form of the projects
directory. What was missing were the four steps between a downloaded copy and a
running console, the first of which is `cp`.

`install.ps1` does them — checks Docker is installed and running, asks for the
two values that have no default, writes the `.env`, and brings the stack up.
`-DryRun` and `-Yes` mean what they do in `install.sh`. It runs under
PowerShell 7 on macOS and Linux too; Windows is why it exists.

Double-click `Install Tektonix.bat` to run it. A `.ps1` cannot be
double-clicked into running — Explorer opens it in Notepad — and Windows client
editions default to an execution policy of Restricted, so even from a terminal
the bare path fails with "running scripts is disabled on this system". The
launcher passes `-ExecutionPolicy Bypass` for that one process and leaves your
machine's setting alone.

### The installer can install the prerequisites

It checked for git, Python, Node and Docker and then stopped, leaving you to
work out the package names — while it already drove apt, pacman and dnf to
install nginx and certbot.

It offers now, for those four plus ripgrep, and for Postgres at the database
step. It asks every time and the answer defaults to no. `--yes` is not consent:
an unattended run should mean "do not stop to ask me", never "put a Node runtime
and a database on this machine". `INSTALL_PREREQS=1` is the opt-in.

Only absent commands are offered. A runtime that is present but below the floor
gets the real remedy instead, since the distribution's package is the version
already installed — Debian and Ubuntu ship Node well behind the floor.

### Download the release instead of cloning

Releases now carry `tektonix-<version>.tar.gz` and its checksum, and the landing
page links to it. One archive serves both install paths on every operating
system, with the dashboard prebuilt so no Node build runs on install.

### The landing page is not part of the agent

It never should have been. `tektonix.io` serves the public page,
`agent.tektonix.io` serves the console, and `site/` is its own project that the
release tarball drops — a self-hosted install has no use for a page selling the
thing you just installed. The page ships no JavaScript; the console is
`noindex`.

**If you had the console installed as an app, reinstall it.** A service worker,
an installed app and a push subscription all belong to an origin, and the
console's origin changed. The old subscriptions were removed server-side;
re-enable notifications from Settings once it is installed from the new host.

### A newsletter instead of a sign-in button

"Sign in" pointed at a private console the visitor has no account on. It is a
newsletter signup now — a name, an address, and what the list is for: the
changelog for each release, plus what is being built next and what turned out
to be a bad idea. Nothing sends yet.

The form is plain HTML, since the page ships no JavaScript, and every outcome
redirects to a real page rather than a status code a browser would show to
nobody. A repeat signup counts as success. Addresses never reach the log, and
every subscriber gets an unsubscribe token at signup so the first issue does
not have to backfill one.

### The review gate blames the right thing

It rejected a clean commit three times and then escalated it. Four causes, all
fixed:

- The baseline re-ran failed checks against a base worktree provisioned
  differently from the branch, so an environment difference read as the
  branch's fault. Provisioning is now an argument the baseline reuses, and
  results are cached per environment as well as per commit.
- A check that could not RUN counted as one that FAILED. A missing command is
  recognised as the harness's fault now and escalates to a human on the first
  round, instead of being handed to an agent that cannot see the environment.
  It still blocks: a check that did not run is not a pass.
- A root dependency install was assumed to cover every package in a repository,
  which only holds when the root manifest declares workspaces.
- The rejection never said why. Check output was captured, shown to the review
  model, then dropped before being stored; it is kept with the verdict now, and
  unrunnable checks are listed apart from real failures.

### Every code scanning alert closed

Forty-four, plus the dependency alerts. Most were three hand-written versions of
one containment check, now a single function — one spelling was weaker than it
looked, since a symlink inside the directory passed it. Stack traces no longer
reach an error response, and the two places that build a command dispatch on a
literal rather than on a value that matched a list.

## v0.6.0 — a name of its own, a router of its own, and a console you can carry

**2026-09-17**

Seventy-five commits on top of v0.5.0, and the largest release so far.

The agent is called Tektonix and lives at tektonix.io. The dashboard installs
as an app on a phone and can wake you with a notification, in whichever of
five colour schemes you pick. Projects are things you create and remove from
that dashboard rather than files you edit by hand. The LiteLLM proxy is gone
and a router this repo owns has taken its place, the console's numbers come
from this box rather than from LangSmith, and Settings became a rail with one
section at a time.

### Tektonix

3D-Agent is now Tektonix, everywhere it is user-visible: the dashboard, the
landing page, the sign-in card, OG cards, the TOTP issuer, the router's
OpenRouter headers, the sandbox image tag, the release tarball and the GitHub
repository (`DJG3DK/tektonix`).

The mark is a plumb line and the palette is called Drafting: dark charcoal
with a gold accent. The landing page was reshot in it and condensed (features
8 → 4, controls 7 → 6), and the hero copy no longer describes a tier system
that was removed weeks ago. For search: a real `robots.txt`, a sitemap,
structured data, and the landing page is prerendered at build time so a
crawler with JavaScript off sees the page rather than an empty root div.

### The dashboard installs as a phone app

Chrome on Android now offers *Install app*, and iOS Safari's *Add to Home
Screen* does the same: a launcher icon, a standalone window with no browser
chrome, and the status bar in the app's own colour. It is the same React app
from the same build -- a web app manifest, a service worker and the icons the
brand generator already knew how to draw (`scripts/brand/build_assets.py`
gained a maskable variant, because Android crops an adaptive icon to whatever
mask the launcher uses and a rounded tile handed to that crop loses its own
corners).

There is deliberately no offline mode. This is a console onto a live agent --
running tasks, streaming logs, a review gate -- so a cached screen would be a
screen that lies. The worker exists for two narrower things: Chrome will not
offer to install an origin that has no fetch handler, and a cold start on a
bad connection should show the app rather than the browser's offline page.
It never touches `/api`, which keeps the session cookie, every mutation and
the task and planning WebSocket upgrades entirely out of its hands; hashed
bundles under `/assets/` are kept forever because their names change when
their contents do; and navigations go to the network first, with the cached
shell only as a fallback. That last one is not a preference: `index.html`
names the hashed bundles of the deploy it came from, so a stale shell served
while the network was fine would ask for a bundle that no longer exists and
white-screen the app after every deploy. Verified by installing a client,
deploying a new bundle underneath it, and reloading.

### Notifications on the phone, and a console in your own colours

The installed app now gets push notifications: the same alerts Telegram
already carried -- a task finishing, escalating, or waiting on an approval --
delivered to the lock screen with the app's own icon. It is one fan-out, not
two. `notify_operators` sends to both transports behind one copy of the
scoping rule, because the alternative is two copies that drift, and the last
time that scoping was wrong (audit H1) a single-repo account was receiving a
live feed of every project.

Permission belongs to a device rather than to an account, so **Settings →
Notifications** talks about this device and says how many others the account
has. Two cases are handled rather than papered over: a browser that has
already been told "block" can never be asked again from JavaScript, so the
panel sends you to site settings instead of offering a button that does
nothing; and iOS delivers web push only to an app installed to the Home
Screen, which the panel detects and explains instead of letting you subscribe
into silence. A subscription the push service reports as gone (404/410) is
deleted rather than retried, since those never recover.

The VAPID keypair is written once to `keys/vapid.json` and read back
thereafter. Regenerating it would invalidate every subscription ever issued,
and the symptom is "notifications just stopped" with nothing in any log.

**Five colour schemes**, per account, in **Settings → Appearance**: Drafting
(the brass the mark was drawn for), Indigo, Orchid, Ember and Moss. Pick one,
see it in a preview pane, then Save applies it everywhere.

The preview is the scheme, not a rendering of it. The pane carries
`data-theme` and every `[data-theme]` block in `theme.css` is scoped by
attribute, so the custom properties inside it resolve to the chosen scheme
while the rest of the page stays on the saved one. A mock built from
hard-coded colours would drift from the stylesheet the first time a token
moved, and it would drift silently -- it would still look like a preview.

Each scheme overrides only what carries its identity: the ground, the accent
family, the ambient light and the shadows' tint. The ambient light had to
become a token to do that; while it was a literal brass `rgba()` in App.css,
a theme changed its buttons and kept the original's light, which is most of
what makes a scheme read as a different room. Everything structural stays
shared, and so do the state colours -- running, waiting, done and failed mean
the same thing in all five, because a colour that carries meaning must not
move with a preference. Every foreground was computed against its own surface
and clears 4.5:1; `tests/test_themes.py` does that arithmetic rather than
trusting the comments, and holds the three copies of the scheme list (the
stylesheet, the picker, and the server's allow-list) in step.

### A project that did not exist yet

Until now a project had to exist before Tektonix could see it: the wizard
and `scripts/add_project.py` both take a directory that is already a git
repository, so the first commit of anything new was made somewhere else, by
hand, before the agent could be pointed at it. The planner's Repo dropdown
now has a third door, **New project…** (admins only): a name, an optional
description and, when a GitHub token is stored, a **Create a private GitHub
repo** box. The server makes the directory under the first allowed root,
refuses a path that exists or that fails the containment rule the wizard
applies, runs `git init -b main` with one commit (a repository with zero
commits gives the worktree no branch to start from), and then runs exactly
the wizard's provisioning steps with the wizard's recommended answers — one
code path, so a project that arrives by either door is wired identically.
The planner opens a session on it at once, and Settings → Projects and every
Repo dropdown refresh without a reload. `scripts/new_project.py` is the
headless twin. Creation is audited as `project.create`.

With the GitHub box ticked, the token is resolved before anything is
created, so a missing token is a refusal and not a half-made directory. The
private repository is made with `auto_init` off, a deploy key is minted for
the project and registered on it with write access, `origin` is set to the
SSH URL — never HTTPS, which the host's credential helper would push as the
operator's personal account — and `main` is pushed from the server process.
The agent still cannot push; this is the API process acting on an admin's
request, the same trust level as the review service's post-merge push,
which is why SECURITY.md's claim about agents and `git push` stays true. A
GitHub failure is a failed step, not an abort: the repository on disk is
real, and the remote can be connected from the project card later.

The planning agent can propose one. When a request turns out to be a new
application rather than a change to the current project, the agent asks for
a name and whether to create a private repo, restates both, and after the
operator confirms calls a `create_project` tool. The tool creates nothing:
it records a proposal, and the dashboard shows a confirm card with Confirm
and Dismiss. Confirm creates the project through the same path and moves the
session onto it, so the conversation continues against the new repository;
a non-admin is told that an admin must confirm. A model that could create
repositories by calling a tool would be one that could create them by being
asked to in a pasted document.

A brand-new project has no checks, so its first review is model-only — and
the merge that lands is the first moment the repository has code for
detection to read. After a merge in a project the reviewer reports as
having no checks, the ship path runs the wizard's detection on the live repo
with its recommended answers and writes `review.checks` into
`projects.json`; the task log shows which checks were written, or that none
are detectable yet. It fills an empty slot only: a non-empty list is never
overwritten, and a project whose checks come from
`builtin-projects.local.js` is left alone. This replaces the manual "add
checks" step that Auto for the GitHub inbox used to point at.

### Taking a project back off the agent

There was a button to add a project and none to remove one, so removing one
meant editing `projects.json`, deleting a worktree, finding the deploy key,
clearing the reviewer's state file and purging six store namespaces by hand --
in that order, because getting it wrong leaves a project half-configured and
unreachable. **Settings → Projects** now has *Remove from Tektonix*.

What it does not do is the half worth stating first: **the live repository is
never touched.** Not a file, not a branch the agent pushed to it, not the
remote. Removing a project means Tektonix forgets it. A control that sits in a
list of someone's own repositories must not be one misread click away from
deleting their code, so the confirmation also asks for the project's name
typed out, and removal is refused outright while a task or a planning turn is
in flight -- pulling the workspace out from under a running task would leave a
half-finished branch nobody owns.

The single exception is the opposite of destructive: `core.sshCommand` is
unset on the live repo, because the agent set it when it minted the deploy
key. Leaving it would point the operator's own git at a key file that no
longer exists and break every push they made by hand afterwards.

What the agent *learned* is a separate choice. **Archive** writes its memory,
generated skills, planning sessions, transcripts and task history to one JSON
file under `archives/`, then removes the rows; **delete** removes them without
the file. The archive is not write-only: the onboarding wizard lists any
archive matching the name of a project being added and offers to restore it,
so re-adding a project is a continuation rather than a fresh start. Archives
are listed in the same panel with their item counts and dates, and can be
deleted there, because an archive nobody can find is a file that accumulates
rather than a safety net.

An archive that fails to write refuses the whole removal rather than
continuing -- the operator asked to keep that, and deleting it anyway is the
one mistake here with no undo.

### The model router is ours

`services/model-router` replaces the LiteLLM proxy. All 21 deployments were
`openrouter/...`, so the proxy was a proxy in front of a proxy; what was
actually used was alias resolution, ordered fallbacks, a billed cost figure
and a callback into our own ledger, and the router does those four things
directly. It also retries transient failures, falls back on a stream up to
the first byte, keeps a stats view of the ledger, runs under pm2 and is
linted and tested in CI.

**Breaking:** LiteLLM is removed — the process, the package, its admin panel,
the WebAuthn gate, its vhosts and its cert. Five consumers were on `:4000`,
and only two were findable by grepping `.env` files. Every one now resolves
through the router on `:4001` **with its own key**; anything of yours still
pointing at `:4000` will stop. What to do when the router refuses a call is
in `docs/runbooks/router-refusals.md`.

The dead tier system (SIMPLE / MEDIUM / COMPLEX / REASONING) is gone from the
router and from the Models page, which now says what it actually controls.

### The model pins are yours

`services/llm-router/config.yaml` is gitignored. The Models page rewrites it
on every repin, so every model change used to arrive in this repo as a diff
somebody had to explain — and an upgrade could overwrite pins an operator had
chosen. The shape is tracked instead, as `config.example.yaml`, which
`install.sh` copies into place on a fresh install and never touches again.

The example's pins are one deployment's answers on one day, not
recommendations. Three consequences worth knowing: the file is not in git, so
it belongs with your backups; a new managed role has to be added to the
example as well as to your live config, and a test enforces that; and a
checkout that has never been installed still reads the example, so the rate
table and the Models page work in a fresh clone.

### The dashboard's numbers come from this box

Three Analytics panels — per-role model usage, tool reliability, run health —
were read back out of LangSmith, which made an optional third-party service
load-bearing for "what is this agent doing", and cost a core: LangSmith's
input masking walked every run's payload on the event loop that serves the
dashboard. The panels now read the router's ledger and the tool-event table
here. Per-role usage is one row per role and says where each number comes
from; the ledger records the alias the client asked for, not only the model
it resolved to, so a role's traffic is no longer split ten ways.

### Settings is a rail

The Settings page was one 4,334px scroll of every card the deployment has,
at three different widths, with two unrelated cards both titled "GitHub". It
is now a rail — Account, Agent behavior, Notifications, Projects, GitHub,
Runtime limits, Environment, Audit log, with admin-only sections hidden from
accounts that cannot use them — and one section at a time, each in a single
readable column.

### Bash is for running things

Measured live: one test-writer subagent made 229 bash calls against one edit
and one write, patching JavaScript through Python heredocs while holding
`read`, `write` and `edit` the whole time. Every bash call starts a container,
so that is wall-clock, not style. The tool descriptions now say what each is
for, the read and shell nudges catch what the model actually types, the repo
tools accept `file_path` so a name slip is not a cliff, and an
empty-but-successful command says what it did.

### Planning and build

A planning session's transcript is kept where a restart cannot take it: the
in-memory buffer died with the process and the checkpoint's message list is
rewritten on every compaction, so a 78-minute turn had no readable history.
The build summarization ceiling is raised from 80k tokens and is tunable —
a coder rode the old ceiling for an hour, re-reading its own output after
each compaction discarded it. A reworded plan step is the same step, the
step counter cannot go backwards, and a verify pass with no diff can still
reach a conclusion.

Auto mode covers GitHub inbox tasks: a Dependabot fix that cannot change a
version string unattended was not safer, only slower. The merge gate still
applies to every one of them.

### The review services read projects.json live

Both Node services bound the project map once at startup, so a project
created or given checks at runtime did not exist for them until pm2
restarted: the reviewer never polled its branches, the deploy service
answered 404 for it, and the agent's wait for a verdict timed out on a
verdict that could not arrive. They now re-read `projects.json` on every
poll and every request. The reviewer also no longer crashes on a project
entry with no `review.checks` — exactly the entry a new project produces —
and returns a verdict instead of an internal error.

### Operating it

A health watchdog restarts only what a restart can fix: it polls each
service's live and ready probes, so a process that is alive but wedged, or
healthy in front of a dead database, is distinguished from one that died — the
silent outage the operator hit before. It covers storefront, webapp and its
compute service. The pm2 memory cap is declared, with its cost stated.

Performance: tool-call streaming is off (langchain-core re-parsed the whole
accumulated argument JSON on every chunk — 25 seconds of CPU for one 734-token
call), the plan-check nag no longer edits the system message so the cached
prompt prefix survives, and a task's bill is durable.

### Packaging

Two seams for the container bundle (`AGENT_HOST_PATH_MAP`, so bind-mount
sources resolve against the host the Docker daemon actually sees), then the
bundle itself: postgres + router + agent as a compose stack, one command on
any host with Docker, all four health checks green on Linux. Windows is the
next test. `docs/roadmap-packaging.md` has the plan, including the CLI and
GitHub-hosted projects.

## v0.5.0 — work that arrives on its own, ten stacks it can check, and a box a second person can run

**2026-09-11**

Thirty-six commits on top of v0.4.0. Three things changed: the agent can pick
work up from GitHub instead of waiting to be told; onboarding detects real
checks for ten stacks instead of two, so the review gate is not a no-op for
most repositories; and the parts that only ever had one operator — a global
auto-approve switch, an unrecorded approval, a lock that lived in one process
— now expect a second person.

### The GitHub inbox

The agent can pick work up from GitHub instead of waiting to be told.
A poller reads each project's open Dependabot pull requests, Dependabot
security alerts, code scanning (CodeQL) alerts grouped per rule, reviews
that request changes, and failing checks on the default branch (from check
runs or, with only *Actions: read*, from workflow runs — Dependabot's own
update jobs excluded). Each source has a
policy per project: **Off**, **Propose** (the item lands in the new
**GitHub** tab and an approve link goes out over Telegram and, optionally,
email) or **Auto** (the task starts at once, within a cap on open auto
tasks). Auto removes only the click: every task this creates runs the
review gate and keeps the operator's merge approval.

**Settings → GitHub** holds the tokens — fine-grained PATs stored encrypted
with the TOTP key, shown as a name and last four characters, with a Test
button that reports which projects a token reaches and whether it may read
alerts, code scanning and checks — the dashboard URL approve links are built on, delivery
switches, and the per-project policy table with budget, cap, author filter
and coder route. The PR tools resolve their token per project from the
same place; `GITHUB_TOKEN` in `.env` is now only a fallback.

Approve links are signed with the auth secret, expire after 48 hours and
are single-use. The link opens a page with one button and only the
button's POST acts, because messengers fetch links for previews.

Also: the origin-remote parser accepts SSH host aliases (a deploy key per
project means one per project; two of three live projects resolved to no
repository until now). `git remote get-url` applied insteadOf rewrites, so
on a box with a GitHub token helper the Settings page reported an SSH
origin as HTTPS and would have shown the helper's token; both the deploy-key
status and the PR-tool slug now read the configured URL.

### Docs and startup

README, INSTALL and the public landing page now describe the inbox's fifth
source (code scanning), the frontend seats, billed-cost budgets, runtime
limits and deploy keys. `MODEL_PLAN` / `MODEL_EXECUTE` / `MODEL_REFLECT` are
no longer required at startup — they had not been read since the alias
pipeline. The landing page's Node floor is 24+, matching `install.sh`.

### Onboarding that produces a real gate

Check detection knew npm properly and treated everything else as "a manifest
exists". A Go, Rust, Ruby, Elixir, Java, PHP or .NET project therefore
onboarded with an **empty checks list** — which does not make the review gate
weaker, it makes it a no-op that still reads as a gate, approving every change
because it runs nothing.

Ten stacks are now read from their own manifests: npm/pnpm/yarn, Python, Go,
Rust, Ruby, Elixir, Java (Maven, or Gradle through `./gradlew` when the repo
ships one), PHP, .NET, and Makefile targets as a fallback. Linters are
proposed only where the repo configures them — a lint its authors never opted
into would fail every review on findings they never agreed to.

The rules the npm path already followed carried over. A suite whose test files
call the network arrives **disabled**, with the file and the idiom named; a
file that stubs or serves its own HTTP (WebMock, httptest, `responses`, nock,
Bypass, Moq) is not counted as calling out, because flagging every honest
suite teaches an operator to click through the flags. A repo that declares its
own reviewer-safe suite is trusted over its aggregate one, in whichever
spelling its stack uses: `test:review`, a Makefile `test-review` target, a
cargo or mix alias, a Gradle `testReview` task, a rake task, a composer
script. And the client may still only narrow what the server proposed —
enabling a flagged suite sends a name, and the server substitutes its own
command.

`scripts/verify_stack_checks.py` (new) is why these are worth trusting: it
runs every proposed command against a fixture repo inside the official
toolchain image, then re-runs it against a deliberately broken assertion,
because a check that cannot fail is not a gate. That is how the rust image
shipping without rustfmt or clippy was found. CI runs the host half on every
push; the container half is a manual job.

The review checkout can now actually run those checks. A git worktree carries
what git carries, so PHP's `vendor/`, Elixir's `deps/` and a bundled Ruby
project's `vendor/bundle` were simply absent and `vendor/bin/phpunit` exited
127 — which reads as a broken suite rather than as nothing having installed
it. The reviewer borrows them from the live checkout, **bound read-only**,
since the code about to run against them is by definition unreviewed. A
branch that changes its own manifest gets a fresh install instead, with
scripts and plugins disabled (`composer install --no-scripts --no-plugins`,
`mix deps.get`) — the same discipline npm's `--ignore-scripts` has always
had here. Bundler is the exception and says so: `bundle install` builds
native extensions, so that branch gets neither the install nor the stale
borrow, and the reason is recorded as a failed setup check.

The same audit found the older node_modules bind had been logging a warning
and keeping a **writable** mount of live's modules when the read-only remount
failed. It unmounts now.

### Team-grade control, not more autonomy

**Auto mode is per project.** It was one global boolean: on meant on
everywhere the account could reach. That was written for a single operator who
understood the sandbox, and it does not survive a second account — a new user
handed the same default inherits it for production along with the scratch
project it was meant for. It now has two halves, the operator's intent and the
projects they intended it for, and both must agree before a task skips a
prompt. Turning it on requires naming projects; there is deliberately no "all
projects" option. Accounts that already had it on are scoped once, at startup,
to the projects that exist, so no deployment changes behaviour silently.

**An audit log**, in the same Postgres as tasks and memory, shown on Settings
for admins: who onboarded a project, who approved or rejected a gated command
and what the command was, who approved a merge, who moved auto-approve or
merge review and for whom, who set a GitHub source to Auto, who generated or
deleted a deploy key, and every inbox item that became a task — by a click, by
a signed link (recorded as *signed link*, because that path carries no
session), or by the poller itself (*github-inbox*). Telegram is a notification
channel: best-effort, unordered, and deleted at the whim of whoever owns the
chat. Writing a record never blocks the action it records.

**Inbox Auto is refused for a project whose review gate runs nothing
mechanical**, and if a project's checks disappear later its items are proposed
rather than started. Otherwise "auto" would mean shipping work that a model's
opinion alone had verified.

**Prompt injection has a fixture test.** SECURITY.md already stated the model;
`tests/test_prompt_injection.py` is now the regression test for it. A repo file
tells the agent, in as many words, to disable merge review, read the deploy key
and force-push, and the tests pin what makes that inert — no tool reaches a
control, every named endpoint refuses an unauthenticated call, the container
mounts only the workspace and is handed exactly two environment variables, and
the approval gate is decided from the account's setting before any file is
read. It is not a proof; injection is contained here by what the text cannot
reach.

### One task per project, across processes

"One task per project" was an in-process lock, which made it true only while
exactly one process existed: a second worker, an overlapping restart, or a
script run against the same database would each hold their own lock object and
happily run two tasks on one worktree. It is a Postgres session-level advisory
lock now, and Postgres drops it when the connection closes, so a crashed
process releases its claim with nobody cleaning up.

### Operating it without reading the source

- **Health routes** on all four processes (`/api/health` on the agent,
  `/health` on the two node services, LiteLLM's own), unauthenticated so a
  monitoring box can reach them, `503` when a check fails, and never echoing a
  secret's value. There was no health route at all until now: every restart
  check in this repo's history curled the agent and got the dashboard's
  `index.html` with a 200.
- **`scripts/doctor.py`** — file modes, env completeness, the key pairs that
  must match between the agent and the node services, every project's
  checkouts, the sandbox image, the pm2 processes. It refuses to print
  anything shaped like a secret.
- **Backups** — `scripts/backup.sh` and `scripts/verify_backup_restore.sh`,
  which restores into a scratch database and checks the tables came back. A
  backup nobody has restored is a hypothesis. `docs/backup.md` has the rest.
- **Releases** — `scripts/package_release.sh` builds a tarball with the
  dashboard prebuilt, so installing needs no Node toolchain.
- **[docs/architecture.md](docs/architecture.md)** (the processes, the
  secrets, the three checkouts, the graph), **[docs/runbooks/](docs/runbooks/)**
  (one page per symptom), **[docs/middleware.md](docs/middleware.md)** (what
  each middleware forbids and which agent has it — subagents do not inherit the
  coordinator's chain), and **[docs/playbooks/](docs/playbooks/README.md)**
  (adding a model role, an inbox source or a runtime knob, each starting with a
  test that fails until the wiring is finished). Tests keep all four honest.

### The task stream

A task page opened mid-run could show less than it knew. The stream now
connects the socket **before** hydrating, buffers what arrives during the
fetch, and merges by content-derived entry id with a monotonic sequence number
per task, so nothing is dropped and nothing is duplicated. A dead socket is
detected after 70 seconds of silence and reconnected; past two minutes with a
live socket the page says *no activity* — the opposite diagnosis, and the one
that tells you the quiet is the agent's.

The coordinator also gets a nudge when it stops maintaining the plan it wrote:
a twelve-item task once sat at 0/12 for two hours and snapped to 12/12 at the
end, with nothing broken except that the list was never touched.

### Smaller things

- Auto mode's delete gate asks git whether the target is repo content or the
  agent's own scratch, instead of matching destructive markers. Deleting a
  probe script it just wrote no longer stops the run; deleting anything git
  tracks still does.
- `/api/health` reports how many projects are onboarded, never which. The
  route has no session behind it, and a private repo's name is the one field
  in that payload that describes its owner rather than the process.
- A request arriving before the database pool is up answers `401` or `503`
  rather than `500`.
- CONTRIBUTING's "exactly what CI does" list is now asserted against
  `.github/workflows/ci.yml`, in both directions. It had drifted twice.
- The test suite no longer calls the live router. conftest's placeholder was
  the real router's address, which is a closed port on a contributor's laptop
  and the production router on the machine that runs it.
- A raw NUL byte in `useTaskStream.ts` made git treat the module as binary, so
  every diff of it read "Binary files differ" and no reviewer ever saw it
  change.

## v0.4.0 — a planner that keeps the brief, a Kimi seat for frontend work, costs as the router bills them (pre-release)

**2026-09-10**

Twenty-eight commits on top of v0.3.0. The headline is that a planning
turn no longer loses the request it was given, that frontend work can go
to a different model than everything else, and that the budget guard
counts what the router actually billed.

### Planning that keeps the brief, and searches before it reads

A hard plan on 2026-09-08 spent 68 model calls reading the repo before
writing a line; the context grew to 169k tokens, was compacted, and the
operator's request — the oldest thing in the window — went first. The
planner now works the other way round:

- **The brief comes first and stays pinned.** `save_brief` is the only
  tool a new session can call until a brief exists; the brief rides in
  the system message on every call after that, so compaction cannot touch
  it, and it persists with the session so follow-up turns do not re-force
  it. Saving the brief also matches the request against the project's
  skills and names the architecture skills to read before any file.
- **Search, then read the window a hit points to.** `search_project`
  (ripgrep, capped per file and overall) and `find_files` (the
  `.gitignore`-aware file list) against the real repo. Loop-proofed from
  the start: an identical search is answered from cache and refused on the
  third; zero hits come back with what was scanned and what to change; a
  per-turn search budget ends searching with "write the plan from what you
  have". Both are dials under **Settings → Runtime limits**.
- **The draft gate.** After N repo reads without a saved plan (default 50)
  file reads close with "save a draft now" and reopen once a plan is
  saved. A gate-forced save replies with what comes next — the budget has
  reset, take the open questions, finish the plan — rather than "the user
  can now use Build Now", which one planner read as "done". Paged reads
  return at least 500 lines whatever `limit` asks; eight reads of a
  1,100-line component become two, with no cooperation from the model.
- **What changed lately, and what may be stale.** The cartographer builds
  a model-free `recent-changes` skill (the newest 30 commits with their
  files) and keeps a freshness ledger: a memory fact that cites a file
  which changed after the fact was first seen is flagged, and the flags
  ride into both agents' memory blocks.
- **Pull requests.** With an optional fine-grained `GITHUB_TOKEN` the
  planner and coder can read a pull request — description, checks, review
  comments, diff — host-side and read-only. The sandbox never sees the
  token. "See PR 12 and fix the audit issues" is now a task.

### A frontend seat, pinned to Kimi

The operator wanted Kimi k3 on frontend work and DeepSeek everywhere
else. `agent/frontend_route.py` decides, with a reason: the choice on the
form beats everything; then the classifier's ui-styling category; then any
named backend path or keyword (a migration, a schema, an endpoint) routes
general ahead of any file count; then a two-thirds majority of frontend
paths; then two distinct keywords. Planning sessions decide on their first
message and stay put. On a frontend task the coordinator and the
investigator use `agent-coder-frontend`; the test-writer keeps its own
pin. The New Task form, Build Now and the new-session panel carry an
Auto / Frontend / General selector, and tasks and sessions show a route
badge with the reason on hover — silent routing is how an expensive run
happens. Both frontend seats are managed roles on the Models page.

### Costs as the router bills them

A planning turn was ended at "$8.09 spent against an $8.00 ceiling" when
OpenRouter had billed $1.72: the guard priced calls from token counts
against a rate table that was missing one model's cache-read discount,
and the 4.7x estimate was the last word. Every router row now carries the
proxy's call id and OpenRouter's billed cost; the budget tracker carries a
call at its estimate only until the router's line for that id appears,
and enforces the ceiling against the billed figure. Fallback rates come
from OpenRouter's live catalog, a pin the catalog does not list by name is
resolved through the per-model endpoints, and the table reloads when
`config.yaml` changes under a running agent. The Build Now popup and the
New Task form seed their budget from the Settings default instead of a
hard-coded $2.

### Loops end, and bad history never reaches a provider

- **A repeat-call guard on every tool.** The third identical call whose
  two predecessors matched the same result is answered from cache; the
  fourth and later are refused; after eight refusals in a row the pass
  ends with an escalation naming the looping tool, so the task can resume
  on a different seat instead of paying for forty refusals. A call whose
  result changes (a poll, a flaky test) is never blocked.
- **Malformed tool calls are stripped from every model request.** A
  truncated `write_todos` from one model was serialised as a tool call
  whose arguments were not JSON, and a stricter provider refused every
  later turn of that task — 37 times — while the fallback silently
  planned instead. The checkpoint is untouched; the request is cleaned.

### The gate and the deploy tell code problems from infrastructure

- **Pre-existing failures do not block.** A check that fails on the
  branch is re-run on a worktree at the base commit; one that fails there
  too is marked pre-existing, does not force NEEDS_FIXES, and is listed
  for the agent with an instruction not to chase it.
- **Deploy preflight.** A project can declare URLs that must answer before
  any build step runs. A failure is its own stage and escalates to a
  human with the dependency named, rather than being handed to the agent
  as a compile error. Found the hard way: a storefront prerender that
  reads the catalog from an API which had been dead for days.
- **Resume works without a top-up.** The resume panel only shows the
  budget field when a task is nearly out of money and sends zero
  otherwise; the endpoint rejected zero, so a task that stopped for any
  reason other than cost could not be resumed from the dashboard.

### A passkey gate for a public admin panel

`services/llm-router/auth-gate` puts a WebAuthn passkey in front of the
LiteLLM admin UI when it is on a public hostname: nginx consults it via
`auth_request`, bearer requests pass through for LiteLLM to judge,
everything else needs a session minted by a passkey ceremony. Sessions
are server-side SHA-256 hashes in a 0600 state file; enrolment is a
single-use 30-minute token. Opt-in, and configured entirely from `.env`.

### Fixes

- **Summarization fired before every model call.** The trigger OR'd a
  message-count clause that the token-sized keep window satisfied on its
  own after every compaction. Triggers are tokens-only now, and a test
  refuses any trigger the keep window can satisfy by itself.
- **A compaction could silence the live stream.** Both publishers tracked
  how many messages they had sent; after a compaction the list was
  shorter than that count and nothing was published until it regrew. A
  coder calling every 40 seconds read as "no movement for 20 minutes".
  Messages are tracked by identity, and a quiet tick sends a heartbeat
  the stall watchdog can see.
- **The plan strip snapped back on refresh** to the list frozen when the
  previous pass ended; the live mirror wins now.
- **Build Now was dead for a long plan.** The goal field was capped at
  20,000 characters and a plan came to 20,364; every click was a silent
  422. The cap is 80,000 and a failed start is shown beside the button.
- **The Build Now strip ran off the edge on a phone**, with the confirm
  button unreachable. It wraps.
- **ripgrep is a prerequisite.** CI installs it, the installer warns with
  the package name, and the search tests skip without it.
- Failure rows in the router log carry the exception text; the
  cartographer and both frontend seats have fallbacks.

### Known limits

- The preflight only checks what a project declares; a build step with an
  undeclared live dependency still fails as a build error.
- The half-open-socket gap for task streams noted in v0.3.0 remains.

### Requirements

Linux, Python 3.12+, Node 24+, Docker, PostgreSQL 14+, an OpenRouter API
key, and now **ripgrep** for the planner's search tools. pm2 optional.
**879 Python tests, 173 frontend tests** pass on this release.

## v0.3.0 — runtime limits in the console, one save bar everywhere, planning that stops for the right reasons (pre-release)

**2026-09-02**

Fourteen commits on top of v0.2.0. The headline is that the dials which
used to need a file edit and a restart are now in the console, and that a
planning turn is no longer killed for the crime of taking a while.

### Runtime limits, from the console

Fourteen limits were module constants and environment variables, so
retuning one meant editing a file and restarting — and a restart is
exactly what you cannot do while the thing you want to retune is running.
They live in the store now (**Settings → Runtime limits**, admin only):

    planning turn budget      planning stall timeout    default task budget
    model calls per run       tool calls per run        review wait timeout
    model call timeout        planning model timeout    lint timeout
    typecheck timeout         test suite timeout        review test timeout
    frontend build timeout    default shell timeout

Every one is read at the point of *use*, so a change lands on the next
turn or task and never mutates something already in flight. Values are
clamped to each knob's bounds rather than rejected; unknown names are
rejected, because a typo must not sit in the database looking like
configuration. Existing environment variables still seed the defaults.
Seconds render beside their plain-units equivalent (1200 → "20 min"), and
the label carries the full reasoning as a tooltip.

Worth knowing: `npm test` was capped at 180s, and a repo that chains
dozens of suites can exceed that. An abort reads to the agent as a
*failing* test rather than a slow one, so it would try to fix a suite that
was merely long. That cap is now a dial.

Deliberately not exposed: auto-approve and merge review. Those change what
the agent is *permitted* to do; these are dials on effort.

### One save bar, on both pages that have something to save

Every editable card on the settings page carried its own Save button — a
row of controls disabled almost all of the time, and an edit in a card you
had scrolled past was easy to abandon. There is now one bar, pinned
bottom-right, that appears only when something is genuinely dirty, names
the count, and offers Discard. On a phone it goes full width above the tab
bar rather than floating over it.

The **model configuration page** gets the same flow. Its static
"Save N changes" button sat at the very bottom of three groups of rows, so
a pin changed in the top group was just as easy to lose. Same bar, same
Discard, and a failed save is reported beside the button that caused it
with the edit kept.

The settings page itself was also re-laid: three cards per row where the
grid arithmetic had silently only ever allowed two, Telegram and Projects
paired into one row, spinner buttons gone from numeric fields where they
sat on top of the digits.

### Planning turns that end for the right reasons

- **Bounded by silence, not by the clock.** A planning turn was killed at
  30 minutes while actively streaming — $2.50 spent, no plan saved. A
  duration ceiling selects against exactly the work it should protect.
  The watchdog now fires only when a turn produces *nothing* for 20
  minutes (adjustable above); duration is unbounded on purpose, and the
  budget ceiling remains the only thing that stops work abruptly.
- **Why it stopped is recorded.** The reason used to be a live WebSocket
  event and nothing else — refresh and it was gone. Outcomes are now
  classified (`completed / stopped / stalled / budget / error`), stored
  with the session, and shown when you open one that ended badly, with the
  note that the context is still there and the turn can be continued.
- **The re-read loop.** One session read `src/core/bot.js` 129 times,
  spent its whole $8 ceiling and produced nothing: planning shared the
  build task's summarization window, which was too small to hold the four
  files the question spanned, so it dropped what it had just read and read
  it again. Planning has its own window now, plus a per-turn per-file read
  ledger that warns at 6 reads and refuses at 14.
- **Live cost, and a UI that notices the turn ended.** Cost read $0 for
  the whole turn because it was banked only at the end; it is mirrored as
  it accrues now. And a half-open socket left the browser showing thinking
  bubbles forever — a liveness watchdog treats 70s of silence as death and
  reconnects, with `running` read back from the server.

### Fixes

- **The investigator subagent was never used.** 281 coder calls, 207
  test-writer, zero investigator, across every task since the router
  rework. Its delegation was written as a preference ("prefer the
  investigator for multi-file research") and the coordinator, holding
  `rg` itself, always declined. It is a rule now, with a trigger it can
  evaluate. Prompt-only — worth re-checking the per-role counts.
- **The public demo went down on two 429s a minute apart.** Its model was
  pinned to a single provider with fallbacks off, the one alias in the
  config with no router-level fallback either. Three layers now:
  preferred provider, any other provider of the same model, then a cheap
  proven tool-caller if the model is unavailable everywhere.
- **The router exited at startup instead of degrading** when the database
  was connected, because LiteLLM shells out to the `prisma` CLI to check
  migrations and could not find it outside the venv. The pm2 config puts
  the venv on `PATH`.
- **The mail agent's model spiralled on reasoning.** One "can you see
  starred mail" turn spent 40K reasoning tokens and $0.72 producing
  nothing visible, since reasoning streams as empty chunks. Reasoning
  effort is pinned low for that role: a tool-driven mail agent needs quick
  tool calls, not deep chains.

### Known limits

The half-open-socket gap fixed for planning streams still exists for task
streams; a task's UI can show it running after the socket has died.

### Requirements

Unchanged: Linux, Python 3.12+, Node 24+, Docker, PostgreSQL 14+, and an
OpenRouter API key. pm2 optional. **740 Python tests, 168 frontend
tests** pass on this release.

## v0.2.0 — landing page, a frontend test suite, Node 24 (pre-release)

**2026-08-30**

Thirteen commits on top of v0.1.0. The headline is that the login screen
is no longer the front door, and the frontend is no longer untested.

### A public landing page

Anyone arriving from a shared link used to hit a password box for a
console they cannot enter. There is now a real page in front of it —
the pipeline, the review gate, the console, the controls the model cannot
override — built from the README's own copy, so the two cannot drift into
telling different stories. Sign-in moved to the top right; the link to the
source moved the other way, off the login card and onto the page where
someone who *cannot* sign in actually has somewhere to go.

The eight screenshots are the ones already in `docs/`, re-encoded to WebP:
2.4MB of PNG becomes 268KB, content-hashed by Vite so they inherit the
immutable cache year.

The page is indexable now (`noindex` dated from when this URL was nothing
but a password box), with a `meta description` and a `canonical` — the SPA
fallback answers 200 with the same shell for every path, so without one a
crawler can index an unbounded set of URLs that are all the same page.

### The frontend has tests

There was no test framework at all: every behaviour was guarded by nothing
but a typecheck. Adds vitest + Testing Library + jsdom, wired into CI
*ahead* of the build. **141 tests.** Branch coverage is 73% against 45% of
statements, and that gap is deliberate — the decision logic is covered;
the rest is chart and form markup where a test asserts little beyond
"React rendered".

Coverage is reported, not enforced. A threshold that fails CI on an
unrelated refactor teaches people to delete tests.

### Node 24

Node 20 left maintenance in April 2026, and its EOL had started to cost
something concrete: the test toolchain had to be pinned back a major
version each to keep supporting it. CI, the installer's prerequisite check
and the documented requirement all move to **24** together — CI testing
one version while the docs tell people to install another verifies
nothing.

**This is the one upgrade note.** If you are on Node 20, `install.sh` will
now refuse until you upgrade. Nothing else in this release requires action.

Re-verified end to end on the new floor: Debian 13 (Python 3.13.5, Node
24.20.0) against a real postgres:16 — `install.sh` completes, then 694
Python tests and 141 frontend tests pass in that same container.

### Fixes

- **Sidebar categories could not be collapsed while a task was running.**
  Expansion was `manuallyExpanded.has(cat) || searching || selectedCategory
  === cat`, a shape that can only ever *add* expansion — so clicking the
  header did nothing, with no indication why. Underneath it,
  `selectedCategory` resolved the category of a *running* task, which is
  filtered out of the category lists and shown in the Running group
  instead: selecting one opened a category it is not a member of.
- **A signed-out visitor was sent past the landing page to the login
  form**, because the opening `getMe()` 401 fired the session-expiry
  handler. An expired session still goes straight to the form.
- **The landing page's nav was declared sticky and never stuck.** The page
  carried `overflow-x: hidden` to stop horizontal scroll; setting one axis
  to `hidden` computes the other to `auto`, which makes the element a
  scroll container — and a scroll-container ancestor silently disables
  `position: sticky` on everything inside it. `overflow-x: clip` clips the
  same overflow without establishing one.
- **BalanceStrip crashed on a 200 with an unexpected body.** `!balance`
  passed for `{}`, then `.toFixed` threw — and it renders inside the
  Sidebar, so the ErrorBoundary blanked the whole console over a
  decorative credit strip.
- **`HEAD` returned 405 on every file at the dist root.** FastAPI's
  `@app.get` registers exactly the methods named, unlike Starlette's plain
  `Route`, which folds `HEAD` in — so a crawler sizing an `og:image`
  before fetching it got a 405.
- **Those files were served with `max-age=86400` and no way to
  revalidate.** They are `no-cache` now, with conditional requests
  answered properly: `FileResponse` sets etag and last-modified but never
  checks them, and only populates them when handed a `stat_result`, so
  both had to be wired up for a revalidation to cost a 304 rather than the
  whole file.
- A malformed WebSocket frame threw a bare `SyntaxError` out of
  `onmessage`. The stream survived either way, but the log said nothing
  about which socket produced it.
- The agent dashboard had `og:title` and `og:description` but no
  `og:image`, so its link preview was a `summary_large_image` with nothing
  to show.

### Requirements

Linux, Python 3.12+, **Node 24+**, Docker, PostgreSQL 14+, and an
OpenRouter API key. pm2 optional.

## v0.1.0 — first public release (pre-release)

**2026-08-29**

The first public cut of 3D-Agent. It has run continuously on one deployment for
several months against three real repositories of varying shape and size, but
this is the first time anyone else can install it. Treat it accordingly: see *Known limits* below.

### What it does

Give it a plain-English goal against a repository you've onboarded. It plans the
work, writes the code, runs that project's **real** test suite, and ships it —
with an independent review gate that must pass before anything merges.

- **Plan → build → verify → review → ship.** Every task runs in a git worktree
  of your live repo, on its own branch, inside a Docker container that can see
  only that worktree.
- **A second model reviews every diff** in an isolated checkout before a merge
  is possible, and optionally a human approves after that. Nothing merges on
  the agent's say-so.
- **Planning Chat** — a separate conversational mode for research and design
  that remembers what it learns and can hand a finished plan to the build
  pipeline.
- **Memory that compounds.** Completed tasks are consolidated into per-project
  memory; a cartographer keeps a structural map of each codebase current.
- **Budget ceilings per task**, so a runaway loop costs a known maximum.
- **Model routing by role.** Planner, coder, reviewer, summarizer and the rest
  are named aliases you repin from the dashboard, with live pricing, agentic
  benchmark standing and per-provider latency/uptime shown at the point of
  choice.

### Setting it up

- **`./install.sh`** takes a fresh clone to a running agent. It checks
  prerequisites, generates secrets in the formats the app actually requires,
  creates the database, builds the sandbox image and the dashboard, and is safe
  to re-run — which is also the upgrade path. `--dry-run` and `--yes` included.
- **Project onboarding from the dashboard** (Settings → Projects) or
  `scripts/add_project.py`. It inspects a directory, proposes a configuration,
  and asks you to confirm it.
- **[INSTALL.md](INSTALL.md)** is the full guide: requirements, configuration
  reference, troubleshooting for the real failure modes, and the update path.

### The safety posture, stated plainly

This runs a model that writes and executes code against your repositories. The
controls are the product, not an afterthought:

- Onboarding **proposes, you approve**. Anything that can't be verified arrives
  switched off with the reason attached. A test script that makes network calls
  is disabled by default — a suite that talks to a live service can *act* on
  production, and no static analysis distinguishes "hits a test server" from
  "hits your production system".
- Projects can only be onboarded from inside `AGENT_PROJECT_ROOTS`, judged after
  symlink resolution. The worktree location and project name are derived by the
  server, never accepted from a request.
- Check and build commands are matched against what the server itself proposed,
  so a client cannot introduce a command for the review or deploy service to
  run.
- Each project pushes with its **own deploy key**, scoped to one repository,
  wired to that repo's `core.sshCommand` alone.
- The agent cannot `git push` — that's on its blocked-command list.

See [SECURITY.md](SECURITY.md) for the full threat model, including which
capabilities are intended and which would be genuine vulnerabilities.

### Hardening before release

A full external review of the codebase ran before this tag; everything it
found at critical or high severity is fixed here, with a regression test each.
Two were serious enough to be worth naming:

- **Host code execution via git hooks.** Every git command runs on the host
  with its working directory inside the agent-writable worktree, and a
  worktree's `.git` is a pointer *file* living in that same writable tree.
  Rewriting it re-aimed git at an agent-controlled directory, hooks included,
  so the post-build commit executed agent-authored code as the server user.
  Hooks are now disabled on every invocation and the pointer is verified
  against server-owned config.
- **Sandbox mount escape.** The container's bind mounts were computed from
  state read out of the worktree — a `node_modules` symlink and the `.git`
  pointer — so the agent could choose what the host mounted into its own
  container (`ln -s /home node_modules` produced `-v /home:/home:ro`). Mount
  targets are now validated against the project's own paths.

Also fixed: alerts that ignored per-user repo scoping, a fail-open in user
creation, streams that dropped data on an unclean reconnect, three
"documented but broken" issues that would have hit the first fresh install,
and the absence of CI.

### Known limits

- **Pre-1.0, and field-tested on exactly one machine.** Expect rough edges on a
  different distro, Postgres version, or non-root install.
- **Linux host required.** The sandbox is Docker; there is no Windows path.
- **Remote access needs HTTPS or an SSH tunnel.** The session cookie is
  `Secure`, so plain HTTP works only on `localhost`/`127.0.0.1`. A LAN or VPN
  address over plain HTTP will drop the cookie and bounce you back to the login
  page. `install.sh` can set up nginx + Let's Encrypt for a domain.
- **Stack detection covers npm/pnpm/yarn and basic Python.** Go, Rust and Ruby
  projects onboard fine but arrive with no checks detected — you add commands by
  hand.
- **The sandbox image ships Node and Python only.** A project needing another
  toolchain needs the image extended.
- **No migrations story yet.** Upgrades are `git pull` plus `./install.sh`; the
  schema is created on first start and has not needed a migration path so far.
- **Costs real money.** Every task calls a paid model. Set
  `DEFAULT_BUDGET_USD` deliberately.

### Requirements

Linux, Python 3.12+, Node 20+, Docker, PostgreSQL 14+, and an OpenRouter API key
(the only paid dependency). pm2 optional.

### License

PolyForm Noncommercial 1.0.0 — source-available, not open source. Free for any
noncommercial use; commercial use needs a separate licence.
