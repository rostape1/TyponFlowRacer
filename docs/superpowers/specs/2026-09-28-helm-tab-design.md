# Helm tab — design

**Status:** draft for review, 2026-09-28
**Mockup:** [2026-09-28-helm-tab-mockup.html](2026-09-28-helm-tab-mockup.html) (open in a browser;
`?layout=landscape&s=chop` etc. picks a state) · [all states](2026-09-28-helm-tab-states.png)

## 1. Purpose

A race display for the helm that answers one question at a glance: **steer up, steer down, or trim?**
It compares our actual apparent wind angle and boat speed with Typon's ORC targets and turns the
difference into one instruction, using the rules the log analysis supports (`docs/polar.md` §3, §8,
§9 and the crib sheet).

Success: a helm who has never read the crib sheet sails to it. The instruction never contradicts the
data (no footing for speed in breeze, no pinching when slow), and the display never shows a stale
number as if it were live.

**What was said** (this session): ORC is the target, not our history. Steer to the target AWA when
waves make target speed impossible. Show AWA, not TWA. Downwind too, down to broad reaches. Landscape
is fine. Palette with high contrast. Nothing changes during a race; recalibration happens after.
The helm steers by this display, not the B&G, so every number on it is corrected (§4).

**Assumed:** the helm uses a phone or iPad on the boat WiFi, mounted near the wheel.

## 2. Where it lives

A fourth tab, **Helm**, next to Map / Charts / Radar in `static/index.html`. That gives:

- the Pi's live NMEA feed (`/nmea` via `nmea-client.js` → `nmea-store.js`), already bootstrapped from
  `/config.json` in boat mode;
- replay for free: `play_logs.sh` on the Mac drives the same store, so the tab can be tested against
  the September races before it is trusted on the water;
- on GitHub Pages there is no NMEA, so the tab shows its NO DATA state (§7). That is expected.

Opened at `http://typonrpi4.local:8080/` (iPhone/iPad resolve `.local`; Android may need
`http://192.168.47.231:8080/`). The page is HTTP, so the Wake Lock API is unavailable: the iPad needs
Auto-Lock off. The tab remembers itself (localStorage) so a home-screen launch opens straight to it.

## 3. Layout

Landscape preferred, portrait supported, chosen by CSS on orientation. Landscape gives the dial ~40%
more size on a phone (it is a half circle, so its size follows the width).

- **Banner** (full width, top): instruction + subtitle. Colour = state (§6).
- **Half-dial** (left in landscape, middle in portrait):
  - upwind: 0–45° AWA each side, bow/wind at the top, tick per degree, labels every 10°;
  - downwind: 60–180° AWA each side, dead downwind at the top, labels every 20°;
  - the **target** is marked four ways so it reads at a glance: a white line across the arc, a large
    white triangle outside it, the **target number in a white pill** on the scale-number ring (the
    scale number it would cover is hidden), and a bright green **groove band** (±1.5° up, ±2° down,
    60% opacity). The **actual AWA bar**, in the banner colour, sits on the arc itself, clear of the
    number ring, and the pill is drawn on top so the target number is never covered. Port on the
    left, starboard on the right; markers move to whichever tack we are on. Beyond the range, the bar
    pins to the edge; the tile still shows the real number.
- **Speed bar** in the dial's core, 70 px wide, spanning target ±1 kn: white line = target (label on
  the right), fill = our speed (label on the left), dashed line = **min** (target − 0.3 kn, labelled
  "min 6.1" in the build-speed colour). The fill takes the banner colour: magenta only when the banner
  says build speed.
- **Tiles**: AWA (corr.) (value, target, "3° wide / high / hot / deep"), BSP (corr.) (value,
  target, ±gap). Corrected numbers throughout (§4); nothing is raw B&G.
- **Footer**: TWS (10 m) · TWA · VMG % of ORC · "tacked N s ago" when relevant.

No arrows on the dial: direction is carried by the banner alone.

## 4. Targets

All from Typon's ORC certificate (the same `Cert` arrays `build_polar.py` uses, 4–24 kn), linearly
interpolated at the live **calibrated TWS at 10 m**:

- upwind: beat angle (TWA) and beat speed (`beat_vmg / cos(beat_angle)`);
- downwind: gybe angle and run speed; ORC run VMG for the VMG %.

**This tab is the instrument the helm steers by, and every number on it is corrected.** The B&G
repeater's raw readings are not used or matched: they are wrong (vane offset, upwash, heel, the
paddlewheel), and showing them would mislead.

- **Actual AWA** = the corrected apparent wind angle by the bow: heel-corrected, masthead offset and
  upwash removed (steps 3 and 5 of §5, stopping before the conversion to true wind). It reads the
  same on both tacks for the same sailing, unlike the B&G, which reads ~2 × the masthead offset apart.
- **Target AWA** is computed at the speed we are actually doing, so "steer 26°" always means ORC's
  true angle: ORC's TWA target (through the water) + masthead TWS + *actual* STW → apparent wind
  through the water → minus leeway → apparent angle by the bow, in the same corrected frame as the
  actual AWA. This is the first half of `display_awa()` in `build_polar.py` (before its "undo our
  corrections" step), generalised to take actual STW and to cover downwind (leeway tapers to zero
  beyond 90° AWA, as in `leeway()`).
- **BSP** shown is the corrected speed through the water (STW, step 2), which is what ORC's speeds
  mean.

Consequence: the tab will disagree with the B&G by a few degrees and a few tenths of a knot, and by a
different amount on each tack. That is intended; the tile labels say "AWA (corr.)" and "BSP (corr.)"
so nobody cross-checks one against the other by mistake.

## 5. Live calibration chain

`nmea-store.js` holds raw values (`awa`, `aws`, `bsp`, `heel`) and an *uncalibrated* true wind. The
Helm tab does not use the store's `twa`/`tws`; it runs the same chain as `build_polar.calibrate()`,
in the same order, on each update:

1. `paddle_starboard(bsp, heel)` — the heeled-to-port over-response (pivot 5.4 kn, slope 0.72);
2. × paddlewheel scale `k` → STW;
3. `heel_correct(awa, aws, heel)`;
4. `leeway(heel, stw, awa, K)`;
5. `true_wind()` with the masthead offset and `upwash_at(tws10)` (two passes, as in `calibrate()`:
   a constant-upwash pass for TWS, then the wind-dependent upwash);
6. TWS × `TWS_TO_10M`.

Compass deviation is not needed (TWA does not use heading). No heel reading → no leeway or heel
correction is possible → the tab says so (§7) rather than using uncorrected numbers.

**Constants are frozen during a race.** `build_polar.py --write-js` writes them, together with the
ORC arrays, into one generated file, `static/js/helm-targets.js`, exactly as it already writes the
router's polar table (between marker comments, never hand-edited). Recalibration = rerun
`build_polar.py --write-js` after a race or the calibration session, review the printed constants,
commit, deploy. Nothing adapts on its own.

**Paddlewheel scale:** `fit_paddlewheel` fits `k` per day. The generator writes the **median of the
per-day fits** (currently the four BBS race days: 1.024, range 1.021–1.048) and prints them; a single
live number is required.

**Heel sign:** the store's `heel` comes from the same `YXXDR` roll field `build_polar.py` parses
(negative = heeled to port). A test asserts the convention on a real log line.

## 6. Instruction rules

Evaluated on smoothed values (§7). `dS` = STW − target speed; `dA` = AWA − target AWA (+ = wider
upwind / deeper downwind); FLOOR = 0.3 kn. Mode: upwind when TWA < 90°, downwind otherwise, with
±10° hysteresis (switch to downwind above 100°, back below 80°).

Colours (palette C): **cyan** `#3fe0f5` = spend speed, **magenta** `#ff4fd8` = build speed,
**green** `#35d49a` = groove, **grey** = no data. Dark text on all banners.

### Upwind

| # | Condition | Banner | Subtitle |
|---|---|---|---|
| 1 | dS < −FLOOR, within 60 s of a tack or rounding | ▼ **BEAR OFF** | build to *target* kn |
| 2 | dS < −FLOOR, dA < −1.5 (pinching) | ▼ **BEAR OFF** | to *target AWA*° |
| 3 | dS < −FLOOR otherwise | **BUILD SPEED** | hold angle · trim for speed (13+ kn: hold angle · flatten · power through chop) |
| 4 | dS ≥ 0 and dA > 1.5 | ▲ **POINT HIGHER** | down to *target* kn |
| 5 | otherwise | **IN THE GROOVE** | hold it (13+ kn: stay flat · feather gusts) |

Why: pointing higher paid while speed fell by less than ~0.3 kn; footing lost; outside the Gate we
were 4° wide and still slow; chop costs speed, not angle (docs/polar.md §3, §8; `upwind_insights`
`gate`). Rule 1 is the exception the data does not argue against: accelerating out of a tack (light
air tacks took 45–100 s to rebuild). Rule 3 does not ask for height while slow (crib rule 7, build
speed before pointing).

### Downwind

| # | Condition | Banner | Subtitle |
|---|---|---|---|
| 1 | dS < −FLOOR **and** VMG < 100% of ORC | ◀/▶ **HEAT UP** | build to *target* kn |
| 2 | dS ≥ 0 and dA < −2 (hot) | ◀/▶ **SAIL DEEPER** | down to *target* kn |
| 3 | VMG ≥ 100%, TWS ≥ 10, dS ≥ −FLOOR | **IN THE GROOVE** | try a degree deeper · watch VMG |
| 4 | otherwise | **IN THE GROOVE** | hold it |

The sideways arrow is the way to turn the bow, which is also the way the bar moves on the dial:
starboard gybe, deeper = turn to port = ◀; placed on the side it points. Why: going deeper added VMG
in 10+ kn (0.01–0.05 kn per degree, partly puffs) and lost it in 6–10 kn, where we already sail
deeper than ORC; slow-but-deep with good VMG is fine. **Provisional:** 70 minutes of downwind data,
and the 0.3 kn floor was only measured upwind.

### Reach

Replay of R5 and R6 showed reach legs (100° TWA) being judged against the run's gybe angle ("sail
deeper by 77°"). The tab cannot tell a reach leg from a broad run without the mark, so: when TWA is
more than **25°** from the current target angle (beat angle upwind, gybe angle downwind) it is a
**REACH**, and it stays one until back within **20°**. On a reach there is no angle advice and no
target on the dial; the speed bar and BSP tile compare with **ORC's polar speed at the angle
sailed** (bilinear on the router's table, `polar_speed()` in `build_polar.py`), and the footer shows
% of polar instead of VMG. Banner: **REACH** · polar *X* kn, green at or above polar − 0.3 kn,
magenta below, never an arrow. Entering or leaving a reach counts as a rounding (§ below).

### Tack / rounding detection

A tack, gybe or rounding = the sign of smoothed AWA flips and holds for 10 s, or the mode switches.
The 60 s window runs from that moment.

## 7. Smoothing, staleness and failure

- **Smoothing:** AWA and STW over 5 s; VMG over 15 s (it jumps with waves and puffs). A new banner
  state must hold 3 s before it replaces the current one. All four numbers are named constants in one
  place, to tune after the first race.
- **Clock:** ages use the stream's own time (the store's ingest timestamp): wall time live, log time
  in replay. Never `Date.now()` against replayed data (cf. `P33`).
- **Fail visibly** — grey banner, the affected number blanked, no VMG, never the last good value:
  - NO NMEA — no connection / nothing received;
  - NO WIND — AWA or AWS older than 12 s (as built: the instruments send wind every ~4.7 s, max
    5.2, so 5 s flickered);
  - NO SPEED — BSP older than 8 s (sent every ~2.2 s, max 2.6);
  - NO HEEL — heel older than 5 s (20 Hz; calibration impossible without it, §5);
  - TWS below 4 kn — "TOO LIGHT", no targets: the certificate's table covers 4–24 kn, and below it
    would be extrapolation. Above 24 kn the 24 kn row is used and the footer says "above ORC range".

## 8. Files

| File | Change |
|---|---|
| `static/js/helm-logic.js` | **new.** Pure functions, no DOM: calibration chain, target lookup, `displayAwa`, `decide()`, smoothing, tack detection. Testable in node |
| `static/js/helm.js` | **new.** The view: SVG dial, speed bar, tiles, banner; subscribes to the store's `update` |
| `static/js/helm-targets.js` | **new, generated** by `build_polar.py --write-js`: ORC arrays + calibration constants |
| `static/index.html` | Helm tab button + view container; script tags versioned like the others (`P41`) |
| `static/css/style.css` | Helm layout, landscape/portrait, palette C |
| `static/sw.js` | **no change**: like the other NMEA modules the helm files are not precached (the tab needs the Pi's feed anyway), so they cannot trip `P43` |
| `tools/build_polar.py` | `--write-js` also writes `helm-targets.js` and a parity fixture (§9) |
| `tests/test_helm.mjs` | **new**, added to `deploy.yml` |
| `docs/helm.md` | **new** topic doc; row in CLAUDE.md's file map, tests table and documentation map |

The Charts tab's gauges keep the store's uncalibrated true wind; changing them is out of scope.

## 9. Testing

- **`tests/test_helm.mjs`** (node sandbox, like `test_replay.mjs`):
  - every row of both rule tables, and their boundaries (dS exactly −0.3 and 0; dA exactly ±1.5 / −2);
  - the 60 s post-tack window opens and closes; hysteresis at 80/100°;
  - each failure state blanks its numbers; stale data never renders as live;
  - banner hold: a one-sample flicker does not change the banner.
- **Python ↔ JS parity** (the `P20` trap: two copies of the calibration). `build_polar.py --write-js`
  also writes `tests/fixtures/helm_parity.json`: a grid of raw inputs (AWA, AWS, BSP, heel, both
  tacks, up and down) with the Python outputs (STW, corrected AWA, TWA, TWS10, target AWA).
  `test_helm.mjs` asserts the JS matches within 0.05° / 0.01 kn. It also asserts that the same
  sailing on port and starboard gives the same corrected AWA (the offset really is removed).
- **Mutation-test** `test_helm.mjs` once (flip a sign in the leeway, drop the hysteresis) — per
  CLAUDE.md.
- **Replay:** play the BBS 2026 races on the Mac (`play_logs.sh`) and look at the tab through each leg;
  spot-check the banner against the race reviews' leg notes.
- **Not verifiable from the desk:** readability in sun, the iPad mount, the Pi at sea. Checked by the
  crew on the water.

## 10. Out of scope

- Recalibrating during a race (deliberately: §5).
- Target speed from our history (the choice was ORC).
- Reaching-specific targets; start-line and layline features.
- A day/high-contrast theme beyond palette C (small follow-up if sunlight demands it).
- Calibrating the Charts tab's true wind.

## 11. Open items to settle on the water

- Smoothing windows and the 3 s banner hold.
- Whether the downwind floor and "try a degree deeper" hold up.
- The actual AWA range on broad reaches with the kite (the dial covers 60–180°).
