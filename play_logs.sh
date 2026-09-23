#!/bin/bash
# Replay the boat's NMEA recordings on this Mac.
#
# Usage: ./play_logs.sh            then open http://localhost:8080/#playback
#        LOG_DIR=/other ./play_logs.sh
#        PORT=8081 ./play_logs.sh
#
# Why this exists rather than `python3 -m http.server --directory static`:
# playback's recording picker is populated from /api/logs and files are fetched
# from /logs/<name>. Both are routes in pi/boat_server.py only. On a plain static
# server — or on GitHub Pages — the picker reads "Recording list unavailable" and
# auto-advance cannot work at all, because there is no list to step through.
#
# Running the real server also synthesises /config.json with useCloudAIS:false,
# which is what you want for replay: otherwise AISstream keeps injecting live
# traffic on top of the historical fleet.

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

PORT=${PORT:-8080}
# The CORRECTED archive. ~/Documents/typon-nmea-logs-raw/ holds the untouched
# originals pulled off the Pi; tools/fix_log_times.py turns those into this one.
# Serving the raw copy would show invented dates for anything recorded before the
# GPS log clock shipped — see P42.
LOG_DIR=${LOG_DIR:-$HOME/Documents/typon-nmea-logs}

if [ ! -d "$LOG_DIR" ]; then
    echo "No recordings at $LOG_DIR" >&2
    echo "Copy them off the Pi first (no SSH needed):" >&2
    echo "  curl -s http://typonrpi4.local:8080/api/logs" >&2
    echo "then GET each /logs/<name> into that directory." >&2
    exit 1
fi

COUNT=$(find "$LOG_DIR" -name 'nmea_*.txt' -type f | wc -l | tr -d ' ')
if [ "$COUNT" = "0" ]; then
    echo "No nmea_*.txt files in $LOG_DIR — nothing to replay." >&2
    exit 1
fi

# Reuse the same port every time so the URL stays bookmarkable, which means
# clearing out a previous run rather than failing to bind.
STALE=$(pgrep -f "boat_server.py --port $PORT" || true)
if [ -n "$STALE" ]; then
    echo "Stopping previous instance on :$PORT ($(echo $STALE | tr '\n' ' '))"
    kill $STALE 2>/dev/null || true
    sleep 1
fi

# A separate cache dir from the Pi's, so a Mac replay session never prunes or
# pollutes cache the boat depends on.
echo "Replaying $COUNT recordings from $LOG_DIR"
echo ""
echo "  Playback:  http://localhost:$PORT/#playback"
echo "  Hub:       http://localhost:$PORT/hub"
echo ""
echo "Press Ctrl+C to stop."
echo ""

exec python3 "$DIR/pi/boat_server.py" \
    --port "$PORT" \
    --log-dir "$LOG_DIR" \
    --cache-dir /tmp/mac-boat-cache
