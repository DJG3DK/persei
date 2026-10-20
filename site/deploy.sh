#!/usr/bin/env bash
# Build the landing page and put it where nginx serves perseiagent.ai.
#
# Static files only: there is no process to restart and nothing to keep warm,
# so a deploy is a copy. Run it from site/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${SITE_ROOT:-/var/www/tektonix-site}"

cd "$ROOT"
echo "building..."
npm run build

[ -f dist/index.html ] || { echo "build produced no dist/index.html" >&2; exit 1; }
grep -q '<script[^>]*src=' dist/index.html && {
    echo "dist/index.html references a script; this page must ship no JavaScript" >&2
    exit 1
}

echo "deploying to $TARGET"
mkdir -p "$TARGET"
# --delete so a renamed hashed asset does not leave its predecessor behind
# forever; the directory is ours entirely.
rsync -a --delete dist/ "$TARGET/"
echo "done. $(find "$TARGET" -type f | wc -l) files."
