# AIS Tracker

Real-time maritime vessel tracking platform for San Francisco Bay. Combines AIS ship data with NOAA tidal currents, wind forecasts, and tide predictions on an interactive Leaflet map with particle animations.

## Tech Stack

- **Frontend:** Vanilla JS (ES6+), Leaflet 1.9.4, Canvas particle animations, no build step
- **Data Pipeline:** GitHub Actions (SFBOFS + NDBC only); tides, currents, and wind fetched directly from APIs in browser (or via Pi reverse proxy on the boat)
- **Hosting:** Two deployment targets, both serve the same `static/` directory:
  - **GitHub Pages** — public URL, used at the dock / from anywhere with internet
  - **Raspberry Pi (`pi/boat_server.py`)** — on the boat WiFi, full server, port 8080 HTTP, serves the UI itself, reverse-proxies and disk-caches NOAA/Open-Meteo/SFBOFS, bridges NMEA→WebSocket. Auto-pulls `boat-mode` branch on boot.
- **AIS:** Direct browser WebSocket to AISstream.io when on internet (no backend proxy); local AIS via NMEA on the boat
- **PWA:** Service Worker (`sw.js`) registers only over HTTPS — i.e. only on GitHub Pages. The Pi serves HTTP, so the SW does **not** activate on the boat (deliberate: HTTP origin, no SW caching, fresh files each load).

## Deployment

| Where you push | What happens | Where it shows up |
|---|---|---|
| `main` branch | GitHub Actions runs `.github/workflows/deploy.yml` → tests → deploys `static/` + assembled `data/` to GitHub Pages | Public GH Pages URL (`https://rostape1.github.io/TyponFlowRacer`) |
| `boat-mode` branch | Nothing automatic. Next time the Pi boots OR you `sudo systemctl restart ais-tracker`, `pi/startup.sh` runs `git fetch && git reset --hard origin/boat-mode` before launching the server | `http://typonrpi4.local:8080/` on boat WiFi |

**Static-only changes** (HTML/JS/CSS) on the Pi: `git pull` is sufficient — aiohttp's `add_static` reads from disk per-request, no restart needed. **Python changes** to `pi/boat_server.py` require `sudo systemctl restart ais-tracker`.

SSH to Pi: `ssh rostape1@TyponRpi4.local` (see memory).

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
                          │  GET /logs         → NMEA log browser  │
                          │  WS  /nmea         → TCP 192.168.47.10 │
                          │                      :10110 bridge     │
                          │                                        │
                          │  Background: SFBOFS pre-warm loop      │
                          │  (1h cycle, exp backoff on failure)    │
                          └────────────────────────────────────────┘

Browser (one URL, two contexts):
- At the dock / shore: load GH Pages URL directly. Service Worker caches everything.
- On the boat: load http://typonrpi4.local:8080/. No SW (HTTP). Pi serves UI + proxies env data + bridges NMEA. /config.json tells the browser to use local AIS, not AISstream.io.

Browser-side modules (same code in both contexts, behavior switches on /config.json):
├── aisstream.js     (cloud AIS via WebSocket — only when useCloudAIS=true)
├── ais-decoder.js   (boat-mode: local AIS from NMEA VHF receiver)
├── nmea-client.js   (WebSocket to /nmea on Pi, or replay)
├── nmea-parser.js → nmea-store.js → sailing-charts.js, competitor-labels.js, radar-view.js
├── data-loader.js   (NOAA tides/currents/water-levels, Open-Meteo wind)
├── router.js + route-worker.js  (isochrone route optimizer)
├── tidal-flow.js    (SFBOFS particle animation)
└── wind-overlay.js  (wind particle animation)
```

Environmental data sources:
- `NOAA CO-OPS API` → direct browser fetch → client-side interpolation → tide/current display
- `NOAA CO-OPS API` → `product=water_level` → real-time gauge observations (6 stations) → observed vs predicted in tide popups + SFBOFS confidence indicator
- `Open-Meteo API` → direct browser fetch (1 batched request, 72 points × 49 hours) → wind particles
- `data/sfbofs/hour_XX.json` → GitHub Actions pre-computed → `tidal-flow.js` particles
- `data/wind/stations.json` → GitHub Actions NDBC fetch → `wind-overlay.js` station markers

## Data Source & Offline Coverage Matrix

This is the ground-truth table of every external data source the browser uses, what proxies it, what pre-caches it, and where the user sees its freshness. **Audit before changing offline behavior.**

| Layer | Pi proxy route | Pre-warmed at startup | Age shown in UI | Notes |
|---|---|---|---|---|
| SFBOFS currents | `/data/sfbofs/{hour}.json` | ✓ hours 0-48 | ✓ flow legend | GH Pages → Pi cache |
| SFBOFS GG hi-res | `/data/sfbofs_gg/{hour}.json` | ✓ same loop | ✓ | |
| HYCOM currents | `/data/hycom/*` | ✗ | ✗ | optional, outside SF Bay |
| Wind grid (Open-Meteo) | `/api/open-meteo/v1/forecast` | ✗ ← **gap** | ✓ wind legend | batched lat/lon array |
| Wind stations (NDBC) | `/data/wind/stations.json` | ✓ | ✗ | static JSON |
| Tide predictions | `/api/noaa/api/prod/datagetter` | ✗ ← **gap** | ✗ ← **gap** | 14 stations |
| Currents predictions | `/api/noaa/api/prod/datagetter` | ✗ ← **gap** | ✗ ← **gap** | 6 stations |
| Water levels (real-time) | `/api/noaa/api/prod/datagetter` | ✗ (intentional, 10-min TTL) | partial | 6 stations |
| Land mask | `/data/land_mask.json` | ✓ | n/a | |
| Meta JSON | `/data/meta.json` | ✓ | n/a | 60s TTL |
| **NOAA chart tiles** | filesystem `/tiles/noaa/{z}/{x}/{y}.png` | run `download_offline.py` | n/a | **default layer**; ArcGIS REST upstream |
| CartoDB Dark tiles | filesystem `/tiles/dark/{z}/{x}/{y}.png` | run `download_offline.py` | n/a | |
| OpenStreetMap tiles | filesystem `/tiles/osm/{z}/{x}/{y}.png` | run `download_offline.py` | n/a | |
| OpenSeaMap tiles | filesystem `/tiles/sea/{z}/{x}/{y}.png` | run `download_offline.py` | n/a | |
| Local NMEA stream | `/nmea` (WebSocket) | n/a | n/a | TCP→WS bridge to 192.168.47.10:10110 |
| AISstream.io | n/a | n/a | n/a | disabled in boat mode (`useCloudAIS:false` from `/config.json`) |

### Offline behavior

Two distinct caching layers, neither overlapping with the other:

**1. Filesystem-served map tiles** (`static/tiles/`, served by aiohttp `add_static`).
- Populated by `download_offline.py` (run manually with internet, idempotent — `download_file` skips files that already exist with size>0).
- Default bbox is SF Bay through Monterey (set in `download_offline.py:DEFAULT_BOUNDS`).
- Served as plain static files by the Pi; never refreshed by the running server.
- When the browser is on `github.io` (`_useLocalTiles=false` at `static/js/app.js:361`), tile URLs go direct to the CDN and these files are unused.

**2. Reverse-proxy disk cache** (`pi/boat_server.py` `DiskCache`, default dir `cache/`).
- Populated two ways: by `sfbofs_prewarm_loop` (every hour while online), and on-demand when a browser request misses cache.
- Per-source TTLs (`pi/boat_server.py:52-65`): SFBOFS 1h, NOAA tides/currents 6h, water levels 10min, Open-Meteo wind 30min, NDBC 10min, meta 1min.
- **Stale-on-error**: when upstream fetch fails and the cached entry exists, `proxy_with_cache` serves the cached body with `X-Cache: STALE` + `X-Cache-Age` headers — up to `MAX_STALE_S` (currently 24h, planned to extend per source).

**The Pi serves HTTP, not HTTPS** (default `--port 8080` in `start_boat.sh`). Browsers refuse to register Service Workers on HTTP origins, so `static/sw.js` does **not** activate when the page is loaded from the Pi. All caching at sea is server-side. `sw.js` is only relevant on the GitHub Pages URL.

### Two URLs, one codebase

| URL | Hosting | When | What's different |
|---|---|---|---|
| `https://rostape1.github.io/TyponFlowRacer` | GitHub Pages (HTTPS) | Dock, shore, anywhere with internet | `/config.json` 404s → app falls back to web mode (cloud AIS, direct CDN tiles, direct API fetches). Service Worker active. |
| `http://typonrpi4.local:8080/` | Raspberry Pi (HTTP) | On boat WiFi | `/config.json` flips `mode:'boat'`, `useCloudAIS:false`, points API base at `/api/noaa` and `/api/open-meteo`, NMEA at `/nmea`. No Service Worker. |

Same `static/` directory deployed to both. `boat-mode` branch is what the Pi runs (`pi/startup.sh` does `git reset --hard origin/boat-mode` on every (re)boot). `main` branch is what GitHub Pages deploys.

## File Map

### Data Pipeline (`.github/`)

| File | Purpose |
|------|---------|
| `scripts/fetch_sfbofs.py` | Download NOAA SFBOFS NetCDF forecast files (f000-f048), regrid (netCDF4+scipy), output per-hour JSON. f000 = cycle time |
| `scripts/fetch_ndbc.py` | Fetch NDBC buoy real-time observations (9 stations) |
| `scripts/requirements.txt` | Python deps for SFBOFS processing (netCDF4, scipy, numpy) |
| `workflows/sfbofs.yml` | Cron: every hour at :20 — checks from nominal NOAA run time (03z/09z/15z/21z), retries until all 48h fetched; clears old run files on new run; saves sfbofs_run as soon as any hours succeed |
| `workflows/ndbc.yml` | Cron: every 10 min (buoy observations); restores full cache (restore-keys: env-data-) so deploy always includes SFBOFS data |
| `workflows/deploy.yml` | Assembles data + static site, deploys to GitHub Pages |

### Legacy Backend (project root, kept for reference/local dev)

| File | Purpose |
|------|---------|
| `main.py` | Entry point for local backend server (FastAPI) |
| `server.py` | FastAPI routes + WebSocket broadcast |
| `sfbofs.py` | Original SFBOFS processing (source for fetch_sfbofs.py) |
| `wind.py` | Original wind fetching (source for fetch_wind.py + fetch_ndbc.py) |
| `currents.py` | Original currents fetching (source for fetch_currents.py) |
| `tides.py` | Original tides fetching (source for fetch_tides.py) |

### Frontend (`static/`)

| File | Purpose |
|------|---------|
| `index.html` | Single page: tab bar (Map/Charts), map container, side panel, legends, forecast timeline, modals, sailing dashboard |
| `js/app.js` (~2400 lines) | Main app: Leaflet map, vessel markers, popups, CPA/TCPA, speed charts, search, forecast UI, mobile quick buttons, offline pre-fetch, NMEA/sailing integration |
| `js/aisstream.js` | Direct browser WebSocket to AISstream.io, parses AIS messages to internal format |
| `js/vessel-store.js` | In-memory vessel database (replaces SQLite), track history, localStorage persistence |
| `js/data-loader.js` | Direct API fetcher (NOAA CO-OPS tides/currents/water levels, Open-Meteo wind) + client-side interpolation. SFBOFS/NDBC still via static JSON. Exposes `getWindGridForHour()` and `getSfbofsRunTime()` for router. |
| `js/router.js` | Isochrone route optimizer: Swan 47 polars, RouterDataStore (pre-loads multi-hour SFBOFS+wind grids with forecast-time-aware temporal interpolation), NOAA ENC land mask, isochrone expansion/pruning, RouteRenderer (colored Leaflet polyline) |
| `js/nmea-parser.js` | Pure NMEA 0183 sentence parser (GGA, RMC, HCHDG, MWV, MWD, VHW, DPT, VTG, ROT, XDR). Handles log file formats with timestamps and source tags. |
| `js/ais-decoder.js` | Browser-side AIS 6-bit decoder for !AIVDM/!AIVDO sentences. Decodes types 1/2/3/5/18/19/24. Multi-part message buffering. |
| `js/nmea-store.js` | NMEA state manager (EventTarget). Current instrument values + time-series ring buffers (1hr @ 1Hz). True wind computation from apparent wind. Events: update, position, ais. |
| `js/nmea-client.js` | NMEA data source: live WebSocket to nmea_ws_proxy.py or file replay with speed control (1x/2x/5x/10x/max). |
| `js/sailing-charts.js` | Charts view: 8 instrument gauges (SOG, BSP, HDG, Depth, AWA, TWA, TWD, TWS) + Chart.js time-series (TWA/TWD/TWS/BSP). |
| `js/competitor-labels.js` | Leaflet tooltips on competitor vessels: distance+trend, speed+trend, bearing, name relative to Typon. |
| `js/radar-view.js` | Strategic Radar tab: polar plot of vessels relative to own ship. Canvas range rings + crosshairs, DOM vessel labels, track trails. Manual zoom (scroll wheel + buttons, 0.25–32nm). |
| `js/tidal-flow.js` | Canvas particle animation + speed heatmap for tidal currents (2000-3000 particles, bilinear interpolation, offscreen-rendered color overlay) |
| `js/wind-overlay.js` | Canvas particle animation for wind (800 arrow-tipped particles with speed number flashing, NDBC station markers, dual color schemes) |
| `css/style.css` | Dark nautical theme, glassmorphism panels, responsive 3-row mobile layout, Leaflet control styling, sailing dashboard |
| `sw.js` | Service Worker: cache-first for external CDN tiles (CartoDB, OSM, NOAA, OpenSeaMap, jsdelivr via `ais-tiles-v1` cache), network-first for HTML/JS, network-first with cache fallback for env APIs (NOAA CO-OPS, Open-Meteo) and static data JSON via `ais-data-v2` cache, stale-while-revalidate after offline download |

### Sailing / NMEA (project root)

| File | Purpose |
|------|---------|
| `nmea_ws_proxy.py` | Legacy standalone TCP-to-WebSocket proxy (port 8765). Superseded by `pi/boat_server.py`. Its `nmea_tcp_broadcast()` function is still imported by `boat_server.py`. |
| `pi/boat_server.py` | **Current Pi server.** aiohttp app that serves `static/`, reverse-proxies + disk-caches NOAA/Open-Meteo/GH-Pages, bridges NMEA TCP→WebSocket at `/nmea`, synthesizes `/config.json`, runs SFBOFS pre-warm loop. HTTP :8080 by default; HTTPS optional with `--ssl-cert/--ssl-key`. |
| `pi/startup.sh` | systemd entrypoint. `git fetch && git reset --hard origin/boat-mode`, starts `nmea_capture.py` for log files, then execs `start_boat.sh`. |
| `pi/ais-tracker.service` | systemd unit running as `rostape1`. `Restart=on-failure`. |
| `start_boat.sh` | Foreground launcher for `pi/boat_server.py`. Used by systemd (via `startup.sh`) and for manual runs. |
| `nmea_capture.py` | NMEA sentence logger writing hourly-rotated files into `logs/`. Started by `startup.sh` alongside the Pi server; logs are also browsable via `/logs` on the Pi. |

## Database Schema

Two tables in SQLite (`ais_tracker.db`):

**`vessels`** — static metadata (updated on AIS static messages)
- `mmsi` (PK), `name`, `ship_type`, `ship_category`, `destination`, `length`, `beam`
- `is_own_vessel`, `first_seen`, `last_seen`

**`positions`** — every AIS position update
- `id` (PK), `mmsi` (FK), `lat`, `lon`, `sog`, `cog`, `heading`, `timestamp`
- Indexes: `mmsi`, `timestamp`, `(mmsi, timestamp)`

WAL mode + async lock serializes writes. Periodic commits every 2 seconds.

## Tests

| File | Purpose |
|------|---------|
| `tests/test_physics.mjs` | Node-based sanity tests for `route-worker.js` polar lookup + apparent wind math. Loads worker source via `new Function` sandbox with a stubbed `self`. Run: `node tests/test_physics.mjs` (also runs in CI before deploy). |
| `tests/test_route.mjs` | End-to-end route tests against live SFBOFS + Open-Meteo + land mask fetched from GitHub Pages. Runs the worker headlessly in Node and prints ETA/distance/avg/ratio per variant. Use this instead of click-and-screenshot when iterating on the router. `node tests/test_route.mjs [--variant=X] [--start=lat,lon --end=lat,lon] [--base=local]`. ~13 s for a full four-variant sweep. |

## Static Data Files

| Path | Description |
|------|-------------|
| `data/sfbofs/hour_XX.json` | SFBOFS current field grid (276x325), one per forecast hour (0-48; hour_00 = cycle time) |
| `data/wind/stations.json` | NDBC buoy observations (9 stations) |
| `data/meta.json` | Timestamps of latest SFBOFS/NDBC data updates |
| `data/land_mask.json` | US Census TIGER/Line land polygons for route optimizer water/land detection (see [docs/land-mask.md](docs/land-mask.md)) |

## Browser-Fetched Data

| Source | API | Data |
|--------|-----|------|
| Tides (14 stations) | NOAA CO-OPS (`api.tidesandcurrents.noaa.gov`) | 3-day predictions, 6-min interval |
| Water levels (6 stations) | NOAA CO-OPS (same API, `product=water_level`) | Latest gauge reading, 10-min cache |
| Currents (6 stations) | NOAA CO-OPS (same API) | 3-day predictions, 6-min interval |
| Wind grid (9×8 = 72 points) | Open-Meteo (`api.open-meteo.com`) | 49 forecast hours, batched in 1 request |

## External Services

| Service | What | Auth | Cache |
|---------|------|------|-------|
| AISstream.io | Cloud AIS via WebSocket | API key in `.env` | Continuous stream |
| NOAA SFBOFS | SF Bay hydrodynamic NetCDF (~57MB) | Public S3 | 6 hours |
| NOAA CO-OPS | Tidal currents + tide heights + water levels (direct browser fetch) | No key | Tides/currents 6h, water levels 10min |
| Open-Meteo | Wind forecast grid (direct browser fetch, batched) | No key (free tier, non-commercial) | 30min in-memory cache |
| NDBC NOAA | Real-time buoy observations | Public | 10 min |

## Configuration

Via environment variables or `.env` file (see `config.py`):

```
AIS_HOST=192.168.47.10    # Local AIS receiver IP
AIS_PORT=10110            # Local AIS receiver port
AIS_PROTOCOL=auto         # auto/tcp/udp
AISSTREAM_API_KEY=        # AISstream.io API key
OWN_MMSI=338361814        # Highlighted own vessel
DB_PATH=ais_tracker.db    # Default: project directory (not ~/.ais_tracker/)
SERVER_HOST=127.0.0.1     # Default local; Fly.io overrides to 0.0.0.0
SERVER_PORT=8888           # Default local; Fly.io overrides to 8080
```

## Running

### Production (GitHub Pages)

Push to `main` branch. GitHub Actions will:
1. Fetch SFBOFS and NDBC data on schedule
2. Deploy `static/` + `data/` to GitHub Pages automatically
3. Tides, currents, and wind are fetched directly by the browser from public APIs

### Local Development

```bash
# Optional: generate SFBOFS data locally
pip install -r .github/scripts/requirements.txt
python .github/scripts/fetch_sfbofs.py  # needs netCDF4 + scipy

# Serve the static site (tides/currents/wind fetch from APIs automatically)
python -m http.server 8888 --directory static
```

Open **http://localhost:8888**. You'll be prompted for your AISstream.io API key on first load (stored in localStorage).

### Tests

```bash
node tests/test_physics.mjs   # polar lookup + apparent wind sanity tests
```

CI runs this in the `test` job of `.github/workflows/deploy.yml` before the deploy job.

### Legacy Backend (original server mode)

```bash
pip install -r requirements.txt
python main.py --demo              # Demo mode (simulated vessels)
python main.py --aisstream         # Cloud AIS (needs API key in .env)
```

### Raspberry Pi (on the boat)

The Pi auto-starts on boot via systemd. Manual control:

```bash
sudo systemctl status ais-tracker          # is it running?
sudo systemctl restart ais-tracker         # picks up Python changes
sudo journalctl -u ais-tracker -f          # live logs
```

`pi/startup.sh` does `git fetch && git reset --hard origin/boat-mode` on each (re)start, so committing+pushing to `boat-mode` is the deploy path. For static-only changes you can also `cd ~/AIS-Tracker && git pull` without restarting — aiohttp serves files from disk per-request.

Browse to **http://typonrpi4.local:8080/** on the boat WiFi. The Pi serves the UI itself (no internet needed once SFBOFS/wind data has been pre-warmed at the dock), bridges NMEA at `/nmea`, and reverse-proxies NOAA/Open-Meteo when there's connectivity.

SSH: `ssh rostape1@TyponRpi4.local`

## Notable Behaviors

- **Three-view tab system** — Map tab (vessel tracking + environmental overlays), Charts tab (NMEA sailing instruments + time-series), and Radar tab (polar plot of nearby vessels). Tab bar at top, all views stay in DOM for instant switching.
- **Strategic Radar** — `radar-view.js` draws range rings + crosshairs on canvas, positions vessel labels as DOM overlays. Manual zoom via scroll wheel or +/- buttons (0.25–32nm range steps). Track trails show recent vessel movement (15min history, clamped to radar range). Falls back to map center when no own position. Speed-colored vessel icons (blue/green/yellow/red).
- **Two URLs, one codebase** — At the dock or on shore use the GitHub Pages URL (`https://rostape1.github.io/TyponFlowRacer`). On the boat WiFi use `http://typonrpi4.local:8080/`. The Pi-served `/config.json` flips the browser to boat mode (local NMEA AIS, env data via Pi proxy) without a separate build.
- **NMEA data pipeline** — `nmea-parser.js` → `nmea-store.js` → `sailing-charts.js` (Charts view) and `competitor-labels.js` (Map view). Data from live WebSocket (`/nmea` on the Pi) or file replay.
- **NMEA AIS decoding** — `ais-decoder.js` decodes raw !AIVDM/!AIVDO sentences from the boat's VHF receiver. Works offline at sea. Falls back to AISstream.io cloud when internet available.
- **Competitor labels** — Toggleable via Labels button. Shows distance (nm), speed (kn), and relative bearing next to each vessel marker. Click a label to open vessel detail popup. Falls back to map center when Typon's position is unknown.
- **Instrument gauges** — 8 gauges: SOG, BSP, HDG, Depth, AWA, TWA, TWD, TWS. TWA color-coded: green (optimal VMG 30-50°), yellow (close-hauled 15-30°), red (in irons <15°).
- **True wind computation** — When only apparent wind (AWA/AWS from $IIMWV-R) is available, computes TWS/TWA/TWD from vector math using BSP and heading. $IIMWD values override when present.
- **NMEA auto-connect** — `nmea-client.js` opens a WebSocket on page load. Source priority: (1) `nmeaWsUrl` from `/config.json` if served by the Pi (resolves `/nmea` against the page origin), (2) saved URL in localStorage, (3) default `ws://raspberrypi.local:8765` (legacy fallback for the old standalone proxy). Connects if reachable, fails silently if not.
- **50 vessel cap** — cloud AIS mode limits to 50 closest vessels to keep the map clean
- **AIS API key embedded** — `DEFAULT_AISSTREAM_KEY` in `app.js` so the app auto-connects on any device. Users can override via `localStorage.setItem('aisstream_api_key', ...)`.
- **Tide forecasts are unlimited range** — harmonic math, no model dependency. Wind limited to 49h (Open-Meteo forecast_hours), current field limited to 48h (SFBOFS)
- **Browser-side data fetching** — Tides (14 NOAA CO-OPS stations), currents (6 stations), and wind (72-point Open-Meteo grid) are fetched directly in the browser. In-memory caches: tides/currents 6h TTL, wind 30min TTL. On the Pi these requests are reverse-proxied through `boat_server.py` and disk-cached so all clients on the boat WiFi share one upstream fetch. On GitHub Pages the Service Worker caches API responses for offline use.
- **Wind grid batched in 1 request** — All 72 grid points × 49 forecast hours fetched via single Open-Meteo API call with comma-separated coordinates. Direction→u/v conversion done in browser JS.
- **Auto-download on load** — `_autoDownload()` fires 8s after page load, silently pre-fetches all data (SFBOFS, NDBC, tides, currents, wind). Retries with exponential backoff (30s→60s→…→5min) if any category fails. Retries immediately (3s grace) when network comes back online. Manual download button still works with progress panel.
- **Per-category download badges** — Status bar shows Flow/Wind/Tide/Curr chips. Turn green when that category downloads successfully (checks actual HTTP response, not just loop completion). Persists in localStorage, resets after 6h. `_getDlStatus()` / `_setDlCategory()` in `app.js`.
- **SFBOFS 404 handling** — download loop breaks on first 404 (model runs don't always produce all 49 hours; later hours missing is normal).
- **SFBOFS file convention** — NOAA publishes nowcast files (n000-n006, hindcast/analysis) and forecast files (f000-f048, actual forecasts). We fetch only the `f` files: f000 = cycle time, f048 = +48h.
- **Offline behavior on GitHub Pages** — Service Worker (`sw.js`) caches external map tiles (CartoDB, OSM, NOAA charts, OpenSeaMap) on first view via `ais-tiles-v1` cache, plus API responses via `ais-data-v2`. Only active over HTTPS — does **not** run when accessed via the Pi (`http://typonrpi4.local:8080/`).
- **Offline behavior on the Pi** — No service worker (HTTP origin). Instead, `pi/boat_server.py` does the caching server-side: a disk cache keyed by SHA1(url) under `cache/`, fresh-on-TTL, stale-on-error (up to `MAX_STALE_S = 24h`), with a startup pre-warm loop that fetches all 49 SFBOFS hours + NDBC + land mask from GitHub Pages every hour while internet is available. So the boat keeps working through satcom outages.
- **Data freshness indicators** — Wind and current field legends show green/yellow dot with relative age (e.g. "3m ago" / "2h 30m ago"). Green = data < 45 min old, yellow = stale. Wind source shows "Open-Meteo". Both legends are same width (210px).
- **Real-time water levels** — 6 of 14 tide stations have NOAA gauges (SF, Alameda, Redwood City, Richmond, Martinez, Port Chicago). `fetchWaterLevels()` in data-loader.js fetches `product=water_level&date=latest`, 10-min cache. Popups show Predicted/Observed/Difference. Dashed green ring on gauge station markers. Only in real-time mode (forecastMinutes === 0).
- **SFBOFS confidence indicator** — `updateFlowConfidence()` in app.js computes avg observed-vs-predicted delta across gauge stations. Shows in flow legend: green (≤0.3ft, reliable), yellow (≤0.5ft, moderate), red (>0.5ft, low). Detail text indicates direction: higher water → stronger currents & earlier slack; lower water → weaker currents & later slack.
- **Route optimizer** — `computeRoute()` in `router.js` orchestrates an isochrone search executed in `js/route-worker.js` (Web Worker, off the main thread). Pre-loads up to 49 hours of SFBOFS current + Open-Meteo wind grids into `RouterDataStore` with temporal interpolation between hourly grids; sub-hour forecast offsets are passed to the worker (`gridOffsetH`) so grid lookups align to the actual start clock instead of flooring to the hour. **48h time budget** (`MAX_TIME_S = 172800`), 72 headings (144 in the Golden Gate HR zone), 60–300 s timesteps depending on whether any wavefront point is in the HR zone / near land / open water. **Wind frame:** TWA is computed against wind-over-water (`wind − current`), not wind-over-ground — the polar expects what the sails actually feel. **Tack penalty** (60 s for >60° change, 20 s for >30°) is applied as a *speed reduction* during the step, not a time addition, so the wavefront stays on a single equal-time isochrone. **Drift fallback:** if a point has wind < 0.5 kn or every heading from it hits land, a single drift point is pushed at the same wall-clock advance, displaced only by the current — so calm patches don't silently empty the wavefront. **Pruning:** best-by-distFromStart per 2° angular sector (180 sectors) for baseline; VMC variant biases by `dist * (1 + 0.5·cos(brg − destBrg))` (range 0.5–1.5× dist, strictly monotonic in dist). `bestToDest` always carried forward. **Destination approach:** within 1 nm of destination (`DEST_APPROACH_NM`), the 200 m land buffer is dropped and strict `_isLand` is used, so harbors and shoreline waypoints can be reached. Swan 47 polars with configurable performance factor (default 85%) and optional `polarFalloff` variant (linear degrade 1.0 @ 150° → 0.85 @ 180°). Variant dropdown in route-planner panel (baseline / vmc / falloff / both); changing it auto-recomputes. Route colored by current benefit: green (favorable >0.3kn), red (adverse >0.3kn), orange (neutral). Time labels at dynamic interval (15min / 30min / 1h / 2h based on total duration). **Details table** (via button) shows BSP, TWS, TWA, AWS, AWA per waypoint, color-coded by point of sail.
- **Position data kept permanently** — for post-voyage analysis

## Key Patterns

- **All I/O is async** — `asyncio.Queue` for inter-task comms, `asyncio.Lock` for DB writes
- **Blocking calls** use `loop.run_in_executor(None, fn)` to run in thread pool
- **Network failures** auto-reconnect with 5s backoff
- **SFBOFS unavailable** → falls back to station-based IDW interpolation
- **Frontend marker management** — `Map<mmsi, Marker>` for O(1) updates
- **Canvas overlays** reposition on map pan/zoom events. Tidal heatmap uses separate pane (z-index 449) below particles (451) and wind (450)
- **Wind particle rendering** — arrow-tipped trails drawn per-frame on canvas. Every 5th particle flashes its speed number (dark pill + colored text) during age 30-80 with fade in/hold/fade out. Numbers drawn after the canvas fade pass to stay crisp. Default color scheme: purple.
- **Forecast** — `forecastMinutes` offset applied to all environmental API queries

## UI Conventions

- Dark theme: `#0a1628` background, `#c8d6e5` text
- Glassmorphism: `backdrop-filter: blur(12px)` on panels
- Leaflet zoom/layer controls: dark nautical theme (desktop), hidden on mobile (pinch-to-zoom)
- Ship type colors: Sailing=#3498db, Cargo=#2ecc71, Tanker=#e74c3c, Own=#f39c12
- Wind particles: purple arrow-tipped trails with flashing speed numbers (every 5th particle). Default color scheme: purple
- Tidal flow particles: smooth colored line trails (blue→cyan→green→yellow→red by speed)
- **Desktop bottom bar**: Timeline strip (scrollable hours + GO button) above status bar. Button bar above timeline with NOW, calendar, download, and layer toggles (Tide Flow, Wind, Tide, Vessels). All buttons are 28px tall with consistent toggle styling.
- **Mobile bottom bar**: 3-row stack (download full-width + layer toggles → forecast quick buttons → status line), collapsible via hamburger button (default: expanded). Timeline scroll strip hidden on mobile. Vessel list auto-closes when tray is collapsed.
- **Mobile forecast quick buttons**: NOW, +1h, +2h, +3h, +4h, Set FCST TIME (opens date/time picker)
- **Mobile status bar**: `● AIS` dot+label · vessel count · [Flow][Wind][Tide][Curr] download badges · DL age · ☰ hamburger
- **Button colors**: Tide Flow=blue, Wind=purple, Tide=cyan, Vessels=orange, Route=green (#27ae60), NOW=blue, Calendar=magenta, Download=green. Active forecast hours=magenta (#e85ab4). OFF state=dim gray for all.
- **Tide Flow button** — combined toggle for SFBOFS particle animation + speed heatmap (was separate Flow + Heatmap buttons)
