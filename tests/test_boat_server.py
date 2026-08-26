#!/usr/bin/env python3
"""Regression tests for the Pi boat server (pi/boat_server.py).

No test framework, matching tests/*.mjs — plain asserts, `ok` lines, non-zero
exit on failure. Run: python3 tests/test_boat_server.py

Covers the failure modes that are invisible until you are offshore:

  1. Cross-language parity. TIDE_STATIONS / CURRENT_STATIONS / the wind grid and
     the NOAA + Open-Meteo URL shapes are hand-mirrored between
     static/js/data-loader.js and pi/boat_server.py. Pre-warming only works if
     the Pi's URL is byte-identical to the browser's, because the disk cache is
     keyed on SHA1(url). A one-character drift turns the whole pre-warm into a
     silent no-op that surfaces only as "no wind at sea".
  2. Date-shifted cache fallback. NOAA prediction URLs embed begin_date=<today
     UTC>, so at UTC midnight the exact key misses and the 30-day stale window
     was unreachable — every tide and current station went blank on day 2
     offline. The alias pointer must find the previous window.
  3. Captive portals. Marina and satcom WiFi answer 200 text/html. That body
     must never be cached, and must never shadow good JSON.
  4. Status handling. 404 has to pass through (the browser's SFBOFS download
     sweep reads it as "this hour was never published"), while 5xx and other
     4xx must fall through to stale-on-error.
  5. Single-flight, atomic writes, pruning, and the path allowlists.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_boat_server():
    import importlib.util
    # P21. Don't write pi/__pycache__: a cached .pyc whose source changed by the same
    # byte count (e.g. WIND_NX 11 → 12) can be reused, and the test then reads
    # stale code and passes against a bug it should catch.
    sys.dont_write_bytecode = True
    path = ROOT / "pi" / "boat_server.py"
    spec = importlib.util.spec_from_file_location("boat_server", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["boat_server"] = mod
    spec.loader.exec_module(mod)
    return mod


try:
    bs = _load_boat_server()
except SystemExit:
    print("aiohttp missing — install with: pip install -r pi/requirements.txt",
          file=sys.stderr)
    raise

# The failure paths under test log warnings by design; silence them so the
# output reads like tests/*.mjs. Set AIS_TEST_VERBOSE=1 to see them.
if not os.environ.get("AIS_TEST_VERBOSE"):
    import logging
    logging.getLogger().setLevel(logging.CRITICAL)
    bs.log.setLevel(logging.CRITICAL)

DATA_LOADER = (ROOT / "static" / "js" / "data-loader.js").read_text()

_failures = []
_skipped = []


def ok(label, cond, detail=""):
    if cond:
        print(f"  ok   {label}")
    else:
        print(f"  FAIL {label}" + (f" — {detail}" if detail else ""))
        _failures.append(label)


def eq(label, actual, expected):
    ok(label, actual == expected, f"got {actual!r}, want {expected!r}")


def skip(label, why):
    print(f"  skip {label} — {why}")
    _skipped.append(label)


def section(title):
    print(f"{title}:")


# ---- JS source extraction (mirrors how test_staleness.mjs reads JS) -------

def js_object_keys(name):
    """Keys of a `const NAME = { 'key': {...}, ... };` object literal."""
    m = re.search(r"const " + name + r" = \{(.*?)\n\};", DATA_LOADER, re.S)
    if not m:
        raise AssertionError(f"could not find {name} in data-loader.js")
    return re.findall(r"'([^']+)':", m.group(1))


def js_number(name):
    m = re.search(r"const " + name + r" = (-?[\d.]+);", DATA_LOADER)
    if not m:
        raise AssertionError(f"could not find {name} in data-loader.js")
    return float(m.group(1))


def js_wind_bounds():
    m = re.search(
        r"const WIND_BOUNDS = \{\s*south:\s*(-?[\d.]+),\s*north:\s*(-?[\d.]+),"
        r"\s*west:\s*(-?[\d.]+),\s*east:\s*(-?[\d.]+)\s*\};",
        DATA_LOADER)
    if not m:
        raise AssertionError("could not find WIND_BOUNDS in data-loader.js")
    return [float(g) for g in m.groups()]


# ---- 1. Cross-language parity --------------------------------------------

section("Cross-language parity — data-loader.js ↔ boat_server.py (P20)")

eq("tide station list matches data-loader.js",
   list(bs.TIDE_STATIONS), js_object_keys("TIDE_STATIONS"))
eq("current station list matches data-loader.js",
   list(bs.CURRENT_STATIONS), js_object_keys("CURRENT_STATIONS"))
ok("tide station list has no duplicates",
   len(set(bs.TIDE_STATIONS)) == len(bs.TIDE_STATIONS))

eq("WIND_NX matches", bs.WIND_NX, int(js_number("WIND_NX")))
eq("WIND_NY matches", bs.WIND_NY, int(js_number("WIND_NY")))
eq("wind bounds match",
   [bs.WIND_BOUNDS_S, bs.WIND_BOUNDS_N, bs.WIND_BOUNDS_W, bs.WIND_BOUNDS_E],
   js_wind_bounds())

# The browser's tide/current URLs are template literals; compare the fixed
# parameter tail, which is what actually has to agree character for character.
_tide_tail = "&product=predictions&datum=MLLW&units=english&time_zone=gmt&format=json&interval=6"
_curr_tail = "&product=currents_predictions&units=english&time_zone=gmt&format=json&interval=6"
ok("data-loader.js still builds the tide URL tail we mirror",
   _tide_tail in DATA_LOADER)
ok("data-loader.js still builds the current URL tail we mirror",
   _curr_tail in DATA_LOADER)
ok("_tide_url ends with that exact tail",
   bs._tide_url("9414290", "20260101", "20260104").endswith(_tide_tail),
   bs._tide_url("9414290", "20260101", "20260104"))
ok("_current_url ends with that exact tail",
   bs._current_url("SFB1201", "20260101", "20260104").endswith(_curr_tail),
   bs._current_url("SFB1201", "20260101", "20260104"))
ok("_tide_url puts begin/end/station ahead of the tail",
   bs._tide_url("9414290", "20260101", "20260104").startswith(
       bs.NOAA_BASE + "?begin_date=20260101&end_date=20260104&station=9414290"))

_om_tail = ("&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
            "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
            "&models=gfs_seamless&wind_speed_unit=kn&forecast_hours=49")
ok("_wind_url ends with the Open-Meteo param tail",
   bs._wind_url().endswith(_om_tail))
eq("_wind_url carries NX*NY coordinate pairs",
   len(re.search(r"latitude=([^&]+)", bs._wind_url()).group(1).split(",")),
   bs.WIND_NX * bs.WIND_NY)

# The real risk is float formatting: JS `.toFixed(4)` vs Python `f"{x:.4f}"`
# round differently at a tie. Build the browser's coordinate strings with node
# and require an exact match, since a divergence silently voids the cache key.
_node = shutil.which("node")
if _node:
    js = """
const WIND_BOUNDS = { south: %r, north: %r, west: %r, east: %r };
const WIND_NX = %d, WIND_NY = %d;
const lats = [], lons = [];
for (let iy = 0; iy < WIND_NY; iy++) {
    const lat = WIND_BOUNDS.south + iy * (WIND_BOUNDS.north - WIND_BOUNDS.south) / (WIND_NY - 1);
    for (let ix = 0; ix < WIND_NX; ix++) {
        const lon = WIND_BOUNDS.west + ix * (WIND_BOUNDS.east - WIND_BOUNDS.west) / (WIND_NX - 1);
        lats.push(lat.toFixed(4));
        lons.push(lon.toFixed(4));
    }
}
process.stdout.write(`latitude=${lats.join(',')}&longitude=${lons.join(',')}`);
""" % (bs.WIND_BOUNDS_S, bs.WIND_BOUNDS_N, bs.WIND_BOUNDS_W, bs.WIND_BOUNDS_E,
       bs.WIND_NX, bs.WIND_NY)
    js_coords = subprocess.run([_node, "-e", js], capture_output=True,
                               text=True, check=True).stdout
    py_coords = re.search(r"latitude=[^&]+&longitude=[^&]+", bs._wind_url()).group(0)
    ok("_wind_url coordinates are byte-identical to the browser's (node toFixed)",
       py_coords == js_coords,
       "float formatting diverged — the shared cache key is void")
else:
    skip("_wind_url coordinates byte-identical to browser's", "node not on PATH")

# ---- 2. Date window + alias key ------------------------------------------

section("NOAA date window and date-independent alias (P06)")

_begin, _end = bs._noaa_date_range_utc()
_t_begin, _t_end = bs._noaa_date_range_utc(1)
import datetime as _dt
_now = _dt.datetime.now(_dt.timezone.utc)
eq("begin_date is today in UTC", _begin, _now.strftime("%Y%m%d"))
eq("end_date is a 3-day window",
   _end, (_now + _dt.timedelta(days=3)).strftime("%Y%m%d"))
eq("day_offset=1 gives tomorrow's window",
   _t_begin, (_now + _dt.timedelta(days=1)).strftime("%Y%m%d"))
ok("data-loader.js still uses a 3-day floor for the same window",
   "Math.max(3, Math.ceil(minutesOffset / 1440) + 1)" in DATA_LOADER)

_a_today = bs._noaa_alias("9414290", "predictions", "6", "MLLW")
_a_tomorrow = bs._noaa_alias("9414290", "predictions", "6", "MLLW")
eq("alias is stable across date windows", _a_today, _a_tomorrow)
# Pinned literal: the end-to-end rollover test below can't detect a date that
# was injected into the alias, because both sides would compute it on the same
# day. This assertion is the real guard.
eq("alias has exactly the expected date-free shape",
   _a_today, "noaa:9414290:predictions:6:MLLW")
ok("alias excludes any date", not re.search(r"\d{8}", _a_today), _a_today)
ok("alias differs per station",
   _a_today != bs._noaa_alias("9414750", "predictions", "6", "MLLW"))
ok("alias differs per product",
   _a_today != bs._noaa_alias("9414290", "currents_predictions", "6", "MLLW"))
ok("alias differs per interval",
   _a_today != bs._noaa_alias("9414290", "predictions", "60", "MLLW"))

# ---- 3. TTL / stale-ceiling dispatch -------------------------------------

section("Per-source TTL and stale ceiling")

eq("water levels get the short TTL",
   bs._ttl_for_noaa("station=9414290&product=water_level&date=latest"),
   bs.TTL_WATER_LEVEL_S)
eq("predictions get the 6h TTL",
   bs._ttl_for_noaa("station=9414290&product=predictions"),
   bs.TTL_TIDES_CURRENTS_S)
eq("tide predictions tolerate 30 days stale",
   bs._max_stale_for_noaa("station=9414290&product=predictions"),
   bs.MAX_STALE_TIDES_S)
eq("current predictions tolerate 30 days stale",
   bs._max_stale_for_noaa("station=SFB1201&product=currents_predictions"),
   bs.MAX_STALE_CURRENTS_S)
eq("real-time gauges do NOT tolerate 30 days stale",
   bs._max_stale_for_noaa("station=9414290&product=water_level"),
   bs.MAX_STALE_WATER_LEVEL_S)
ok("a stale gauge reading expires long before a prediction does",
   bs.MAX_STALE_WATER_LEVEL_S < bs.MAX_STALE_TIDES_S)

# ---- 4. Content-type gate ------------------------------------------------

section("Captive-portal content-type gate (P07)")

for ct in ("application/json", "application/json; charset=utf-8",
           "text/json", "application/geo+json", "APPLICATION/JSON"):
    ok(f"accepts {ct!r}", bs._acceptable_json(ct))
for ct in ("text/html", "text/html; charset=iso-8859-1", "text/plain",
           "application/octet-stream", "", None):
    ok(f"rejects {ct!r}", not bs._acceptable_json(ct))

# ---- 5. Path allowlists --------------------------------------------------

section("Proxy path allowlists")

ok("NOAA tail accepts the real endpoint",
   bool(bs._NOAA_TAIL.match("api/prod/datagetter")))
for tail in ("api/prod/datagetter/../../evil", "api/prod/datagetter?x",
             "../etc/passwd", "api/prod/datagetterX", ""):
    ok(f"NOAA tail rejects {tail!r}", not bs._NOAA_TAIL.match(tail))
ok("Open-Meteo tail accepts the real endpoint",
   bool(bs._OPEN_METEO_TAIL.match("v1/forecast")))
ok("Open-Meteo tail rejects a traversal",
   not bs._OPEN_METEO_TAIL.match("v1/forecast/../../x"))
for name in ("hour_00.json", "land_mask.json", "meta.json", "a-b_c.1.json"):
    ok(f"data file allows {name!r}", bool(bs._DATA_FILE.match(name)))
for name in ("../secret", "a/b.json", "hour 00.json", "hour_00.json?x=1", ""):
    ok(f"data file rejects {name!r}", not bs._DATA_FILE.match(name))
for name in ("nmea_2026-04-15_210133.txt", "nmea_2026_04_15.txt"):
    ok(f"log file allows {name!r}", bool(bs._LOG_FILE.match(name)))
for name in ("nmea_../../etc/passwd", "boat_server.py", "nmea_x.txt.bak",
             "nmea_2026-04-15_210133.txt.tmp"):
    ok(f"log file rejects {name!r}", not bs._LOG_FILE.match(name))

# ---- 6. DiskCache --------------------------------------------------------

section("DiskCache (P10 atomic writes, P11 prune)")

_tmp = Path(tempfile.mkdtemp(prefix="ais-cache-test-"))
try:
    cache = bs.DiskCache(_tmp)
    URL = "https://example.test/a.json?station=1"

    ok("miss on an empty cache", cache.get(URL) is None)

    cache.put(URL, b'{"v":1}', "application/json; charset=utf-8", 200,
              alias="noaa:1:predictions:6:MLLW")
    got = cache.get(URL)
    ok("put/get round-trips the body", got is not None and got[1] == b'{"v":1}')
    eq("charset is stripped from the stored content type",
       got[0]["content_type"], "application/json")
    eq("status is stored", got[0]["status"], 200)
    ok("timestamp is recorded", abs(got[0]["ts"] - time.time()) < 5)
    ok("no .tmp files are left behind",
       not list(_tmp.glob("*.tmp")), [p.name for p in _tmp.glob('*.tmp')])

    ok("alias resolves to the entry",
       cache.get_alias("noaa:1:predictions:6:MLLW")[1] == b'{"v":1}')
    ok("unknown alias misses", cache.get_alias("noaa:9:x:6:MLLW") is None)

    # An entry written under yesterday's URL must still be reachable by alias —
    # this is the UTC-midnight blackout.
    OLD = "https://example.test/a.json?begin_date=20260101&station=1"
    NEW = "https://example.test/a.json?begin_date=20260102&station=1"
    c2 = bs.DiskCache(_tmp)
    c2.put(OLD, b'{"day":1}', "application/json", 200, alias="noaa:rollover")
    ok("tomorrow's exact URL misses", c2.get(NEW) is None)
    ok("but the alias still finds yesterday's body",
       c2.get_alias("noaa:rollover")[1] == b'{"day":1}')

    # touch() is the 304 path
    body_path, meta_path = cache._paths(URL)
    meta = json.loads(meta_path.read_text())
    meta["ts"] = time.time() - 9999
    meta_path.write_text(json.dumps(meta))
    cache.touch(URL)
    ok("touch refreshes the timestamp without rewriting the body",
       time.time() - cache.get(URL)[0]["ts"] < 5
       and cache.get(URL)[1] == b'{"v":1}')

    # prune: age sweep
    p_dir = Path(tempfile.mkdtemp(prefix="ais-prune-age-"))
    c3 = bs.DiskCache(p_dir)
    c3.put("https://x.test/old", b"O" * 10, "application/json", 200)
    c3.put("https://x.test/new", b"N" * 10, "application/json", 200)
    bp, mp = c3._paths("https://x.test/old")
    m = json.loads(mp.read_text())
    m["ts"] = time.time() - 40 * 24 * 3600
    mp.write_text(json.dumps(m))
    eq("prune removes the over-age entry",
       c3.prune(max_age_s=31 * 24 * 3600, max_bytes=10 ** 9), 1)
    ok("prune keeps the fresh entry", c3.get("https://x.test/new") is not None)
    ok("prune deleted the old body too", not bp.exists())
    shutil.rmtree(p_dir, ignore_errors=True)

    # prune: size cap, oldest first
    p_dir = Path(tempfile.mkdtemp(prefix="ais-prune-size-"))
    c4 = bs.DiskCache(p_dir)
    for i in range(4):
        c4.put(f"https://x.test/{i}", b"z" * 1000, "application/json", 200)
        bp, mp = c4._paths(f"https://x.test/{i}")
        m = json.loads(mp.read_text())
        m["ts"] = time.time() - (100 - i)   # 0 is oldest
        mp.write_text(json.dumps(m))
    removed = c4.prune(max_age_s=10 ** 9, max_bytes=2500)
    ok("prune evicts until under the byte cap", removed >= 2, f"removed {removed}")
    ok("prune evicted the oldest first", c4.get("https://x.test/0") is None)
    ok("prune kept the newest", c4.get("https://x.test/3") is not None)
    shutil.rmtree(p_dir, ignore_errors=True)

    ok("corrupt meta is treated as a miss (not an exception)",
       (lambda: (meta_path.write_text("{not json"), cache.get(URL))[1])() is None)
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

# ---- 7. proxy_with_cache -------------------------------------------------

section("proxy_with_cache — offline behaviour (P06, P07, P08, P10)")


class _CM:
    def __init__(self, outcome, delay=0.0):
        self.outcome, self.delay = outcome, delay

    async def __aenter__(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, *a):
        return False


class _Resp:
    def __init__(self, status, body, ct):
        self.status, self._body = status, body
        self.headers = {"Content-Type": ct}

    async def read(self):
        return self._body


class _Session:
    """Stands in for aiohttp.ClientSession; records every upstream GET."""

    def __init__(self, outcome, delay=0.0):
        self.outcome, self.delay, self.calls = outcome, delay, []

    def get(self, url, timeout=None):
        self.calls.append(url)
        out = self.outcome(url) if callable(self.outcome) else self.outcome
        return _CM(out, self.delay)


class _Request:
    def __init__(self, app):
        self.app = app


def run_proxy(session, cache, url="https://up.test/d.json", **kw):
    app = {"http": session, "inflight": {}}
    return asyncio.run(bs.proxy_with_cache(_Request(app), url, cache, **kw))


def body_of(resp):
    b = resp.body
    return bytes(b) if b is not None else b''


_tmp = Path(tempfile.mkdtemp(prefix="ais-proxy-test-"))
try:
    import aiohttp

    # Fresh cache → HIT, upstream never touched.
    c = bs.DiskCache(_tmp / "hit")
    c.put("https://up.test/d.json", b'{"cached":1}', "application/json", 200)
    s = _Session(_Resp(200, b'{"live":1}', "application/json"))
    r = run_proxy(s, c, ttl_s=3600)
    eq("fresh cache serves HIT", r.headers.get("X-Cache"), "HIT")
    eq("HIT does not call upstream", len(s.calls), 0)
    eq("HIT returns the cached body", body_of(r), b'{"cached":1}')

    # Cold cache, good JSON → MISS and cached.
    c = bs.DiskCache(_tmp / "miss")
    s = _Session(_Resp(200, b'{"live":1}', "application/json"))
    r = run_proxy(s, c, ttl_s=3600)
    eq("cold cache fetches upstream", r.headers.get("X-Cache"), "MISS")
    eq("MISS returns the live body", body_of(r), b'{"live":1}')
    ok("MISS writes through to disk",
       c.get("https://up.test/d.json")[1] == b'{"live":1}')

    # Captive portal: 200 text/html must not be cached and must not shadow JSON.
    c = bs.DiskCache(_tmp / "portal")
    c.put("https://up.test/d.json", b'{"good":1}', "application/json", 200)
    meta_path = c._paths("https://up.test/d.json")[1]
    m = json.loads(meta_path.read_text())
    m["ts"] = time.time() - 7200          # expired, so upstream is consulted
    meta_path.write_text(json.dumps(m))
    s = _Session(_Resp(200, b"<html>Sign in to WiFi</html>", "text/html"))
    r = run_proxy(s, c, ttl_s=3600, max_stale_s=30 * 24 * 3600)
    eq("captive portal falls back to cache", r.headers.get("X-Cache"), "STALE")
    eq("captive portal body is never served", body_of(r), b'{"good":1}')
    ok("captive portal body is never cached",
       c.get("https://up.test/d.json")[1] == b'{"good":1}')

    # 5xx → stale, not passed through.
    c = bs.DiskCache(_tmp / "5xx")
    c.put("https://up.test/d.json", b'{"good":1}', "application/json", 200)
    mp = c._paths("https://up.test/d.json")[1]
    m = json.loads(mp.read_text()); m["ts"] = time.time() - 7200
    mp.write_text(json.dumps(m))
    s = _Session(_Resp(503, b"upstream down", "text/plain"))
    r = run_proxy(s, c, ttl_s=3600, max_stale_s=30 * 24 * 3600)
    eq("503 serves stale cache", r.headers.get("X-Cache"), "STALE")
    eq("503 does not leak the error body", body_of(r), b'{"good":1}')

    # 404 must pass through — the SFBOFS sweep depends on it.
    c = bs.DiskCache(_tmp / "404")
    s = _Session(_Resp(404, b"not found", "text/plain"))
    r = run_proxy(s, c, ttl_s=3600)
    eq("404 passes through as 404", r.status, 404)
    ok("404 is not cached", c.get("https://up.test/d.json") is None)

    # Network error with no cache at all → 504, never a fake 200.
    c = bs.DiskCache(_tmp / "dead")
    s = _Session(aiohttp.ClientConnectionError("no route to host"))
    r = run_proxy(s, c, ttl_s=3600)
    eq("offline with an empty cache returns 504", r.status, 504)

    # THE ROLLOVER BUG: exact key misses, alias saves the day. The alias is
    # derived with _noaa_alias for both windows, so this covers the derivation
    # too — putting the date back into the alias key must break this test.
    c = bs.DiskCache(_tmp / "rollover")
    y_begin, y_end = bs._noaa_date_range_utc(-1)
    t_begin, t_end = bs._noaa_date_range_utc()
    OLD = bs._tide_url("9414290", y_begin, y_end)
    NEW = bs._tide_url("9414290", t_begin, t_end)
    ok("the two windows really are different URLs", OLD != NEW)
    alias_old = bs._noaa_alias("9414290", "predictions", "6", "MLLW")
    c.put(OLD, b'{"predictions":1}', "application/json", 200, alias=alias_old)
    s = _Session(aiohttp.ClientConnectionError("satcom down"))
    r = run_proxy(s, c, url=NEW, ttl_s=3600, max_stale_s=30 * 24 * 3600,
                  alias=bs._noaa_alias("9414290", "predictions", "6", "MLLW"))
    eq("UTC rollover still serves tides offline", r.status, 200)
    eq("rollover is served as STALE", r.headers.get("X-Cache"), "STALE")
    eq("rollover names the reason",
       r.headers.get("X-Cache-Reason"), "upstream-error-date-shift")
    eq("rollover returns the previous window's body",
       body_of(r), b'{"predictions":1}')

    # Without an alias the rollover is unrecoverable — proves the alias is load-bearing.
    c = bs.DiskCache(_tmp / "rollover-noalias")
    c.put(OLD, b'{"predictions":1}', "application/json", 200)
    s = _Session(aiohttp.ClientConnectionError("satcom down"))
    r = run_proxy(s, c, url=NEW, ttl_s=3600, max_stale_s=30 * 24 * 3600)
    eq("no alias means the rollover still 504s (regression guard)", r.status, 504)

    # Past the stale ceiling, refuse rather than lie.
    c = bs.DiskCache(_tmp / "ceiling")
    c.put("https://up.test/d.json", b'{"ancient":1}', "application/json", 200)
    mp = c._paths("https://up.test/d.json")[1]
    m = json.loads(mp.read_text()); m["ts"] = time.time() - 40 * 24 * 3600
    mp.write_text(json.dumps(m))
    s = _Session(aiohttp.ClientConnectionError("down"))
    r = run_proxy(s, c, ttl_s=3600, max_stale_s=24 * 3600)
    eq("beyond the stale ceiling returns 504, not stale data", r.status, 504)

    # Single-flight: 20 concurrent callers, one upstream fetch.
    c = bs.DiskCache(_tmp / "inflight")
    s = _Session(_Resp(200, b'{"live":1}', "application/json"), delay=0.05)

    async def _burst():
        app = {"http": s, "inflight": {}}
        reqs = [bs.proxy_with_cache(_Request(app), "https://up.test/d.json", c,
                                    ttl_s=3600) for _ in range(20)]
        return await asyncio.gather(*reqs)

    results = asyncio.run(_burst())
    eq("20 concurrent requests cause 1 upstream fetch", len(s.calls), 1)
    ok("every concurrent caller gets the body",
       all(body_of(x) == b'{"live":1}' for x in results))
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

# ---- Result --------------------------------------------------------------

print()
_total = len(_failures)
_msg = f"{_total} failed" if _total else "all passed"
if _skipped:
    _msg += f", {len(_skipped)} skipped"
print(_msg)
if _failures:
    for f in _failures:
        print(f"  - {f}")
    sys.exit(1)
