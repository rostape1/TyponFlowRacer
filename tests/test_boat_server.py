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
import ast
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

# ---- 5b. /api/logs -------------------------------------------------------

section("/api/logs — the playback picker's index")

if not hasattr(bs, "handle_logs_api"):
    # The hub/playback change ships separately; without it this route does not
    # exist yet and these assertions are not applicable.
    skip("/api/logs assertions", "handle_logs_api not present in boat_server.py")
else:
    _logs_tmp = tempfile.mkdtemp()
    try:
        _ld = Path(_logs_tmp)
        (_ld / "nmea_2026-09-13_120000.txt").write_bytes(b"x" * 2048)
        (_ld / "nmea_2026-05-23_200145.txt").write_bytes(b"y" * 1024)
        # Must not be advertised: handle_log_file would refuse to serve them, so
        # listing them would offer the operator a recording they cannot open.
        (_ld / "notes.txt").write_bytes(b"z")
        (_ld / "nmea_2026-09-13_120000.txt.tmp").write_bytes(b"z")

        class _LogsRequest:
            def __init__(self, app):
                self.app = app

        _resp = asyncio.run(bs.handle_logs_api(_LogsRequest({"log_dir": _ld})))
        _data = json.loads(_resp.body)

        eq("lists only servable nmea_*.txt files", _data["count"], 2)
        ok("every listed name passes the serving allowlist",
           all(bs._LOG_FILE.match(f["name"]) for f in _data["files"]))
        ok("urls point at the download route",
           all(f["url"] == "/logs/" + f["name"] for f in _data["files"]))
        eq("total bytes sums the listed files", _data["bytes"], 3072)
        ok("newest first", _data["files"][0]["name"] > _data["files"][1]["name"])
        ok("reports free space", isinstance(_data["free"], int) and _data["free"] > 0)
        # A cached listing would claim recordings exist that don't, or hide new ones.
        eq("listing is uncacheable", _resp.headers.get("Cache-Control"), "no-store")
    finally:
        shutil.rmtree(_logs_tmp, ignore_errors=True)

section("Log retention — recordings are never auto-deleted")

# nmea_capture.py's KEEP_DAYS sweep was removed deliberately: NMEA logs are
# irreplaceable race data. The HTTP cache still prunes (P11); the logs must not.
# Parsed rather than grepped — the file's own comments discuss the deleted
# sweep by name, and a text search would match the explanation of the fix.
_cap_path = ROOT / "nmea_capture.py"
_cap_tree = ast.parse(_cap_path.read_text())

_cap_funcs = {n.name for n in ast.walk(_cap_tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
_cap_consts = {t.id for n in ast.walk(_cap_tree) if isinstance(n, ast.Assign)
               for t in n.targets if isinstance(t, ast.Name)}
_cap_calls = {ast.unparse(n.func) for n in ast.walk(_cap_tree) if isinstance(n, ast.Call)}

ok("no age-based log sweep function",
   not {"cleanup_old_logs", "cleanup_loop"} & _cap_funcs,
   f"found {sorted({'cleanup_old_logs', 'cleanup_loop'} & _cap_funcs)}")
ok("no KEEP_DAYS retention constant", "KEEP_DAYS" not in _cap_consts)
ok("nothing in nmea_capture deletes a file",
   not {"os.remove", "os.unlink", "shutil.rmtree"} & _cap_calls,
   f"found {sorted({'os.remove', 'os.unlink', 'shutil.rmtree'} & _cap_calls)}")
ok("free space is reported instead", "shutil.disk_usage" in _cap_calls)

# The 112d-uptime bug: elapsed time computed as wall clock minus wall clock,
# across the NTP step that corrects the Pi's clock at boot (it has no RTC).
ok("uptime is measured monotonically", "time.monotonic" in _cap_calls)
ok("uptime_seconds exists", "uptime_seconds" in _cap_funcs)

# Asserting time.monotonic is *called somewhere* is not enough — the bug could
# be reintroduced in status_html() while uptime_seconds() sits unused. There is
# no wall-clock start timestamp left to subtract, so assert that stays true.
_cap_subscripts = {ast.unparse(n) for n in ast.walk(_cap_tree)
                   if isinstance(n, ast.Subscript)}
ok("no wall-clock start timestamp exists to subtract",
   not any("start_time" in x for x in _cap_subscripts),
   f"found {[x for x in _cap_subscripts if 'start_time' in x]}")

_status_fn = next((n for n in ast.walk(_cap_tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "status_html"), None)
ok("status_html exists", _status_fn is not None)
if _status_fn is not None:
    _status_calls = {ast.unparse(n.func) for n in ast.walk(_status_fn)
                     if isinstance(n, ast.Call)}
    ok("status_html gets uptime from uptime_seconds()",
       "uptime_seconds" in _status_calls)
    ok("status_html never reads the wall clock",
       "time.time" not in _status_calls,
       "status_html calls time.time() — that is the 112-day bug")

# ---- 5d. The disk guard actually guards -----------------------------------

section("Disk alert — the guard that replaced deletion (P34)")

try:
    import importlib.util as _ilu
    sys.dont_write_bytecode = True          # P21
    _spec = _ilu.spec_from_file_location("nmea_capture", _cap_path)
    _cap = _ilu.module_from_spec(_spec)
    _spec.loader.exec_module(_cap)
except (SystemExit, ImportError) as _e:
    skip("disk alert assertions", f"nmea_capture not importable ({_e})")
    _cap = None

if _cap is not None:
    _dtmp = tempfile.mkdtemp()
    try:
        _cap.LOG_DIR = _dtmp
        # A stand-in for months of archive: the bug was dividing THIS by uptime.
        (Path(_dtmp) / "nmea_2026-05-23_200145.txt").write_bytes(b"x" * (8 * 1024 * 1024))

        # Fresh restart: too small a sample to project from. The old code read
        # "~0 hours left" next to a green free-space row.
        _cap.stats["start_monotonic"] = time.monotonic() - 90
        _cap.stats["bytes_written"] = 250_000
        _cap._disk_cache = None
        _d = _cap.disk_stats()
        ok("no headroom projection before the minimum sample", _d["rate"] is None)
        eq("and nothing is rendered for it", _cap.format_headroom(_d["free"], _d["rate"]), "")

        # With a real sample the rate must come from bytes this process wrote,
        # NOT from the size of the log directory.
        # bytes_written deliberately MUCH smaller than the archive on disk, so
        # the two candidate numerators give clearly different answers.
        _cap.stats["start_monotonic"] = time.monotonic() - 3600
        _cap.stats["bytes_written"] = 500 * 1024
        _cap._disk_cache = None
        _d = _cap.disk_stats()
        _expected = (500 * 1024) / 3600
        ok("headroom rate is bytes_written/uptime",
           abs(_d["rate"] - _expected) < _expected * 0.05,
           f"got {_d['rate']}, want ~{_expected}")
        ok("headroom rate is NOT the log directory total",
           _d["rate"] < _d["bytes"] / 3600 * 0.5,
           "rate looks like it came from the directory size")

        # An archive with no writes this session must not project at all.
        _cap.stats["bytes_written"] = 0
        _cap._disk_cache = None
        ok("no writes this session means no projection",
           _cap.disk_stats()["rate"] is None)

        # A failing write is a STORAGE fault. Reporting it as "Disconnected"
        # sends the operator to power-cycle the VHF instead of the card.
        _cap.stats.update(connected=True, source="ws://t", sentences=1,
                          current_file=str(Path(_dtmp) / "nmea_2026-05-23_200145.txt"),
                          write_error="OSError: [Errno 28] No space left on device")
        _cap._disk_cache = None
        _html = _cap.status_html()
        ok("a write failure headlines as a disk fault", "Logging FAILED" in _html)
        ok("and never simultaneously claims Connected", "🟢 Connected" not in _html)
        ok("and raises a loud banner", "LOGGING STOPPED" in _html)

        _cap.stats["write_error"] = None
        _cap._disk_cache = None
        ok("a healthy write reports Connected again",
           "🟢 Connected" in _cap.status_html())

        # Memoised: the page self-refreshes every 5s over an unbounded directory.
        _cap._disk_cache = None
        _first = _cap.disk_stats()
        ok("disk_stats is memoised", _cap.disk_stats() is _first)

        # static/hub.html re-implements the same amber/red thresholds in JS. That
        # is a hand-maintained mirror (P20's shape): change one and the hub says
        # 🟢 while the status page says amber, with nothing failing.
        _hub = (ROOT / "static" / "hub.html").read_text()
        _warn_gb = _cap.DISK_WARN_BYTES // 1024**3
        _crit_gb = _cap.DISK_CRIT_BYTES // 1024**3
        ok("hub.html mirrors DISK_WARN_BYTES",
           f"{_warn_gb} * 1024 ** 3" in _hub,
           f"expected '{_warn_gb} * 1024 ** 3' in hub.html")
        ok("hub.html mirrors DISK_CRIT_BYTES",
           "1024 ** 3" in _hub and _crit_gb == 1,
           f"DISK_CRIT_BYTES is {_crit_gb} GB; hub.html hardcodes 1024 ** 3")
    finally:
        shutil.rmtree(_dtmp, ignore_errors=True)

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

# ---- NMEA transports: UDP listener + bridge liveness (P40) ---------------
#
# The receiver's XPort serves TCP to one client at a time and refuses everyone
# else, so an abrupt Pi power-off can leave the slot claimed by a session nobody
# owns and lock the Pi out. UDP has no session to orphan. These tests pin the
# three things that make that switch safe: datagrams reassemble into sentences,
# only the configured receiver is trusted, and a bridge that dies says so
# instead of impersonating a silent receiver.

sys.path.insert(0, str(ROOT))
import socket as _socket  # noqa: E402
import nmea_ws_proxy as nwp  # noqa: E402

section("NMEA transports — UDP reassembly, source filter, liveness (P40)")

_src_bs = (ROOT / "pi" / "boat_server.py").read_text()
_tcp_def = re.search(r'"--tcp-port",\s*type=int,\s*default=(\d+)', _src_bs)
_udp_def = re.search(r'"--udp-port",\s*type=int,\s*default=(\d+)', _src_bs)
ok("both transports declare a default port", bool(_tcp_def and _udp_def))
if _tcp_def and _udp_def:
    # Flipping the XPort from TCP to UDP must not also mean changing a port, or
    # the config change lands on a closed door and looks like a dead receiver.
    eq("UDP default port matches the TCP default port",
       _udp_def.group(1), _tcp_def.group(1))
else:
    skip("UDP default port matches the TCP default port",
         "port defaults not parseable from boat_server.py")

# Absolute ceilings, not just "whatever the constant says" — a test written
# relative to the constant passes happily after someone raises it to 2**40.
ok("the UDP reassembly bound is an actual bound",
   nwp.UDP_BUFFER_MAX <= 1 << 20, f"got {nwp.UDP_BUFFER_MAX}")
ok("the UDP queue bound is an actual bound",
   0 < nwp.UDP_QUEUE_MAX <= 100_000, f"got {nwp.UDP_QUEUE_MAX}")


class _FakeQueue:
    """Queue stand-in recording what the datagram protocol enqueued."""

    def __init__(self, maxsize=0):
        self.items = []
        self.maxsize = maxsize

    def put_nowait(self, item):
        if self.maxsize and len(self.items) >= self.maxsize:
            raise asyncio.QueueFull
        self.items.append(item)

    def get_nowait(self):
        if not self.items:
            raise asyncio.QueueEmpty
        return self.items.pop(0)


_q = _FakeQueue()
nwp._NmeaDatagramProtocol(_q, allow_from="10.0.0.1").datagram_received(
    b"$GPGGA,1*11\r\n$GPRMC,2*22\r\n", ("10.0.0.1", 5))
eq("one datagram carrying two sentences yields two lines",
   _q.items, ["$GPGGA,1*11", "$GPRMC,2*22"])

_q = _FakeQueue()
_proto = nwp._NmeaDatagramProtocol(_q)
_proto.datagram_received(b"$GPGGA,partial", ("1.2.3.4", 5))
eq("a sentence split across datagrams is not emitted early", _q.items, [])
_proto.datagram_received(b"-rest*7F\r\n", ("1.2.3.4", 5))
eq("and is emitted once its terminator arrives",
   _q.items, ["$GPGGA,partial-rest*7F"])

# Position and AIS data steer a boat: an unfiltered UDP listener on the boat LAN
# would let anything inject a track.
_q = _FakeQueue()
nwp._NmeaDatagramProtocol(_q, allow_from="10.0.0.1").datagram_received(
    b"$GPGGA,spoofed*00\r\n", ("10.0.0.99", 5))
eq("datagrams from an unexpected source are ignored", _q.items, [])

_q = _FakeQueue()
_proto = nwp._NmeaDatagramProtocol(_q)
# Fixed size, deliberately NOT relative to the constant: sizing the payload off
# UDP_BUFFER_MAX means raising the constant makes this test attempt a huge
# allocation and crash instead of failing, which reads as a broken suite rather
# than a removed bound.
_proto.datagram_received(b"$GPGGA,keep*11\r\n" + b"x" * 70000, ("1.2.3.4", 5))
eq("an unterminated flood cannot grow the reassembly buffer",
   len(_proto._bufs.get("1.2.3.4", b"")), 0)
eq("but complete sentences in front of it survive the discard",
   _q.items, ["$GPGGA,keep*11"])

# Two sources interleaving must not have their partial sentences spliced
# together — that is how a junk AIVDM gets manufactured.
_q = _FakeQueue()
_proto = nwp._NmeaDatagramProtocol(_q)
_proto.datagram_received(b"$AAAAA,one", ("1.1.1.1", 5))
_proto.datagram_received(b"$BBBBB,two", ("2.2.2.2", 5))
_proto.datagram_received(b"-end*01\r\n", ("1.1.1.1", 5))
_proto.datagram_received(b"-end*02\r\n", ("2.2.2.2", 5))
eq("per-source reassembly keeps two senders from splicing",
   _q.items, ["$AAAAA,one-end*01", "$BBBBB,two-end*02"])

# A bare string must not degrade the membership test into a substring match.
_q = _FakeQueue()
nwp._NmeaDatagramProtocol(_q, allow_from="192.168.47.1").datagram_received(
    b"$GPGGA,x*00\r\n", ("2.168.47.", 5))
eq("a string allow_from is not treated as a substring filter", _q.items, [])

# Once the receiver is in UDP mode the TCP path fails forever by design. A fixed
# 5s retry would write ~17k lines a day into startup.log, which shares a
# directory with the race recordings and has no rotation (P11, P34).
eq("the first retry is still prompt", nwp.retry_delay(1), 5.0)
ok("the retry interval grows", nwp.retry_delay(3) > nwp.retry_delay(2),
   f"{nwp.retry_delay(2)} -> {nwp.retry_delay(3)}")
eq("and is capped rather than unbounded", nwp.retry_delay(50), nwp.RETRY_MAX_S)
ok("the cap is short enough to recover promptly", nwp.RETRY_MAX_S <= 120,
   f"got {nwp.RETRY_MAX_S}")

# Unversioned script URLs + no Cache-Control let a browser reuse stale JS after a
# deploy: the Pi serves the new code, Chrome runs the old one, and the missing
# feature has an empty console. Cost a debugging session on 2026-09-14.
async def _cache_headers():
    class _Req:
        def __init__(self, path):
            self.path = path

    async def _plain(_r):
        return web.Response(text="x")

    async def _api(_r):
        return web.Response(text="{}", headers={"Cache-Control": "no-store"})

    out = {}
    for path in ("/", "/hub", "/js/app.js", "/css/style.css", "/index.html",
                 "/tiles/noaa/12/1/2.png", "/favicon.ico"):
        r = await bs.revalidate_static(_Req(path), _plain)
        out[path] = r.headers.get("Cache-Control")
    # A handler that set its own stricter policy must win.
    r = await bs.revalidate_static(_Req("/api/health"), _api)
    out["_api"] = r.headers.get("Cache-Control")
    return out


from aiohttp import web  # noqa: E402
_ch = asyncio.run(_cache_headers())
eq("app.js must be revalidated, not heuristically cached",
   _ch["/js/app.js"], "no-cache")
eq("the page itself must be revalidated", _ch["/"], "no-cache")
eq("so must /hub", _ch["/hub"], "no-cache")
eq("and the stylesheet", _ch["/css/style.css"], "no-cache")
# Tiles are large and effectively immutable — they must stay cacheable offline.
eq("chart tiles stay cacheable", _ch["/tiles/noaa/12/1/2.png"], None)
eq("images stay cacheable", _ch["/favicon.ico"], None)
eq("a handler's own no-store is not downgraded", _ch["_api"], "no-store")


_q = _FakeQueue(maxsize=2)
_st_drop = {}
nwp._NmeaDatagramProtocol(_q, status=_st_drop).datagram_received(
    b"a*1\r\nb*2\r\nc*3\r\nd*4\r\n", ("1.2.3.4", 5))
eq("a full queue sheds lines instead of raising", len(_q.items), 2)
eq("the shed count is reported, not swallowed", _st_drop.get("dropped"), 2)
# Shedding the newest would leave the consumer replaying a stale backlog while
# health showed lines flowing — the exact "looks live and isn't" shape.
eq("the OLDEST lines are shed, so the feed stays current", _q.items, ["c*3", "d*4"])

# A rejected datagram must be visible in /api/health, not just on stdout:
# "refusing every datagram" and "receiver is silent" read identically otherwise.
_q = _FakeQueue()
_st_rej = {}
_proto = nwp._NmeaDatagramProtocol(_q, allow_from={"10.0.0.1"}, status=_st_rej)
_proto.datagram_received(b"$GPGGA,spoof*00\r\n", ("10.0.0.99", 5))
_proto.datagram_received(b"$GPGGA,spoof*00\r\n", ("10.0.0.99", 5))
eq("rejected datagrams are counted", _st_rej.get("rejected"), 2)
eq("and name the source that was refused", _st_rej.get("rejected_from"), "10.0.0.99")


def _free_udp_port():
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _udp_end_to_end():
    """Real socket, real datagram — the reassembly unit tests can't prove the
    listener actually binds and delivers."""
    port = _free_udp_port()
    got = []
    seen = {}

    async def send_fn(client, text):
        got.append(text)

    real_queue_cls = asyncio.Queue

    class _SpyQueue(real_queue_cls):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            seen["maxsize"] = self.maxsize

    status = {}
    asyncio.Queue = _SpyQueue
    try:
        task = asyncio.create_task(nwp.nmea_udp_broadcast(
            port, lambda: ["sink"], send_fn, bind_host="127.0.0.1",
            allow_from={"127.0.0.1"}, status=status))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if status.get("state") == "listening":
                break
    finally:
        asyncio.Queue = real_queue_cls
    tx = _socket.socket(_socket.AF_INET, _socket.SOCK_DGRAM)
    tx.sendto(b"!AIVDM,1,1,,A,15M,0*7B\r\n", ("127.0.0.1", port))
    for _ in range(100):
        await asyncio.sleep(0.02)
        if got:
            break
    tx.close()
    # Snapshot before cancelling: teardown legitimately moves state to "stopped".
    state_while_running = status.get("state")
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    return state_while_running, got, seen.get("maxsize")


_udp_state, _udp_got, _udp_maxsize = asyncio.run(_udp_end_to_end())
eq("the UDP listener binds and reports itself listening", _udp_state, "listening")
eq("a real datagram reaches the clients", _udp_got, ["!AIVDM,1,1,,A,15M,0*7B"])
# Without this, the real listener could be switched to an unbounded Queue and
# every other assertion here would still pass.
eq("the real listener queue is bounded by UDP_QUEUE_MAX",
   _udp_maxsize, nwp.UDP_QUEUE_MAX)


async def _tcp_survives_unexpected_error():
    """The bridge feeds the WebSocket fan-out and, through it, the logger. If a
    non-OSError kills it, the boat looks exactly like a silent receiver."""
    status = {}
    attempts = []

    async def boom(host, port):
        attempts.append(1)
        raise ValueError("not an OSError")

    async def send_fn(client, text):
        return None

    orig = asyncio.open_connection
    asyncio.open_connection = boom
    try:
        task = asyncio.create_task(nwp.nmea_tcp_broadcast(
            "1.2.3.4", 10110, lambda: [], send_fn, status=status))
        for _ in range(100):
            await asyncio.sleep(0.02)
            if status.get("last_error"):
                break
        alive = not task.done()
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        return alive, status, len(attempts)
    finally:
        asyncio.open_connection = orig


_alive, _tcp_status, _tries = asyncio.run(_tcp_survives_unexpected_error())
ok("an unexpected exception does not kill the TCP bridge", _alive)
ok("and it is reported rather than swallowed",
   "ValueError" in (_tcp_status.get("last_error") or ""),
   f"got {_tcp_status.get('last_error')!r}")
eq("the bridge is retrying, not dead", _tcp_status.get("state"), "retrying")


async def _task_states():
    async def boom():
        raise RuntimeError("bridge died")

    async def forever():
        await asyncio.sleep(60)

    dead = asyncio.create_task(boom())
    live = asyncio.create_task(forever())
    await asyncio.sleep(0.05)
    out = (bs._task_state(dead), bs._task_state(live), bs._task_state(None))
    live.cancel()
    try:
        await live
    except BaseException:
        pass
    return out


_dead, _live, _none = asyncio.run(_task_states())
eq("a crashed bridge task reports itself dead", _dead["alive"], False)
ok("with the reason attached", "bridge died" in (_dead["error"] or ""),
   f"got {_dead['error']!r}")
eq("a running bridge task reports alive", _live["alive"], True)
eq("a never-started task reports dead", _none["alive"], False)


# /api/health must actually surface all of this, or it is unreachable at sea.
_health_tmp = tempfile.mkdtemp(prefix="ais-health-")


# Without this, deleting the on_startup block that starts the UDP listener
# leaves every other assertion green while the boat silently reverts to
# single-client TCP — P40 re-introduced with a passing suite.
class _Args:
    def __init__(self, **kw):
        defaults = dict(
            port=8080, ssl_cert=None, ssl_key=None,
            tcp_host="127.0.0.1", tcp_port=10110,
            udp_port=_free_udp_port(), udp_allow_any=False, udp_bind="127.0.0.1",
            mmsi=1, cache_dir=str(Path(_health_tmp) / "cache"),
            log_dir=str(Path(_health_tmp) / "logs"),
            gh_pages_base="https://example.test",
            static_dir=str(ROOT / "static"),
        )
        defaults.update(kw)
        for k, v in defaults.items():
            setattr(self, k, v)


async def _wiring(**kw):
    app = bs.build_app(_Args(**kw))
    await bs.on_startup(app)
    try:
        return {
            "tcp_alive": bool(app.get("nmea_task")) and not app["nmea_task"].done(),
            "udp_task": app.get("nmea_udp_task"),
            "allow_from": app.get("udp_allow_from"),
        }
    finally:
        await bs.on_cleanup(app)


_w = asyncio.run(_wiring())
ok("on_startup actually starts the TCP bridge", _w["tcp_alive"])
ok("on_startup actually starts the UDP listener", _w["udp_task"] is not None)
eq("the UDP source filter defaults to the configured receiver",
   _w["allow_from"], ["127.0.0.1"])

_w_any = asyncio.run(_wiring(udp_allow_any=True))
eq("--udp-allow-any opens the filter, and only when asked",
   _w_any["allow_from"], None)

_w_off = asyncio.run(_wiring(udp_port=0))
ok("--udp-port 0 disables the listener", _w_off["udp_task"] is None)

# A hostname would be compared against a dotted quad and match nothing, so
# build_app must resolve it rather than storing the name.
_w_name = asyncio.run(_wiring(tcp_host="localhost"))
ok("a hostname --tcp-host is resolved to literal IPs for the filter",
   _w_name["allow_from"] == ["127.0.0.1"],
   f"got {_w_name['allow_from']!r}")


class _HealthRequest:
    def __init__(self, app):
        self.app = app


async def _health_payload():
    logs = Path(_health_tmp) / "healthlogs"
    logs.mkdir(parents=True, exist_ok=True)

    async def forever():
        await asyncio.sleep(60)

    tcp_task = asyncio.create_task(forever())
    app = {
        "log_dir": logs,
        "nmea_stats": {"lines": 7, "last_line_ts": time.time(), "last_line": "$GPGGA,1*11",
                       "transport": "udp", "tcp_lines": 0, "udp_lines": 7},
        "nmea_tcp_status": {"state": "retrying", "last_error": "ConnectionRefusedError: [111]"},
        "nmea_udp_status": {"state": "listening", "last_error": None, "dropped": 0},
        "nmea_task": tcp_task,
        "nmea_udp_task": None,
        "tcp_host": "192.168.47.10",
        "tcp_port": 10110,
        "udp_port": 10110,
        "udp_bind": "0.0.0.0",
        "udp_allow_from": ["192.168.47.10"],
        "nmea_clients": set(),
        "started_monotonic": time.monotonic(),
    }
    resp = await bs.handle_health(_HealthRequest(app))
    tcp_task.cancel()
    try:
        await tcp_task
    except BaseException:
        pass
    return json.loads(resp.body)


_health = asyncio.run(_health_payload())
_hn = _health["nmea"]
eq("health names the transport that is actually feeding", _hn.get("transport"), "udp")
eq("health keeps the original source field for continuity",
   _hn.get("source"), "192.168.47.10:10110")
eq("health reports the TCP refusal reason", _hn["tcp"].get("state"), "retrying")
ok("including the errno text",
   "ConnectionRefusedError" in (_hn["tcp"].get("last_error") or ""))
eq("health reports TCP bridge liveness", _hn["tcp"].get("alive"), True)
eq("a dead bridge stops advertising 'listening'", _hn["udp"].get("state"), "dead")
eq("health reports the UDP source filter", _hn["udp"].get("allow_from"),
   ["192.168.47.10"])

shutil.rmtree(_health_tmp, ignore_errors=True)

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
