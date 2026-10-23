#!/usr/bin/env bash
# Build a release tarball with the dashboard already compiled.
#
# Installing from a git clone needs Node to build the dashboard. That is the
# right trade while developing and the wrong one on a server, where Node is
# otherwise only needed by the two review services. A tarball made here ships
# frontend/dist, so install.sh finds it built and skips the Node step entirely.
#
#   scripts/package_release.sh [version]      # default: git describe
#
# Produces dist/persei-<version>.tar.gz plus a .sha256 next to it.
#
# What it deliberately does NOT include: .env or any other secret, the
# database, backups, node_modules, .git, or the review worktrees. A release is
# code plus a built dashboard; everything else belongs to the installation.
set -euo pipefail

AGENT_HOME="${AGENT_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
cd "$AGENT_HOME"

VERSION="${1:-$(git describe --tags --always --dirty 2>/dev/null || date -u +%Y%m%dT%H%M%SZ)}"
OUT_DIR="$AGENT_HOME/dist"
STAGE="$(mktemp -d)"
NAME="persei-$VERSION"   # the product's name (Tektonix before 0.9.4)
trap 'rm -rf "$STAGE"' EXIT

echo "packaging $NAME"

# git archive takes HEAD, not the working tree: a release is a commit, not
# whatever happens to be on disk. Say so, rather than quietly shipping
# something that does not match the files in front of you.
if [ -n "$(git status --porcelain 2>/dev/null | grep -v '^?? ')" ]; then
    echo "  note: the working tree has uncommitted changes; this tarball is HEAD ($(git rev-parse --short HEAD))"
fi

# 1. Build the dashboard. This is the whole point of the exercise, so a
#    failure here stops the release rather than shipping a tarball whose
#    "prebuilt" dashboard is last week's.
echo "  building the dashboard (needs Node 24+)"
(cd frontend && npm ci --silent && npm run build >/dev/null)
[ -f frontend/dist/index.html ] || { echo "frontend build produced no dist/index.html" >&2; exit 1; }

# 2. Everything git tracks, which is code and docs and nothing secret.
echo "  collecting tracked files"
mkdir -p "$STAGE/$NAME"
git archive --format=tar HEAD | tar -x -C "$STAGE/$NAME"

# 2a. ...except site/, which is perseiagent.ai's public landing page. It is a
# separate build with its own deps, it is marketing rather than product, and
# an installation has no use for it -- somebody self-hosting Persei wants
# the console, not a page selling it to them. Removed here rather than being
# untracked, because it IS source that belongs in the repo.
rm -rf "$STAGE/$NAME/site"

# 3. The built dashboard, which git does not track on purpose.
echo "  adding the built dashboard"
mkdir -p "$STAGE/$NAME/frontend"
cp -r frontend/dist "$STAGE/$NAME/frontend/dist"

# 4. A receipt, so an installed box can say what it is running.
cat > "$STAGE/$NAME/RELEASE.json" <<EOF
{
  "version": "$VERSION",
  "commit": "$(git rev-parse HEAD 2>/dev/null || echo unknown)",
  "built_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "dashboard_prebuilt": true,
  "includes_landing_page": false
}
EOF

# 5. Refuse to ship a secret. Cheap, and the failure it prevents is permanent.
LEAKS=$(cd "$STAGE/$NAME" && find . \( -name '.env' -o -name '*.key' -o -name '.initial-admin-password' \) \
        -not -name '.env.example' -print)
if [ -n "$LEAKS" ]; then
    echo "refusing to package: the staged tree contains secrets:" >&2
    echo "$LEAKS" >&2
    exit 1
fi

mkdir -p "$OUT_DIR"
TARBALL="$OUT_DIR/$NAME.tar.gz"
tar -czf "$TARBALL" -C "$STAGE" "$NAME"
(cd "$OUT_DIR" && sha256sum "$NAME.tar.gz" > "$NAME.tar.gz.sha256")

echo "wrote $TARBALL ($(du -h "$TARBALL" | cut -f1))"
echo "     $(cat "$OUT_DIR/$NAME.tar.gz.sha256")"
echo
echo "To install from it on a server:"
echo "  tar -xzf $NAME.tar.gz && cd $NAME && ./install.sh"
echo "The dashboard is already built, so install.sh will not run a Node build."
