#!/bin/bash
# Start NMEA WebSocket proxy + static file server for Raspberry Pi
# Usage: ./start_boat.sh
# Browse to http://raspberrypi.local:8888

DIR="$(cd "$(dirname "$0")" && pwd)"

# Generate self-signed TLS certificate for WSS (allows HTTPS pages to connect)
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
    kill $PROXY_PID $SERVER_PID 2>/dev/null
    wait $PROXY_PID $SERVER_PID 2>/dev/null
    exit 0
}

trap cleanup SIGINT SIGTERM

echo "Starting NMEA WebSocket proxy on port 8765 (ws) + 8766 (wss)..."
python3 "$DIR/nmea_ws_proxy.py" --ssl-cert "$CERT_FILE" --ssl-key "$KEY_FILE" &
PROXY_PID=$!

echo "Starting static file server on port 8888..."
python3 -m http.server 8888 --directory "$DIR/static" &
SERVER_PID=$!

echo ""
echo "Sailing dashboard ready:"
echo "  http://$(hostname).local:8888"
echo "  NMEA WebSocket (plain): ws://$(hostname).local:8765"
echo "  NMEA WebSocket (TLS):   wss://$(hostname).local:8766"
echo ""
echo "First time on HTTPS? Trust the certificate by opening:"
echo "  https://$(hostname).local:8766"
echo ""
echo "Press Ctrl+C to stop."

wait
