#!/usr/bin/env python3
"""Boat-mode server for the AIS Tracker.

Runs on the Raspberry Pi on the boat WiFi. Single asyncio process that:

  1. Serves the static AIS Tracker UI (HTTP by default, HTTPS optional via
     --ssl-cert/--ssl-key) from ../static.
  2. Synthesizes /config.json so the browser switches to boat mode
     (local NMEA AIS, no AISstream.io, env data via this Pi's reverse proxy).
  3. Reverse-proxies + disk-caches NOAA CO-OPS and Open-Meteo so all clients
     on the boat WiFi share one fetch and the cache survives flaky satcom.
  4. Reverse-proxies + disk-caches the pre-computed SFBOFS / NDBC / meta JSON
     hosted on GitHub Pages, with a startup-time SFBOFS pre-warm.
  5. Bridges the local NMEA TCP stream (default 192.168.47.10:10110) to a
     WebSocket at /nmea for the browser.

Usage:
    python3 pi/boat_server.py                       # HTTP on :8080
    python3 pi/boat_server.py --ssl-cert certs/server.crt --ssl-key certs/server.key
"""

import argparse
import asyncio
import datetime as _dt
import hashlib
import html
import json
import logging
import os
import re
import shutil
import ssl
import sys
import time
from pathlib import Path
from typing import Optional

try:
    import aiohttp
    from aiohttp import web
except ImportError:
    print("Install aiohttp: pip install -r pi/requirements.txt", file=sys.stderr)
    raise SystemExit(1)

# Reuse the TCP→broadcast core from the legacy proxy module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from nmea_ws_proxy import nmea_tcp_broadcast  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("boat")

# ---- TTLs ---------------------------------------------------------------
# Match the in-memory caches in static/js/data-loader.js so behavior is
# consistent whether a client hits the proxy or its own JS cache.
TTL_TIDES_CURRENTS_S = 6 * 3600         # NOAA predictions
TTL_WATER_LEVEL_S = 10 * 60             # NOAA real-time gauges
TTL_OPEN_METEO_S = 30 * 60              # wind grid
TTL_SFBOFS_S = 60 * 60                  # 1 h, also pre-warmed hourly
TTL_NDBC_S = 10 * 60
TTL_META_S = 60

UPSTREAM_TIMEOUT_S = 10.0
# /data/* bodies run to ~1.2 MB. A *total* timeout aborts them mid-transfer over
# satcom, so every hour fails and the whole 49-file sweep restarts forever. Bound
# the handshake and each individual socket read instead of the whole transfer.
DATA_SOCK_CONNECT_S = 10.0
DATA_SOCK_READ_S = 60.0
# Refuse to serve cache older than this on upstream error: stale data is fine,
# day-old data masquerading as current is dangerous on the water.
# Split per-source — coastal cruising can run a week without internet, but the
# usefulness of stale data depends on what kind of data it is.
MAX_STALE_DEFAULT_S = 24 * 3600
MAX_STALE_TIDES_S = 30 * 24 * 3600   # harmonic predictions, stable for weeks
MAX_STALE_CURRENTS_S = 30 * 24 * 3600  # same
MAX_STALE_WATER_LEVEL_S = 24 * 3600    # real-time gauge — stale = misleading
MAX_STALE_OPEN_METEO_S = 7 * 24 * 3600
MAX_STALE_SFBOFS_S = 7 * 24 * 3600     # SFBOFS only forecasts 48h anyway
MAX_STALE_NDBC_S = 7 * 24 * 3600
MAX_STALE_META_S = 24 * 3600

# Disk-cache housekeeping. Nothing used to be deleted, so date-stamped NOAA URLs
# accumulated (~22/day) and any unauthenticated LAN client could mint unlimited
# entries with junk query params and fill the SD card — which also stops NMEA
# logging. Age cap stays above MAX_STALE_TIDES_S so the stale window survives.
CACHE_MAX_AGE_S = 31 * 24 * 3600
CACHE_MAX_BYTES = 1024 * 1024 * 1024   # 1 GB

# Pre-warm pacing
PREWARM_OK_INTERVAL_S = 3600            # next cycle when last cycle succeeded
PREWARM_FAIL_BACKOFF_S = (60, 120, 240, 300, 300)  # exponential, capped
PREWARM_REQUEST_GAP_S = 0.2             # be polite to GH Pages

DATA_TIMEOUT = aiohttp.ClientTimeout(
    sock_connect=DATA_SOCK_CONNECT_S, sock_read=DATA_SOCK_READ_S
)
DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT_S)


def _acceptable_json(content_type: str) -> bool:
    """True when the upstream body is plausibly the JSON the route expects.

    Pitfall P07. A captive-portal login page comes back as 200 text/html; caching it would
    overwrite good JSON and then re-serve the login page as STALE for weeks.
    """
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct in ("application/json", "text/json") or ct.endswith("+json")


# ---- Disk cache ---------------------------------------------------------

class DiskCache:
    """Simple SHA1-keyed disk cache with stale-on-error semantics."""

    def __init__(self, cache_dir: Path):
        self.dir = cache_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def _key(self, url: str) -> str:
        return hashlib.sha1(url.encode("utf-8")).hexdigest()

    def _paths(self, url: str):
        k = self._key(url)
        return self.dir / f"{k}.bin", self.dir / f"{k}.meta.json"

    def _alias_path(self, alias: str) -> Path:
        return self.dir / f"{self._key(alias)}.alt.json"

    def _write_atomic(self, path: Path, data: bytes):
        """Write via a .tmp sibling + os.replace so readers never see a torn body. (P10)"""
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, url: str):
        body_path, meta_path = self._paths(url)
        if not body_path.exists() or not meta_path.exists():
            return None
        try:
            meta = json.loads(meta_path.read_text())
            body = body_path.read_bytes()
            return meta, body
        except (OSError, ValueError):
            return None

    def get_alias(self, alias: str):
        """Newest entry stored under a date-independent alias, if any. (P06)

        NOAA prediction URLs embed begin_date=<today UTC>, so after midnight the
        exact key misses and the 30-day stale window would be unreachable. The
        alias pointer lets the stale-on-error branch find the previous window.
        """
        try:
            ptr = json.loads(self._alias_path(alias).read_text())
        except (OSError, ValueError):
            return None
        url = ptr.get("url")
        if not url:
            return None
        return self.get(url)

    def put(self, url: str, body: bytes, content_type: str, status: int,
            alias: Optional[str] = None, last_modified: Optional[str] = None):
        body_path, meta_path = self._paths(url)
        # aiohttp's web.Response(content_type=...) rejects charset, so strip
        # it here once at write time rather than on every cache hit.
        ct = (content_type or "application/octet-stream").split(";")[0].strip()
        try:
            # Body first, then meta — both atomic, so a crash between them
            # leaves a complete body with the previous timestamp, never a
            # fresh timestamp pointing at a half-written body.
            self._write_atomic(body_path, body)
            self._write_atomic(meta_path, json.dumps({
                "ts": time.time(),
                "content_type": ct,
                "status": status,
                "last_modified": last_modified,
                "url": url,
            }).encode("utf-8"))
            if alias:
                self._write_atomic(
                    self._alias_path(alias),
                    json.dumps({"url": url, "ts": time.time()}).encode("utf-8"),
                )
        except OSError as e:
            log.warning("cache write failed for %s: %s", url, e)

    def touch(self, url: str):
        """Refresh an entry's timestamp — upstream answered 304 Not Modified."""
        _body_path, meta_path = self._paths(url)
        try:
            meta = json.loads(meta_path.read_text())
            meta["ts"] = time.time()
            self._write_atomic(meta_path, json.dumps(meta).encode("utf-8"))
        except (OSError, ValueError) as e:
            log.warning("cache touch failed for %s: %s", url, e)

    def prune(self, max_age_s: float, max_bytes: int) -> int:
        """Delete over-age entries, then oldest-first until under `max_bytes`. (P11)

        This applies to the HTTP cache only — cached copies of upstream data
        that can always be refetched. NMEA logs are the opposite: irreplaceable
        race recordings, never deleted, guarded by a free-space alert on
        nmea_capture.py's status page instead.
        """
        now = time.time()
        removed = 0
        entries = []

        def _unlink(*paths):
            for p in paths:
                try:
                    p.unlink()
                except OSError:
                    pass

        for body_path in self.dir.glob("*.bin"):
            meta_path = self.dir / f"{body_path.stem}.meta.json"
            try:
                ts = float(json.loads(meta_path.read_text())["ts"])
                size = body_path.stat().st_size
            except (OSError, ValueError, KeyError, TypeError):
                ts, size = 0.0, 0
            if now - ts > max_age_s:
                _unlink(body_path, meta_path)
                removed += 1
                continue
            entries.append((ts, size, body_path, meta_path))

        total = sum(e[1] for e in entries)
        for _ts, size, body_path, meta_path in sorted(entries, key=lambda e: e[0]):
            if total <= max_bytes:
                break
            _unlink(body_path, meta_path)
            total -= size
            removed += 1

        if removed:
            log.info("cache prune: removed %d entries, %.0f MB remaining",
                     removed, total / (1024 * 1024))
        return removed


# ---- Single-flight upstream fetch ---------------------------------------

async def _fetch_upstream(app: web.Application, url: str,
                          timeout: "aiohttp.ClientTimeout"):
    """Fetch `url` once, even when several clients ask at the same moment. (P10)

    Returns (status, body, content_type), or None when the fetch failed.
    Concurrent callers for the same URL await the first fetch instead of each
    opening their own connection — a cold cache otherwise meant 49 simultaneous
    upstream fetches per client.
    """
    inflight: dict = app["inflight"]
    existing = inflight.get(url)
    if existing is not None:
        return await asyncio.shield(existing)

    fut = asyncio.get_running_loop().create_future()
    inflight[url] = fut
    result = None
    try:
        session: aiohttp.ClientSession = app["http"]
        async with session.get(url, timeout=timeout) as resp:
            body = await resp.read()
            result = (
                resp.status, body,
                resp.headers.get("Content-Type", "application/octet-stream"),
            )
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("upstream failed for %s: %s", url, e)
    finally:
        inflight.pop(url, None)
        if not fut.done():
            fut.set_result(result)
    return result


# ---- Reverse-proxy helper ------------------------------------------------

async def proxy_with_cache(
    request: web.Request,
    upstream_url: str,
    cache: DiskCache,
    ttl_s: float,
    max_stale_s: float = MAX_STALE_DEFAULT_S,
    timeout: Optional["aiohttp.ClientTimeout"] = None,
    alias: Optional[str] = None,
) -> web.Response:
    """Fetch `upstream_url`, serving from disk cache when fresh or on failure."""
    cached = cache.get(upstream_url)
    now = time.time()

    if cached:
        meta, body = cached
        age = now - meta["ts"]
        if age < ttl_s:
            return web.Response(
                body=body,
                status=meta.get("status", 200),
                content_type=meta.get("content_type", "application/json"),
                headers={"X-Cache": "HIT", "X-Cache-Age": f"{int(age)}"},
            )

    result = await _fetch_upstream(request.app, upstream_url, timeout or DEFAULT_TIMEOUT)
    if result is not None:
        status, body, ct = result
        if 200 <= status < 300:
            if _acceptable_json(ct):
                cache.put(upstream_url, body, ct, status, alias=alias)
                return web.Response(
                    body=body, status=status,
                    content_type=ct.split(";")[0].strip(),
                    headers={"X-Cache": "MISS"},
                )
            # Don't cache and don't serve — a captive portal answering 200
            # text/html must not overwrite or shadow good JSON.
            log.warning("upstream %s returned %s, not JSON (captive portal?)",
                        upstream_url, ct)
        elif status == 404:
            # P08/P03. Pass a real 404 through: the browser's SFBOFS download sweep
            # relies on 404 meaning "this hour was never published".
            return web.Response(
                body=body, status=404,
                content_type=ct.split(";")[0].strip(),
                headers={"X-Cache": "MISS"},
            )
        else:
            log.warning("upstream %s returned status %d", upstream_url, status)

    # Upstream unusable (network error, 5xx, non-404 4xx, or wrong body type).
    stale = cached
    reason = "upstream-error"
    if stale is None and alias:
        stale = cache.get_alias(alias)
        reason = "upstream-error-date-shift"
    if stale:
        meta, body = stale
        stale_age = now - meta["ts"]
        if stale_age <= max_stale_s:
            return web.Response(
                body=body,
                status=meta.get("status", 200),
                content_type=meta.get("content_type", "application/json"),
                headers={
                    "X-Cache": "STALE",
                    "X-Cache-Age": f"{int(stale_age)}",
                    "X-Cache-Reason": reason,
                },
            )
        log.warning("cache for %s exceeds MAX_STALE (%.0fh), refusing",
                    upstream_url, stale_age / 3600)
    return web.Response(status=504, text="upstream unreachable, no fresh cache")


def _ttl_for_noaa(query: str) -> float:
    if "product=water_level" in query:
        return TTL_WATER_LEVEL_S
    return TTL_TIDES_CURRENTS_S


def _max_stale_for_noaa(query: str) -> float:
    if "product=water_level" in query:
        return MAX_STALE_WATER_LEVEL_S
    if "product=currents_predictions" in query:
        return MAX_STALE_CURRENTS_S
    return MAX_STALE_TIDES_S


def _noaa_alias(station: str, product: str, interval: str, datum: str) -> str:
    """Date-independent secondary cache key for a NOAA datagetter request.

    Pitfall P06. begin_date/end_date are deliberately excluded: they roll over at UTC
    midnight, which would otherwise turn every pre-warmed tide and current
    entry into a cache miss and blank all the stations while offline.
    """
    return f"noaa:{station}:{product}:{interval}:{datum}"


# Tail allowlists. The proxy {tail:.*} regex would otherwise let a LAN client
# coerce the upstream URL via path traversal (..%2F..) or `@evil.com` tricks
# and poison shared cache entries. Lock to known endpoints only.
_NOAA_TAIL = re.compile(r"^api/prod/datagetter$")
_OPEN_METEO_TAIL = re.compile(r"^v1/forecast$")
# Single safe filename: alnum, dot, dash, underscore. Disallows slashes and ..
_DATA_FILE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _reject_invalid_tail() -> web.Response:
    return web.Response(status=404, text="not found")


# ---- Route handlers ------------------------------------------------------

async def handle_cert(request: web.Request) -> web.Response:
    cert_path = Path(request.app["ssl_cert"])
    if not cert_path.exists():
        return web.Response(status=404, text="cert not found")
    return web.FileResponse(
        cert_path,
        headers={"Content-Disposition": 'attachment; filename="ais-tracker.crt"'},
    )


async def handle_config(request: web.Request) -> web.Response:
    cfg = request.app["config_payload"]
    return web.json_response(cfg, headers={"Cache-Control": "no-store"})


async def handle_proxy_noaa(request: web.Request) -> web.Response:
    tail = request.match_info["tail"]
    if not _NOAA_TAIL.match(tail):
        return _reject_invalid_tail()
    qs = request.rel_url.query_string
    q = request.rel_url.query
    alias = None
    if q.get("station") and q.get("product"):
        alias = _noaa_alias(q["station"], q["product"],
                            q.get("interval", ""), q.get("datum", ""))
    upstream = f"https://api.tidesandcurrents.noaa.gov/{tail}"
    if qs:
        upstream = f"{upstream}?{qs}"
    return await proxy_with_cache(
        request, upstream, request.app["cache"],
        ttl_s=_ttl_for_noaa(qs),
        max_stale_s=_max_stale_for_noaa(qs),
        alias=alias,
    )


async def handle_proxy_open_meteo(request: web.Request) -> web.Response:
    tail = request.match_info["tail"]
    if not _OPEN_METEO_TAIL.match(tail):
        return _reject_invalid_tail()
    qs = request.rel_url.query_string
    upstream = f"https://api.open-meteo.com/{tail}"
    if qs:
        upstream = f"{upstream}?{qs}"
    return await proxy_with_cache(
        request, upstream, request.app["cache"],
        ttl_s=TTL_OPEN_METEO_S,
        max_stale_s=MAX_STALE_OPEN_METEO_S,
    )


def _make_gh_pages_handler(subpath: str, ttl_s: float, max_stale_s: float = MAX_STALE_DEFAULT_S):
    async def handler(request: web.Request) -> web.Response:
        tail = request.match_info["tail"]
        if not _DATA_FILE.match(tail):
            return _reject_invalid_tail()
        base = request.app["gh_pages_base"].rstrip("/")
        upstream = f"{base}/{subpath}/{tail}"
        return await proxy_with_cache(
            request, upstream, request.app["cache"],
            ttl_s=ttl_s, max_stale_s=max_stale_s, timeout=DATA_TIMEOUT,
        )
    return handler


async def handle_data_catchall(request: web.Request) -> web.Response:
    """Generic /data/* — tries cache → GH Pages → local static copy.

    Specific subpaths (/data/sfbofs/*, /data/wind/*, etc.) are matched by
    earlier routes. This catches everything else (e.g. land_mask.json) so
    the route optimizer keeps working whether or not the Pi has internet.
    """
    tail = request.match_info["tail"]
    if not _DATA_FILE.match(tail):
        return _reject_invalid_tail()
    base = request.app["gh_pages_base"].rstrip("/")
    upstream = f"{base}/data/{tail}"
    resp = await proxy_with_cache(
        request, upstream, request.app["cache"],
        ttl_s=TTL_SFBOFS_S, max_stale_s=MAX_STALE_SFBOFS_S, timeout=DATA_TIMEOUT,
    )
    if resp.status != 504:
        return resp
    # Last resort: serve the copy committed under static/data/.
    local = request.app["static_dir"] / "data" / tail
    if local.exists() and local.is_file():
        log.info("serving %s from local static fallback", tail)
        return web.FileResponse(local, headers={"X-Cache": "LOCAL"})
    return resp


async def handle_meta(request: web.Request) -> web.Response:
    base = request.app["gh_pages_base"].rstrip("/")
    upstream = f"{base}/data/meta.json"
    return await proxy_with_cache(
        request, upstream, request.app["cache"],
        ttl_s=TTL_META_S, max_stale_s=MAX_STALE_META_S,
    )


# ---- NMEA log download ---------------------------------------------------

_LOG_FILE = re.compile(r"^nmea_[\d_-]+\.txt$")


def _tzname() -> str:
    """Local zone abbreviation (PDT/PST here) for labelling displayed times."""
    return time.strftime("%Z") or "local"


async def handle_logs_index(request: web.Request) -> web.Response:
    log_dir: Path = request.app["log_dir"]
    files = sorted(log_dir.glob("nmea_*.txt"), reverse=True)

    def fmt_size(b):
        return f"{b/1024:.0f} KB" if b < 1024 * 1024 else f"{b/1024/1024:.1f} MB"

    rows = ""
    for f in files:
        # Only list names /logs/{filename} will actually serve.
        if not _LOG_FILE.match(f.name):
            continue
        try:
            stat = f.stat()
        except OSError:
            continue  # rotated away mid-listing, or still being written
        size = fmt_size(stat.st_size)
        # Local time, matching the filenames (which are local) and the capture
        # status page. Sentence timestamps inside the files are UTC with a Z.
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
        name = html.escape(f.name)
        rows += (
            f"<tr><td><a href='/logs/{name}'>{name}</a></td>"
            f"<td>{mtime}</td><td>{size}</td></tr>\n"
        )
    if not rows:
        rows = "<tr><td colspan='3' style='color:#8395a7'>No log files yet</td></tr>"

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>NMEA Logs</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,system-ui,sans-serif;background:#0a1628;color:#c8d6e5;padding:20px}}
h1{{font-size:1.4em;margin-bottom:16px;color:#f5f6fa}}
table{{width:100%;border-collapse:collapse;background:rgba(255,255,255,0.05);border-radius:10px;overflow:hidden}}
th{{text-align:left;padding:10px 14px;font-size:0.8em;color:#8395a7;border-bottom:1px solid rgba(255,255,255,0.1)}}
td{{padding:10px 14px;font-size:0.9em;border-bottom:1px solid rgba(255,255,255,0.05)}}
td:nth-child(2),td:nth-child(3){{color:#8395a7;white-space:nowrap}}
a{{color:#3498db;text-decoration:none}}
a:hover{{text-decoration:underline}}
tr:last-child td{{border-bottom:none}}
</style>
</head>
<body>
<h1>NMEA Logs</h1>
<p style="color:#8395a7;font-size:0.8em;margin-bottom:12px">Filenames and dates below are local
time ({_tzname()}). Timestamps inside each file are UTC, marked with a trailing Z.</p>
<table>
<thead><tr><th>File</th><th>Modified ({_tzname()})</th><th>Size</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body>
</html>"""
    return web.Response(text=page, content_type="text/html")


async def handle_log_file(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    if not _LOG_FILE.match(filename):
        return _reject_invalid_tail()
    log_dir: Path = request.app["log_dir"]
    path = log_dir / filename
    if not path.exists():
        return _reject_invalid_tail()
    return web.FileResponse(
        path,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _process_alive(needle: str) -> Optional[dict]:
    """Find a running process whose command line contains `needle`.

    Reads /proc directly rather than shelling out to pgrep: no subprocess, works
    in the aiohttp event loop, and degrades to None off Linux (so the test suite
    on macOS just reports unknown rather than failing).
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    now = time.time()
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(
                "utf-8", "replace")
        except OSError:
            continue
        if needle not in cmdline:
            continue
        try:
            started = entry.stat().st_mtime
        except OSError:
            started = None
        return {
            "pid": int(entry.name),
            "cmdline": cmdline.strip()[:200],
            "age_s": int(now - started) if started else None,
        }
    return None


async def handle_health(request: web.Request) -> web.Response:
    """What is actually running on this Pi, answerable over HTTP.

    Exists because the Pi's SSH password was lost on 2026-09-13, which made
    journalctl unreachable and left "the voyage recorder stopped, why?"
    undiagnosable. Anything needed to answer that has to be reachable here.
    """
    log_dir: Path = request.app["log_dir"]

    logger = _process_alive("nmea_capture.py")
    newest = None
    try:
        files = sorted((f for f in log_dir.glob("nmea_*.txt") if _LOG_FILE.match(f.name)),
                       key=lambda f: f.stat().st_mtime, reverse=True)
        if files:
            st = files[0].stat()
            newest = {
                "name": files[0].name,
                "bytes": st.st_size,
                "mtime": int(st.st_mtime),
                # The number that actually matters: is anything being written?
                "age_s": int(time.time() - st.st_mtime),
            }
    except OSError:
        pass

    try:
        usage = shutil.disk_usage(log_dir)
        free, capacity = usage.free, usage.total
    except OSError:
        free = capacity = None

    boot_log = None
    try:
        text = (log_dir / "startup.log").read_text(errors="replace")
        boot_log = text.splitlines()[-40:]
    except OSError:
        pass

    # "Recording" is the honest bottom line: a live process that is not writing
    # is not recording, and that distinction is the whole point of this endpoint.
    recording = bool(newest and newest["age_s"] < 120)

    return web.json_response(
        {
            "recording": recording,
            "logger": logger,          # None = not running (or not on Linux)
            "newest_log": newest,
            "disk": {"free": free, "capacity": capacity},
            "server_uptime_s": int(time.monotonic() - request.app["started_monotonic"]),
            "boot_log_tail": boot_log,
        },
        headers={"Cache-Control": "no-store"},
    )


async def handle_logs_api(request: web.Request) -> web.Response:
    """JSON index of NMEA logs, for the playback picker and the hub page.

    Deliberately uncached: this reports what is on disk right now. A stale
    listing here would offer the operator a recording that no longer exists,
    or hide one that does — the "looks live and isn't" failure this repo
    exists to avoid.
    """
    log_dir: Path = request.app["log_dir"]
    total = 0
    entries = []
    for f in sorted(log_dir.glob("nmea_*.txt"), reverse=True):
        # Only advertise names handle_log_file will actually serve.
        if not _LOG_FILE.match(f.name):
            continue
        try:
            stat = f.stat()
        except OSError:
            continue  # rotated away mid-listing
        total += stat.st_size
        entries.append({
            "name": f.name,
            "bytes": stat.st_size,
            "mtime": int(stat.st_mtime),
            "url": f"/logs/{f.name}",
        })

    try:
        usage = shutil.disk_usage(log_dir)
        free, capacity = usage.free, usage.total
    except OSError:
        free = capacity = None

    return web.json_response(
        {"files": entries, "count": len(entries), "bytes": total,
         "free": free, "capacity": capacity},
        headers={"Cache-Control": "no-store"},
    )


# ---- NMEA WebSocket bridge -----------------------------------------------

async def handle_nmea_ws(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    request.app["nmea_clients"].add(ws)
    log.info("NMEA client connected (%d total)", len(request.app["nmea_clients"]))
    try:
        async for _msg in ws:
            pass  # client→server traffic ignored; this is a broadcast feed
    finally:
        request.app["nmea_clients"].discard(ws)
        log.info("NMEA client disconnected (%d total)", len(request.app["nmea_clients"]))
    return ws


# ---- Environmental pre-warm (wind, tides, currents) ---------------------
# Mirrors the URL conventions in static/js/data-loader.js so the cache keys
# match what the browser will request through the reverse-proxy. If you change
# the JS station list or wind grid here, mirror it there (and vice-versa).

# NOAA tide stations — mirror of TIDE_STATIONS in data-loader.js. No count in
# this comment on purpose: tests/test_boat_server.py asserts the two lists match.
TIDE_STATIONS = [
    "9414290", "9414750", "9414764", "9414816", "9414874", "9414688",
    "9414523", "9414458", "9414509", "9414131", "9413663", "9413450",
    "9414863", "9415056", "9415102", "9415144",
]

# 6 NOAA current-prediction stations (mirror of CURRENT_STATIONS).
CURRENT_STATIONS = [
    "SFB1201", "SFB1203", "SFB1204", "SFB1205", "SFB1206", "SFB1211",
]

# Open-Meteo wind grid (mirror of WIND_BOUNDS / WIND_NX / WIND_NY).
WIND_BOUNDS_S = 36.40
WIND_BOUNDS_N = 38.10
WIND_BOUNDS_W = -122.95
WIND_BOUNDS_E = -121.80
WIND_NX = 11
WIND_NY = 16

NOAA_BASE = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"


def _noaa_date_range_utc(day_offset: int = 0):
    """Same begin/end as static/js/data-loader.js _noaaDateRange (3-day window).

    `day_offset=1` gives tomorrow's window, which the pre-warm loop also fetches
    so the UTC-midnight rollover doesn't turn every cached entry into a miss.
    """
    today = _dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(days=day_offset)
    begin = today.strftime("%Y%m%d")
    end = (today + _dt.timedelta(days=3)).strftime("%Y%m%d")
    return begin, end


def _tide_url(station_id: str, begin: str, end: str) -> str:
    return (
        f"{NOAA_BASE}?begin_date={begin}&end_date={end}&station={station_id}"
        "&product=predictions&datum=MLLW&units=english&time_zone=gmt"
        "&format=json&interval=6"
    )


def _current_url(station_id: str, begin: str, end: str) -> str:
    return (
        f"{NOAA_BASE}?begin_date={begin}&end_date={end}&station={station_id}"
        "&product=currents_predictions&units=english&time_zone=gmt"
        "&format=json&interval=6"
    )


def _wind_url() -> str:
    """Build the same batched Open-Meteo URL the browser builds.

    Cache key matches exactly so a browser request hits this entry. The browser
    formats lat/lon to 4 decimals (`.toFixed(4)`); we replicate that here.
    """
    lats: list[str] = []
    lons: list[str] = []
    for iy in range(WIND_NY):
        lat = WIND_BOUNDS_S + iy * (WIND_BOUNDS_N - WIND_BOUNDS_S) / (WIND_NY - 1)
        for ix in range(WIND_NX):
            lon = WIND_BOUNDS_W + ix * (WIND_BOUNDS_E - WIND_BOUNDS_W) / (WIND_NX - 1)
            lats.append(f"{lat:.4f}")
            lons.append(f"{lon:.4f}")
    return (
        f"{OPEN_METEO_BASE}"
        f"?latitude={','.join(lats)}&longitude={','.join(lons)}"
        "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
        "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
        "&models=gfs_seamless&wind_speed_unit=kn&forecast_hours=49"
    )


async def _prewarm_url(
    app: web.Application,
    url: str,
    label: str,
    ttl_s: float = 0.0,
    timeout: Optional["aiohttp.ClientTimeout"] = None,
    alias: Optional[str] = None,
) -> str:
    """Fetch one URL into the disk cache.

    Returns one of three outcomes so callers can tell a real gap from a fault:
      "ok"      — cached, already fresh within `ttl_s`, or 304 Not Modified
      "missing" — 404, upstream never published this file
      "fail"    — timeout, 5xx, or a body that isn't the JSON we expect

    Wrapped so a single failure doesn't propagate and kill the pre-warm loop.
    """
    cache: DiskCache = app["cache"]
    session: aiohttp.ClientSession = app["http"]
    cached = cache.get(url)
    if cached and ttl_s > 0 and (time.time() - cached[0]["ts"]) < ttl_s:
        return "ok"

    headers = {}
    if cached and cached[0].get("last_modified"):
        headers["If-Modified-Since"] = cached[0]["last_modified"]

    try:
        async with session.get(
            url, timeout=timeout or DEFAULT_TIMEOUT, headers=headers
        ) as r:
            if r.status == 304:
                cache.touch(url)
                return "ok"
            body = await r.read()
            ct = r.headers.get("Content-Type", "application/json")
            if r.status == 200:
                if not _acceptable_json(ct):
                    log.warning("pre-warm %s: %s, not JSON (captive portal?)",
                                label, ct)
                    return "fail"
                cache.put(url, body, ct, 200, alias=alias,
                          last_modified=r.headers.get("Last-Modified"))
                return "ok"
            if r.status == 404:
                return "missing"
            log.info("pre-warm %s: status %d", label, r.status)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("pre-warm %s failed: %s", label, e)
    return "fail"


def _prewarm_sleep(cycle_failed: bool, backoff_idx: int):
    """Decide how long to sleep after a pre-warm cycle.

    Returns (sleep_seconds, next_backoff_idx). Shared by both pre-warm loops so
    the backoff schedule can't drift between them.
    """
    if cycle_failed:
        sleep_s = PREWARM_FAIL_BACKOFF_S[
            min(backoff_idx, len(PREWARM_FAIL_BACKOFF_S) - 1)
        ]
        return sleep_s, backoff_idx + 1
    return PREWARM_OK_INTERVAL_S, 0


async def env_prewarm_loop(app: web.Application):
    """Pre-warm Open-Meteo wind grid + NOAA tide/current predictions.

    Runs alongside `sfbofs_prewarm_loop`. Each category is wrapped in its own
    try/except so a single failure can't kill the loop. Sleeps the same hourly
    interval; backs off on failure.
    """
    backoff_idx = 0
    try:
        while True:
            log.info("env pre-warm starting")
            ok = 0
            cycle_failed = False

            # 1. Wind grid (single batched request, 176 points × 49 hours)
            if await _prewarm_url(app, _wind_url(), "wind") == "ok":
                ok += 1
            else:
                cycle_failed = True
            await asyncio.sleep(PREWARM_REQUEST_GAP_S)

            # 2 + 3. Tide and current predictions, for today's window and
            # tomorrow's. The begin_date rolls at UTC midnight, so caching only
            # today's would leave every station a miss right after the rollover.
            for day_offset in (0, 1):
                begin, end = _noaa_date_range_utc(day_offset)
                for station_id in TIDE_STATIONS:
                    if await _prewarm_url(
                        app, _tide_url(station_id, begin, end),
                        f"tide:{station_id}:{begin}",
                        alias=_noaa_alias(station_id, "predictions", "6", "MLLW"),
                    ) == "ok":
                        ok += 1
                    else:
                        cycle_failed = True
                    await asyncio.sleep(PREWARM_REQUEST_GAP_S)

                for station_id in CURRENT_STATIONS:
                    if await _prewarm_url(
                        app, _current_url(station_id, begin, end),
                        f"current:{station_id}:{begin}",
                        alias=_noaa_alias(station_id, "currents_predictions", "6", ""),
                    ) == "ok":
                        ok += 1
                    else:
                        cycle_failed = True
                    await asyncio.sleep(PREWARM_REQUEST_GAP_S)

            sleep_s, backoff_idx = _prewarm_sleep(cycle_failed, backoff_idx)
            if cycle_failed:
                log.info(
                    "env pre-warm had failures (%d ok); retry in %ds", ok, sleep_s
                )
            else:
                log.info(
                    "env pre-warm complete: %d urls cached; next cycle in %dm",
                    ok, sleep_s // 60,
                )
            await asyncio.sleep(sleep_s)
    except asyncio.CancelledError:
        return


# ---- SFBOFS pre-warm -----------------------------------------------------

async def sfbofs_prewarm_loop(app: web.Application):
    """Pre-warm cache with SFBOFS forecast hours + one-shot data files.

    Adaptive interval: when a cycle succeeds we sleep PREWARM_OK_INTERVAL_S
    (1 h). When any fetch in a cycle fails (most likely cause: satcom is
    down), we sleep PREWARM_FAIL_BACKOFF_S[backoff] (60s, 2m, 4m, 5m, 5m)
    so that within ~60 s of internet returning we re-attempt and start
    catching up — without hammering NOAA/GH Pages while the link is flaky.
    """
    cache: DiskCache = app["cache"]
    base = app["gh_pages_base"].rstrip("/")
    backoff_idx = 0
    try:
        while True:
            log.info("SFBOFS pre-warm starting")
            ok = 0
            cycle_failed = False
            try:
                for h in range(0, 49):
                    url = f"{base}/data/sfbofs/hour_{h:02d}.json"
                    res = await _prewarm_url(
                        app, url, f"sfbofs:hour_{h:02d}",
                        ttl_s=TTL_SFBOFS_S, timeout=DATA_TIMEOUT,
                    )
                    if res == "ok":
                        ok += 1
                    elif res == "missing":
                        # Later hours missing is normal — a model run doesn't
                        # always publish all 49. hour_00 missing is not, and
                        # must not be logged as a clean "0 hours cached".
                        if h == 0:
                            cycle_failed = True
                        log.info("SFBOFS hour_%02d missing (404), stopping", h)
                        break
                    else:
                        cycle_failed = True
                    await asyncio.sleep(PREWARM_REQUEST_GAP_S)
                # NDBC + miscellaneous /data/* one-shots.
                for relpath in ("data/wind/stations.json", "data/land_mask.json", "data/meta.json"):
                    url = f"{base}/{relpath}"
                    if await _prewarm_url(
                        app, url, relpath, timeout=DATA_TIMEOUT
                    ) != "ok":
                        cycle_failed = True
                    await asyncio.sleep(PREWARM_REQUEST_GAP_S)
                cache.prune(CACHE_MAX_AGE_S, CACHE_MAX_BYTES)
            except Exception:
                # Don't let an unexpected error kill the loop forever — log
                # and treat the whole cycle as a failure so we back off then
                # retry instead of going silent for an hour.
                log.exception("pre-warm cycle crashed; will retry after backoff")
                cycle_failed = True

            sleep_s, backoff_idx = _prewarm_sleep(cycle_failed, backoff_idx)
            if cycle_failed:
                log.info("pre-warm cycle had failures (%d hours ok); retry in %ds", ok, sleep_s)
            else:
                log.info("pre-warm complete: %d hours cached; next cycle in %dm", ok, sleep_s // 60)
            await asyncio.sleep(sleep_s)
    except asyncio.CancelledError:
        return


# ---- App lifecycle -------------------------------------------------------

async def on_startup(app: web.Application):
    app["http"] = aiohttp.ClientSession()
    app["nmea_clients"] = set()

    async def send_to_ws(client: web.WebSocketResponse, text: str):
        if not client.closed:
            await client.send_str(text)

    def clients_snapshot():
        return list(app["nmea_clients"])

    app["nmea_task"] = asyncio.create_task(
        nmea_tcp_broadcast(app["tcp_host"], app["tcp_port"], clients_snapshot, send_to_ws)
    )
    app["prewarm_task"] = asyncio.create_task(sfbofs_prewarm_loop(app))
    app["env_prewarm_task"] = asyncio.create_task(env_prewarm_loop(app))


async def on_cleanup(app: web.Application):
    tasks = [t for t in (
        app.get("nmea_task"),
        app.get("prewarm_task"),
        app.get("env_prewarm_task"),
    ) if t]
    for t in tasks:
        t.cancel()
    # Await them before closing the session, or an in-flight request logs a
    # "Session is closed" traceback on shutdown.
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if "http" in app:
        await app["http"].close()


def build_app(args) -> web.Application:
    static_dir = Path(args.static_dir).resolve()
    if not static_dir.exists():
        raise SystemExit(f"static dir not found: {static_dir}")

    app = web.Application(client_max_size=1024 * 1024)
    app["cache"] = DiskCache(Path(args.cache_dir).resolve())
    # url -> Future, so concurrent requests for one URL share a single fetch.
    app["inflight"] = {}
    app["gh_pages_base"] = args.gh_pages_base
    app["tcp_host"] = args.tcp_host
    app["tcp_port"] = args.tcp_port
    app["static_dir"] = static_dir
    app["log_dir"] = Path(args.log_dir).resolve()
    app["ssl_cert"] = args.ssl_cert or ""
    app["config_payload"] = {
        "mode": "boat",
        "useCloudAIS": False,
        "ownMmsi": args.mmsi,
        "apiBase": {
            "noaa": "/api/noaa/api/prod/datagetter",
            "openMeteo": "/api/open-meteo/v1/forecast",
        },
        "dataBase": "/data",
        # Same-origin /nmea — browser uses page protocol/host/port automatically.
        "nmeaWsUrl": "/nmea",
    }

    app.router.add_get("/config.json", handle_config)
    app.router.add_get("/certs/server.crt", handle_cert)
    app.router.add_get("/logs", handle_logs_index)
    app.router.add_get("/logs/{filename}", handle_log_file)
    app.router.add_get("/api/logs", handle_logs_api)
    app.router.add_get("/api/health", handle_health)
    app.router.add_get("/api/noaa/{tail:.*}", handle_proxy_noaa)
    app.router.add_get("/api/open-meteo/{tail:.*}", handle_proxy_open_meteo)
    app.router.add_get("/data/meta.json", handle_meta)
    app.router.add_get("/data/sfbofs/{tail:.*}", _make_gh_pages_handler("data/sfbofs", TTL_SFBOFS_S, MAX_STALE_SFBOFS_S))
    app.router.add_get("/data/sfbofs_gg/{tail:.*}", _make_gh_pages_handler("data/sfbofs_gg", TTL_SFBOFS_S, MAX_STALE_SFBOFS_S))
    app.router.add_get("/data/wind/{tail:.*}", _make_gh_pages_handler("data/wind", TTL_NDBC_S, MAX_STALE_NDBC_S))
    app.router.add_get("/data/hycom/{tail:.*}", _make_gh_pages_handler("data/hycom", TTL_SFBOFS_S, MAX_STALE_SFBOFS_S))
    # Catch-all for any other /data/<file>.json (land_mask.json, etc.) —
    # cache + GH Pages + local-static fallback so the router never breaks.
    app.router.add_get("/data/{tail:.*}", handle_data_catchall)
    app.router.add_get("/nmea", handle_nmea_ws)

    # aiohttp's add_static doesn't serve index.html for "/", so do it manually.
    async def handle_root(_request):
        return web.FileResponse(static_dir / "index.html")
    app.router.add_get("/", handle_root)

    # Navigation hub. The map stays at "/" so nothing about the existing
    # bookmark changes; /hub is the index of everything else.
    async def handle_hub(_request):
        return web.FileResponse(static_dir / "hub.html")
    app.router.add_get("/hub", handle_hub)
    # Static site last — catches everything else (JS, CSS, images, manifest, sw.js).
    app.router.add_static("/", static_dir, show_index=False, follow_symlinks=False)

    app["started_monotonic"] = time.monotonic()   # monotonic, not wall clock (P33)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    p = argparse.ArgumentParser(description="AIS Tracker boat-mode Pi server")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--ssl-cert", default=None)
    p.add_argument("--ssl-key", default=None)
    p.add_argument("--tcp-host", default="192.168.47.10")
    p.add_argument("--tcp-port", type=int, default=10110)
    p.add_argument("--mmsi", type=int, default=338361814)
    p.add_argument("--cache-dir", default="cache")
    p.add_argument("--log-dir", default=str(Path(__file__).resolve().parent.parent / "logs"))
    p.add_argument(
        "--gh-pages-base",
        default="https://rostape1.github.io/TyponFlowRacer",
        help="Origin where the GH Pages static data lives",
    )
    p.add_argument(
        "--static-dir",
        default=str(Path(__file__).resolve().parent.parent / "static"),
    )
    args = p.parse_args()

    ssl_ctx: Optional[ssl.SSLContext] = None
    if args.ssl_cert and args.ssl_key:
        ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_ctx.load_cert_chain(args.ssl_cert, args.ssl_key)
        scheme = "https"
    else:
        scheme = "http"
        log.warning("Running without TLS — browsers will refuse Service Worker and geolocation")

    app = build_app(args)
    log.info("Boat server starting at %s://0.0.0.0:%d/", scheme, args.port)
    log.info("NMEA bridge → tcp://%s:%d", args.tcp_host, args.tcp_port)
    log.info("Own MMSI: %d", args.mmsi)
    web.run_app(app, host="0.0.0.0", port=args.port, ssl_context=ssl_ctx, access_log=None)


if __name__ == "__main__":
    main()
