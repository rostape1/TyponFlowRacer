#!/usr/bin/env python3
"""
Generate land_mask.json from GSHHS (Global Self-consistent Hierarchical
High-resolution Shoreline) coastline data.

GSHHS Level 1 polygons trace the actual ocean/land boundary — the bay,
Golden Gate, and Pacific coast are all correctly represented as water
without needing separate water-body downloads or flood-fill heuristics.

Data source: NOAA/NCEI (public domain).

Usage:  python3 .github/scripts/generate_land_mask.py
Output: static/data/land_mask.json
"""

import base64
import io
import json
import math
import struct
import sys
import zipfile
from pathlib import Path

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

SOUTH, WEST, NORTH, EAST = 36.4, -123.1, 38.15, -121.6
GRID_RES = 0.002  # ~222m — Golden Gate is ~7 cells wide
SIMPLIFY_TOLERANCE = 0.0005  # ~55m Douglas-Peucker (grid is 222m, so 55m detail is sufficient)
MIN_RING_POINTS = 6

GSHHG_URL = "https://github.com/GenericMappingTools/gshhg-gmt/releases/download/2.3.7/gshhg-shp-2.3.7.zip"
GSHHG_LOCAL = "/tmp/gshhg-shp-2.3.7.zip"


def _parse_shapefile(shp_bytes):
    """Minimal .shp parser. Returns list of records, each = list of rings [(lon,lat)...]."""
    offset = 100
    records = []
    while offset + 8 <= len(shp_bytes):
        _, content_words = struct.unpack_from(">ii", shp_bytes, offset)
        content_len = content_words * 2
        offset += 8
        if offset + content_len > len(shp_bytes):
            break
        rec = shp_bytes[offset:offset + content_len]
        offset += content_len
        if len(rec) < 44:
            continue
        rec_type = struct.unpack_from("<i", rec, 0)[0]
        if rec_type not in (5, 15):
            continue
        num_parts = struct.unpack_from("<i", rec, 36)[0]
        num_points = struct.unpack_from("<i", rec, 40)[0]
        parts_off = 44
        points_off = parts_off + num_parts * 4
        if len(rec) < points_off + num_points * 16:
            continue
        parts = [struct.unpack_from("<i", rec, parts_off + i * 4)[0] for i in range(num_parts)]
        rings = []
        for p in range(num_parts):
            start = parts[p]
            end = parts[p + 1] if p + 1 < num_parts else num_points
            ring = []
            for i in range(start, end):
                x, y = struct.unpack_from("<dd", rec, points_off + i * 16)
                ring.append((x, y))
            if len(ring) >= 3:
                rings.append(ring)
        if rings:
            records.append(rings)
    return records


def _to_latlon(ring):
    """Convert (lon,lat) shapefile ring to (lat,lon)."""
    return [(lat, lon) for lon, lat in ring]


def _signed_area(ring):
    area = 0.0
    n = len(ring)
    for i in range(n):
        j = (i + 1) % n
        area += ring[i][1] * ring[j][0] - ring[j][1] * ring[i][0]
    return area / 2.0


def _point_in_ring(lat, lon, ring):
    inside = False
    j = len(ring) - 1
    for i in range(len(ring)):
        yi, xi = ring[i]
        yj, xj = ring[j]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _ring_in_bbox(ring):
    lats = [p[0] for p in ring]
    lons = [p[1] for p in ring]
    return max(lats) >= SOUTH and min(lats) <= NORTH and max(lons) >= WEST and min(lons) <= EAST


def _point_line_dist(p, a, b):
    dx, dy = b[1] - a[1], b[0] - a[0]
    if dx == 0 and dy == 0:
        return math.sqrt((p[0] - a[0]) ** 2 + (p[1] - a[1]) ** 2)
    t = max(0, min(1, ((p[1] - a[1]) * dx + (p[0] - a[0]) * dy) / (dx * dx + dy * dy)))
    return math.sqrt((p[0] - a[0] - t * dy) ** 2 + (p[1] - a[1] - t * dx) ** 2)


def _simplify(ring, tol):
    if len(ring) <= 4:
        return ring

    def dp(pts, s, e):
        if e - s < 2:
            return [pts[s], pts[e]]
        mx, mi = 0, s
        for i in range(s + 1, e):
            d = _point_line_dist(pts[i], pts[s], pts[e])
            if d > mx:
                mx, mi = d, i
        if mx > tol:
            return dp(pts, s, mi)[:-1] + dp(pts, mi, e)
        return [pts[s], pts[e]]

    sys.setrecursionlimit(max(sys.getrecursionlimit(), len(ring) * 2))
    r = dp(ring, 0, len(ring) - 1)
    if len(r) >= 3 and ring[0] == ring[-1]:
        r[-1] = r[0]
    return r


def fetch_gshhs():
    """Download GSHHS high-resolution coastline, extract land polygons overlapping our bbox."""
    print("Step 1: Loading GSHHS coastline data")

    # Try local file first, fall back to download
    local = Path(GSHHG_LOCAL)
    if local.exists():
        print(f"  Using local file: {local}")
        outer_zip = zipfile.ZipFile(str(local))
    else:
        print(f"  Downloading {GSHHG_URL}...", end="", flush=True)
        r = requests.get(GSHHG_URL, timeout=300)
        r.raise_for_status()
        print(f" {len(r.content) // 1024}KB")
        outer_zip = zipfile.ZipFile(io.BytesIO(r.content))

    # GSHHS Level 1 = ocean/land boundary (high resolution)
    shp_path = "GSHHS_shp/h/GSHHS_h_L1.shp"
    print(f"  Extracting {shp_path}...")
    shp_bytes = outer_zip.read(shp_path)
    records = _parse_shapefile(shp_bytes)
    print(f"  {len(records)} Level 1 (land) polygons worldwide")

    polygons = []
    for rec in records:
        for ring_raw in rec:
            ring = _to_latlon(ring_raw)
            if len(ring) >= 4 and _ring_in_bbox(ring):
                polygons.append(ring)

    polygons.sort(key=lambda r: abs(_signed_area(r)), reverse=True)
    print(f"  {len(polygons)} polygons overlap bbox")
    for i, p in enumerate(polygons[:5]):
        lats = [pt[0] for pt in p]
        lons = [pt[1] for pt in p]
        print(f"    #{i}: {len(p)} pts, lat {min(lats):.2f}-{max(lats):.2f}, "
              f"lon {min(lons):.2f}-{max(lons):.2f}")

    return polygons


def _is_land(lat, lon, polygons):
    for p in polygons:
        if _point_in_ring(lat, lon, p["outer"]):
            return True
    return False


def rasterize(polygons):
    """Pre-compute a binary land grid for O(1) lookups in the browser."""
    from matplotlib.path import Path
    import numpy as np

    print("\nStep 2: Rasterizing land grid")
    rows = int((NORTH - SOUTH) / GRID_RES)
    cols = int((EAST - WEST) / GRID_RES)
    total = rows * cols
    print(f"  Grid: {rows}x{cols} = {total:,} cells at {GRID_RES}° ({GRID_RES * 111000:.0f}m)")

    # Build grid of all cell centers
    lats = np.linspace(SOUTH + GRID_RES / 2, NORTH - GRID_RES / 2, rows)
    lons = np.linspace(WEST + GRID_RES / 2, EAST - GRID_RES / 2, cols)
    lon_grid, lat_grid = np.meshgrid(lons, lats)
    points = np.column_stack([lon_grid.ravel(), lat_grid.ravel()])

    land = np.zeros(total, dtype=bool)

    for i, poly in enumerate(polygons):
        ring = poly["outer"]
        # matplotlib Path uses (x, y) = (lon, lat)
        verts = [(p[1], p[0]) for p in ring]
        path = Path(verts)
        print(f"  Testing polygon {i} ({len(ring)} pts)...", end="", flush=True)
        inside = path.contains_points(points)
        count = inside.sum()
        land |= inside
        print(f" {count:,} cells inside")

    land_count = land.sum()
    print(f"  Land: {land_count:,} / Water: {total - land_count:,}")

    # Pack into bitfield
    bits = bytearray((total + 7) // 8)
    for idx in range(total):
        if land[idx]:
            bits[idx // 8] |= 1 << (idx % 8)

    encoded = base64.b64encode(bytes(bits)).decode("ascii")
    print(f"  Encoded size: {len(encoded):,} bytes")

    return {
        "south": SOUTH, "west": WEST, "north": NORTH, "east": EAST,
        "resolution": GRID_RES, "rows": rows, "cols": cols,
        "data": encoded,
    }


def validate_polygons(polygons):
    tests = [
        # Land
        (37.78, -122.41, True, "Downtown SF"),
        (37.60, -122.45, True, "Pacifica"),
        (37.50, -122.35, True, "SF Peninsula inland"),
        (37.40, -122.10, True, "South Bay inland"),
        (37.33, -122.03, True, "Santa Cruz Mtns"),
        (36.97, -122.03, True, "Santa Cruz city"),
        (37.10, -122.25, True, "Davenport coast"),
        (37.05, -122.20, True, "Santa Cruz Mtns coast"),
        (37.00, -122.15, True, "North Santa Cruz"),
        # Bay water
        (37.80, -122.35, False, "Central SF Bay"),
        (37.60, -122.20, False, "South Bay near RWC"),
        # Golden Gate
        (37.825, -122.475, False, "Golden Gate mid-channel"),
        # Pacific
        (37.70, -122.52, False, "Pacific off SF"),
        (37.50, -122.60, False, "Pacific off HMB"),
        (37.20, -122.45, False, "Pacific off Ano Nuevo"),
        (37.00, -122.50, False, "Pacific off SC"),
        (36.80, -121.95, False, "Monterey Bay north"),
        (36.65, -121.90, False, "Monterey Bay"),
    ]
    print("\nPolygon validation:")
    ok = True
    for lat, lon, expect, name in tests:
        is_land = any(_point_in_ring(lat, lon, p["outer"]) for p in polygons)
        status = "PASS" if is_land == expect else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"  [{status}] {name}: {'LAND' if is_land else 'WATER'} (expected {'LAND' if expect else 'WATER'})")
    return ok


def validate_grid(grid):
    tests = [
        # Land
        (37.78, -122.41, True, "Downtown SF"),
        (37.50, -122.30, True, "SF Peninsula inland"),
        (37.10, -122.25, True, "Davenport coast"),
        (37.05, -122.20, True, "Santa Cruz Mtns coast"),
        (37.00, -122.15, True, "North Santa Cruz"),
        (37.53, -122.36, True, "Crystal Springs Reservoir"),
        (37.47, -122.14, True, "Anderson Reservoir area"),
        (37.35, -122.08, True, "Santa Cruz Mtns interior"),
        # Water
        (37.80, -122.35, False, "Central SF Bay"),
        (37.60, -122.20, False, "South Bay near RWC"),
        (37.825, -122.475, False, "Golden Gate mid-channel"),
        (37.82, -122.48, False, "Golden Gate west"),
        (37.83, -122.47, False, "Golden Gate east"),
        (37.70, -122.52, False, "Pacific off SF"),
        (37.50, -122.60, False, "Pacific off HMB"),
        (37.40, -122.45, False, "Pacific off Pescadero"),
        (37.20, -122.45, False, "Pacific off Ano Nuevo"),
        (37.10, -122.40, False, "Pacific off Davenport"),
        (37.00, -122.25, False, "Pacific off Santa Cruz"),
        (36.65, -121.90, False, "Monterey Bay"),
    ]
    print("\nGrid validation:")
    bits = base64.b64decode(grid["data"])
    ok = True
    for lat, lon, expect, name in tests:
        r = int((lat - grid["south"]) / grid["resolution"])
        c = int((lon - grid["west"]) / grid["resolution"])
        idx = r * grid["cols"] + c
        is_land = (bits[idx >> 3] & (1 << (idx & 7))) != 0
        status = "PASS" if is_land == expect else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"  [{status}] {name}: grid={'LAND' if is_land else 'WATER'} "
              f"(expected {'LAND' if expect else 'WATER'})")
    return ok


def main():
    out_path = Path(__file__).resolve().parent.parent.parent / "static" / "data" / "land_mask.json"

    raw_polygons = fetch_gshhs()
    if not raw_polygons:
        print("ERROR: No GSHHS polygons found in bbox")
        return 1

    # Simplify
    total_before = sum(len(p) for p in raw_polygons)
    simplified = [_simplify(p, SIMPLIFY_TOLERANCE) for p in raw_polygons]
    simplified = [p for p in simplified if len(p) >= MIN_RING_POINTS]
    total_after = sum(len(p) for p in simplified)
    print(f"\nSimplified: {total_before} -> {total_after} points")

    # GSHHS Level 1 = land polygons (no holes needed — bay is outside the polygon)
    polygons = [{"outer": p, "holes": []} for p in simplified]

    passed = validate_polygons(polygons)

    grid = rasterize(polygons)
    if not validate_grid(grid):
        passed = False

    # Round and write
    for p in polygons:
        p["outer"] = [[round(a, 6), round(b, 6)] for a, b in p["outer"]]

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"polygons": polygons, "grid": grid}, f, separators=(",", ":"))

    size_kb = out_path.stat().st_size / 1024
    print(f"\nOutput: {out_path}")
    print(f"  {len(polygons)} polygons, {total_after} points, {size_kb:.0f} KB")
    print("\nAll passed!" if passed else "\nWARNING: Some checks failed!")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
