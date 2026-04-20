/**
 * Data loader — fetches environmental data from public APIs
 * and provides client-side interpolation for tides and currents.
 *
 * Tides & currents: direct NOAA CO-OPS API
 * Wind grid: batched Open-Meteo API
 * SFBOFS & NDBC: static JSON from GitHub Pages (server-side processing)
 */

// Station metadata (embedded from backend Python files)
const TIDE_STATIONS = {
    '9414290': { name: 'San Francisco (Golden Gate)', lat: 37.8063, lon: -122.4659 },
    '9414750': { name: 'Alameda', lat: 37.7720, lon: -122.3003 },
    '9414764': { name: 'Oakland Inner Harbor', lat: 37.7950, lon: -122.2820 },
    '9414816': { name: 'Berkeley', lat: 37.8650, lon: -122.3070 },
    '9414874': { name: 'Corte Madera Creek', lat: 37.9433, lon: -122.5130 },
    '9414688': { name: 'San Leandro Marina', lat: 37.6950, lon: -122.1920 },
    '9414523': { name: 'Redwood City', lat: 37.5068, lon: -122.2119 },
    '9414458': { name: 'San Mateo Bridge (West)', lat: 37.5800, lon: -122.2530 },
    '9414509': { name: 'Dumbarton Bridge', lat: 37.5067, lon: -122.1150 },
    '9414131': { name: 'Half Moon Bay', lat: 37.5025, lon: -122.4822 },
    '9414863': { name: 'Richmond (Chevron Pier)', lat: 37.9283, lon: -122.4000 },
    '9415056': { name: 'Pinole Point', lat: 38.0150, lon: -122.3630 },
    '9415102': { name: 'Martinez', lat: 38.0346, lon: -122.1252 },
    '9415144': { name: 'Port Chicago', lat: 38.0560, lon: -122.0395 },
};

const CURRENT_STATIONS = {
    'SFB1201': { name: 'Golden Gate Bridge', lat: 37.8117, lon: -122.4717 },
    'SFB1203': { name: 'Alcatraz (North)', lat: 37.8317, lon: -122.4217 },
    'SFB1204': { name: 'Alcatraz (South)', lat: 37.8183, lon: -122.4200 },
    'SFB1205': { name: 'Angel Island (East)', lat: 37.8633, lon: -122.4217 },
    'SFB1206': { name: 'Raccoon Strait', lat: 37.8567, lon: -122.4467 },
    'SFB1211': { name: 'Bay Bridge', lat: 37.8033, lon: -122.3633 },
};

// Cache for fetched station data
const _tideCache = new Map();   // stationId → { predictions, fetchedAt }
const _currentCache = new Map();

const DATA_BASE = 'data';  // Relative to site root (for SFBOFS + NDBC static JSON)
const NOAA_API = 'https://api.tidesandcurrents.noaa.gov/api/prod/datagetter';

// --- Helpers ---

function _ymd(d) {
    return d.getUTCFullYear().toString() +
        String(d.getUTCMonth() + 1).padStart(2, '0') +
        String(d.getUTCDate()).padStart(2, '0');
}

function _noaaDateRange(minutesOffset = 0) {
    const now = new Date();
    const begin = _ymd(now);
    // Cover at least 3 days; extend if forecast goes beyond 48h
    const daysAhead = Math.max(3, Math.ceil(minutesOffset / 1440) + 1);
    const end = _ymd(new Date(now.getTime() + daysAhead * 86400000));
    return { begin, end };
}

// --- Forecast hour mapping ---

function forecastHour(minutesOffset) {
    return Math.min(48, Math.max(0, Math.floor(minutesOffset / 60)));
}

// --- SFBOFS Current Field ---

async function fetchCurrentField(minutesOffset = 0) {
    const hour = forecastHour(minutesOffset);
    const url = `${DATA_BASE}/sfbofs/hour_${String(hour).padStart(2, '0')}.json`;
    const res = await fetch(url);
    if (!res.ok) return null;
    return res.json();
}

// --- Wind Field (batched Open-Meteo API) ---

const OPEN_METEO_API = 'https://api.open-meteo.com/v1/forecast';
const WIND_BOUNDS = { south: 37.30, north: 38.10, west: -122.95, east: -122.10 };
const WIND_NX = 9;
const WIND_NY = 8;
let _windGridCache = null; // { grids: Map<hour, gridObj>, fetchedAt }

async function _fetchWindGridFromAPI() {
    // Generate 72 lat/lon pairs in row-major order (iy,ix)
    const lats = [];
    const lons = [];
    for (let iy = 0; iy < WIND_NY; iy++) {
        const lat = WIND_BOUNDS.south + iy * (WIND_BOUNDS.north - WIND_BOUNDS.south) / (WIND_NY - 1);
        for (let ix = 0; ix < WIND_NX; ix++) {
            const lon = WIND_BOUNDS.west + ix * (WIND_BOUNDS.east - WIND_BOUNDS.west) / (WIND_NX - 1);
            lats.push(lat.toFixed(4));
            lons.push(lon.toFixed(4));
        }
    }

    const url = `${OPEN_METEO_API}?latitude=${lats.join(',')}&longitude=${lons.join(',')}`
        + '&current=wind_speed_10m,wind_direction_10m,wind_gusts_10m'
        + '&hourly=wind_speed_10m,wind_direction_10m,wind_gusts_10m'
        + '&models=gfs_seamless&wind_speed_unit=kn&forecast_hours=49';

    const res = await fetch(url);
    if (!res.ok) return null;
    const data = await res.json();

    // Response is an array of 72 objects (one per coordinate pair)
    if (!Array.isArray(data) || data.length !== lats.length) return null;

    const nowStr = new Date().toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
    const grids = new Map();

    for (let hour = 0; hour < 49; hour++) {
        const u = Array.from({ length: WIND_NY }, () => new Array(WIND_NX).fill(0));
        const v = Array.from({ length: WIND_NY }, () => new Array(WIND_NX).fill(0));
        const gusts = Array.from({ length: WIND_NY }, () => new Array(WIND_NX).fill(0));

        for (let idx = 0; idx < data.length; idx++) {
            const iy = Math.floor(idx / WIND_NX);
            const ix = idx % WIND_NX;
            const point = data[idx];

            let spd, dir, gst;
            if (hour === 0 && point.current) {
                spd = point.current.wind_speed_10m || 0;
                dir = point.current.wind_direction_10m || 0;
                gst = point.current.wind_gusts_10m || 0;
            } else if (point.hourly) {
                spd = point.hourly.wind_speed_10m?.[hour] || 0;
                dir = point.hourly.wind_direction_10m?.[hour] || 0;
                gst = point.hourly.wind_gusts_10m?.[hour] || 0;
            } else {
                continue;
            }

            const rad = dir * Math.PI / 180;
            u[iy][ix] = Math.round(-spd * Math.sin(rad) * 100) / 100;
            v[iy][ix] = Math.round(-spd * Math.cos(rad) * 100) / 100;
            gusts[iy][ix] = Math.round(gst * 10) / 10;
        }

        grids.set(hour, {
            bounds: WIND_BOUNDS,
            nx: WIND_NX,
            ny: WIND_NY,
            model: 'GFS Seamless',
            source: 'Open-Meteo',
            fetched_at: nowStr,
            forecast_hour: hour,
            u,
            v,
            gusts,
        });
    }

    return grids;
}

async function fetchWindField(minutesOffset = 0) {
    const hour = forecastHour(minutesOffset);

    // Check wind grid cache (30min TTL)
    if (!_windGridCache || Date.now() - _windGridCache.fetchedAt > 30 * 60000) {
        try {
            const grids = await _fetchWindGridFromAPI();
            if (grids) {
                _windGridCache = { grids, fetchedAt: Date.now() };
            }
        } catch (e) {
            console.log('Wind grid fetch failed:', e.message);
        }
    }

    let grid = null;
    if (_windGridCache && _windGridCache.grids.has(hour)) {
        grid = _windGridCache.grids.get(hour);
    }

    // NDBC stations — still fetched from GitHub Pages static JSON
    let stations = [];
    if (minutesOffset === 0) {
        try {
            const stationsRes = await fetch(`${DATA_BASE}/wind/stations.json`);
            if (stationsRes.ok) stations = await stationsRes.json();
        } catch (e) { /* optional */ }
    }

    if (!grid) return null;
    return { grid, stations, forecast_hour: hour };
}

// --- Tide Height Interpolation ---

function _parsePredictions(predictions) {
    const result = [];
    for (const p of predictions) {
        try {
            const t = new Date(p.t.replace(' ', 'T') + 'Z');
            const v = parseFloat(p.v);
            if (!isNaN(t.getTime()) && !isNaN(v)) {
                result.push({ time: t, value: v });
            }
        } catch (e) { /* skip */ }
    }
    result.sort((a, b) => a.time - b.time);
    return result;
}

function _interpolateHeight(predictions, targetTime) {
    const parsed = _parsePredictions(predictions);
    if (!parsed.length) return null;

    for (let i = 0; i < parsed.length - 1; i++) {
        const t0 = parsed[i].time;
        const v0 = parsed[i].value;
        const t1 = parsed[i + 1].time;
        const v1 = parsed[i + 1].value;

        if (targetTime >= t0 && targetTime <= t1) {
            const total = t1 - t0;
            if (total === 0) return v0;
            const frac = (targetTime - t0) / total;
            return Math.round((v0 + frac * (v1 - v0)) * 100) / 100;
        }
    }

    // Outside range — return nearest
    if (targetTime <= parsed[0].time) return parsed[0].value;
    return parsed[parsed.length - 1].value;
}

function _findNextExtreme(predictions, targetTime) {
    const parsed = _parsePredictions(predictions);
    if (parsed.length < 3) return null;

    for (let i = 1; i < parsed.length - 1; i++) {
        const t = parsed[i].time;
        if (t <= targetTime) continue;

        const vPrev = parsed[i - 1].value;
        const v = parsed[i].value;
        const vNext = parsed[i + 1].value;

        if (v >= vPrev && v >= vNext && v > vPrev) {
            return {
                type: 'High',
                time: parsed[i].time.toISOString().slice(0, 16).replace('T', ' '),
                height_ft: Math.round(v * 100) / 100,
            };
        }
        if (v <= vPrev && v <= vNext && v < vPrev) {
            return {
                type: 'Low',
                time: parsed[i].time.toISOString().slice(0, 16).replace('T', ' '),
                height_ft: Math.round(v * 100) / 100,
            };
        }
    }
    return null;
}

async function fetchTideHeights(minutesOffset = 0) {
    const targetTime = new Date(Date.now() + minutesOffset * 60000);
    const { begin, end } = _noaaDateRange(minutesOffset);

    const fetches = Object.entries(TIDE_STATIONS).map(async ([stationId, info]) => {
        try {
            let data = _tideCache.get(stationId);
            if (!data || Date.now() - data.fetchedAt > 6 * 3600000) {
                const url = `${NOAA_API}?begin_date=${begin}&end_date=${end}&station=${stationId}&product=predictions&datum=MLLW&units=english&time_zone=gmt&format=json&interval=6`;
                const res = await fetch(url);
                if (!res.ok) return null;
                const json = await res.json();
                data = { predictions: json.predictions || [], fetchedAt: Date.now() };
                _tideCache.set(stationId, data);
            }

            const height = _interpolateHeight(data.predictions, targetTime);
            const extreme = _findNextExtreme(data.predictions, targetTime);

            return {
                station_id: stationId,
                name: info.name,
                lat: info.lat,
                lon: info.lon,
                height_ft: height,
                next_extreme: extreme,
            };
        } catch (e) {
            return null;
        }
    });

    const all = await Promise.all(fetches);
    return all.filter(r => r !== null);
}

// --- Tidal Current Interpolation ---

function _interpolateCurrent(predictions, targetTime) {
    if (!predictions || !predictions.length) return null;

    const parsed = [];
    for (const p of predictions) {
        try {
            const t = new Date(p.Time.replace(' ', 'T') + 'Z');
            const vel = parseFloat(p.Velocity_Major);
            const floodDir = parseFloat(p.meanFloodDir || 0);
            const ebbDir = parseFloat(p.meanEbbDir || 180);
            if (!isNaN(t.getTime()) && !isNaN(vel)) {
                parsed.push({ time: t, velocity: vel, floodDir, ebbDir });
            }
        } catch (e) { /* skip */ }
    }

    parsed.sort((a, b) => a.time - b.time);
    if (!parsed.length) return null;

    // Find surrounding predictions
    let before = null;
    let after = null;

    for (let i = 0; i < parsed.length; i++) {
        if (parsed[i].time <= targetTime) {
            before = parsed[i];
            if (i + 1 < parsed.length) after = parsed[i + 1];
        } else if (before === null) {
            // targetTime is before all predictions
            const p = parsed[0];
            return {
                speed: Math.round(Math.abs(p.velocity) * 100) / 100,
                direction: p.velocity >= 0 ? p.floodDir : p.ebbDir,
                type: p.velocity >= 0 ? 'flood' : 'ebb',
                velocity: Math.round(p.velocity * 100) / 100,
            };
        } else {
            break;
        }
    }

    if (!before) return null;

    if (!after) {
        return {
            speed: Math.round(Math.abs(before.velocity) * 100) / 100,
            direction: before.velocity >= 0 ? before.floodDir : before.ebbDir,
            type: before.velocity >= 0 ? 'flood' : 'ebb',
            velocity: Math.round(before.velocity * 100) / 100,
        };
    }

    // Linear interpolation
    const total = after.time - before.time;
    if (total === 0) {
        const p = before;
        return {
            speed: Math.round(Math.abs(p.velocity) * 100) / 100,
            direction: p.velocity >= 0 ? p.floodDir : p.ebbDir,
            type: p.velocity >= 0 ? 'flood' : 'ebb',
            velocity: Math.round(p.velocity * 100) / 100,
        };
    }

    const frac = (targetTime - before.time) / total;
    const vel = before.velocity + frac * (after.velocity - before.velocity);
    const direction = vel >= 0 ? before.floodDir : before.ebbDir;

    return {
        speed: Math.round(Math.abs(vel) * 100) / 100,
        direction: direction,
        type: vel >= 0 ? 'flood' : 'ebb',
        velocity: Math.round(vel * 100) / 100,
    };
}

async function fetchCurrents(minutesOffset = 0) {
    const targetTime = new Date(Date.now() + minutesOffset * 60000);
    const { begin, end } = _noaaDateRange(minutesOffset);

    const fetches = Object.entries(CURRENT_STATIONS).map(async ([stationId, info]) => {
        try {
            let data = _currentCache.get(stationId);
            if (!data || Date.now() - data.fetchedAt > 6 * 3600000) {
                const url = `${NOAA_API}?begin_date=${begin}&end_date=${end}&station=${stationId}&product=currents_predictions&units=english&time_zone=gmt&format=json&interval=6`;
                const res = await fetch(url);
                if (!res.ok) return null;
                const json = await res.json();
                const preds = json.current_predictions?.cp || json.predictions || [];
                data = { predictions: preds, fetchedAt: Date.now() };
                _currentCache.set(stationId, data);
            }

            const current = _interpolateCurrent(data.predictions, targetTime);
            if (!current) return null;

            return {
                station_id: stationId,
                name: info.name,
                lat: info.lat,
                lon: info.lon,
                ...current,
            };
        } catch (e) {
            return null;
        }
    });

    const all = await Promise.all(fetches);
    return all.filter(r => r !== null);
}

// --- Data meta / freshness ---

async function fetchMeta() {
    try {
        const res = await fetch(`${DATA_BASE}/meta.json`);
        if (!res.ok) return null;
        return res.json();
    } catch (e) {
        return null;
    }
}

// --- Offline download ---

async function downloadAllForOffline(onProgress, onCategory) {
    const { begin, end } = _noaaDateRange(0);
    const totalItems = 49 + 1 + Object.keys(TIDE_STATIONS).length + Object.keys(CURRENT_STATIONS).length + 1;
    let completed = 0;

    function tick() {
        completed++;
        if (onProgress) onProgress(completed, totalItems);
    }

    // SFBOFS (49 files — still static JSON; stop on first 404, later hours may not exist)
    let flowOk = 0;
    for (let h = 0; h <= 48; h++) {
        try {
            const resp = await fetch(`${DATA_BASE}/sfbofs/hour_${String(h).padStart(2, '0')}.json`);
            if (resp.ok) flowOk++;
            else if (resp.status === 404) break;
        } catch (e) {}
        tick();
    }
    if (onCategory) onCategory('flow', flowOk);

    // NDBC stations (still static JSON)
    try { await fetch(`${DATA_BASE}/wind/stations.json`); } catch (e) {}
    tick();

    // Tides — NOAA API (SW caches each response)
    let tidesOk = 0;
    for (const stationId of Object.keys(TIDE_STATIONS)) {
        try {
            await fetch(`${NOAA_API}?begin_date=${begin}&end_date=${end}&station=${stationId}&product=predictions&datum=MLLW&units=english&time_zone=gmt&format=json&interval=6`);
            tidesOk++;
        } catch (e) {}
        tick();
    }
    if (onCategory) onCategory('tides', tidesOk > 0);

    // Currents — NOAA API (SW caches each response)
    let currOk = 0;
    for (const stationId of Object.keys(CURRENT_STATIONS)) {
        try {
            await fetch(`${NOAA_API}?begin_date=${begin}&end_date=${end}&station=${stationId}&product=currents_predictions&units=english&time_zone=gmt&format=json&interval=6`);
            currOk++;
        } catch (e) {}
        tick();
    }
    if (onCategory) onCategory('currents', currOk > 0);

    // Wind grid — single batched Open-Meteo request
    let windOk = 0;
    try { await _fetchWindGridFromAPI(); windOk++; } catch (e) {}
    tick();
    if (onCategory) onCategory('wind', windOk > 0);

    return { flowOk, tidesOk, currOk, windOk };
}
