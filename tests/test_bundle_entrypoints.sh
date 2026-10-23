#!/usr/bin/env bash
# The bundle's three entrypoints, run for real with their last `exec` and
# the privileged tools stubbed. What they decide before that exec is what
# a fresh install, an upgrade and a rotation depend on:
#
#   * init-secrets.sh writes one secret per volume (the sandbox launcher's
#     key included), carries an upgrade's values over from the old shared
#     volume, and refuses to hand the agent a password the database was not
#     initialised with;
#   * the agent's entrypoint moves the two files the review services read
#     into the shared volume, survives an empty environment under `set -u`,
#     never calls docker (it has no socket), and drops to PUID/PGID with no
#     supplementary groups;
#   * the reviewer's entrypoint drops to PUID/PGID, or stays put unset.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
ROOT="$PWD"
T="$(mktemp -d)"; trap 'rm -rf "$T"' EXIT

pass=0; fail=0
ok()   { pass=$((pass+1)); echo "  ok   $1"; }
bad()  { fail=$((fail+1)); echo "  FAIL $1"; echo "       stderr: $(tail -c 400 "$T/err" 2>/dev/null | tr '\n' '|')"; echo "       calls:  $(tail -n 3 "$T/calls" 2>/dev/null | tr '\n' '|')"; }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

# Stubs on PATH. Each records its arguments; setpriv and the entrypoints'
# real targets (docker-entrypoint.sh, uvicorn, node) just record and stop.
mkdir -p "$T/bin"
for stub in docker-entrypoint.sh uvicorn setpriv chown useradd groupadd docker node; do
    printf '#!/bin/sh\necho "%s $*" >> "%s/calls"\n' "$stub" "$T" > "$T/bin/$stub"
    chmod +x "$T/bin/$stub"
done
# psql records its stdin too: that is where the SQL goes (init-secrets.sh
# --rotate; psql does not interpolate variables in -c).
printf '#!/bin/sh\necho "psql $* <<$(cat)" >> "%s/calls"\n' "$T" > "$T/bin/psql"
chmod +x "$T/bin/psql"
# The entrypoints drop privileges only when they ARE root (`id -u`); the
# suite runs unprivileged on CI, so the question is answered as root here
# and the drop itself is a stubbed setpriv. Everything else id is asked
# goes to the real one.
printf '#!/bin/sh\n[ "$1" = -u ] && [ $# = 1 ] && { echo 0; exit 0; }\nexec /usr/bin/id "$@"\n' > "$T/bin/id"
chmod +x "$T/bin/id"
PY="$(command -v python3)"
ln -s "$PY" "$T/bin/python"
export PATH="$T/bin:$PATH"
calls() { cat "$T/calls" 2>/dev/null || true; }
reset() { rm -rf "$T/v"; mkdir -p "$T/v"; : > "$T/calls"; }

# ---------------------------------------------------------------------------
echo "init-secrets.sh"
INIT="$ROOT/docker/postgres/init-secrets.sh"
run_init() {   # env... -- runs the wrapper in a clean environment
    env -i PATH="$PATH" TEKTONIX_PG_SECRET_DIR="$T/v/pg" TEKTONIX_ROUTER_KEY_DIR="$T/v/rk" TEKTONIX_LAUNCHER_KEY_DIR="$T/v/lk" \
        TEKTONIX_LEGACY_SECRETS_DIR="$T/v/legacy" PGDATA="$T/v/pgdata" "$@" sh "$INIT" postgres >/dev/null 2>"$T/err"
}

reset
run_init
check "a fresh install gets a generated password and key, one per volume" \
    '[[ -s "$T/v/pg/postgres_password" && -s "$T/v/rk/model_router_key" && "$(cat "$T/v/rk/model_router_key")" == sk-* ]]'
check "the password is not written next to the key" '[[ ! -e "$T/v/rk/postgres_password" && ! -e "$T/v/pg/model_router_key" ]]'
check "the sandbox launcher's key is generated in its own volume" \
    '[[ "$(cat "$T/v/lk/launcher_key")" =~ ^[0-9a-f]{64}$ && ! -e "$T/v/lk/model_router_key" && ! -e "$T/v/rk/launcher_key" ]]'
check "the files are readable by the other containers' users" '[[ "$(stat -c %a "$T/v/pg/postgres_password")" == 444 ]]'
check "then postgres starts with the password file, not the value" 'grep -q "^docker-entrypoint.sh postgres" "$T/calls"'
first="$(cat "$T/v/pg/postgres_password")"
run_init
lkey="$(cat "$T/v/lk/launcher_key")"
check "a restart keeps them" '[[ "$(cat "$T/v/pg/postgres_password")" == "$first" && "$(cat "$T/v/lk/launcher_key")" == "$lkey" ]]'
rm -f "$T/v/lk/launcher_key"
run_init
check "a lost launcher key is generated again" '[[ -s "$T/v/lk/launcher_key" && "$(cat "$T/v/lk/launcher_key")" != "$lkey" ]]'

reset
mkdir -p "$T/v/legacy"; printf 'old-pw' > "$T/v/legacy/postgres_password"; printf 'sk-old' > "$T/v/legacy/model_router_key"
mkdir -p "$T/v/pgdata"; echo 16 > "$T/v/pgdata/PG_VERSION"
run_init
check "an upgrade carries both secrets over from the old shared volume" \
    '[[ "$(cat "$T/v/pg/postgres_password")" == old-pw && "$(cat "$T/v/rk/model_router_key")" == sk-old ]]'

reset
mkdir -p "$T/v/pgdata"; echo 16 > "$T/v/pgdata/PG_VERSION"
run_init
check "an initialised database with no stored password assumes the old default and says so" \
    '[[ "$(cat "$T/v/pg/postgres_password")" == agent ]] && grep -q "secret volume was lost" "$T/err"'

reset
run_init POSTGRES_PASSWORD=mine MODEL_ROUTER_KEY=sk-mine
check "on a fresh database .env values win" \
    '[[ "$(cat "$T/v/pg/postgres_password")" == mine && "$(cat "$T/v/rk/model_router_key")" == sk-mine ]]'
mkdir -p "$T/v/pgdata"; echo 16 > "$T/v/pgdata/PG_VERSION"
run_init POSTGRES_PASSWORD=changed-later
check "a changed POSTGRES_PASSWORD after first init is not written for the agent" '[[ "$(cat "$T/v/pg/postgres_password")" == mine ]]'
check "and the log says how to rotate" 'grep -q -- "--rotate" "$T/err"'
run_init POSTGRES_PASSWORD=mine
check "the same value as stored is not a rotation" '! grep -q -- "--rotate" "$T/err"'
env -i PATH="$PATH" TEKTONIX_PG_SECRET_DIR="$T/v/pg" TEKTONIX_ROUTER_KEY_DIR="$T/v/rk" POSTGRES_PASSWORD=changed-later POSTGRES_DB=tektonix sh "$INIT" --rotate </dev/null >/dev/null 2>"$T/err"
want="<<ALTER USER \"agent\" PASSWORD :'pw'"
check "--rotate alters the role with the value as a psql variable and rewrites the file" \
    '[[ "$(cat "$T/v/pg/postgres_password")" == changed-later ]] && grep -q "^psql .*-v pw=changed-later " "$T/calls" && grep -qF -- "$want" "$T/calls" && ! grep -q "PASSWORD .changed-later" "$T/calls"'

reset
run_init MODEL_ROUTER_KEY=sk-local-dev
check "the old published default key is treated as unset" '[[ "$(cat "$T/v/rk/model_router_key")" != sk-local-dev ]]'

# ---------------------------------------------------------------------------
echo "agent entrypoint"
AGENT="$ROOT/docker/agent/entrypoint.sh"
run_agent() {   # env... -- an empty environment but for what is given
    env -i PATH="$PATH" HOME="$T" "$@" sh "$AGENT" >/dev/null 2>"$T/err"
}
# The entrypoint's paths are fixed at /app/...; run it in a scratch root.
# A relative /app would need a chroot, so instead the script is copied with
# /app rewritten to the scratch directory -- the only edit made to it.
mkdir -p "$T/app"
sed "s|/app|$T/app|g" "$AGENT" > "$T/agent-entrypoint.sh"
AGENT="$T/agent-entrypoint.sh"

rm -rf "$T/app"; mkdir -p "$T/app"; : > "$T/calls"
run_agent
check "an empty environment (set -u) gets through to the server" 'grep -q "^uvicorn agent.server:app" "$T/calls"'
check "and never calls docker: the sandbox launcher builds the sandbox image" '! grep -q "^docker " "$T/calls"'
check "the secrets are generated in the data directory when there is no shared one" \
    '[[ -s "$T/app/data/auth_secret_key" && -s "$T/app/data/review_control_secret" ]]'
check "and projects.json is created where AGENT_PROJECTS_JSON says" \
    '[[ ! -e "$T/app/shared" ]]'

rm -rf "$T/app"; mkdir -p "$T/app/data"; : > "$T/calls"
printf 'rcs-before' > "$T/app/data/review_control_secret"; printf '{"projects":{"x":{}}}' > "$T/app/data/projects.json"
printf 'ask' > "$T/app/data/auth_secret_key"
run_agent TEKTONIX_SHARED_DIR="$T/app/shared" AGENT_PROJECTS_JSON="$T/app/shared/projects.json"
check "an upgrade moves the review secret and projects.json to the shared volume" \
    '[[ "$(cat "$T/app/shared/review_control_secret")" == rcs-before && "$(cat "$T/app/shared/projects.json")" == *'"'"'"x"'"'"'* ]]'
check "and they are gone from the data volume, which the review services no longer mount" \
    '[[ ! -e "$T/app/data/review_control_secret" && ! -e "$T/app/data/projects.json" ]]'
check "AUTH_SECRET_KEY stays in the data volume" '[[ "$(cat "$T/app/data/auth_secret_key")" == ask ]]'
check "it still runs as root when PUID is unset" '! grep -q "^setpriv" "$T/calls"'

rm -rf "$T/app"; mkdir -p "$T/app/data"; : > "$T/calls"
# A socket where the old compose file mounted one: an upgrade that kept it
# must not hand its group to the server.
"$PY" -c "import socket,sys; s=socket.socket(socket.AF_UNIX); s.bind(sys.argv[1])" "$T/docker.sock"
run_agent PUID=4321 PGID=4322 DOCKER_SOCKET="$T/docker.sock" TEKTONIX_SHARED_DIR="$T/app/shared"
check "with PUID/PGID the server runs as that user with no supplementary groups, socket or not" \
    'grep -q "^setpriv --reuid=4321 --regid=4322 --clear-groups uvicorn agent.server:app" "$T/calls"'
check "the user is created when the uid is unknown" 'grep -q "^useradd -u 4321 -g 4322" "$T/calls" && grep -q "^groupadd -g 4322" "$T/calls"'
check "the volumes it writes are made its own first" \
    'grep -q "^chown -R 4321:4322 $T/app/data" "$T/calls" && grep -q "^chown -R 4321:4322 $T/app/shared" "$T/calls"'

# ---------------------------------------------------------------------------
echo "reviewer entrypoint"
# Its /app paths (the credential copies it chowns) point into the scratch
# tree, as the agent entrypoint's do: on CI nothing may write under /app.
sed "s|/app|$T/app|g" "$ROOT/docker/reviewer/entrypoint.sh" > "$T/reviewer-entrypoint.sh"
REVIEWER="$T/reviewer-entrypoint.sh"
: > "$T/calls"
env -i PATH="$PATH" sh "$REVIEWER" node services/agent-review/server.js
check "unset, the command runs as is" '[[ "$(calls)" == "node services/agent-review/server.js" ]]'
: > "$T/calls"
env -i PATH="$PATH" PUID=4321 PGID=4322 REVIEW_STATE_DIR="$T/rs" sh "$REVIEWER" node services/commit-reviewer/reviewer.js
check "with PUID/PGID the command runs as that user" 'grep -q "^setpriv --reuid=4321 --regid=4322 --clear-groups node services/commit-reviewer/reviewer.js" "$T/calls"'
check "after the verdict volume is made its own" 'grep -q "^chown -R 4321:4322 $T/rs " "$T/calls"'

echo "$pass passed, $fail failed"
[[ $fail == 0 ]]
