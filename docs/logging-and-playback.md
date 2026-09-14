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

Three surfaces, one rule: **store UTC, display local.**

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
| Speed | 1x, 2x, 5x, 10x, 60x, Max. `Max` is speed `0`, and `beginReplay` must call `setReplaySpeed` after `startReplay` or `speed \|\| 1` silently coerces Max to 1x |
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
