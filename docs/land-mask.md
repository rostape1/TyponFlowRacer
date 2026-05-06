# Land Mask — GSHHS Implementation

## Overview

The route optimizer needs to distinguish water from land to prevent routes crossing peninsulas. The land mask is stored in `static/data/land_mask.json` — a pre-computed binary grid for O(1) lookups. Generated from GSHHS (Global Self-consistent Hierarchical High-resolution Shoreline) coastline data.

## Data Source

| Dataset | Source | Purpose |
|---------|--------|---------|
| GSHHS Level 1 "h" | [GMT/GSHHG releases](https://github.com/GenericMappingTools/gshhg-gmt/releases) | Ocean/land boundary (high resolution, ~200m) |

GSHHS Level 1 polygons trace the actual coastline. The bay, Golden Gate, and Pacific coast are all correctly represented as water (outside the polygon). Inland features (reservoirs, lakes) are inside the polygon = land. No holes, flood-fill, or water body downloads needed.

## How It Works

1. **Load GSHHS** — Extract Level 1 "h" (high resolution) polygons from the shapefile that overlap our bounding box (lat 36.4–38.15, lon -123.1 to -121.6).

2. **Simplify** — Douglas-Peucker at 0.0005° (~55m) tolerance. GSHHS "h" vertices are already ~200m apart, so minimal reduction occurs.

3. **Rasterize** — All polygon cell-centers are tested using `matplotlib.path.Path.contains_points()` (vectorized C, processes 656K points in seconds). Inside polygon = land, outside = water.

## Output Format

```json
{
  "polygons": [],
  "grid": {
    "south": 36.4, "west": -123.1, "north": 38.15, "east": -121.6,
    "resolution": 0.002,
    "rows": 875, "cols": 750,
    "data": "<base64-encoded bitfield>"
  }
}
```

The `polygons` array is empty (grid-only mode). The router uses the grid for all land/water checks.

## Grid Specifications

| Property | Value |
|----------|-------|
| Resolution | 0.002° (~222m per cell) |
| Grid size | 875 × 750 = 656,250 cells |
| Golden Gate width | ~7 cells |
| Encoded size | ~109KB |
| File size | ~106KB |

## Key Design Decisions

- **GSHHS over TIGER** — TIGER state boundaries extend offshore (territorial waters) and TIGER areawater includes Pacific Ocean polygons that destroy the coastline when carved as holes. GSHHS traces the physical coastline precisely.
- **No flood-fill needed** — GSHHS polygons are self-consistent: bay water is outside the polygon (ocean-connected via GG), reservoirs are inside (land). Simple inside/outside test suffices.
- **Grid resolution 0.002°** — Golden Gate strait (~1.6km) gets ~7 cells across. Adequate for route safety.
- **matplotlib vectorized rasterization** — 656K point-in-polygon tests complete in seconds vs minutes for pure Python.
- **Grid-only output** — Polygons stripped from JSON to keep file at 106KB. The grid provides O(1) lookup; polygons were only a fallback.

## Regenerating

```bash
# Download GSHHS once (142MB zip, or use cached /tmp copy)
curl -L -o /tmp/gshhg-shp-2.3.7.zip \
  "https://github.com/GenericMappingTools/gshhg-gmt/releases/download/2.3.7/gshhg-shp-2.3.7.zip"

# Generate (requires matplotlib, numpy, requests)
python3 .github/scripts/generate_land_mask.py
```

Uses local `/tmp/gshhg-shp-2.3.7.zip` if present, otherwise downloads from GitHub.

## Validation Points

| Point | Expected | Location |
|-------|----------|----------|
| 37.78, -122.41 | Land | Downtown SF |
| 37.60, -122.45 | Land | Pacifica |
| 37.50, -122.35 | Land | SF Peninsula |
| 37.10, -122.25 | Land | Davenport coast |
| 37.80, -122.35 | Water | Central SF Bay |
| 37.60, -122.20 | Water | South Bay |
| 37.825, -122.475 | Water | Golden Gate |
| 37.70, -122.52 | Water | Pacific off SF |
| 37.50, -122.60 | Water | Pacific off HMB |
| 37.40, -122.45 | Water | Pacific off Pescadero |
| 36.65, -121.90 | Water | Monterey Bay |
| 37.53, -122.36 | Land | Crystal Springs Reservoir |
