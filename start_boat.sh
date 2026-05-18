#!/bin/bash
# Start the boat-mode server on the Raspberry Pi.
# Single process: serves the static AIS Tracker UI over HTTPS, reverse-proxies
# environmental APIs, and bridges local NMEA TCP to a WebSocket.
#
# Usage: ./start_boat.sh
# Browse to https://raspberrypi.local:8443 (trust the self-signed cert once).

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# --- Self-signed TLS cert (required for Service Worker + geolocation) -----
CERT_DIR="$DIR/certs"
CERT_FILE="$CERT_DIR/server.crt"
KEY_FILE="$CERT_DIR/server.key"

if [ ! -f "$CERT_FILE" ] || [ ! -f "$KEY_FILE" ]; then
    echo "Generating self-signed TLS certificate..."
    mkdir -p "$CERT_DIR"
    PI_IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    SAN="DNS:raspberrypi.local,DNS:$(hostname).local"
    [ -n "$PI_IP" ] && SAN="$SAN,IP:$PI_IP"
    openssl req -x509 -newkey rsa:2048 -nodes \
        -keyout "$KEY_FILE" -out "$CERT_FILE" \
        -days 3650 -subj "/CN=raspberrypi.local" \
        -addext "subjectAltName=$SAN" 2>/dev/null
    echo "Certificate generated at $CERT_DIR"
fi

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

PORT=${PORT:-8443}
OWN_MMSI=${OWN_MMSI:-338361814}

echo "Starting boat-mode server on port $PORT..."
python3 "$DIR/pi/boat_server.py" \
    --ssl-cert "$CERT_FILE" --ssl-key "$KEY_FILE" \
    --port "$PORT" --mmsi "$OWN_MMSI" &
SERVER_PID=$!

echo ""
echo "Sailing dashboard ready:"
echo "  https://$(hostname).local:$PORT/"
echo "  NMEA WebSocket:          wss://$(hostname).local:$PORT/nmea"
echo ""
echo "First time? Open https://$(hostname).local:$PORT/ once to trust the cert."
echo "Press Ctrl+C to stop."

wait
