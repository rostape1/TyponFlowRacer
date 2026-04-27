#!/bin/bash
# Start NMEA WebSocket proxy + static file server for Raspberry Pi
# Usage: ./start_boat.sh
# Browse to http://raspberrypi.local:8888

DIR="$(cd "$(dirname "$0")" && pwd)"

cleanup() {
    echo "Shutting down..."
    kill $PROXY_PID $SERVER_PID 2>/dev/null
    wait $PROXY_PID $SERVER_PID 2>/dev/null
    exit 0
}

trap cleanup SIGINT SIGTERM

echo "Starting NMEA WebSocket proxy on port 8765..."
python3 "$DIR/nmea_ws_proxy.py" &
PROXY_PID=$!

echo "Starting static file server on port 8888..."
python3 -m http.server 8888 --directory "$DIR/static" &
SERVER_PID=$!

echo ""
echo "Sailing dashboard ready:"
echo "  http://$(hostname).local:8888"
echo "  NMEA WebSocket: ws://$(hostname).local:8765"
echo ""
echo "Press Ctrl+C to stop."

wait
