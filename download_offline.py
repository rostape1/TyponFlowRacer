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
import concurrent.futures
import json
import math
import os
import threading
import time
import urllib.request

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(SCRIPT_DIR, "static")

# Default bounds: SF Bay through Monterey (the Typon's typical cruising range).
# Bounds order: (lat_min, lon_min, lat_max, lon_max)
DEFAULT_BOUNDS = (36.45, -123.05, 38.20, -121.75)
DEFAULT_ZOOM_RANGE = (10, 15)

# Tile sources. Each entry is `{z}/{x}/{y}` format-string based unless it names a
# different `scheme`; NOAA charts use ArcGIS REST (`{lod}/{y}/{x}`, lod = z - 2).
# Saved locally as `tiles/<dir>/{z}/{x}/{y}.png` to match what static/js/app.js
# requests when _useLocalTiles is true.
TILE_SOURCES = {
    "osm": {
        "url": "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
        "dir": "tiles/osm",
        # OSM's tile usage policy forbids bulk hammering; slow this origin down
        # more than the rest now that the default bbox is much larger.
        "delay": 0.25,
    },
    "dark": {
        # Esri World Dark Gray Canvas (keyless). CartoDB's dark_all now watermarks
        # unregistered traffic with "API KEY REQUIRED". Note {y} before {x}.
        "url": "https://services.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}",
        "dir": "tiles/dark",
    },
    "sea": {
        "url": "https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png",
        "dir": "tiles/sea",
    },
    "noaa": {
        "url": (
            "https://gis.charttools.noaa.gov/arcgis/rest/services/"
            "MarineChart_Services/NOAACharts/MapServer/tile/{lod}/{y}/{x}"
        ),
        "scheme": "arcgis_lod",
        "dir": "tiles/noaa",
        "min_zoom": 2,   # lod = zoom - 2 must be >= 0
    },
}

# Concurrency. Browsers do 6 parallel connections per origin; 8 workers across
# 4 tile origins is well-behaved (~2 connections per origin amortized).
DOWNLOAD_WORKERS = 8
# Per-request pause after each download, overridable per source via "delay".
TILE_DEFAULT_DELAY_S = 0.05

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

    # Write to a .part sibling and rename on success. Writing straight to `dest`
    # left a truncated PNG behind on Ctrl-C or a dropped connection, which the
    # getsize(dest) > 0 skip test above then treated as complete forever.
    part = dest + ".part"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "AIS-Tracker-Offline/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = resp.read()
        if not data:
            return False
        with open(part, "wb") as f:
            f.write(data)
        os.replace(part, dest)
        return True
    except Exception:
        # Some sea tiles return 404 for open ocean — that's fine
        try:
            os.remove(part)
        except OSError:
            pass
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


def _build_tile_url(source, zoom, x, y):
    """Construct the upstream URL for one tile.

    Sources declaring `"scheme": "arcgis_lod"` use ArcGIS REST ordering
    (`{lod}/{y}/{x}`, lod = z - 2) rather than the plain `{z}/{x}/{y}` format
    string. Returns None to skip a tile the source can't serve (NOAA below its
    `min_zoom`).
    """
    if source.get("scheme") == "arcgis_lod":
        if zoom < source.get("min_zoom", 0):
            return None
        return source["url"].format(lod=zoom - 2, x=x, y=y)
    return source["url"].format(z=zoom, x=x, y=y)


def download_tiles(bounds, zoom_min, zoom_max):
    """Download map tiles for the given bounds and zoom range, in parallel."""
    lat_min, lon_min, lat_max, lon_max = bounds

    # Build the full work list first so we can show accurate progress. Tiles a
    # source can't serve are dropped here, so `total` matches estimate_tiles().
    jobs = []  # (url, dest, delay)
    for source in TILE_SOURCES.values():
        delay = source.get("delay", TILE_DEFAULT_DELAY_S)
        for zoom in range(zoom_min, zoom_max + 1):
            x_min, y_max = lat_lon_to_tile(lat_min, lon_min, zoom)
            x_max, y_min = lat_lon_to_tile(lat_max, lon_max, zoom)
            for x in range(x_min, x_max + 1):
                for y in range(y_min, y_max + 1):
                    url = _build_tile_url(source, zoom, x, y)
                    if url is None:
                        continue
                    dest = os.path.join(STATIC_DIR, source["dir"], str(zoom), str(x), f"{y}.png")
                    jobs.append((url, dest, delay))

    total = len(jobs)
    counter = {"processed": 0, "downloaded": 0, "skipped": 0, "errors": 0}
    lock = threading.Lock()
    start_ts = time.time()

    def _worker(job):
        url, dest, delay = job
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            with lock:
                counter["skipped"] += 1
                counter["processed"] += 1
            return
        ok = download_file(url, dest)
        if delay:
            time.sleep(delay)
        with lock:
            if ok:
                counter["downloaded"] += 1
            else:
                counter["errors"] += 1
            counter["processed"] += 1

    print(f"Downloading {total} tiles with {DOWNLOAD_WORKERS} parallel workers...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
        futures = [ex.submit(_worker, j) for j in jobs]
        last_print = 0.0
        for _ in concurrent.futures.as_completed(futures):
            now = time.time()
            if now - last_print > 0.5 or counter["processed"] == total:
                with lock:
                    p = counter["processed"]
                    d = counter["downloaded"]
                    s = counter["skipped"]
                    e = counter["errors"]
                pct = p * 100 // total if total else 100
                elapsed = now - start_ts
                rate = d / elapsed if elapsed > 0 and d > 0 else 0
                eta = (total - p) / rate if rate > 0 else 0
                print(
                    f"\r  [{pct:3d}%] {p}/{total}  "
                    f"({d} new, {s} cached, {e} err)  "
                    f"{rate:.1f}/s  ETA {eta:.0f}s",
                    end="", flush=True,
                )
                last_print = now

    elapsed = time.time() - start_ts
    print(
        f"\nDone in {elapsed:.0f}s: "
        f"{counter['downloaded']} new, {counter['skipped']} cached, "
        f"{counter['errors']} errors, {counter['processed']} processed"
    )


def estimate_tiles(bounds, zoom_min, zoom_max):
    """Estimate how many tiles will be downloaded.

    Counted per source rather than tiles × sources: NOAA serves nothing below
    its `min_zoom`, so a flat multiply disagreed with download_tiles()' own total.
    """
    lat_min, lon_min, lat_max, lon_max = bounds
    per_zoom = {}
    for zoom in range(zoom_min, zoom_max + 1):
        x_min, y_max = lat_lon_to_tile(lat_min, lon_min, zoom)
        x_max, y_min = lat_lon_to_tile(lat_max, lon_max, zoom)
        per_zoom[zoom] = (x_max - x_min + 1) * (y_max - y_min + 1)
    total = 0
    for source in TILE_SOURCES.values():
        min_zoom = source.get("min_zoom", 0)
        total += sum(n for z, n in per_zoom.items() if z >= min_zoom)
    return total


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
    per_layer = total_est // len(TILE_SOURCES)
    print(f"AIS Tracker Offline Downloader")
    print(f"Bounds: {bounds}")
    print(f"Zoom: {zoom_min}-{zoom_max}")
    print(f"Estimated tiles: {total_est} total (~{per_layer} per layer × {len(TILE_SOURCES)} sources)")
    print(f"Estimated size: ~{total_est * 15 // 1024} MB (already-cached tiles will be skipped)")
    print(f"Concurrent workers: {DOWNLOAD_WORKERS}")
    print()

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


if __name__ == "__main__":
    main()
