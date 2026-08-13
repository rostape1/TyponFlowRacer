/**
 * Regression tests for the SFBOFS staleness gate in data-loader.js.
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
const FRESH = 't21z 08/12';

console.log('Staleness gate:');

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

console.log('High-res path (router consumes this):');

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

console.log(`\n${pass + fail} tests: ${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
