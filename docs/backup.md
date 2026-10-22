# Backup and restore

One Postgres database holds everything that cannot be rebuilt from this repo:

- every task and its checkpoints, which is what makes a task resumable
- `agent_history_fts`, the searchable index of episodes, tasks and build
  transcripts. Worth knowing that it is not only an index: records are
  DEMOTED rather than deleted when the nightly pruner removes them from the
  store, so it is the only remaining copy of what was pruned. It can be
  rebuilt from whatever the store still holds
  (`scripts/backfill_history_index.py`) — which is not the same thing
- planning sessions and their plans
- project memory, org memory and the episodes consolidation feeds on
- runtime limits, the GitHub inbox and its **encrypted** tokens
- user accounts, sessions, 2FA secrets and recovery codes

Lose it and the box still runs. It just has no history, no memory and no
accounts.

> **The dump is not enough on its own.** `AUTH_SECRET_KEY` in `.env` is what
> decrypts the TOTP secrets and the stored GitHub tokens. A restore onto a box
> with a different key comes back with unreadable secrets and every 2FA user
> locked out. Keep `.env` with the dump, or at minimum keep that one value
> somewhere you trust and separate.
>
> **`keys/` belongs with it.** Two different things live there and both fail
> silently if they are missing or replaced:
>
> * `keys/vapid.json` is the keypair every web-push subscription was issued
>   against. A restore with a different one does not error anywhere — the rows
>   in `agent_push_subscriptions` still look fine, every send is simply
>   rejected, and the first anyone knows is an escalation nobody was told
>   about. There is no rotation path: if it is genuinely lost, delete the
>   subscription rows too so each device re-registers instead of sitting on an
>   endpoint that can never work again.
> * `keys/<project>.key` are the per-project deploy keys. Without them the
>   post-merge push fails and the repo's `core.sshCommand` points at nothing.
>
> `archives/` is optional but unrecoverable: it is what removed projects knew
> (`docs/architecture.md` §5). Nothing else holds a copy.

---

## Taking a backup

```bash
cd /home/tektonix
./scripts/backup.sh                 # writes backups/agent-<UTC timestamp>.dump
./scripts/backup.sh /mnt/elsewhere  # or somewhere off this box
```

It uses `pg_dump --format=custom`, which is compressed and restorable table by
table. The script refuses to call the result a backup unless the dump lists at
least five tables, so a truncated or half-written file fails now rather than on
the day you need it. It keeps the last 14 by default (`BACKUP_KEEP`).

Nightly, next to the existing jobs:

```
30 3 * * * /home/tektonix/scripts/backup.sh >> /home/tektonix/data/backup.log 2>&1
```

**Put a copy somewhere else.** A dump sitting on the same disk as the database
survives exactly the failures that do not matter.

---

## Proving the backup works

A dump nobody has restored is a hope. This restores one into a scratch
database, checks the rows are really there, and drops it again. It never
writes to the live database:

```bash
./scripts/verify_backup_restore.sh              # newest dump
./scripts/verify_backup_restore.sh backups/agent-20260911T173239Z.dump
```

Real output from this box, on an 811 MB database:

```
verifying agent-20260911T173239Z.dump -> scratch database restore_check_20260911173350
  ok    task checkpoints: 74222
  ok    checkpoint payloads: 37748
  ok    store rows (memory, sessions, settings, inbox): 258
  ok    user accounts: 2
  ok    settings in the store: 2
  ok    GitHub settings restored (encrypted with AUTH_SECRET_KEY -- keep .env with the dump)
restore verified: agent-20260911T173239Z.dump is usable
```

Roughly a minute to dump, forty seconds to restore, at that size.

The scratch database is created by the local postgres superuser over peer
auth, because the agent's own role usually cannot `CREATE DATABASE`. That is
the only step that needs more privilege than the agent itself has.

---

## Restoring for real

Onto a box where the agent is **not running** — a restore into a database the
agent is using will fight it.

```bash
# 1. stop everything that writes
pm2 stop tektonix commit-reviewer agent-review

# 2. restore into a fresh database, then point .env at it
sudo -u postgres psql -c 'CREATE DATABASE langgraph_agent_restored OWNER langgraph_agent'

#    Extensions first, as the superuser. The dump contains
#    `CREATE EXTENSION vector` (pgvector, for semantic episode search) and
#    pgvector is not a "trusted" extension, so the application role cannot
#    create it -- with --exit-on-error that one statement fails the entire
#    restore. Skip this step on a database that never had the extension.
sudo -u postgres psql -d langgraph_agent_restored -c 'CREATE EXTENSION IF NOT EXISTS vector'

#    Then restore everything EXCEPT the extension objects, which now exist and
#    belong to the superuser -- restoring them as the application role fails on
#    `COMMENT ON EXTENSION`. scripts/verify_backup_restore.sh does exactly this.
pg_restore -l backups/agent-<stamp>.dump \
  | grep -v -E '(^|; )[0-9]+ [0-9]+ (EXTENSION|COMMENT - EXTENSION)' > /tmp/restore.list
pg_restore --dbname="postgresql://langgraph_agent:...@localhost:5432/langgraph_agent_restored" \
           --no-owner --exit-on-error -L /tmp/restore.list backups/agent-<stamp>.dump

# 3. edit .env: LANGGRAPH_PG_DSN -> ...langgraph_agent_restored
#    and make sure AUTH_SECRET_KEY is the one that went with this dump

# 4. start the agent and check it can actually see the data
pm2 start tektonix
curl -s 127.0.0.1:8100/api/health | python3 -m json.tool
pm2 start agent-review commit-reviewer
```

Restoring into a **new** database rather than over the old one means the
original is still there if the restore turns out to be the wrong dump.

### After a restore, check these by hand

- **Log in.** If 2FA rejects your code, `AUTH_SECRET_KEY` does not match the
  one the dump was taken with. Stop and find the right key; there is no way
  around it.
- **Settings → GitHub → Test** on each token. A token that fails to decrypt is
  the same key mismatch.
- **Open a recent task.** Its stream, plan and diff come from the checkpoints.
- **The projects themselves are not in the dump.** They are git checkouts on
  disk (`/home/<project>`), plus `projects.json`, deploy keys under `keys/`,
  and `services/commit-reviewer/review-secrets/`. Those need their own copy —
  the database knows a project by name, not by content.

---

## The compose bundle

Postgres is not published to the host there, and its password lives in a
volume, so the script runs the dump inside the container:

```bash
./scripts/backup.sh --bundle                 # backups/agent-<stamp>.dump
                                             # backups/agent-<stamp>-volumes.tgz
```

The tarball is the part of the bundle the dump is useless without, taken
from the agent container: `/app/data` (`AUTH_SECRET_KEY`, the encrypted
first password), `/app/router-config` (the model pins), `/app/shared`
(`projects.json`, the review-control secret) and the two generated secrets
(`/run/tektonix-pg`, `/run/tektonix-router`). Nothing in `.env` is read.
The projects themselves are in `PROJECTS_DIR` on the host and are not in
either file. The same nightly line works, with `--bundle`.

**Restoring the bundle**, with the agent stopped and the database restored
beside the old one. On a new machine, `docker compose up -d` once first, so
the volumes and an empty database exist.

```bash
# 1. stop everything that writes, and the router, which reads its key at
#    start; postgres stays up
docker compose stop agent agent-review commit-reviewer router

# 2. a fresh database next to the old one, then the dump into it. If the
#    dump lists an EXTENSION (pg_restore --list), create it first as in
#    step 2 above; the bundle's image has no pgvector and never made one.
docker compose exec -T postgres createdb -U agent tektonix_restored
docker compose exec -T postgres pg_restore -U agent -d tektonix_restored \
    --no-owner --exit-on-error < backups/agent-<stamp>.dump

# 3. the volumes, from the tarball: the agent's own through the agent, the
#    two secrets through postgres (the agent mounts them read-only).
#    --pull never: the agent's pull_policy is build, and `run` would
#    otherwise rebuild it first (or fail, in the desktop app's stack)
docker compose run --rm -T --no-deps --pull never --entrypoint tar agent xzf - -C / \
    app/data app/router-config app/shared < backups/agent-<stamp>-volumes.tgz
docker compose exec -T postgres tar xzf - -C / \
    run/tektonix-pg run/tektonix-router < backups/agent-<stamp>-volumes.tgz

# 4. the database role takes the restored password. A new machine's
#    database was initialised with a password of its own, which the file
#    from step 3 just replaced; on the same machine this changes nothing.
docker compose exec -T postgres sh -c \
    'POSTGRES_PASSWORD="$(cat /run/tektonix-pg/postgres_password)" sh /tektonix/init-secrets.sh --rotate'

# 5. point the agent at the restored database and start it
#    (.env: POSTGRES_DB=tektonix_restored), then check it sees the data
docker compose up -d
curl -s 127.0.0.1:8100/api/health | python3 -m json.tool
```

Restoring the password file matters even when the database was never lost:
`pgdata` outlives a lost `pgsecret`, and then the agent has no way to open
it. The checks in "After a restore" apply here too.
`tests/test_backup_bundle.sh` runs this block, with docker stubbed, on
every CI run.

---

## The desktop app

The app runs the same bundle (compose project `tektonix`) from a directory
of its own, which holds its compose file and its `.env` with your
OpenRouter key and settings:

| | |
|---|---|
| Linux | `~/.local/share/io.tektonix.desktop/stack` |
| Windows | `%LOCALAPPDATA%\io.tektonix.desktop\stack` |

Everything above for the bundle applies, run from that directory. With the
stack started, from a checkout of this repository (on Windows, in Git Bash
or WSL):

```bash
AGENT_HOME="$HOME/.local/share/io.tektonix.desktop/stack" \
    scripts/backup.sh --bundle ~/persei-backups
```

Keep a copy of that directory's `.env` beside the two files: the dump and
the tarball do not hold the OpenRouter key. To restore onto a new machine,
install the app and finish its setup once, so the stack and its volumes
exist; Stop it from the panel, put the `.env` back, start the database
alone with `docker compose up -d --no-build postgres`, and follow
"Restoring the bundle" from that directory through step 4. Step 5 is Start
in the panel: the app's images are pulled, not built, and a plain
`docker compose up` would try to build them. The projects folder is yours
on disk, as in the bundle.

---

## What is not backed up here

| Not in the dump | Where it lives | What to do |
|---|---|---|
| The projects' code | `/home/<project>` git checkouts; `PROJECTS_DIR` in the bundle | they have a remote; that is the backup |
| `projects.json`, deploy keys, review secrets | on disk in the install; the `reviewshared` volume in the bundle | copy them with `.env` (`--bundle` tars them) |
| Router config and pins | `services/model-router/config.yaml`; the `routerconfig` volume in the bundle | **not in git** — gitignored since the Models page rewrites it on every repin. Keep a copy (`--bundle` tars it); `config.example.yaml` is only a starting point |
| Logs (`routing.jsonl`, pm2 logs) | on disk | rotate them; they are evidence, not state |
