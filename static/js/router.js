/**
 * Isochrone route optimizer for SF Bay sailing.
 * Finds the fastest path from A to B through time-varying current + wind fields.
 */

// --- Swan 47 Polar Table ---
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

    // Extrapolate below minimum polar TWS (6 kn) — linear scale from zero
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

// --- Water detection from NOAA ENC land polygons ---
// Pre-extracted from S-57 charts (US5CA12M, US5CA13M, US5CA16M, US4CA11M).
// Point-in-polygon test: if point is inside any land polygon, it's land.

let _landPolygons = null;
let _landMaskLoading = null;

async function _loadLandMask() {
    if (_landPolygons) return _landPolygons;
    if (_landMaskLoading) return _landMaskLoading;
    _landMaskLoading = fetch('data/land_mask.json')
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
            console.log(`Land mask loaded: ${_landPolygons.length} polygons`);
            return _landPolygons;
        })
        .catch(e => {
            console.warn('Land mask failed to load:', e);
            _landPolygons = [];
            return _landPolygons;
        });
    return _landMaskLoading;
}

function _pointInPoly(lat, lon, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const yi = ring[i][0], xi = ring[i][1];
        const yj = ring[j][0], xj = ring[j][1];
        if (((yi > lat) !== (yj > lat)) && (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi)) {
            inside = !inside;
        }
    }
    return inside;
}

function _isLand(lat, lon) {
    if (!_landPolygons || _landPolygons.length === 0) return false;
    for (const poly of _landPolygons) {
        if (lat < poly.minLat || lat > poly.maxLat || lon < poly.minLon || lon > poly.maxLon) continue;
        if (_pointInPoly(lat, lon, poly.outer)) {
            for (const hole of poly.holes) {
                if (_pointInPoly(lat, lon, hole)) return false;
            }
            return true;
        }
    }
    return false;
}

// ~200m safety buffer around land
const LAND_BUFFER_DEG = 0.002;

function _isTooCloseToLand(lat, lon) {
    if (_isLand(lat, lon)) return true;
    const b = LAND_BUFFER_DEG;
    return _isLand(lat + b, lon) ||
           _isLand(lat - b, lon) ||
           _isLand(lat, lon + b) ||
           _isLand(lat, lon - b);
}

// --- Haversine ---
const DEG2RAD = Math.PI / 180;
const RAD2DEG = 180 / Math.PI;
const NM_PER_DEG_LAT = 60;

function _haversineNm(lat1, lon1, lat2, lon2) {
    const dLat = (lat2 - lat1) * DEG2RAD;
    const dLon = (lon2 - lon1) * DEG2RAD;
    const a = Math.sin(dLat / 2) ** 2 +
              Math.cos(lat1 * DEG2RAD) * Math.cos(lat2 * DEG2RAD) * Math.sin(dLon / 2) ** 2;
    return 2 * Math.asin(Math.sqrt(a)) * 3440.065;
}

function _bearingDeg(lat1, lon1, lat2, lon2) {
    const dLon = (lon2 - lon1) * DEG2RAD;
    const y = Math.sin(dLon) * Math.cos(lat2 * DEG2RAD);
    const x = Math.cos(lat1 * DEG2RAD) * Math.sin(lat2 * DEG2RAD) -
              Math.sin(lat1 * DEG2RAD) * Math.cos(lat2 * DEG2RAD) * Math.cos(dLon);
    return (Math.atan2(y, x) * RAD2DEG + 360) % 360;
}

// --- Router Data Store ---
class RouterDataStore {
    constructor() {
        this.sfbofsGrids = new Map();
        this.windGrids = new Map();
        this.sfbofsBounds = null;
        this.sfbofsNx = 0;
        this.sfbofsNy = 0;
        this.startTimeMs = 0;
    }

    async preload(startTimeMs, hoursNeeded, startLat, startLon, endLat, endLon) {
        this.startTimeMs = startTimeMs;
        this.sfbofsGrids.clear();
        this.windGrids.clear();

        const runTime = getSfbofsRunTime();
        const elapsedHours = runTime ? Math.max(0, Math.floor((startTimeMs - runTime.getTime()) / 3600000)) : 0;

        const sfbofsFetches = [];
        for (let h = 0; h <= hoursNeeded; h++) {
            const fileIndex = Math.min(48, elapsedHours + h);
            sfbofsFetches.push(
                fetchCurrentField(h * 60).then(data => {
                    if (data && !data.unavailable && !data.error) {
                        this.sfbofsGrids.set(h, data);
                    }
                }).catch(() => {})
            );
        }
        await Promise.all([
            Promise.all(sfbofsFetches),
            _loadLandMask(),
        ]);

        for (let h = 0; h <= hoursNeeded; h++) {
            const grid = getWindGridForHour(h);
            if (grid) this.windGrids.set(h, grid);
        }

        if (this.windGrids.size === 0) {
            try {
                await fetchWindField(0);
                for (let h = 0; h <= hoursNeeded; h++) {
                    const grid = getWindGridForHour(h);
                    if (grid) this.windGrids.set(h, grid);
                }
            } catch (e) {}
        }

        const firstGrid = this.sfbofsGrids.values().next().value;
        if (firstGrid) {
            this.sfbofsBounds = firstGrid.bounds;
            this.sfbofsNx = firstGrid.nx;
            this.sfbofsNy = firstGrid.ny;
        }

        return {
            sfbofsHours: this.sfbofsGrids.size,
            windHours: this.windGrids.size,
        };
    }

    isWater(lat, lon) {
        return !_isTooCloseToLand(lat, lon);
    }

    _bilinearGrid(grid, lat, lon) {
        const b = grid.bounds;
        if (lat < b.south || lat > b.north || lon < b.west || lon > b.east) return null;
        const fy = (lat - b.south) / (b.north - b.south) * (grid.ny - 1);
        const fx = (lon - b.west) / (b.east - b.west) * (grid.nx - 1);
        const iy = Math.floor(fy);
        const ix = Math.floor(fx);
        if (iy < 0 || iy >= grid.ny - 1 || ix < 0 || ix >= grid.nx - 1) return null;
        const ty = fy - iy;
        const tx = fx - ix;
        const u00 = grid.u[iy][ix], u01 = grid.u[iy][ix + 1];
        const u10 = grid.u[iy + 1][ix], u11 = grid.u[iy + 1][ix + 1];
        const v00 = grid.v[iy][ix], v01 = grid.v[iy][ix + 1];
        const v10 = grid.v[iy + 1][ix], v11 = grid.v[iy + 1][ix + 1];
        const vx = (1 - ty) * ((1 - tx) * u00 + tx * u01) + ty * ((1 - tx) * u10 + tx * u11);
        const vy = (1 - ty) * ((1 - tx) * v00 + tx * v01) + ty * ((1 - tx) * v10 + tx * v11);
        return { vx, vy };
    }

    interpolateCurrent(lat, lon, timeMs) {
        const hoursFromStart = (timeMs - this.startTimeMs) / 3600000;
        const h0 = Math.floor(hoursFromStart);
        const h1 = h0 + 1;
        const frac = hoursFromStart - h0;

        const g0 = this.sfbofsGrids.get(h0);
        const g1 = this.sfbofsGrids.get(h1);

        if (!g0 && !g1) return null;
        if (!g0) return this._bilinearGrid(g1, lat, lon);
        if (!g1) return this._bilinearGrid(g0, lat, lon);

        const v0 = this._bilinearGrid(g0, lat, lon);
        const v1 = this._bilinearGrid(g1, lat, lon);
        if (!v0 && !v1) return null;
        if (!v0) return v1;
        if (!v1) return v0;

        return {
            vx: _lerp(v0.vx, v1.vx, frac),
            vy: _lerp(v0.vy, v1.vy, frac),
        };
    }

    interpolateWind(lat, lon, timeMs) {
        const hoursFromStart = (timeMs - this.startTimeMs) / 3600000;
        const h0 = Math.floor(hoursFromStart);
        const h1 = h0 + 1;
        const frac = hoursFromStart - h0;

        const g0 = this.windGrids.get(h0);
        const g1 = this.windGrids.get(h1);

        if (!g0 && !g1) return null;

        const sample = (grid) => {
            const b = grid.bounds;
            if (lat < b.south || lat > b.north || lon < b.west || lon > b.east) return null;
            const fy = (lat - b.south) / (b.north - b.south) * (grid.ny - 1);
            const fx = (lon - b.west) / (b.east - b.west) * (grid.nx - 1);
            const iy = Math.floor(fy);
            const ix = Math.floor(fx);
            if (iy < 0 || iy >= grid.ny - 1 || ix < 0 || ix >= grid.nx - 1) return null;
            const ty = fy - iy;
            const tx = fx - ix;
            const u = (1 - ty) * ((1 - tx) * grid.u[iy][ix] + tx * grid.u[iy][ix + 1]) +
                      ty * ((1 - tx) * grid.u[iy + 1][ix] + tx * grid.u[iy + 1][ix + 1]);
            const v = (1 - ty) * ((1 - tx) * grid.v[iy][ix] + tx * grid.v[iy][ix + 1]) +
                      ty * ((1 - tx) * grid.v[iy + 1][ix] + tx * grid.v[iy + 1][ix + 1]);
            return { u, v, speed: Math.sqrt(u * u + v * v) };
        };

        if (!g0) return sample(g1);
        if (!g1) return sample(g0);

        const w0 = sample(g0);
        const w1 = sample(g1);
        if (!w0 && !w1) return null;
        if (!w0) return w1;
        if (!w1) return w0;

        const u = _lerp(w0.u, w1.u, frac);
        const v = _lerp(w0.v, w1.v, frac);
        return { u, v, speed: Math.sqrt(u * u + v * v) };
    }
}

// --- Isochrone Engine ---
// Based on the James (1957) isochrone method for time-optimal sailing routing.
// Wavefront expands outward each time step; pruning keeps only the outer
// envelope (farthest-reachable frontier), preventing backtracking by construction.

const NUM_HEADINGS = 72;
const HEADING_STEP = 360 / NUM_HEADINGS;
const TIME_STEP_S = 120;
const MAX_TIME_S = 28800;
const DEST_RADIUS_NM = 0.15;
const PRUNE_SECTORS = 180;
const MAX_DIVERSION_DEG = 120;

function computeRoute(startLat, startLon, endLat, endLon, startTimeMs, perfFactor, onProgress) {
    const store = new RouterDataStore();

    const hoursNeeded = Math.ceil(MAX_TIME_S / 3600) + 1;

    return store.preload(startTimeMs, hoursNeeded, startLat, startLon, endLat, endLon).then(info => {
        if (info.windHours === 0) {
            return { error: 'No wind data available' };
        }
        if (info.sfbofsHours === 0) {
            return { error: 'No current data available' };
        }

        const maxSteps = Math.floor(MAX_TIME_S / TIME_STEP_S);
        const dtHours = TIME_STEP_S / 3600;

        let wavefront = [{
            lat: startLat, lon: startLon,
            timeMs: startTimeMs,
            parent: null,
            heading: -1,
        }];

        const isochrones = [];
        const destBrg = _bearingDeg(startLat, startLon, endLat, endLon);
        let maxWindSeen = 0;

        for (let step = 0; step < maxSteps; step++) {
            const newPoints = [];

            for (const pt of wavefront) {
                const current = store.interpolateCurrent(pt.lat, pt.lon, pt.timeMs);
                const wind = store.interpolateWind(pt.lat, pt.lon, pt.timeMs);

                if (!wind || wind.speed < 0.5) continue;
                if (wind.speed > maxWindSeen) maxWindSeen = wind.speed;

                const windFromDeg = (Math.atan2(-wind.u, -wind.v) * RAD2DEG + 360) % 360;

                for (let hi = 0; hi < NUM_HEADINGS; hi++) {
                    const headingDeg = hi * HEADING_STEP;
                    const headingRad = headingDeg * DEG2RAD;

                    let twa = Math.abs(headingDeg - windFromDeg);
                    if (twa > 180) twa = 360 - twa;

                    const bsp = getBoatSpeed(twa, wind.speed, perfFactor);
                    if (bsp < 0.5) continue;

                    const bvx = bsp * Math.sin(headingRad);
                    const bvy = bsp * Math.cos(headingRad);

                    const gvx = bvx + (current ? current.vx : 0);
                    const gvy = bvy + (current ? current.vy : 0);

                    const dLat = (gvy / NM_PER_DEG_LAT) * dtHours;
                    const dLon = (gvx / (NM_PER_DEG_LAT * Math.cos(pt.lat * DEG2RAD))) * dtHours;

                    const newLat = pt.lat + dLat;
                    const newLon = pt.lon + dLon;

                    const newPt = {
                        lat: newLat, lon: newLon,
                        timeMs: pt.timeMs + TIME_STEP_S * 1000,
                        parent: pt,
                        heading: headingDeg,
                        cvx: current ? current.vx : 0,
                        cvy: current ? current.vy : 0,
                    };

                    if (_haversineNm(newLat, newLon, endLat, endLon) < DEST_RADIUS_NM) {
                        const path = _smoothPath(_backtrack(newPt));
                        return {
                            path,
                            isochrones,
                            elapsedMin: Math.round((newPt.timeMs - startTimeMs) / 60000),
                            distanceNm: _pathDistance(path),
                        };
                    }

                    if (!store.isWater(newLat, newLon)) continue;

                    // Check that the segment doesn't cross land
                    if (_segmentCrossesLand(pt.lat, pt.lon, newLat, newLon)) continue;

                    // Bearing filter: reject points expanding >120° off course
                    const ptBrg = _bearingDeg(startLat, startLon, newLat, newLon);
                    let brgDiff = Math.abs(ptBrg - destBrg);
                    if (brgDiff > 180) brgDiff = 360 - brgDiff;
                    if (brgDiff > MAX_DIVERSION_DEG) continue;

                    newPoints.push(newPt);
                }
            }

            if (newPoints.length === 0) {
                if (maxWindSeen < 3) {
                    return { error: 'Wind too light for routing (' + maxWindSeen.toFixed(1) + ' kn max)' };
                }
                return { error: 'No reachable path — route may be blocked by land' };
            }

            wavefront = _pruneIsochrone(newPoints, startLat, startLon, endLat, endLon);

            if (step % 5 === 0) {
                isochrones.push(wavefront.map(p => [p.lat, p.lon]));
                if (onProgress) onProgress(step, maxSteps);
            }
        }

        const bestDist = Math.min(...wavefront.map(p => _haversineNm(p.lat, p.lon, endLat, endLon)));
        return { error: 'Destination not reached within ' + Math.round(MAX_TIME_S / 60) + ' min (closest: ' + bestDist.toFixed(1) + ' nm away, wind: ' + maxWindSeen.toFixed(1) + ' kn)' };
    });
}

function _backtrack(point) {
    const path = [];
    let p = point;
    while (p) {
        path.unshift({ lat: p.lat, lon: p.lon, timeMs: p.timeMs, heading: p.heading, cvx: p.cvx || 0, cvy: p.cvy || 0 });
        p = p.parent;
    }
    return path;
}

function _pathDistance(path) {
    let d = 0;
    for (let i = 1; i < path.length; i++) {
        d += _haversineNm(path[i - 1].lat, path[i - 1].lon, path[i].lat, path[i].lon);
    }
    return Math.round(d * 10) / 10;
}

function _pruneIsochrone(points, originLat, originLon, destLat, destLon) {
    // Sector pruning: keep closest-to-destination per angular sector
    const sectors = new Array(PRUNE_SECTORS).fill(null);

    for (const pt of points) {
        const brg = _bearingDeg(originLat, originLon, pt.lat, pt.lon);
        const sector = Math.floor(brg / (360 / PRUNE_SECTORS)) % PRUNE_SECTORS;
        const distToDest = _haversineNm(pt.lat, pt.lon, destLat, destLon);

        if (!sectors[sector] || distToDest < sectors[sector].distToDest) {
            sectors[sector] = { pt, distToDest };
        }
    }

    return sectors.filter(s => s !== null).map(s => s.pt);
}

// Check if the straight line between two points crosses land
function _segmentCrossesLand(lat1, lon1, lat2, lon2) {
    const steps = Math.max(3, Math.ceil(_haversineNm(lat1, lon1, lat2, lon2) / 0.05));
    for (let i = 1; i < steps; i++) {
        const t = i / steps;
        const lat = lat1 + (lat2 - lat1) * t;
        const lon = lon1 + (lon2 - lon1) * t;
        if (_isTooCloseToLand(lat, lon)) return true;
    }
    return false;
}

// Smooth backtracked path: remove intermediate points that zigzag
function _smoothPath(path) {
    if (path.length < 3) return path;
    let smoothed = [path[0]];
    let i = 0;
    while (i < path.length - 1) {
        // Try to skip ahead as far as possible without crossing land
        let best = i + 1;
        for (let j = Math.min(i + 8, path.length - 1); j > i + 1; j--) {
            if (!_segmentCrossesLand(path[i].lat, path[i].lon, path[j].lat, path[j].lon)) {
                best = j;
                break;
            }
        }
        smoothed.push(path[best]);
        i = best;
    }
    return smoothed;
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

        // Draw colored segments
        for (let i = 1; i < path.length; i++) {
            const p0 = path[i - 1];
            const p1 = path[i];

            // Compute current benefit along track direction
            let color = '#ffffff';
            const trackDirRad = Math.atan2(p1.lon - p0.lon, p1.lat - p0.lat);
            const benefit = p0.cvx * Math.sin(trackDirRad) + p0.cvy * Math.cos(trackDirRad);
            if (benefit > 0.3) color = '#2ecc71';
            else if (benefit < -0.3) color = '#e74c3c';

            L.polyline([[p0.lat, p0.lon], [p1.lat, p1.lon]], {
                color, weight: 4, opacity: 0.9,
            }).addTo(this.routeLayer);
        }

        // Time labels every 10 minutes
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

        // Draw isochrones (translucent)
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
