#!/bin/bash
# Runs on boot via systemd (ais-tracker.service).
# Pulls latest code, then starts the boat server.

set -e
DIR="$(cd "$(dirname "$0")/.." && pwd)"   # repo root

cd "$DIR"

echo "[startup] Pulling latest code..."
git fetch origin 2>&1 && git reset --hard origin/boat-mode 2>&1 || echo "[startup] git update failed (offline?), continuing with current code"

echo "[startup] Starting NMEA logger..."
python3 "$DIR/nmea_capture.py" --ws-url ws://localhost:8080/nmea &

echo "[startup] Starting boat server..."
exec "$DIR/start_boat.sh"
