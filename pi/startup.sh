#!/bin/bash
# Runs on boot via systemd (ais-tracker.service).
# Pulls latest code, then starts the boat server.

set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"   # repo root

cd "$DIR"

# Same default as start_boat.sh, so the two can't disagree about the port.
PORT=${PORT:-8080}
export PORT

echo "[startup] Pulling latest code..."
git fetch origin 2>&1 && git reset --hard origin/boat-mode 2>&1 || echo "[startup] git update failed (offline?), continuing with current code"

echo "[startup] Starting NMEA logger..."
python3 "$DIR/nmea_capture.py" --ws-url "ws://localhost:$PORT/nmea" --web-port 8081 &
# start_boat.sh sweeps stale nmea_capture.py processes; keep the one we just started.
KEEP_PIDS=$!
export KEEP_PIDS

echo "[startup] Starting boat server..."
exec "$DIR/start_boat.sh"
