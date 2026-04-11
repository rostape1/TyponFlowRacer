# AIS Tracker

A lightweight AIS vessel tracker for sailboats. Connects to a WiFi AIS receiver, decodes NMEA messages, stores vessel data in SQLite, and displays everything on an interactive nautical map.

## Features

- Real-time vessel tracking via AIS with WebSocket updates
- Interactive dark-themed nautical map (Leaflet) with multiple tile layers (CartoDB, OSM, NOAA charts, OpenSeaMap)
- **Per-vessel visibility toggles** — show/hide individual vessels on the map from the side panel
- CPA/TCPA collision risk warnings with color-coded alerts
- Speed history charts (2-hour track) with acceleration/deceleration coloring
- Tidal current overlay with animated particle flow visualization (6 NOAA stations in SF Bay)
- Offline-capable with locally cached map tiles
- Demo mode with simulated vessel movements in SF Bay

## Quick Start

```bash
pip install -r requirements.txt

# Demo mode (simulated vessels in SF Bay)
python main.py --demo

# Live mode (connects to AIS receiver)
python main.py
```

Then open **http://localhost:8888**

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
AIS Receiver (WiFi TCP) → ais_listener.py → ais_decoder.py (pyais)
    → database.py (SQLite) + server.py (FastAPI WebSocket) → Browser (Leaflet map)
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
