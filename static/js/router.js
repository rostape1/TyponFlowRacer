/**
 * Isochrone route optimizer for SF Bay sailing.
 * Data loading on main thread, computation in Web Worker.
 */

// --- Swan 47 Polar Table (kept for RouteRenderer and other main-thread uses) ---
const POLAR_TWA = [52, 60, 75, 90, 110, 120, 135, 150];
const POLAR_TWS = [6, 8, 10, 12, 14, 16, 20];
const POLAR_BSP = [
    [5.53, 6.47, 7.06, 7.35, 7.49, 7.57, 7.66],
    [5.81, 6.73, 7.28, 7.56, 7.70, 7.78, 7.86],
    [6.00, 6.90, 7.44, 7.74, 7.93, 8.03, 8.16],
    [5.87, 6.83, 7.43, 7.77, 8.01, 8.19, 8.42],
    [5.60, 6.77, 7.51, 7.93, 8.19, 8.39, 8.63],
    [5.45, 6.63, 7.44, 7.90, 8.22, 8.48, 8.89],
    [4.94, 6.12, 7.03, 7.63, 8.02, 8.33, 8.90],
    [4.18, 5.33, 6.30, 7.09, 7.64, 8.00, 8.57],
];

function _lerp(a, b, t) {
    return a + (b - a) * t;
}

function getBoatSpeed(twaDeg, tws, perfFactor = 0.85) {
    if (twaDeg > 180) twaDeg = 360 - twaDeg;
    if (twaDeg < POLAR_TWA[0]) return 0;
    if (tws < 1) return 0;
    let lightAirScale = 1.0;
    if (tws < POLAR_TWS[0]) {
        lightAirScale = tws / POLAR_TWS[0];
        tws = POLAR_TWS[0];
    }
    const twaClamped = Math.min(twaDeg, POLAR_TWA[POLAR_TWA.length - 1]);
    const twsClamped = Math.min(tws, POLAR_TWS[POLAR_TWS.length - 1]);
    let ti = 0;
    for (let i = 0; i < POLAR_TWA.length - 1; i++) {
        if (twaClamped >= POLAR_TWA[i] && twaClamped <= POLAR_TWA[i + 1]) { ti = i; break; }
    }
    let si = 0;
    for (let i = 0; i < POLAR_TWS.length - 1; i++) {
        if (twsClamped >= POLAR_TWS[i] && twsClamped <= POLAR_TWS[i + 1]) { si = i; break; }
    }
    const tFrac = (twaClamped - POLAR_TWA[ti]) / (POLAR_TWA[ti + 1] - POLAR_TWA[ti]);
    const sFrac = (twsClamped - POLAR_TWS[si]) / (POLAR_TWS[si + 1] - POLAR_TWS[si]);
    const v00 = POLAR_BSP[ti][si];
    const v01 = POLAR_BSP[ti][si + 1];
    const v10 = POLAR_BSP[ti + 1][si];
    const v11 = POLAR_BSP[ti + 1][si + 1];
    const bsp = _lerp(_lerp(v00, v01, sFrac), _lerp(v10, v11, sFrac), tFrac);
    return bsp * lightAirScale * perfFactor;
}

// --- Water detection (land mask loading for main thread) ---
let _landPolygons = null;
let _landGrid = null;
let _landMaskLoading = null;

async function _loadLandMask() {
    if (_landPolygons) return _landPolygons;
    if (_landMaskLoading) return _landMaskLoading;
    _landMaskLoading = fetch('data/land_mask.json?v=4')
        .then(r => r.json())
        .then(data => {
            _landPolygons = data.polygons.map(poly => {
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
            if (data.grid) {
                const g = data.grid;
                const binary = atob(g.data);
                const bytes = new Uint8Array(binary.length);
                for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
                _landGrid = {
                    south: g.south, west: g.west, north: g.north, east: g.east,
                    res: g.resolution, rows: g.rows, cols: g.cols, bits: bytes,
                };
                const landCells = Array.from(bytes).reduce((n, b) => n + (b ? (b.toString(2).match(/1/g) || []).length : 0), 0);
                console.log(`Land mask loaded: grid ${g.rows}x${g.cols}, ${landCells} land / ${g.rows * g.cols - landCells} water + ${_landPolygons.length} polygons`);
            } else {
                console.log(`Land mask loaded: ${_landPolygons.length} polygons (no grid)`);
            }
            return _landPolygons;
        })
        .catch(e => {
            console.warn('Land mask failed to load:', e);
            _landPolygons = [];
            return _landPolygons;
        });
    return _landMaskLoading;
}

// --- Haversine (used by RouteRenderer) ---
const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;
const NM_PER_DEG_LAT = 60;

const MAX_TIME_S = 86400;

// --- Router Data Store (data loading only) ---
class RouterDataStore {
    constructor() {
        this.sfbofsGrids = new Map();
        this.sfbofsGridsHR = new Map();
        this.hycomGrids = new Map();
        this.windGrids = new Map();
        this.startTimeMs = 0;
    }

    async preload(startTimeMs, hoursNeeded) {
        this.startTimeMs = startTimeMs;
        this.sfbofsGrids.clear();
        this.sfbofsGridsHR.clear();
        this.hycomGrids.clear();
        this.windGrids.clear();

        const forecastOffsetMin = Math.max(0, Math.floor((startTimeMs - Date.now()) / 60000));

        const sfbofsFetches = [];
        const sfbofsHRFetches = [];
        for (let h = 0; h <= hoursNeeded; h++) {
            const offsetMin = forecastOffsetMin + h * 60;
            sfbofsFetches.push(
                fetchCurrentField(offsetMin).then(data => {
                    if (data && !data.unavailable && !data.error) {
                        this.sfbofsGrids.set(h, data);
                    }
                }).catch(() => {})
            );
            sfbofsHRFetches.push(
                fetchCurrentFieldHighRes(offsetMin).then(data => {
                    if (data) this.sfbofsGridsHR.set(h, data);
                }).catch(() => {})
            );
        }
        await Promise.all([
            Promise.all(sfbofsFetches),
            Promise.all(sfbofsHRFetches),
            _loadLandMask(),
            fetchHycomCurrents().then(cache => {
                if (cache) {
                    for (const [h, grid] of cache.grids) {
                        this.hycomGrids.set(h, grid);
                    }
                }
            }).catch(() => {}),
        ]);

        const forecastOffsetHours = Math.floor(forecastOffsetMin / 60);
        for (let h = 0; h <= hoursNeeded; h++) {
            const grid = getWindGridForHour(forecastOffsetHours + h);
            if (grid) this.windGrids.set(h, grid);
        }

        if (this.windGrids.size === 0) {
            try {
                await fetchWindField(forecastOffsetMin);
                for (let h = 0; h <= hoursNeeded; h++) {
                    const grid = getWindGridForHour(forecastOffsetHours + h);
                    if (grid) this.windGrids.set(h, grid);
                }
            } catch (e) {}
        }

        return { windHours: this.windGrids.size };
    }
}

// --- Route Computation (Web Worker orchestrator) ---
let _routeWorker = null;

function computeRoute(startLat, startLon, endLat, endLon, startTimeMs, perfFactor, onProgress) {
    const store = new RouterDataStore();
    const hoursNeeded = Math.ceil(MAX_TIME_S / 3600) + 1;

    return store.preload(startTimeMs, hoursNeeded).then(info => {
        if (info.windHours === 0) {
            return { error: 'No wind data available' };
        }

        return new Promise((resolve, reject) => {
            if (_routeWorker) _routeWorker.terminate();
            _routeWorker = new Worker('js/route-worker.js');

            _routeWorker.onmessage = (e) => {
                if (e.data.type === 'progress') {
                    if (onProgress) onProgress(e.data.elapsedS, e.data.maxTimeS);
                } else if (e.data.type === 'result') {
                    _routeWorker.terminate();
                    _routeWorker = null;
                    resolve(e.data.data);
                }
            };

            _routeWorker.onerror = (e) => {
                _routeWorker.terminate();
                _routeWorker = null;
                reject(new Error('Route worker error: ' + e.message));
            };

            _routeWorker.postMessage({
                params: { startLat, startLon, endLat, endLon, startTimeMs, perfFactor },
                sfbofsGrids: [...store.sfbofsGrids],
                sfbofsGridsHR: [...store.sfbofsGridsHR],
                hycomGrids: [...store.hycomGrids],
                windGrids: [...store.windGrids],
                landPolygons: _landPolygons,
                landGrid: _landGrid,
            });
        });
    });
}

// --- Route Renderer ---

class RouteRenderer {
    constructor(map) {
        this.map = map;
        this.routeLayer = L.layerGroup();
        this.startMarker = null;
        this.endMarker = null;
    }

    drawRoute(result) {
        this.routeLayer.clearLayers();
        this.routeLayer.addTo(this.map);

        const path = result.path;
        if (!path || path.length < 2) return;

        for (let i = 1; i < path.length; i++) {
            const p0 = path[i - 1];
            const p1 = path[i];

            let color = '#ffffff';
            if (p1.cBenefit > 0.3) color = '#2ecc71';
            else if (p1.cBenefit < -0.3) color = '#e74c3c';

            L.polyline([[p0.lat, p0.lon], [p1.lat, p1.lon]], {
                color, weight: 4, opacity: 0.9,
            }).addTo(this.routeLayer);
        }

        const startMs = path[0].timeMs;
        for (const pt of path) {
            const elapsedMin = Math.round((pt.timeMs - startMs) / 60000);
            if (elapsedMin > 0 && elapsedMin % 10 === 0) {
                const label = L.divIcon({
                    html: `<span style="background:rgba(10,22,40,0.85);color:#f39c12;padding:1px 4px;border-radius:3px;font-size:10px;white-space:nowrap">${elapsedMin}m</span>`,
                    className: 'route-time-label',
                    iconSize: [30, 14],
                    iconAnchor: [15, 7],
                });
                L.marker([pt.lat, pt.lon], { icon: label, interactive: false }).addTo(this.routeLayer);
            }
        }

        if (result.isochrones) {
            for (const iso of result.isochrones) {
                if (iso.length > 2) {
                    L.polyline(iso, { color: '#a0b0c0', weight: 1, opacity: 0.3, dashArray: '3,4' })
                        .addTo(this.routeLayer);
                }
            }
        }
    }

    setStart(lat, lon) {
        if (this.startMarker) this.map.removeLayer(this.startMarker);
        this.startMarker = L.circleMarker([lat, lon], {
            radius: 8, color: '#2ecc71', fillColor: '#2ecc71', fillOpacity: 0.9, weight: 2,
        }).addTo(this.map).bindTooltip('Start', { permanent: true, direction: 'top', offset: [0, -10], className: 'route-tooltip' });
    }

    setEnd(lat, lon) {
        if (this.endMarker) this.map.removeLayer(this.endMarker);
        this.endMarker = L.circleMarker([lat, lon], {
            radius: 8, color: '#e74c3c', fillColor: '#e74c3c', fillOpacity: 0.9, weight: 2,
        }).addTo(this.map).bindTooltip('End', { permanent: true, direction: 'top', offset: [0, -10], className: 'route-tooltip' });
    }

    clear() {
        this.routeLayer.clearLayers();
        if (this.startMarker) { this.map.removeLayer(this.startMarker); this.startMarker = null; }
        if (this.endMarker) { this.map.removeLayer(this.endMarker); this.endMarker = null; }
    }
}
