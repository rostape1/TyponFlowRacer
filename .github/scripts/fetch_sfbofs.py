#!/usr/bin/env python3
"""
Fetch SFBOFS current field data from NOAA S3 and output per-hour JSON files.

Downloads NetCDF files from the NOAA SF Bay Operational Forecast System,
extracts surface u/v velocity, regrids to regular lat/lon, and writes
JSON files for hours 0-48.

Requires: netCDF4, scipy, numpy
"""

import json
import logging
import os
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError

import numpy as np

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

S3_BUCKET = "https://noaa-nos-ofs-pds.s3.amazonaws.com"
MODEL_RUNS = ["21", "15", "09", "03"]

BOUNDS = {
    "south": 37.40,
    "north": 38.05,
    "west": -122.65,
    "east": -122.10,
}

GRID_SPACING = 0.002  # ~200m

OUTPUT_DIR = Path(__file__).resolve().parent.parent.parent / "static" / "data" / "sfbofs"


def _s3_url(date_str: str, run_hour: str, forecast_hour: int = 0) -> str:
    d = datetime.strptime(date_str, "%Y%m%d")
    return (
        f"{S3_BUCKET}/sfbofs/netcdf/{d.year}/{d.month:02d}/{d.day:02d}/"
        f"sfbofs.t{run_hour}z.{date_str}.fields.f{forecast_hour:03d}.nc"
    )


def _download_file(url: str, dest: str) -> bool:
    try:
        req = Request(url, headers={"User-Agent": "AIS-Tracker/1.0"})
        with urlopen(req, timeout=120) as resp:
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


def _head_ok(url: str) -> bool:
    try:
        req = Request(url, method="HEAD", headers={"User-Agent": "AIS-Tracker/1.0"})
        with urlopen(req, timeout=10):
            return True
    except (URLError, TimeoutError, OSError):
        return False


def _find_latest_run() -> tuple[str, str] | None:
    now = datetime.now(timezone.utc)
    for days_back in range(2):
        d = now - timedelta(days=days_back)
        date_str = d.strftime("%Y%m%d")
        for run in MODEL_RUNS:
            run_hour = int(run)
            if days_back == 0 and now.hour < run_hour + 1:
                logger.debug(f"Skipping {date_str} t{run}z (too recent)")
                continue
            url0 = _s3_url(date_str, run, 0)
            ok0 = _head_ok(url0)
            logger.info(f"Check {date_str} t{run}z: hour_00={'OK' if ok0 else '404'}")
            if ok0:
                logger.info(f"Found SFBOFS run: {date_str} t{run}z")
                return (date_str, run)
    return None


def _extract_and_regrid(nc_path: str, forecast_hour: int) -> dict | None:
    import netCDF4 as nc
    from scipy.interpolate import griddata

    try:
        ds = nc.Dataset(nc_path, "r")
    except Exception as e:
        logger.error(f"Failed to open NetCDF: {e}")
        return None

    try:
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

        lats = ds.variables[lat_var][:]
        lons = ds.variables[lon_var][:]

        if np.max(lons) > 180:
            lons = np.where(lons > 180, lons - 360, lons)

        u_data = ds.variables[u_var]
        v_data = ds.variables[v_var]

        if u_data.ndim == 3:
            u_surface = u_data[0, 0, :]
            v_surface = v_data[0, 0, :]
        elif u_data.ndim == 2:
            u_surface = u_data[0, :]
            v_surface = v_data[0, :]
        else:
            u_surface = u_data[:]
            v_surface = v_data[:]

        if hasattr(u_surface, 'filled'):
            u_surface = u_surface.filled(0.0)
            v_surface = v_surface.filled(0.0)

        u_surface = np.array(u_surface, dtype=np.float64)
        v_surface = np.array(v_surface, dtype=np.float64)
        lats = np.array(lats, dtype=np.float64)
        lons = np.array(lons, dtype=np.float64)

        # m/s to knots
        MS_TO_KN = 1.94384
        u_surface *= MS_TO_KN
        v_surface *= MS_TO_KN

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

        if len(lats_sub) < 10:
            logger.error("Too few points in bounding box")
            return None

        ny = int((BOUNDS["north"] - BOUNDS["south"]) / GRID_SPACING) + 1
        nx = int((BOUNDS["east"] - BOUNDS["west"]) / GRID_SPACING) + 1

        grid_lat = np.linspace(BOUNDS["south"], BOUNDS["north"], ny)
        grid_lon = np.linspace(BOUNDS["west"], BOUNDS["east"], nx)
        grid_lon_2d, grid_lat_2d = np.meshgrid(grid_lon, grid_lat)

        points = np.column_stack([lons_sub, lats_sub])
        u_grid = griddata(points, u_sub, (grid_lon_2d, grid_lat_2d), method="linear", fill_value=0.0)
        v_grid = griddata(points, v_sub, (grid_lon_2d, grid_lat_2d), method="linear", fill_value=0.0)

        u_grid = np.round(u_grid, 3)
        v_grid = np.round(v_grid, 3)

        return {
            "bounds": BOUNDS,
            "nx": nx,
            "ny": ny,
            "source": "NOAA SFBOFS",
            "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
            "forecast_hour": forecast_hour,
            "u": u_grid.tolist(),
            "v": v_grid.tolist(),
        }
    finally:
        ds.close()


def _process_hour(args: tuple) -> tuple[int, dict | None]:
    """Download and process a single forecast hour. Designed for multiprocessing."""
    forecast_hour, date_str, run_hour, model_run_label = args
    url = _s3_url(date_str, run_hour, forecast_hour=forecast_hour)

    with tempfile.NamedTemporaryFile(suffix=".nc", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        if not _download_file(url, tmp_path):
            logger.warning(f"Failed to download hour {forecast_hour}")
            return (forecast_hour, None)

        result = _extract_and_regrid(tmp_path, forecast_hour)
        if result:
            result["model_run"] = model_run_label
        return (forecast_hour, result)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def main():
    run_info = _find_latest_run()
    if not run_info:
        logger.error("No SFBOFS data available on S3")
        sys.exit(1)

    date_str, run_hour = run_info

    # Load meta to check what we already have
    meta_path = OUTPUT_DIR.parent / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            pass

    same_run = meta.get("sfbofs_run") == f"{date_str}_t{run_hour}z"
    cached_hours = meta.get("sfbofs_hours", 0)
    cached_max = meta.get("sfbofs_max_hour", -1)

    if same_run and cached_hours >= 49:
        logger.info(f"SFBOFS already complete ({date_str} t{run_hour}z, {cached_hours}h), skipping")
        return

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # For a new run, clear stale hour files from the previous run first
    # so hours that don't exist in the new run don't serve old data
    if not same_run:
        for old in OUTPUT_DIR.glob("hour_*.json"):
            old.unlink()
        logger.info("Cleared old run hour files")

    # Build a human-readable model run label, e.g. "t15z 04/19"
    d = datetime.strptime(date_str, "%Y%m%d")
    model_run_label = f"t{run_hour}z {d.month:02d}/{d.day:02d}"

    # SFBOFS forecast files: f000 = cycle time, f001 = +1h, ..., f048 = +48h
    if same_run:
        hours_to_fetch = [h for h in range(49)
                          if not (OUTPUT_DIR / f"hour_{h:02d}.json").exists()]
        existing_count = 49 - len(hours_to_fetch)
        logger.info(f"Run {date_str} t{run_hour}z: {existing_count} hours on disk, fetching up to {len(hours_to_fetch)} missing")
    else:
        hours_to_fetch = list(range(49))
        existing_count = 0
        logger.info(f"New run {date_str} t{run_hour}z: fetching up to 49 hours")

    new_success = 0
    max_hour = cached_max if same_run else -1
    if hours_to_fetch:
        # Process in batches of 4 (parallel within batch, sequential between batches)
        # Break when an entire batch fails (NOAA hasn't published those hours yet)
        batch_size = 4
        for i in range(0, len(hours_to_fetch), batch_size):
            batch = hours_to_fetch[i:i + batch_size]
            args = [(h, date_str, run_hour, model_run_label) for h in batch]
            batch_success = 0
            with ProcessPoolExecutor(max_workers=4) as pool:
                futures = {pool.submit(_process_hour, a): a[0] for a in args}
                for future in as_completed(futures):
                    hour, result = future.result()
                    if result:
                        out_path = OUTPUT_DIR / f"hour_{hour:02d}.json"
                        out_path.write_text(json.dumps(result))
                        new_success += 1
                        batch_success += 1
                        max_hour = max(max_hour, hour)
                        logger.info(f"Wrote hour {hour:02d} ({existing_count + new_success}/49)")
                    else:
                        logger.warning(f"Failed hour {hour}")
            if batch_success == 0:
                logger.info(f"Batch starting at hour {batch[0]} had no successes — NOAA likely hasn't published beyond hour {max_hour}")
                break

    total_success = existing_count + new_success
    logger.info(f"SFBOFS: {total_success}/49 hours available (max hour {max_hour}, {new_success} newly fetched)")

    # Save run ID as soon as any hours succeed so incremental logic kicks in on next retry
    if new_success > 0:
        meta["sfbofs_run"] = f"{date_str}_t{run_hour}z"
    meta["sfbofs_hours"] = total_success
    meta["sfbofs_max_hour"] = max_hour
    meta["sfbofs_updated"] = datetime.now(timezone.utc).isoformat()
    meta_path.write_text(json.dumps(meta, indent=2))

    if total_success == 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
