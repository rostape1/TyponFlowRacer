#!/usr/bin/env python3
"""
Fetch HRRR wind grid from Open-Meteo API and output per-hour JSON files.

Optimized: fetches all 49 forecast hours per grid point in a single request,
then slices into per-hour output files. Total: 72 API calls instead of 3,528.
"""

import json
import logging
import math
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BOUNDS = {
    "south": 37.30,
    "north": 38.10,
    "west": -122.95,
    "east": -122.10,
}

GRID_NX = 9
GRID_NY = 8

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "data" / "wind"

# Open-Meteo: fetch all hours in one request per point
OPEN_METEO_ALL_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
    "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
    "&models=gfs_seamless"
    "&wind_speed_unit=kn"
    "&forecast_hours=49"
)


def _fetch_url(url: str, timeout: int = 15) -> str | None:
    try:
        req = Request(url, headers={"User-Agent": "AIS-Tracker/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (URLError, TimeoutError, OSError) as e:
        logger.debug(f"Fetch failed: {url} — {e}")
        return None


def _fetch_point(iy: int, ix: int, lat: float, lon: float) -> tuple | None:
    """Fetch all forecast hours for a single grid point.

    Returns (iy, ix, current_data, hourly_data) or None.
    current_data = (u, v, gust)
    hourly_data = list of 49 (u, v, gust) tuples
    """
    url = OPEN_METEO_ALL_URL.format(lat=lat, lon=lon)
    text = _fetch_url(url)
    if not text:
        return None

    try:
        data = json.loads(text)

        # Current conditions (hour 0)
        current = data.get("current", {})
        cur_speed = current.get("wind_speed_10m", 0) or 0
        cur_dir = current.get("wind_direction_10m", 0) or 0
        cur_gust = current.get("wind_gusts_10m", 0) or 0
        cur_rad = cur_dir * math.pi / 180
        cur_u = round(-cur_speed * math.sin(cur_rad), 2)
        cur_v = round(-cur_speed * math.cos(cur_rad), 2)

        # Hourly forecast (hours 0-48)
        hourly = data.get("hourly", {})
        speeds = hourly.get("wind_speed_10m", [])
        dirs = hourly.get("wind_direction_10m", [])
        gusts = hourly.get("wind_gusts_10m", [])

        hourly_data = []
        for h in range(min(49, len(speeds))):
            spd = speeds[h] or 0
            d = dirs[h] or 0
            g = gusts[h] or 0
            rad = d * math.pi / 180
            u = round(-spd * math.sin(rad), 2)
            v = round(-spd * math.cos(rad), 2)
            hourly_data.append((u, v, round(g, 1)))

        return (iy, ix, (cur_u, cur_v, round(cur_gust, 1)), hourly_data)
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.debug(f"Parse error for ({lat},{lon}): {e}")
        return None


def main():
    lats = [BOUNDS["south"] + i * (BOUNDS["north"] - BOUNDS["south"]) / (GRID_NY - 1)
            for i in range(GRID_NY)]
    lons = [BOUNDS["west"] + i * (BOUNDS["east"] - BOUNDS["west"]) / (GRID_NX - 1)
            for i in range(GRID_NX)]

    # Fetch all grid points with all hours in parallel
    all_results = {}
    success_count = 0

    with ThreadPoolExecutor(max_workers=20) as pool:
        futures = {}
        for iy, lat in enumerate(lats):
            for ix, lon in enumerate(lons):
                f = pool.submit(_fetch_point, iy, ix, lat, lon)
                futures[f] = (iy, ix)

        for f in as_completed(futures):
            result = f.result()
            if result:
                iy, ix, current_data, hourly_data = result
                all_results[(iy, ix)] = (current_data, hourly_data)
                success_count += 1

    total_points = GRID_NX * GRID_NY
    if success_count < total_points * 0.5:
        logger.error(f"Too few grid points: {success_count}/{total_points}")
        sys.exit(1)

    logger.info(f"Fetched {success_count}/{total_points} grid points from Open-Meteo")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Write per-hour JSON files
    for hour in range(49):
        u_grid = [[0.0] * GRID_NX for _ in range(GRID_NY)]
        v_grid = [[0.0] * GRID_NX for _ in range(GRID_NY)]
        gust_grid = [[0.0] * GRID_NX for _ in range(GRID_NY)]

        for (iy, ix), (current_data, hourly_data) in all_results.items():
            if hour == 0:
                # Use current conditions for hour 0
                u, v, gust = current_data
            elif hour < len(hourly_data):
                u, v, gust = hourly_data[hour]
            else:
                continue
            u_grid[iy][ix] = u
            v_grid[iy][ix] = v
            gust_grid[iy][ix] = gust

        result = {
            "bounds": BOUNDS,
            "nx": GRID_NX,
            "ny": GRID_NY,
            "model": "HRRR 3km",
            "source": "NOAA HRRR",
            "fetched_at": now_str,
            "forecast_hour": hour,
            "u": u_grid,
            "v": v_grid,
            "gusts": gust_grid,
        }

        out_path = OUTPUT_DIR / f"hour_{hour:02d}.json"
        out_path.write_text(json.dumps(result))

    logger.info(f"Wrote 49 wind grid files to {OUTPUT_DIR}")

    # Update meta
    meta_path = OUTPUT_DIR.parent / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass
    meta["wind_updated"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta, indent=2))


if __name__ == "__main__":
    main()
