#!/bin/bash
# Start the boat-mode server on the Raspberry Pi.
# Serves over plain HTTP — closed boat network, no cert needed.
#
# Usage: ./start_boat.sh
# Browse to http://raspberrypi.local:8080

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

cleanup() {
    echo "Shutting down..."
    kill $SERVER_PID 2>/dev/null
    wait $SERVER_PID 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

# Kill any stale instances from previous runs.
kill_stale() {
    local pat="$1"
    local pids
    pids=$(pgrep -f "$pat" | grep -v "^$$\$" || true)
    if [ -n "$pids" ]; then
        echo "Killing stale ($pat): $(echo $pids | tr '\n' ' ')"
        kill $pids 2>/dev/null || true
        sleep 0.5
        kill -9 $pids 2>/dev/null || true
    fi
}
kill_stale "boat_server.py"
kill_stale "nmea_ws_proxy.py"        # legacy
kill_stale "http.server 8888"        # legacy

PORT=${PORT:-8080}
OWN_MMSI=${OWN_MMSI:-338361814}

echo "Starting boat-mode server on port $PORT..."
python3 "$DIR/pi/boat_server.py" \
    --port "$PORT" --mmsi "$OWN_MMSI" &
SERVER_PID=$!

echo ""
echo "Sailing dashboard ready:"
echo "  http://$(hostname).local:$PORT/"
echo "  NMEA WebSocket:          ws://$(hostname).local:$PORT/nmea"
echo ""
echo "Press Ctrl+C to stop."

wait
