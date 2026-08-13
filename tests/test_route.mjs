/**
 * End-to-end route tests against live forecast data.
 *
 * Loads route-worker.js into a Node sandbox (same trick as test_physics.mjs),
 * pulls live SFBOFS + Open-Meteo wind + land mask off the deployed GitHub
 * Pages site, and runs a fixed SF→Monterey route through all four pruning
 * variants. Prints a table of ETA / distance / avg / ratio / error so we can
 * regress router changes in a few seconds instead of clicking around in a
 * browser.
 *
 * Run: node tests/test_route.mjs
 *      node tests/test_route.mjs --variant=vmc      # one variant only
 *      node tests/test_route.mjs --base=local       # use local static/data/
 *
 * Requires Node 18+ (built-in fetch).
 */

import { readFileSync, writeFileSync, existsSync, mkdirSync, statSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const ROOT = join(__dirname, '..');

// --- CLI args ---
const args = Object.fromEntries(process.argv.slice(2).map(a => {
    const m = a.match(/^--([^=]+)(?:=(.*))?$/);
    return m ? [m[1], m[2] ?? true] : [a, true];
}));

const BASE = args.base === 'local'
    ? 'file://' + ROOT + '/static'
    : 'https://rostape1.github.io/TyponFlowRacer';

// --- Route: GG bridge + 1 nm west → Monterey Harbor ---
// Golden Gate Bridge midspan is ~37.8199, -122.4783. 1 nm at lat 37.8 is
// ~0.0208° lon, so 1 nm west is approximately -122.4991. Endpoint sits in
// the water just outside Monterey Harbor (NOAA station 9413450). Override
// either with `--start=lat,lon` or `--end=lat,lon`.
function parseLatLon(s, dflt) {
    if (!s) return dflt;
    const [lat, lon] = s.split(',').map(Number);
    return { lat, lon };
}
const { lat: START_LAT, lon: START_LON } = parseLatLon(args.start, { lat: 37.8199, lon: -122.4991 });
const { lat: END_LAT,   lon: END_LON   } = parseLatLon(args.end,   { lat: 36.6100, lon: -121.8900 });

const VARIANTS = args.variant
    ? [args.variant]
    : ['baseline', 'vmc', 'falloff', 'both'];

// --- Fetch helpers ---
async function fetchJson(url) {
    if (url.startsWith('file://')) {
        return JSON.parse(readFileSync(url.replace('file://', ''), 'utf8'));
    }
    const res = await fetch(url);
    if (!res.ok) throw new Error(`${url} → HTTP ${res.status}`);
    return res.json();
}

async function fetchSfbofs(hour) {
    const pad = String(hour).padStart(2, '0');
    try { return await fetchJson(`${BASE}/data/sfbofs/hour_${pad}.json`); }
    catch { return null; }
}

// HYCOM is the offshore current source — covers lat 36.4–38.16 incl.
// Monterey, where SFBOFS (SF Bay only, lat 37.4–38.05) drops out. Without
// it the engine treats everything south of lat 37.4 as zero current, which
// silently changes route geometry. Mirrors data-loader.js fetchHycomCurrents.
async function fetchHycom() {
    try {
        const data = await fetchJson(`${BASE}/data/hycom/currents.json`);
        const pairs = [];
        for (const [h, g] of Object.entries(data.hours)) pairs.push([Number(h), g]);
        return pairs;
    } catch { return []; }
}

// Match data-loader.js bounds/dimensions and URL format exactly so we hit
// the same upstream as the browser.
const WIND_BOUNDS = { south: 36.40, north: 38.10, west: -122.95, east: -121.80 };
const WIND_NX = 11, WIND_NY = 16;

async function fetchWindGrids(hoursNeeded) {
    const lats = [], lons = [];
    for (let iy = 0; iy < WIND_NY; iy++) {
        const lat = WIND_BOUNDS.south + iy * (WIND_BOUNDS.north - WIND_BOUNDS.south) / (WIND_NY - 1);
        for (let ix = 0; ix < WIND_NX; ix++) {
            const lon = WIND_BOUNDS.west + ix * (WIND_BOUNDS.east - WIND_BOUNDS.west) / (WIND_NX - 1);
            lats.push(lat.toFixed(4));
            lons.push(lon.toFixed(4));
        }
    }
    const url = `https://api.open-meteo.com/v1/forecast`
        + `?latitude=${lats.join(',')}&longitude=${lons.join(',')}`
        + `&hourly=wind_speed_10m,wind_direction_10m`
        + `&models=gfs_seamless&wind_speed_unit=kn&forecast_hours=${hoursNeeded + 1}`;
    const data = await fetchJson(url);
    if (!Array.isArray(data) || data.length !== lats.length)
        throw new Error('Open-Meteo returned unexpected shape');

    const grids = new Map();
    for (let h = 0; h <= hoursNeeded; h++) {
        const u = Array.from({ length: WIND_NY }, () => new Array(WIND_NX).fill(0));
        const v = Array.from({ length: WIND_NY }, () => new Array(WIND_NX).fill(0));
        for (let idx = 0; idx < data.length; idx++) {
            const iy = Math.floor(idx / WIND_NX);
            const ix = idx % WIND_NX;
            const pt = data[idx];
            const spd = pt.hourly?.wind_speed_10m?.[h] || 0;
            const dir = pt.hourly?.wind_direction_10m?.[h] || 0;
            const rad = dir * Math.PI / 180;
            u[iy][ix] = -spd * Math.sin(rad);
            v[iy][ix] = -spd * Math.cos(rad);
        }
        grids.set(h, { bounds: WIND_BOUNDS, nx: WIND_NX, ny: WIND_NY, u, v });
    }
    return grids;
}

// --- Land mask in the worker's expected shape ---
function unpackLandGrid(g) {
    const binary = Buffer.from(g.data, 'base64');
    return {
        south: g.south, west: g.west, north: g.north, east: g.east,
        res: g.resolution, rows: g.rows, cols: g.cols, bits: binary,
    };
}

function indexLandPolygons(polys) {
    return polys.map(poly => {
        const outer = poly.outer;
        let minLat = Infinity, maxLat = -Infinity, minLon = Infinity, maxLon = -Infinity;
        for (const [lat, lon] of outer) {
            if (lat < minLat) minLat = lat;
            if (lat > maxLat) maxLat = lat;
            if (lon < minLon) minLon = lon;
            if (lon > maxLon) maxLon = lon;
        }
        return { outer, holes: poly.holes || [], minLat, maxLat, minLon, maxLon };
    });
}

// --- Haversine for rhumb-ratio computation ---
function haversineNm(lat1, lon1, lat2, lon2) {
    const DEG = Math.PI / 180;
    const dLat = (lat2 - lat1) * DEG;
    const dLon = (lon2 - lon1) * DEG;
    const a = Math.sin(dLat/2)**2 + Math.cos(lat1*DEG)*Math.cos(lat2*DEG)*Math.sin(dLon/2)**2;
    return 2 * Math.asin(Math.sqrt(a)) * 3440.065;
}

// --- Boot worker ---
function makeWorker() {
    const src = readFileSync(join(ROOT, 'static/js/route-worker.js'), 'utf8');
    let messageHandler = null;
    let result = null;
    let progressCount = 0;
    let lastProgress = null;
    const fakeSelf = {
        set onmessage(fn) { messageHandler = fn; },
        get onmessage() { return messageHandler; },
        postMessage(msg) {
            if (msg.type === 'result') result = msg.data;
            else if (msg.type === 'progress') { progressCount++; lastProgress = msg; }
        },
    };
    // eslint-disable-next-line no-new-func
    new Function('self', src)(fakeSelf);
    return {
        run(message) {
            result = null;
            progressCount = 0;
            lastProgress = null;
            messageHandler({ data: message });
            return { result, progressCount, lastProgress };
        },
    };
}

// --- Main ---
const CACHE_DIR = join(ROOT, 'tests', '.cache');
const CACHE_TTL_MS = 30 * 60 * 1000; // 30 min — wind/sfbofs barely change in that window
if (!existsSync(CACHE_DIR)) mkdirSync(CACHE_DIR, { recursive: true });

function cacheFresh(file) {
    if (!existsSync(file)) return false;
    return (Date.now() - statSync(file).mtimeMs) < CACHE_TTL_MS;
}

async function main() {
    const startTimeMs = Date.now();
    const hoursNeeded = 49;
    const rhumbNm = haversineNm(START_LAT, START_LON, END_LAT, END_LON);

    console.log(`Route: (${START_LAT}, ${START_LON}) → (${END_LAT}, ${END_LON})`);
    console.log(`Rhumb: ${rhumbNm.toFixed(1)} nm   Data: ${BASE}\n`);

    const sfCache = join(CACHE_DIR, 'sfbofs.json');
    let sfbofsPairs;
    if (cacheFresh(sfCache)) {
        sfbofsPairs = JSON.parse(readFileSync(sfCache, 'utf8'));
        console.log(`SFBOFS (cached) ${sfbofsPairs.length}/${hoursNeeded + 1} hours`);
    } else {
        process.stdout.write('Fetching SFBOFS ');
        sfbofsPairs = [];
        for (let h = 0; h <= hoursNeeded; h++) {
            const grid = await fetchSfbofs(h);
            if (grid) sfbofsPairs.push([h, grid]);
            process.stdout.write('.');
        }
        writeFileSync(sfCache, JSON.stringify(sfbofsPairs));
        console.log(` ${sfbofsPairs.length}/${hoursNeeded + 1} hours`);
    }

    const windCache = join(CACHE_DIR, 'wind.json');
    let windGrids;
    // --wind=synth[:dir,kn] uses a uniform wind field — no API needed, ideal
    // for regressing route geometry. Default: NW 15 kn (typical SF summer).
    // --wind=live forces a fresh Open-Meteo fetch. Default: cache → Open-Meteo
    // → static fallback → synth.
    const windMode = args.wind || 'auto';
    function synthWind(dirDeg, speedKn) {
        const rad = dirDeg * Math.PI / 180;
        const u = -speedKn * Math.sin(rad);
        const v = -speedKn * Math.cos(rad);
        const bounds = { south: 36.0, north: 38.2, west: -123.5, east: -121.5 };
        const nx = 11, ny = 12;
        const uG = Array.from({ length: ny }, () => new Array(nx).fill(u));
        const vG = Array.from({ length: ny }, () => new Array(nx).fill(v));
        const g = new Map();
        for (let h = 0; h <= hoursNeeded; h++) g.set(h, { bounds, nx, ny, u: uG, v: vG });
        return g;
    }
    if (windMode.startsWith('synth')) {
        const m = windMode.match(/^synth(?::(\d+),(\d+(?:\.\d+)?))?$/);
        const dir = m && m[1] ? Number(m[1]) : 315; // NW
        const spd = m && m[2] ? Number(m[2]) : 15;
        windGrids = synthWind(dir, spd);
        console.log(`Wind   (synth) uniform ${dir}° @ ${spd} kn, ${windGrids.size} hours`);
    } else if (windMode !== 'live' && cacheFresh(windCache)) {
        windGrids = new Map(JSON.parse(readFileSync(windCache, 'utf8')));
        console.log(`Wind   (cached) ${windGrids.size} hours`);
    } else {
        try {
            process.stdout.write('Fetching wind (Open-Meteo batched)...');
            windGrids = await fetchWindGrids(hoursNeeded);
            writeFileSync(windCache, JSON.stringify([...windGrids]));
            console.log(` ${windGrids.size} hours`);
        } catch (e) {
            console.log(` FAILED → using synth NW 15 kn`);
            windGrids = synthWind(315, 15);
        }
    }

    const lmCache = join(CACHE_DIR, 'land_mask.json');
    let landMask;
    if (cacheFresh(lmCache)) {
        landMask = JSON.parse(readFileSync(lmCache, 'utf8'));
        console.log('Land   (cached)');
    } else {
        process.stdout.write('Fetching land mask...');
        landMask = await fetchJson(`${BASE}/data/land_mask.json`);
        writeFileSync(lmCache, JSON.stringify(landMask));
        console.log('');
    }
    const landPolygons = indexLandPolygons(landMask.polygons || []);
    const landGrid = landMask.grid ? unpackLandGrid(landMask.grid) : null;

    const hyCache = join(CACHE_DIR, 'hycom.json');
    let hycomPairs;
    if (cacheFresh(hyCache)) {
        hycomPairs = JSON.parse(readFileSync(hyCache, 'utf8'));
        console.log(`HYCOM  (cached) ${hycomPairs.length} hours`);
    } else {
        process.stdout.write('Fetching HYCOM...');
        hycomPairs = await fetchHycom();
        writeFileSync(hyCache, JSON.stringify(hycomPairs));
        console.log(` ${hycomPairs.length} hours`);
    }

    const baseMessage = {
        sfbofsGrids: sfbofsPairs,
        sfbofsGridsHR: [],
        hycomGrids: hycomPairs,
        windGrids: [...windGrids],
        landPolygons,
        landGrid,
    };

    console.log('\nVariant         ETA       Distance    Avg     Ratio   Status');
    console.log('-'.repeat(72));

    const worker = makeWorker();
    for (const variant of VARIANTS) {
        const t0 = Date.now();
        const { result, progressCount, lastProgress } = worker.run({
            ...baseMessage,
            params: {
                startLat: START_LAT, startLon: START_LON,
                endLat: END_LAT, endLon: END_LON,
                startTimeMs, perfFactor: 0.85, variant, gridOffsetH: 0,
            },
        });
        const wallMs = Date.now() - t0;
        const progStr = lastProgress
            ? `prog ${progressCount}x last=${(lastProgress.elapsedS/3600).toFixed(1)}h`
            : 'no progress';

        if (result?.error) {
            console.log(`${variant.padEnd(16)} —         —           —       —       ${result.error}  [${progStr}, ${wallMs}ms]`);
            continue;
        }
        if (!result) {
            console.log(`${variant.padEnd(16)} no result returned  [${progStr}, ${wallMs}ms]`);
            continue;
        }
        const eta = result.elapsedMin;
        const dist = result.distanceNm;
        const avg = dist / (eta / 60);
        const ratio = dist / rhumbNm;
        console.log(
            `${variant.padEnd(16)} ${String(eta).padStart(4)} min  ${dist.toFixed(1).padStart(6)} nm  `
            + `${avg.toFixed(1).padStart(4)} kn  ${ratio.toFixed(2).padStart(5)}×  ok  [${progStr}, ${wallMs}ms]`
        );
    }
}

main().catch(e => { console.error(e); process.exit(1); });
