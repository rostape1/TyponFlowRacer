# AIS Tracker

Real-time maritime vessel tracking platform for San Francisco Bay. Combines AIS ship data with NOAA tidal currents, wind forecasts, and tide predictions on an interactive Leaflet map with particle animations.

## Tech Stack

- **Frontend:** Vanilla JS (ES6+), Leaflet 1.9.4, Canvas particle animations, no build step
- **Data Pipeline:** GitHub Actions (scheduled Python scripts fetch/process environmental data)
- **Hosting:** GitHub Pages (static site + pre-computed JSON data)
- **AIS:** Direct browser WebSocket to AISstream.io (no backend proxy)
- **PWA:** Service Worker (`sw.js`) + manifest for offline/installable on iPhone

## Architecture

```
GitHub Actions (scheduled)          GitHub Pages (static hosting)
├── SFBOFS: 4x/day (NetCDF→JSON)   ├── index.html + JS/CSS
├── Wind grid: hourly (HRRR)       ├── data/sfbofs/hour_00..48.json
├── NDBC buoys: every 10min        ├── data/wind/hour_00..48.json + stations.json
├── Tides: 2x/day                  ├── data/tides/{14 stations}.json
└── Currents: 2x/day               └── data/currents/{6 stations}.json

Browser (PWA)
├── AISstream.io WebSocket → aisstream.js (parse) → vessel-store.js (in-memory DB)
├── Static JSON → data-loader.js (fetch + client-side interpolation)
├── tidal-flow.js (SFBOFS current field particle animation)
├── wind-overlay.js (HRRR wind particle animation)
└── Service Worker caches everything for offline
```

Environmental data is pre-computed by GitHub Actions and served as static JSON:
- `data/sfbofs/hour_XX.json` → `tidal-flow.js` particles
- `data/wind/hour_XX.json` + `stations.json` → `wind-overlay.js` particles
- `data/tides/{station}.json` → client-side interpolation → tide display
- `data/currents/{station}.json` → client-side interpolation → current overlay

## File Map

### Data Pipeline (`.github/`)

| File | Purpose |
|------|---------|
| `scripts/fetch_sfbofs.py` | Download NOAA SFBOFS NetCDF, regrid (netCDF4+scipy), output per-hour JSON |
| `scripts/fetch_wind.py` | Fetch HRRR wind grid via Open-Meteo (72 points, all hours in 1 request per point) |
| `scripts/fetch_ndbc.py` | Fetch NDBC buoy real-time observations (9 stations) |
| `scripts/fetch_tides.py` | Fetch NOAA CO-OPS tide predictions (14 stations, 3 days) |
| `scripts/fetch_currents.py` | Fetch NOAA CO-OPS current predictions (6 stations, 3 days) |
| `scripts/requirements.txt` | Python deps for SFBOFS processing (netCDF4, scipy, numpy) |
| `workflows/sfbofs.yml` | Cron: every 6h after model runs |
| `workflows/wind.yml` | Cron: hourly (HRRR updates) |
| `workflows/ndbc.yml` | Cron: every 10 min (buoy observations) |
| `workflows/tides.yml` | Cron: 2x/day (tides + currents, deterministic predictions) |
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
| `index.html` | Single page: map container, side panel, legends, forecast timeline, modals |
| `js/app.js` (~1900 lines) | Main app: Leaflet map, vessel markers, popups, CPA/TCPA, speed charts, search, forecast UI, mobile quick buttons, offline pre-fetch |
| `js/aisstream.js` | Direct browser WebSocket to AISstream.io, parses AIS messages to internal format |
| `js/vessel-store.js` | In-memory vessel database (replaces SQLite), track history, localStorage persistence |
| `js/data-loader.js` | Static JSON fetcher + client-side interpolation for tides/currents |
| `js/tidal-flow.js` | Canvas particle animation + speed heatmap for tidal currents (2000-3000 particles, bilinear interpolation, offscreen-rendered color overlay) |
| `js/wind-overlay.js` | Canvas particle animation for wind (800 arrow-tipped particles with speed number flashing, NDBC station markers, dual color schemes) |
| `js/tidal-flow.js` | Canvas particle animation + speed heatmap for tidal currents (2000-3000 particles, bilinear interpolation, offscreen-rendered color overlay) |
| `js/wind-overlay.js` | Canvas particle animation for wind (800 arrow-tipped particles with speed number flashing, NDBC station markers, dual color schemes) |
| `css/style.css` | Dark nautical theme, glassmorphism panels, responsive 3-row mobile layout, Leaflet control styling |
| `sw.js` | Service Worker: cache-first for external CDN tiles (CartoDB, OSM, NOAA, OpenSeaMap via `ais-tiles-v1` cache), network-first for HTML/JS, stale-while-revalidate for environmental APIs after download (`ais-env-data-v1` cache) |

## Database Schema

Two tables in SQLite (`ais_tracker.db`):

**`vessels`** — static metadata (updated on AIS static messages)
- `mmsi` (PK), `name`, `ship_type`, `ship_category`, `destination`, `length`, `beam`
- `is_own_vessel`, `first_seen`, `last_seen`

**`positions`** — every AIS position update
- `id` (PK), `mmsi` (FK), `lat`, `lon`, `sog`, `cog`, `heading`, `timestamp`
- Indexes: `mmsi`, `timestamp`, `(mmsi, timestamp)`

WAL mode + async lock serializes writes. Periodic commits every 2 seconds.

## Static Data Files

| Path | Description |
|------|-------------|
| `data/sfbofs/hour_XX.json` | SFBOFS current field grid (276x325), one per forecast hour (0-48) |
| `data/wind/hour_XX.json` | HRRR wind grid (9x8), one per forecast hour (0-48) |
| `data/wind/stations.json` | NDBC buoy observations (9 stations) |
| `data/tides/{station_id}.json` | Tide height predictions (3 days) per station |
| `data/currents/{station_id}.json` | Tidal current predictions (3 days) per station |
| `data/meta.json` | Timestamps of latest data updates |

## External Services

| Service | What | Auth | Cache |
|---------|------|------|-------|
| AISstream.io | Cloud AIS via WebSocket | API key in `.env` | Continuous stream |
| NOAA SFBOFS | SF Bay hydrodynamic NetCDF (~57MB) | Public S3 | 6 hours |
| NOAA CO-OPS | Tidal currents + tide heights | No key | 6 hours |
| Open-Meteo | HRRR wind forecast grid (shown as "NOAA HRRR" in UI) | No key | 30 min |
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
1. Fetch all environmental data on schedule
2. Deploy `static/` + `data/` to GitHub Pages automatically

### Local Development

```bash
# Generate data locally (one-time)
pip install -r .github/scripts/requirements.txt
python .github/scripts/fetch_tides.py
python .github/scripts/fetch_currents.py
python .github/scripts/fetch_wind.py
python .github/scripts/fetch_ndbc.py
python .github/scripts/fetch_sfbofs.py  # needs netCDF4 + scipy

# Serve the static site
python -m http.server 8888 --directory static
```

Open **http://localhost:8888**. You'll be prompted for your AISstream.io API key on first load (stored in localStorage).

### Legacy Backend (original server mode)

```bash
pip install -r requirements.txt
python main.py --demo              # Demo mode (simulated vessels)
python main.py --aisstream         # Cloud AIS (needs API key in .env)
```

## Notable Behaviors

- **50 vessel cap** — cloud AIS mode limits to 50 closest vessels to keep the map clean
- **Tide forecasts are unlimited range** — harmonic math, no model dependency. Wind/current field limited to 48h (HRRR/SFBOFS)
- **Startup environmental refresh** — background task fetches all 48h of wind, current field, tidal currents, and tide height data on startup, then repeats every 30 minutes. Saves all forecast data to disk (`static/data/`) for offline use.
- **Offline forecast persistence** — All 48h of forecast data (wind grid, SFBOFS current field, tidal currents, tide heights) is saved to JSON files on disk after each refresh cycle. On restart (even offline), forecast caches load from disk immediately so forecast mode works without internet.
- **Offline cache files** — `static/data/wind_forecasts.json` (48h wind grid), `static/data/sfbofs_forecasts.json` (48h current field), `static/data/tides/*.json` (14 station predictions), `static/data/currents/*.json` (6 station predictions), plus hour-0 files `wind_field.json`, `wind_stations.json`, `sfbofs_field.json`
- **Offline mode** — Service Worker automatically caches all external map tiles (CartoDB, OSM, NOAA charts, OpenSeaMap) on first view via `ais-tiles-v1` cache. `download_offline.py` can additionally pre-cache tiles, currents, wind for areas not yet viewed.
- **PWA offline pre-fetch** — Download button (in bottom button bar) pre-fetches 24h of tide, current, wind, and current-field data for offline PWA use. Probes wind/SFBOFS `fetched_at` before downloading — skips if data unchanged since last download. Tracks last download time in localStorage, shown in status bar ("DL: 2h ago") and download panel. After download, Service Worker switches to **stale-while-revalidate** for env API requests — serves cached data instantly, refreshes in background if online. `X-From-Cache` response header signals cached data to the app. Loading progress bar suppressed when serving from cache.
- **Data freshness indicators** — Wind and current field legends show green/yellow dot with relative age (e.g. "3m ago" / "2h 30m ago"). Green = data < 45 min old, yellow = stale. Both legends are same width (210px).
- **Wind grid fetched in parallel** — 72 grid points fetched concurrently via ThreadPoolExecutor (20 workers) instead of sequentially. Reduces wind data load from minutes to ~3 seconds.
- **SFBOFS returns stale cache immediately** — if disk cache exists, `/api/current-field` returns it instantly while refreshing the 57MB NetCDF download in the background. No more blocking on slow S3 downloads.
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
- **Desktop bottom bar**: Timeline strip (scrollable hours + GO button) above status bar. Button bar above timeline with NOW, calendar, download, and layer toggles (Flow, Heatmap, Wind, Tide, Vessels). All buttons are 28px tall with consistent toggle styling.
- **Mobile bottom bar**: 3-row stack (layer toggles + download → forecast quick buttons → status line), collapsible via hamburger button (default: expanded). Timeline scroll strip hidden on mobile. Vessel list auto-closes when tray is collapsed.
- **Mobile forecast quick buttons**: NOW, +1h, +2h, +3h, +4h, Set FCST TIME (opens date/time picker)
- **Button colors**: Flow=blue, Heatmap=orange, Wind=purple, Tide=cyan, Vessels=orange, NOW=blue, Calendar=magenta, Download=green. Active forecast hours=magenta (#e85ab4). OFF state=dim gray for all.
