#!/bin/bash
# Offline debug script for Raspberry Pi NMEA WebSocket proxy.
# Run from your Mac while connected to the same Wi-Fi as the Pi (no internet required).
#
# Usage:
#   ./debug_pi.sh              # auto-discover Pi
#   ./debug_pi.sh 192.168.4.51 # use explicit IP
#   PI_USER=pi ./debug_pi.sh   # override SSH user (default: pi)

set -u

PI_USER="${PI_USER:-rostape1}"
WS_PORT=8765
SSH_PORT=22

c_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
c_green() { printf "\033[32m%s\033[0m\n" "$*"; }
c_yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
c_cyan()  { printf "\033[36m%s\033[0m\n" "$*"; }
hr()      { printf "\n\033[1;34m── %s ─────────────────────────────────\033[0m\n" "$*"; }

PI="${1:-}"

hr "1. Mac network info"
MAC_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
GW=$(route -n get default 2>/dev/null | awk '/gateway/ {print $2}')
echo "Mac IP:     ${MAC_IP:-<none>}"
echo "Gateway:    ${GW:-<none>}"
if [ -z "$MAC_IP" ]; then
  c_red "No Wi-Fi/Ethernet IP. Check you're connected to the network."
  exit 1
fi
SUBNET=$(echo "$MAC_IP" | cut -d. -f1-3)

hr "2. Discover Pi"
if [ -z "$PI" ]; then
  echo "Trying mDNS (TyponRpi4.local, raspberrypi.local)..."
  for HOST in TyponRpi4.local raspberrypi.local; do
    PI=$(ping -c 1 -W 1000 "$HOST" 2>/dev/null | awk -F'[()]' '/PING/ {print $2; exit}')
    [ -n "$PI" ] && { c_green "Found via mDNS ($HOST): $PI"; break; }
  done
  if [ -n "$PI" ]; then :
  else
    c_yellow "mDNS failed. Scanning $SUBNET.0/24 (takes ~5s)..."
    for i in $(seq 1 254); do ping -c1 -W1 -t1 "$SUBNET.$i" >/dev/null 2>&1 & done
    wait
    PI=$(arp -an | grep -iE 'b8:27:eb|dc:a6:32|e4:5f:01|2c:cf:67|d8:3a:dd' | head -1 | awk -F'[()]' '{print $2}')
    if [ -n "$PI" ]; then
      c_green "Found Pi by MAC prefix: $PI"
    else
      c_red "Could not find Pi automatically."
      echo "Devices on network:"
      arp -an | grep -v incomplete
      echo
      echo "Re-run with explicit IP:  ./debug_pi.sh <ip>"
      exit 1
    fi
  fi
fi
echo "Using PI=$PI"

hr "3. Reachability"
if ping -c 2 -W 1000 "$PI" >/dev/null 2>&1; then
  c_green "ping OK"
else
  c_red "ping FAILED — Pi unreachable. Wrong network?"
  exit 1
fi

if nc -z -w 2 "$PI" $SSH_PORT 2>/dev/null; then
  c_green "SSH port $SSH_PORT open"
else
  c_yellow "SSH port $SSH_PORT closed (not fatal)"
fi

if nc -z -w 2 "$PI" $WS_PORT 2>/dev/null; then
  c_green "WS port $WS_PORT open"
  WS_OPEN=1
else
  c_red "WS port $WS_PORT CLOSED — nmea_ws_proxy.py not running on Pi"
  WS_OPEN=0
fi

hr "4. WebSocket handshake"
if [ "$WS_OPEN" = "1" ]; then
  RESP=$(curl -s -i -N --max-time 3 \
    -H "Connection: Upgrade" -H "Upgrade: websocket" \
    -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
    -H "Sec-WebSocket-Version: 13" \
    "http://$PI:$WS_PORT/" 2>&1 | head -5)
  echo "$RESP"
  if echo "$RESP" | grep -q "101"; then
    c_green "WebSocket handshake OK (101 Switching Protocols)"
  else
    c_red "WebSocket handshake failed"
  fi
else
  c_yellow "Skipped (port closed)"
fi

hr "5. Live NMEA stream (10 frames)"
if [ "$WS_OPEN" = "1" ]; then
  python3 - <<PYEOF
import asyncio, sys
try:
    import websockets
except ImportError:
    print("websockets module not installed on Mac — skipping. Install with: pip3 install websockets")
    sys.exit(0)

async def main():
    try:
        async with websockets.connect("ws://$PI:$WS_PORT", open_timeout=3) as ws:
            for i in range(10):
                msg = await asyncio.wait_for(ws.recv(), timeout=5)
                print(f"[{i+1}] {msg}".rstrip())
    except asyncio.TimeoutError:
        print("TIMEOUT — proxy connected but no NMEA data flowing.")
        print("Check on Pi: nc -zv 192.168.47.10 10110  (boat instruments)")
    except Exception as e:
        print(f"ERROR: {e}")

asyncio.run(main())
PYEOF
else
  c_yellow "Skipped (port closed)"
fi

hr "6. Browser setup"
echo "Open the GitHub Pages site, then paste in DevTools console:"
echo
c_cyan "  localStorage.setItem('nmea_ws_url', 'ws://$PI:$WS_PORT'); location.reload();"
echo
echo "Then open the Charts tab — gauges should populate within a few seconds."

hr "If something failed, SSH into the Pi and check:"
cat <<EOF
  ssh $PI_USER@$PI

  # On the Pi:
  hostname -I
  ps aux | grep -E 'nmea_ws_proxy|start_boat' | grep -v grep
  ss -tlnp | grep $WS_PORT             # is it listening on 0.0.0.0?
  nc -zv 192.168.47.10 10110           # can Pi see the boat instruments?

  # Restart proxy in foreground to see logs:
  cd ~/ais-tracker  # adjust path
  python3 nmea_ws_proxy.py
EOF

echo
c_green "Done. PI=$PI"
