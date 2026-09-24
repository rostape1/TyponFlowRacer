/**
 * Local MMSI→name database.
 *
 * AIS separates identity from position. A name arrives in a Type 5 (Class A,
 * roughly every 6 minutes) or a Type 24 (Class B, rarer still), while position
 * reports arrive every 2-10 seconds. So a vessel shows as a bare MMSI until its
 * next static broadcast — and in replay it is worse, because startReplay/seek
 * reset the store, so names only accumulate from the start of the current file
 * and are dropped at every auto-advance boundary.
 *
 * Measured coverage and the rest of the rationale: docs/vessel-names.md.
 *
 * Three layers, highest precedence first:
 *
 *   MANUAL   typed or pasted by the operator. Wins over everything, including a
 *            name currently being received — overriding a garbled or wrong AIS
 *            name is the entire reason to type one. **No UI yet: console only.**
 *   LEARNED  heard from AIS, this session or an earlier one. Beats the seed file,
 *            which may be weeks old.
 *   SEED     static/vessel_names.json, built from the archive by
 *            tools/build_vessel_names.mjs.
 *
 * Deliberately NOT in vessel-store.js. The store records what was actually
 * received; this is a lookup. Writing cached names into vessel records would
 * persist them to localStorage.vesselStore, where they would be
 * indistinguishable from data the receiver really sent.
 */

const VesselNames = (() => {
    const SEED = new Map();
    const LEARNED = new Map();   // mmsi -> [name, lastSeenEpochDay]
    const MANUAL = new Map();    // mmsi -> name

    const KEY_LEARNED = 'vesselNamesLearned';
    const KEY_MANUAL = 'vesselNamesManual';

    /**
     * Cap on LEARNED, evicting least-recently-seen.
     *
     * Unbounded was the original design and it was wrong for the reason P11
     * documents about the disk cache: the quota is shared, and this is the
     * cosmetic consumer. localStorage is ~5 MB per origin and vessel-store.js
     * ALSO lives there. Overflowing would silently stop vessel tracks
     * persisting, which matters far more than a remembered name.
     *
     * The entries carry a last-seen day precisely so a cap can choose what to
     * drop. Without it the only possible eviction is "discard everything".
     */
    const MAX_LEARNED = 5000;

    // Writes are debounced. learn() is called from vessel-store.js upsert() —
    // the AIS hot path — and seek() re-ingests up to 150k lines synchronously in
    // one frame. Serialising the whole map per learned name cost 72 ms of
    // JSON.stringify and 11.1 MB of garbage over 800 learns, which is P37's
    // coalescing problem reappearing one layer down.
    const FLUSH_DELAY_MS = 2000;
    let learnedDirty = false;
    let manualDirty = false;
    let flushTimer = null;
    let seedLoaded = false;

    function today() { return Math.floor(Date.now() / 86400000); }

    /** An MMSI is exactly 9 digits. Anything else is not a key we will store. */
    function validMmsi(mmsi) {
        const s = String(mmsi == null ? '' : mmsi).trim();
        return /^\d{9}$/.test(s) ? s : null;
    }

    /**
     * Normalise a name, or return null if there is nothing left.
     *
     * AIS pads fixed-width text with '@'; ais-decoder.js strips trailing padding
     * but some encoders leave it mid-string. Control characters are removed
     * because they corrupt any label or log line they reach.
     *
     * Quotes and ampersands are deliberately KEPT — real vessels are called
     * BAIT & STITCH and O'BRIEN, and mangling a name is its own kind of wrong
     * answer on a navigation display. Rendering safety is escapeHtml()'s job at
     * the point of interpolation, not this function's.
     *
     * Angle brackets are still dropped as belt and braces; nothing is legitimately
     * named with them.
     *
     * NOTE: written with \x escapes on purpose. Literal control bytes here made
     * the whole file binary to grep(1), so `grep -rnE 'P[0-4][0-9]' static/js/`
     * — the pitfall-discovery command CLAUDE.md prescribes — silently skipped it.
     */
    function cleanName(raw) {
        if (typeof raw !== 'string') return null;
        const s = raw
            .replace(/@+/g, ' ')
            .replace(/[\x00-\x1f\x7f-\x9f]/g, '')
            .replace(/[<>]/g, '')
            .replace(/\s+/g, ' ')
            .trim();
        return s.length ? s.slice(0, 64) : null;
    }

    /**
     * Escape for interpolation into HTML, including inside a quoted attribute.
     *
     * Required, not optional. app.js interpolates labels into `title="…"` and
     * `aria-label="…"`, and the AIS 6-bit charset maps values 32-63 straight
     * through (ais-decoder.js), so `"` (34), `'` (39) and `&` (38) are all legal
     * name bytes from a checksum-valid Type 5. Stripping `<>` does nothing for
     * attribute context: `X" onclick=alert(1)` needs no new tag, fits the 20-char
     * name field, and would persist into localStorage and the seed file to fire
     * on every load. This is the P19 sink, one layer out from the legend.
     */
    function escapeHtml(s) {
        return String(s == null ? '' : s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function readStore(key) {
        try {
            const raw = localStorage.getItem(key);
            if (!raw) return;
            const obj = JSON.parse(raw);
            if (!obj || typeof obj !== 'object') return;
            for (const k of Object.keys(obj)) {
                const m = validMmsi(k);
                if (!m) continue;
                if (key === KEY_MANUAL) {
                    const n = cleanName(obj[k]);
                    if (n) MANUAL.set(m, n);
                } else {
                    // Accept both shapes: "NAME" (original) and [name, day].
                    const v = obj[k];
                    const n = cleanName(Array.isArray(v) ? v[0] : v);
                    const d = Array.isArray(v) && Number.isFinite(v[1]) ? v[1] : today();
                    if (n) LEARNED.set(m, [n, d]);
                }
            }
        } catch (e) {
            // Corrupt or unavailable storage must not stop the app booting. A
            // forgotten name is cosmetic; a blank map is still usable.
        }
    }

    function writeNow(key) {
        try {
            const out = {};
            if (key === KEY_MANUAL) {
                for (const [k, v] of MANUAL) out[k] = v;
            } else {
                for (const [k, v] of LEARNED) out[k] = v;
            }
            localStorage.setItem(key, JSON.stringify(out));
        } catch (e) {
            // Quota exceeded, or private browsing. The in-memory map still works
            // for this session. This runs off the AIS path and must never throw.
        }
    }

    /** Flush any pending writes immediately. Called on pagehide and by tests. */
    function flush() {
        if (flushTimer) { clearTimeout(flushTimer); flushTimer = null; }
        if (learnedDirty) { writeNow(KEY_LEARNED); learnedDirty = false; }
        if (manualDirty) { writeNow(KEY_MANUAL); manualDirty = false; }
    }

    function scheduleFlush() {
        if (flushTimer) return;
        flushTimer = setTimeout(() => { flushTimer = null; flush(); }, FLUSH_DELAY_MS);
    }

    readStore(KEY_LEARNED);
    readStore(KEY_MANUAL);

    // Nothing learned may be lost to a close or a tab switch. pagehide is the
    // one event that fires reliably on mobile Safari, which is what the boat
    // actually runs.
    if (typeof addEventListener === 'function') {
        addEventListener('pagehide', flush);
        addEventListener('visibilitychange', () => {
            if (typeof document !== 'undefined' && document.visibilityState === 'hidden') flush();
        });
    }

    function evictIfNeeded() {
        if (LEARNED.size <= MAX_LEARNED) return;
        // Least-recently-seen first. Only runs on overflow, so the sort cost is
        // paid once per thousands of inserts.
        const byAge = [...LEARNED.entries()].sort((a, b) => a[1][1] - b[1][1]);
        for (let i = 0; i < LEARNED.size - MAX_LEARNED; i++) LEARNED.delete(byAge[i][0]);
    }

    /**
     * Record a name heard from AIS.
     *
     * Learned during replay too, unlike anything in vessel-store.js, and that
     * divergence is deliberate: identity is timeless, position is not. That
     * 368309230 is FINAL FINAL is as true today as it was in June; where she was
     * in June is emphatically not.
     *
     * But replaying an old log must not OVERWRITE a newer name. A vessel gets
     * renamed, today's feed teaches the new name, you replay June — and without
     * the day stamp the June name wins and sticks, permanently, with nothing in
     * the UI saying so. So an observation only replaces a newer one when it is
     * at least as recent.
     */
    function learn(mmsi, name, seenDay) {
        const m = validMmsi(mmsi);
        const n = cleanName(name);
        if (!m || !n) return false;
        if (MANUAL.has(m)) return false;          // never clobber an override
        const day = Number.isFinite(seenDay) ? seenDay : today();
        const cur = LEARNED.get(m);
        if (cur) {
            if (cur[0] === n) {                   // same name: refresh recency only
                if (day > cur[1]) { cur[1] = day; learnedDirty = true; scheduleFlush(); }
                return false;
            }
            if (day < cur[1]) return false;       // an older sighting must not win
        }
        LEARNED.set(m, [n, day]);
        evictIfNeeded();
        learnedDirty = true;
        scheduleFlush();
        return true;
    }

    function setManual(mmsi, name) {
        const m = validMmsi(mmsi);
        const n = cleanName(name);
        if (!m || !n) return false;
        MANUAL.set(m, n);
        manualDirty = true;
        flush();                                  // an operator action: persist now
        return true;
    }

    function forgetManual(mmsi) {
        const m = validMmsi(mmsi);
        if (!m || !MANUAL.has(m)) return false;
        MANUAL.delete(m);
        manualDirty = true;
        flush();
        return true;
    }

    /** Drop a learned name — the correction path for one bad AIS burst. */
    function forgetLearned(mmsi) {
        const m = validMmsi(mmsi);
        if (!m || !LEARNED.has(m)) return false;
        LEARNED.delete(m);
        learnedDirty = true;
        flush();
        return true;
    }

    /** The best known name for an MMSI, or null. */
    function get(mmsi) {
        const m = validMmsi(mmsi);
        if (!m) return null;
        const l = LEARNED.get(m);
        return MANUAL.get(m) || (l && l[0]) || SEED.get(m) || null;
    }

    /** Where a name came from: 'manual' | 'learned' | 'seed' | null. */
    function source(mmsi) {
        const m = validMmsi(mmsi);
        if (!m) return null;
        if (MANUAL.has(m)) return 'manual';
        if (LEARNED.has(m)) return 'learned';
        if (SEED.has(m)) return 'seed';
        return null;
    }

    /**
     * The label to show for a vessel. Always returns something: a blank label is
     * worse than an MMSI, and "MMSI undefined" is worse than both.
     *
     * The MMSI fallback uses the RAW value, not the validated one. Coast and base
     * stations have identities that are not 9 digits (leading zeros are lost when
     * the decoder returns an integer), and rendering several of those as an
     * identical "Unknown vessel" on a collision-avoidance display is worse than
     * showing an odd number.
     */
    function displayName(vessel) {
        if (!vessel) return 'Unknown vessel';
        const m = validMmsi(vessel.mmsi);
        if (m && MANUAL.has(m)) return MANUAL.get(m);
        const received = cleanName(vessel.name) || cleanName(vessel.shipname);
        if (received) return received;
        if (m && get(m)) return get(m);
        const raw = vessel.mmsi == null ? '' : String(vessel.mmsi).trim();
        return raw ? `MMSI ${raw}` : 'Unknown vessel';
    }

    /**
     * Pull an MMSI and name out of pasted text.
     *
     * Accepts a vessel-details URL — which already carries both, so nothing is
     * ever fetched — or a plain "368309230 Final Final". Nothing here touches the
     * network: a browser cannot read a cross-origin response from those sites
     * anyway (no CORS), and scraping them would breach their terms.
     */
    function parseVesselRef(text) {
        if (typeof text !== 'string' || !text.trim()) return null;
        const s = text.trim();

        // [^\n]*? not [^]*?: the latter spans newlines, so pasting two vessel
        // URLs at once paired the first MMSI with the second vessel's name.
        const url = /mmsi[:/=](\d{9})[^\n]*?vessel[:/=]([^/?#\s]+)/i.exec(s);
        if (url) {
            let raw = url[2];
            try { raw = decodeURIComponent(raw); } catch (e) { /* keep as-is */ }
            const name = cleanName(raw.replace(/_/g, ' '));
            if (name) return { mmsi: url[1], name };
        }

        const plain = /^(\d{9})\s+(.+)$/.exec(s);
        if (plain) {
            const name = cleanName(plain[2]);
            if (name) return { mmsi: plain[1], name };
        }
        return null;
    }

    /**
     * Load the seed file. Failure is not an error: it is an optimisation, and on
     * a fresh deploy or a boat with no copy the app must fall back to MMSI labels.
     *
     * Promise.resolve().then(...) wraps the fetch so a SYNCHRONOUS throw (a bad
     * URL, or fetch missing entirely) becomes a rejection this chain can catch,
     * rather than escaping to the caller. Do not "simplify" it away.
     */
    function load(url = 'vessel_names.json') {
        return Promise.resolve()
            .then(() => fetch(url, { cache: 'no-cache' }))
            .then((r) => (r && r.ok ? r.json() : null))
            .then((d) => {
                const names = d && d.names;
                if (!names || typeof names !== 'object') return 0;
                let n = 0;
                for (const k of Object.keys(names)) {
                    const m = validMmsi(k);
                    const v = cleanName(names[k]);
                    if (m && v) { SEED.set(m, v); n++; }
                }
                seedLoaded = n > 0;
                return n;
            })
            .catch(() => 0);
    }

    function stats() {
        return {
            seed: SEED.size, learned: LEARNED.size, manual: MANUAL.size,
            seedLoaded, pendingWrite: learnedDirty || manualDirty,
        };
    }

    /** Test seam: populate the seed layer without a fetch. */
    function _setSeedForTest(obj) {
        SEED.clear();
        for (const k of Object.keys(obj || {})) {
            const m = validMmsi(k), v = cleanName(obj[k]);
            if (m && v) SEED.set(m, v);
        }
    }

    return {
        load, get, source, learn, setManual, forgetManual, forgetLearned,
        displayName, parseVesselRef, escapeHtml, flush, stats,
        _setSeedForTest, MAX_LEARNED,
    };
})();
