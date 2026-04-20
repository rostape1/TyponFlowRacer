# AIS Tracker

Real-time maritime vessel tracking platform for San Francisco Bay. Combines AIS ship data with NOAA tidal currents, wind forecasts, and tide predictions on an interactive Leaflet map with particle animations.

Deployed as a static PWA on GitHub Pages — no backend required.

## Features

- **Real-time AIS vessel tracking** — direct browser WebSocket to AISstream.io (API key embedded, no setup needed)
- **Tidal flow animation** — NOAA SFBOFS hydrodynamic model (276×325 grid), animated particles + speed heatmap toggled together via "Tide Flow" button
- **Wind overlay** — 72-point Open-Meteo grid, animated arrow-tipped particles with flashing speed numbers
- **Tide height stations** — 14 NOAA CO-OPS stations across SF Bay, toggleable markers
- **Current stations** — 6 NOAA CO-OPS stations with live arrows
- **48-hour forecast timeline** — scrollable timeline on desktop; NOW/+1h/+2h/+3h/+4h quick buttons on mobile; calendar picker for unlimited range (tides only beyond 48h)
- **Auto-download on load** — silently pre-fetches all data 8s after page open, retries with exponential backoff if offline; per-category badges (Flow/Wind/Tide/Curr) turn green as each finishes. Flow badge shows `Flow +Xh` — hours of forward forecast remaining from now.
- **PWA** — installable on iPhone via "Add to Home Screen", service worker caches tiles + API responses for offline use
- **CPA/TCPA collision warnings** — color-coded alerts, speed history charts
- **Data freshness indicators** — green/yellow dot with relative age on current + wind legends

## Live App

**[rostape1.github.io/TyponFlowRacer](https://rostape1.github.io/TyponFlowRacer)**

No setup needed — opens and connects automatically.

## Architecture

```
GitHub Actions (scheduled)          GitHub Pages (static hosting)
├── SFBOFS: 4x/day (NetCDF→JSON)   ├── index.html + JS/CSS
└── NDBC buoys: every 10min        ├── data/sfbofs/hour_00..48.json
                                   └── data/wind/stations.json

Browser (PWA) — direct API fetches
├── AISstream.io WebSocket → vessel tracking (API key embedded)
├── NOAA CO-OPS API → tides (14 stations) + currents (6 stations)
├── Open-Meteo API → wind grid (9×8 = 72 points, 1 batched request)
├── Static JSON → SFBOFS current field + NDBC buoy observations
└── Service Worker → offline caching for tiles + API responses
```

## Mobile UI

Three-row collapsible bottom bar:
1. **Layers tray** — Download (full-width), then: Tide Flow · Wind · Tide · Vessels
2. **Forecast buttons** — NOW · +1h · +2h · +3h · +4h · Set FCST TIME
3. **Status bar** — `● AIS · 0 vessels · [Flow][Wind][Tide][Curr] · DL: Xm ago · ☰`

## Data Sources

| Source | Data | Update frequency |
|--------|------|-----------------|
| AISstream.io | Vessel AIS | Continuous WebSocket |
| NOAA SFBOFS | Current field (276×325 grid) | 4×/day (03z/09z/15z/21z) |
| NOAA CO-OPS | Tides (14 stations), Currents (6 stations) | Direct browser fetch, 6h cache |
| Open-Meteo | Wind grid (72 points, 49h forecast) | Direct browser fetch, 30min cache |
| NDBC | Buoy observations (9 stations) | Every 10 min via GitHub Actions |

## SFBOFS Tidal Flow Refresh Pipeline

NOAA publishes the SF Bay hydrodynamic model 4× per day (03z / 09z / 15z / 21z UTC). Files appear on S3 progressively as the model computes each forecast hour — not all at once.

**GitHub Actions cron: every hour at :20**

1. `_find_latest_run()` scans NOAA S3 newest-first. A run only qualifies if **both** `hour_00` and `hour_12` exist — ensures at least 12 forecast hours are computed before we commit to it.
2. Runs that started less than 1 hour ago are skipped (model is still initialising).
3. **New run:** clears old hour files, downloads all 49 hours in parallel (4 workers), saves `sfbofs_run` in `meta.json` as soon as any hours succeed.
4. **Same run, hours missing:** downloads only the missing `hour_XX.json` files — incremental fill until all 48h are present.
5. **Same run, complete (≥48h):** skips entirely, no download or deploy.
6. Triggers GitHub Pages deploy after each fetch.

**NDBC cron: every 10 minutes**

Restores the latest combined cache (including SFBOFS files) before updating `wind/stations.json`, so NDBC deploys never wipe tidal flow data.

**Typical data freshness**

| Scenario | Age when you see it |
|----------|-------------------|
| Best case | ~1h after NOAA run time |
| Typical (full 48h) | ~2h after NOAA run time |
| Worst case | ~7h (6h between runs + 1h buffer) |

**Flow badge in the app**

The `Flow +Xh` badge shows hours of **forward forecast remaining from now**, computed from the model run time stored in `hour_00.json`. It recomputes on every page reload so it decreases as time passes. Grey = nothing downloaded. Yellow pulse = downloading. Green `Flow +Xh` = X hours of future tidal forecast available.

## Local Development

```bash
# Serve the static site (APIs fetch automatically from browser)
python -m http.server 8888 --directory static
```

Open **http://localhost:8888**. Tides, currents, and wind fetch live from public APIs. SFBOFS data uses whatever is currently deployed to GitHub Pages.

### Generate SFBOFS data locally (optional)

```bash
pip install -r .github/scripts/requirements.txt
python .github/scripts/fetch_sfbofs.py
```

Requires `netCDF4`, `scipy`, `numpy`. Downloads ~57MB NetCDF from NOAA S3 and outputs `data/sfbofs/hour_00..48.json`.

## Legacy Backend

The original Python/FastAPI/SQLite backend (`main.py`, `server.py`, etc.) is kept in the project root for reference and local development. It supported local AIS receivers, Raspberry Pi deployment, and Fly.io cloud hosting. The current production app is fully static.

```bash
pip install -r requirements.txt
python main.py --demo      # Demo mode (simulated vessels)
python main.py --aisstream # Cloud AIS (needs AISSTREAM_API_KEY in .env)
```
