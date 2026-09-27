# Instrument calibration session

About 45 minutes on the water. The Pi logs everything, so nobody needs to watch the instruments.
**Just write down the clock time at the start and end of each step** (phone clock is fine; the
logs run on GPS time). Afterwards, send the times and the analysis fits everything from the log.

## Before you go

- **Slack water**, in flat water, in the middle of the Bay: away from the shore, the channel edges
  and the Gate. Near slack the current hardly changes over 45 minutes, so it is the same for every
  step.
- **Steady wind of 8–12 kn** if you can choose. That is where the open port/starboard question is.
- Check the Pi is logging: the capture page on port 8081 shows the file growing.
- Sails up and trimmed normally for the beats; the motoring steps are without sails.

## The steps

| # | What to do | What it fixes |
|---|---|---|
| 1 | **Motor upright, no sails, dead straight: 2 min each way on a reciprocal pair** (e.g. north, then south). Do the pair twice: at ~5 kn and at ~7 kn. | The current on the spot, the paddlewheel's true scale, and whether that scale changes with speed |
| 2 | **Same, on a second pair at 90° to the first** (e.g. east, then west), ~6 kn. | Compass deviation on four headings, and a check on the current |
| 3 | **Rudder:** during step 1, note the rudder reading while motoring dead straight. | The rudder zero (now only estimated, ±1–2°) |
| 4 | **Beat 3 min on starboard, tack, 3 min on port**, normal heel. The helm steers by the **jib telltales, not the display**. Once each board settles, someone **writes down the display AWA** (and the boat speed). Then repeat once. | The paddlewheel error on each tack, directly (the current is known from step 1). The written-down AWA shows whether the vane's light-air offset is ~1° or ~3° |
| 5 | **5–6 tacks, 2 min on each board**, steady helm and trim, no big trim changes between boards. | Upwash and vane offset from the "wind direction doesn't jump at a tack" test, in clean conditions |
| 6 | **Repeat one reciprocal pair from step 1.** | Confirms the current didn't change, so steps 4–5 can be trusted |
| 7 | *Optional, steady wind:* run downwind 3 min, then beat 3 min. | Wind speed upwind vs downwind (it reads ~6% higher downwind now), and the paddlewheel downwind |
| 8 | *Optional, a windless day:* motor 2 min each way on a reciprocal pair. | With no wind, apparent wind speed should equal boat speed: calibrates the wind speed sensor |

Steps 1 and 6 bracket everything else: with the current measured on the spot, steps 2–5 need no
assumptions. That is what race data can't give us.

## What to write down

| Step | Start | End | Notes (heading, speed, display AWA, anything odd) |
|---|---|---|---|
| 1a north ~5 kn | | | |
| 1b south ~5 kn | | | |
| 1c north ~7 kn | | | |
| 1d south ~7 kn | | | |
| 2a east | | | |
| 2b west | | | |
| 3 rudder reading | | | |
| 4a starboard beat | | | display AWA: |
| 4b port beat | | | display AWA: |
| 4c starboard beat | | | display AWA: |
| 4d port beat | | | display AWA: |
| 5 tacks | | | |
| 6 reciprocal pair | | | |
| 7 run / beat | | | |

## At the dock or the next haul-out

- **Paddlewheel:** where exactly it sits (distance off the centreline, which side) and which way it
  points. It is known to be off to one side. The race logs show it over-reads heeled to port
  (starboard tack), by ~0.4 kn at 6.5 kn, and its reading is twice as jumpy there.
- **Wind instrument settings:** whether the display has an AWA offset set. Once this session is
  analysed, the fitted offset goes in there, so the same number means the same angle on both tacks.

## What happens afterwards

Send the times (a photo of this table is fine). The analysis reads each step from the log and fits:
the paddlewheel scale per tack and speed, compass deviation, the vane offset and upwash, rudder
zero, and wind speed. These replace the estimates made from race data, which can't separate the
instruments from the current each tack sailed in.
