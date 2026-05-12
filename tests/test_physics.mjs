/**
 * Physics sanity tests for route-worker math.
 * Run: node tests/test_physics.mjs
 * No dependencies, no live APIs, no impact on production.
 */

// Extract the functions from route-worker by evaluating in a sandbox
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const workerSrc = readFileSync(join(__dirname, '../static/js/route-worker.js'), 'utf8');

// Expose internals for testing
const sandbox = {};
const fn = new Function('self', 'module',
    workerSrc + `\nmodule.getBoatSpeed = getBoatSpeed;\nmodule._lerp = _lerp;`
);
fn({ postMessage() {} }, sandbox);
const { getBoatSpeed } = sandbox;

// Apparent wind helper (same formula as route-worker)
function apparentWind(twa, tws, bsp) {
    const twaRad = twa * Math.PI / 180;
    const awx = tws * Math.sin(twaRad);
    const awy = tws * Math.cos(twaRad) + bsp;
    return {
        aws: Math.sqrt(awx * awx + awy * awy),
        awa: Math.atan2(awx, awy) * 180 / Math.PI,
    };
}

let passed = 0, failed = 0;

function assert(condition, msg) {
    if (condition) { passed++; }
    else { failed++; console.error(`  FAIL: ${msg}`); }
}

function approx(a, b, tol = 0.1) { return Math.abs(a - b) < tol; }

// --- Polar Tests ---
console.log('Polar lookup:');

const bsp90_10 = getBoatSpeed(90, 10, 1.0);
assert(bsp90_10 > 7 && bsp90_10 < 8, `BSP at TWA=90 TWS=10 should be ~7.4, got ${bsp90_10.toFixed(2)}`);

assert(getBoatSpeed(30, 10, 1.0) === 0, 'BSP at TWA=30 (below min 52°) should be 0');
assert(getBoatSpeed(90, 0.5, 1.0) === 0, 'BSP at TWS=0.5 (below 1kn) should be 0');

const bsp_perf = getBoatSpeed(90, 10, 0.85);
assert(approx(bsp_perf, bsp90_10 * 0.85, 0.01), `85% perf factor should scale BSP linearly`);

const bsp_light = getBoatSpeed(90, 3, 1.0);
const bsp_6 = getBoatSpeed(90, 6, 1.0);
assert(bsp_light < bsp_6, `Light air (3kn) BSP should be less than 6kn BSP`);
assert(bsp_light > 0, `BSP in 3kn wind should still be positive`);

console.log(`  ${passed} passed`);

// --- Apparent Wind Tests ---
console.log('Apparent wind:');
const prevPassed = passed;

// Dead downwind: AWS = TWS - BSP
const dd = apparentWind(180, 10, 5);
assert(approx(dd.aws, 5, 0.1), `Dead downwind TWS=10 BSP=5: AWS should be 5, got ${dd.aws.toFixed(1)}`);
assert(approx(Math.abs(dd.awa), 180, 1), `Dead downwind: AWA should be 180°, got ${dd.awa.toFixed(0)}°`);

// Dead upwind: AWS = TWS + BSP
const du = apparentWind(0, 10, 5);
assert(approx(du.aws, 15, 0.1), `Dead upwind TWS=10 BSP=5: AWS should be 15, got ${du.aws.toFixed(1)}`);
assert(approx(du.awa, 0, 1), `Dead upwind: AWA should be 0°, got ${du.awa.toFixed(0)}°`);

// Beam reach: AWS = sqrt(TWS² + BSP²)
const br = apparentWind(90, 10, 5);
const expected = Math.sqrt(100 + 25);
assert(approx(br.aws, expected, 0.1), `Beam reach TWS=10 BSP=5: AWS should be ${expected.toFixed(1)}, got ${br.aws.toFixed(1)}`);

// Key physics constraints
const cases = [
    { twa: 170, tws: 12.7, bsp: 6.2, label: 'broad run' },
    { twa: 150, tws: 15, bsp: 7, label: 'deep broad reach' },
    { twa: 45, tws: 12, bsp: 6, label: 'close hauled' },
    { twa: 90, tws: 8, bsp: 5, label: 'beam reach' },
];

for (const c of cases) {
    const aw = apparentWind(c.twa, c.tws, c.bsp);
    // Running (TWA > 90): AWS must be less than TWS + BSP
    assert(aw.aws <= c.tws + c.bsp + 0.1, `${c.label}: AWS (${aw.aws.toFixed(1)}) must be <= TWS+BSP (${c.tws + c.bsp})`);
    // Running downwind: AWS should be less than TWS
    if (c.twa > 90) {
        assert(aw.aws < c.tws, `${c.label}: AWS (${aw.aws.toFixed(1)}) must be < TWS (${c.tws}) when running`);
    }
    // Beating: AWS should be greater than TWS
    if (c.twa < 90) {
        assert(aw.aws > c.tws, `${c.label}: AWS (${aw.aws.toFixed(1)}) must be > TWS (${c.tws}) when beating`);
    }
    // AWA should be less than TWA when running (apparent shifts forward)
    if (c.twa > 90) {
        assert(Math.abs(aw.awa) < c.twa, `${c.label}: AWA (${Math.abs(aw.awa).toFixed(0)}°) must be < TWA (${c.twa}°)`);
    }
}

console.log(`  ${passed - prevPassed} passed`);

// --- Summary ---
console.log(`\n${passed + failed} tests: ${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
