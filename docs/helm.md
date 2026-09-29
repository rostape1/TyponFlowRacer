# Helm tab

The race steering display: one instruction for the helm — steer up, steer down, or trim — from
corrected apparent wind angle and boat speed against Typon's ORC targets. Design history and the
reasoning behind each choice: [superpowers/specs/2026-09-28-helm-tab-design.md](superpowers/specs/2026-09-28-helm-tab-design.md).

## Opening it

- On the boat WiFi: `http://typonrpi4.local:8080/#helm` (Android may need `http://192.168.47.231:8080/#helm`).
  The tab reopens by itself if it was the last one used, so a home-screen icon lands on it.
- The page is HTTP, so it cannot keep the screen awake: turn **Auto-Lock off** on the iPad.
- Landscape is preferred (the half-dial is larger); portrait works.
- On the Mac: `./play_logs.sh`, open `http://localhost:8080/#helm`, pick a recording in playback.
- On GitHub Pages there is no NMEA feed, so it shows NO NMEA. Expected.

## Reading the dial

Two marks, told apart by shape and place rather than colour: the **boat** is a solid wedge inside
the ring, coloured like the banner; the **target** is the same wedge, hollow and white, outside it.
They are tall and narrow and meet tip to tip on the ring, so each reads as pointing along its radius
(the first build had squat wedges that read as sideways triangles). A wide translucent white band on the ring
is the groove (±1.5° up, ±2° down). Steer the solid tip under the hollow one. Reaching there is no
target, only the boat wedge.

**The big numbers are the differences, not the readings.** The helm steers on "how far off", so the
angle difference sits under the dial (`2° HIGH` / `3° LOW` upwind, `3° DEEP` / `2° HOT` downwind,
`ON ANGLE`; none while reaching) and the
speed difference inside it, above the speed bar (`+0.2 kn`). The raw corrected readings and their
targets are in the tiles, with no differences repeated there.

**One colour at a time.** The only colour on screen is the banner's, and it goes only on what the
instruction is about: the boat wedge always; the angle difference on SAIL DEEPER / POINT HIGHER
(cyan); the speed difference and bar on BUILD / HEAT UP / BEAR OFF (magenta); everything in the
groove (green). All else is white or grey: target wedge, groove band, floor tick, PORT/STBD, tile
headers. An earlier build had about eight colours with green meaning four different things.

The speed bar shows the boat speed itself, filling up from the bottom, zoomed to a 2 kn window
(target ±1 kn, clamped at the ends) so a tenth of a knot is visible. The white line mid-height is the
target, labelled on the right; the dashed line is the **floor**: boat speed at target − 0.3 kn, the
crib sheet's "speed floor" row, labelled on the left. Above it, pointing high still pays in VMG;
below it, speed is the problem ([polar.md](polar.md) §3). The speed difference is written under the
bar. (It was briefly a centre-zero bar of the difference; up-and-down for faster-and-slower read
better.)

Scale numbers sit outside the ring every 10° upwind (0–45° AWA) and every 20° downwind (60–180°),
bold, and are hidden under the target wedge. Ticks every 5°, long every 10°; finer ticks were a grey
comb in glare. Only the current tack's PORT/STBD label is shown, on its side.

## Every number is corrected

The helm steers by this display, not the B&G. Raw readings carry the vane offset, upwash, heel and
paddlewheel errors, so the tab runs `build_polar.py`'s calibration chain live on the raw NMEA
(paddlewheel heel response and scale → heel correction → leeway → offset and upwash → 10 m wind)
and shows **AWA (corr.)** and **BSP (corr.)**. They will differ from the B&G by a few degrees and a
few tenths, differently on each tack. That is the point.

The target AWA is ORC's true angle converted at the speed actually sailed, minus leeway, in the same
corrected frame: slower means the target reads a little wider.

## The instruction

`dS` = speed − target speed, `dA` = AWA − target AWA (+ = low upwind / deep downwind), floor 0.3 kn.
Upwind under 90° TWA, downwind above, with hysteresis (switch at 100°, back at 80°).

**Upwind** — speed first; never foot below the target angle when slow.

| Situation | Banner |
|---|---|
| more than 0.3 kn slow, within 60 s of a tack or rounding | ▼ BEAR OFF |
| more than 0.3 kn slow, more than 1.5° high (pinching) | ▼ BEAR OFF |
| more than 0.3 kn slow otherwise | BUILD SPEED |
| at or above target speed, more than 1.5° low | ▲ POINT HIGHER |
| not slow, more than 1.5° high, VMG under ORC | ▼ BEAR OFF |
| otherwise (incl. high with VMG at or over ORC) | IN THE GROOVE |

**Downwind** — slow only matters if VMG suffers too.

| Situation | Banner |
|---|---|
| more than 0.3 kn slow and VMG under ORC | HEAT UP |
| at or above target speed, more than 2° hot | SAIL DEEPER |
| otherwise | IN THE GROOVE |

The downwind arrow is the way to turn the bow (◀ on the left of the text, ▶ on the right):
starboard gybe, deeper = turn to port = ◀.

**Reach** — TWA more than 25° from the target angle (back under 20°): no angle advice, speed against
ORC's polar at the angle sailed. The tab cannot see the mark, so it cannot tell a reach leg from a
broad run any other way.
Entering or leaving REACH must also hold 3 s, since it takes the target off the dial and puts it
back. While reaching, the dial scale follows the AWA rather than the mode (downwind scale above 55°,
upwind back under 50°): the upwind scale ends at 45°, and a close reach in upwind mode sat pinned
past its end. AWA 45–60° is on neither scale and still pins at the edge.

High while holding speed is kept only while VMG is at or over ORC: §3 measured 2–4° high gaining and
says nothing past that, and speed can bleed off over minutes before the 0.3 kn floor notices. An
earlier rule held any amount of high at speed, and showed "7° HIGH" under IN THE GROOVE. In the groove
the angle difference is green only inside the groove band, white outside it.

Evidence for each rule: [polar.md](polar.md) §3 (pointing pays while speed holds; footing loses),
§8 (chop costs speed, not angle), §9. §3 also found going deeper paid in 10+ kn; the groove used to
add "try a degree deeper · watch VMG" there, but shown on every good run it was noise to the helm, so
the groove banner has no second line at all.

**VMG as % of ORC sits at the right end of the banner** (`92% VMG`; `101% polar` while reaching),
in the banner's text colour: the outcome next to the instruction, and the number the high-at-speed
rule tests. In portrait it takes its own line under the instruction. Blank in the no-data states.

**The banner is one instruction, never a second line.** Earlier builds added trim and target advice
under it ("hold angle · trim for speed", "flatten · power through chop", "down to 6.4 kn", "to 26°").
Repeated every few seconds it was noise, and every number in it is already on the dial. Only the
grey NO-DATA states keep a reason line (e.g. "no wind for 12 s").

## Failing visibly

Grey banner, numbers blanked, never the last good value: **NO NMEA** (nothing, or nothing for 5 s,
checked against the clock rather than the last sentence), **NO WIND** (12 s), **NO SPEED** (8 s),
**NO HEEL** (5 s; without heel the wind angle cannot be corrected). The limits are per input because
the instruments send at very different rates (measured in the BBS logs): apparent wind every ~4.7 s
(max 5.2), boat speed every ~2.2 s (max 2.6), heel at 20 Hz. A single 5 s limit flickered NO WIND
on 44% of wind gaps. **TOO LIGHT** (under 4 kn, the certificate's first
column). Ages use the stream clock: wall time live, log time in replay, so a paused replay is not
stale.

Smoothing 5 s (angle, speed, wind), 15 s (VMG); a new instruction must hold 3 s before the banner
changes, except when REACH or the mode switches: those redraw the whole dial, so the banner switches in
the same update instead of lagging the dial by 3 s. The banner colour fades over 0.3 s. There are no
other transitions: at 10× replay a state change every few seconds is the data, not a render glitch. All tunable in `HelmLogic.C` (`static/js/helm-logic.js`).

## Recalibrating after a race

Nothing adapts during a race. Afterwards, with the new logs mirrored to the Mac:

```bash
uv run --with numpy --with pandas --with matplotlib --with scipy \
    python tools/build_polar.py --write-js      # re-parses; add --cache to reuse the grid
node tests/test_helm.mjs
```

`--write-js` rewrites `static/js/helm-targets.js` (the ORC table + frozen calibration) and
`tests/fixtures/helm_parity.json`. Before writing, it checks that `helm_chain()` reproduces
`calibrate()` on the whole log grid and exits if not. Review the printed constants (paddlewheel
scale per day, offset, upwash, leeway), commit, deploy. The paddlewheel scale is the median of the
per-day fits.

## Files and tests

| File | Role |
|---|---|
| `static/js/helm-targets.js` | **Generated.** ORC certificate, polar table, calibration constants |
| `static/js/helm-logic.js` | Pure logic, no DOM: correction chain, targets, rules, smoothing, tack/reach detection |
| `static/js/helm.js` | The view (SVG dial, speed bar, tiles, banner) |
| `static/js/nmea-store.js` | Keeps signed `roll` and per-input timestamps (`awaAt`, `bspAt`, `rollAt`) for the tab |
| `tools/build_polar.py` | `helm_chain()`, `awa_by_bow()`, `polar_speed()`, `write_helm_js()` |
| `tests/test_helm.mjs` | Python parity (chain, target, polar), every rule and boundary, the engine, heel sign on a real log line. Mutation-tested. In CI |

The calibration exists twice, in Python and JS (the `P20` trap); the parity fixture is what holds
them together.

## Known limitations

- **The wind angle updates only every ~5 s.** That is the rate the instruments put `$IIMWV` on the
  NMEA feed, so the banner and dial cannot react to a wind change faster. If the B&G can be set to
  output apparent wind more often, the display gets proportionally quicker; that is an instrument
  setting, not the Pi.
- **Old iPads.** The helm files stay at Safari 12 syntax (iPads stuck at iOS 12.5: Air 1, mini 2/3):
  no class static fields, `?.`, `??` or `replaceChildren`, and a plain `font-size` before every
  `clamp()`. `tests/test_helm.mjs` guards it. If the code still fails to load, the tab says HELM
  UNAVAILABLE instead of staying empty. Not tested on a physical iOS 12 device.

- **Port vs starboard.** Under 13 kn port reads a few degrees wider than starboard (probably
  paddlewheel or vane; [polar.md](polar.md) §2). The tab inherits it: port tends to show "low",
  starboard "high". The on-water calibration session is the fix.
- **Upwash taper.** `true_wind()` tapers the upwash by the *raw* vane angle, offset included, so the
  taper starts ~2.75° earlier on starboard and the tacks differ by up to ~0.5° at 26–35° AWA. Same
  in the analysis; changing it means re-running the analysis.
- **Downwind evidence is thin** (70 minutes), and the 0.3 kn floor was measured upwind only.
- **Replay seeks reset tack detection**, so jumping into the middle of a tack does not open the
  post-tack window. Continuous play and live sailing are unaffected.
