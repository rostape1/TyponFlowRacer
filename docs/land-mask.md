# Land Mask — TIGER/Line Implementation

## Overview

The route optimizer needs to distinguish water from land to prevent routes crossing peninsulas. The land mask is stored in `static/data/land_mask.json` — a pre-computed binary grid for O(1) lookups, plus polygon fallback. Generated from US Census TIGER/Line data.

## Data Sources

| Dataset | URL Pattern | Purpose |
|---------|------------|---------|
| State boundary (500k) | `census.gov/.../cb_2022_us_state_500k.zip` | California mainland + island outlines |
| County areawater | `census.gov/.../tl_2022_{FIPS}_areawater.zip` | SF Bay, reservoirs, lakes carved as holes |

Counties fetched: San Francisco (06075), Alameda (06001), Marin (06041), San Mateo (06081), Contra Costa (06013), Santa Clara (06085), Santa Cruz (06087), Monterey (06053).

## How It Works

1. **Mainland polygon** — The largest ring from the California state boundary that overlaps the bounding box (lat 36.4–38.15, lon -123.1 to -121.6). This traces the actual coastline, so the Pacific Ocean and most of SF Bay are naturally outside the polygon (= water).

2. **Island polygons** — Smaller rings from the state boundary (Angel Island, Yerba Buena, Alcatraz, Farallon Islands) become separate polygons.

3. **Water body holes** — TIGER areawater polygons from 8 bay-area counties are overlaid. Those inside the mainland polygon become holes (inland water: South Bay, Richardson Bay, reservoirs, etc.).

4. **Simplification** — Douglas-Peucker at 0.0001° tolerance (~11m) reduces point count from ~72K to ~21K while preserving shape fidelity.

5. **Grid rasterization** — Polygons are rasterized into a 350×300 binary grid (0.005° ≈ 555m per cell) using scanline fill. The grid is base64-encoded (~17KB) and included in the JSON output. This enables O(1) land detection in the browser instead of O(n) polygon traversal.

## Why the Grid Matters

The California state boundary polygon has ~3,077 vertices. The route optimizer's isochrone engine calls `_isLand()` thousands of times per step (72 headings × 240 steps × buffer checks). Without the grid, each call traverses all 3,077 edges — billions of iterations total, freezing the browser. With the grid, each call is a single array index lookup.

## Output Format

```json
{
  "polygons": [...],
  "grid": {
    "south": 36.4, "west": -123.1, "north": 38.15, "east": -121.6,
    "resolution": 0.005,
    "rows": 350, "cols": 300,
    "data": "<base64-encoded bitfield>"
  }
}
```

The router (`static/js/router.js`) uses the grid when available, falling back to polygon point-in-polygon when outside the grid bounds.

## Key Design Decisions

- **State boundary, not coastline file** — The 500k cartographic boundary follows the coast closely enough that SF Bay and the Golden Gate strait are naturally outside the polygon. No manual water polygons needed for open water.
- **Per-county areawater** — Only needed for enclosed inland water (South Bay south of the Dumbarton Bridge, Richardson Bay, etc.) where the state boundary wraps around both shores.
- **Grid resolution 0.005° (555m)** — Matches the router's time step (~550m at sailing speed). Cell size provides natural ~275m buffer. No dilation needed.
- **Scanline rasterization** — O(rows × edges) instead of O(cells × edges), runs in seconds.
- **Bounding box crop** — Only keeps geometry within the coverage area (SF to Monterey), keeping the file at ~500KB.

## Regenerating

```bash
pip install requests
python3 .github/scripts/generate_land_mask.py
```

Downloads ~3MB from census.gov, runs 11 validation checks (6 land + 5 water points), rasterizes the grid, writes to `static/data/land_mask.json`. Exit code 0 = all passed, 1 = validation failure.

## Validation Points

| Point | Expected | Location |
|-------|----------|----------|
| 37.78, -122.41 | Land | Downtown SF |
| 37.60, -122.45 | Land | Pacifica |
| 37.50, -122.35 | Land | SF Peninsula |
| 37.40, -122.10 | Land | South Bay inland |
| 37.33, -122.03 | Land | Santa Cruz Mountains |
| 36.97, -122.03 | Land | Santa Cruz city |
| 37.80, -122.35 | Water | Central SF Bay |
| 37.50, -122.60 | Water | Pacific Ocean (HMB) |
| 37.00, -122.50 | Water | Pacific Ocean (SC) |
| 37.825, -122.475 | Water | Golden Gate strait |
| 36.62, -121.90 | Water | Monterey Bay |
