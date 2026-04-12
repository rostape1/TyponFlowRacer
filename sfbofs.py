"""
SFBOFS — NOAA SF Bay Operational Forecast System gridded current data.

Downloads the latest FVCOM forecast from NOAA's public S3 bucket,
extracts surface u/v velocity, regrids to a regular lat/lon grid,
and serves it to the frontend for particle-based flow visualization.
"""

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

import numpy as np

logger = logging.getLogger(__name__)

S3_BUCKET = "https://noaa-nos-ofs-pds.s3.amazonaws.com"
MODEL_RUNS = ["21", "15", "09", "03"]  # Try most recent first

# SF Bay bounding box for regridding
BOUNDS = {
    "south": 37.40,
    "north": 38.05,
    "west": -122.65,
    "east": -122.10,
}

# Grid resolution (~200m spacing)
GRID_SPACING = 0.002  # degrees

# Cache — keyed by forecast_hour
_grid_cache: dict[int, dict] = {}
_cache_times: dict[int, datetime] = {}
_fetch_lock = None  # Initialized lazily to avoid event loop issues
CACHE_TTL = 6 * 3600  # 6 hours

# Offline cache path
OFFLINE_PATH = Path(__file__).parent / "static" / "data" / "sfbofs_field.json"
OFFLINE_FORECAST_PATH = Path(__file__).parent / "static" / "data" / "sfbofs_forecasts.json"
_forecast_cache_loaded = False


def _s3_url(date_str: str, run_hour: str, forecast_hour: int = 0) -> str:
    """Build S3 URL for a SFBOFS fields file."""
    d = datetime.strptime(date_str, "%Y%m%d")
    return (
        f"{S3_BUCKET}/sfbofs/netcdf/{d.year}/{d.month:02d}/{d.day:02d}/"
        f"sfbofs.t{run_hour}z.{date_str}.fields.n{forecast_hour:03d}.nc"
    )


def _download_file(url: str, dest: str) -> bool:
    """Download a file from URL to dest path."""
    try:
        req = Request(url, headers={"User-Agent": "AIS-Tracker/1.0"})
        with urlopen(req, timeout=60) as resp:
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
        return True
    except (URLError, TimeoutError, OSError) as e:
        logger.debug(f"Download failed: {url} — {e}")
        return False


def _find_latest_run() -> tuple[str, str] | None:
    """Find the most recent available SFBOFS model run on S3.

    Returns (date_str, run_hour) or None.
    """
    now = datetime.now(timezone.utc)

    # Try today and yesterday
    for days_back in range(2):
        d = now - __import__("datetime").timedelta(days=days_back)
        date_str = d.strftime("%Y%m%d")

        for run in MODEL_RUNS:
            run_hour = int(run)
            # Only try runs that have had time to be published (allow ~3h processing)
            if days_back == 0 and now.hour < run_hour + 3:
                continue

            url = _s3_url(date_str, run, forecast_hour=0)
            # Quick HEAD check
            try:
                req = Request(url, method="HEAD", headers={"User-Agent": "AIS-Tracker/1.0"})
                with urlopen(req, timeout=10):
                    logger.info(f"Found SFBOFS run: {date_str} t{run}z")
                    return (date_str, run)
            except (URLError, TimeoutError, OSError):
                continue

    return None


def _extract_and_regrid(nc_path: str, source_url: str = "") -> dict | None:
    """Open NetCDF file, extract surface u/v, regrid to regular lat/lon."""
    try:
        import netCDF4 as nc
        from scipy.interpolate import griddata
    except ImportError as e:
        logger.error(f"Missing dependency: {e}. Install: pip install netCDF4 scipy")
        return None

    try:
        ds = nc.Dataset(nc_path, "r")
    except Exception as e:
        logger.error(f"Failed to open NetCDF: {e}")
        return None

    try:
        # Log available variables for debugging
        logger.info(f"NetCDF variables: {list(ds.variables.keys())}")

        # FVCOM uses cell-center coordinates for velocity
        # Try common variable names
        lat_var = lon_var = u_var = v_var = None

        for name in ["latc", "lat"]:
            if name in ds.variables:
                lat_var = name
                break
        for name in ["lonc", "lon"]:
            if name in ds.variables:
                lon_var = name
                break
        for name in ["u", "water_u"]:
            if name in ds.variables:
                u_var = name
                break
        for name in ["v", "water_v"]:
            if name in ds.variables:
                v_var = name
                break

        if not all([lat_var, lon_var, u_var, v_var]):
            logger.error(f"Missing variables. Found: lat={lat_var}, lon={lon_var}, u={u_var}, v={v_var}")
            return None

        # Read cell-center coordinates
        lats = ds.variables[lat_var][:]
        lons = ds.variables[lon_var][:]

        # Debug: log coordinate ranges to diagnose bounding box issues
        logger.info(f"Coordinate ranges: lat [{np.min(lats):.4f}, {np.max(lats):.4f}], lon [{np.min(lons):.4f}, {np.max(lons):.4f}]")

        # Convert 0-360 longitude to -180-180 if needed
        if np.max(lons) > 180:
            lons = np.where(lons > 180, lons - 360, lons)
            logger.info(f"Converted lon to -180/180: [{np.min(lons):.4f}, {np.max(lons):.4f}]")

        # Read velocity — shape is typically (time, siglay, nele) for FVCOM
        u_data = ds.variables[u_var]
        v_data = ds.variables[v_var]

        logger.info(f"u shape: {u_data.shape}, lat shape: {lats.shape}")

        # Extract surface layer (first sigma layer) at first time step
        if u_data.ndim == 3:  # (time, siglay, nele)
            u_surface = u_data[0, 0, :]
            v_surface = v_data[0, 0, :]
        elif u_data.ndim == 2:  # (time, nele)
            u_surface = u_data[0, :]
            v_surface = v_data[0, :]
        else:
            u_surface = u_data[:]
            v_surface = v_data[:]

        # Convert from numpy masked array if needed
        if hasattr(u_surface, 'filled'):
            u_surface = u_surface.filled(0.0)
            v_surface = v_surface.filled(0.0)

        u_surface = np.array(u_surface, dtype=np.float64)
        v_surface = np.array(v_surface, dtype=np.float64)
        lats = np.array(lats, dtype=np.float64)
        lons = np.array(lons, dtype=np.float64)

        # Convert m/s to knots
        MS_TO_KN = 1.94384
        u_surface *= MS_TO_KN
        v_surface *= MS_TO_KN

        # Filter to bounding box (with some margin)
        margin = 0.05
        mask = (
            (lats >= BOUNDS["south"] - margin) &
            (lats <= BOUNDS["north"] + margin) &
            (lons >= BOUNDS["west"] - margin) &
            (lons <= BOUNDS["east"] + margin)
        )

        lats_sub = lats[mask]
        lons_sub = lons[mask]
        u_sub = u_surface[mask]
        v_sub = v_surface[mask]

        logger.info(f"Points in bounding box: {len(lats_sub)}")

        if len(lats_sub) < 10:
            logger.error("Too few points in bounding box")
            return None

        # Build regular grid
        ny = int((BOUNDS["north"] - BOUNDS["south"]) / GRID_SPACING) + 1
        nx = int((BOUNDS["east"] - BOUNDS["west"]) / GRID_SPACING) + 1

        grid_lat = np.linspace(BOUNDS["south"], BOUNDS["north"], ny)
        grid_lon = np.linspace(BOUNDS["west"], BOUNDS["east"], nx)
        grid_lon_2d, grid_lat_2d = np.meshgrid(grid_lon, grid_lat)

        # Regrid using linear interpolation on unstructured points
        points = np.column_stack([lons_sub, lats_sub])
        u_grid = griddata(points, u_sub, (grid_lon_2d, grid_lat_2d), method="linear", fill_value=0.0)
        v_grid = griddata(points, v_sub, (grid_lon_2d, grid_lat_2d), method="linear", fill_value=0.0)

        # Round to reduce JSON size
        u_grid = np.round(u_grid, 3)
        v_grid = np.round(v_grid, 3)

        result = {
            "bounds": BOUNDS,
            "nx": nx,
            "ny": ny,
            "source": "NOAA SFBOFS",
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "u": u_grid.tolist(),
            "v": v_grid.tolist(),
        }

        logger.info(f"Regridded to {nx}x{ny} grid ({nx * ny} cells)")
        return result

    finally:
        ds.close()


async def get_current_field(forecast_hour: int = 0) -> dict | None:
    """Get the regridded current velocity field. Uses cache + S3 fetch.

    forecast_hour: 0 = nowcast, 1-48 = forecast hours ahead.
    """
    global _fetch_lock

    # Load forecast cache from disk on first call
    load_forecast_cache()

    # On first call for nowcast, load offline cache so we have data immediately
    if forecast_hour == 0 and 0 not in _grid_cache and OFFLINE_PATH.exists():
        try:
            data = json.loads(OFFLINE_PATH.read_text())
            _grid_cache[0] = data
            _cache_times[0] = datetime(2000, 1, 1, tzinfo=timezone.utc)
            logger.info("Loaded offline SFBOFS cache (will refresh in background)")
        except Exception:
            pass

    # Check memory cache for this forecast hour
    cached = _grid_cache.get(forecast_hour)
    cached_time = _cache_times.get(forecast_hour)
    if cached and cached_time:
        age = (datetime.now(timezone.utc) - cached_time).total_seconds()
        if age < CACHE_TTL:
            return cached

    # Lazy-init lock (must be in event loop context)
    if _fetch_lock is None:
        _fetch_lock = asyncio.Lock()

    # Return stale cache immediately if available, refresh in background
    stale_data = _grid_cache.get(forecast_hour)

    async with _fetch_lock:
        # Double-check after acquiring lock (another request may have completed)
        cached = _grid_cache.get(forecast_hour)
        cached_time = _cache_times.get(forecast_hour)
        if cached and cached_time:
            age = (datetime.now(timezone.utc) - cached_time).total_seconds()
            if age < CACHE_TTL:
                return cached

        # Try to fetch and process in a thread (blocking I/O)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, _fetch_and_process, forecast_hour)

        if result:
            _grid_cache[forecast_hour] = result
            _cache_times[forecast_hour] = datetime.now(timezone.utc)
            return result

    # Return stale data if online fetch failed
    if stale_data:
        return stale_data

    return None


def _fetch_and_process(forecast_hour: int = 0) -> dict | None:
    """Download latest SFBOFS file and extract grid (runs in thread)."""
    run_info = _find_latest_run()
    if not run_info:
        logger.warning("No SFBOFS data available on S3")
        return None

    date_str, run_hour = run_info
    url = _s3_url(date_str, run_hour, forecast_hour=forecast_hour)

    # Download to temp file
    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        logger.info(f"Downloading SFBOFS: {url}")
        if not _download_file(url, tmp_path):
            # If requested forecast hour is not available, fall back to closest
            if forecast_hour > 0:
                logger.warning(f"Forecast hour {forecast_hour} not available, trying hour 0")
                url = _s3_url(date_str, run_hour, forecast_hour=0)
                if not _download_file(url, tmp_path):
                    return None
            else:
                return None

        logger.info("Extracting and regridding...")
        result = _extract_and_regrid(tmp_path, source_url=url)

        if result:
            result["forecast_hour"] = forecast_hour

        # Save offline cache (only for nowcast)
        if result and forecast_hour == 0:
            try:
                OFFLINE_PATH.parent.mkdir(parents=True, exist_ok=True)
                OFFLINE_PATH.write_text(json.dumps(result))
                logger.info(f"Saved offline cache: {OFFLINE_PATH}")
            except Exception as e:
                logger.debug(f"Failed to save offline cache: {e}")

        return result

    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


async def save_offline_field():
    """Download and cache the SFBOFS field for offline use."""
    result = await get_current_field()
    if result:
        OFFLINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        OFFLINE_PATH.write_text(json.dumps(result))
        return True
    return False


def save_forecast_cache():
    """Save all forecast hours to disk for offline use."""
    if not _grid_cache:
        return
    try:
        data = {}
        for hour, grid in _grid_cache.items():
            cached_time = _cache_times.get(hour)
            if cached_time:
                data[str(hour)] = {
                    "grid": grid,
                    "cached_at": cached_time.isoformat(),
                }
        OFFLINE_FORECAST_PATH.parent.mkdir(parents=True, exist_ok=True)
        OFFLINE_FORECAST_PATH.write_text(json.dumps(data))
        logger.info(f"Saved SFBOFS forecast cache: {len(data)} hours to disk")
    except Exception as e:
        logger.warning(f"Failed to save SFBOFS forecast cache: {e}")


def load_forecast_cache():
    """Load forecast hours from disk into memory cache."""
    global _forecast_cache_loaded
    if _forecast_cache_loaded:
        return
    _forecast_cache_loaded = True
    if not OFFLINE_FORECAST_PATH.exists():
        return
    try:
        data = json.loads(OFFLINE_FORECAST_PATH.read_text())
        loaded = 0
        for hour_str, entry in data.items():
            hour = int(hour_str)
            if hour not in _grid_cache:
                _grid_cache[hour] = entry["grid"]
                _cache_times[hour] = datetime.fromisoformat(entry["cached_at"])
                loaded += 1
        if loaded:
            logger.info(f"Loaded SFBOFS forecast cache from disk: {loaded} hours")
    except Exception as e:
        logger.warning(f"Failed to load SFBOFS forecast cache: {e}")
