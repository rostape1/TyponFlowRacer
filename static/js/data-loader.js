/**
 * Static data loader — fetches pre-computed JSON from GitHub Pages
 * and provides client-side interpolation for tides and currents.
 *
 * Replaces the backend API calls with static file fetches.
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

const DATA_BASE = 'data';  // Relative to site root

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

// --- Wind Field ---

async function fetchWindField(minutesOffset = 0) {
    const hour = forecastHour(minutesOffset);
    const gridUrl = `${DATA_BASE}/wind/hour_${String(hour).padStart(2, '0')}.json`;
    const stationsUrl = `${DATA_BASE}/wind/stations.json`;

    const [gridRes, stationsRes] = await Promise.all([
        fetch(gridUrl),
        minutesOffset === 0 ? fetch(stationsUrl) : Promise.resolve(null),
    ]);

    const grid = gridRes.ok ? await gridRes.json() : null;
    let stations = [];
    if (stationsRes && stationsRes.ok) {
        stations = await stationsRes.json();
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
    const results = [];

    const fetches = Object.entries(TIDE_STATIONS).map(async ([stationId, info]) => {
        try {
            // Check cache
            let data = _tideCache.get(stationId);
            if (!data || Date.now() - data.fetchedAt > 6 * 3600000) {
                const res = await fetch(`${DATA_BASE}/tides/${stationId}.json`);
                if (!res.ok) return null;
                const json = await res.json();
                data = { predictions: json.predictions, fetchedAt: Date.now() };
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
    const results = [];

    const fetches = Object.entries(CURRENT_STATIONS).map(async ([stationId, info]) => {
        try {
            let data = _currentCache.get(stationId);
            if (!data || Date.now() - data.fetchedAt > 6 * 3600000) {
                const res = await fetch(`${DATA_BASE}/currents/${stationId}.json`);
                if (!res.ok) return null;
                const json = await res.json();
                data = { predictions: json.predictions, fetchedAt: Date.now() };
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

async function downloadAllForOffline(onProgress) {
    const items = [];

    // SFBOFS (49 files)
    for (let h = 0; h <= 48; h++) {
        items.push(`${DATA_BASE}/sfbofs/hour_${String(h).padStart(2, '0')}.json`);
    }
    // Wind (49 files + stations)
    for (let h = 0; h <= 48; h++) {
        items.push(`${DATA_BASE}/wind/hour_${String(h).padStart(2, '0')}.json`);
    }
    items.push(`${DATA_BASE}/wind/stations.json`);
    // Tides (14 files)
    for (const id of Object.keys(TIDE_STATIONS)) {
        items.push(`${DATA_BASE}/tides/${id}.json`);
    }
    // Currents (6 files)
    for (const id of Object.keys(CURRENT_STATIONS)) {
        items.push(`${DATA_BASE}/currents/${id}.json`);
    }

    let completed = 0;
    for (const url of items) {
        try {
            await fetch(url);
        } catch (e) { /* continue */ }
        completed++;
        if (onProgress) onProgress(completed, items.length);
    }

    return completed;
}
