#!/usr/bin/env bash
#
# Persei installer.
#
# Asks for what it cannot derive, derives everything else, and shows you each
# step before it runs. Safe to re-run: every step checks whether it already
# happened, and it never overwrites an existing .env or projects.json.
#
#   ./install.sh                 interactive (recommended)
#   ./install.sh --dry-run       show what would happen, change nothing
#   ./install.sh --yes           non-interactive; reads answers from the
#                                environment (see --help)
#
# It can install the prerequisites it finds missing, but it always asks first
# and the answer defaults to no. --yes does not count as permission for that:
# "do not stop to ask me" should never quietly mean "put a Node runtime and a
# database on this machine". INSTALL_PREREQS=1 is the explicit opt-in.
#
set -uo pipefail

AGENT_HOME="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$AGENT_HOME" || exit 1

DRY_RUN=0
ASSUME_YES=0

# --- output -----------------------------------------------------------------
if [ -t 1 ]; then
    B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[31m'; G=$'\033[32m'; Y=$'\033[33m'; N=$'\033[0m'
else
    B=""; DIM=""; R=""; G=""; Y=""; N=""
fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s==>%s %s%s\n' "$B" "$N" "$B" "$*$N"; }
ok()   { printf '  %s✓%s %s\n' "$G" "$N" "$*"; }
warn() { printf '  %s!%s %s\n' "$Y" "$N" "$*"; }
die()  { printf '\n%serror:%s %s\n' "$R" "$N" "$*" >&2; exit 1; }
note() { printf '    %s%s%s\n' "$DIM" "$*" "$N"; }

usage() {
    cat <<'EOF'
Persei installer

  ./install.sh [--dry-run] [--yes] [--help]

  --dry-run   Print every action without performing it.
  --yes       Non-interactive. Answers come from the environment:
                INSTALL_PREREQS=1   allow installing missing prerequisites
                                    (git, python3, node, docker, ripgrep,
                                     postgresql) with the system package
                                     manager. Without it an unattended run
                                     installs nothing and stops instead, and
                                     an interactive run asks first either way.
                PG_DSN              Postgres DSN (required)
                OPENROUTER_API_KEY  OpenRouter key (required)
                ADMIN_EMAIL         first admin account (default admin@example.com)
                SKIP_DOCKER=1       don't build the sandbox image
                SKIP_FRONTEND=1     don't build the dashboard bundle
                AGENT_DOMAIN        set up nginx + TLS for this domain
                                    (empty or unset = skip; access over an
                                     SSH tunnel instead)
                LETSENCRYPT_EMAIL   address Let's Encrypt sends expiry warnings to
EOF
}

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --yes|-y)  ASSUME_YES=1 ;;
        --help|-h) usage; exit 0 ;;
        *) die "unknown option: $arg (try --help)" ;;
    esac
done

run() {
    if [ "$DRY_RUN" = "1" ]; then
        note "would run: $*"
        return 0
    fi
    "$@"
}

# Ask a question with a default. In --yes mode the default is taken silently,
# which is why every REQUIRED answer is validated separately rather than
# defaulted to something wrong.
ask() {
    local prompt="$1" default="${2:-}" reply
    if [ "$ASSUME_YES" = "1" ]; then
        printf '%s' "$default"
        return
    fi
    # Same as confirm: without a terminal the read cannot happen, and the
    # default is the honest answer rather than a shell error.
    if ! { exec 3</dev/tty; } 2>/dev/null; then
        printf '%s' "$default"
        return
    fi
    if [ -n "$default" ]; then
        read -r -p "  $prompt [$default]: " reply <&3
        printf '%s' "${reply:-$default}"
    else
        read -r -p "  $prompt: " reply <&3
        printf '%s' "${reply:-}"
    fi
    exec 3<&-
}

# Reads from /dev/tty, not stdin, so `curl ... | bash` still asks the person
# instead of consuming the script it is being fed. With no controlling
# terminal at all there is nobody to ask: say so and answer no, rather than
# letting the failed read look like a decision somebody made.
confirm() {
    local prompt="$1"
    [ "$ASSUME_YES" = "1" ] && return 0
    # Opening it is the only honest test. `[ -r /dev/tty ]` passes in places
    # where the open then fails with ENXIO -- a process with no controlling
    # terminal still has the device node -- and the failed redirection leaves
    # `reply` unset, which under `set -u` aborts the installer outright.
    if ! { exec 3</dev/tty; } 2>/dev/null; then
        warn "no terminal to ask on — assuming no to: $prompt"
        return 1
    fi
    local reply=""
    read -r -p "  $prompt [y/N] " reply <&3
    exec 3<&-
    [[ "$reply" =~ ^[Yy] ]]
}

# --- packages ---------------------------------------------------------------
# Detected once and used twice: by the prerequisite offer below, and by the
# nginx step much further down. Detection only -- calling this installs
# nothing.
#
# Debian has sites-available/sites-enabled; Arch has neither, its nginx.conf
# includes conf.d/*.conf and nothing else, so writing a "vhost" into a
# sites-available directory that does not exist would silently do nothing.
PKG_MGR=""
PKG_INSTALL=""
PKG_NGINX=""
NGINX_LAYOUT="archlinux"
detect_pkg_manager() {
    if command -v apt-get >/dev/null 2>&1; then
        PKG_MGR="apt"
        # `sudo env VAR=...` rather than `sudo VAR=...`: sudo's own policy can
        # refuse an environment assignment on its command line, and env sets it
        # for the child regardless. Without DEBIAN_FRONTEND, apt tries a dialog
        # frontend, fails for want of a TERM, and prints half a screen of
        # debconf fallback noise before doing the right thing anyway.
        PKG_INSTALL="sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq"
        PKG_NGINX="nginx certbot python3-certbot-nginx"
        NGINX_LAYOUT="debian"
    elif command -v pacman >/dev/null 2>&1; then
        PKG_MGR="pacman"
        PKG_INSTALL="sudo pacman -S --noconfirm --needed"
        PKG_NGINX="nginx certbot certbot-nginx"
        NGINX_LAYOUT="archlinux"
    elif command -v dnf >/dev/null 2>&1; then
        PKG_MGR="dnf"
        PKG_INSTALL="sudo dnf install -y"
        PKG_NGINX="nginx certbot python3-certbot-nginx"
        NGINX_LAYOUT="archlinux"   # conf.d layout, same as Arch
    fi
}
detect_pkg_manager

# What each command is called for the manager in play. Only the packages this
# installer will ever offer to install are listed.
prereq_package() {
    case "$1" in
        git)     printf 'git' ;;
        rg)      printf 'ripgrep' ;;
        python3) if [ "$PKG_MGR" = "pacman" ]; then printf 'python'; else printf 'python3'; fi ;;
        node)    printf 'nodejs' ;;
        docker)  if [ "$PKG_MGR" = "apt" ]; then printf 'docker.io'; else printf 'docker'; fi ;;
        psql)    printf 'postgresql' ;;
        *)       printf '%s' "$1" ;;
    esac
}

# Offer to install the named commands. Returns 0 only if an install actually
# ran, so the caller knows whether re-checking is worth it.
#
# Consent is required every time, and --yes is NOT consent: a non-interactive
# run should mean "do not stop to ask me", never "put a Node runtime and a
# database on this machine without telling me". INSTALL_PREREQS=1 is the
# explicit opt-in for an unattended build that does want that.
offer_to_install() {
    [ "$#" -gt 0 ] || return 1
    local cmd pkgs=""
    for cmd in "$@"; do pkgs="$pkgs $(prereq_package "$cmd")"; done
    pkgs="${pkgs# }"

    if [ -z "$PKG_INSTALL" ]; then
        warn "no supported package manager found (apt, pacman or dnf) — install these yourself"
        return 1
    fi

    say ""
    say "  This installer can install the missing ones for you:"
    note "$PKG_INSTALL $pkgs"
    if [ "$ASSUME_YES" = "1" ]; then
        if [ "${INSTALL_PREREQS:-0}" != "1" ]; then
            warn "--yes does not authorise installing packages — re-run with INSTALL_PREREQS=1 to allow it"
            return 1
        fi
        say "  INSTALL_PREREQS=1 given — installing"
    elif ! confirm "Install them now?"; then
        warn "not installing anything — install them yourself and re-run"
        return 1
    fi

    if [ "$DRY_RUN" = "1" ]; then
        note "would run: $PKG_INSTALL $pkgs"
        return 0
    fi

    # dpkg's unpacking chatter runs to hundreds of lines and buries the only
    # thing worth reading, which is what the re-check says afterwards. Kept,
    # not discarded: on failure the tail is exactly what diagnoses it.
    say "  installing:$(printf ' %s' $pkgs) — this can take a minute"
    local log
    log="$(mktemp)"
    # shellcheck disable=SC2086  # both are deliberately word-split
    if ! $PKG_INSTALL $pkgs >"$log" 2>&1; then
        warn "the package install did not succeed:"
        tail -15 "$log" | while IFS= read -r line; do note "$line"; done
        rm -f "$log"
        return 1
    fi
    rm -f "$log"
    ok "installed"
    return 0
}

# Sourcing with TEKTONIX_INSTALL_LIB=1 defines the helpers above and stops
# before the installer does anything. That is how tests/test_install_prereqs.sh
# checks the package-name mapping and, more importantly, the consent rules --
# without a package manager, and without installing a thing.
if [ "${TEKTONIX_INSTALL_LIB:-0}" = "1" ]; then
    return 0 2>/dev/null \
        || die "TEKTONIX_INSTALL_LIB=1 is for sourcing this file, not running it"
fi

# --- 0. preflight -----------------------------------------------------------
step "Checking prerequisites"

missing=0
need() {
    local cmd="$1" why="$2"
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$cmd — $(command -v "$cmd")"
    else
        warn "$cmd not found — $why"
        missing=1
        ABSENT="$ABSENT $cmd"
    fi
}

# ABSENT collects what is not installed AT ALL, which is the only thing a
# package manager can fix. A runtime that is present but too old is tracked
# separately (OUTDATED): installing the distro's package would be a no-op,
# since the version already on the machine IS the distro's package.
check_prereqs() {
    missing=0
    ABSENT=""
    OUTDATED=""

    need git    "required to create per-project worktrees"
    need python3 "the agent runs on Python 3.12+"
    need node   "the dashboard build and the review services need Node 24+"
    need docker "the agent's bash/edit tools run inside a container; without it the FIRST tool call of the first task fails"
    # ripgrep is what the planner's repo search shells out to. Soft, not fatal:
    # without it the search tools answer "not installed" and everything else
    # works, so a missing rg must not block the install (the CI dry-run runner
    # has no rg, and neither will many first installs). It is still offered,
    # because being asked once beats reading a warning and forgetting.
    if command -v rg >/dev/null 2>&1; then
        ok "rg — $(command -v rg)"
    else
        warn "rg (ripgrep) not found — the planner's repo search needs it"
        ABSENT="$ABSENT rg"
    fi

    if command -v python3 >/dev/null 2>&1; then
        PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,12) else 0)' 2>/dev/null || echo 0)
        [ "$PY_OK" = "1" ] || {
            warn "python3 is $(python3 -V 2>&1 | cut -d" " -f2); 3.12+ required"
            missing=1
            OUTDATED="$OUTDATED python3"
        }
    fi

    # 24, not 20: Node 20 left maintenance in April 2026, and the frontend's own
    # test toolchain has already moved past it.
    if command -v node >/dev/null 2>&1; then
        NODE_MAJOR=$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)
        [ "$NODE_MAJOR" -ge 24 ] 2>/dev/null || {
            warn "node is v$NODE_MAJOR; 24+ required"
            missing=1
            OUTDATED="$OUTDATED node"
        }
    fi

    if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
        warn "docker is installed but not usable by this user (try: sudo usermod -aG docker \$USER, then re-login)"
        missing=1
    fi

    command -v pm2 >/dev/null 2>&1 && ok "pm2 — optional, for running as a service" \
                                   || note "pm2 not found (optional: npm i -g pm2 to run as a managed service)"
}

check_prereqs

# A dry run says what it would ask rather than asking: --dry-run promises to
# show every action, and "it would have offered to install these" is one.
if [ -n "$ABSENT" ] && [ "$DRY_RUN" = "1" ]; then
    note "would offer to install:$ABSENT"
fi

# shellcheck disable=SC2086  # ABSENT is a deliberately word-split list
if [ -n "$ABSENT" ] && [ "$DRY_RUN" != "1" ] && offer_to_install $ABSENT; then
    step "Re-checking prerequisites"
    check_prereqs
    # A distro package can be older than this project's floor -- Debian and
    # Ubuntu have shipped Node well behind 24 for most of its life. Saying
    # "installed" and then failing the version check two lines later is the
    # confusing outcome; name the real remedy instead.
    case " $OUTDATED " in
        *" node "*)    note "the distro's nodejs is older than 24 — use nvm or NodeSource for a current one" ;;
    esac
    case " $OUTDATED " in
        *" python3 "*) note "the distro's python3 is older than 3.12 — use pyenv, or deadsnakes on Ubuntu" ;;
    esac
fi

[ "$missing" = "0" ] || die "install the missing prerequisites above, then re-run."

# --- 1. answers -------------------------------------------------------------
step "Configuration"

if [ -f .env ]; then
    ok ".env already exists — keeping it (delete it to start over)"
    ENV_EXISTS=1
else
    ENV_EXISTS=0
    say "  Three answers are needed. Everything else is generated."
    say ""

    PG_DSN="${PG_DSN:-}"
    [ -n "$PG_DSN" ] || PG_DSN=$(ask "Postgres DSN" "postgresql://postgres@localhost:5432/tektonix")
    [ -n "$PG_DSN" ] || die "a Postgres DSN is required"

    OPENROUTER_API_KEY="${OPENROUTER_API_KEY:-}"
    if [ -z "$OPENROUTER_API_KEY" ]; then
        say "  An OpenRouter key is the only paid dependency (openrouter.ai/keys)."
        OPENROUTER_API_KEY=$(ask "OpenRouter API key")
    fi
    [ -n "$OPENROUTER_API_KEY" ] || die "an OpenRouter API key is required"

    ADMIN_EMAIL="${ADMIN_EMAIL:-}"
    [ -n "$ADMIN_EMAIL" ] || ADMIN_EMAIL=$(ask "Email for the first admin account" "admin@example.com")
fi

# --- 2. secrets -------------------------------------------------------------
if [ "$ENV_EXISTS" = "0" ]; then
    step "Generating secrets"
    # AUTH_SECRET_KEY must decode to 16/24/32 RAW bytes. `openssl rand -hex 32`
    # yields 64 hex CHARACTERS, which decodes to 48 bytes and is rejected at
    # startup -- a documented footgun, so the installer generates it correctly
    # rather than leaving it to a copy-paste.
    if [ "$DRY_RUN" = "1" ]; then
        AUTH_SECRET_KEY="<generated>"; MODEL_ROUTER_KEY="<generated>"
        note "would generate AUTH_SECRET_KEY (32 raw bytes, urlsafe-base64) and MODEL_ROUTER_KEY"
    else
        AUTH_SECRET_KEY=$(python3 -c 'import base64,secrets;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')
        MODEL_ROUTER_KEY="sk-$(python3 -c 'import secrets;print(secrets.token_hex(24))')"
        # Shared between the agent (which sends it) and the review service
        # (which checks it) to authorise merge/deploy. Undocumented and
        # ungenerated until now, so a by-the-book install failed at the merge
        # step of EVERY task -- after paying for the whole build.
        REVIEW_CONTROL_SECRET=$(python3 -c 'import secrets;print(secrets.token_hex(32))')
        ok "AUTH_SECRET_KEY generated (32 raw bytes — the format the app actually requires)"
        ok "MODEL_ROUTER_KEY generated (shared by the agent and the router)"
    fi
fi

# --- 3. write config --------------------------------------------------------
step "Writing configuration"

if [ "$ENV_EXISTS" = "0" ]; then
    if [ "$DRY_RUN" = "1" ]; then
        note "would write .env and services/model-router/.env"
    else
        umask 077
        cat > .env <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ). Never commit this file.
LANGGRAPH_PG_DSN=$PG_DSN

# The router this agent's model aliases resolve through (services/model-router).
MODEL_ROUTER_URL=http://127.0.0.1:4001/v1
MODEL_ROUTER_KEY=$MODEL_ROUTER_KEY

DEFAULT_BUDGET_USD=10.0
API_PORT=8100

AUTH_SECRET_KEY=$AUTH_SECRET_KEY
ADMIN_EMAIL=$ADMIN_EMAIL

# Where projects may be onboarded from, and where their worktrees are created.
# Onboarding gives an agent write access to what it points at, so keep this
# as narrow as your layout allows.
AGENT_PROJECT_ROOTS=$HOME
AGENT_SANDBOX_ROOT=$HOME/agent-workspaces

# Optional: password-reset email. Leave blank to disable.
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASS=
SMTP_FROM=

LANGSMITH_TRACING=false
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=
LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S=30

# Authorises merge/deploy calls from the agent to the review service. Must
# match the value in services/shared/.env, which is where the two Node
# services read it from.
REVIEW_CONTROL_SECRET=$REVIEW_CONTROL_SECRET
EOF
        cat > services/model-router/.env <<EOF
# Generated by install.sh. Never commit this file.
# The MODEL PROXY's own credentials, and nothing else: a file whose blast
# radius is "anything that can read the router's config" must not also hold
# the secret that authorises merge and deploy. That lives in
# services/shared/.env -- see services/shared/service-env.js.
OPENROUTER_API_KEY=$OPENROUTER_API_KEY
MODEL_ROUTER_KEY=$MODEL_ROUTER_KEY
EOF
        mkdir -p services/shared
        cat > services/shared/.env <<EOF
# Generated by install.sh. Never commit this file.
# Secrets for the two Node services (agent-review, commit-reviewer).
#
# Must equal REVIEW_CONTROL_SECRET in the agent's own .env -- the agent sends
# it on every merge/deploy call, these services check it. A mismatch means
# every merge is refused; an empty value disables the mutating endpoints
# entirely, which is the safe direction.
REVIEW_CONTROL_SECRET=$REVIEW_CONTROL_SECRET
EOF
        umask 022
        ok "wrote .env, services/model-router/.env and services/shared/.env (mode 600)"
    fi
fi

if [ -f projects.json ]; then
    ok "projects.json already exists — keeping it"
else
    if [ "$DRY_RUN" = "1" ]; then
        note "would create an empty projects.json"
    else
        printf '{\n  "projects": {}\n}\n' > projects.json
        ok "created an empty projects.json — add projects from the dashboard or scripts/add_project.py"
    fi
fi

# --- 4. database ------------------------------------------------------------
step "Database"

API_PORT_VALUE=$(grep -E '^API_PORT=' .env 2>/dev/null | cut -d= -f2 || true)
API_PORT_VALUE=${API_PORT_VALUE:-8100}
DB_NAME=$(printf '%s' "${PG_DSN:-}" | sed -n 's|.*/\([^/?]*\)\(?.*\)\{0,1\}$|\1|p')
if [ "$ENV_EXISTS" = "1" ]; then
    PG_DSN=$(grep -E '^LANGGRAPH_PG_DSN=' .env | cut -d= -f2-)
    DB_NAME=$(printf '%s' "$PG_DSN" | sed -n 's|.*/\([^/?]*\)\(?.*\)\{0,1\}$|\1|p')
fi

if [ "$DRY_RUN" = "1" ]; then
    note "would verify the database is reachable and create '$DB_NAME' if missing"
elif command -v psql >/dev/null 2>&1; then
    # -w: never prompt for a password. Without it psql blocks on a hidden
    # prompt when the DSN omits one -- which hangs an unattended install with
    # no visible reason. PGCONNECT_TIMEOUT bounds an unreachable host.
    if PGCONNECT_TIMEOUT=5 psql -w "$PG_DSN" -c 'SELECT 1' >/dev/null 2>&1; then
        ok "connected to $DB_NAME"
    else
        warn "cannot connect to $DB_NAME"
        if command -v createdb >/dev/null 2>&1 && confirm "create database '$DB_NAME' now?"; then
            if PGCONNECT_TIMEOUT=5 createdb -w "$DB_NAME" 2>/dev/null; then
                ok "created $DB_NAME"
            else
                warn "createdb failed — create it yourself, then re-run:"
                note "sudo -u postgres createdb $DB_NAME"
                note "and make sure the DSN in .env can authenticate (password, or a peer/trust entry in pg_hba.conf)"
            fi
        else
            warn "create the database yourself before starting the agent"
        fi
    fi
else
    # No psql, but Postgres is REQUIRED -- the app opens a connection pool at
    # startup and retries forever if it cannot reach one, so it never begins
    # serving and the log fills with "Connection refused". Saying "skipping
    # the check" here read as reassurance; a clean Debian 13 container
    # installed perfectly, then hung exactly this way.
    #
    # A plain TCP connect needs no client tools, so reachability can still be
    # verified. Python is already installed by this point.
    DB_HOST=$(printf '%s' "$PG_DSN" | sed -n 's|.*://\([^@/]*@\)\{0,1\}\([^:/?]*\).*|\2|p')
    DB_PORT=$(printf '%s' "$PG_DSN" | sed -n 's|.*://[^/]*:\([0-9]\{1,\}\).*|\1|p')
    DB_HOST=${DB_HOST:-127.0.0.1}
    DB_PORT=${DB_PORT:-5432}
    if python3 -c "
import socket, sys
try:
    socket.create_connection(('$DB_HOST', $DB_PORT), timeout=5).close()
except OSError:
    sys.exit(1)
" 2>/dev/null; then
        ok "Postgres is reachable at $DB_HOST:$DB_PORT"
        note "database '$DB_NAME' must exist; the agent creates its own TABLES but not the database"
    else
        warn "NOTHING IS LISTENING at $DB_HOST:$DB_PORT — Postgres is required."
        warn "The agent will not start without it: it retries the connection"
        warn "pool forever and never begins serving."
        if offer_to_install psql; then
            # Arch ships the package without a data directory; Debian and
            # Fedora initialise one for you. initdb on an already-initialised
            # cluster refuses rather than destroys, so this guard is about
            # noise, not safety.
            if [ "$PKG_MGR" = "pacman" ] && [ ! -d /var/lib/postgres/data/base ]; then
                run sudo -u postgres initdb -D /var/lib/postgres/data \
                    || warn "initdb failed — initialise the cluster yourself"
            fi
            run sudo systemctl enable --now postgresql \
                || warn "could not start postgresql — start it yourself, then re-run"
            if run sudo -u postgres createdb "$DB_NAME" 2>/dev/null; then
                ok "created $DB_NAME"
            else
                warn "database '$DB_NAME' was not created"
                note "sudo -u postgres createdb $DB_NAME"
            fi
            note "the DSN in .env must also be able to authenticate — a password, or a peer/trust entry in pg_hba.conf"
        else
            note "Debian/Ubuntu:  sudo apt install postgresql && sudo -u postgres createdb $DB_NAME"
            note "Arch/CachyOS:   sudo pacman -S postgresql"
            note "                sudo -u postgres initdb -D /var/lib/postgres/data   # Arch does NOT do this for you"
            note "                sudo systemctl enable --now postgresql"
            note "                sudo -u postgres createdb $DB_NAME"
        fi
    fi
fi

# --- 5. python --------------------------------------------------------------
step "Python environment"

if [ -d .venv ]; then
    ok ".venv already exists"
else
    run python3 -m venv .venv && ok "created .venv"
fi
if [ "$DRY_RUN" = "1" ]; then
    note "would install requirements.txt into .venv"
else
    say "  installing dependencies (this takes a minute)…"
    .venv/bin/pip install -q --upgrade pip >/dev/null 2>&1
    if .venv/bin/pip install -q -r requirements.txt; then
        ok "agent dependencies installed"
    else
        die "pip install failed — see the output above"
    fi
fi

# The router's config is the operator's file, not the repo's: the Models page
# rewrites it on every repin, so it is gitignored and seeded from the example
# once. Never overwritten -- an upgrade that clobbered the pins someone chose
# would be the worst kind of silent change.
if [ -f services/model-router/config.yaml ]; then
    ok "router config already exists (yours — left alone)"
elif [ "$DRY_RUN" = "1" ]; then
    note "would copy services/model-router/config.example.yaml to config.yaml"
elif [ -f services/model-router/config.example.yaml ]; then
    cp services/model-router/config.example.yaml services/model-router/config.yaml \
        && ok "wrote services/model-router/config.yaml from the example — repin from Settings → Models" \
        || die "could not write services/model-router/config.yaml"
else
    die "services/model-router/config.example.yaml is missing — the router has no aliases to serve"
fi

if [ -d services/model-router/venv ]; then
    ok "router venv already exists"
elif [ "$DRY_RUN" = "1" ]; then
    note "would create services/model-router/venv and install its requirements"
else
    python3 -m venv services/model-router/venv \
        && services/model-router/venv/bin/pip install -q -r services/model-router/requirements.txt \
        && ok "router dependencies installed" \
        || warn "router dependency install failed — see services/model-router/requirements.txt"
fi

# --- 6. sandbox image -------------------------------------------------------
step "Sandbox container image"

if [ "${SKIP_DOCKER:-0}" = "1" ]; then
    warn "skipped (SKIP_DOCKER=1) — the first tool call of the first task will fail without it"
elif [ "$DRY_RUN" = "1" ]; then
    note "would build tektonix-sandbox:latest from docker/agent-sandbox/"
elif docker image inspect tektonix-sandbox:latest >/dev/null 2>&1; then
    ok "tektonix-sandbox:latest already built"
else
    say "  building tektonix-sandbox:latest (a few minutes; it includes headless Chromium so the agent can see the UIs it builds)…"
    if docker build -q -t tektonix-sandbox:latest docker/agent-sandbox/ >/dev/null; then
        ok "sandbox image built"
    else
        warn "sandbox build failed — run it yourself: docker build -t tektonix-sandbox:latest docker/agent-sandbox/"
    fi
fi

# --- 7. frontend ------------------------------------------------------------
step "Dashboard"

# A release tarball (scripts/package_release.sh) ships frontend/dist already
# built, so installing from one needs no Node build at all -- the agent serves
# those files as they are. A git clone has no dist (it is gitignored), so that
# path still builds, which is also what you want while developing.
#
# Checked on dist alone, not on dist AND node_modules: a tarball has the first
# and not the second, and requiring both sent every tarball install through a
# full npm ci for files it already had.
if [ "${SKIP_FRONTEND:-0}" = "1" ]; then
    warn "skipped (SKIP_FRONTEND=1) — the server has no UI to serve until you run: cd frontend && npm ci && npm run build"
elif [ -f frontend/dist/index.html ]; then
    ok "dashboard already built — using the prebuilt frontend/dist (no Node build needed)"
elif [ "$DRY_RUN" = "1" ]; then
    note "would run npm ci && npm run build in frontend/ (no prebuilt dist here)"
else
    say "  installing and building the dashboard…"
    if (cd frontend && npm ci --silent >/dev/null 2>&1 && npm run build >/dev/null 2>&1); then
        ok "dashboard built to frontend/dist"
    else
        warn "dashboard build failed — run it yourself: cd frontend && npm ci && npm run build"
    fi
fi

# The logo tools (agent/tools/logo_tools.py) call LogoLoom's own Node modules,
# which need its dependencies present. Optional on purpose: the agent works
# fine without them and simply has no logo tools, which is a better outcome
# than an install that fails over a feature most people will never use. sharp
# ships prebuilt binaries, so --ignore-scripts is safe here and keeps a
# postinstall script out of the install path.
if [ ! -f services/logoloom/package.json ]; then
    :
elif [ -d services/logoloom/node_modules ]; then
    ok "logo tools ready"
elif [ "$DRY_RUN" = "1" ]; then
    note "would run npm ci in services/logoloom/ (logo and brand-kit tools)"
else
    say "  installing the logo tools…"
    if (cd services/logoloom && npm ci --silent --ignore-scripts --no-audit --no-fund >/dev/null 2>&1); then
        ok "logo tools ready"
    else
        warn "logo tools unavailable — the agent runs without them; to enable: cd services/logoloom && npm ci"
    fi
fi
# Tracing a raster logo needs a vectorizer, which is a 3MB binary from another
# project. Not fetched here: four of the five logo tools work without it, and
# a separate download is the operator's call, not a side effect of installing.
if [ -f services/logoloom/fetch-vtracer.sh ] && [ ! -x services/logoloom/vendor/vtracer ]; then
    note "to also trace existing raster logos: ./services/logoloom/fetch-vtracer.sh"
fi

# --- 8. remote access -------------------------------------------------------
step "Remote access"

# The session cookie is issued with the Secure flag, which browsers only return
# over HTTPS *or* to localhost. That makes the choice here binary and worth
# stating plainly: a tunnel to 127.0.0.1 works, and anything else needs real
# TLS. Plain HTTP on a LAN or VPN address silently fails -- the login POST
# succeeds, the cookie is dropped, and the next request bounces back to the
# login page with no error anywhere.
say "  The dashboard binds 127.0.0.1:$API_PORT_VALUE and is not reachable from outside"
say "  this host. Two supported ways in:"
say ""
say "    1. SSH tunnel (nothing to configure, nothing exposed):"
# `hostname` is not installed on a minimal Arch image (it lives in
# inetutils), so this printed "root@" with nothing after it. Fall back
# through the options that need no package, then to a placeholder.
_HOST=$(hostname -f 2>/dev/null || hostname 2>/dev/null || cat /proc/sys/kernel/hostname 2>/dev/null || echo "your-server")
say "         ssh -L 8100:127.0.0.1:8100 $(id -un)@${_HOST}"
say "    2. A domain with HTTPS, set up below."
say ""

DOMAIN="${AGENT_DOMAIN:-}"
if [ -z "$DOMAIN" ] && [ "$ASSUME_YES" != "1" ]; then
    if confirm "Set up nginx + a Let's Encrypt certificate on a domain now?"; then
        DOMAIN=$(ask "Domain (e.g. agent.example.com)")
    fi
fi

if [ -z "$DOMAIN" ]; then
    ok "skipped — use the SSH tunnel above (you can re-run this script later with AGENT_DOMAIN set)"
elif [ "$DRY_RUN" = "1" ]; then
    note "would install nginx+certbot, write a vhost for $DOMAIN, and request a certificate"
else
    LE_EMAIL="${LETSENCRYPT_EMAIL:-$ADMIN_EMAIL}"

    # DNS first. Requesting a certificate for a domain that does not point here
    # burns a Let's Encrypt rate-limit slot and fails with a message about
    # challenge validation rather than about DNS.
    RESOLVED=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -1)
    MYIP=$(curl -s --max-time 10 https://api.ipify.org 2>/dev/null || echo "")
    if [ -z "$RESOLVED" ]; then
        warn "$DOMAIN does not resolve yet — add an A record pointing at this host first"
        warn "skipping TLS setup; re-run with AGENT_DOMAIN=$DOMAIN once DNS is live"
        DOMAIN=""
    elif [ -n "$MYIP" ] && [ "$RESOLVED" != "$MYIP" ]; then
        warn "$DOMAIN resolves to $RESOLVED but this host appears to be $MYIP"
        if ! confirm "continue anyway?"; then DOMAIN=""; fi
    else
        ok "$DOMAIN resolves to $RESOLVED"
    fi
fi

if [ -n "$DOMAIN" ] && [ "$DRY_RUN" != "1" ]; then
    # PKG_INSTALL/PKG_NGINX/NGINX_LAYOUT come from detect_pkg_manager, run at
    # the top so the prerequisite step can use the same detection.

    for pkg_cmd in nginx certbot; do
        command -v "$pkg_cmd" >/dev/null 2>&1 || {
            if [ -n "$PKG_INSTALL" ]; then
                say "  installing $pkg_cmd…"
                $PKG_INSTALL $PKG_NGINX >/dev/null 2>&1 \
                    || warn "could not install $pkg_cmd automatically — install it and re-run"
            else
                warn "no supported package manager found — install nginx and certbot, then re-run"
            fi
        }
    done

    if [ "$NGINX_LAYOUT" = "debian" ]; then
        VHOST=/etc/nginx/sites-available/tektonix
    else
        VHOST=/etc/nginx/conf.d/tektonix.conf
        sudo mkdir -p /etc/nginx/conf.d
    fi
    if [ -f "$VHOST" ]; then
        ok "nginx vhost already exists at $VHOST — leaving it alone"
    else
        # Port 80 only at this stage, ON PURPOSE. A 443 block referencing
        # certificate files that do not exist yet makes `nginx -t` fail
        # outright, so the server cannot even reload to serve the ACME
        # challenge. certbot --nginx adds the TLS block (and the redirect)
        # after the certificate exists, copying this proxy config into it.
        sudo tee "$VHOST" >/dev/null <<NGINX
server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;

    location ^~ /.well-known/acme-challenge/ { root /var/www/html; allow all; }

    location / {
        # nginx's 1MB default rejects file attachments with its OWN 413 page
        # before the request reaches the app, so uploads fail no matter what
        # the app's own limits say.
        client_max_body_size 64m;

        proxy_pass         http://127.0.0.1:$API_PORT_VALUE/;
        proxy_http_version 1.1;

        # The dashboard streams task and planning output over WebSockets.
        # Without these the page loads fine and then never shows live output.
        proxy_set_header   Upgrade \$http_upgrade;
        proxy_set_header   Connection "upgrade";

        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        # \$proxy_add_x_forwarded_for appends the real peer LAST, which is the
        # hop the app's rate limiter reads. Do not replace this with a
        # pass-through of the client's own header.
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;

        # A planning turn or a long build step can run for many minutes with
        # no bytes on the wire; nginx's 60s default would kill it mid-run.
        proxy_read_timeout 1800s;
        proxy_send_timeout 1800s;
    }
}
NGINX
        sudo mkdir -p /var/www/html
        # conf.d is included directly; only Debian needs the enable symlink.
        if [ "$NGINX_LAYOUT" = "debian" ]; then
            sudo ln -sfn "$VHOST" /etc/nginx/sites-enabled/tektonix
        fi
        if sudo nginx -t >/dev/null 2>&1; then
            sudo systemctl reload nginx && ok "nginx vhost installed for $DOMAIN"
        else
            warn "nginx config test failed — run 'sudo nginx -t' to see why"
            if [ "$NGINX_LAYOUT" = "debian" ]; then
                sudo rm -f /etc/nginx/sites-enabled/tektonix
            else
                sudo rm -f "$VHOST"
            fi
            DOMAIN=""
        fi
    fi
fi

if [ -n "$DOMAIN" ] && [ "$DRY_RUN" != "1" ]; then
    if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
        ok "certificate for $DOMAIN already exists"
    else
        say "  requesting a certificate from Let's Encrypt…"
        if sudo certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos \
                -m "$LE_EMAIL" --redirect >/dev/null 2>&1; then
            ok "certificate issued; nginx now serves https://$DOMAIN"
            note "renewal is automatic — verify with: sudo certbot renew --dry-run"
        else
            warn "certbot failed. Common causes: port 80 not reachable from the"
            warn "internet, or DNS not yet propagated. Re-run:"
            note "sudo certbot --nginx -d $DOMAIN"
        fi
    fi
fi

# --- done -------------------------------------------------------------------
step "Done"

if [ "$DRY_RUN" = "1" ]; then
    say "  Dry run — nothing was changed."
    exit 0
fi

cat <<EOF

  Start the router, then the agent:

    ${B}services/model-router/venv/bin/uvicorn router.app:app --host 127.0.0.1 --port 4001${N}
    ${B}.venv/bin/uvicorn agent.server:app --host 127.0.0.1 --port 8100${N}

  Or under pm2, with the two review services (the merge gate; /_review/
  answers 502 without them):

    ${B}pm2 start ecosystem.config.js && pm2 start services/model-router/ecosystem.config.js${N}
    ${B}pm2 start services/commit-reviewer/reviewer.js --name commit-reviewer${N}
    ${B}pm2 start services/agent-review/server.js --name agent-review${N}

  Then open ${B}http://127.0.0.1:8100${N}. The first admin password is stored
  encrypted on first startup, never logged; show it once with
    ${B}.venv/bin/python scripts/show_initial_password.py${N}

  Finally, add a project to work on: Settings → Projects in the dashboard, or
    ${B}.venv/bin/python scripts/add_project.py /path/to/your/repo${N}

  Full guide: INSTALL.md
EOF
