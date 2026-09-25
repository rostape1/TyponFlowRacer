# Route optimizer

Isochrone route search for Typon across the SFBOFS current field and the Open-Meteo wind grid.
`computeRoute()` in `static/js/router.js` orchestrates; the search itself runs in
`static/js/route-worker.js` (a Web Worker, off the main thread).

Relevant pitfalls: `P27` `P28` `P29` `P30` `P31` — fetch by ID from [pitfalls.md](pitfalls.md).
Land/water detection: [land-mask.md](land-mask.md).

**Verify router changes with `node tests/test_route.mjs`, not screenshots.** A full four-variant
sweep is ~13s and prints ETA / distance / avg speed / ratio per variant.

---

## Data loading

`RouterDataStore` pre-loads up to 49 hours of SFBOFS current and Open-Meteo wind grids, with
temporal interpolation between the hourly grids. Sub-hour forecast offsets are passed to the worker
as `gridOffsetH` so grid lookups align to the actual start clock instead of flooring to the hour.

The store consumes `data-loader.js` (`getWindGridForHour()`, `getSfbofsRunTime()`), which means it
inherits the SFBOFS staleness gate (`P01`) — a stalled pipeline makes the router refuse rather than
plan against a frozen field. The high-res Golden Gate path is subject to the same gate.

---

## The search

| Parameter | Value |
|---|---|
| Time budget | `MAX_TIME_S = 172800` (48h) |
| Headings | 72, or 144 inside the Golden Gate high-resolution zone |
| Timestep | 60-300s, chosen by whether any wavefront point is in the HR zone / near land / open water |
| Angular pruning | 180 sectors (2° each) |
| Destination approach | `DEST_APPROACH_NM = 1` |

### Wind frame

TWA is computed against **wind-over-water** (`wind − current`), not wind-over-ground. The polar
describes what the sails feel. In a 3kn Golden Gate ebb the two differ enough to produce a boat speed
the boat cannot achieve, and then a route that depends on it (`P27`).

### Tack penalty

60s for a heading change > 60°, 20s for > 30° — applied as a **speed reduction during the step**,
never as added time. The wavefront is an equal-time contour; adding time to one branch puts it on a
different clock from its siblings and pruning then compares points that aren't comparable (`P28`).

### Drift fallback

If a point has wind < 0.5kn, or every heading from it hits land, a single **drift point** is pushed
at the same wall-clock advance, displaced only by the current. Without this the wavefront could
silently empty and report "no reachable path" in open water (`P29`).

### Pruning

Baseline keeps the best point by `distFromStart` per angular sector. The `vmc` variant biases by
`dist * (1 + 0.5·cos(brg − destBrg))`, bounded to 0.5–1.5× dist so the score stays **strictly
monotonic in dist** (`P30`). `bestToDest` is always carried forward regardless of pruning.

### Destination approach

Within `DEST_APPROACH_NM` of the destination the 200m land buffer is dropped and strict `_isLand` is
used, so harbors and shoreline waypoints are reachable. Monterey harbor blocked at 2.8nm before this
(`P31`).

---

## Polars and variants

**Typon's ORC International certificate** ([polar.md](polar.md)), expanded by
`tools/build_polar.py --write-js` into both `router.js` and `route-worker.js`. TWA 30–180°, TWS 4–24 kn.
Best VMG sits at the cert's beat and gybe angles (asserted in `tests/test_physics.mjs`).
Performance factor default 95%: race sailing made a median 96% of rated VMG, measured before the
leeway correction. Corrected for leeway and wind height, it is 88–93% VMG upwind (88% in 13+ kn) and 94–100%
downwind; the default has not
been revisited ([polar.md](polar.md) §1). It replaced a generic
Swan 47 × 85% that treated anything under 52° as unsailable.

| Variant | Behavior |
|---|---|
| `baseline` | Distance-from-start pruning, flat performance factor |
| `vmc` | Velocity-made-good-to-course biased pruning |
| `falloff` | Sets the worker's `polarFalloff` flag: degrades beyond the table's last TWA. The ORC table reaches 180°, so it is currently a no-op |
| `both` | `vmc` + `falloff` |

Selected from a dropdown in the route-planner panel; changing it auto-recomputes. The variant string
is resolved to worker flags in `route-worker.js` (`polarFalloff = variant === 'falloff' || variant === 'both'`).

---

## Rendering

`RouteRenderer` draws a coloured Leaflet polyline, segment-coloured by current benefit: green
(favorable > 0.3kn), red (adverse > 0.3kn), orange (neutral). Time labels at a dynamic interval —
15min / 30min / 1h / 2h depending on total duration. Arrival times clock off the **route start**, not
`now`.

The details table (via button) shows BSP, TWS, TWA, AWS and AWA per waypoint, colour-coded by point
of sail.

---

## Known open work

- Pareto pruning and per-bucket `stepS` are specced but not built.
- No automated assertion that a route is *optimal* — `test_route.mjs` prints metrics for comparison
  between variants and runs, and regressions are caught by eye on those numbers.
