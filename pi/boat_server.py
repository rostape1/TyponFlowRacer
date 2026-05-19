#!/usr/bin/env python3
"""Boat-mode server for the AIS Tracker.

Runs on the Raspberry Pi on the boat WiFi. Single asyncio process that:

  1. Serves the static AIS Tracker UI (HTTPS, self-signed) from ../static.
  2. Synthesizes /config.json so the browser switches to boat mode
     (local NMEA AIS, no AISstream.io, env data via this Pi's reverse proxy).
  3. Reverse-proxies + disk-caches NOAA CO-OPS and Open-Meteo so all clients
     on the boat WiFi share one fetch and the cache survives flaky satcom.
  4. Reverse-proxies + disk-caches the pre-computed SFBOFS / NDBC / meta JSON
     hosted on GitHub Pages, with a startup-time SFBOFS pre-warm.
  5. Bridges the local NMEA TCP stream (default 192.168.47.10:10110) to a
     WebSocket at /nmea for the browser.

Usage:
    python3 pi/boat_server.py --ssl-cert certs/server.crt --ssl-key certs/server.key
"""

import argparse
import asyncio
import glob
import hashlib
import json
import logging
import os
import re
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
# Refuse to serve cache older than this on upstream error: stale data is fine,
# day-old data masquerading as current is dangerous on the water.
MAX_STALE_S = 24 * 3600

# Pre-warm pacing
PREWARM_OK_INTERVAL_S = 3600            # next cycle when last cycle succeeded
PREWARM_FAIL_BACKOFF_S = (60, 120, 240, 300, 300)  # exponential, capped
PREWARM_REQUEST_GAP_S = 0.2             # be polite to GH Pages


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

    def put(self, url: str, body: bytes, content_type: str, status: int):
        body_path, meta_path = self._paths(url)
        # aiohttp's web.Response(content_type=...) rejects charset, so strip
        # it here once at write time rather than on every cache hit.
        ct = (content_type or "application/octet-stream").split(";")[0].strip()
        try:
            body_path.write_bytes(body)
            meta_path.write_text(json.dumps({
                "ts": time.time(),
                "content_type": ct,
                "status": status,
            }))
        except OSError as e:
            log.warning("cache write failed for %s: %s", url, e)


# ---- Reverse-proxy helper ------------------------------------------------

async def proxy_with_cache(
    request: web.Request,
    upstream_url: str,
    cache: DiskCache,
    ttl_s: float,
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

    session: aiohttp.ClientSession = request.app["http"]
    try:
        async with session.get(upstream_url, timeout=aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT_S)) as resp:
            body = await resp.read()
            ct = resp.headers.get("Content-Type", "application/octet-stream")
            if resp.status == 200:
                cache.put(upstream_url, body, ct, resp.status)
            return web.Response(
                body=body, status=resp.status,
                content_type=ct.split(";")[0].strip(),
                headers={"X-Cache": "MISS"},
            )
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        log.warning("upstream failed for %s: %s", upstream_url, e)
        if cached:
            meta, body = cached
            stale_age = now - meta["ts"]
            if stale_age <= MAX_STALE_S:
                return web.Response(
                    body=body,
                    status=meta.get("status", 200),
                    content_type=meta.get("content_type", "application/json"),
                    headers={
                        "X-Cache": "STALE",
                        "X-Cache-Age": f"{int(stale_age)}",
                        "X-Cache-Reason": "upstream-error",
                    },
                )
            log.warning("cache for %s exceeds MAX_STALE (%.0fh), refusing",
                        upstream_url, stale_age / 3600)
        return web.Response(status=504, text="upstream unreachable, no fresh cache")


def _ttl_for_noaa(query: str) -> float:
    if "product=water_level" in query:
        return TTL_WATER_LEVEL_S
    return TTL_TIDES_CURRENTS_S


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
    upstream = f"https://api.tidesandcurrents.noaa.gov/{tail}"
    if qs:
        upstream = f"{upstream}?{qs}"
    return await proxy_with_cache(
        request, upstream, request.app["cache"], _ttl_for_noaa(qs)
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
        request, upstream, request.app["cache"], TTL_OPEN_METEO_S
    )


def _make_gh_pages_handler(subpath: str, ttl_s: float):
    async def handler(request: web.Request) -> web.Response:
        tail = request.match_info["tail"]
        if not _DATA_FILE.match(tail):
            return _reject_invalid_tail()
        base = request.app["gh_pages_base"].rstrip("/")
        upstream = f"{base}/{subpath}/{tail}"
        return await proxy_with_cache(request, upstream, request.app["cache"], ttl_s)
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
    resp = await proxy_with_cache(request, upstream, request.app["cache"], TTL_SFBOFS_S)
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
    return await proxy_with_cache(request, upstream, request.app["cache"], TTL_META_S)


# ---- NMEA log download ---------------------------------------------------

_LOG_FILE = re.compile(r"^nmea_[\d_-]+\.txt$")


async def handle_logs_index(request: web.Request) -> web.Response:
    log_dir: Path = request.app["log_dir"]
    files = sorted(log_dir.glob("nmea_*.txt"), reverse=True)

    def fmt_size(b):
        return f"{b/1024:.0f} KB" if b < 1024 * 1024 else f"{b/1024/1024:.1f} MB"

    rows = ""
    for f in files:
        stat = f.stat()
        size = fmt_size(stat.st_size)
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
        rows += (
            f"<tr><td><a href='/logs/{f.name}'>{f.name}</a></td>"
            f"<td>{mtime}</td><td>{size}</td></tr>\n"
        )
    if not rows:
        rows = "<tr><td colspan='3' style='color:#8395a7'>No log files yet</td></tr>"

    html = f"""<!DOCTYPE html>
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
<table>
<thead><tr><th>File</th><th>Modified</th><th>Size</th></tr></thead>
<tbody>{rows}</tbody>
</table>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_log_file(request: web.Request) -> web.Response:
    filename = request.match_info["filename"]
    if not _LOG_FILE.match(filename):
        return web.Response(status=404, text="not found")
    log_dir: Path = request.app["log_dir"]
    path = log_dir / filename
    if not path.exists():
        return web.Response(status=404, text="not found")
    return web.FileResponse(
        path,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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
    session: aiohttp.ClientSession = app["http"]
    backoff_idx = 0
    try:
        while True:
            log.info("SFBOFS pre-warm starting")
            ok = 0
            cycle_failed = False
            try:
                for h in range(0, 49):
                    url = f"{base}/data/sfbofs/hour_{h:02d}.json"
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT_S)) as r:
                            if r.status == 404:
                                log.info("SFBOFS hour_%02d missing (404), stopping", h)
                                break
                            body = await r.read()
                            if r.status == 200:
                                cache.put(url, body, r.headers.get("Content-Type", "application/json"), 200)
                                ok += 1
                            else:
                                cycle_failed = True
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        log.warning("pre-warm sfbofs %02d failed: %s", h, e)
                        cycle_failed = True
                    await asyncio.sleep(PREWARM_REQUEST_GAP_S)
                # NDBC + miscellaneous /data/* one-shots.
                for relpath in ("data/wind/stations.json", "data/land_mask.json", "data/meta.json"):
                    url = f"{base}/{relpath}"
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=UPSTREAM_TIMEOUT_S)) as r:
                            if r.status == 200:
                                cache.put(url, await r.read(), r.headers.get("Content-Type", "application/json"), 200)
                            else:
                                cycle_failed = True
                    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                        log.warning("pre-warm %s failed: %s", relpath, e)
                        cycle_failed = True
            except Exception:
                # Don't let an unexpected error kill the loop forever — log
                # and treat the whole cycle as a failure so we back off then
                # retry instead of going silent for an hour.
                log.exception("pre-warm cycle crashed; will retry after backoff")
                cycle_failed = True

            if cycle_failed:
                sleep_s = PREWARM_FAIL_BACKOFF_S[min(backoff_idx, len(PREWARM_FAIL_BACKOFF_S) - 1)]
                backoff_idx += 1
                log.info("pre-warm cycle had failures (%d hours ok); retry in %ds", ok, sleep_s)
            else:
                backoff_idx = 0
                sleep_s = PREWARM_OK_INTERVAL_S
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


async def on_cleanup(app: web.Application):
    for t in (app.get("nmea_task"), app.get("prewarm_task")):
        if t:
            t.cancel()
    if "http" in app:
        await app["http"].close()


def build_app(args) -> web.Application:
    static_dir = Path(args.static_dir).resolve()
    if not static_dir.exists():
        raise SystemExit(f"static dir not found: {static_dir}")

    app = web.Application(client_max_size=1024 * 1024)
    app["cache"] = DiskCache(Path(args.cache_dir).resolve())
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
    app.router.add_get("/api/noaa/{tail:.*}", handle_proxy_noaa)
    app.router.add_get("/api/open-meteo/{tail:.*}", handle_proxy_open_meteo)
    app.router.add_get("/data/meta.json", handle_meta)
    app.router.add_get("/data/sfbofs/{tail:.*}", _make_gh_pages_handler("data/sfbofs", TTL_SFBOFS_S))
    app.router.add_get("/data/sfbofs_gg/{tail:.*}", _make_gh_pages_handler("data/sfbofs_gg", TTL_SFBOFS_S))
    app.router.add_get("/data/wind/{tail:.*}", _make_gh_pages_handler("data/wind", TTL_NDBC_S))
    app.router.add_get("/data/hycom/{tail:.*}", _make_gh_pages_handler("data/hycom", TTL_SFBOFS_S))
    # Catch-all for any other /data/<file>.json (land_mask.json, etc.) —
    # cache + GH Pages + local-static fallback so the router never breaks.
    app.router.add_get("/data/{tail:.*}", handle_data_catchall)
    app.router.add_get("/nmea", handle_nmea_ws)

    # aiohttp's add_static doesn't serve index.html for "/", so do it manually.
    async def handle_root(_request):
        return web.FileResponse(static_dir / "index.html")
    app.router.add_get("/", handle_root)
    # Static site last — catches everything else (JS, CSS, images, manifest, sw.js).
    app.router.add_static("/", static_dir, show_index=False, follow_symlinks=False)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main():
    p = argparse.ArgumentParser(description="AIS Tracker boat-mode Pi server")
    p.add_argument("--port", type=int, default=8443)
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
