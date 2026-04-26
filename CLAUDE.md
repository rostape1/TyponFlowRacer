# AIS Tracker

Real-time maritime vessel tracking platform for San Francisco Bay. Combines AIS ship data with NOAA tidal currents, wind forecasts, and tide predictions on an interactive Leaflet map with particle animations.

## Tech Stack

- **Frontend:** Vanilla JS (ES6+), Leaflet 1.9.4, Canvas particle animations, no build step
- **Data Pipeline:** GitHub Actions (SFBOFS + NDBC only); tides, currents, and wind fetched directly from APIs in browser
- **Hosting:** GitHub Pages (static site + SFBOFS/NDBC data)
- **AIS:** Direct browser WebSocket to AISstream.io (no backend proxy)
- **PWA:** Service Worker (`sw.js`) + manifest for offline/installable on iPhone

## Architecture

```
GitHub Actions (scheduled)          GitHub Pages (static hosting)
├── SFBOFS: 4x/day (NetCDF→JSON)   ├── index.html + JS/CSS
└── NDBC buoys: every 10min        ├── data/sfbofs/hour_00..48.json
                                   └── data/wind/stations.json (NDBC)

Browser (PWA) — direct API fetches
├── AISstream.io WebSocket → aisstream.js (parse) → vessel-store.js (in-memory DB)
├── NOAA CO-OPS API → data-loader.js (tides: 14 stations, currents: 6 stations)
├── Open-Meteo API → data-loader.js (wind grid: 9×8, batched in 1 request)
├── Static JSON → data-loader.js (SFBOFS current field, NDBC buoy obs)
├── tidal-flow.js (SFBOFS current field particle animation)
├── wind-overlay.js (wind particle animation)
└── Service Worker caches API responses + tiles for offline
```

Environmental data sources:
- `NOAA CO-OPS API` → direct browser fetch → client-side interpolation → tide/current display
- `Open-Meteo API` → direct browser fetch (1 batched request, 72 points × 49 hours) → wind particles
- `data/sfbofs/hour_XX.json` → GitHub Actions pre-computed → `tidal-flow.js` particles
- `data/wind/stations.json` → GitHub Actions NDBC fetch → `wind-overlay.js` station markers

## File Map

### Data Pipeline (`.github/`)

| File | Purpose |
|------|---------|
| `scripts/fetch_sfbofs.py` | Download NOAA SFBOFS NetCDF, regrid (netCDF4+scipy), output per-hour JSON. Skips 6h hindcast (n000-n005); n006→hour_00 (cycle time) |
| `scripts/fetch_ndbc.py` | Fetch NDBC buoy real-time observations (9 stations) |
| `scripts/requirements.txt` | Python deps for SFBOFS processing (netCDF4, scipy, numpy) |
| `workflows/sfbofs.yml` | Cron: every hour at :20 — checks from nominal NOAA run time (03z/09z/15z/21z), retries until all 42h fetched; clears old run files on new run; saves sfbofs_run as soon as any hours succeed |
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
| `index.html` | Single page: map container, side panel, legends, forecast timeline, modals |
| `js/app.js` (~1900 lines) | Main app: Leaflet map, vessel markers, popups, CPA/TCPA, speed charts, search, forecast UI, mobile quick buttons, offline pre-fetch |
| `js/aisstream.js` | Direct browser WebSocket to AISstream.io, parses AIS messages to internal format |
| `js/vessel-store.js` | In-memory vessel database (replaces SQLite), track history, localStorage persistence |
| `js/data-loader.js` | Direct API fetcher (NOAA CO-OPS tides/currents, Open-Meteo wind) + client-side interpolation. SFBOFS/NDBC still via static JSON. |
| `js/tidal-flow.js` | Canvas particle animation + speed heatmap for tidal currents (2000-3000 particles, bilinear interpolation, offscreen-rendered color overlay) |
| `js/wind-overlay.js` | Canvas particle animation for wind (800 arrow-tipped particles with speed number flashing, NDBC station markers, dual color schemes) |
| `css/style.css` | Dark nautical theme, glassmorphism panels, responsive 3-row mobile layout, Leaflet control styling |
| `sw.js` | Service Worker: cache-first for external CDN tiles (CartoDB, OSM, NOAA, OpenSeaMap via `ais-tiles-v1` cache), network-first for HTML/JS, network-first with cache fallback for env APIs (NOAA CO-OPS, Open-Meteo) and static data JSON via `ais-data-v2` cache, stale-while-revalidate after offline download |

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
| `data/sfbofs/hour_XX.json` | SFBOFS current field grid (276x325), one per forecast hour (0-42; hour_00 = cycle time) |
| `data/wind/stations.json` | NDBC buoy observations (9 stations) |
| `data/meta.json` | Timestamps of latest SFBOFS/NDBC data updates |

## Browser-Fetched Data

| Source | API | Data |
|--------|-----|------|
| Tides (14 stations) | NOAA CO-OPS (`api.tidesandcurrents.noaa.gov`) | 3-day predictions, 6-min interval |
| Currents (6 stations) | NOAA CO-OPS (same API) | 3-day predictions, 6-min interval |
| Wind grid (9×8 = 72 points) | Open-Meteo (`api.open-meteo.com`) | 49 forecast hours, batched in 1 request |

## External Services

| Service | What | Auth | Cache |
|---------|------|------|-------|
| AISstream.io | Cloud AIS via WebSocket | API key in `.env` | Continuous stream |
| NOAA SFBOFS | SF Bay hydrodynamic NetCDF (~57MB) | Public S3 | 6 hours |
| NOAA CO-OPS | Tidal currents + tide heights (direct browser fetch) | No key | 6h in-memory cache |
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

### Legacy Backend (original server mode)

```bash
pip install -r requirements.txt
python main.py --demo              # Demo mode (simulated vessels)
python main.py --aisstream         # Cloud AIS (needs API key in .env)
```

## Notable Behaviors

- **50 vessel cap** — cloud AIS mode limits to 50 closest vessels to keep the map clean
- **AIS API key embedded** — `DEFAULT_AISSTREAM_KEY` in `app.js` so the app auto-connects on any device. Users can override via `localStorage.setItem('aisstream_api_key', ...)`.
- **Tide forecasts are unlimited range** — harmonic math, no model dependency. Wind limited to 49h (Open-Meteo forecast_hours), current field limited to 42h (SFBOFS, 48h model minus 6h hindcast)
- **Browser-side data fetching** — Tides (14 NOAA CO-OPS stations), currents (6 stations), and wind (72-point Open-Meteo grid) are fetched directly in the browser. In-memory caches: tides/currents 6h TTL, wind 30min TTL. Service Worker caches API responses for offline use.
- **Wind grid batched in 1 request** — All 72 grid points × 49 forecast hours fetched via single Open-Meteo API call with comma-separated coordinates. Direction→u/v conversion done in browser JS.
- **Auto-download on load** — `_autoDownload()` fires 8s after page load, silently pre-fetches all data (SFBOFS, NDBC, tides, currents, wind). Retries with exponential backoff (30s→60s→…→5min) if any category fails. Retries immediately (3s grace) when network comes back online. Manual download button still works with progress panel.
- **Per-category download badges** — Status bar shows Flow/Wind/Tide/Curr chips. Turn green when that category downloads successfully (checks actual HTTP response, not just loop completion). Persists in localStorage, resets after 6h. `_getDlStatus()` / `_setDlCategory()` in `app.js`.
- **SFBOFS 404 handling** — download loop breaks on first 404 (model runs don't always produce all 43 hours; later hours missing is normal).
- **SFBOFS 6h hindcast offset** — NOAA SFBOFS NetCDF files n000-n005 are hindcast (valid before the cycle time). `fetch_sfbofs.py` skips these and maps n006→hour_00 (cycle time), n007→hour_01, through n048→hour_42.
- **Offline mode** — Service Worker automatically caches all external map tiles (CartoDB, OSM, NOAA charts, OpenSeaMap) on first view via `ais-tiles-v1` cache.
- **PWA offline pre-fetch** — After download, SW switches to **stale-while-revalidate** — serves cached data instantly, refreshes in background if online. Tracks last download time in localStorage, shown in status bar ("DL: 2h ago").
- **Data freshness indicators** — Wind and current field legends show green/yellow dot with relative age (e.g. "3m ago" / "2h 30m ago"). Green = data < 45 min old, yellow = stale. Wind source shows "Open-Meteo". Both legends are same width (210px).
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
- **Button colors**: Tide Flow=blue, Wind=purple, Tide=cyan, Vessels=orange, NOW=blue, Calendar=magenta, Download=green. Active forecast hours=magenta (#e85ab4). OFF state=dim gray for all.
- **Tide Flow button** — combined toggle for SFBOFS particle animation + speed heatmap (was separate Flow + Heatmap buttons)
