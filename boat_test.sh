#!/bin/bash
# Offline boat test — sync project to Pi, start proxy + static server, verify it works.
# Run from your Mac in the AIS Tracker project root, on the boat's Wi-Fi (no internet).
#
# Usage:
#   ./boat_test.sh                  # auto-discover via mDNS
#   ./boat_test.sh 192.168.47.201   # explicit IP
#   PI_USER=rostape1 ./boat_test.sh

set -u

PI_USER="${PI_USER:-rostape1}"
PI_HOST="${1:-TyponRpi4.local}"
REMOTE_DIR="ais-tracker"
WS_PORT=8765
WEB_PORT=8888

c_red()   { printf "\033[31m%s\033[0m\n" "$*"; }
c_green() { printf "\033[32m%s\033[0m\n" "$*"; }
c_yellow(){ printf "\033[33m%s\033[0m\n" "$*"; }
c_cyan()  { printf "\033[36m%s\033[0m\n" "$*"; }
hr()      { printf "\n\033[1;34m── %s ─────────────────────────────────\033[0m\n" "$*"; }
die()     { c_red "$*"; exit 1; }

[ -f nmea_ws_proxy.py ] || die "Run this from the AIS Tracker project root (nmea_ws_proxy.py not found)."
[ -f start_boat.sh ]    || die "start_boat.sh missing — wrong directory?"

hr "1. Resolve Pi"
PI_IP=$(ping -c 1 -W 1500 "$PI_HOST" 2>/dev/null | awk -F'[()]' '/PING/ {print $2; exit}')
if [ -z "$PI_IP" ]; then
  c_yellow "$PI_HOST didn't resolve. Falling back to ARP scan..."
  SUBNET=$(ipconfig getifaddr en0 | cut -d. -f1-3)
  for i in $(seq 1 254); do ping -c1 -W1 -t1 "$SUBNET.$i" >/dev/null 2>&1 & done; wait
  PI_IP=$(arp -an | grep -iE 'b8:27:eb|dc:a6:32|e4:5f:01|2c:cf:67|d8:3a:dd|88:a2:9e' | head -1 | awk -F'[()]' '{print $2}')
  [ -z "$PI_IP" ] && die "Could not find Pi. Run with explicit IP: ./boat_test.sh <ip>"
fi
c_green "Pi at $PI_IP (user: $PI_USER)"
CTL="/tmp/boat_test_ssh_$$"
SSH_OPTS="-o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o ControlMaster=auto -o ControlPath=$CTL -o ControlPersist=300"
SSH="ssh $SSH_OPTS $PI_USER@$PI_IP"
trap "ssh -O exit $SSH_OPTS $PI_USER@$PI_IP 2>/dev/null; rm -f $CTL" EXIT

hr "2. SSH reachable?"
if $SSH "echo ok" 2>&1 | grep -q ok; then
  c_green "SSH OK"
else
  die "SSH failed. Check user/password or that SSH is enabled on the Pi."
fi

hr "3. Check websockets package on Pi"
if $SSH "python3 -c 'import websockets' 2>/dev/null"; then
  c_green "websockets installed"
else
  c_yellow "websockets missing on Pi — installing from bundled vendor/websockets-bundle.tgz..."
  if [ ! -f vendor/websockets-bundle.tgz ]; then
    c_red "vendor/websockets-bundle.tgz missing — can't install offline."
    exit 1
  fi
  scp $SSH_OPTS vendor/websockets-bundle.tgz "$PI_USER@$PI_IP:/tmp/" || die "scp failed"
  $SSH 'SITE=$(python3 -c "import site; print(site.getusersitepackages())"); mkdir -p "$SITE" && tar -xzf /tmp/websockets-bundle.tgz -C "$SITE" && rm /tmp/websockets-bundle.tgz && python3 -c "import websockets; print(\"installed:\", websockets.__version__)"' \
    && c_green "websockets installed offline" \
    || die "Offline install failed"
fi

hr "4. Copy bundle to Pi"
[ -f static.tgz ] || die "static.tgz missing — run ./build_static_bundle.sh on home WiFi first"
$SSH "mkdir -p ~/$REMOTE_DIR"
scp $SSH_OPTS nmea_ws_proxy.py start_boat.sh "$PI_USER@$PI_IP:~/$REMOTE_DIR/" || die "scp of scripts failed"

c_cyan "Shipping bundle ($(ls -lh static.tgz | awk '{print $5}'))..."
scp $SSH_OPTS static.tgz "$PI_USER@$PI_IP:~/$REMOTE_DIR/" \
  || die "bundle scp failed — re-run, scp will retry from scratch"

# Atomic extract: build static.new, then swap. Existing static/ is untouched if extract fails.
$SSH "cd ~/$REMOTE_DIR && rm -rf static.new && mkdir static.new \
  && tar -xzf static.tgz -C static.new \
  && rm -rf static && mv static.new static \
  && rm static.tgz" \
  || die "extract on Pi failed (existing static/ left intact)"
c_green "Bundle deployed (static/ atomically swapped)"

hr "5. Stop any existing proxy"
$SSH "pkill -f nmea_ws_proxy.py; pkill -f 'http.server $WEB_PORT'; true" >/dev/null 2>&1
sleep 1

hr "6. Start start_boat.sh in background on Pi"
$SSH "cd ~/$REMOTE_DIR && chmod +x start_boat.sh && nohup ./start_boat.sh > ~/boat.log 2>&1 < /dev/null &"
sleep 4
if $SSH "pgrep -f nmea_ws_proxy.py >/dev/null"; then
  c_green "Proxy running"
else
  c_red "Proxy didn't start. Last log lines:"
  $SSH "tail -30 ~/boat.log"
  exit 1
fi
if $SSH "pgrep -f 'http.server $WEB_PORT' >/dev/null"; then
  c_green "Static server running"
else
  c_yellow "Static file server not detected (may still be OK)"
fi

hr "7. Verify WS port open from Mac"
if nc -z -w 3 "$PI_IP" $WS_PORT; then
  c_green "ws://$PI_IP:$WS_PORT reachable"
else
  c_red "WS port closed — check ~/boat.log on Pi"
fi
if nc -z -w 3 "$PI_IP" $WEB_PORT; then
  c_green "http://$PI_IP:$WEB_PORT reachable"
else
  c_yellow "Web port closed"
fi

hr "8. Verify static/ on Pi"
TILE_SAMPLE=$(find static/tiles/osm -type f -name '*.png' 2>/dev/null | head -1 | sed 's|^static/||')
ok=1
curl -fsS "http://$PI_IP:$WEB_PORT/index.html" >/dev/null || { c_red "  index.html unreachable"; ok=0; }
curl -fsS "http://$PI_IP:$WEB_PORT/lib/leaflet.js" >/dev/null || { c_red "  lib/leaflet.js unreachable"; ok=0; }
curl -fsS "http://$PI_IP:$WEB_PORT/data/land_mask.json" >/dev/null || { c_red "  data/land_mask.json unreachable"; ok=0; }
if [ -n "$TILE_SAMPLE" ]; then
  curl -fsS "http://$PI_IP:$WEB_PORT/$TILE_SAMPLE" >/dev/null || { c_red "  sample tile $TILE_SAMPLE unreachable"; ok=0; }
fi
[ "$ok" = "1" ] && c_green "static/ verified (index + leaflet + land_mask + sample tile)" || c_red "static/ incomplete — extract may have failed"

hr "9. Sniff 5 NMEA frames"
python3 - <<PYEOF
import asyncio, sys
try: import websockets
except ImportError:
    print("websockets module missing on Mac; skipping sniff. (pip3 install websockets)")
    sys.exit(0)
async def main():
    try:
        async with websockets.connect("ws://$PI_IP:$WS_PORT", open_timeout=4) as ws:
            for i in range(5):
                msg = await asyncio.wait_for(ws.recv(), timeout=6)
                print(f"  [{i+1}] {msg}".rstrip())
    except asyncio.TimeoutError:
        print("  TIMEOUT — proxy is up but no NMEA data flowing.")
        print("  Check from Pi:  nc -zv 192.168.47.10 10110")
    except Exception as e:
        print(f"  ERROR: {e}")
asyncio.run(main())
PYEOF

hr "Done — open in browser"
c_cyan "  http://$PI_IP:$WEB_PORT      (Pi-hosted, fully offline)"
echo
c_cyan "  In the page, hit the Charts tab. If gauges stay blank, open DevTools console and run:"
c_cyan "    localStorage.setItem('nmea_ws_url', 'ws://$PI_IP:$WS_PORT'); location.reload();"
echo
echo "Tail proxy logs:   ssh $PI_USER@$PI_IP 'tail -f ~/boat.log'"
echo "Stop proxy:        ssh $PI_USER@$PI_IP 'pkill -f nmea_ws_proxy.py; pkill -f \"http.server $WEB_PORT\"'"
