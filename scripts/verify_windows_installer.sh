#!/usr/bin/env bash
# Verify a released Windows installer's Authenticode signature from Linux:
# the signer, the chain to Microsoft's code-signing root, and the RFC 3161
# timestamp that keeps the signature valid after the signing certificate
# (a short-lived one, with Azure Artifact Signing) expires.
#
#   scripts/verify_windows_installer.sh <installer.exe> [expected publisher CN]
#   scripts/verify_windows_installer.sh --release v0.9.1 [expected publisher CN]
#
# The Microsoft root is not in the Linux trust stores (they carry TLS roots,
# not code-signing ones), which is why a bare `osslsigncode verify` reports
# "unable to get local issuer certificate" on a correctly signed file. The
# root is fetched from Microsoft's own PKI site and pinned by fingerprint
# here, so a substituted certificate fails instead of being trusted.
set -euo pipefail

ROOT_URL="https://www.microsoft.com/pkiops/certs/Microsoft%20Identity%20Verification%20Root%20Certificate%20Authority%202020.crt"
# Microsoft Identity Verification Root Certificate Authority 2020, valid to 2045.
ROOT_SHA256="53:67:F2:0C:7A:DE:0E:2B:CA:79:09:15:05:6D:08:6B:72:0C:33:C1:FA:2A:26:61:AC:F7:87:E3:29:2E:12:70"
REPO="DJG3DK/persei"

die() { echo "verify: $*" >&2; exit 1; }
command -v osslsigncode >/dev/null || die "osslsigncode is not installed (apt install osslsigncode)"
command -v openssl >/dev/null || die "openssl is not installed"

work="$(mktemp -d)"; trap 'rm -rf "$work"' EXIT

if [ "${1:-}" = "--release" ]; then
    tag="${2:?usage: --release <tag> [publisher]}"; publisher="${3:-}"
    command -v gh >/dev/null || die "gh is needed for --release"
    gh release download "$tag" -R "$REPO" -p '*_x64-setup.exe' -D "$work" >/dev/null
    exe="$(find "$work" -maxdepth 1 -name '*_x64-setup.exe' | head -1)"
    [ -n "$exe" ] || die "release $tag has no Windows installer"
else
    exe="${1:?usage: $0 <installer.exe> [publisher]}"; publisher="${2:-}"
    [ -f "$exe" ] || die "$exe does not exist"
fi

curl -sfL --max-time 30 -o "$work/root.crt" "$ROOT_URL" || die "could not fetch the Microsoft root from $ROOT_URL"
openssl x509 -inform DER -in "$work/root.crt" -out "$work/root.pem" 2>/dev/null \
    || openssl x509 -in "$work/root.crt" -out "$work/root.pem" 2>/dev/null \
    || die "the downloaded root is not a certificate"
got="$(openssl x509 -in "$work/root.pem" -noout -fingerprint -sha256 | cut -d= -f2)"
[ "$got" = "$ROOT_SHA256" ] || die "the Microsoft root's fingerprint changed ($got); refusing to trust it"

out="$(osslsigncode verify -in "$exe" -CAfile "$work/root.pem" -TSA-CAfile "$work/root.pem" 2>&1)" \
    || { echo "$out" >&2; die "$(basename "$exe"): signature does NOT verify"; }
echo "$out" | grep -q "^Signature verification: ok" || die "$(basename "$exe"): no valid signature"
# The timestamp, checked with OpenSSL rather than by osslsigncode: 2.8 cannot
# read the tokens Microsoft's timestamping service has written since late
# 2026 and says "Timestamp is not available" (v0.9.2, 2026-10-07).
osslsigncode extract-signature -pem -in "$exe" -out "$work/sig.pem" >/dev/null 2>&1 \
    || die "$(basename "$exe"): could not read its signature"
openssl pkcs7 -in "$work/sig.pem" -outform DER -out "$work/sig.der" \
    || die "$(basename "$exe"): its signature is not PKCS#7"
stamped="$(python3 "$(dirname "$0")/verify_authenticode_timestamp.py" "$work/sig.der" "$work/root.pem")" \
    || die "$(basename "$exe"): the timestamp does not verify"

signer="$(echo "$out" | grep -m1 -E '^\s+Subject: .*/CN=' | sed -E 's/.*\/CN=//')"
if [ -n "$publisher" ] && [ "$signer" != "$publisher" ]; then
    die "$(basename "$exe"): signed by '$signer', expected '$publisher'"
fi
echo "$(basename "$exe"): signed by $signer, chain verified, timestamped $stamped"
