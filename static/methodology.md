# How the numbers are made

Everything on this page comes from Typon's own instrument logs, recorded on the boat's Raspberry
Pi, plus the AIS positions of other boats that our VHF receiver heard. Nothing comes from a race
tracker. This page explains how the raw readings become "% of ORC", target angles, leeway and the
race reviews, and how far to trust each.

## The raw data

- **What is logged:** GPS position, speed and course over ground (1 per second). Compass heading,
  heel and pitch (10–20 per second). Paddlewheel boat speed. Masthead apparent wind angle and
  speed (updated about every 5 s). Rudder angle. Depth. AIS reports from other boats.
- **When it counts:** only race time, from the start to the finish in the official results.
  Pre-start circling, motoring and deliveries are left out.
- **Clock:** log times come from GPS, not from the Pi's own clock, which had no battery and was
  sometimes wrong by days.

## Calibration: correcting the instruments

The instruments are refitted from the race data every time the analysis runs. Nothing is typed in
by hand. The corrections, in the order they are applied:

1. **Compass deviation.** In stretches sailed nearly upright (heel under 4°, so no leeway), the
   compass heading should match the GPS track once current is taken out. The difference, fitted as
   a smooth curve by heading, is the deviation: about −4° on our usual starboard beat heading and
   +1° on port. Uncorrected, it would look like 2–3° of leeway on each tack.
2. **Paddlewheel.** Boat speed through the water is compared with GPS speed once current is
   removed. The log reads 0–5% low, and the factor is fitted per day (it changes with fouling).
3. **Masthead wind angle and heel.** A heeled vane sees the wind in a tilted plane and reads a
   little narrow, about 1° at 25° of heel. Corrected per reading from the heel sensor.
4. **Leeway** (below): fitted next, because the wind correction depends on it.
5. **Masthead angle offset and upwash.** The vane is not perfectly aligned (+3.5°), and the sails
   bend the wind at the masthead (about 4° upwash). The test: the true wind direction should not
   jump when we tack. Both numbers are chosen so that over 72 tacks it does not (residual 0.1°).
   The instrument display on the boat is still uncorrected: upwind it reads ~7° wide on starboard
   and ~2° narrow on port.
6. **Wind height.** ORC's wind speeds are for 10 m above the water. Our masthead is about 20 m up,
   where the wind is stronger, so the masthead speed is multiplied by 0.925. Without this, every
   ORC target would be for too much wind.
7. **Rudder zero:** +1.7°.

## How the metrics are calculated

### True wind

From apparent wind, boat speed and heading, in the standard way, using the corrected readings
above. Two results:

- **True wind angle (TWA), through the water:** the angle between the wind and the direction the
  boat is actually moving through the water, so leeway is included. That is how ORC defines its
  angles. The angle by the bow is TWA minus leeway.
- **True wind speed (TWS):** at 10 m height, ORC's reference. The masthead display reads about
  8% more.

### Leeway

Leeway is the sideways slip: the difference between where the bow points and where the boat goes
through the water. It is measured from compass heading against the GPS track, in 46 upwind
stretches that include both tacks. Using both tacks separates leeway (which flips side with the
tack) from current (which does not). The fit:

**leeway = 2.75° × (heel / 20°)^2.25 × (6.5 kn / boat speed)²**

So leeway grows faster than heel, and more at low speed. Measured directly per heel range:

| Heel | under 20° | 20–23° | 23–26° | 26–28° | 28–30° | 30–34° |
|---|---|---|---|---|---|---|
| Leeway | 1.5° | 3.0° | 4.5° | 5.0° | 6.0° | 7.25° |

Each extra degree of heel adds about 0.33° of leeway at 20°, 0.43° at 25° and 0.54° at 30°. That
is why the crib sheet says to keep heel at 20–25°: past that, the extra heel buys very little
speed and costs pointing.

### Current

Current = GPS velocity minus velocity through the water (heading, leeway and boat speed), averaged
over 5 minutes. It comes from the same sensors, so it is an estimate, not a separate measurement.

### VMG and "% of ORC"

The benchmark is **Typon's ORC certificate**: its target speeds and angles for each wind speed.
Each moment is scored by the point of sail:

| True wind angle | Scored as | ORC target shown |
|---|---|---|
| up to 55° (beating) | VMG to windward ÷ ORC's best beat VMG | beat angle and target speed |
| 130° and more (running) | VMG downwind ÷ ORC's best run VMG | gybe angle and target speed |
| in between (reaching) | boat speed ÷ ORC's speed at the angle sailed | speed only: the course sets the angle |

VMG = boat speed × cos(TWA). So a beat can lose to ORC by pointing too low ("footing"), too high
("pinching") or by being slow at the right angle. The race box shows angle and speed against
target separately for that reason.

- **Track colours:** red at 85% of ORC and below, yellow at 91%, green at 97% and above. Grey is the
  turn of a tack or gybe itself (5 s before to 15 s after), where a VMG means nothing. The slow
  build back to speed after the turn is scored, so a slow tack shows red.
- **Smoothing:** Typon's figures on the map are 31-second medians, so a single wave doesn't show.
- **Wind below 4 kn** (the certificate's lightest column) is not scored.

### Other boats

Rivals have no instruments we can see, only AIS reports about every 30 s. Their speed and course
through the water are their GPS speed and course minus the current *we* measured at that moment.
Their true wind angle uses *our* wind (a 2-minute average). Each is then scored against *its own*
ORC certificate, in the same way as Typon.

So a rival's % assumes it had our wind and our current. Allow ±2–3 points per leg, and more when
it was far from us in shifty wind. Figures like 110% on a leg usually mean it had more wind than
we measured, not that it sailed 10% above its certificate. AIS positions that are clearly wrong
(more than 10 nm from us, or implying a jump faster than 25 kn) are dropped.

The grey dots on the map are every other boat our AIS heard within 3 nm while moving. They are
not scored.

## The race reviews

- **Legs** are split where the boat changes between upwind and downwind. Each leg's % of ORC is the
  time ORC says the leg should take (the rhumb line, the average wind, beat, run or reach as the
  mark angle requires) divided by the time we took. So it includes tacks, gybes, roundings and
  the route. "While sailing" figures leave out the manoeuvres.
- **Corrected time** = elapsed time × handicap, from the official results.
- **Rating vs sailing:** the corrected gap to a rival is split in two:
  - *rating* is the gap there would have been if both boats sailed exactly at their own
    certificates over the same legs in our wind;
  - *sailing* is the rest.

  A reach finish, for example, favours the J/100 on rating.
- **Rival roundings:** a rival's closest AIS pass to where we rounded. When that is far from our
  rounding point, the reviews say the leg split against that rival is uncertain.
- **Tacks:**
  - *cost* is the distance lost compared with carrying on at the VMG before the tack;
  - *recovery* is the seconds until boat speed is back to 95% of what it was before.

  The crib sheet's typical recovery: 27 s in 6–10 kn, 36 s in 10–16 kn, 42 s in 16+ kn.
- **Heel by tack, helm, round-ups, overstands:** read directly from the instruments and the GPS
  track. Causes that the logs cannot see (sail trim, a slow spinnaker set, a luff for right of
  way) are written as inferences and put to the crew as questions.
- **Sea state:** from our own pitching, measured 20 times a second. The spread of pitch, after
  removing slow trim changes, shows how rough it was. Its dominant period is the wave encounter
  period.

## How far to trust it

- **Solid:** positions, the course sailed, times, results, boat speed (±0.3 kn), heel, helm.
- **Good:**
  - wind angles, ±2°;
  - our upwind % of ORC, ±2–3 points (a 2° angle error moves VMG about 2.5%);
  - downwind % of ORC, a little better.
- **Less certain:**
  - *Light air.* Below about 13 kn the vane and the compass disagree by about 4° through tacks,
    and the masthead wind speed reads about 6% higher downwind than upwind. Light-air figures
    carry about ±5 points.
  - *Leeway under 15° of heel.* Two methods disagree by about 1°.
- **Port vs starboard differences** in angle to the wind over a single beat mostly show wind
  shifts, not the helm. The wind direction varies 5–10° from one tack to the next within a race,
  while the instrument itself shows no jump when we tack. Pointing is judged by comparing GPS
  courses with rivals on the same tack at the same moment, which doesn't depend on our wind.
- **ORC's targets in breeze are not a realistic 100%.** The certificate assumes flat water and
  ideal depowering. In 16+ kn upwind, Wowla made 88% of its own certificate across the series and we
  made 87%. So in breeze, about 88% is fleet pace, and a leg there is judged against the rivals, not
  against 100%. Our best moments in 16+ kn were *higher* than ORC's angle but about 0.3 kn under its
  speed: ORC's angle and speed together happened in under 3% of them.
- **Chop costs speed, not angle.** At the same wind, rougher water (measured by our pitch) cost
  about 7% of target speed per degree of pitch spread, while the angle widened only about 1°. The
  loss is largest in 6–13 kn and almost gone in 16+ kn, where the boat has power to spare. Wowla
  lost much less in chop, but on few samples, and "rough" is measured where *we* were. A heavy boat
  carries through one wave but re-accelerates slowly after it, which may be why light air with chop
  is where we lose. We already sail wider than ORC, so footing further is not the fix the data
  supports.
- **Tide** doesn't enter our own % of ORC: speed and wind angle are both through the water, and the
  true wind is computed relative to the water, which is what the sails feel. Tide matters through
  the waves it builds (wind against tide), the route, and the rivals' % (which assumes our current).
- **What the logs cannot see:** sail trim, sail choice, crew position, traveller and sheet
  positions. The data shows *that* something was slow, often *where*, but rarely *why*.
