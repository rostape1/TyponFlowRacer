# Vessel names

Why a contact shows as `MMSI 368309230` instead of a name, and what the local
database does about it.

---

## The problem

AIS separates identity from position, and broadcasts them at wildly different rates.

| Message | Carries | Rate |
|---|---|---|
| Type 1/2/3 (Class A), 18/19 (Class B) | position, SOG, COG | every 2-10 s |
| **Type 5** (Class A) | **name**, callsign, dimensions | every ~6 min |
| **Type 24** (Class B) | **name** (part A), dimensions (part B) | rarer still |

So a vessel appears on the map long before it says who it is. Tune in halfway
through a Type 5 cycle and you wait minutes for a name.

**Replay makes it worse.** `startReplay()` and `seek()` reset the store, so names
accumulate only from the start of the *current* file — and at every auto-advance
boundary they are dropped and must be learned again. Scrubbing back to the start
of a race threw away every name.

Measured on the 84-file archive:

| | |
|---|---|
| Distinct vessels seen | 824 |
| Broadcast a name at some point | 765 (92.8%) |
| **Never broadcast a name** | **59 (7.2%)** |

That last row is the part no amount of listening fixes.

---

## The database

`static/js/vessel-names.js`. Three layers, highest precedence first:

| Layer | Source | Persisted in |
|---|---|---|
| `MANUAL` | typed or pasted by the operator — **no UI yet, console only** | `localStorage.vesselNamesManual` |
| `LEARNED` | heard from AIS, this session or an earlier one | `localStorage.vesselNamesLearned` |
| `SEED` | `static/vessel_names.json`, built from the archive | the file itself |

**Manual outranks even a name currently being received.** Overriding a garbled or
wrong AIS name is the entire reason to type one, so a later broadcast must not
silently undo the correction. **Learned beats seed** because the receiver is more
current than a file built weeks ago.

### Resolution happens at display time, not in the store

`vessel-store.js` keeps recording only what was actually received; the database is
a lookup consulted by `vesselLabel()` in `app.js` and by `competitor-labels.js`.

This is deliberate. Writing resolved names into vessel records would persist them
to `localStorage.vesselStore`, where they would be indistinguishable from data the
receiver really sent — the "looks live and isn't" failure this project is organised
around.

### Names are learned during replay, on purpose

`vessel-store.js` refuses to persist anything while replaying, so a historical
contact cannot come back as live traffic. Names are the one deliberate exception:
**identity is timeless, position is not.** That `368309230` is FINAL FINAL is as
true today as it was in June; where she was in June is exactly what must not leak
into the live picture. So replaying old races improves the database.

`VesselNames.learn()` is called from `upsert()` — the single choke point every AIS
message passes, live or replayed — rather than from the four separate `upsert`
call sites, which could drift.

### P19 in attribute context

Vessel labels are interpolated into popup HTML **and into `title="…"` /
`aria-label="…"` attributes**. The AIS 6-bit charset maps values 32-63 straight
through, so `"` (34), `'` (39) and `&` (38) are all legal name bytes from a
checksum-valid Type 5 — no attacker required, a bit error will do.

Attribute context needs no angle bracket to break out of. A name of
`X" onclick=alert(1)` is 19 characters, fits the 20-char Type 5 name field, and
would escape an `aria-label` — then persist into `localStorage` and the seed file
and fire on every load. **Stripping `<>` does nothing about this**, which is why
the first version of this feature was wrong while asserting it was safe.

So: `cleanName()` normalises (strips `@` padding, control characters and angle
brackets, collapses whitespace, caps at 64 chars) but deliberately **keeps quotes
and ampersands** — real vessels are called `BAIT & STITCH` and `O'BRIEN`, and
mangling a name is its own wrong answer on a navigation display. Escaping is
`escapeHtml()`'s job at the point of interpolation: `app.js` uses
`vesselLabelHtml()` at every HTML site and plain `vesselLabel()` only for
comparison. `tests/test_vessel_names.mjs` renders the real `aria-label` string and
asserts the payload stays inert inside it.

Label sites: the popup, the side panel, the vessel-toggle `aria-label`, the search
filter (`app.js`), the competitor tooltip (`competitor-labels.js`) and the radar
(`radar-view.js` — `textContent`, so no escaping needed there).

### Everything degrades to an MMSI

The seed file is an optimisation, never a dependency. A 404 on a fresh deploy, no
copy on the boat, a full `localStorage`, private browsing, corrupt stored JSON — all
fall back to `MMSI 368309230`. Nothing throws: `learn()` runs on the AIS message
path and must never take the feed down. Asserted in `tests/test_vessel_names.mjs`.

---

## Building the seed

```bash
node tools/build_vessel_names.mjs [LOG_DIR] [-o OUT]
```

Defaults to `~/Documents/typon-nmea-logs` → `static/vessel_names.json`. Re-run
after pulling new recordings; keys are sorted so the committed diff stays readable.

It reuses `static/js/ais-decoder.js` rather than reimplementing the decoder, which
is why it is Node and not Python. A second AIS decoder in another language would be
two implementations that must agree forever — the trap `P20` is filed against.

**The file lives in `static/`, not `data/`.** `data/` is not in git and
`static/data/` is gitignored, so a seed placed in either would silently never reach
the Pi. `static/` is served directly by both GitHub Pages and the Pi's
`add_static`, which means no proxy route and no pre-warm entry.

Current build: 84 files, 3.6 M AIVDM lines → **774 names, 22 KB**.

The file reports three deliberately distinct counts, because conflating them is how
two different "coverage" figures got into circulation:

| Field | Meaning | Value |
|---|---|---|
| `vessels_seen` | distinct MMSIs seen **with a position** | 824 |
| `named_with_position` | ...of which a name is known — **this is coverage** | 765 (92.8%) |
| `never_named` | seen, but never broadcast a name | 59 (7.2%) |
| `names_known` | total names in the file, including 9 MMSIs seen without a position | 774 |

---

## Measured effect

Early in a replay of `nmea_2026-09-19_095204.txt`, 284 vessels on screen:

| | Before | After |
|---|---|---|
| Name received in-stream | 209 | 209 |
| Resolved from the database | — | **74** |
| Bare MMSI | 75 | **1** |
| Coverage | 73.6% | **99.6%** |

222 names were learned and persisted during that same run.

---

## Not automated: online lookup

An online lookup for the 7% that never broadcast a name is **not implemented**, and
MarineTraffic specifically is ruled out for three independent reasons:

- **Cloudflare 403** on the first automated request, measured 2026-09-23.
- **No `access-control-allow-origin`**, so a browser could not read a 200 either.
- Their terms forbid scraping.

`VesselNames.parseVesselRef()` therefore accepts a *pasted* vessel-details URL —
which already contains both MMSI and name, so nothing is fetched — or a plain
`368309230 Final Final`.

> **Not yet wired to any UI.** `parseVesselRef`, `setManual`, `forgetManual`,
> `forgetLearned` and `source` are API-only: they work from the devtools console
> and nowhere else. `source()` in particular exists so the popup can eventually
> distinguish a name resolved from a weeks-old file from one just received, which
> matters on a collision-avoidance display — but nothing renders it today.

A real lookup would need a source that is both legitimate and CORS-capable (or
proxied). `data.fcc.gov` is the strongest candidate: free, public, and US MMSIs are
366-369. It was unreachable from the development network when this was written, so
nothing was built against it rather than guessing at a response shape.
