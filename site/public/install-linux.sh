#!/bin/sh
# Install the Persei desktop app on Linux.
#
#   curl -fsSL https://perseiagent.ai/install-linux.sh | sh
#   curl -fsSL https://perseiagent.ai/install-linux.sh | sh -s -- --version v0.9.2-rc2
#
# Downloads the AppImage from the newest release on GitHub (or the version
# named), checks it against the SHA-256 GitHub publishes for it, puts it
# where the app keeps itself, adds Persei to your app menu, and starts it.
# Your own files only: nothing here needs root. Docker, if it is missing, is
# set up by the app itself, which asks for your password once.
set -eu

REPO="DJG3DK/persei"
API="${TEKTONIX_API:-https://api.github.com/repos/$REPO/releases}"
VERSION=""

while [ $# -gt 0 ]; do
    case "$1" in
        --version) VERSION="${2:-}"; shift 2 ;;
        --version=*) VERSION="${1#--version=}"; shift ;;
        -h|--help) sed -n '2,12p' "$0" 2>/dev/null || true; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

say() { printf '%s\n' "$*"; }
die() { printf 'persei: %s\n' "$*" >&2; exit 1; }

[ "$(uname -s)" = Linux ] || die "this installer is for Linux; Windows has its own installer at perseiagent.ai"
case "$(uname -m)" in
    x86_64|amd64) ;;
    *) die "there is no Persei build for $(uname -m) yet (x86_64 only)" ;;
esac
command -v curl >/dev/null || die "curl is needed"
command -v sha256sum >/dev/null || die "sha256sum is needed (coreutils)"
case "$VERSION" in
    "") RELEASE_URL="$API/latest" ;;
    v[0-9]*) RELEASE_URL="$API/tags/$VERSION" ;;
    *) die "a version looks like v0.9.2 or v0.9.2-rc2" ;;
esac

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
case "$DATA" in /*) ;; *) DATA="$HOME/.local/share" ;; esac
DIR="$DATA/io.tektonix.desktop"
TARGET="$DIR/Tektonix.AppImage"
mkdir -p "$DIR"

say "Looking up the release..."
JSON="$(curl -fsSL -H 'Accept: application/vnd.github+json' "$RELEASE_URL")" \
    || die "could not read the release from GitHub${VERSION:+ ($VERSION)}"

# The AppImage's download URL and digest, from the release's asset list.
# GitHub sends this JSON pretty-printed or on one line, depending on the
# server that answers (one line broke the plain one-liner, 2026-10-07), so
# the three fields are pulled out wherever they are, one per line, in order:
# "name" opens each asset, and only assets carry "digest" and
# "browser_download_url". Quotes inside the release notes are escaped, so
# nothing written there can match.
FOUND="$(printf '%s\n' "$JSON" \
    | grep -oE '"(name|digest|browser_download_url)": *"[^"]*"' \
    | awk '
    /"name":/ { n = $0; sub(/.*"name": *"/, "", n); sub(/".*/, "", n); cur = n }
    cur ~ /^(Persei|Tektonix)_[0-9A-Za-z.-]+_amd64\.AppImage$/ && /"digest":/ {
        d = $0; sub(/.*"digest": *"sha256:/, "", d); sub(/".*/, "", d); digest = d }
    cur ~ /^(Persei|Tektonix)_[0-9A-Za-z.-]+_amd64\.AppImage$/ && /"browser_download_url":/ {
        u = $0; sub(/.*"browser_download_url": *"/, "", u); sub(/".*/, "", u); url = u }
    END { if (url != "") print url, digest }')"
URL="${FOUND%% *}"
SUM="${FOUND#* }"
[ -n "$URL" ] || die "that release has no Linux app${VERSION:+ ($VERSION)}; see https://github.com/$REPO/releases"
case "$URL" in
    "https://github.com/$REPO/releases/download/"*|file://*) ;;
    *) die "unexpected download address: $URL" ;;
esac
printf '%s' "$SUM" | grep -Eq '^[0-9a-f]{64}$' || die "GitHub gave no checksum for $URL; not installing it"

PART="$TARGET.part"
trap 'rm -f "$PART"' EXIT
say "Downloading ${URL##*/}..."
curl -fL --progress-bar -o "$PART" "$URL" || die "the download failed"
GOT="$(sha256sum "$PART" | cut -d' ' -f1)"
[ "$GOT" = "$SUM" ] || die "the download does not match GitHub's checksum (got $GOT); not installing it"
chmod 755 "$PART"
mv -f "$PART" "$TARGET"
trap - EXIT

# The menu entry and icon, written by the app itself (the same code it runs
# when started from anywhere else), without opening a window.
"$TARGET" --integrate || die "the app could not add itself to the menu"
say "Installed: Persei is in your app menu."

if [ -n "${WAYLAND_DISPLAY:-}${DISPLAY:-}" ]; then
    say "Starting it..."
    if command -v setsid >/dev/null; then
        setsid "$TARGET" >/dev/null 2>&1 < /dev/null &
    else
        nohup "$TARGET" >/dev/null 2>&1 < /dev/null &
    fi
else
    say "Open it from your app menu."
fi
