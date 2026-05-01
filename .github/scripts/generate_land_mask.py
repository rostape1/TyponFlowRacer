#!/usr/bin/env python3
"""
Generate land_mask.json from US Census TIGER/Line data.

Uses two TIGER datasets:
  1. State boundary (cb_2022_us_state_500k) — mainland California polygon
  2. Area water (tl_2022_XXXXX_areawater) — SF Bay and other water bodies as holes

Data source: US Census Bureau (public domain, no API key).

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
SIMPLIFY_TOLERANCE = 0.0001
MIN_RING_POINTS = 6

# Bay-area county FIPS codes for water body extraction
WATER_COUNTIES = [
    "06075",  # San Francisco
    "06001",  # Alameda
    "06041",  # Marin
    "06081",  # San Mateo
    "06013",  # Contra Costa
    "06085",  # Santa Clara
    "06087",  # Santa Cruz
    "06053",  # Monterey
]
# Minimum water body area (sq degrees) to include as a hole — filters out tiny ponds
MIN_WATER_AREA = 0.00001


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


def _download_shp(url, label):
    """Download a zip, extract .shp, parse it."""
    print(f"  Downloading {label}...", end="", flush=True)
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    shp_name = [n for n in z.namelist() if n.endswith(".shp")][0]
    records = _parse_shapefile(z.read(shp_name))
    print(f" {len(r.content)//1024}KB, {len(records)} records")
    return records


def _to_latlon(ring):
    """Convert (lon,lat) shapefile ring to (lat,lon) and ensure CCW."""
    converted = [(lat, lon) for lon, lat in ring]
    if _signed_area(converted) < 0:
        converted = list(reversed(converted))
    return converted


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
    """Check if (lat,lon) ring overlaps our bbox."""
    lats = [p[0] for p in ring]
    lons = [p[1] for p in ring]
    return max(lats) >= SOUTH and min(lats) <= NORTH and max(lons) >= WEST and min(lons) <= EAST


def _point_line_dist(p, a, b):
    dx, dy = b[1] - a[1], b[0] - a[0]
    if dx == 0 and dy == 0:
        return math.sqrt((p[0]-a[0])**2 + (p[1]-a[1])**2)
    t = max(0, min(1, ((p[1]-a[1])*dx + (p[0]-a[0])*dy) / (dx*dx + dy*dy)))
    return math.sqrt((p[0] - a[0] - t*dy)**2 + (p[1] - a[1] - t*dx)**2)


def _simplify(ring, tol):
    if len(ring) <= 4:
        return ring
    def dp(pts, s, e):
        if e - s < 2:
            return [pts[s], pts[e]]
        mx, mi = 0, s
        for i in range(s+1, e):
            d = _point_line_dist(pts[i], pts[s], pts[e])
            if d > mx:
                mx, mi = d, i
        if mx > tol:
            return dp(pts, s, mi)[:-1] + dp(pts, mi, e)
        return [pts[s], pts[e]]
    sys.setrecursionlimit(max(sys.getrecursionlimit(), len(ring)*2))
    r = dp(ring, 0, len(ring)-1)
    if len(r) >= 3 and ring[0] == ring[-1]:
        r[-1] = r[0]
    return r


def fetch_mainland():
    """Get California mainland polygon from TIGER state boundaries."""
    print("Step 1: Mainland polygon")
    url = "https://www2.census.gov/geo/tiger/GENZ2022/shp/cb_2022_us_state_500k.zip"
    records = _download_shp(url, "state boundaries (500k)")

    # Find all rings from records overlapping our bbox, convert to (lat,lon)
    all_rings = []
    for rec in records:
        for ring_raw in rec:
            ring = _to_latlon(ring_raw)
            if len(ring) >= 4:
                all_rings.append(ring)

    # Sort by area, keep only those overlapping bbox
    all_rings.sort(key=lambda r: abs(_signed_area(r)), reverse=True)
    in_bbox = [r for r in all_rings if _ring_in_bbox(r)]

    # The largest ring overlapping our bbox is the California mainland
    mainland = in_bbox[0] if in_bbox else None
    islands = in_bbox[1:]  # smaller rings = islands (Farallon, Yerba Buena, etc.)

    if mainland:
        lats = [p[0] for p in mainland]
        lons = [p[1] for p in mainland]
        print(f"  Mainland: {len(mainland)} pts, lat {min(lats):.2f}-{max(lats):.2f}")
        print(f"  Islands: {len(islands)}")

    return mainland, islands


def fetch_water_bodies():
    """Get water body polygons from TIGER areawater for bay-area counties."""
    print("\nStep 2: Water bodies (holes)")
    water_rings = []

    for fips in WATER_COUNTIES:
        url = f"https://www2.census.gov/geo/tiger/TIGER2022/AREAWATER/tl_2022_{fips}_areawater.zip"
        try:
            records = _download_shp(url, f"county {fips}")
        except Exception as e:
            print(f"  WARNING: Failed to download {fips}: {e}")
            continue

        for rec in records:
            for ring_raw in rec:
                ring = _to_latlon(ring_raw)
                area = abs(_signed_area(ring))
                if area < MIN_WATER_AREA:
                    continue
                if not _ring_in_bbox(ring):
                    continue
                water_rings.append(ring)

    print(f"  Total water bodies in bbox: {len(water_rings)}")
    return water_rings


def _is_land_polygon(lat, lon, polygons):
    """Check if a point is on land using polygon testing."""
    for p in polygons:
        if _point_in_ring(lat, lon, p["outer"]):
            if any(_point_in_ring(lat, lon, h) for h in p["holes"]):
                return False
            return True
    return False


GRID_RES = 0.005  # ~550m resolution
GRID_BUFFER_PX = 0  # grid cell size (550m) provides natural buffer

def _scanline_fill(ring, grid, rows, cols, south, west, res, value=True):
    """Fill grid cells inside a polygon ring using scanline rasterization."""
    n = len(ring)
    for r in range(rows):
        lat = south + (r + 0.5) * res
        # Find all x-intersections at this latitude
        intersections = []
        for i in range(n):
            j = (i + 1) % n
            y0, x0 = ring[i]
            y1, x1 = ring[j]
            if y0 == y1:
                continue
            if (y0 > lat) == (y1 > lat):
                continue
            x_int = x0 + (lat - y0) * (x1 - x0) / (y1 - y0)
            intersections.append(x_int)
        intersections.sort()
        # Fill between pairs
        for k in range(0, len(intersections) - 1, 2):
            c_start = max(0, int((intersections[k] - west) / res))
            c_end = min(cols, int((intersections[k + 1] - west) / res) + 1)
            for c in range(c_start, c_end):
                idx = r * cols + c
                if value:
                    grid[idx // 8] |= 1 << (idx % 8)
                else:
                    grid[idx // 8] &= ~(1 << (idx % 8))


def rasterize(polygons):
    """Pre-compute a binary land grid for O(1) lookups in the browser."""
    print("\nStep 3: Rasterizing land grid")
    rows = int((NORTH - SOUTH) / GRID_RES)
    cols = int((EAST - WEST) / GRID_RES)
    print(f"  Grid: {rows}x{cols} = {rows*cols:,} cells at {GRID_RES}° ({GRID_RES*111000:.0f}m)")

    bits = bytearray((rows * cols + 7) // 8)

    # Scanline fill each polygon
    for pi, p in enumerate(polygons):
        outer = p["outer"]
        if isinstance(outer[0], list):
            outer = [(pt[0], pt[1]) for pt in outer]
        _scanline_fill(outer, bits, rows, cols, SOUTH, WEST, GRID_RES, value=True)
        for hole in p["holes"]:
            h = hole
            if isinstance(h[0], list):
                h = [(pt[0], pt[1]) for pt in h]
            _scanline_fill(h, bits, rows, cols, SOUTH, WEST, GRID_RES, value=False)

    # Count before dilation
    land_before = sum(bin(b).count('1') for b in bits)

    # Dilate: expand land cells by GRID_BUFFER_PX pixels in all 4 directions
    if GRID_BUFFER_PX > 0:
        for _ in range(GRID_BUFFER_PX):
            orig = bytearray(bits)
            for r in range(rows):
                for c in range(cols):
                    idx = r * cols + c
                    if (orig[idx // 8] >> (idx % 8)) & 1:
                        for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                            nr, nc = r + dr, c + dc
                            if 0 <= nr < rows and 0 <= nc < cols:
                                nidx = nr * cols + nc
                                bits[nidx // 8] |= 1 << (nidx % 8)

    land_after = sum(bin(b).count('1') for b in bits)

    encoded = base64.b64encode(bytes(bits)).decode("ascii")
    print(f"  Land cells: {land_before:,} -> {land_after:,} (after {GRID_BUFFER_PX}px dilation)")
    print(f"  Encoded size: {len(encoded):,} bytes")

    return {
        "south": SOUTH, "west": WEST, "north": NORTH, "east": EAST,
        "resolution": GRID_RES, "rows": rows, "cols": cols,
        "data": encoded,
    }


def validate(polygons):
    tests = [
        (37.78, -122.41, True, "Downtown SF"),
        (37.60, -122.45, True, "Pacifica"),
        (37.50, -122.35, True, "SF Peninsula inland"),
        (37.40, -122.10, True, "South Bay inland"),
        (37.33, -122.03, True, "Santa Cruz Mtns"),
        (36.97, -122.03, True, "Santa Cruz city"),
        (37.80, -122.35, False, "Central SF Bay"),
        (37.50, -122.60, False, "Pacific Ocean off HMB"),
        (37.00, -122.50, False, "Pacific Ocean off SC"),
        (37.825, -122.475, False, "Golden Gate strait"),
        (36.62, -121.90, False, "Monterey Bay"),
    ]
    print("\nValidation:")
    ok = True
    for lat, lon, expect, name in tests:
        is_land = False
        for p in polygons:
            if _point_in_ring(lat, lon, p["outer"]):
                in_hole = any(_point_in_ring(lat, lon, h) for h in p["holes"])
                if not in_hole:
                    is_land = True
                    break
        status = "PASS" if is_land == expect else "FAIL"
        if status == "FAIL":
            ok = False
        print(f"  [{status}] {name}: {'LAND' if is_land else 'WATER'} (expected {'LAND' if expect else 'WATER'})")
    return ok


def main():
    out_path = Path(__file__).resolve().parent.parent.parent / "static" / "data" / "land_mask.json"

    mainland, islands = fetch_mainland()
    if not mainland:
        print("ERROR: No mainland polygon found")
        return 1

    water = fetch_water_bodies()

    # Filter water bodies: only keep those inside the mainland polygon
    holes = [w for w in water if _point_in_ring(w[0][0], w[0][1], mainland)]
    print(f"\n{len(holes)} water bodies inside mainland polygon")

    # Build output: mainland + holes, plus island polygons
    polygons = [{"outer": mainland, "holes": holes}]
    for island in islands:
        polygons.append({"outer": island, "holes": []})

    print(f"{len(polygons)} total polygons ({len(islands)} islands)")

    # Simplify
    total_before = sum(len(p["outer"]) + sum(len(h) for h in p["holes"]) for p in polygons)
    for p in polygons:
        p["outer"] = _simplify(p["outer"], SIMPLIFY_TOLERANCE)
        p["holes"] = [_simplify(h, SIMPLIFY_TOLERANCE) for h in p["holes"]]
        p["holes"] = [h for h in p["holes"] if len(h) >= MIN_RING_POINTS]
    polygons = [p for p in polygons if len(p["outer"]) >= MIN_RING_POINTS]
    total_after = sum(len(p["outer"]) + sum(len(h) for h in p["holes"]) for p in polygons)
    print(f"Simplified: {total_before} -> {total_after} points")

    passed = validate(polygons)

    # Rasterize for fast browser lookups
    grid = rasterize(polygons)

    # Round and write
    for p in polygons:
        p["outer"] = [[round(a, 6), round(b, 6)] for a, b in p["outer"]]
        p["holes"] = [[[round(a, 6), round(b, 6)] for a, b in h] for h in p["holes"]]

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
