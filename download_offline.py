#!/usr/bin/env python3
"""
Download all assets needed for offline operation:
1. Leaflet JS/CSS library
2. Map tiles for SF Bay area (zoom levels 10-15)
3. OpenSeaMap tiles for the same area

Run this script while connected to the internet.
Tiles are stored in static/tiles/ and served by the app.

Usage:
    python download_offline.py
    python download_offline.py --bounds 37.7,-122.6,37.9,-122.3 --zoom 10-16
"""

import argparse
import json
import math
import os
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(SCRIPT_DIR, "static")

# SF Bay default bounds
DEFAULT_BOUNDS = (37.65, -122.60, 37.95, -122.30)
DEFAULT_ZOOM_RANGE = (10, 15)

# Tile sources
TILE_SOURCES = {
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "dir": "tiles/osm",
    },
    "dark": {
        "url": "https://a.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}.png",
        "dir": "tiles/dark",
    },
    "sea": {
        "url": "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png",
        "dir": "tiles/sea",
    },
    "noaa": {
        "url": "https://tileservice.charts.noaa.gov/tiles/50000_1/{z}/{x}/{y}.png",
        "dir": "tiles/noaa",
    },
}

LEAFLET_FILES = {
    "lib/leaflet.js": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js",
    "lib/leaflet.css": "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css",
    "lib/images/marker-icon.png": "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png",
    "lib/images/marker-icon-2x.png": "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png",
    "lib/images/marker-shadow.png": "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png",
    "lib/images/layers.png": "https://unpkg.com/leaflet@1.9.4/dist/images/layers.png",
    "lib/images/layers-2x.png": "https://unpkg.com/leaflet@1.9.4/dist/images/layers-2x.png",
}


def lat_lon_to_tile(lat, lon, zoom):
    """Convert lat/lon to tile x,y at given zoom level."""
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def download_file(url, dest):
    """Download a single file."""
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return False  # Already exists

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AIS-Tracker-Offline/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            with open(dest, "wb") as f:
                f.write(resp.read())
        return True
    except Exception as e:
        # Some sea tiles return 404 for open ocean — that's fine
        return False


def download_leaflet():
    """Download Leaflet library files."""
    print("Downloading Leaflet library...")
    for dest_rel, url in LEAFLET_FILES.items():
        dest = os.path.join(STATIC_DIR, dest_rel)
        if download_file(url, dest):
            print(f"  ✓ {dest_rel}")
        else:
            print(f"  - {dest_rel} (already exists)")


def download_tiles(bounds, zoom_min, zoom_max):
    """Download map tiles for the given bounds and zoom range."""
    lat_min, lon_min, lat_max, lon_max = bounds

    total_tiles = estimate_tiles(bounds, zoom_min, zoom_max)
    downloaded = 0
    skipped = 0
    processed = 0

    for source_name, source in TILE_SOURCES.items():
        print(f"\nDownloading {source_name} tiles (zoom {zoom_min}-{zoom_max})...")

        for zoom in range(zoom_min, zoom_max + 1):
            x_min, y_max = lat_lon_to_tile(lat_min, lon_min, zoom)
            x_max, y_min = lat_lon_to_tile(lat_max, lon_max, zoom)

            tile_count = (x_max - x_min + 1) * (y_max - y_min + 1)

            for x in range(x_min, x_max + 1):
                for y in range(y_min, y_max + 1):
                    processed += 1
                    url = source["url"].format(z=zoom, x=x, y=y)
                    dest = os.path.join(STATIC_DIR, source["dir"], str(zoom), str(x), f"{y}.png")

                    if os.path.exists(dest) and os.path.getsize(dest) > 0:
                        skipped += 1
                    elif download_file(url, dest):
                        downloaded += 1

                    # Progress indicator
                    pct = processed * 100 // total_tiles
                    print(f"\r  [{pct:3d}%] {processed}/{total_tiles} tiles  ({downloaded} new, {skipped} cached)", end="", flush=True)

                    # Rate limiting — skip delay for cached tiles
                    if not (os.path.exists(dest) and os.path.getsize(dest) > 0):
                        time.sleep(0.05)

            print(f"\n  zoom {zoom}: {tile_count} tiles ({x_max - x_min + 1}x{y_max - y_min + 1})")

    print(f"\nDone! {downloaded} new + {skipped} cached = {processed} total tiles")


def estimate_tiles(bounds, zoom_min, zoom_max):
    """Estimate how many tiles will be downloaded."""
    lat_min, lon_min, lat_max, lon_max = bounds
    total = 0
    for zoom in range(zoom_min, zoom_max + 1):
        x_min, y_max = lat_lon_to_tile(lat_min, lon_min, zoom)
        x_max, y_min = lat_lon_to_tile(lat_max, lon_max, zoom)
        total += (x_max - x_min + 1) * (y_max - y_min + 1)
    return total * len(TILE_SOURCES)


def main():
    parser = argparse.ArgumentParser(description="Download offline assets for AIS Tracker")
    parser.add_argument("--bounds", type=str, default=None,
                        help="Lat/lon bounds: lat_min,lon_min,lat_max,lon_max (default: SF Bay)")
    parser.add_argument("--zoom", type=str, default=None,
                        help="Zoom range: min-max (default: 10-15)")
    args = parser.parse_args()

    if args.bounds:
        bounds = tuple(float(x) for x in args.bounds.split(","))
    else:
        bounds = DEFAULT_BOUNDS

    if args.zoom:
        zoom_min, zoom_max = (int(x) for x in args.zoom.split("-"))
    else:
        zoom_min, zoom_max = DEFAULT_ZOOM_RANGE

    total_est = estimate_tiles(bounds, zoom_min, zoom_max)
    print(f"AIS Tracker Offline Downloader")
    print(f"Bounds: {bounds}")
    print(f"Zoom: {zoom_min}-{zoom_max}")
    print(f"Estimated tiles: {total_est} (across {len(TILE_SOURCES)} tile sources)")
    print(f"Estimated size: ~{total_est * 15 // 1024} MB\n")

    download_leaflet()
    download_tiles(bounds, zoom_min, zoom_max)
    download_currents()
    download_wind()

    print(f"\nOffline assets saved to: {STATIC_DIR}")
    print("The app will automatically use local tiles when available.")


# --- Current predictions offline cache ---
CURRENT_STATIONS = {
    "SFB1201": "Golden Gate Bridge",
    "SFB1203": "Alcatraz (North)",
    "SFB1204": "Alcatraz (South)",
    "SFB1205": "Angel Island (East)",
    "SFB1206": "Raccoon Strait",
    "SFB1211": "Bay Bridge",
}

NOAA_CURRENTS_URL = (
    "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
    "?date=today&station={station}&product=currents_predictions"
    "&units=english&time_zone=gmt&format=json&interval=6"
)


def download_currents():
    """Download tidal current predictions for SF Bay stations."""
    print("\nDownloading tidal current predictions...")
    currents_dir = os.path.join(STATIC_DIR, "data", "currents")
    os.makedirs(currents_dir, exist_ok=True)

    for station_id, name in CURRENT_STATIONS.items():
        url = NOAA_CURRENTS_URL.format(station=station_id)
        dest = os.path.join(currents_dir, f"{station_id}.json")

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AIS-Tracker-Offline/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))

            # Extract predictions array
            predictions = None
            if "current_predictions" in data:
                predictions = data["current_predictions"].get("cp", [])
            elif "predictions" in data:
                predictions = data["predictions"]

            if predictions:
                with open(dest, "w") as f:
                    json.dump(predictions, f, indent=2)
                print(f"  ✓ {name} ({station_id}): {len(predictions)} predictions")
            else:
                print(f"  ✗ {name} ({station_id}): no data returned")

        except Exception as e:
            print(f"  ✗ {name} ({station_id}): {e}")


if __name__ == "__main__":
    main()


# --- Wind data offline cache ---

WIND_GRID_NX = 7
WIND_GRID_NY = 8
WIND_BOUNDS = (37.40, -122.65, 38.05, -122.10)  # south, west, north, east

OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
    "&models=gfs_seamless"
    "&wind_speed_unit=kn"
)

NDBC_STATIONS_OFFLINE = {
    "FTPC1": {"name": "Fort Point", "lat": 37.8060, "lon": -122.4659},
    "RCMC1": {"name": "Richmond", "lat": 37.9228, "lon": -122.4098},
    "AAMC1": {"name": "Alameda", "lat": 37.7717, "lon": -122.2992},
    "OKXC1": {"name": "Oakland", "lat": 37.8067, "lon": -122.3340},
    "PPXC1": {"name": "Point Potrero", "lat": 37.9078, "lon": -122.3728},
}

NDBC_REALTIME_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station}.txt"


def download_wind():
    """Download wind grid from Open-Meteo and NDBC station data for offline use."""
    import math

    data_dir = os.path.join(STATIC_DIR, "data")
    os.makedirs(data_dir, exist_ok=True)

    # --- Grid from Open-Meteo ---
    print("\nDownloading wind grid from Open-Meteo (HRRR)...")
    south, west, north, east = WIND_BOUNDS
    lats = [south + i * (north - south) / (WIND_GRID_NY - 1) for i in range(WIND_GRID_NY)]
    lons = [west + i * (east - west) / (WIND_GRID_NX - 1) for i in range(WIND_GRID_NX)]

    u_grid = [[0.0] * WIND_GRID_NX for _ in range(WIND_GRID_NY)]
    v_grid = [[0.0] * WIND_GRID_NX for _ in range(WIND_GRID_NY)]
    gust_grid = [[0.0] * WIND_GRID_NX for _ in range(WIND_GRID_NY)]
    success = 0

    for iy, lat in enumerate(lats):
        for ix, lon in enumerate(lons):
            url = OPEN_METEO_URL.format(lat=lat, lon=lon)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "AIS-Tracker-Offline/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                current = data.get("current", {})
                speed_kn = current.get("wind_speed_10m", 0) or 0
                direction = current.get("wind_direction_10m", 0) or 0
                gust_kn = current.get("wind_gusts_10m", 0) or 0

                dir_rad = direction * math.pi / 180
                u_grid[iy][ix] = round(-speed_kn * math.sin(dir_rad), 2)
                v_grid[iy][ix] = round(-speed_kn * math.cos(dir_rad), 2)
                gust_grid[iy][ix] = round(gust_kn, 1)
                success += 1
            except Exception:
                pass

    if success > 0:
        grid_data = {
            "bounds": {"south": south, "north": north, "west": west, "east": east},
            "nx": WIND_GRID_NX, "ny": WIND_GRID_NY,
            "model": "HRRR 3km",
            "source": "Open-Meteo (NOAA HRRR)",
            "fetched_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
            "u": u_grid, "v": v_grid, "gusts": gust_grid,
        }
        dest = os.path.join(data_dir, "wind_field.json")
        with open(dest, "w") as f:
            json.dump(grid_data, f)
        print(f"  ✓ Wind grid: {success}/{WIND_GRID_NX * WIND_GRID_NY} points")
    else:
        print("  ✗ Wind grid: no data retrieved")

    # --- NDBC stations ---
    print("Downloading NDBC station observations...")
    stations = []
    for station_id, info in NDBC_STATIONS_OFFLINE.items():
        url = NDBC_REALTIME_URL.format(station=station_id)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "AIS-Tracker-Offline/1.0"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                text = resp.read().decode("utf-8")
            lines = text.strip().split("\n")
            if len(lines) < 3:
                continue
            header = lines[0].replace("#", "").split()
            data_line = lines[2].split()
            col = {h: i for i, h in enumerate(header)}
            wspd = data_line[col["WSPD"]]
            wdir = data_line[col["WDIR"]]
            gst = data_line[col.get("GST", -1)] if "GST" in col else "MM"
            if wspd == "MM" or wdir == "MM":
                continue
            stations.append({
                "id": station_id, "name": info["name"],
                "lat": info["lat"], "lon": info["lon"],
                "speed_kn": round(float(wspd) * 1.94384, 1),
                "gust_kn": round(float(gst) * 1.94384, 1) if gst != "MM" else None,
                "direction": float(wdir),
                "timestamp": time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime()),
            })
            print(f"  ✓ {info['name']} ({station_id})")
        except Exception as e:
            print(f"  ✗ {info['name']} ({station_id}): {e}")

    if stations:
        dest = os.path.join(data_dir, "wind_stations.json")
        with open(dest, "w") as f:
            json.dump(stations, f, indent=2)
        print(f"  {len(stations)} stations saved")
