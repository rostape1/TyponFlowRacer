"""
Wind data — HRRR forecast grid via Open-Meteo + NDBC station observations.

Fetches gridded wind data from the NOAA HRRR model (via Open-Meteo API)
and real anemometer readings from NDBC coastal stations. Serves both
to the frontend for particle-based wind visualization and station markers.
"""

import asyncio
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

logger = logging.getLogger(__name__)

# SF Bay + offshore bounding box (extends west to cover offshore buoys)
BOUNDS = {
    "south": 37.30,
    "north": 38.10,
    "west": -122.95,
    "east": -122.10,
}

# Coarse grid — wind varies slowly over bay distances
GRID_NX = 9
GRID_NY = 8

# Cache settings
GRID_CACHE_TTL = 1800    # 30 minutes for HRRR forecast
STATION_CACHE_TTL = 600  # 10 minutes for NDBC observations

# Caches
_grid_cache = None
_grid_cache_time = None
_station_cache = None
_station_cache_time = None
_forecast_cache = {}  # keyed by "forecast_{hour}" → {"grid": ..., "_cached_at": datetime}
_fetch_lock = None

# Offline paths
OFFLINE_GRID_PATH = Path(__file__).parent / "static" / "data" / "wind_field.json"
OFFLINE_STATION_PATH = Path(__file__).parent / "static" / "data" / "wind_stations.json"
OFFLINE_FORECAST_PATH = Path(__file__).parent / "static" / "data" / "wind_forecasts.json"
_forecast_cache_loaded = False

# NDBC stations in SF Bay area
NDBC_STATIONS = {
    "FTPC1": {"name": "Fort Point", "lat": 37.8060, "lon": -122.4659},
    "SFXC1": {"name": "SF Bar Pilots", "lat": 37.7600, "lon": -122.6900},
    "RCMC1": {"name": "Richmond", "lat": 37.9228, "lon": -122.4098},
    "AAMC1": {"name": "Alameda", "lat": 37.7717, "lon": -122.2992},
    "OKXC1": {"name": "Oakland", "lat": 37.8067, "lon": -122.3340},
    "PPXC1": {"name": "Point Potrero", "lat": 37.9078, "lon": -122.3728},
    "46026": {"name": "SF Buoy (offshore)", "lat": 37.7590, "lon": -122.8330},
    "TIBC1": {"name": "Tiburon", "lat": 37.8911, "lon": -122.4478},
    "46012": {"name": "Half Moon Bay", "lat": 37.3630, "lon": -122.8810},
}

# Open-Meteo API — current conditions
OPEN_METEO_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
    "&models=gfs_seamless"
    "&wind_speed_unit=kn"
)

# Open-Meteo API — hourly forecast
OPEN_METEO_FORECAST_URL = (
    "https://api.open-meteo.com/v1/forecast"
    "?latitude={lat}&longitude={lon}"
    "&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m"
    "&models=gfs_seamless"
    "&wind_speed_unit=kn"
    "&forecast_hours={hours}"
)

# NDBC real-time data
NDBC_URL = "https://www.ndbc.noaa.gov/data/realtime2/{station}.txt"

# Conversion constants
MS_TO_KN = 1.94384


def _fetch_url(url: str, timeout: int = 15) -> str | None:
    """Fetch URL content as string."""
    try:
        req = Request(url, headers={"User-Agent": "AIS-Tracker/1.0"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (URLError, TimeoutError, OSError) as e:
        logger.debug(f"Fetch failed: {url} — {e}")
        return None


def _fetch_grid(forecast_hour: int = 0) -> dict | None:
    """Fetch HRRR wind grid from Open-Meteo (runs in thread).

    forecast_hour: 0 = current conditions, 1-48 = forecast hours ahead.
    """
    lats = [BOUNDS["south"] + i * (BOUNDS["north"] - BOUNDS["south"]) / (GRID_NY - 1)
            for i in range(GRID_NY)]
    lons = [BOUNDS["west"] + i * (BOUNDS["east"] - BOUNDS["west"]) / (GRID_NX - 1)
            for i in range(GRID_NX)]

    # Initialize grids
    u_grid = [[0.0] * GRID_NX for _ in range(GRID_NY)]
    v_grid = [[0.0] * GRID_NX for _ in range(GRID_NY)]
    gust_grid = [[0.0] * GRID_NX for _ in range(GRID_NY)]

    success_count = 0
    is_forecast = forecast_hour > 0

    for iy, lat in enumerate(lats):
        for ix, lon in enumerate(lons):
            if is_forecast:
                url = OPEN_METEO_FORECAST_URL.format(
                    lat=lat, lon=lon, hours=forecast_hour + 1
                )
            else:
                url = OPEN_METEO_URL.format(lat=lat, lon=lon)
            text = _fetch_url(url)
            if not text:
                continue

            try:
                data = json.loads(text)

                if is_forecast:
                    # Extract the target hour from hourly arrays
                    hourly = data.get("hourly", {})
                    speeds = hourly.get("wind_speed_10m", [])
                    dirs = hourly.get("wind_direction_10m", [])
                    gusts = hourly.get("wind_gusts_10m", [])
                    idx = min(forecast_hour, len(speeds) - 1) if speeds else -1
                    if idx < 0:
                        continue
                    speed_kn = speeds[idx] or 0
                    direction = dirs[idx] or 0
                    gust_kn = gusts[idx] or 0
                else:
                    current = data.get("current", {})
                    speed_kn = current.get("wind_speed_10m", 0) or 0
                    direction = current.get("wind_direction_10m", 0) or 0
                    gust_kn = current.get("wind_gusts_10m", 0) or 0

                # Convert meteorological "from" direction + speed to u/v components
                dir_rad = direction * math.pi / 180
                u_grid[iy][ix] = round(-speed_kn * math.sin(dir_rad), 2)
                v_grid[iy][ix] = round(-speed_kn * math.cos(dir_rad), 2)
                gust_grid[iy][ix] = round(gust_kn, 1)
                success_count += 1
            except (json.JSONDecodeError, KeyError, TypeError) as e:
                logger.debug(f"Parse error for ({lat},{lon}): {e}")

    if success_count < GRID_NX * GRID_NY * 0.5:
        logger.warning(f"Too few grid points fetched: {success_count}/{GRID_NX * GRID_NY}")
        return None

    logger.info(f"Wind grid fetched: {success_count}/{GRID_NX * GRID_NY} points from Open-Meteo (HRRR) forecast_hour={forecast_hour}")

    result = {
        "bounds": BOUNDS,
        "nx": GRID_NX,
        "ny": GRID_NY,
        "model": "HRRR 3km",
        "source": "Open-Meteo (NOAA HRRR)",
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "forecast_hour": forecast_hour,
        "u": u_grid,
        "v": v_grid,
        "gusts": gust_grid,
    }

    # Save offline cache (only for current conditions)
    if not is_forecast:
        try:
            OFFLINE_GRID_PATH.parent.mkdir(parents=True, exist_ok=True)
            OFFLINE_GRID_PATH.write_text(json.dumps(result))
        except Exception as e:
            logger.debug(f"Failed to save offline wind grid: {e}")

    return result


def _fetch_stations() -> list | None:
    """Fetch real-time wind from NDBC stations (runs in thread)."""
    stations = []

    for station_id, info in NDBC_STATIONS.items():
        url = NDBC_URL.format(station=station_id)
        text = _fetch_url(url)
        if not text:
            continue

        try:
            lines = text.strip().split("\n")
            if len(lines) < 3:
                continue

            # First line: header, second line: units, third+: data
            header = lines[0].replace("#", "").split()
            data_line = lines[2].split()  # Most recent observation

            col = {h: i for i, h in enumerate(header)}

            wdir_idx = col.get("WDIR")
            wspd_idx = col.get("WSPD")
            gst_idx = col.get("GST")

            if wdir_idx is None or wspd_idx is None:
                continue

            wdir_str = data_line[wdir_idx]
            wspd_str = data_line[wspd_idx]
            gst_str = data_line[gst_idx] if gst_idx is not None else "MM"

            if wdir_str == "MM" or wspd_str == "MM":
                continue

            # NDBC: WSPD/GST in m/s, WDIR in degrees true
            speed_kn = float(wspd_str) * MS_TO_KN
            gust_kn = float(gst_str) * MS_TO_KN if gst_str != "MM" else None
            direction = float(wdir_str)

            # Build timestamp from data columns
            yr = data_line[col["YY"]]
            mo = data_line[col["MM"]]
            dy = data_line[col["DD"]]
            hh = data_line[col["hh"]]
            mm = data_line[col["mm"]]
            timestamp = f"{yr}-{mo}-{dy}T{hh}:{mm}Z"

            stations.append({
                "id": station_id,
                "name": info["name"],
                "lat": info["lat"],
                "lon": info["lon"],
                "speed_kn": round(speed_kn, 1),
                "gust_kn": round(gust_kn, 1) if gust_kn is not None else None,
                "direction": direction,
                "timestamp": timestamp,
            })

        except (ValueError, IndexError) as e:
            logger.debug(f"Parse error for station {station_id}: {e}")

    if not stations:
        logger.warning("No NDBC station data retrieved")
        return None

    logger.info(f"Wind stations fetched: {len(stations)}/{len(NDBC_STATIONS)}")

    # Save offline cache
    try:
        OFFLINE_STATION_PATH.parent.mkdir(parents=True, exist_ok=True)
        OFFLINE_STATION_PATH.write_text(json.dumps(stations))
    except Exception as e:
        logger.debug(f"Failed to save offline wind stations: {e}")

    return stations


def save_forecast_cache():
    """Save all forecast hours to disk for offline use."""
    if not _forecast_cache:
        return
    try:
        data = {}
        for key, entry in _forecast_cache.items():
            hour = key.replace("forecast_", "")
            data[hour] = {
                "grid": entry["grid"],
                "cached_at": entry["_cached_at"].isoformat(),
            }
        OFFLINE_FORECAST_PATH.parent.mkdir(parents=True, exist_ok=True)
        OFFLINE_FORECAST_PATH.write_text(json.dumps(data))
        logger.info(f"Saved wind forecast cache: {len(data)} hours to disk")
    except Exception as e:
        logger.warning(f"Failed to save wind forecast cache: {e}")


def load_forecast_cache():
    """Load forecast hours from disk into memory cache."""
    global _forecast_cache, _forecast_cache_loaded
    if _forecast_cache_loaded:
        return
    _forecast_cache_loaded = True
    if not OFFLINE_FORECAST_PATH.exists():
        return
    try:
        data = json.loads(OFFLINE_FORECAST_PATH.read_text())
        loaded = 0
        for hour_str, entry in data.items():
            cache_key = f"forecast_{hour_str}"
            if cache_key not in _forecast_cache:
                _forecast_cache[cache_key] = {
                    "grid": entry["grid"],
                    "_cached_at": datetime.fromisoformat(entry["cached_at"]),
                }
                loaded += 1
        if loaded:
            logger.info(f"Loaded wind forecast cache from disk: {loaded} hours")
    except Exception as e:
        logger.warning(f"Failed to load wind forecast cache: {e}")


async def get_wind_field(forecast_hour: int = 0) -> dict | None:
    """Get combined wind grid + station observations. Uses caches.

    forecast_hour: 0 = current conditions, 1-48 = forecast hours ahead.
    When forecasting, NDBC stations are excluded (observations only).
    """
    global _grid_cache, _grid_cache_time, _station_cache, _station_cache_time, _forecast_cache, _fetch_lock

    now = datetime.now(timezone.utc)
    is_forecast = forecast_hour > 0

    # For forecast requests, use separate forecast cache
    if is_forecast:
        load_forecast_cache()  # Load from disk on first call
        cache_key = f"forecast_{forecast_hour}"
        cached = _forecast_cache.get(cache_key)
        if cached and (now - cached["_cached_at"]).total_seconds() < GRID_CACHE_TTL:
            return {"grid": cached["grid"], "stations": [], "forecast_hour": forecast_hour}

        if _fetch_lock is None:
            _fetch_lock = asyncio.Lock()

        async with _fetch_lock:
            # Double-check
            cached = _forecast_cache.get(cache_key)
            if cached and (now - cached["_cached_at"]).total_seconds() < GRID_CACHE_TTL:
                return {"grid": cached["grid"], "stations": [], "forecast_hour": forecast_hour}

            loop = asyncio.get_event_loop()
            grid_result = await loop.run_in_executor(None, _fetch_grid, forecast_hour)
            if grid_result:
                _forecast_cache[cache_key] = {"grid": grid_result, "_cached_at": now}
                return {"grid": grid_result, "stations": [], "forecast_hour": forecast_hour}
            return None

    # Real-time path (original logic)

    # On first call, load offline cache immediately so we have data right away
    if _grid_cache is None and OFFLINE_GRID_PATH.exists():
        try:
            _grid_cache = json.loads(OFFLINE_GRID_PATH.read_text())
            # Set to epoch so offline data is immediately considered stale
            _grid_cache_time = datetime(2000, 1, 1, tzinfo=timezone.utc)
            logger.info("Loaded offline wind grid cache (will refresh in background)")
        except Exception:
            pass

    if _station_cache is None and OFFLINE_STATION_PATH.exists():
        try:
            _station_cache = json.loads(OFFLINE_STATION_PATH.read_text())
            _station_cache_time = datetime(2000, 1, 1, tzinfo=timezone.utc)
            logger.info("Loaded offline wind station cache (will refresh in background)")
        except Exception:
            pass

    # Check if both caches are fresh
    grid_fresh = (_grid_cache and _grid_cache_time and
                  (now - _grid_cache_time).total_seconds() < GRID_CACHE_TTL)
    stations_fresh = (_station_cache and _station_cache_time and
                      (now - _station_cache_time).total_seconds() < STATION_CACHE_TTL)

    if grid_fresh and stations_fresh:
        return {"grid": _grid_cache, "stations": _station_cache}

    # Lazy-init lock
    if _fetch_lock is None:
        _fetch_lock = asyncio.Lock()

    async with _fetch_lock:
        # Double-check after acquiring lock
        now = datetime.now(timezone.utc)
        grid_fresh = (_grid_cache and _grid_cache_time and
                      (now - _grid_cache_time).total_seconds() < GRID_CACHE_TTL)
        stations_fresh = (_station_cache and _station_cache_time and
                          (now - _station_cache_time).total_seconds() < STATION_CACHE_TTL)

        loop = asyncio.get_event_loop()

        # Fetch stale data in parallel
        grid_future = None
        station_future = None

        if not grid_fresh:
            grid_future = loop.run_in_executor(None, _fetch_grid, 0)
        if not stations_fresh:
            station_future = loop.run_in_executor(None, _fetch_stations)

        if grid_future:
            result = await grid_future
            if result:
                _grid_cache = result
                _grid_cache_time = datetime.now(timezone.utc)

        if station_future:
            result = await station_future
            if result:
                _station_cache = result
                _station_cache_time = datetime.now(timezone.utc)

    if not _grid_cache and not _station_cache:
        return None

    return {
        "grid": _grid_cache,
        "stations": _station_cache or [],
    }
