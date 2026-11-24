/**
 * Regression tests for SFBOFS data honesty in data-loader.js / app.js.
 *
 * Two related invariants, both about never showing the user stale or wrong
 * data: the staleness gate (don't serve a dead model run) and the download
 * sweep (don't silently truncate coverage, and don't report a reach number
 * that gaps would make wrong).
 *
 * Guards a bug class that has now recurred twice:
 *   1. A stale model run made every forecast offset alias to hour_48, so NOW
 *      and +4h rendered an identical current field (the original report).
 *   2. The first fix refused to fetch when the *cached* run time was old.
 *      _sfbofsRunTime is seeded from localStorage, so a browser carrying a
 *      dead run never fetched, never learned the server had a fresh run, and
 *      the flow layer stayed permanently empty.
 *
 * Loads data-loader.js in a `new Function` sandbox with a stubbed fetch, so no
 * network and no browser required. Run: node tests/test_staleness.mjs
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const src = readFileSync(join(ROOT, 'static/js/data-loader.js'), 'utf8');

let pass = 0, fail = 0;
function check(name, ok, detail = '') {
    if (ok) { pass++; console.log(`  ok   ${name}`); }
    else { fail++; console.log(`  FAIL ${name}${detail ? ' — ' + detail : ''}`); }
}

// Build a fresh module instance. `seed` is the localStorage model_run (a
// previous session's cached value); `serverRun` is what the files actually say.
function build({ seed, serverRun }) {
    const fetched = [];
    const sb = {
        localStorage: { getItem: () => JSON.stringify(seed ? { flow_model_run: seed } : {}) },
        console, Date, Math, JSON, Map, Set, Array, Number, String, Object,
        parseInt, parseFloat, isNaN, AbortController, setTimeout, clearTimeout, Promise,
        window: undefined,
        fetch: async (url) => {
            fetched.push(url);
            const m = url.match(/hour_(\d+)\.json/);
            return {
                ok: true, headers: { get: () => null },
                json: async () => ({
                    model_run: serverRun, forecast_hour: m ? Number(m[1]) : null,
                    u: [[1]], v: [[1]],
                }),
            };
        },
    };
    const keys = Object.keys(sb);
    const api = new Function(...keys,
        src + '\n; return { fetchCurrentField, fetchCurrentFieldHighRes };'
    )(...keys.map(k => sb[k]));
    return { api, fetched };
}

const STALE = 't15z 07/21';   // the dead run from the 2026-08 outage

// Must stay within SFBOFS_RUN_STALE_HOURS of the moment the test runs, so
// build it from the current time instead of a hardcoded date. A fixed
// calendar date "looks fresh" only until real time catches up to it — that
// drift is exactly what broke this test suite (and silently blocked every
// CI deploy) for a day and a half in 2026-08.
const _freshRunTime = new Date(Date.now() - 2 * 3600000);
const FRESH = `t${String(_freshRunTime.getUTCHours()).padStart(2, '0')}z ` +
    `${String(_freshRunTime.getUTCMonth() + 1).padStart(2, '0')}/${String(_freshRunTime.getUTCDate()).padStart(2, '0')}`;

console.log('Staleness gate (P01):');

// The deadlock: a stale localStorage seed must not prevent discovering the
// fresh run on the server.
{
    const { api } = build({ seed: STALE, serverRun: FRESH });
    const r = await api.fetchCurrentField(0);
    check('stale localStorage seed still serves fresh server data',
        !r.unavailable && !r.stale, JSON.stringify({ stale: r.stale, age: r.runAgeHours }));
}

// The original bug: distinct forecast offsets must resolve to distinct hours.
{
    const { api } = build({ seed: null, serverRun: FRESH });
    const now = await api.fetchCurrentField(0);
    const p4 = await api.fetchCurrentField(240);
    check('NOW and +4h resolve to different forecast hours',
        now.forecast_hour !== p4.forecast_hour,
        `both ${now.forecast_hour}`);
}

// The guard must still fire when the pipeline really is dead.
{
    const { api } = build({ seed: STALE, serverRun: STALE });
    const r = await api.fetchCurrentField(0);
    check('genuinely stalled pipeline is refused', r.stale === true && r.runAgeHours > 12);
}

console.log('High-res path — router consumes this (P01):');

{
    const { api } = build({ seed: STALE, serverRun: STALE });
    const r = await api.fetchCurrentFieldHighRes(0);
    check('high-res refuses a stalled run', r === null);
}
{
    const { api, fetched } = build({ seed: STALE, serverRun: FRESH });
    await api.fetchCurrentFieldHighRes(0);
    check('high-res never aliases to hour_48 on a stale seed',
        !fetched.some(u => u.includes('sfbofs_gg') && u.includes('hour_48')));
}

console.log('Concurrency (router preloads 49 hours at once):');

for (const [label, seed] of [['cold', null], ['stale seed', STALE]]) {
    const { api, fetched } = build({ seed, serverRun: FRESH });
    await Promise.all(Array.from({ length: 49 }, (_, h) => api.fetchCurrentField(h * 60)));
    // 49 hours + at most 1 shared run-time discovery. More than that means the
    // memoization broke and every caller is re-discovering independently.
    check(`${label}: 49 concurrent calls stay within 50 requests`,
        fetched.length <= 50, `${fetched.length} requests`);
}

// --- Download sweep: transient faults must not truncate coverage ---

// behave: (hour, attemptIndex) -> 200 | 404 | 500 | 'throw'
function buildDl(behave) {
    const attempts = {};
    const sb = {
        localStorage: { getItem: () => '{}', setItem: () => {} },
        console: { log() {}, warn() {} },
        Date, Math, JSON, Map, Set, Array, Number, String, Object,
        parseInt, parseFloat, isNaN, AbortController, setTimeout, clearTimeout, Promise,
        window: undefined,
        fetch: async (url) => {
            const m = url.match(/hour_(\d+)\.json/);
            if (!m) return { ok: true, status: 200, headers: { get: () => null }, json: async () => ({}) };
            const h = Number(m[1]);
            attempts[h] = (attempts[h] || 0) + 1;
            const v = behave(h, attempts[h]);
            if (v === 'throw') throw new Error('network');
            if (v === 404) return { ok: false, status: 404, headers: { get: () => null } };
            if (v === 500) return { ok: false, status: 500, headers: { get: () => null } };
            return { ok: true, status: 200, headers: { get: () => null },
                     json: async () => ({ model_run: 't21z 08/12', forecast_hour: h }) };
        },
    };
    const keys = Object.keys(sb);
    const api = new Function(...keys, src + '\n; return { downloadAllForOffline };')(...keys.map(k => sb[k]));
    return { api, attempts };
}
async function sweep(behave) {
    const { api, attempts } = buildDl(behave);
    let flow = null;
    await api.downloadAllForOffline(null, (cat, v) => { if (cat === 'flow') flow = v; });
    return { flow, attempts };
}

console.log('Download sweep (P03):');
{
    const { flow } = await sweep(() => 200);
    check('gapless run caches all 49 hours',
        flow.count === 49 && flow.maxHour === 48 && flow.gaps.length === 0);
}
{
    // A 404 means the run genuinely produced fewer hours — stop, don't call it a gap.
    const { flow, attempts } = await sweep((h) => h >= 20 ? 404 : 200);
    check('404 breaks cleanly and is not a gap',
        flow.count === 20 && flow.maxHour === 19 && flow.gaps.length === 0);
    check('404 is not retried', attempts[20] === 1);
}
{
    const { flow, attempts } = await sweep((h, a) => (h === 20 && a === 1) ? 'throw' : 200);
    check('transient fault is retried once and recovers',
        flow.count === 49 && flow.gaps.length === 0);
    check('the retry actually happened', attempts[20] === 2);
}
{
    // The original bug: one blip used to cap coverage at that hour.
    const { flow } = await sweep((h) => h === 20 ? 500 : 200);
    check('persistent fault records a gap and keeps going',
        flow.maxHour === 48 && flow.count === 48 && flow.gaps.join() === '20',
        JSON.stringify({ max: flow.maxHour, gaps: flow.gaps }));
}
{
    const { flow } = await sweep((h) => (h === 5 || h === 30) ? 500 : 200);
    check('every gap is recorded', flow.gaps.join() === '5,30', flow.gaps.join());
}
{
    const { flow } = await sweep(() => 'throw');
    check('total failure reports false, not success-with-gaps', flow === false);
}

// --- Gauge vs prediction: compare at the reading's time, reject old readings (P44) ---
// The bug: a 3-hour-old reading (served by the SW) was compared with the
// prediction for NOW. On a rising king tide that said "1.8 ft lower" when the
// gauges were 1.2 ft higher.
console.log('Gauge vs prediction (P44):');
{
    const sb = {
        localStorage: { getItem: () => '{}' },
        console, Date, Math, JSON, Map, Set, Array, Number, String, Object,
        parseInt, parseFloat, isNaN, AbortController, setTimeout, clearTimeout, Promise,
        window: undefined, fetch: async () => { throw new Error('no network in this test'); },
    };
    const keys = Object.keys(sb);
    const { compareGaugeToPrediction, _tideCache, WATER_LEVEL_MAX_AGE_MS } = new Function(...keys,
        src + '\n; return { compareGaugeToPrediction, _tideCache, WATER_LEVEL_MAX_AGE_MS };'
    )(...keys.map(k => sb[k]));

    // A tide rising 1 ft/hour: 2.0 ft at 16:00Z up to 6.0 ft at 20:00Z, every 6 min.
    const predictions = [];
    for (let m = 0; m <= 240; m += 6) {
        const t = new Date(Date.UTC(2026, 10, 24, 16, m));
        predictions.push({ t: t.toISOString().slice(0, 16).replace('T', ' '), v: (2 + m / 60).toFixed(3) });
    }
    _tideCache.set('9414290', { predictions, fetchedAt: Date.now() });
    const now = Date.UTC(2026, 10, 24, 19, 48);

    // Fresh reading, 1.2 ft above its own-time prediction (5.7 at 19:42).
    const fresh = compareGaugeToPrediction('9414290', { value: 6.9, time: '2026-11-24 19:42' }, now);
    check('fresh reading is compared with the prediction at its own time',
        fresh && Math.abs(fresh.predicted - 5.7) < 0.01, JSON.stringify(fresh));
    check('fresh reading: delta is +1.2 (higher)', fresh && Math.abs(fresh.delta - 1.2) < 0.01, JSON.stringify(fresh));
    check('fresh reading is marked fresh', fresh && fresh.fresh === true);
    check('age is reported in minutes', fresh && fresh.ageMin === 6, JSON.stringify(fresh));

    // The bug's input: a reading from 16:42, also 1.2 ft above ITS prediction (2.7).
    // Against NOW's prediction (5.8) it would read 3.1 ft "lower".
    const old = compareGaugeToPrediction('9414290', { value: 3.9, time: '2026-11-24 16:42' }, now);
    check('3-hour-old reading is marked stale', old && old.fresh === false, JSON.stringify(old));
    check('stale reading still compares at its own time, never at now',
        old && Math.abs(old.delta - 1.2) < 0.01, JSON.stringify(old));

    check('limit is 30 minutes', WATER_LEVEL_MAX_AGE_MS === 30 * 60000);
    const edge = compareGaugeToPrediction('9414290', { value: 5, time: '2026-11-24 19:18' }, now);
    check('exactly 30 min old is still fresh', edge && edge.fresh === true, JSON.stringify(edge));
    const over = compareGaugeToPrediction('9414290', { value: 5, time: '2026-11-24 19:17' }, now);
    check('31 min old is stale', over && over.fresh === false, JSON.stringify(over));

    const before = compareGaugeToPrediction('9414290', { value: 5, time: '2026-11-24 15:30' }, now);
    check('reading outside the prediction window has no delta (no nearest-value guess)',
        before && before.predicted === null && before.delta === null, JSON.stringify(before));
    check('a station with no predictions has no delta',
        (compareGaugeToPrediction('9999999', { value: 5, time: '2026-11-24 19:42' }, now) || {}).delta === null);
    check('an unparseable time returns null',
        compareGaugeToPrediction('9414290', { value: 5, time: 'garbage' }, now) === null);

    // Device clock behind the gauge: an old cached reading must not pass as live.
    const ahead5 = compareGaugeToPrediction('9414290', { value: 5, time: '2026-11-24 19:53' }, now);
    check('5 min in the future is still fresh (clock jitter)', ahead5 && ahead5.fresh === true && !ahead5.clockMismatch, JSON.stringify(ahead5));
    const ahead40 = compareGaugeToPrediction('9414290', { value: 5, time: '2026-11-24 20:28' }, now);
    check('40 min in the future is NOT fresh', ahead40 && ahead40.fresh === false, JSON.stringify(ahead40));
    check('40 min in the future is flagged as a clock mismatch', ahead40 && ahead40.clockMismatch === true, JSON.stringify(ahead40));
}

// --- The legend verdict (P44): only fresh readings, compared at their own time ---
console.log('Flow-confidence verdict (P44):');
{
    const sb = {
        localStorage: { getItem: () => '{}' },
        console, Date, Math, JSON, Map, Set, Array, Number, String, Object,
        parseInt, parseFloat, isNaN, AbortController, setTimeout, clearTimeout, Promise,
        window: undefined, fetch: async () => { throw new Error('no network'); },
    };
    const keys = Object.keys(sb);
    const { flowConfidenceVerdict } = new Function(...keys,
        src + '\n; return { flowConfidenceVerdict };')(...keys.map(k => sb[k]));
    const G = (delta, extra = {}) => ({ gauge: { delta, fresh: true, ageMin: 6, clockMismatch: false, ...extra } });

    // The king-tide case: fresh gauges 0.5..1.2 ft higher.
    let v = flowConfidenceVerdict([G(1.23), G(1.13), G(1.17), G(0.70), G(0.53)]);
    check('king tide: says HIGHER', v.sign === 'higher', JSON.stringify(v));
    check('king tide: low confidence (avg > 0.5)', v.level === 'low' && Math.abs(v.avgDelta - 0.952) < 0.01, JSON.stringify(v));

    v = flowConfidenceVerdict([G(-0.4), G(-0.4)]);
    check('lower, moderate band', v.level === 'moderate' && v.sign === 'lower', JSON.stringify(v));
    v = flowConfidenceVerdict([G(0.1), G(-0.2)]);
    check('within 0.3 is high confidence', v.level === 'high', JSON.stringify(v));

    // Stale readings never vote — the bug's shape: an old reading that looks very low.
    v = flowConfidenceVerdict([G(0.2), G(-3.0, { fresh: false, ageMin: 190 })]);
    check('a stale reading is excluded from the verdict', v.level === 'high' && v.used === 1, JSON.stringify(v));

    v = flowConfidenceVerdict([G(-3.0, { fresh: false, ageMin: 190 }), G(-2.0, { fresh: false, ageMin: 185 })]);
    check('all stale: unavailable, reason stale', v.level === 'unavailable' && v.reason === 'stale', JSON.stringify(v));
    check('all stale: reports the newest age', v.newestAgeMin === 185, JSON.stringify(v));

    v = flowConfidenceVerdict([{ name: 'no gauge' }, { name: 'none either' }]);
    check('no readings at all: unavailable, reason no-readings (never blank)', v.level === 'unavailable' && v.reason === 'no-readings', JSON.stringify(v));
    v = flowConfidenceVerdict([]);
    check('no stations at all: unavailable, reason no-readings', v.level === 'unavailable' && v.reason === 'no-readings', JSON.stringify(v));

    v = flowConfidenceVerdict([G(null), G(null)]);
    check('fresh but outside the prediction window: reason no-prediction, not stale', v.level === 'unavailable' && v.reason === 'no-prediction', JSON.stringify(v));

    v = flowConfidenceVerdict([G(1.0, { fresh: false, clockMismatch: true, ageMin: -40 })]);
    check('clock mismatch only: reason clock', v.level === 'unavailable' && v.reason === 'clock', JSON.stringify(v));
}

// --- fetchWaterLevels keeps the last reading when a refresh fails (P44) ---
// Dropping it turned "verdict" into a blank legend; kept, it shows as stale.
console.log('Water-level refresh failure (P44):');
{
    let mode = 'ok';
    const sb = {
        localStorage: { getItem: () => '{}' },
        console, Date, Math, JSON, Map, Set, Array, Number, String, Object,
        parseInt, parseFloat, isNaN, AbortController, setTimeout, clearTimeout, Promise,
        window: undefined,
        fetch: async () => {
            if (mode === 'throw') throw new TypeError('offline');
            if (mode === '503') return { ok: false, status: 503, json: async () => ({}) };
            return { ok: true, json: async () => ({ data: [{ t: '2026-11-24 19:42', v: '7.239', s: '0.1', q: 'p' }] }) };
        },
    };
    const keys = Object.keys(sb);
    const { fetchWaterLevels, _waterLevelCache } = new Function(...keys,
        src + '\n; return { fetchWaterLevels, _waterLevelCache };')(...keys.map(k => sb[k]));

    let r = await fetchWaterLevels();
    check('first fetch returns readings', r.size > 0 && r.get('9414290').value === 7.239);
    for (const e of _waterLevelCache.values()) e.fetchedAt -= 11 * 60000;   // expire the 10-min memo
    mode = 'throw';
    r = await fetchWaterLevels();
    check('network error: last reading is kept (with its original time)',
        r.get('9414290') && r.get('9414290').time === '2026-11-24 19:42', JSON.stringify([...r.keys()]));
    mode = '503';
    r = await fetchWaterLevels();
    check('HTTP 503: last reading is kept', !!r.get('9414290'));
}

// --- Tide marker: show the measurement when we have a fresh one (P44) ---
console.log('Tide marker reading (P44):');
{
    const sb = {
        localStorage: { getItem: () => '{}' },
        console, Date, Math, JSON, Map, Set, Array, Number, String, Object,
        parseInt, parseFloat, isNaN, AbortController, setTimeout, clearTimeout, Promise,
        window: undefined, fetch: async () => { throw new Error('no network'); },
    };
    const keys = Object.keys(sb);
    const { tideMarkerReading } = new Function(...keys,
        src + '\n; return { tideMarkerReading };')(...keys.map(k => sb[k]));
    const gauge = (extra = {}) => ({ observed: 10.42, predicted: 9.18, delta: 1.24, ageMin: 6, fresh: true, clockMismatch: false, ...extra });

    let r = tideMarkerReading({ height_ft: 9.18, gauge: gauge() }, 0);
    check('fresh gauge, real time: marker shows the measurement', r.source === 'measured' && r.value === 10.42, JSON.stringify(r));
    r = tideMarkerReading({ height_ft: 9.18, gauge: gauge({ fresh: false, ageMin: 190 }) }, 0);
    check('stale gauge: falls back to the prediction, never the old measurement', r.source === 'predicted' && r.value === 9.18, JSON.stringify(r));
    r = tideMarkerReading({ height_ft: 9.18, gauge: gauge({ fresh: false, clockMismatch: true }) }, 0);
    check('clock mismatch: falls back to the prediction', r.source === 'predicted' && r.value === 9.18, JSON.stringify(r));
    r = tideMarkerReading({ height_ft: 7.5, gauge: gauge() }, 120);
    check('forecast mode: always the prediction, even with a fresh gauge', r.source === 'predicted' && r.value === 7.5, JSON.stringify(r));
    r = tideMarkerReading({ height_ft: 9.18 }, 0);
    check('no gauge: the prediction', r.source === 'predicted' && r.value === 9.18, JSON.stringify(r));
    r = tideMarkerReading({ height_ft: null }, 0);
    check('no data at all: null value', r.value === null, JSON.stringify(r));
    r = tideMarkerReading({ height_ft: 9.18, gauge: gauge({ observed: NaN }) }, 0);
    check('unparseable measurement: the prediction', r.source === 'predicted', JSON.stringify(r));
}

console.log(`\n${pass + fail} tests: ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
