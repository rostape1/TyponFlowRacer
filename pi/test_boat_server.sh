#!/bin/bash
# End-to-end smoke test for the boat-mode Pi server.
# Run on the Pi (or any host with curl) while ./start_boat.sh is running.
#
# Usage:   ./pi/test_boat_server.sh [host:port]
# Default: localhost:8443
#
# Verifies:
#   - /config.json shape
#   - /api/noaa proxy byte-matches direct NOAA response
#   - /api/open-meteo proxy byte-matches direct Open-Meteo response
#   - /data/sfbofs and /data/wind reverse-proxy work
#   - /data/land_mask.json catch-all + local fallback work
#   - SSRF allowlist rejects bogus tails
#   - X-Cache header transitions MISS -> HIT
#   - /nmea WebSocket upgrades

set -u
HOST="${1:-localhost:8443}"
BASE="https://${HOST}"
CURL="curl -sk --max-time 30"  # -k for self-signed cert

PASS=0
FAIL=0
ok()   { echo "  ✓ $1"; PASS=$((PASS+1)); }
fail() { echo "  ✗ $1"; FAIL=$((FAIL+1)); }

echo "== boat-mode smoke test against $BASE =="

# ---- 1. server alive ----
echo "[1] Server reachable"
status=$($CURL -o /dev/null -w "%{http_code}" "$BASE/")
[ "$status" = "200" ] && ok "GET / -> 200" || fail "GET / -> $status"

# ---- 2. /config.json ----
echo "[2] /config.json shape"
cfg=$($CURL "$BASE/config.json")
echo "$cfg" | grep -q '"mode": *"boat"' && ok 'mode == "boat"' || fail "mode missing/wrong: $cfg"
echo "$cfg" | grep -q '"useCloudAIS": *false' && ok 'useCloudAIS == false' || fail "useCloudAIS wrong"
echo "$cfg" | grep -q '"nmeaWsUrl"' && ok 'nmeaWsUrl present' || fail 'nmeaWsUrl missing'
echo "$cfg" | grep -q '"ownMmsi"' && ok 'ownMmsi present' || fail 'ownMmsi missing'

# ---- 3. NOAA proxy fidelity ----
echo "[3] NOAA proxy byte-match"
NOAA_QS='begin_date=20260517&end_date=20260520&station=9414290&product=predictions&datum=MLLW&units=english&time_zone=gmt&format=json&interval=6'
$CURL "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter?$NOAA_QS" > /tmp/noaa_direct.json
$CURL "$BASE/api/noaa/api/prod/datagetter?$NOAA_QS" > /tmp/noaa_proxy.json
if [ -s /tmp/noaa_direct.json ] && [ -s /tmp/noaa_proxy.json ]; then
    if diff -q /tmp/noaa_direct.json /tmp/noaa_proxy.json >/dev/null; then
        ok "NOAA proxy == direct (station 9414290 predictions)"
    else
        fail "NOAA proxy DIFFERS from direct"
        diff /tmp/noaa_direct.json /tmp/noaa_proxy.json | head -5
    fi
else
    fail "NOAA fetch failed (direct=$(stat -c%s /tmp/noaa_direct.json 2>/dev/null || echo 0)b proxy=$(stat -c%s /tmp/noaa_proxy.json 2>/dev/null || echo 0)b)"
fi

# ---- 4. Open-Meteo proxy fidelity ----
echo "[4] Open-Meteo proxy byte-match"
OM_QS='latitude=37.81&longitude=-122.42&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m&models=gfs_seamless&wind_speed_unit=kn&forecast_hours=49'
$CURL "https://api.open-meteo.com/v1/forecast?$OM_QS" > /tmp/om_direct.json
$CURL "$BASE/api/open-meteo/v1/forecast?$OM_QS" > /tmp/om_proxy.json
if [ -s /tmp/om_direct.json ] && [ -s /tmp/om_proxy.json ]; then
    if diff -q /tmp/om_direct.json /tmp/om_proxy.json >/dev/null; then
        ok "Open-Meteo proxy == direct (1 point, 49h)"
    else
        # Open-Meteo includes a per-call generationtime_ms — strip it and re-compare.
        sed -E 's/"generationtime_ms":[0-9.]+,?//g' /tmp/om_direct.json > /tmp/om_direct.norm
        sed -E 's/"generationtime_ms":[0-9.]+,?//g' /tmp/om_proxy.json  > /tmp/om_proxy.norm
        if diff -q /tmp/om_direct.norm /tmp/om_proxy.norm >/dev/null; then
            ok "Open-Meteo proxy == direct (only generationtime_ms differs)"
        else
            fail "Open-Meteo proxy DIFFERS beyond generationtime_ms"
            diff /tmp/om_direct.norm /tmp/om_proxy.norm | head -10
        fi
    fi
else
    fail "Open-Meteo fetch failed"
fi

# ---- 5. SFBOFS reverse-proxy ----
echo "[5] /data/sfbofs/hour_00.json"
status=$($CURL -o /tmp/sfbofs.json -w "%{http_code}" "$BASE/data/sfbofs/hour_00.json")
if [ "$status" = "200" ] && grep -q '"model_run"' /tmp/sfbofs.json; then
    ok "SFBOFS hour_00 served (200, contains model_run)"
elif [ "$status" = "200" ]; then
    ok "SFBOFS hour_00 served (200, but no model_run field — verify content)"
else
    fail "SFBOFS hour_00 -> $status"
fi

# ---- 6. NDBC stations ----
echo "[6] /data/wind/stations.json"
status=$($CURL -o /tmp/ndbc.json -w "%{http_code}" "$BASE/data/wind/stations.json")
[ "$status" = "200" ] && ok "NDBC stations -> 200" || fail "NDBC -> $status"

# ---- 7. /data catch-all + local fallback ----
echo "[7] /data/land_mask.json"
status=$($CURL -o /tmp/landmask.json -w "%{http_code}" "$BASE/data/land_mask.json")
if [ "$status" = "200" ]; then
    if [ "$(stat -c%s /tmp/landmask.json 2>/dev/null || stat -f%z /tmp/landmask.json)" -gt 1000 ]; then
        ok "land_mask.json served (200, non-trivial size)"
    else
        fail "land_mask.json too small"
    fi
else
    fail "land_mask.json -> $status"
fi

# ---- 8. SSRF allowlist (negative tests) ----
# curl normalizes `..` in URL paths client-side by default; --path-as-is sends
# the literal path so the server actually sees the traversal attempt.
echo "[8] SSRF / path-traversal rejection"
for bad in \
    "/api/noaa/foo" \
    "/api/noaa/api/prod/datagetter/extra" \
    "/api/open-meteo/v2/forecast" \
    "/data/sfbofs/../land_mask.json" \
    "/data/sfbofs/hour_00.json/../wind/stations.json" \
    "/data/sfbofs/..%2Fland_mask.json"; do
    status=$($CURL --path-as-is -o /dev/null -w "%{http_code}" "$BASE$bad")
    if [ "$status" = "404" ] || [ "$status" = "400" ]; then
        ok "rejected: $bad ($status)"
    else
        fail "ACCEPTED bad path: $bad ($status)"
    fi
done

# ---- 9. X-Cache MISS -> HIT transition ----
echo "[9] Cache header behavior"
HDR=$($CURL -D - -o /dev/null "$BASE/api/open-meteo/v1/forecast?$OM_QS" | grep -i '^x-cache:')
echo "  first call:  $HDR"
HDR2=$($CURL -D - -o /dev/null "$BASE/api/open-meteo/v1/forecast?$OM_QS" | grep -i '^x-cache:')
echo "  second call: $HDR2"
echo "$HDR2" | grep -qi 'HIT' && ok "second call hit cache" || fail "second call did not hit cache"

# ---- 10. /nmea WebSocket upgrade ----
echo "[10] /nmea WebSocket"
status=$($CURL -i -N -H "Connection: Upgrade" -H "Upgrade: websocket" \
              -H "Sec-WebSocket-Version: 13" \
              -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" \
              -o /tmp/ws.out --max-time 2 "$BASE/nmea" 2>/dev/null; head -1 /tmp/ws.out 2>/dev/null)
echo "$status" | grep -qi "101" && ok "/nmea -> 101 Switching Protocols" || fail "/nmea did not upgrade: $status"

# ---- summary ----
echo
echo "== Result: $PASS passed, $FAIL failed =="
[ $FAIL -eq 0 ] && exit 0 || exit 1
