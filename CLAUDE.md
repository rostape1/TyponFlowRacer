# AIS Tracker

Real-time maritime vessel tracking platform for San Francisco Bay. Combines AIS ship data with NOAA tidal currents, wind forecasts, and tide predictions on an interactive Leaflet map with particle animations.

## Tech Stack

- **Backend:** Python 3.11, FastAPI + Uvicorn (async throughout), SQLite (aiosqlite, WAL mode)
- **Frontend:** Vanilla JS (ES6+), Leaflet 1.9.4, Canvas particle animations, no build step
- **Deployment:** Fly.io (Docker, `sjc` region), also runs on Raspberry Pi
- **PWA:** Service Worker (`sw.js`) + manifest for offline/installable support

## Architecture

```
AIS Source (local hardware / AISstream.io / demo)
    ↓
ais_decoder.py (NMEA → dict via pyais)
    ↓
asyncio.Queue
    ↓
main.py process_decoded()
    ├── database.py → SQLite (upsert vessel + insert position)
    └── server.py → WebSocket broadcast to all connected browsers
                        ↓
                   app.js (update Leaflet markers in real-time)
```

Environmental data flows independently:
- `/api/current-field` → SFBOFS NetCDF grid (or station IDW fallback) → `tidal-flow.js` particles
- `/api/wind-field` → NOAA HRRR grid + NDBC buoys → `wind-overlay.js` particles
- `/api/tide-height` → NOAA CO-OPS harmonic predictions → timeline display
- `/api/currents` → 6 tidal current stations → interpolated overlay

## File Map

### Backend (project root)

| File | Purpose |
|------|---------|
| `main.py` | Entry point. Async orchestration: selects AIS source, runs decoder, DB init, starts server, background environmental data refresh |
| `server.py` | FastAPI routes + WebSocket broadcast. All API endpoints defined here |
| `config.py` | Env var config (`AIS_HOST`, `AIS_PORT`, `AISSTREAM_API_KEY`, `OWN_MMSI`, `DB_PATH`, etc.) |
| `database.py` | SQLite async ORM: `upsert_vessel`, `insert_position`, `get_vessel_track`, `get_stats` |
| `ais_decoder.py` | NMEA sentence decoding (pyais), ship type categorization, multi-part message buffering |
| `ais_listener.py` | TCP/UDP local AIS receiver (192.168.47.10:10110), auto-detect protocol, reconnect logic |
| `aisstream_listener.py` | WebSocket client for AISstream.io cloud API, bounding box subscription |
| `currents.py` | NOAA CO-OPS tidal current API, 6 stations, inverse distance weighting interpolation |
| `tides.py` | NOAA tide height predictions, 14 stations, harmonic interpolation |
| `sfbofs.py` | NOAA SF Bay hydrodynamic model: S3 NetCDF download, FVCOM unstructured→regular grid regrid |
| `wind.py` | NOAA HRRR wind grid via Open-Meteo (72 points, parallel fetch via ThreadPoolExecutor) + NDBC buoy observations, dual color schemes |
| `download_offline.py` | Pre-cache tiles, currents, wind data for offline use |
| `README.md` | User-facing documentation: features, setup, architecture, deployment |
| `CLAUDE.md` | AI assistant context: tech stack, file map, API endpoints, patterns |

### Frontend (`static/`)

| File | Purpose |
|------|---------|
| `index.html` | Single page: map container, side panel, legends, forecast timeline, modals |
| `js/app.js` (~1900 lines) | Main app: Leaflet map, vessel markers, popups, CPA/TCPA, speed charts, search, forecast UI, mobile quick buttons, offline pre-fetch |
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

## API Endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /` | Serve index.html |
| `GET /api/vessels` | All vessels with latest position |
| `GET /api/vessels/{mmsi}` | Single vessel detail |
| `GET /api/vessels/{mmsi}/track?hours=2` | Position history track |
| `GET /api/stats` | Vessel/position counts, own vessel info |
| `GET /api/currents?time=0` | Tidal current at 6 stations (time=minutes offset) |
| `GET /api/current-field?time=0` | SFBOFS gridded current field (276x325) |
| `GET /api/wind-field?time=0` | HRRR wind grid + NDBC stations |
| `GET /api/tide-height?time=0` | Tide predictions at 14 stations |
| `WS /ws` | Real-time vessel position updates |

The `time` parameter is minutes offset from now (used by forecast timeline).

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

```bash
pip install -r requirements.txt
python main.py --demo              # Demo mode (simulated vessels)
python main.py --local             # Local AIS hardware
python main.py --aisstream         # Cloud AIS (needs API key)
python main.py --verbose           # Verbose vessel logging (every position update)
python main.py                     # Auto-detect (local → cloud → demo)
```

Local server runs at `http://localhost:8888`. Deploys to Fly.io via `fly deploy` (see `fly.toml`).

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
