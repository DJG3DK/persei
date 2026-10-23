#!/bin/sh
# The bundle's generated secrets, written before Postgres starts.
#
# The database password and the model router's key used to be compose
# defaults -- `agent` and `sk-local-dev` -- identical in every installation
# and printed in the example .env. This writes a random one of each on first
# boot, and every service that needs one reads it from a file
# (POSTGRES_PASSWORD_FILE, MODEL_ROUTER_KEY_FILE).
#
# One volume per secret, mounted only where that secret is read: the
# password in `pgsecret` (the agent), the key in `routerkey` (the router,
# the agent, the commit reviewer), and the sandbox launcher's key in
# `launcherkey` (the launcher and the agent). Both used to sit in one `bundlesecrets`
# volume that every consumer mounted whole, so the router and the reviewer
# could read the database password. That volume is still mounted here, and
# an upgrade's values are carried over from it on the first start.
#
# It runs here because postgres is the first service to start: everything
# else waits for it to be healthy, so the files exist before anyone reads
# them.
#
# An operator's own value in .env still wins on a fresh database -- except
# `sk-local-dev`, the old published default, which is treated as unset.
# On an initialised database a changed POSTGRES_PASSWORD is NOT applied:
# the image reads it on first init only, so writing it to the file the
# agent reads would give the agent a password the database does not have.
# Rotate with `--rotate` (below) instead.
#
#   docker compose exec postgres sh /tektonix/init-secrets.sh --rotate
#
# changes the role's password to the POSTGRES_PASSWORD the container was
# started with and rewrites the file; then `docker compose restart agent`.
set -eu

pgdir=${TEKTONIX_PG_SECRET_DIR:-/run/tektonix-pg}
rkdir=${TEKTONIX_ROUTER_KEY_DIR:-/run/tektonix-router}
lkdir=${TEKTONIX_LAUNCHER_KEY_DIR:-}
legacy=${TEKTONIX_LEGACY_SECRETS_DIR:-/run/tektonix-secrets}
pgdata=${PGDATA:-/var/lib/postgresql/data}
mkdir -p "$pgdir" "$rkdir"
umask 022

rand() {
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
}

write() {   # dir name value
    # 0444: the readers do not run as the postgres user.
    printf '%s' "$3" > "$1/$2.tmp"
    chmod 0444 "$1/$2.tmp"
    mv "$1/$2.tmp" "$1/$2"
}

if [ "${1:-}" = "--rotate" ]; then
    : "${POSTGRES_PASSWORD:?set POSTGRES_PASSWORD in .env and recreate the postgres container first}"
    # Over the socket, as the bootstrap superuser; the value goes in as a
    # psql variable, never into the SQL text. On stdin: psql does not
    # interpolate variables in -c, so until 2026-10-22 this sent the literal
    # :'pw' and every rotation failed with a syntax error.
    printf '%s\n' "ALTER USER \"${POSTGRES_USER:-agent}\" PASSWORD :'pw'" \
        | psql -v ON_ERROR_STOP=1 -v pw="$POSTGRES_PASSWORD" -U "${POSTGRES_USER:-agent}" -d "${POSTGRES_DB:-postgres}" -q
    write "$pgdir" postgres_password "$POSTGRES_PASSWORD"
    echo "[init-secrets] rotated the database password; restart the agent to pick it up"
    exit 0
fi

carry_over() {   # name dir -- the pre-2026-09-29 location, copied once
    if [ ! -s "$2/$1" ] && [ -s "$legacy/$1" ]; then
        write "$2" "$1" "$(cat "$legacy/$1")"
        echo "[init-secrets] carried $1 over from the shared secrets volume"
    fi
}
carry_over postgres_password "$pgdir"
carry_over model_router_key "$rkdir"

initialised=0
[ -s "$pgdata/PG_VERSION" ] && initialised=1

if [ -n "${POSTGRES_PASSWORD:-}" ]; then
    if [ "$initialised" = 1 ] && [ -s "$pgdir/postgres_password" ] \
        && [ "$(cat "$pgdir/postgres_password")" != "$POSTGRES_PASSWORD" ]; then
        echo "[init-secrets] POSTGRES_PASSWORD in .env is not the password this database was initialised with." >&2
        echo "[init-secrets] Postgres applies it on first init only, so it is NOT written for the agent to use;" >&2
        echo "[init-secrets] the database and the agent keep the old one. To rotate:" >&2
        echo "[init-secrets]   docker compose exec postgres sh /tektonix/init-secrets.sh --rotate && docker compose restart agent" >&2
    else
        write "$pgdir" postgres_password "$POSTGRES_PASSWORD"
    fi
elif [ ! -s "$pgdir/postgres_password" ]; then
    if [ "$initialised" = 1 ]; then
        # A database initialised before this script existed was created with
        # the old default. Keep it working; say how to rotate it. The other
        # way here is a lost secret volume beside a surviving database, and
        # then the guess is wrong: say that too.
        write "$pgdir" postgres_password agent
        echo "[init-secrets] existing database and no stored password: assuming the old default 'agent'." >&2
        echo "[init-secrets] If this database was created after 2026-09-28, the secret volume was lost; restore it" >&2
        echo "[init-secrets] from a backup, or set POSTGRES_PASSWORD in .env and run this script with --rotate." >&2
    else
        write "$pgdir" postgres_password "$(rand)"
        echo "[init-secrets] generated a database password in the pgsecret volume"
    fi
fi

if [ -n "${MODEL_ROUTER_KEY:-}" ] && [ "$MODEL_ROUTER_KEY" != "sk-local-dev" ]; then
    write "$rkdir" model_router_key "$MODEL_ROUTER_KEY"
elif [ ! -s "$rkdir/model_router_key" ]; then
    write "$rkdir" model_router_key "sk-$(rand)"
    echo "[init-secrets] generated a model router key in the routerkey volume"
fi

# The sandbox launcher's key: the only thing between the agent's network and
# the one service that holds the Docker socket. Nobody chooses it, so there
# is no .env value to honour; a lost one is simply generated again, and the
# launcher and the agent read it at their next start.
if [ -n "$lkdir" ]; then
    mkdir -p "$lkdir"
    if [ ! -s "$lkdir/launcher_key" ]; then
        write "$lkdir" launcher_key "$(rand)"
        echo "[init-secrets] generated a sandbox launcher key in the launcherkey volume"
    fi
fi

unset POSTGRES_PASSWORD MODEL_ROUTER_KEY
export POSTGRES_PASSWORD_FILE="$pgdir/postgres_password"
exec docker-entrypoint.sh "$@"
