#!/usr/bin/env bash
# Build the landing page and put it where nginx serves perseiagent.ai.
#
# The page itself is static files, so this part of a deploy is a copy. Run it
# from site/. The download buttons and the newsletter form are served by
# site/server (pm2 tektonix-newsletter): a change there needs that service
# restarted too, with its .env exported (see site/server/README.md).
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
