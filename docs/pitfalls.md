# Critical pitfalls — solved problems, do not re-investigate

Every entry here cost real debugging time or shipped a real failure. Re-"optimizing" one
re-introduces a bug already paid for.

> **Retrieval: fetch entries by ID, never read this file whole.** The grouped index in
> [CLAUDE.md](../CLAUDE.md#critical-pitfalls--do-not-re-investigate) tells you *which* IDs apply
> to what you're touching. Then:
>
> ```bash
> grep -n '\[P06\]' docs/pitfalls.md          # -> line number, then Read with offset/limit
> # or print exactly that entry:
> awk 'index($0,"[P06]")&&/^## /{f=1;print;next} f&&/^## /{exit} f' docs/pitfalls.md
> ```
>
> IDs are permanent. Never renumber. New pitfalls take the next unused number, even if an
> earlier one is retired.
>
> **18 of these are mechanically enforced** — `P01` `P03` `P06` `P07` `P08` `P10` `P11` `P15` `P16`
> `P18` `P20` `P21` `P22` `P24` `P33` `P34` `P36` `P37` have a test that fails if the fix is undone. Entries marked
> **`ENFORCED`** name the test. The rest rely on this file being read, so if you touch a doc-only
> pitfall's code area, ask whether an assertion could promote it.
>
> Code implementing a guarded pitfall cites its ID in a comment
> (`grep -rn 'P[0-9][0-9]' pi/ static/js/ static/sw.js`), so a trap is discoverable from the code
> and not only from the index.

---

## SFBOFS pipeline and the forecast timeline

## A stale model run makes every forecast offset render identically [P01]

**`ENFORCED`** — `tests/test_staleness.mjs`: refuses a stalled run, and NOW vs +4h must resolve to different hours.

`fetchCurrentField()` indexes files as `min(48, elapsedHours + offsetHours)`. Once a run is old
enough that `elapsedHours >= 48`, *every* offset clamps to `hour_48.json` — NOW, +2h and +4h all
draw the same field, and the app presents a dead forecast as live data with no visual difference.

This is the 2026-08 outage: GitHub auto-disabled the cron workflows for inactivity, the field
froze, and it read as normal for **three weeks**.

The gate: `fetchCurrentField()` refuses to return a grid when the run is older than
`SFBOFS_RUN_STALE_HOURS` (12h — SFBOFS reruns every 6h), returning
`{unavailable:true, stale:true, runAgeHours}`. The flow legend shows a red "model run is Nh old —
fetch pipeline stalled" and the grid is **cleared, not drawn**. Guarded by
`tests/test_staleness.mjs`, which also covers the aliasing case directly (NOW and +4h must resolve
to different hours). Do not "simplify" the gate away because the data looks present. See also
[P02].

## Scheduled workflows are disabled after 60 days without a push [P02]

GitHub disables cron workflows after 60 days with no repo **pushes**. Cron runs and cache writes
do not count. This is the root cause of [P01]'s three-week freeze.

`ndbc.yml` commits `.github/data-heartbeat` once a week purely to reset that clock. The filename is
extension-less on purpose — `.gitignore` used to carry a bare `*.txt` that would have swallowed it
([P23]).

**When data goes stale, check this first:**
```bash
gh api repos/:owner/:repo/actions/workflows --jq '.workflows[] | {name, state}'
gh workflow enable <name>   # if state is disabled_inactivity
```

## A 404 in the download sweep is normal; anything else is not [P03]

**`ENFORCED`** — `tests/test_staleness.mjs`: 404 breaks cleanly and is not a gap; transient faults retry once then record a gap.

NOAA model runs don't always publish all 49 hours, so a **404 means "this hour was never
published"** and breaking the sweep there is correct. Every *other* fault — timeout, 5xx,
cold-cache miss — is transient and must not be treated the same way.

Before the fix, any error broke the loop: one blip at hour 20 silently capped coverage at 20h and
reported it as though 20h were the run's real extent. Now a non-404 fault is retried once, and if it
still fails the hour is recorded in `flow_gaps` and the sweep **continues**.

This is why the Pi proxy must pass a real 404 through untouched ([P08]) — swallowing it into a 504
or a stale body would make the sweep read "never published" as "keep going" or vice versa.

## The download badge counts reach, not files [P04]

`Flow +Nh` is how far ahead of *now* the cached forecast reaches, computed from `flow_max_hour` —
**not** from the file count. With gaps the two diverge and the count understates reach. It counts
down as the run ages and jumps back up on the next NOAA cycle.

If any hour is missing the badge turns amber and reads `Flow +Nh ⚠<n>`, with a tooltip naming the
missing hours. An incomplete sweep must never render as the green all-clear — that is the whole
point of the badge, and it is the same failure shape as [P01]: present-looking data that is not
what it claims.

## Fetch the `f` files, not the `n` files [P05]

NOAA publishes nowcast files (`n000`-`n006`, hindcast/analysis) and forecast files (`f000`-`f048`,
actual forecasts). We fetch only `f`: `f000` = cycle time, `f048` = +48h. Mixing in `n` files gives
you past-looking analysis where the UI promises forecast.

## `ndbc.yml`'s `restore-keys` is what puts SFBOFS data in the deploy [P32]

The two data workflows share one `static/data` cache. `ndbc.yml` runs every 10 minutes and saves
under `env-data-${{ github.run_id }}-ndbc`, but it **restores** with:

```yaml
key: env-data
restore-keys: |
  env-data-
```

The prefix `restore-keys` is what makes it pick up the newest cache from *any* run, including
`sfbofs.yml`'s. Remove it, or "tidy" it to an exact key, and the NDBC job restores nothing, saves a
cache containing only buoy data, and the next deploy ships **without any SFBOFS current field** — the
map loses its currents with every workflow green. Same silent-success shape as [P12].

---

## Pi reverse proxy and disk cache

## A date in the cache key defeats the entire stale-on-error window [P06]

**`ENFORCED`** — `tests/test_boat_server.py`: the rollover serves yesterday's window, **and** a second test asserts it still 504s *without* the alias.

NOAA prediction URLs embed `begin_date=<today UTC>`, and `DiskCache` keys on SHA1 of the **full
URL**. So at 00:00 UTC every pre-warmed tide and current entry became a cache miss, upstream was
unreachable, and the proxy returned 504 — **every tide and current station blank on day 2 offline**,
with nothing in the UI explaining why. `MAX_STALE_TIDES_S` / `MAX_STALE_CURRENTS_S` (30 days) were
unreachable dead code: stale-on-error could only ever help within the same UTC day.

Fix: each NOAA entry also writes an **alias pointer** keyed on (station, product, interval, datum)
with the dates stripped (`_noaa_alias`, `DiskCache.get_alias`). When the exact key misses *and*
upstream is down, the stale branch falls back to the alias and sets
`X-Cache-Reason: upstream-error-date-shift`. `env_prewarm_loop` also warms tomorrow's window each
cycle.

`tests/test_boat_server.py` guards this, including a check that it still 504s **without** the alias —
so the fix cannot be quietly removed. Tides are the one dataset that should never fail offline;
harmonic predictions stay valid for weeks.

## Captive portals answer 200 with HTML, and it gets cached as truth [P07]

**`ENFORCED`** — `tests/test_boat_server.py`: a 200 text/html body is neither cached nor served.

Marina and satcom WiFi intercept requests and return `200 text/html` login pages. Any body cached
without a content-type check overwrites good JSON and is then re-served as `X-Cache: STALE` for up
to the source's ceiling — 30 days for tides. The boat shows a login page's worth of nothing where
the tide curve was.

`_acceptable_json()` gates it: a non-JSON 200 is **neither cached nor served**, and falls through to
the stale branch like any other failure. Accepts `application/json`, `text/json`, `*+json`, with or
without a charset.

## Only 404 passes through; 5xx and other 4xx must fall back to cache [P08]

**`ENFORCED`** — `tests/test_boat_server.py`: 404 passes through; 503 serves stale and never leaks the error body.

Stale-on-error originally fired only on exceptions (`ClientError`, `TimeoutError`), so a NOAA 503 or
a GH-Pages 500 was proxied straight through to the browser while a perfectly good cached prediction
sat on disk unused.

Now `status >= 500` and non-404 4xx both fall through to the stale branch. **404 still passes
through untouched** because the browser's download sweep depends on that exact signal ([P03]).
Getting this backwards breaks one of the two.

## A total timeout makes satcom pre-warm mathematically impossible [P09]

`UPSTREAM_TIMEOUT_S = 10.0` was applied as aiohttp's **total** timeout, including body transfer. An
SFBOFS hour is ~1.2 MB. Over satcom every single hour aborted at 10s → `cycle_failed` → 60s
backoff → the whole 49-file sweep restarted from hour 0 and aborted again. Net effect: roughly
**1.4 GB/day** of metered bandwidth burned while the cache never populated once.

`/data/*` bodies now use `DATA_TIMEOUT` with `sock_connect` / `sock_read` instead of `total`, skip
hours still within TTL, and send `If-Modified-Since` from the cached meta (304 → `DiskCache.touch`).
Never reintroduce a total timeout on a large-body route.

## Cache writes must be atomic, and concurrent fetches must be coalesced [P10]

**`ENFORCED`** — `tests/test_boat_server.py`: 20 concurrent requests cause 1 upstream fetch; no `.tmp` files left behind.

`body_path.write_bytes()` truncates in place, so a reader could see torn JSON mid-write, and a crash
between the body and meta writes left a fresh timestamp pointing at a half-written body. Writes now
go to a `.tmp` sibling then `os.replace()`.

Separately there was no single-flight: a cold cache meant **49 simultaneous upstream fetches per
client**, multiplied by every browser on the boat WiFi — a request storm on the one link the whole
design exists to protect, while the docs claimed clients shared one fetch. `_fetch_upstream` +
`app["inflight"]` now make concurrent callers for the same URL await the first fetch.

## An unbounded cache fills the SD card and takes NMEA logging with it [P11]

**`ENFORCED`** — `tests/test_boat_server.py`: prune removes over-age entries and evicts oldest-first under the byte cap.

Keys include the query string, and `_noaa_date_range_utc()` embeds the date, so ~22 new NOAA entries
appear per day and nothing was ever deleted. Any unauthenticated client on the boat WiFi could also
mint unlimited entries with junk query params. A full SD card also stops `nmea_capture.py` writing
logs, so you lose the voyage record as collateral.

`DiskCache.prune()` runs once per SFBOFS cycle: age sweep at `CACHE_MAX_AGE_S`, then oldest-first
until under `CACHE_MAX_BYTES`. Note the contrast with NMEA logs, which are **never** pruned ([P34]):
cache entries are refetchable, recordings are not.

## "Pre-warm complete: 0 hours cached" was logged as success [P12]

A 404 at `hour_00` ended the pre-warm cycle without setting `cycle_failed`, so the loop logged
completion, reported zero hours, and slept a full hour instead of backing off and retrying. A 404 on
`hour_00` specifically is now a failed cycle; later hours missing still breaks cleanly per [P03].

---

## Browser bootstrap, tiles and the Service Worker

## Module-scope code runs before `/config.json` resolves [P13]

`window.APP_CONFIG` is populated by an async fetch, but the tile layers and the first environmental
loads are constructed at module scope. `loadCurrents()`, `loadCurrentField()`, `loadWindField()` and
`loadTideHeight()` were invoked synchronously at the bottom of the file, so on that first pass
`noaaApi()` / `openMeteoApi()` still returned the hardcoded `api.tidesandcurrents.noaa.gov` and
`api.open-meteo.com` URLs — **the first load at sea bypassed the Pi proxy entirely**, hung for the
full timeout, and showed "unavailable" until the next refresh tick.

Anything that must honour boat mode has to be gated on `_configPromise`, as AIS and the NMEA socket
already were. Tile layers can't wait (they're constructed at module scope), so
`_serveTilesFromDisk()` guesses from the hostname and a `_configPromise.then()` reconciler re-points
them if the config disagrees — see [P15].

## The `/config.json` fetch gates AIS and NMEA, so it needs a timeout [P14]

`connectAISStream()` and the NMEA auto-connect both `await _configPromise`. The bootstrap was a bare
`fetch()` with no `AbortController`, so a **hanging** (not failing) request left the app with no AIS
and no NMEA *indefinitely*, status pill never updating. Captive-portal WiFi that accepts the
connection and then stalls is the canonical trigger — the same class as [P07].

Now bounded by `CONFIG_TIMEOUT_MS` (2.5s) and falls through to web-mode defaults on abort. Every
other fetch in the repo uses `_fetchWithTimeout` for exactly this reason; a new one that doesn't is a
bug.

## Local tiles exist only at z10-15, and "not github.io" is not "on the boat" [P15]

**`ENFORCED`** — `tests/test_invariants.mjs`: `LOCAL_TILE_*_Z` must equal `DEFAULT_ZOOM_RANGE`, the clamp must be wired into layer options, and localhost must be excluded.

`download_offline.py` fetches `DEFAULT_ZOOM_RANGE = (10, 15)`. The Leaflet layers declared `maxZoom`
16-19 with no `maxNativeZoom`, so zooming past 15 — routine when picking a slip or a mark — 404'd
every tile and painted a **black canvas** with no CDN fallback. Layers now set
`minNativeZoom`/`maxNativeZoom` to `LOCAL_TILE_MIN_Z`/`LOCAL_TILE_MAX_Z` so Leaflet upscales instead.

Separately, the switch was `!location.hostname.endsWith('github.io')`, which is "not GitHub Pages",
not "local tiles exist". That captured **localhost**, so the documented local-dev flow
(`python -m http.server --directory static`) requested `tiles/*` from a gitignored, empty directory
and showed a blank basemap with no explanation. `_serveTilesFromDisk()` now prefers
`APP_CONFIG.mode === 'boat'` and excludes localhost.

## Service Worker quota eviction deleted the app shell [P16]

**`ENFORCED`** — `tests/test_invariants.mjs`: loads `sw.js` with a fake Cache API; the app shell survives a quota event and `hour_00` outlives `hour_48`.

On `QuotaExceededError`, `safeCachePut` dropped the oldest 20% of the **same** cache. Two distinct
disasters, because the Cache API preserves insertion order:

- For `CACHE_NAME` the oldest entries are the install-time `ASSETS` — `leaflet.js`, `app.js`,
  `style.css`. A quota error while caching any HTML/JS response silently destroyed offline
  bootability.
- For `DATA_CACHE` insertion order is **ascending SFBOFS hour**, so it evicted `hour_00..09` — the
  hours actually being sailed — and kept +40h.

The unbounded `TILE_CACHE` is what creates the pressure, and tiles are always re-fetchable. Eviction
now frees space there, never from `CACHE_NAME`, and `DATA_CACHE` drops the **farthest** forecast
hours.

## Bump the tile cache name when the tile CDN changes [P17]

The dark base moved CartoDB → ArcGIS while `TILE_CACHE` stayed `ais-tiles-v1`, and `activate`
explicitly preserves known cache names. Every previously cached CartoDB tile became unreachable
dead weight consuming quota (accelerating [P16]), and users kept getting CartoDB's
"API KEY REQUIRED" watermarked tiles served **cache-first, forever**. Bumped to `ais-tiles-v2`; the
`activate` handler deletes unknown names automatically.

## `hostname.endsWith(host)` matches an attacker's subdomain [P18]

**`ENFORCED`** — `tests/test_invariants.mjs`: `isTileHost` rejects `evilservices.arcgisonline.com` and accepts `a.tile.openstreetmap.org`.

`TILE_HOSTS.some(h => url.hostname.endsWith(h))` also matches
`evilservices.arcgisonline.com`. Use `hostname === h || hostname.endsWith('.' + h)` — which still
covers OSM's `{a,b,c}.tile.openstreetmap.org`.

## Legend text is fetched data, so it is an injection sink [P19]

The flow and wind legends moved from `textContent` to `innerHTML` to add a coloured freshness dot,
which made `data.source`, `data.model_run` and `model_obs_time` — all read from fetched JSON — an
injection sink. Build the dot as an element and keep the model strings as text nodes.

---

## The JS ↔ Python mirror, tests and CI

## The two station lists and the wind grid are a hand-maintained mirror [P20]

**`ENFORCED`** — `tests/test_boat_server.py`: station lists and grid constants must match, and coordinates are compared against real `node` `toFixed(4)`.

`TIDE_STATIONS`, `CURRENT_STATIONS`, `WIND_BOUNDS`/`WIND_NX`/`WIND_NY` and the NOAA + Open-Meteo URL
shapes exist **twice**: `static/js/data-loader.js` and `pi/boat_server.py`. The disk cache is keyed
on SHA1(url), so pre-warming only helps if the Pi's URL is **byte-identical** to the browser's. A
one-character drift doesn't error — it silently turns the entire pre-warm into a no-op that surfaces
only as "no wind at sea", weeks later.

The float formatting is the subtle part: JS `.toFixed(4)` and Python `f"{x:.4f}"` round differently
at a tie (half-up vs half-even). `tests/test_boat_server.py` compares the generated coordinates
against `node`'s actual `toFixed(4)` output rather than assuming they agree.

Counts, for reference: 16 tide stations, 6 current stations, 11×16 = 176 wind points. Don't put a
count in a comment — the test asserts the lists match, and a wrong count in prose is exactly what
lets real drift go unnoticed.

## A stale `.pyc` can make a test pass against a bug [P21]

**`ENFORCED`** — `tests/test_boat_server.py` sets `sys.dont_write_bytecode`; found by mutation-testing that suite.

`importlib.spec_from_file_location` uses `__pycache__`, validated on source **mtime + size**. A
mutation that changes the byte count not at all — `WIND_NX = 11` → `WIND_NX = 12` — can be served
from a cached `.pyc`, so the test reads stale code and passes against a bug it is designed to catch.
Found while mutation-testing the suite.

`tests/test_boat_server.py` sets `sys.dont_write_bytecode = True` before loading `boat_server`. If
you write another Python test that imports repo code by path, do the same.

## CI could not see most of the repo [P22]

**`ENFORCED`** — indirectly: CI now runs on `pi/**` and `tests/**`, so the other enforced tests actually execute.

`deploy.yml`'s push trigger was `paths: ['static/**', '.github/workflows/deploy.yml']`. So a commit
touching only `tests/**` never ran the test job — which is exactly the shape of the two "Fix flaky
staleness test" commits, both of which landed **without CI**. `pi/boat_server.py`,
`download_offline.py` and `nmea_*.py` had no coverage and could not trigger CI at all.

`paths` now includes `tests/**`, `pi/**`, `download_offline.py`, `nmea_*.py`. When you add a new
top-level script or directory that CI should gate, add it here too.

---

## Git, ports and the two deploy targets

## `.gitignore` patterns are unanchored by default [P23]

A bare `*.txt`, added to keep NMEA captures out, also matched `requirements.txt`,
`pi/requirements.txt` and `.github/scripts/requirements.txt`. Those three survived only because they
were already tracked — any regenerated or new `.txt` would have been silently dropped by `git add`,
with no error. It's also why the Actions keepalive file had to be extension-less ([P02]).

Now `logs/*.txt` + `nmea_*.txt` with a `!**/requirements.txt` guard. Verify any new ignore rule:
```bash
git check-ignore -v --no-index <path>   # prints the rule that matched, or nothing
```

Related: narrowing an ignore rule does **not** untrack files already in the index — that needs
`git rm --cached`, and history keeps them regardless.

## The Pi's port and scheme drifted across five files [P24]

**`ENFORCED`** — `tests/test_invariants.mjs`: the `--port` default, `start_boat.sh`'s `PORT`, the smoke test's `TARGET` default and scheme derivation, and `nmea_capture`'s ws URL and web port must all agree.

`start_boat.sh` launches plain **HTTP on 8080**, but `boat_server.py --port` defaulted to `8443`
(an HTTPS-by-convention port) with an HTTPS-only docstring, `pi/test_boat_server.sh` defaulted to
`https://localhost:8443` and hardcoded the scheme so **all ten of its checks failed** against the
shipped server, `nmea_capture.py --ws-url` defaulted to `wss://…:8443/nmea`, and `USER_GUIDE.md`
still said `:8888`. All now 8080/HTTP. See the ports table in
[CLAUDE.md](../CLAUDE.md#ports-and-who-owns-them) before adding or changing one.

## `boat-mode` is a live deploy branch, not a working branch [P25]

`pi/startup.sh` runs `git fetch && git reset --hard origin/boat-mode` on **every** boot and
`systemctl restart`. Pushing to `boat-mode` therefore stages code onto the boat's live navigation
server, to be picked up the next time it restarts — possibly while under way, and with no
opportunity to verify at sea first.

`main` is what GitHub Pages deploys. The two are separate decisions: you can push to `main` alone
(`git push origin HEAD:main`) to update the web version while leaving the boat on known-good code.
Prefer that when you are not physically at the boat.

## Static changes reload themselves; Python changes do not [P26]

aiohttp's `add_static` reads from disk per request, so a `git pull` is enough for HTML/JS/CSS on the
Pi. Any change to `pi/boat_server.py` needs `sudo systemctl restart ais-tracker` — a pull alone
leaves the old module in memory and you will debug behavior that is no longer in the file.

---

## Route optimizer

## TWA is computed against wind-over-water, not wind-over-ground [P27]

The polar describes what the sails feel, which is `wind − current`, not the ground-frame wind. Using
ground-frame wind in a 3kn Golden Gate ebb produces a boat speed the boat cannot achieve and a route
that depends on it. Details: [docs/router.md](router.md).

## A tack penalty added as time breaks the isochrone [P28]

The wavefront is an **equal-time** contour. Adding 60s to one branch puts that point on a different
clock from its siblings, so the isochrone stops meaning anything and pruning compares
non-comparable points. The penalty is applied as a *speed reduction during the step* instead.

## A wavefront point with no legal heading must still advance [P29]

If a point has wind < 0.5kn, or every heading from it hits land, it used to contribute nothing and
the wavefront could silently empty — reported to the user as "no reachable path" in open water. A
single **drift point** is now pushed at the same wall-clock advance, displaced only by the current.

## A pruning score must be strictly monotonic in distance [P30]

The VMC variant biases by `dist * (1 + 0.5·cos(brg − destBrg))`, deliberately bounded to
0.5–1.5× dist so it stays strictly increasing in `dist`. An earlier version let a
closer-but-better-aimed point beat a further one, which collapsed back-sector points into the coast
and lost the wavefront's spread. `bestToDest` is always carried forward regardless of pruning.

## The land buffer has to be dropped near the destination [P31]

The 200m land buffer that keeps routes off the shore also makes every harbor and shoreline waypoint
unreachable. Within `DEST_APPROACH_NM` (1nm) of the destination the buffer is dropped and strict
`_isLand` is used instead. Monterey harbor was unreachable at 2.8nm before this.

---

## NMEA logging, the capture status page and playback

## Elapsed time must be monotonic — the Pi has no clock [P33]

**`ENFORCED`** — `tests/test_boat_server.py` AST-walks `nmea_capture.py`: no `start_time` subscript
exists anywhere, and `status_html()` must call `uptime_seconds()` and must not call `time.time()`.
Verified by mutation: reintroducing the bug fails two assertions.

A Raspberry Pi has no battery-backed RTC. It boots believing whatever `fake-hwclock` last wrote to
the SD card, then NTP steps the clock — possibly by months — once the network comes up. Anything
that measures a duration as `time.time()` now minus a `time.time()` captured at startup counts that
correction as elapsed time.

The status page reported **`112d 16h 12m` uptime for a process 35 minutes old**, after the Pi had
been unplugged for a week — the gap from the last `fake-hwclock` save (May 23) to the real date
(Sep 13). Nobody would have noticed the number was fiction, and it invites exactly the wrong
conclusion: "capture has been running continuously, so the feed is fine."

Use `time.monotonic()` for every duration; wall clock is only for timestamps you intend to display.
There is deliberately **no wall-clock start timestamp in `stats`** — leaving one there is an
invitation for the next person to subtract it. The same trap applies to any age-based sweep — see
[P34].

## NMEA logs are never deleted, and that needs a guard [P34]

**`ENFORCED`** — `tests/test_boat_server.py` asserts `nmea_capture.py` defines no `cleanup_old_logs`
/ `cleanup_loop`, has no `KEEP_DAYS`, and calls no `os.remove` / `os.unlink` / `shutil.rmtree`; plus
ten functional assertions on the alert itself (headroom numerator, minimum sample, the write-error
headline, memoisation).

`nmea_capture.py` used to delete logs older than `KEEP_DAYS = 28`. They are race recordings — the
only copy of what actually happened, and the input to the playback viewer. Four months of sailing
data survived only because the sweep ran with the wrong clock ([P33]); once the clock corrected,
the next hourly pass would have deleted 27 files.

The sweep is gone. Nothing in `nmea_capture.py` deletes a file. **This is the opposite of the HTTP
cache**, which still prunes hard ([P11]) because every byte in it is refetchable.

"Keep forever" on an SD card is its own hazard, so the guard is an alert, not a deletion: the status
page shows total log size, free space and a projected headroom in days, going amber under 5 GB and
red under 1 GB. If the card fills, writes fail and capture stops silently — the alert is the only
thing standing between the operator and that.

Three things about that alert are load-bearing, and all three were wrong on the first attempt:

- **A failing write is reported as a storage fault, not a link fault.** `_outfile.write()` raising
  `OSError` used to unwind into `capture_ws()`'s `except (OSError, …)` reconnect handler, so a full
  card displayed 🔴 **Disconnected** — indistinguishable from a VHF or receiver failure, and the
  first thing an operator would go and power-cycle. `log_sentence()` now catches it, sets
  `stats["write_error"]`, and that outranks the connection dot.
- **The headroom projection divides bytes written by *this process* by its own uptime.** Dividing
  the log *directory* total instead reported a Pi restarted a minute ago as filling the card at
  hundreds of MB/s — "~0 hours left" beside a green 🟢 158 GB row. A gauge that cries wolf on every
  restart is a gauge nobody reads, which defeats the whole point.
- **Nothing is projected before `HEADROOM_MIN_SAMPLE_S`** (10 min). No number beats a wrong one here.

`disk_stats()` is memoised for 30s: the page self-refreshes every 5s over a directory that now grows
without bound (~8.8k files/year), in the same process writing the live NMEA stream.

## The transport bar is positioned against measured bars, not a fixed offset [P35]

The bottom of the map is a stack of independently-toggled fixed elements — `layers-tray`,
`timeline-strip`, `status-bar`, `forecast-quick-btns` — and which are visible depends on the active
tab and the viewport. A hardcoded `bottom:` for `#replay-bar` overlapped the Vessels/Labels/Route
buttons on desktop and the layer toggles on mobile.

`positionReplayBar()` in `app.js` measures the topmost visible bar and sets `bottom` from it. Two
traps if you rewrite it:

- **`offsetParent` is `null` for `position: fixed` elements**, by spec, even when fully visible.
  Using it as the visibility test skips every bar and collapses the transport onto the status bar.
  Test with `getComputedStyle(el).display === 'none'` instead.
- The mobile stack collapses via the hamburger **without firing `resize`**, so a `ResizeObserver` on
  those bars is what keeps it correct, not the window event alone.

## A closed WebSocket's handlers fire late and clobber replay state [P36]

**`ENFORCED`** — `tests/test_replay.mjs`: a stale socket's `onclose` must not change status, and its
`onmessage` must not ingest into a running replay.

`startReplay()` calls `disconnect()`, which closes the live socket. But `close()` is asynchronous:
the socket's `onclose` fires *afterwards*, and the handlers were bound to `this`, so it set the
client status to `'disconnected'` **while the recording was playing**. The header read
"Disconnected" over a visibly advancing scrubber, which is what made the transport look broken —
the user reported "there was no play button" because nothing on screen said playback was running.

`onmessage` was the more dangerous half: a late-arriving live sentence would ingest into the middle
of a replay, mixing present-day instrument data into a historical picture.

Fix: `_doConnect()` captures the socket in a local and every handler checks
`this.ws === sock && !this._stopped` before acting. Do not "simplify" that back to `this.ws`.

## The own-ship marker needs a deadband, or it shakes [P37]

**`ENFORCED`** — `tests/test_replay.mjs` covers the coalescing half (one bulk span per playback
frame); the deadband itself is doc-only.

Two compounding causes, both of which have to stay fixed:

1. **Coalescing.** `nmea-store` dispatches `'ais'` synchronously per sentence, and its listener
   calls `updateMarker()` + `updatePanel()` (a full `innerHTML` rebuild of up to 50 cards). Replay
   ingests in bulk, so every replay ingest — the per-frame batch **and** a seek — runs inside
   `beginBulk()`/`endBulk()`, which coalesces to one `'ais-batch'` event. Without it a scrub froze
   the tab for minutes and normal playback redrew hundreds of times a second.
2. **A deadband on own position.** GPS arrives at ~10 Hz and a boat at the dock has a couple of
   metres of noise; its AIS position is coarser and disagrees. Redrawing on every event made the
   icon visibly shake. `ownMarkerNeedsUpdate()` holds the marker unless it moved >2 m
   (`OWN_MOVE_DEG`), turned >2° (`OWN_TURN_DEG`), and at least 200 ms has passed.

The deadband is not cosmetic: a jittering own-ship icon on a navigation display is actively
misleading about your own position. If you raise the thresholds, check a moving boat still tracks
smoothly — at 6 kn the marker updates ~1.5×/s, which is the intended floor.

## A shell script that git-pulls itself deploys one boot late [P38]

`pi/startup.sh` does `git reset --hard origin/boat-mode` and is *itself* under
that reset. bash reads a script incrementally as it executes, so the running
instance keeps following the version it started with. Python files are read when
their process starts — which happens *after* the pull — so they DO get the new
code immediately.

Net effect: after one power-cycle, `boat_server.py` was on the new commit
(`/api/health` existed) while `startup.sh` was still the old one (no boot log, no
logger supervision, SSH key not installed). It looks like a partial or corrupted
deploy and is neither.

**Anything you change in `startup.sh`, `start_boat.sh`, or the systemd unit takes
effect on the boot *after* the one that pulls it.** Two power-cycles, or verify
with something the new shell code produces — `logs/startup.log` existing is the
cheap tell.

## "The logger is dead" and "the logger has nothing to log" look identical [P39]

On 2026-09-13 recording stopped and the first conclusion was that
`nmea_capture.py` had crashed: `:8081` was refusing connections and the log file
had frozen. It had not crashed — it was running, connected, and receiving zero
sentences, because the NMEA source had stopped sending.

The two states are indistinguishable from outside unless something reports the
*feed*, not just the process and the file. `/api/health` therefore reports
`nmea.lines`, `nmea.last_line_age_s` and the source `host:port` alongside logger
liveness, and `recording` is computed from whether the newest log is actually
growing — not from whether a process exists.

Note the subtlety in how those line counts are collected: `nmea_tcp_broadcast`
invokes `send_fn` once per connected client, so counting inside the send path
sees nothing when no browser is attached — precisely the situation you are
debugging. A stats-only pseudo-client sits permanently in the client snapshot so
every line is observed regardless.

Also: the receiver answering ARP proves its network interface is powered, and
nothing more. An MDA-5 with the instrument bus off is present on the LAN and
silent on TCP.

---

## Adding a pitfall

Writeup goes here under a new `## <one-line title> [Pnn]` heading with the next unused number.
[CLAUDE.md](../CLAUDE.md#critical-pitfalls--do-not-re-investigate) gets **at most one ~15-word
line** ending in that ID, filed under the "Touching …" group for the code area it traps. If a group
passes ~15 lines, merge overlapping entries rather than letting the index grow without bound.

State the cost. "X is wrong, do Y" is a rule; "X shipped a three-week silent outage, because Z" is
a reason someone won't undo it.
