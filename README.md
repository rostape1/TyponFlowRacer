# AIS Tracker

A lightweight AIS vessel tracker for sailboats. Connects to a WiFi AIS receiver, decodes NMEA messages, stores vessel data in SQLite, and displays everything on an interactive nautical map. Also works without hardware via AISstream.io cloud AIS data.

## Features

- Real-time vessel tracking via AIS with WebSocket updates
- **Multiple AIS data sources** — local receiver, AISstream.io cloud, or demo mode (auto-detects)
- Interactive dark-themed nautical map (Leaflet) with multiple tile layers (CartoDB, OSM, NOAA charts, OpenSeaMap)
- **Auto-detect online/offline** — uses CDN tiles when online, falls back to local cache when offline
- **Per-vessel visibility toggles** — show/hide individual vessels on the map from the side panel
- **50 closest vessels** cap — keeps the map clean when using cloud AIS data
- CPA/TCPA collision risk warnings with color-coded alerts
- Speed history charts (2-hour track) with acceleration/deceleration coloring
- **High-resolution tidal current visualization** powered by NOAA SFBOFS hydrodynamic model (see below)
- **Wind overlay** with animated particles from HRRR model
- **PWA support** — installable on iPhone/iPad via "Add to Home Screen" for full-screen native feel
- Offline-capable with locally cached map tiles and current data
- Demo mode with simulated vessel movements in SF Bay

## Quick Start

```bash
pip install -r requirements.txt

# Auto mode (tries local AIS receiver, falls back to AISstream.io)
python main.py

# Demo mode (simulated vessels in SF Bay)
python main.py --demo

# Force local AIS receiver only
python main.py --local

# Force cloud AIS only (aisstream.io)
python main.py --aisstream

# Verbose vessel logging (shows every position update)
python main.py --verbose
```

Then open **http://localhost:8888**

### AISstream.io Setup

For cloud AIS data (no hardware needed), get a free API key at [aisstream.io](https://aisstream.io) and add it to `.env`:

```
AISSTREAM_API_KEY=your_key_here
```

The app loads `.env` automatically. In auto mode (no flags), it tries the local AIS receiver first and falls back to AISstream.io if the key is set.

## Tidal Current Visualization

The app shows animated tidal current flow using particle trails on the map, similar to Windy.com. Color indicates current speed: **blue** (slack) → **cyan** → **green** → **yellow** → **orange** → **red** (strong). A legend in the bottom-left corner shows the scale and data source.

### Data Sources (layered, highest priority first)

1. **NOAA SFBOFS** (SF Bay Operational Forecast System) — high-resolution gridded velocity field from a hydrodynamic model running on an unstructured mesh (~100-200m resolution, 102k+ grid cells). Shows realistic eddies, channel acceleration, and coastal effects. Data is downloaded from NOAA's public S3 bucket (`noaa-nos-ofs-pds`), ~57MB per forecast file, cached for 6 hours.

2. **NOAA Tidal Current Stations** — 6 point stations in SF Bay (Golden Gate, Alcatraz, Angel Island, Raccoon Strait, Bay Bridge). Uses inverse distance weighting to interpolate between stations. Used as fallback when SFBOFS is unavailable.

### How it works

- **Backend** (`sfbofs.py`): Downloads the latest SFBOFS NetCDF forecast from S3 → extracts surface u/v velocity from the FVCOM triangular grid → regrids to a regular 276x325 lat/lon grid using scipy → serves as JSON via `/api/current-field`
- **Backend** (`currents.py`): Fetches tidal predictions from NOAA CO-OPS API for 6 stations → interpolates to current time → serves via `/api/currents`
- **Frontend** (`tidal-flow.js`): Canvas-based particle animation. 3000 particles advected by the velocity field each frame. Bilinear grid interpolation when SFBOFS data is available, falls back to IDW station interpolation. Zoom-compensated speed so visual flow rate stays constant across zoom levels.

### Dependencies for tidal flow

- `netCDF4` — reads NOAA's NetCDF forecast files (C extension; on Pi: `apt install python3-netcdf4`)
- `numpy` — array operations
- `scipy` — regridding from unstructured mesh to regular grid

## Configuration

Edit `config.py` or set environment variables:

| Setting | Default | Env Var |
|---------|---------|---------|
| AIS Receiver IP | 192.168.47.10 | `AIS_HOST` |
| AIS Receiver Port | 10110 | `AIS_PORT` |
| AISstream API Key | (none) | `AISSTREAM_API_KEY` |
| Own MMSI | 338361814 | `OWN_MMSI` |
| Server Host | 127.0.0.1 | `SERVER_HOST` |
| Server Port | 8888 | `SERVER_PORT` |
| Database Path | ~/.ais_tracker/ais_tracker.db | `DB_PATH` |

## Database Location

The SQLite database is stored at **`~/.ais_tracker/ais_tracker.db`**, NOT in the project folder. This is because the project lives on iCloud Drive, and SQLite's file locking does not work on cloud-synced filesystems.

All position data is kept permanently for post-voyage analysis. To check database size:
```bash
ls -lh ~/ais_tracker.db
```

## Architecture

```
AIS Data Sources (auto-detect priority):
  1. Local AIS Receiver (WiFi TCP/UDP) → ais_listener.py → ais_decoder.py (pyais)
  2. AISstream.io (WebSocket) → aisstream_listener.py (JSON decode)
  3. Demo mode → main.py (simulated vessels)
    → database.py (SQLite) + server.py (FastAPI WebSocket) → Browser (Leaflet map)

NOAA S3 (SFBOFS NetCDF) → sfbofs.py (regrid) → /api/current-field → tidal-flow.js (particles)
NOAA CO-OPS API → currents.py → /api/currents → tidal-flow.js (fallback) + current arrows
Open-Meteo (HRRR) → wind.py → /api/wind-field → wind-overlay.js (particles)
```

## Deployment on Raspberry Pi

1. Install Python 3.11+ and pip
2. Clone this project
3. `pip install -r requirements.txt`
4. `python main.py` (connects to AIS receiver on boat WiFi)
5. Access from any device on the boat network at `http://<pi-ip>:8888`

For auto-start on boot, create a systemd service:
```bash
sudo tee /etc/systemd/system/ais-tracker.service << 'EOF'
[Unit]
Description=AIS Tracker
After=network.target

[Service]
ExecStart=/usr/bin/python3 /home/pi/AIS-Tracker/main.py
WorkingDirectory=/home/pi/AIS-Tracker
Restart=always
User=pi

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable ais-tracker
sudo systemctl start ais-tracker
```

## Cloud Deployment (Fly.io)

The app is configured for deployment on **Fly.io** with the included `Dockerfile` and `fly.toml`. It auto-detects that no local AIS receiver is available and falls back to AISstream.io.

```bash
# Install Fly CLI
brew install flyctl

# Login and create the app
fly auth login
fly launch

# Set your AIS API key as a secret (not stored in the image)
fly secrets set AISSTREAM_API_KEY=your_key_here

# Deploy
fly deploy

# Scale to 1 machine (free tier)
fly scale count 1
```

The app will be live at `https://your-app-name.fly.dev`. The `fly.toml` sets San Jose region (`sjc`) for low latency to SF Bay. Free tier includes auto-stop on idle and auto-start on request (~2-3s cold start).

Other hosting options: **Railway**, **Render**, or **Oracle Cloud** (most generous free tier).

## iOS App

The frontend can be wrapped as a native iOS app using **Capacitor** (Ionic's native bridge). The existing HTML/JS/CSS runs inside a WebView — no rewrite needed. For the app version, AIS data comes from **AISstream.io** instead of a local receiver, subscribing by bounding box based on the user's map view. No backend required for core functionality.

Stack: Capacitor + existing frontend + AISstream.io WebSocket API + GPS for location. Offline support via cached map tiles and last-known vessel positions in IndexedDB. A lightweight backend would only be needed for user accounts, saved regions, or push notifications.
