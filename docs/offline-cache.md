# Offline caching architecture

How the app keeps working with no internet. Two caching layers that never overlap, plus the reasons
each rule exists. Read this before changing anything in `pi/boat_server.py`'s proxy path or
`static/sw.js`.

Relevant pitfalls: `P06` `P07` `P08` `P09` `P10` `P11` `P12` `P16` `P17` — fetch by ID from
[pitfalls.md](pitfalls.md).

---

## Why there are two layers

| | GitHub Pages (HTTPS) | Raspberry Pi (HTTP :8080) |
|---|---|---|
| Caching mechanism | Service Worker (`static/sw.js`) | Server-side `DiskCache` in `pi/boat_server.py` |
| Why not the other | Pi serves **HTTP**, and browsers refuse to register a Service Worker on an HTTP origin | GH Pages is static hosting — there is no server to cache in |
| Shared by all clients | No, per-browser | Yes, one cache for every device on the boat WiFi |

This is deliberate, not an oversight. `sw.js` is only ever relevant on the GitHub Pages URL; all
caching at sea is server-side.

---

## Layer 1 — filesystem map tiles

`static/tiles/`, served by aiohttp's `add_static`. Plain static files; the running server never
refreshes them.

- Populated by `download_offline.py` — run manually while you have internet. Idempotent: it skips
  files that already exist with size > 0, and writes via `.part` + `os.replace()` so an interrupted
  run doesn't leave a truncated PNG that the skip test then treats as complete forever.
- `DEFAULT_BOUNDS` (SF Bay through Monterey) is the authoritative bbox. `DEFAULT_ZOOM_RANGE` is
  `(10, 15)`. Override with `--bounds` / `--zoom`.
- Four sources: NOAA chart (the default layer), Esri Dark Gray, OpenStreetMap, OpenSeaMap. The NOAA
  source uses an ArcGIS LOD scheme (`lod = z - 2`, y-then-x ordering), dispatched on the source's
  `"scheme"` field rather than a string comparison on its key.
- OSM gets a longer per-request delay than the others, per OSM's tile usage policy.

**Only served when `_serveTilesFromDisk()` is true** (`static/js/app.js`) — boat mode per
`/config.json`, or a non-`github.io`, non-localhost host. On GitHub Pages *and* on localhost, tile
URLs go direct to the CDN and these files are unused. Because only z10-15 exist on disk, the layers
clamp `minNativeZoom`/`maxNativeZoom` so Leaflet upscales past z15 rather than 404ing into a black
canvas (`P15`).

---

## Layer 2 — the Pi's reverse-proxy disk cache

`DiskCache` in `pi/boat_server.py`, default dir `cache/`. Keyed on **SHA1 of the full upstream URL**,
including the query string — which is why the JS and Python URL builders must agree byte-for-byte
(`P20`).

### Population

Three paths:

1. `sfbofs_prewarm_loop` — SFBOFS hours 0-48, plus NDBC stations, land mask and meta, from GitHub
   Pages. Hourly.
2. `env_prewarm_loop` — the batched Open-Meteo wind URL, all 16 tide stations and all 6 current
   stations. Hourly, and it warms **today's and tomorrow's** NOAA date window (`P06`).
3. On demand, when a browser request misses.

Both loops share `_prewarm_url()` (three-way `ok` / `missing` / `fail`) and `_prewarm_sleep()`.
Success → `PREWARM_OK_INTERVAL_S`; failure → `PREWARM_FAIL_BACKOFF_S` (60, 120, 240, 300, 300).

`data/sfbofs_gg/` (hi-res Golden Gate) has a route but is **not** pre-warmed — on-demand only.

### Freshness

Per-source TTLs: SFBOFS 1h, NOAA tides/currents 6h, water levels 10min, Open-Meteo wind 30min,
NDBC 10min, meta 1min. Within TTL the proxy answers `X-Cache: HIT` and never touches the network.

The pre-warm skips hours still inside TTL and sends `If-Modified-Since` from the cached meta,
treating 304 as success via `DiskCache.touch()`. `/data/*` bodies use `DATA_TIMEOUT`
(`sock_connect` / `sock_read`), never a total timeout — see `P09` for why that distinction is the
difference between a working cache and 1.4 GB/day of wasted satcom.

### Stale-on-error, and what counts as an error

When upstream is unusable and a cached entry exists, `proxy_with_cache` serves the cached body with
`X-Cache: STALE` + `X-Cache-Age`, up to a per-source ceiling:

| Ceiling | Value | Why |
|---|---|---|
| `MAX_STALE_TIDES_S`, `MAX_STALE_CURRENTS_S` | 30 days | Harmonic predictions stay valid for weeks |
| `MAX_STALE_OPEN_METEO_S`, `MAX_STALE_SFBOFS_S`, `MAX_STALE_NDBC_S` | 7 days | SFBOFS only forecasts 48h anyway |
| `MAX_STALE_WATER_LEVEL_S`, `MAX_STALE_META_S`, `MAX_STALE_DEFAULT_S` | 24 hours | A stale real-time gauge reading is worse than none |

Beyond the ceiling the proxy returns **504** rather than serve data masquerading as current.

"Unusable" is broader than a network error:

- Network exception → stale.
- `status >= 500`, or 4xx other than 404 → stale (`P08`).
- `200` whose content-type isn't JSON → **neither cached nor served**, falls through to stale
  (`P07` — captive portals).
- `404` → **passed through untouched.** The browser's SFBOFS download sweep reads 404 as "this hour
  was never published" (`P03`); swallowing it would corrupt the sweep's coverage accounting.

### The date-shift alias

NOAA prediction URLs embed `begin_date=<today UTC>`. Since the key is SHA1 of the whole URL, UTC
midnight used to invalidate every pre-warmed tide and current entry at once — see `P06` for the full
failure. Each NOAA entry additionally writes an alias pointer keyed on
(station, product, interval, datum) with dates stripped. On upstream failure with no exact hit, the
stale branch resolves the alias and sets `X-Cache-Reason: upstream-error-date-shift`.

### Integrity and bounds

- **Atomic writes.** Body then meta, each via a `.tmp` sibling + `os.replace()`. A crash between the
  two leaves a complete body with the *old* timestamp — never a fresh timestamp on a partial body.
- **Single-flight.** `_fetch_upstream` + `app["inflight"]`: concurrent callers for one URL await the
  first fetch (`P10`).
- **Pruning.** `DiskCache.prune()` once per SFBOFS cycle — age sweep at `CACHE_MAX_AGE_S`, then
  oldest-first under `CACHE_MAX_BYTES` (`P11`).
- **Path allowlists.** `_NOAA_TAIL`, `_OPEN_METEO_TAIL`, `_DATA_FILE` and `_LOG_FILE` are exact
  patterns, not prefixes — the `{tail:.*}` proxy route would otherwise let a LAN client steer the
  upstream URL via traversal or `@evil.com` and poison shared cache entries.

---

## The Service Worker (GitHub Pages only)

Three caches: `CACHE_NAME` (`ais-tracker-<build hash>`, the app shell), `DATA_CACHE`
(`ais-data-v10`), `TILE_CACHE` (`ais-tiles-v2`).

- **Tiles** — cache-first, matched on exact host or a true subdomain (`P18`).
- **HTML/JS** — network-first. `APP_BUILD` is stamped with the commit hash at deploy time by
  `deploy.yml`, so `CACHE_NAME` rotates per deploy and `activate` deletes the old one.
- **Env APIs and static data JSON** — network-first with cache fallback, switching to
  stale-while-revalidate after an explicit offline download.
- **`/config.json` bypasses the SW entirely** — it must always reflect the serving origin's truth.

### Quota eviction

On `QuotaExceededError`, eviction frees space in `TILE_CACHE` (unbounded, and always re-fetchable)
and **never** in `CACHE_NAME`. For `DATA_CACHE` it drops the **farthest** forecast hours, ranked by
the `hour_(\d+)` in the URL — insertion order would delete `hour_00..09`, the hours actually being
sailed. Both of those were real bugs; see `P16`.

Bump `TILE_CACHE`'s version whenever the tile CDN changes, or dead tiles are served cache-first
forever (`P17`).

---

## Verifying it

- `python3 tests/test_boat_server.py` — the cache semantics above, offline, no network. Includes the
  date-shift rollover, captive-portal rejection, 404-vs-5xx handling, stale ceilings, single-flight
  and pruning.
- `./pi/test_boat_server.sh [host:port]` — end-to-end against a running Pi. Needs a live server and
  network, so it is **not** in CI.
- `X-Cache` / `X-Cache-Age` / `X-Cache-Reason` on any proxied response tell you which path served it.
