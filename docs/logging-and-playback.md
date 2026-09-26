# NMEA logging, the hub, and race playback

Everything about what gets recorded on the boat, how you find it, and how you watch it back.
Companion to the [pitfall index](pitfalls.md) entries `P33` `P34` `P35` `P36` `P37`.

---

## What records what

| Piece | Where | Job |
|---|---|---|
| `nmea_capture.py` | Pi, port **8081** | Subscribes to the boat server's `/nmea` WebSocket, writes every sentence to an hourly-rotated file in `logs/`, serves a status page |
| `pi/boat_server.py` | Pi, port **8080** | Bridges the receiver's NMEA stream — TCP client *and* UDP listener (`P40`) — to `/nmea`, serves `/logs`, `/logs/<file>`, `/api/logs`, `/hub` |
| `static/js/nmea-client.js` | Browser | Live WebSocket **or** file replay, into one shared store |

`nmea_capture.py` deliberately reads from the boat server rather than the receiver directly, so
everything fans out from `/nmea`.

Over **TCP** the receiver accepts one client at a time and the Pi holds it, so `nc`-ing
`192.168.47.10:10110` from a laptop while the Pi is running is refused — that much is by design.
But do **not** read a refusal as proof the system is healthy: the same refusal is what a wedged
receiver looks like, and it is indistinguishable from outside. That ambiguity cost ~10 hours of
recording on 2026-09-13/14. See [`P40`](pitfalls.md) and
[nmea-hardware.md](nmea-hardware.md) before drawing any conclusion from that port.

Over **UDP** nothing holds a slot at all, so a refusal there means nothing whatsoever. Check
`/api/health` — `nmea.transport` names the transport actually feeding the boat, and
`nmea.tcp.state` / `nmea.udp.state` say why the other one isn't.

---

## Retention: nothing is ever deleted

There is no age sweep. `KEEP_DAYS`, `cleanup_old_logs()` and `cleanup_loop()` were removed — see
`P34` for what that nearly cost. Logs are the only record of a race and the input to playback.

This is the **opposite** of the HTTP disk cache in `boat_server.py`, which still prunes hard by age
and by byte cap (`P11`). The distinction is refetchability: a cached NOAA response can always be
downloaded again, a recording of last Saturday cannot.

### The guard

"Keep forever" on an SD card fails eventually, and it fails silently: writes start erroring and
capture stops. So the status page carries a storage card:

```
Logs on disk   229.9 MB
Free space     🟢 158.5 GB
Headroom       ~273 days left
```

| Free space | State | Page shows |
|---|---|---|
| ≥ 5 GB | 🟢 ok | figures only |
| 1–5 GB | 🟡 low | amber banner: copy logs off when convenient |
| < 1 GB | 🔴 critical | red banner: capture will stop; copy off and delete by hand |
| unreadable | ⚪ unknown | amber banner: check the card is still mounted |

Thresholds are `DISK_WARN_BYTES` / `DISK_CRIT_BYTES` in `nmea_capture.py`. They are absolute byte
counts, not percentages — what matters is how many more hours of sentences fit, and a percentage of
an unknown card size does not answer that. **`static/hub.html` mirrors these two values**, so change
both or the hub and the status page will disagree.

"Headroom" divides free space by `stats["bytes_written"] / uptime` — bytes **this process** wrote,
never the log directory total. Dividing the directory total reported a just-restarted Pi as filling
the card at hundreds of MB/s (`P34`). Nothing is shown until `HEADROOM_MIN_SAMPLE_S` (10 min) of
uptime; `disk_stats()` is memoised for `DISK_STATS_TTL_S` (30s) because the page refreshes every 5s
over a directory that now grows without bound.

**Clearing space is a manual, deliberate act.** Copy files off, then delete them yourself.

---

## Timezones

Three surfaces, one rule: **store UTC, display local.** A fourth question — *whose* clock — is
answered in [The clock](#the-clock) below, and it is the one that actually went wrong.

| Where | Zone | Notes |
|---|---|---|
| Sentence timestamps inside a log | **UTC**, explicit trailing `Z` | Unambiguous, DST-proof, comparable across the archive, and what the replay parser expects |
| Log filenames | local | `nmea_2026-09-13_120000.txt` is local noon |
| `/logs` listing, capture status page, playback clock | local, labelled | `PDT`/`PST` named explicitly |

The `Z` is a later addition. Before it, a line read `2026-09-13 19:03:12` inside a file named
`120000` — a seven-hour inconsistency with nothing saying which was which. Logs written before the
change have no `Z` and are parsed as UTC, which is what they are, so the archive stays readable.

Do not switch stored timestamps to local time. It would split the archive into two conventions,
break `nmea-client.js`'s parser (which appends `Z` when absent), and reintroduce DST ambiguity for
one hour every autumn.

## The clock

Getting the zone right is worthless if the clock itself is wrong, and for ten days it was. See
[`P42`](pitfalls.md) for the full account; the operational summary:

**The logger derives its own clock from GPS and never trusts the Pi's.** The Pi has no RTC, and at
sea there is no internet, so NTP never runs and `fake-hwclock` restores the time from the last
shutdown — leaving the clock behind by however long the Pi was off, cumulatively across boots. It
reached **115 hours wrong**, which invented the dates on 52 of 76 recordings. Nothing looked broken,
because the filename and the line prefixes share the bad clock and therefore agree with each other.

`$..RMC` is the only usable source: it carries a date as well as a time. `$GPGGA` has time only, and
this bus emits no `$ZDA`.

| Concern | How it is handled |
|---|---|
| A corrupt sentence setting the clock | Checksum validated, and status must be `A` — an invalid fix may carry the receiver's own uninitialised clock |
| One bad sentence redating a whole file | Adoption requires **two agreeing readings** within `CLOCK_STEP_TOLERANCE_S` (2 s) |
| The clock changing part-way through a file | **Rotates.** One file must never hold two clocks, or replay sees time run backwards and auto-advance compares meaningless gaps |
| No GPS fix at all | Logging continues — losing sentences is worse — but the filename gets `_noclock`, the header says the times are unverified, and the `:8081` status page goes red |
| Durations | Untouched. `uptime_seconds()` stays on `time.monotonic()`; a GPS step is exactly the jump `P33` is about. Asserted in `tests/test_log_clock.py` |

**Optionally, the system clock too.** `--set-system-clock` plus `pi/ais-set-clock.sh` steps the
system clock onto GPS truth, fixing what the log offset cannot reach: file mtimes,
`boat_server.py`'s own log lines, `/api/logs` ordering. Off by default, because it needs a one-time
privileged setup and the logs are already correct without it:

```bash
sudo install -m 755 -o root -g root pi/ais-set-clock.sh /usr/local/sbin/ais-set-clock
echo 'rostape1 ALL=(root) NOPASSWD: /usr/local/sbin/ais-set-clock' \
    | sudo tee /etc/sudoers.d/ais-set-clock
sudo chmod 440 /etc/sudoers.d/ais-set-clock
sudo visudo -c
```

Then add `--set-system-clock` to `nmea_capture.py`'s invocation in `pi/startup.sh`. The helper
validates its argument's shape, bounds the year to 2024-2040 (a GPS rollover could otherwise name
1999), and stands down if NTP is actually synchronised rather than fighting it.

**The permanent fix is hardware** — a DS3231 RTC on the I²C header, so the Pi boots knowing the time
with neither GPS nor internet. Everything above is the software half.

### Repairing recordings already on disk

```bash
python3 tools/fix_log_times.py ~/Documents/typon-nmea-logs-raw            # survey
python3 tools/fix_log_times.py ~/Documents/typon-nmea-logs-raw \
    -o ~/Documents/typon-nmea-logs --apply
```

Measures the offset per file from GPS, **splits** where the clock stepped mid-recording, shifts every
line prefix, and renames to true local time. It never modifies or deletes its input (`P34`) and never
guesses: a file with no valid fix is copied to `no-gps/` and listed in `manifest.json` as unresolved.
Output is line-count-audited — a repair that loses sentences is far worse than a wrong filename.

Run on the real archive 2026-09-23: 76 files → 84 (six had mid-file clock steps), 21.8 M lines in and
out, worst residual drift 0.000 s.

Corrected names land at odd minutes past the hour (`nmea_2026-09-19_095204.txt`) because the original
rotation boundaries were aligned to the wrong clock. That is truthful, not a new defect.

The convention on the Mac: **`typon-nmea-logs-raw/` holds the untouched originals, `typon-nmea-logs/`
holds the corrected copies and is what `play_logs.sh` serves.** Pull from the Pi into `-raw`, then
re-run the repair.

## Uptime and the missing clock

A Raspberry Pi has no battery-backed RTC. See `P33` for the full trap. The short version:
`uptime_seconds()` uses `time.monotonic()`, and every duration must. There is deliberately no
wall-clock start timestamp in `stats` for anyone to subtract. Filenames and sentence timestamps are
still wall-clock, and are correct, because they are written after NTP has settled.

---

## The hub (`/hub`)

A static page, `static/hub.html`, self-contained like the other Pi-served pages. Links Map, Charts,
Radar, Playback, the capture status page on `:8081`, and the log browser.

Two rules it follows, both deliberate:

- **Map stays at `/`.** The hub is additive; the existing bookmark and muscle memory are unchanged.
- **Every fetch is `no-store`, and failure is visible.** A cached recording count would claim
  recordings exist that do not. On error the cards read `unavailable` in red and the footer names
  the error, rather than leaving a hopeful placeholder.

Values are written with `textContent`, never `innerHTML` — filenames and counts come from fetched
JSON, and `P19` applies to a status page as much as to the map legend.

---

## Playback

### The data path

Replay is not a separate rendering mode. `nmea-client.js` feeds `nmea-store.js`, whose `ingest()`
decodes AIS via `AISDecoder.processSentence` and dispatches the same events the live WebSocket
produces. Map markers, instrument gauges, competitor labels and the radar plot therefore
animate from a recording without knowing one is playing.

```
/logs/<file>  ──fetch──▶  NmeaClient.loadUrl
local file    ──────────▶  NmeaClient.loadFile ──▶ loadText
                                                     │
                                        startReplay / seek
                                                     ▼
                                   nmea-store  ──▶  Map · Charts · Radar
```

### Transport controls

Reachable from `/#playback`, from the hub, or by loading a local file in the Charts tab. The bar is
global rather than living inside a view, because you watch a race on the map.

Getting in: the **Replay** button in the layer row (next to Route), the `/#playback` deep link, or
the hub. The button is a toggle and lights pink while the transport is open — it is the only way back
in after leaving, so do not remove it.

| Control | Notes |
|---|---|
| Recording picker | Populated from `/api/logs`; shows name and size. Names are **local** date/time |
| ⏮ | `seek(0)` |
| ▶ Play / ⏸ Pause | Labelled deliberately: a bare glyph read as decoration and users could not find it |
| Speed | 1x, 2x, 5x, 10x, 30x, 60x, Max. `Max` is speed `0`, and `beginReplay` must call `setReplaySpeed` after `startReplay` or `speed \|\| 1` silently coerces Max to 1x |
| Scrubber | Seeks on release, not on drag — see below |
| Clock | The recording's own time, rendered **local** (PDT/PST) |
| Exit to Live | Leaves replay entirely: clears the store and the map, reconnects the live WebSocket |

Loading a recording pans the map to own position as soon as the log reveals a fix (polled, because a
log can run thousands of lines before the first one). Typon carries a permanent name label and a
pulsing ring so she is findable among 50 contacts — see `P37` for why the marker also has a movement
deadband.

### Environmental layers are OFF during replay

The overlays are driven by `forecastMinutes` as an offset from `Date.now()`. Nothing in
`data-loader.js`, `tidal-flow.js` or `wind-overlay.js` knows a replay is running, so left alone they
draw **today's** current, wind and tide under a historical track — and the flow legend happily reads
today's model run. That is a wrong conclusion waiting to happen about a tidal gate that did not exist
on the day.

So `suspendEnvLayersForReplay()` switches Tide Flow, Wind and Tide off when a recording loads,
disables their buttons, and shows an amber banner saying why.
`restoreEnvLayersAfterReplay()` puts back exactly what was on before. Both drive the real toggle
handlers rather than reimplementing their teardown.

Why not just fetch the historical data? Per layer:

| Layer | Historical availability |
|---|---|
| Tide heights, station currents | **Available.** NOAA CO-OPS predictions are harmonic; `begin_date`/`end_date` accept past dates. |
| Wind grid | Available, but via Open-Meteo's **archive** API — a different endpoint from the forecast one. |
| SFBOFS current field | **Not available.** Only the newest run exists; `hour_00..48` is overwritten every cycle and the pipeline keeps no archive. |

### Future work: replay with the conditions that were actually there

Two routes, and the second is better than the first.

**1. Fetch historical predictions.** For tides and station currents, request the recording's own date
range instead of today's. Both are harmonic so the data is real, not interpolated. The SFBOFS particle
field cannot follow without archiving runs, so it would stay off.

**2. Use the boat's own measurements — already in every log.** The instrument bus records what the
boat actually experienced, which beats any model along the track:

| Sentence | Rate | Carries | Parsed today? |
|---|---|---|---|
| `$IIMWD` | ~390/h | true wind direction + speed | yes (`MWD`) |
| `$IIMWV` | ~390/h | apparent wind angle + speed | yes (`MWV`) |
| `$IIVHW` | ~390/h | speed through water | yes (`VHW`) |
| `$GPRMC` / `$IIVTG` | ~1000/h | SOG, COG | yes |
| **`$IIVDR`** | **~1000/h** | **current set and drift, already computed by the instruments** | **NO** |

`$IIVDR,12.73,T,,M,0.00,N` is set 12.73°T, drift 0.00 kn — the boat computes the current from SOG
versus speed-through-water and broadcasts it once a second, and `nmea-parser.js` has no `VDR` case,
so it is logged and discarded. Adding that case would give measured current along the whole track for
every recording already on disk, with no new external data source. It is ground truth where SFBOFS is
a model.

(Both are future work. Nothing below the `$IIVDR` row is implemented.)

### Race picker: tracks coloured by % of ORC

The **Race** dropdown (first on the replay bar) lists every race of every regatta in `static/races/index.json`
(one `<regatta>.json` per entry in `tools/regattas.json`, written by `tools/race_tracks.py`).
Picking one:

- draws each boat's whole race track, coloured red → yellow → green by % of its own ORC
  certificate (scoring and calibration: [polar.md](polar.md) §7), and frames the map on Typon's track;
- loads the recording holding the gun (`RaceTracks.fileForTime`) and seeks to it;
- adds a **race box** to the legend that follows the replay clock: per boat, % of ORC, TWA against
  the ORC target angle ("4° low (footing)"), and speed against the target speed with the delta.
  Hovering a track shows the same lines for that point, and clicking it jumps the replay there.

The tracks are Leaflet layers, not store state, so they survive the hourly store resets at each
auto-advance. With them drawn, a new recording does not re-centre the map on the boat. Picking a
recording by hand, loading a local file, or Exit to Live removes them.

For the crew there is a second, lighter way in: `race.html`, which plays the same race files on a
clock with no recordings or server behind it, so it works on GitHub Pages ([polar.md](polar.md) §7).

**Jumps re-read only 10 minutes.** A race-day recording is ~450k lines an hour (AIS plus 20 Hz
attitude), and the full re-read to a gun 40 minutes into one froze the tab for over a minute. So
race jumps call `seek(idx, { warmupMs: 10 min })`: position, wind and every contact that reported in
those 10 minutes are right, and a contact last heard earlier is missing until it next reports. The
scrubber still does the full re-read, and `tests/test_replay.mjs` still asserts "seek == play
through" for it. The warmup path has its own assertions, mutation-checked.

### Auto-advance into the next recording

Recordings rotate hourly, so a three-hour race is three files and used to mean reaching for the
picker twice. When playback runs out, it rolls into the recording that continues the current one.

Three things make this safe rather than merely convenient.

**It never trusts the picker's order.** `/api/logs` returns **newest first**, so the `<option>`
below the current one is an hour *earlier*. "Advance to the next item in the list" plays a race
backwards. `NmeaClient.nextContiguousLog()` sorts by the filename's own timestamp instead and
ignores list order entirely.

**It measures against the log's real last sentence, not an assumed hour.** The logger does not only
rotate on the hour — it also restarts, which produces `nmea_2026-09-18_021420.txt` directly after
`nmea_2026-09-18_020000.txt`. "Next = +1 h" rejects a pair that is in truth seconds apart. So the
gap is `successor's filename time − current log's last sentence timestamp`, and must be within
`NmeaClient.MAX_LOG_GAP_MS` (15 min). The tolerance is **symmetric**: real files overlap by a minute
or so when a restart happens mid-second, and that is still one continuous run.

Filenames are local, sentences are UTC — the "store UTC, display local" rule above — so
`logStartTime()` builds its epoch from local date components. It returns `null`, never `NaN`, for
anything unparseable: `NaN` is not `null`, so it survives null checks and then compares false
against every threshold.

**A successor that is too far away stops playback and says so.** The clock reads
`end — next is 4d later` rather than silently chaining a Saturday race into an unrelated Tuesday
delivery, which would look perfectly continuous on the map. That message is sticky
(`replayEndNote` in `app.js`) because `syncReplayUi` runs on a 250 ms timer and would otherwise
overwrite it before it could be read.

Two deliberate non-triggers:

- **Scrubbing to the end does not advance.** `_finishReplay(reason)` is reached both by playback
  running out (`'end'`) and by dragging the scrubber to max (`'seek'`); only the former advances.
  Having the next hour launch itself because you dragged the slider is startling, not helpful.
- **A local file opened from disk never advances** — there is no list to step through.

The recording list is **refetched before each advance**. Start watching the 22:00 file while 23:00
is still being written and the list fetched when the bar opened does not contain the file you now
want. The current URL is captured *before* that refetch, because a failed refetch rebuilds the
picker with a placeholder and blanks `.value`.

At the boundary the store resets, so competitor tracks and trails restart and rebuild over the next
few seconds. That is the honest option: carrying tracks across would be wiped by the first scrub
anyway, since the scrubber only ever spans the current file.

`tests/test_replay.mjs` covers the selector directly — newest-first ordering, the mid-hour restart,
the threshold either side, unparseable names, an unknown current file, and scrub-to-end not
advancing. Mutation-tested with six deliberate bugs, including "trust the list order" and "parse the
filename as UTC".

### Replaying on a Mac, away from the boat

`./play_logs.sh` — serves `~/Documents/typon-nmea-logs` (override with `LOG_DIR=`) at
`http://localhost:8080/#playback`. Same port as the Pi on purpose, so the local bookmark never
drifts; the script clears a previous instance off the port rather than failing to bind.

**A plain static server cannot do this, and neither can GitHub Pages.** The recording picker reads
`/api/logs` and files come from `/logs/<name>`, both routes in `pi/boat_server.py`. Without them the
picker reads "Recording list unavailable" and **auto-advance cannot work at all** — there is no list
to step through. Loading a file through the Charts tab's local-file input still works, but a local
file deliberately never auto-advances.

Running the real server also synthesises `/config.json` with `useCloudAIS:false`, which is what you
want: otherwise AISstream keeps injecting live traffic over the historical fleet.

Copy recordings off the Pi over HTTP — no SSH needed — by walking `/api/logs` and fetching each
`/logs/<name>`. Skip files already present at the same size and the copy is idempotent, so it can be
re-run to collect only what is new.

### Why seeking re-reads the log

Instrument state accumulates: position, heading, wind and AIS targets each persist until a later
sentence overwrites them. Moving an index alone would show the boat at 14:05 still carrying
readings from 14:50. So `seek(idx)` resets the store and re-ingests `[0, idx)` with no timing.

An hour of NMEA is ~12k lines and reseeks in milliseconds; the largest logs are ~150k lines. That
is why the scrubber seeks on `change` and only previews the clock on `input` — seeking on every
drag event would rebuild state dozens of times per second.

`tests/test_replay.mjs` asserts the invariant directly: **seeking to N leaves the store identical to
playing through to N**, verified in both directions. 57 assertions, mutation-tested with nine
deliberate bugs. See `P35` for the transport bar's positioning traps, `P36` for the stale-socket
guard, `P37` for the coalescing and deadband.

### Replayed contacts must never look live

This is the sharpest edge in the whole feature, and it shipped broken once. AIS targets do **not**
live in `nmea-store`: the `'ais'` listener feeds `vesselStore`, the `vessels` Map, Leaflet markers
and track polylines, none of which `NmeaStore.reset()` touches. So:

- `nmea-store` stamps every decoded vessel with `_reportedAt` — the sentence's own timestamp — and
  `VesselStore.upsert()` uses that for `_lastUpdate` and for track point times. Stamping `Date.now()`
  made a June race read as current traffic.
- `VesselStore.now()` returns the **log clock** in replay (published by `NmeaClient.onReplayClock`),
  so `prune()` and `getTrack()` do not delete historical contacts as instantly stale.
- Nothing persists to `localStorage` while replaying, or a reload would restore the fleet as live.
- `setReplayMode()` clears in **both** directions, and `NmeaClient.onReplayReset` lets `app.js` drop
  the Leaflet markers and track lines too. Clearing only on entry, and trusting the caller to tidy up
  on exit, is precisely how the leak happened.

### Speed `0` and `startReplay`

`startReplay(speed)` does `speed || 1`, so `startReplay(0)` silently becomes 1x. Max speed is
reachable through `setReplaySpeed(0)`, which is what the speed dropdown calls. Worth knowing if you
write a test that expects `startReplay(0)` to dump the whole log at once — it will not.
