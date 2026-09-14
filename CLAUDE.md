# AIS Tracker

Real-time maritime vessel tracking for San Francisco Bay. AIS ship data plus NOAA tidal currents,
wind forecasts and tide predictions on an interactive Leaflet map with particle animations. Runs
both as a public web app and as an offline-capable navigation aid on the boat.

**This file is a lean index — detail lives in `docs/`.** Load the relevant doc from the
[Documentation Map](#documentation-map) before working in an area:
[docs/offline-cache.md](docs/offline-cache.md) before touching the Pi proxy or `sw.js`,
[docs/router.md](docs/router.md) before the route optimizer,
[docs/pitfalls.md](docs/pitfalls.md) **by ID** before editing anything with a pitfall filed against
it.

> **Load `docs/pitfalls.md` by SECTION, never whole.** It is a lookup table, not a document. The
> grouped index in [Critical pitfalls](#critical-pitfalls--do-not-re-investigate) tells you which
> IDs apply to what you're touching; fetch those two or three:
> ```bash
> grep -n '\[P06\]' docs/pitfalls.md          # -> line, then Read with offset/limit
> awk 'index($0,"[P06]")&&/^## /{f=1;print;next} f&&/^## /{exit} f' docs/pitfalls.md
> ```
> Every other doc in `docs/` is small enough to read whole.

---

## How to work here

Only what's specific to this repo. General scope, length and correction behavior come from the
harness and are deliberately not restated.

**This is a navigation aid used offshore.** The characteristic failure here is not a crash — it is
data that *looks* live and isn't (`P01` `P04` `P06`). Prefer failing visibly and loudly over
degrading silently. A layer that can't get fresh data must say so in the UI, not draw the last thing
it had.

**Which process for which job.** Pick one lane per job rather than a different one each session.

- **Planning → the superpowers chain.** `brainstorming` first; let its classification pick the
  ceremony. Most work here is *Bounded* (a scoped change to code that already exists) → short design
  in chat, then build. Reserve spec + `writing-plans` for genuinely new subsystems.
- **Debugging → grep the pitfall index FIRST, then `superpowers:systematic-debugging`.** Look up
  your area's `[Pnn]` entries before forming a hypothesis. The expensive failure here is
  re-investigating something already solved. Use **one** debugging skill — gstack `investigate` has
  the same root-cause-first premise at greater length; running both is duplication.
- **Review → `/pre-ship-review` is the gate, and the only one.** Do not also run gstack's review
  family (`autoplan`, `review`, `devex-review`, `health`, `cso`), and do not run bare
  `5-pass-review` — `.claude/skills/pre-ship-review/` is the repo-local replacement and fixes four
  measured flaws in it.
- **Verification → targeted, not generic.** Don't add "double-check your work" passes or spawn
  subagents to re-read your own diff. **Do** verify the named risk paths: run the test suites, and
  when you change something a test can't see (rendering, the Pi at sea), say plainly that you
  couldn't verify it rather than implying you did. Mutation-test a new test suite once — that is how
  `P21` was found.
- **Subagents.** Delegate only large, genuinely independent, parallelizable work. Don't delegate what
  you can finish in a handful of tool calls. When agents run in parallel on the same repo, partition
  by file — two agents editing one file corrupt each other, and a doc agent running beside a code
  agent will document behavior the code agent is still changing.

**Where detail goes.** New detail belongs in `docs/*.md`, never in this file — see
[Documentation maintenance](#documentation-maintenance).

---

## ⚠️ MANDATORY rules

### Verify visual changes in a browser — code reading proves nothing

This is a Leaflet + Canvas app. For any change to the map, overlays, legends, charts or radar, load
it and look. `python3 -m http.server 8888 --directory static` then the gstack `/browse` tool
(`$B goto`, `$B console --errors`, `$B screenshot`). Confirm the edited file is actually being served
before concluding anything — a hard reload, or a temporary `console.log`.

`_serveTilesFromDisk()` excludes localhost, so **local dev exercises the same CDN path as GitHub
Pages** (`P15`). If `/browse` is wedged, say the change is unverified rather than reasoning about it
from source.

### Adding an environmental data layer — 5-file checklist

The Pi's cache key is SHA1 of the upstream URL, so a layer that is not mirrored *exactly* on both
sides silently never pre-warms (`P20`). When adding one, update **all five**:

1. `static/js/data-loader.js` — the fetcher, its in-memory TTL, and the URL builder.
2. `pi/boat_server.py` — the proxy route, its `_ttl_for_*`, its `MAX_STALE_*` ceiling, **and** the
   pre-warm loop. Its generated URL must be byte-identical to (1).
3. `static/sw.js` — the fetch handler branch, if it should work offline on GitHub Pages.
4. The [coverage matrix](#data-source--offline-coverage-matrix) below — the row is the audit trail.
5. `tests/test_boat_server.py` — add it to the parity assertions, so drift fails CI instead of
   surfacing as "no data at sea" weeks later.

### Before shipping a change to an offline or data path — run `/pre-ship-review`

Required for material changes to `pi/boat_server.py`, `static/js/data-loader.js`, `static/sw.js`,
`download_offline.py`, or the `.github/workflows/` data pipeline. `.claude/skills/pre-ship-review/`.

### Know which branch you're pushing to

`boat-mode` is the boat's **live deploy branch** — `pi/startup.sh` runs
`git reset --hard origin/boat-mode` on every restart. `main` is what GitHub Pages deploys. They are
separate decisions; `git push origin HEAD:main` updates the web app while leaving the boat on
known-good code. Prefer that when you're not at the boat (`P25`). Python changes need
`systemctl restart`; static changes don't (`P26`).

---

## Ports and who owns them

| Port | Owner | Notes |
|---|---|---|
| **8080** | `pi/boat_server.py` via `start_boat.sh` | HTTP. The boat server. `PORT` env overrides. |
| 8081 | `nmea_capture.py` status page | `--web-port`, set explicitly in `startup.sh`. Also the disk-space alert (`P34`) |
| 8888 | local dev static server, and legacy `main.py` | `python3 -m http.server 8888 --directory static` |
| 10110 | AIS receiver (`192.168.47.10`) | **UDP broadcast since 2026-09-14 — this is the live path.** The Pi listens on `0.0.0.0:10110`, accepting only from `--tcp-host`. The TCP client still runs as a fallback and fails by design; `ECONNREFUSED` there is now normal (`P40`) |
| 8443 | *nothing* — historical HTTPS default | Source of a five-file drift bug (`P24`). Only used if you pass `--ssl-cert`. |
| 8765 / 8766 | legacy `nmea_ws_proxy.py` standalone | Superseded. Still the last-resort NMEA fallback in `app.js`: **8765 for `ws://`, 8766 for `wss://`**, picked from `location.protocol`. On GitHub Pages (HTTPS) it therefore probes `wss://raspberrypi.local:8766` and logs a benign `ERR_NAME_NOT_RESOLVED` off-boat. |

---

## Tech Stack

- **Frontend:** Vanilla JS (ES6+), Leaflet 1.9.4, Canvas particle animations, **no build step**
- **Data pipeline:** GitHub Actions (SFBOFS + NDBC only); tides, currents and wind are fetched
  directly from APIs in the browser, or via the Pi reverse proxy on the boat
- **AIS:** direct browser WebSocket to AISstream.io on the internet; local NMEA/VHF on the boat
- **PWA:** `sw.js` registers only over HTTPS, i.e. only on GitHub Pages — see
  [docs/offline-cache.md](docs/offline-cache.md)

## Deployment

| Where you push | What happens | Where it shows up |
|---|---|---|
| `main` | `.github/workflows/deploy.yml` → tests → deploys `static/` + assembled `data/` | `https://rostape1.github.io/TyponFlowRacer` |
| `boat-mode` | Nothing automatic. On next boot or `sudo systemctl restart ais-tracker`, `pi/startup.sh` does `git fetch && git reset --hard origin/boat-mode` | `http://typonrpi4.local:8080/` on boat WiFi |

SSH to Pi: `ssh rostape1@TyponRpi4.local`

## Architecture

```
                                  ┌─────────────────────────────────┐
GitHub Actions (scheduled)        │  GitHub Pages                   │
├── SFBOFS: 4x/day (NetCDF→JSON)  │  ├── index.html + JS/CSS        │
└── NDBC buoys: every 10min       │  ├── data/sfbofs/hour_00..48    │
                                  │  └── data/wind/stations.json    │
                                  └────────────┬────────────────────┘
                                               │ Pi pre-warms cache from here
                                               ▼
                          ┌────────────────────────────────────────┐
                          │  Raspberry Pi — pi/boat_server.py      │
                          │  (systemd: pi/ais-tracker.service)     │
                          │  HTTP :8080  (HTTPS optional via cert) │
                          │                                        │
                          │  GET /             → static/index.html │
                          │  GET /js/...       → static/js/...     │
                          │  GET /config.json  → boat-mode config  │
                          │  GET /api/noaa/*   → reverse-proxy +   │
                          │  GET /api/open-meteo/* disk cache      │
                          │  GET /data/*       → GH Pages + cache  │
                          │                      + local fallback  │
                          │  GET /hub          → navigation hub    │
                          │  GET /logs         → NMEA log browser  │
                          │  GET /api/logs     → log index (JSON)  │
                          │  WS  /nmea         → TCP 192.168.47.10 │
                          │                      :10110 bridge     │
                          │                      + UDP :10110      │
                          │                                        │
                          │  Background: SFBOFS pre-warm loop      │
                          │  + env pre-warm (wind/tides/currents)  │
                          │  (1h cycle, exp backoff on failure)    │
                          └────────────────────────────────────────┘

Browser-side modules (same code both contexts, behavior switches on /config.json):
├── aisstream.js     (cloud AIS via WebSocket — only when useCloudAIS=true)
├── ais-decoder.js   (boat-mode: local AIS from NMEA VHF receiver)
├── nmea-client.js   (WebSocket to /nmea on Pi, or replay)
├── nmea-parser.js → nmea-store.js → sailing-charts.js, competitor-labels.js, radar-view.js
├── data-loader.js   (NOAA tides/currents/water-levels, Open-Meteo wind)
├── router.js + route-worker.js  (isochrone route optimizer)
├── tidal-flow.js    (SFBOFS particle animation)
└── wind-overlay.js  (wind particle animation)
```

### Two URLs, one codebase

| URL | Hosting | When | What's different |
|---|---|---|---|
| `https://rostape1.github.io/TyponFlowRacer` | GitHub Pages (HTTPS) | Dock, shore, anywhere with internet | `/config.json` 404s → web mode (cloud AIS, direct CDN tiles, direct API fetches). Service Worker active. |
| `http://typonrpi4.local:8080/` | Raspberry Pi (HTTP) | On boat WiFi | `/config.json` sets `mode:'boat'`, `useCloudAIS:false`, API base `/api/noaa` + `/api/open-meteo`, NMEA `/nmea`. No Service Worker. |

## Data Source & Offline Coverage Matrix

Ground truth for every external data source: what proxies it, what pre-caches it, where the user sees
its freshness. **Audit before changing offline behavior.** Mechanics and rationale:
[docs/offline-cache.md](docs/offline-cache.md).

| Layer | Pi proxy route | Pre-warmed | Age shown in UI | Notes |
|---|---|---|---|---|
| SFBOFS currents | `/data/sfbofs/{hour}.json` | ✓ hours 0-48 | ✓ flow legend | GH Pages → Pi cache |
| SFBOFS GG hi-res | `/data/sfbofs_gg/{hour}.json` | ✗ (on-demand only) | ✓ | route exists, pre-warm sweeps only `data/sfbofs/` |
| HYCOM currents | `/data/hycom/*` | ✗ | ✗ | optional, outside SF Bay |
| Wind grid (Open-Meteo) | `/api/open-meteo/v1/forecast` | ✓ `env_prewarm_loop` | ✓ wind legend | batched lat/lon array, 176 points |
| Wind stations (NDBC) | `/data/wind/stations.json` | ✓ | ✗ | static JSON |
| Tide predictions | `/api/noaa/api/prod/datagetter` | ✓ `env_prewarm_loop` | ✗ ← **gap** | 16 stations |
| Currents predictions | `/api/noaa/api/prod/datagetter` | ✓ `env_prewarm_loop` | ✗ ← **gap** | 6 stations |
| Water levels (real-time) | `/api/noaa/api/prod/datagetter` | ✗ (intentional, 10-min TTL) | partial | 6 stations |
| Land mask | `/data/land_mask.json` | ✓ | n/a | [docs/land-mask.md](docs/land-mask.md) |
| Meta JSON | `/data/meta.json` | ✓ | n/a | 60s TTL |
| **NOAA chart tiles** | filesystem `/tiles/noaa/{z}/{x}/{y}.png` | `download_offline.py` | n/a | **default layer**; ArcGIS REST upstream |
| Esri Dark Gray / OSM / OpenSeaMap tiles | filesystem `/tiles/{dark,osm,sea}/…` | `download_offline.py` | n/a | z10-15 only (`P15`) |
| Local NMEA stream | `/nmea` (WebSocket) | n/a | ✓ `/api/health` `nmea.transport` | **UDP broadcast** from 192.168.47.10:10110 → WS bridge; TCP client is the standing fallback (`P40`) |
| AISstream.io | n/a | n/a | n/a | disabled in boat mode |

---

## Critical pitfalls — DO NOT re-investigate

**These are SOLVED problems.** Re-"optimizing" one re-introduces a bug already paid for. This is an
**index, not an explanation** — a line tells you *that* a trap exists, never enough to work around it
safely. Look up your group's IDs in [docs/pitfalls.md](docs/pitfalls.md), by ID, one at a time.

### Touching SFBOFS, the forecast timeline or the data pipeline
- A stale run makes every forecast offset alias to `hour_48` and render identically `P01`
- GitHub disables cron workflows after 60 days with no *pushes*; the heartbeat exists for that `P02`
- In the download sweep a 404 is normal and breaks cleanly; anything else is transient `P03`
- The download badge counts forecast *reach*, never file count; gaps must go amber `P04`
- Fetch NOAA's `f` files (forecast), never the `n` files (nowcast/analysis) `P05`
- `ndbc.yml`'s prefix `restore-keys` is what puts SFBOFS data in the deploy `P32`

### Touching the Pi reverse proxy or disk cache
- A date in the cache key made the 30-day stale window unreachable and blanked tides `P06`
- Captive portals answer `200 text/html`; caching that shadows good JSON for weeks `P07`
- Only 404 passes through — 5xx and other 4xx must fall back to cache `P08`
- A *total* timeout on a 1.2 MB body makes satcom pre-warm impossible `P09`
- Cache writes must be atomic, and concurrent fetches for one URL coalesced `P10`
- An unbounded cache fills the SD card and stops NMEA logging with it `P11`
- A 404 at `hour_00` used to log "pre-warm complete: 0 hours" as success `P12`

### Touching browser bootstrap, tiles or the Service Worker
- Module-scope code runs before `/config.json` resolves, so it bypasses the Pi proxy `P13`
- The `/config.json` fetch gates AIS *and* NMEA — without a timeout it wedges both `P14`
- Local tiles exist only at z10-15, and "not github.io" wrongly captured localhost `P15`
- SW quota eviction deleted the app shell, and the forecast hours being sailed `P16`
- Bump `TILE_CACHE`'s version when the tile CDN changes, or dead tiles serve forever `P17`
- `hostname.endsWith(host)` also matches `evil<host>` — use exact or `'.'+host` `P18`
- Legend text interpolates fetched JSON, so `innerHTML` there is an injection sink `P19`

### Touching the JS ↔ Python mirror, tests or CI
- Station lists and the wind grid exist twice and must agree byte-for-byte `P20`
- A stale `.pyc` can make a test pass against a bug it was written to catch `P21`
- CI's `paths` filter excluded `tests/**` and `pi/**` — commits landed unreviewed `P22`

### Touching git, ports or deploys
- `.gitignore` patterns are unanchored: a bare `*.txt` swallowed every `requirements.txt` `P23`
- The Pi's port/scheme drifted across five files; check the ports table first `P24`
- `boat-mode` is a live deploy branch — pushing stages code onto the boat `P25`
- Static changes reload from disk; Python changes need `systemctl restart` `P26`

### Touching the route optimizer
- TWA is against wind-over-**water** (`wind − current`), not wind-over-ground `P27`
- A tack penalty added as *time* breaks the equal-time isochrone — reduce speed `P28`
- A point with no legal heading must still advance, as a drift point `P29`
- A pruning score must stay strictly monotonic in distance `P30`
- The 200 m land buffer must be dropped near the destination or harbors are unreachable `P31`

### Touching NMEA logging, the capture status page or playback
- The Pi has no RTC, so any duration from wall clock counts the NTP step as elapsed `P33`
- NMEA logs are race data and are never deleted; the guard is a free-space alert `P34`
- The replay transport measures the visible bottom bars; `offsetParent` is null when fixed `P35`
- A closed WebSocket's handlers fire late and clobbered replay status mid-playback `P36`
- Own-ship marker needs bulk-coalescing plus a movement deadband or it shakes `P37`
- `startup.sh` git-pulls itself, so shell/systemd changes land one boot late `P38`
- A dead logger and a silent NMEA source look identical; `/api/health` separates them `P39`
- The receiver is a single-client-TCP Lantronix XPort; any power cut can lock the Pi out `P40`

### Touching static serving, the browser cache or the boot script chain
- Unversioned JS with no `Cache-Control` let Chrome run last week's app against today's Pi `P41`

---

## File Map

### Data pipeline (`.github/`)

| File | Purpose |
|------|---------|
| `scripts/fetch_sfbofs.py` | Download NOAA SFBOFS NetCDF (f000-f048), regrid (netCDF4+scipy), write per-hour JSON |
| `scripts/fetch_ndbc.py` | NDBC buoy real-time observations (9 stations) |
| `workflows/sfbofs.yml` | Hourly at :20 — checks from nominal run time (03/09/15/21z), retries until all 48h fetched |
| `workflows/ndbc.yml` | Every 10 min; also commits the weekly keepalive heartbeat (`P02`) |
| `workflows/deploy.yml` | Tests (JS + Python + `py_compile` + `bash -n`) → assemble data → deploy Pages |

### Frontend (`static/`)

| File | Purpose |
|------|---------|
| `index.html` | Single page: tab bar (Map/Charts/Radar), map, side panel, legends, timeline, modals, playback transport |
| `hub.html` | Navigation hub at `/hub` — links every view plus live storage figures from `/api/logs` |
| `js/app.js` (~3300 lines) | Leaflet map, vessel markers, popups, CPA/TCPA, search, forecast UI, offline pre-fetch, config bootstrap, tile-layer selection |
| `js/aisstream.js` | Browser WebSocket to AISstream.io → internal vessel format |
| `js/vessel-store.js` | In-memory vessel DB, track history, localStorage persistence |
| `js/data-loader.js` | NOAA CO-OPS tides/currents/water levels + Open-Meteo wind, client-side interpolation, SFBOFS staleness gate |
| `js/router.js` + `js/route-worker.js` | Isochrone route optimizer — [docs/router.md](docs/router.md) |
| `js/nmea-parser.js` | NMEA 0183 parser (GGA, RMC, HCHDG, MWV, MWD, VHW, DPT, VTG, ROT, XDR) |
| `js/ais-decoder.js` | Browser AIS 6-bit decoder for !AIVDM/!AIVDO, types 1/2/3/5/18/19/24 |
| `js/nmea-store.js` | NMEA state manager (EventTarget), ring buffers, true-wind computation |
| `js/nmea-client.js` | Live WebSocket to `/nmea`, or file replay with speed control |
| `js/sailing-charts.js` | Charts view: 8 gauges + Chart.js time-series |
| `js/competitor-labels.js` | Leaflet tooltips: distance/speed/bearing relative to Typon |
| `js/radar-view.js` | Radar tab: polar plot, canvas rings, DOM labels, manual zoom 0.25–32nm |
| `js/tidal-flow.js` | SFBOFS particle animation + speed heatmap |
| `js/wind-overlay.js` | Wind particle animation + NDBC station markers |
| `css/style.css` | Dark nautical theme, glassmorphism, responsive mobile layout |
| `sw.js` | Service Worker — [docs/offline-cache.md](docs/offline-cache.md) |

### Pi / NMEA tooling

| File | Purpose |
|------|---------|
| `pi/boat_server.py` | **The boat server.** aiohttp: serves `static/`, reverse-proxies + disk-caches NOAA/Open-Meteo/GH-Pages, bridges NMEA (TCP client **and** UDP listener) → WS at `/nmea`, synthesizes `/config.json`, runs both pre-warm loops. HTTP :8080. |
| `pi/startup.sh` | systemd entrypoint: `git reset --hard origin/boat-mode`, start `nmea_capture.py`, exec `start_boat.sh` |
| `pi/ais-tracker.service` | systemd unit, runs as `rostape1`, `Restart=on-failure` |
| `pi/requirements.txt` | `aiohttp`, `websockets` |
| `start_boat.sh` | Foreground launcher, `PORT=8080` default |
| `nmea_capture.py` | Hourly-rotated NMEA logger into `logs/`, browsable at `/logs` |
| `nmea_ws_proxy.py` | Legacy standalone TCP→WS proxy (:8765). Superseded, but `nmea_tcp_broadcast()` and `nmea_udp_broadcast()` are still imported by `boat_server.py`. |
| `download_offline.py` | Manual idempotent tile + asset pre-fetch into `static/tiles/`. `DEFAULT_BOUNDS` is authoritative. |

### Legacy backend (root, reference / local dev only)

`main.py`, `server.py`, `sfbofs.py`, `wind.py`, `currents.py`, `tides.py` — the original FastAPI
server and the sources the `.github/scripts/` fetchers were derived from. Not deployed anywhere. The
SQLite schema they use (`vessels`, `positions`, WAL mode) is superseded in the browser by
`vessel-store.js`.

## Tests

| File | Purpose |
|------|---------|
| `tests/test_physics.mjs` | `route-worker.js` polar lookup + apparent wind math, `new Function` sandbox with stubbed `self`. In CI. |
| `tests/test_staleness.mjs` | SFBOFS staleness gate (`P01`) + download sweep (`P03`): stale-seed deadlock, NOW-vs-+4h aliasing, high-res parity, 49-call request budget. Sandboxed `fetch`, no network. In CI. |
| `tests/test_invariants.mjs` | Cross-file invariants — the traps that live in the gap between two files. `sw.js` host matching (`P18`) and quota eviction (`P16`) are **functional**: `sw.js` is loaded in a sandbox with a fake Cache API and its real functions called. Tile zoom range (`P15`) and the Pi port/scheme (`P24`) are cross-file source assertions, because the thing under test *is* agreement between two files' literals. In CI. |
| `tests/test_boat_server.py` | Pi proxy + cache: JS↔Python parity (`P20`, incl. `toFixed` comparison against real `node`), UTC-rollover alias (`P06`), captive portals (`P07`), 404-vs-5xx (`P08`), stale ceilings, single-flight, prune, allowlists. Also the NMEA transports (`P40`): UDP reassembly, source filtering, bridge-task liveness in `/api/health`. In CI; needs `pip install -r pi/requirements.txt`. |
| `tests/test_replay.mjs` | Playback scrub (`nmea-client.js`) in a sandbox with a fake store and controllable clock. The invariant: seeking to N leaves the store identical to playing through to N. Mutation-tested. In CI. |
| `tests/test_route.mjs` | End-to-end route runs against live SFBOFS + Open-Meteo. Prints ETA/distance/avg/ratio per variant, ~13s for four. **Use this instead of screenshots for router work.** Not in CI (network). |
| `pi/test_boat_server.sh` | Curl smoke test against a running Pi: proxy byte-parity, local fallback, SSRF rejection, `X-Cache` MISS→HIT, `/nmea` upgrade. Not in CI (live server). |

```bash
node tests/test_physics.mjs        # polar + apparent wind
node tests/test_staleness.mjs      # staleness gate + download sweep
node tests/test_invariants.mjs     # cross-file invariants (sw.js, zoom range, ports)
node tests/test_replay.mjs         # playback seek/scrub invariant
python3 tests/test_boat_server.py  # Pi proxy/cache + JS↔Python parity
```

CI runs all five in `deploy.yml`'s `test` job, plus `py_compile` on the Pi/root Python and `bash -n`
on the boat shell scripts.

**20 of the 41 pitfalls are mechanically enforced** — a test fails if you undo the fix. Those are
`P01` `P03` `P06` `P07` `P08` `P10` `P11` `P15` `P16` `P18` `P20` `P21` `P22` `P24` `P33` `P34` `P36` `P37` `P40` `P41`. The rest are
documentation-only: the index is the only thing standing between you and re-introducing them. If you
fix a doc-only pitfall's code area, consider whether an assertion could move it into the enforced set.

Code that implements a guarded pitfall **cites its ID in a comment**
(`grep -rnE 'P[0-4][0-9]' pi/ static/js/ nmea_ws_proxy.py` — a `P0[0-9]` pattern silently misses everything past P09),
so the trap is discoverable from the code, not only from this file.

## Data and services

| Static file | Description |
|------|-------------|
| `data/sfbofs/hour_XX.json` | SFBOFS current grid (276×325), one per forecast hour (0-48; `hour_00` = cycle time) |
| `data/wind/stations.json` | NDBC buoy observations (9 stations) |
| `data/meta.json` | Timestamps of latest SFBOFS/NDBC updates |
| `data/land_mask.json` | TIGER/Line land polygons for water/land detection — [docs/land-mask.md](docs/land-mask.md) |

| Browser-fetched | API | Data |
|--------|-----|------|
| Tides (16 stations) | NOAA CO-OPS | 3-day predictions, 6-min interval, 6h TTL |
| Water levels (6 stations) | NOAA CO-OPS `product=water_level` | Latest gauge reading, 10-min TTL |
| Currents (6 stations) | NOAA CO-OPS | 3-day predictions, 6-min interval, 6h TTL |
| Wind grid (11×16 = 176 points) | Open-Meteo | 49 forecast hours, **1 batched request**, 30-min TTL |

| External service | Auth |
|---------|------|
| AISstream.io | `DEFAULT_AISSTREAM_KEY` in `app.js`; override via `localStorage.aisstream_api_key` |
| NOAA SFBOFS (~57MB NetCDF), CO-OPS, NDBC | none, public |
| Open-Meteo | none (free tier, non-commercial) |

## Configuration

Env vars or `.env` (see `config.py`) — these drive the **legacy** backend; the browser reads
`/config.json` instead.

```
AIS_HOST=192.168.47.10    AIS_PORT=10110    AIS_PROTOCOL=auto
AISSTREAM_API_KEY=        OWN_MMSI=338361814
DB_PATH=ais_tracker.db    SERVER_HOST=127.0.0.1    SERVER_PORT=8888
```

## Running

```bash
# Local dev — tides/currents/wind fetch from live APIs, tiles from CDN
python3 -m http.server 8888 --directory static      # → http://localhost:8888

# Optional: generate SFBOFS data locally
pip install -r .github/scripts/requirements.txt && python .github/scripts/fetch_sfbofs.py

# On the Pi
sudo systemctl status|restart ais-tracker
sudo journalctl -u ais-tracker -f
```

---

## Notable behaviors

- **Three-view tab system** — Map (vessels + environment), Charts (NMEA instruments), Radar (polar
  plot). All views stay in DOM for instant switching.
- **Strategic Radar** — canvas range rings + crosshairs, DOM vessel labels, 15min track trails,
  manual zoom 0.25–32nm, speed-coloured icons. Falls back to map center with no own position.
- **NMEA pipeline** — `nmea-parser.js` → `nmea-store.js` → `sailing-charts.js` + `competitor-labels.js`.
  Live WebSocket or file replay.
- **NMEA AIS decoding** — `ais-decoder.js` decodes the boat's VHF receiver directly, so AIS works
  offline at sea; AISstream.io is the internet fallback.
- **NMEA auto-connect** — source priority: `nmeaWsUrl` from `/config.json`, then localStorage, then
  `ws://raspberrypi.local:8765` (or `wss://…:8766` when the page is HTTPS). Fails silently if
  unreachable — on GitHub Pages that failure is expected and logs one console error.
- **True wind computation** — from apparent (AWA/AWS `$IIMWV-R`) plus BSP and heading when `$IIMWD`
  is absent; `$IIMWD` overrides when present.
- **Instrument gauges** — SOG, BSP, HDG, Depth, AWA, TWA, TWD, TWS. TWA coloured green (VMG 30-50°),
  yellow (close-hauled 15-30°), red (in irons <15°).
- **Competitor labels** — distance/speed/bearing relative to Typon, toggleable, click to open the
  vessel popup.
- **50 vessel cap** — cloud AIS mode keeps only the 50 closest.
- **Forecast range asymmetry** — tides are unlimited (harmonic math, no model). Wind caps at 49h
  (Open-Meteo), current field at 48h (SFBOFS).
- **Auto-download on load** — `_autoDownload()` fires 8s after load, pre-fetches everything, retries
  with exponential backoff (30s→5min) and immediately (3s grace) when the network returns.
- **Per-category download badges** — Flow/Wind/Tide/Curr chips, green on a verified HTTP response
  (not merely loop completion), persisted in localStorage, reset after 6h. See `P04` for what the
  Flow number means.
- **Data freshness indicators** — flow and wind legends show a green/yellow dot plus relative age;
  green under 45 min. Failures render red text in the legend, not a blank.
- **Real-time water levels** — 6 of 16 tide stations have gauges. Popups show
  Predicted/Observed/Difference; dashed green ring on gauge markers. Real-time mode only
  (`forecastMinutes === 0`).
- **SFBOFS confidence indicator** — `updateFlowConfidence()` averages observed-vs-predicted delta
  across gauges: green ≤0.3ft, yellow ≤0.5ft, red above. Higher water → stronger currents and
  earlier slack.
- **Position data kept permanently** — for post-voyage analysis.
- **NMEA logs kept permanently too** — no retention sweep; a free-space alert on `:8081`
  guards the card, and a failing write says "disk", not "disconnected" (`P34`).

- **Navigation hub** at `/hub` — links every view; map stays at `/`.
- **Race playback** — pick a Pi recording, scrub to any moment; drives Map, Charts and Radar alike.
  Details: [docs/logging-and-playback.md](docs/logging-and-playback.md).

## Key patterns

- **All Python I/O is async** — `asyncio.Queue` between tasks, `asyncio.Lock` for DB writes,
  `run_in_executor` for blocking calls.
- **Network failures auto-reconnect** with 5s backoff.
- **SFBOFS unavailable** → station-based IDW interpolation fallback (but not when *stale* — `P01`).
- **Frontend markers** — `Map<mmsi, Marker>` for O(1) updates.
- **Canvas overlays** reposition on pan/zoom. Panes: tidal heatmap 449, wind 450, particles 451.
- **Forecast** — a single `forecastMinutes` offset is applied to every environmental query.

## UI conventions

- Dark theme `#0a1628` / text `#c8d6e5`; glassmorphism `backdrop-filter: blur(12px)`.
- Ship colors: Sailing `#3498db`, Cargo `#2ecc71`, Tanker `#e74c3c`, Own `#f39c12`.
- Button colors: Tide Flow blue, Wind purple, Tide cyan, Vessels orange, Route `#27ae60`, NOW blue,
  Calendar magenta, Download green; active forecast hour `#e85ab4`; OFF is dim gray.
- Wind particles purple arrow-tipped trails, every 5th flashing its speed. Tidal flow particles
  blue→cyan→green→yellow→red by speed.
- **Desktop bottom bar** — timeline strip (scrollable hours + GO) above the status bar, button bar
  above that; all buttons 28px.
- **Mobile bottom bar** — 3-row stack (download + layer toggles → forecast quick buttons → status),
  collapsible via hamburger, expanded by default. No timeline strip.
- **Mobile status bar** — `● AIS` · vessel count · [Flow][Wind][Tide][Curr] · DL age · ☰
- **Tide Flow button** is one toggle for particles + heatmap.

---

## Documentation maintenance

Two tiers, and the rule is **structural, not a byte count**:

- **CLAUDE.md** = lean index: always-on rules, ports, commands, tables that are ground truth,
  one-line pitfall/behavior summaries, pointers. A bullet here is one or two sentences; the moment it
  becomes a paragraph it belongs in a doc.
- **`docs/*.md`** = the in-depth reference for one topic.

**Do not word-shave.** This file was 41 KB as a monolith, and the fix was never to trim adjectives —
it was to move the three sections that had turned into prose (a 2.2 KB single bullet on the router,
the Pi cache semantics, the offline behavior) into `docs/`. If it feels bloated again, find the
*section that turned into prose* and move that section out. Rewriting a line tighter is not
maintenance. Rough tripwire: past ~30 KB, go looking.

**Adding a pitfall.** Writeup in [docs/pitfalls.md](docs/pitfalls.md) under a new heading with the
next unused `[Pnn]` (never renumber). CLAUDE.md gets **at most one ~15-word line** ending in that ID,
filed under the "Touching …" group for the area it traps. State the cost, not just the rule.

**When you change behavior**, put the detail in the doc and adjust the one-liner + pointer here. A
new topic doc gets a row in the map below.

## Documentation Map

| Topic | File |
|-------|------|
| **Critical pitfalls** — solved traps, full writeups, retrieve by ID | [docs/pitfalls.md](docs/pitfalls.md) |
| **Offline caching** — both layers, TTLs, stale-on-error, the date alias, SW eviction | [docs/offline-cache.md](docs/offline-cache.md) |
| **Route optimizer** — isochrone search, wind frame, pruning, polars, variants | [docs/router.md](docs/router.md) |
| **Logging, hub and playback** — retention, the disk alert, the transport, seek semantics | [docs/logging-and-playback.md](docs/logging-and-playback.md) |
| **NMEA receiver hardware** — the XPort, why a refusal is ambiguous, safe probing, TCP↔UDP | [docs/nmea-hardware.md](docs/nmea-hardware.md) |
| Land mask — TIGER/Line polygons, water/land detection | [docs/land-mask.md](docs/land-mask.md) |
| Router open work / next session notes | [docs/router-next-session.md](docs/router-next-session.md) |
| **Pre-ship review gate** — the 5 passes, pitfall injection, adversarial verification | [.claude/skills/pre-ship-review/SKILL.md](.claude/skills/pre-ship-review/SKILL.md) |
| End-user documentation | [USER_GUIDE.md](USER_GUIDE.md) |
