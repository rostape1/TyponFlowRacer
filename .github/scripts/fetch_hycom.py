#!/usr/bin/env python3
"""
Fetch HYCOM ocean surface currents via OPeNDAP and output as JSON.

Covers coastal California (lat 36.4-38.15, lon -123.1 to -121.6) at 1/12° resolution.
Outputs 9 time steps (3-hourly = 27 hours) of surface u/v velocity.

Usage:  python3 .github/scripts/fetch_hycom.py
Output: static/data/hycom/currents.json
"""

import json
import sys
import urllib.request
import re
from pathlib import Path
from datetime import datetime, timezone

OPENDAP_BASE = (
    "https://tds.hycom.org/thredds/dodsC/"
    "FMRC_ESPC-D-V02_uv3z/FMRC_ESPC-D-V02_uv3z_best.ncd"
)

SOUTH, NORTH = 36.4, 38.15
WEST, EAST = -123.1, -121.6

LAT_START = -80.0
LAT_STEP = 0.04
LON_STEP = 0.08
MS_TO_KN = 1.94384

TIME_STEPS = 9  # 3-hourly = 27 hours


def lat_idx(lat):
    return round((lat - LAT_START) / LAT_STEP)


def lon_idx(lon):
    return round(((lon + 360) % 360) / LON_STEP)


def fetch_opendap(url):
    print(f"  Fetching OPeNDAP...", end="", flush=True)
    req = urllib.request.Request(url, headers={"User-Agent": "AIS-Tracker/1.0"})
    r = urllib.request.urlopen(req, timeout=120)
    data = r.read().decode("utf-8")
    print(f" {len(data)//1024}KB")
    return data


def parse_array_1d(text, name):
    pattern = rf"\n{name}\[\d+\]\n([\s\S]*?)(?:\n\n|\n{name}\.|$)"
    m = re.search(pattern, text)
    if not m:
        return []
    return [float(x.strip()) for x in m.group(1).strip().split(",") if x.strip()]


def parse_grid_2d(text, var_name, time_idx, ny, nx):
    section = text.split(f"{var_name}.{var_name}")[1] if f"{var_name}.{var_name}" in text else ""
    rows = []
    for r in range(ny):
        pattern = f"[{time_idx}][0][{r}],"
        for line in section.split("\n"):
            if pattern in line:
                vals_str = line[line.index(pattern) + len(pattern):]
                vals = [float(x.strip()) for x in vals_str.split(",") if x.strip()]
                rows.append(vals)
                break
        else:
            rows.append([0.0] * nx)
    return rows


def main():
    out_dir = Path(__file__).resolve().parent.parent.parent / "static" / "data" / "hycom"
    out_path = out_dir / "currents.json"

    lat_s = lat_idx(SOUTH)
    lat_e = lat_idx(NORTH)
    lon_s = lon_idx(WEST)
    lon_e = lon_idx(EAST)
    ny = lat_e - lat_s + 1
    nx = lon_e - lon_s + 1

    print(f"HYCOM fetch: lat[{lat_s}:{lat_e}] lon[{lon_s}:{lon_e}] = {ny}x{nx} grid")

    url = (
        f"{OPENDAP_BASE}.ascii?"
        f"water_u[0:1:{TIME_STEPS-1}][0][{lat_s}:{lat_e}][{lon_s}:{lon_e}],"
        f"water_v[0:1:{TIME_STEPS-1}][0][{lat_s}:{lat_e}][{lon_s}:{lon_e}],"
        f"lat[{lat_s}:{lat_e}],lon[{lon_s}:{lon_e}],"
        f"time[0:1:{TIME_STEPS-1}]"
    )

    text = fetch_opendap(url)

    lats = parse_array_1d(text, "lat")
    lons_raw = parse_array_1d(text, "lon")
    lons = [l - 360 if l > 180 else l for l in lons_raw]

    if not lats or not lons:
        print("ERROR: Failed to parse lat/lon arrays")
        return 1

    bounds = {
        "south": round(lats[0], 4),
        "north": round(lats[-1], 4),
        "west": round(lons[0], 4),
        "east": round(lons[-1], 4),
    }
    print(f"  Bounds: {bounds}")

    hours = {}
    for t in range(TIME_STEPS):
        u_grid = parse_grid_2d(text, "water_u", t, ny, nx)
        v_grid = parse_grid_2d(text, "water_v", t, ny, nx)

        # Convert m/s to knots, round to 3 decimal places
        u_kn = [[round(v * MS_TO_KN, 3) if v == v else 0 for v in row] for row in u_grid]
        v_kn = [[round(v * MS_TO_KN, 3) if v == v else 0 for v in row] for row in v_grid]

        hour_key = t * 3
        hours[hour_key] = {"u": u_kn, "v": v_kn, "bounds": bounds, "nx": nx, "ny": ny}

    print(f"  Parsed {len(hours)} time steps")

    # Validate: check a few values aren't all zero
    non_zero = sum(1 for h in hours.values() for row in h["u"] for v in row if abs(v) > 0.001)
    print(f"  Non-zero u values: {non_zero}")
    if non_zero == 0:
        print("WARNING: All current values are zero")

    out_dir.mkdir(parents=True, exist_ok=True)
    output = {
        "hours": hours,
        "bounds": bounds,
        "nx": nx,
        "ny": ny,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(out_path, "w") as f:
        json.dump(output, f, separators=(",", ":"))

    size_kb = out_path.stat().st_size / 1024
    print(f"\nOutput: {out_path} ({size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
