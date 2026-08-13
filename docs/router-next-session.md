# Router work — next session

Continuation plan for the isochrone router rework started 2026-05-19. Background: see `~/.claude/projects/-Users-peterrostas-Projects-AIS-Tracker/memory/router_correctness_initiative.md` for what's been shipped.

## State on 2026-05-22 (later: sweep results)

Sweep below executed 2026-05-22 session-2. Routes 2 and 5–7 in the original
table had coordinates that landed **on land** (Berkeley city ≠ Berkeley
Marina; (37.86,-122.30) is solid East Bay shore). Re-pick using the
land-mask probe before treating any route as a "blocked" bug.

Corrected Berkeley→Sausalito: `--start=37.866,-122.32 --end=37.859,-122.485`
(7.8 nm rhumb). All four variants complete but ratios are bad:
baseline 1.61×, vmc **3.36×**, falloff **3.28×**, both 1.92×.
This confirms #1 below (over-detour) and surfaces a second bug:
**VMC variant is catastrophically broken on short-to-medium routes**
(produces 26 nm path for 7.8 nm rhumb). The "both" variant inherits it
on Route 6 (88 nm for 24 nm rhumb).

## State on 2026-05-22 (original)

Live (main + boat-mode):
- 48 h time budget, drift fallback for dead air, wind-over-water TWA, sub-hour grid alignment, tack penalty as speed reduction, monotonic VMC score, h/m progress UI.
- Headless test harness: `node tests/test_route.mjs`.

Known issues, in rough priority order:
1. **Routes still over-detour at ~1.43× rhumb** even with sailable wind throughout. Audit finding #2c (Pareto pruning) and #4 (per-bucket stepS) are the expected fixes but neither has shipped.
2. **Monterey Harbor approach blocks at 2.8 nm** across all four variants. Land-buffer geometry around Pt Pinos. **Accept as known limitation** per user direction (2026-05-22): "prioritize making sure the routing works correctly even if only to 2.8 nm from Monterey."
3. **Land mask check is Manhattan-cross** — diagonal gaps slip through. Audit #7.
4. **Polar duplicated** in `router.js` + `route-worker.js`. Audit #19.
5. **Repo carries 33 MB of accidentally-committed logs** in history at `8edf32a`. HEAD is clean. Force-push to remove pending user OK.

## Recommended next step

Before touching the engine again, **verify correctness on routes that already complete**. Sandbox blocked the Open-Meteo fetch in the last attempt — retry from a fresh session.

Suggested sweep (each line is one `node tests/test_route.mjs` invocation):

| # | Start | End | Why |
|---|---|---|---|
| 1 | `37.82,-122.45` | `37.82,-122.42` | Crissy → Alcatraz, 1.4 nm sanity |
| 2 | `37.86,-122.30` | `37.85,-122.49` | Berkeley → Sausalito, 5 nm in-bay |
| 3 | `37.81,-122.41` | `37.58,-122.25` | SF → San Mateo Bridge, 18 nm south bay |
| 4 | `37.82,-122.50` | `37.45,-122.55` | 1nm W of GG → Half Moon Bay offshore, 25 nm |
| 5 | `37.82,-122.50` | `37.40,-123.10` | 1nm W of GG → 30 nm SW open ocean |
| 6 | `37.82,-122.50` | `37.49,-122.80` | 1nm W of GG → W of Pillar Point, 35 nm |
| 7 | `37.82,-122.50` | `36.85,-122.10` | 1nm W of GG → N Monterey Bay offshore, 65 nm |

For each route, run all 4 variants. Check:
- Does it complete (no error)?
- Is ratio sane (< 1.5×)?
- Do variants differ in plausible ways (VMC slightly different than baseline)?
- Does avg kn match what the wind forecast suggests (no implausibly fast/slow boats)?

Any route where the engine produces something visibly wrong is the next bug to chase. The audit identified 15 more candidate issues — pick based on what the sweep surfaces.

## Then, if engine is correct on completing routes

Ship the audit's #2c (Pareto pruning) and #4 (per-bucket stepS) — both are documented in `router_correctness_initiative.md`. They are expected to reduce the 1.43× detour.

## Workflow reminders

- Ship in small PRs. Boat-mode → cherry-pick to main → GH Pages deploys in ~90 s.
- Never `git add -A`. Stage `static/js/route-worker.js static/js/router.js static/js/app.js tests/test_route.mjs static/index.html` by name.
- Run `tests/test_route.mjs` after every router change; don't wait on user screenshots.
- Confirm UI changes in browser only if there's a visual element.
