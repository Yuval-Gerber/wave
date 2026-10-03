#!/bin/zsh
# THE real end of the Keychain password prompts (v2, 2026-09-02).
#
# v1 failed in the field: macOS guards every Keychain item with a hidden
# PARTITION LIST of allowed signer types, keyed by the signing cert's
# team id (its OU field). Our v1 self-signed cert had NO OU → no partition
# → "Always Allow" could never stick and macOS re-prompted every launch.
#
# v2 does it completely:
#   1. replaces the cert with one carrying OU=WAVESIGN01 (the team id),
#   2. trusts it for code signing,
#   3. re-signs the INSTALLED app in place (no rebuild needed),
#   4. stamps "teamid:WAVESIGN01" into the partition list of every Wave
#      Keychain item — your Mac password is asked ONCE by this script.
#
# After it runs: restart Wave → ONE final "Always Allow" round (new cert =
# new identity, macOS asks one last time — the grant now STICKS) → every
# restart after that is silent, forever, across rebuilds.
set -euo pipefail

if pgrep -xq "Wave"; then
    echo "Wave is RUNNING — quit it first (re-signing a live binary can kill it)."
    exit 1
fi

NAME="Wave Signing"
TEAM="WAVESIGN01"
KEYCHAIN="$HOME/Library/Keychains/login.keychain-db"

echo "==> Your Mac (login) password — used once to stamp the Keychain items"
read -rs "PW?password: "
echo ""

echo "==> Removing the old team-less identity (if present)"
security delete-identity -c "$NAME" "$KEYCHAIN" 2>/dev/null || true

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
cat > "$TMP/ext.cnf" <<CNF
[req]
distinguished_name=dn
x509_extensions=ext
prompt=no
[dn]
CN=$NAME
OU=$TEAM
[ext]
keyUsage=critical,digitalSignature
extendedKeyUsage=critical,codeSigning
basicConstraints=critical,CA:false
CNF

echo "==> Generating the certificate (OU=$TEAM, 10 years)"
openssl req -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -keyout "$TMP/key.pem" -out "$TMP/cert.pem" -config "$TMP/ext.cnf"

echo "==> Importing key + certificate"
security import "$TMP/key.pem" -k "$KEYCHAIN" -T /usr/bin/codesign
security import "$TMP/cert.pem" -k "$KEYCHAIN"

echo "==> Trusting it for code signing (macOS may confirm once)"
security add-trusted-cert -r trustRoot -p codeSign -k "$KEYCHAIN" "$TMP/cert.pem"

echo "==> Letting codesign use the key without a prompt"
security set-key-partition-list -S "apple-tool:,apple:,codesign:" \
    -s -k "$PW" "$KEYCHAIN" > /dev/null

echo "==> Re-signing the installed app in place"
for app in "$HOME/Desktop/Wave.app" "$(dirname "$0")/../dist/Wave.app"; do
    if [ -d "$app" ]; then
        codesign --force --deep -s "$NAME" "$app"
        echo "    signed: $app"
    fi
done

echo "==> Stamping the partition list on every Wave Keychain item"
for acct in $(security dump-keychain "$KEYCHAIN" 2>/dev/null \
        | awk '/"acct"<blob>=/{acct=$0} /"svce"<blob>="Wave"/{print acct}' \
        | sed 's/.*="\(.*\)"/\1/' | sort -u); do
    if security set-generic-password-partition-list \
            -S "apple-tool:,apple:,teamid:$TEAM" \
            -s "Wave" -a "$acct" -k "$PW" "$KEYCHAIN" > /dev/null 2>&1; then
        echo "    stamped: $acct"
    else
        echo "    FAILED to stamp: $acct (fix by hand later — not fatal)"
    fi
done

echo ""
echo "Done. Now: restart Wave → click 'Always Allow' on each prompt ONE"
echo "last time (the new identity) → after that, every restart is silent."
