#!/usr/bin/env bash
# Deterministic pre-review checks. NO LLM, no subagents, a few seconds.
#
# Exists because of what a measured run of the five-pass gate found on 2026-09-24
# (the vessel-name database):
#
#   Pass 1 (logic)       11 findings   Pass 3 (readability) 15 findings
#   Pass 4 (perf/sec)     8 findings   Passes 2 & 5         died to API timeouts
#
# Three passes independently found the SAME top four defects, and every finding
# worth acting on arrived with a measurement attached. The single worst one — an
# untracked generated file inside sw.js's atomic cache.addAll(), which would have
# destroyed all offline capability silently — was reachable by one assertion:
#
#     git ls-files --error-unmatch static/<each ASSETS entry>
#
# Two source files were also literally BINARY to grep(1) because a regex was
# written with raw control bytes, so their P19 citations were invisible to the
# `grep -rnE 'P[0-4][0-9]'` that CLAUDE.md prescribes for pitfall discovery. Also
# free to check.
#
# ~270k tokens of review agents to find things that cost milliseconds at the
# keyboard. So those checks live here, run BEFORE any reviewer is dispatched, and
# the reviewers receive a diff with this class of defect already stripped out.
#
# Usage:  smoke.sh ["<diff command>"]    # e.g. smoke.sh "git diff HEAD"
#         smoke.sh                       # staged if any, else working tree vs HEAD
# Exit:   0 unless the scope itself is unresolvable — findings are reported, not
#         fatal. Prints SMOKE_FINDINGS=<n> last; n>0 means fix before dispatching.

set -uo pipefail
cd "$(git rev-parse --show-toplevel 2>/dev/null)" || { echo "ERROR: not a git repo"; exit 1; }

DIFF_CMD="${1:-}"
if [ -z "$DIFF_CMD" ]; then
    if ! git diff --cached --quiet 2>/dev/null; then DIFF_CMD="git diff --cached"
    else DIFF_CMD="git diff HEAD"; fi
fi

# Untracked files are in scope too: that is precisely how the P43 defect hid.
FILES=$( { eval "$DIFF_CMD --name-only" 2>/dev/null; git ls-files --others --exclude-standard; } \
         | sort -u | grep -vE '^$' || true )

N=0
finding() { N=$((N+1)); echo "  [S$N] $*"; }
has() { echo "$FILES" | grep -qE "$1"; }

echo "SMOKE_SCOPE=$DIFF_CMD"
echo "SMOKE_FILES=$(echo "$FILES" | grep -c . || true)"
echo

# ── 1. Source files must be text, and pitfall citations grep-visible ──────────
# A raw control byte anywhere in a source file makes grep(1) treat it as binary
# and skip it silently, so `grep -rnE 'P[0-4][0-9]'` — the discovery mechanism
# CLAUDE.md documents — stops seeing that file's pitfall comments entirely.
echo "1. source files are text"
for f in $FILES; do
    case "$f" in
        *.js|*.mjs|*.py|*.sh|*.md|*.json|*.html|*.css|*.yml) ;;
        *) continue ;;
    esac
    [ -f "$f" ] || continue
    # Test the CONSEQUENCE, not a proxy for it: grep -I reports no match for a
    # file it considers binary, which is exactly how the P19 citation went
    # missing. A lone ESC byte is still text to grep; a NUL is not.
    if ! grep -Iq . "$f" 2>/dev/null; then
        finding "$f is BINARY to grep, so 'grep -rnE \"P[0-4][0-9]\"' skips it silently. Use \\xNN escapes in regexes."
    fi
done
[ "$N" = 0 ] && echo "   ok"

# ── 2. sw.js precache manifest: exists AND tracked (P43) ─────────────────────
# cache.addAll() rejects as a unit. One 404 and the Service Worker never
# activates, so the app shell, DATA_CACHE and TILE_CACHE are all lost — and it
# looks completely normal while online.
echo "2. sw.js ASSETS are present and tracked (P43)"
BEFORE=$N
if [ -f static/sw.js ]; then
    ASSETS=$(awk '/const ASSETS = \[/{f=1;next} f&&/\]/{exit} f' static/sw.js \
             | grep -oE "['\"][^'\"]+['\"]" | tr -d "'\"" || true)
    for a in $ASSETS; do
        case "$a" in http*|//*|./|.) continue ;; esac
        [ -f "static/$a" ] || finding "sw.js ASSETS lists static/$a which does not exist"
        git ls-files --error-unmatch "static/$a" >/dev/null 2>&1 \
            || finding "sw.js ASSETS lists static/$a which is NOT TRACKED — the deploy checkout would 404 it"
    done
fi
[ "$N" = "$BEFORE" ] && echo "   ok"

# ── 3. Diverged copies — did THIS edit reach both? ───────────────────────────
# P20's shape. These pairs must agree; each has drifted at least once.
echo "3. diverged copies agree (P20)"
BEFORE=$N
cmp_expr() {   # cmp_expr <label> <fileA> <fileB> <grep -oE pattern>
    a=$(grep -hoE "$4" "$2" 2>/dev/null | head -1)
    b=$(grep -hoE "$4" "$3" 2>/dev/null | head -1)
    if [ -n "$a$b" ] && [ "$a" != "$b" ]; then
        finding "$1 disagree: $2 has '$a', $3 has '$b'"
    fi
}
cmp_expr "control-char strip in the two cleanName copies" \
    static/js/vessel-names.js tools/build_vessel_names.mjs 'x00-.x1f.x7f[^]]*'
# docs/logging-and-playback.md: "static/hub.html mirrors these two values, so
# change both or the hub and the status page will disagree." The two files SPELL
# the numbers differently on purpose (1024 ** 3 vs 1 * 1024**3), so evaluate them
# rather than pattern-matching the text — a check that cries wolf gets ignored,
# which is worse than no check.
DISK_CMP=$(python3 - <<'PYEOF'
import re
def vals(path, pat):
    out = set()
    for m in re.finditer(pat, open(path, encoding='utf-8').read()):
        try:
            out.add(int(eval(m.group(1).replace('**', '**'))))
        except Exception:
            pass
    return out
py = vals('nmea_capture.py', r'DISK_(?:WARN|CRIT)_BYTES\s*=\s*([0-9 *]+)')
js = vals('static/hub.html', r'free\s*<\s*([0-9 *]+)')
print('MISMATCH' if py != js else 'OK', sorted(py), sorted(js))
PYEOF
)
case "$DISK_CMP" in
    MISMATCH*) finding "disk thresholds differ between nmea_capture.py and static/hub.html: $DISK_CMP" ;;
esac
[ "$N" = "$BEFORE" ] && echo "   ok"

# ── 4. Everything parses ─────────────────────────────────────────────────────
echo "4. syntax"
BEFORE=$N
for f in static/js/*.js static/sw.js; do
    node --check "$f" >/dev/null 2>&1 || finding "node --check failed: $f"
done
python3 -m py_compile pi/boat_server.py nmea_capture.py nmea_ws_proxy.py \
    download_offline.py tools/fix_log_times.py >/dev/null 2>&1 \
    || finding "py_compile failed on the Pi/root Python"
bash -n start_boat.sh play_logs.sh pi/startup.sh pi/test_boat_server.sh pi/ais-set-clock.sh \
    >/dev/null 2>&1 || finding "bash -n failed on a boat shell script"
for f in $FILES; do
    case "$f" in *.json) python3 -c "import json,sys; json.load(open('$f'))" 2>/dev/null \
        || finding "invalid JSON: $f" ;; esac
done
[ "$N" = "$BEFORE" ] && echo "   ok"

# ── 5. The test suites ───────────────────────────────────────────────────────
# All of them, always. They take about two seconds together, and P22 was CI
# skipping tests it should have run.
echo "5. test suites"
BEFORE=$N
for t in test_physics test_staleness test_invariants test_replay test_vessel_names; do
    node "tests/$t.mjs" >/tmp/smoke_$t.log 2>&1 || finding "tests/$t.mjs FAILED (see /tmp/smoke_$t.log)"
done
# P21: a stale .pyc once made a test pass against the bug it was written to catch.
find . -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
for t in test_boat_server test_log_clock; do
    python3 "tests/$t.py" >/tmp/smoke_$t.log 2>&1 || finding "tests/$t.py FAILED (see /tmp/smoke_$t.log)"
done
[ "$N" = "$BEFORE" ] && echo "   ok"

# ── 6. CI runs what exists ───────────────────────────────────────────────────
echo "6. every test suite is wired into CI (P22)"
BEFORE=$N
for t in tests/*.mjs tests/*.py; do
    case "$t" in *test_route.mjs) continue ;; esac   # network, deliberately not in CI
    grep -q "$(basename "$t")" .github/workflows/deploy.yml \
        || finding "$t is not referenced in deploy.yml — it will never run in CI"
done
[ "$N" = "$BEFORE" ] && echo "   ok"

# ── 7. Docs claims that are cheap to check ───────────────────────────────────
echo "7. doc cross-references resolve"
BEFORE=$N
for link in $(grep -ohE '\(docs/[a-z0-9-]+\.md\)' CLAUDE.md | tr -d '()' | sort -u); do
    [ -f "$link" ] || finding "CLAUDE.md links $link which does not exist"
done
if has 'docs/pitfalls\.md'; then
    LAST=$(grep -oE '\[P[0-9]{2}\]' docs/pitfalls.md | tr -d '[]P' | sort -n | tail -1)
    grep -q "P$LAST" CLAUDE.md || finding "P$LAST is in docs/pitfalls.md but not indexed in CLAUDE.md"
fi
[ "$N" = "$BEFORE" ] && echo "   ok"

echo
echo "SMOKE_FINDINGS=$N"
