# AIS Tracker

A lightweight AIS vessel tracker for sailboats. Connects to a WiFi AIS receiver, decodes NMEA messages, stores vessel data in SQLite, and displays everything on an interactive nautical map.

## Features

- Real-time vessel tracking via AIS with WebSocket updates
- Interactive dark-themed nautical map (Leaflet) with multiple tile layers (CartoDB, OSM, NOAA charts, OpenSeaMap)
- **Per-vessel visibility toggles** — show/hide individual vessels on the map from the side panel
- CPA/TCPA collision risk warnings with color-coded alerts
- Speed history charts (2-hour track) with acceleration/deceleration coloring
- **High-resolution tidal current visualization** powered by NOAA SFBOFS hydrodynamic model (see below)
- Offline-capable with locally cached map tiles and current data
- Demo mode with simulated vessel movements in SF Bay

## Quick Start

```bash
pip install -r requirements.txt

# Demo mode (simulated vessels in SF Bay)
python main.py --demo

# Live mode (connects to AIS receiver)
python main.py

# Verbose vessel logging (shows every position update)
python main.py --demo --verbose
```

Then open **http://localhost:8888**

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
| Own MMSI | 338361814 | `OWN_MMSI` |
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
AIS Receiver (WiFi TCP/UDP) → ais_listener.py → ais_decoder.py (pyais)
    → database.py (SQLite) + server.py (FastAPI WebSocket) → Browser (Leaflet map)

NOAA S3 (SFBOFS NetCDF) → sfbofs.py (regrid) → /api/current-field → tidal-flow.js (particles)
NOAA CO-OPS API → currents.py → /api/currents → tidal-flow.js (fallback) + current arrows
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
