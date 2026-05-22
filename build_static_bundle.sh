#!/bin/bash
# Build a self-contained static.tgz bundle for offline deploy to the boat Pi.
# MUST be run on home/internet WiFi — iCloud Drive needs to fetch dataless files.
# Output: static.tgz in the project root (~60 MB compressed).
#
# Usage:
#   ./build_static_bundle.sh

set -eu

c_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
c_green() { printf "\033[32m%s\033[0m\n" "$*"; }
c_cyan()  { printf "\033[36m%s\033[0m\n" "$*"; }
hr()      { printf "\n\033[1;34m── %s ─────────────────────────────────\033[0m\n" "$*"; }
die()     { c_red "$*"; exit 1; }

[ -d static ] || die "Run this from the AIS Tracker project root (static/ not found)."

STAGE=/tmp/ais-static-staged

hr "1. Copy static/ to a local non-iCloud staging area"
c_cyan "cp -R reads every byte, which forces iCloud to materialize placeholders."
c_cyan "This step needs internet so iCloud can fetch dataless files."
rm -rf "$STAGE"
if ! cp -R static "$STAGE" 2>/tmp/build_static_errors; then
    c_red "Copy failed — likely lost internet mid-run (iCloud couldn't fetch a file)."
    head -5 /tmp/build_static_errors
    die "Aborted. Reconnect to internet and re-run."
fi
COUNT=$(find "$STAGE" -type f | wc -l | tr -d ' ')
c_green "Copied $COUNT files to $STAGE (all materialized, no longer iCloud)"

hr "2. Build tarball"
tar -czf static.tgz -C "$STAGE" .
SIZE=$(ls -lh static.tgz | awk '{print $5}')
c_green "static.tgz built: $SIZE"

rm -rf "$STAGE"

hr "Done"
c_cyan "Next: switch to boat WiFi and run ./boat_test.sh"
