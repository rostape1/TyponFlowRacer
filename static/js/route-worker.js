/**
 * Route computation Web Worker.
 * Runs the isochrone engine off the main thread to keep the UI responsive.
 * Receives serialized grid data + land mask, posts progress and result.
 */

// --- Polar Table ---
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

function _lerp(a, b, t) { return a + (b - a) * t; }

function getBoatSpeed(twaDeg, tws, perfFactor) {
    if (twaDeg > 180) twaDeg = 360 - twaDeg;
    if (twaDeg < POLAR_TWA[0]) return 0;
    if (tws < 1) return 0;
    let lightAirScale = 1.0;
    if (tws < POLAR_TWS[0]) { lightAirScale = tws / POLAR_TWS[0]; tws = POLAR_TWS[0]; }
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
    const bsp = _lerp(_lerp(POLAR_BSP[ti][si], POLAR_BSP[ti][si + 1], sFrac),
                       _lerp(POLAR_BSP[ti + 1][si], POLAR_BSP[ti + 1][si + 1], sFrac), tFrac);
    return bsp * lightAirScale * perfFactor;
}

// --- Land Detection ---
let _landGrid = null;
let _landPolygons = null;

function _pointInPoly(lat, lon, ring) {
    let inside = false;
    for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
        const yi = ring[i][0], xi = ring[i][1];
        const yj = ring[j][0], xj = ring[j][1];
        if (((yi > lat) !== (yj > lat)) && (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi))
            inside = !inside;
    }
    return inside;
}

function _isLand(lat, lon) {
    if (_landGrid) {
        const g = _landGrid;
        if (lat >= g.south && lat <= g.north && lon >= g.west && lon <= g.east) {
            const row = Math.floor((lat - g.south) / g.res);
            const col = Math.floor((lon - g.west) / g.res);
            if (row >= 0 && row < g.rows && col >= 0 && col < g.cols) {
                const idx = row * g.cols + col;
                return (g.bits[idx >> 3] & (1 << (idx & 7))) !== 0;
            }
        }
        return false;
    }
    if (!_landPolygons || _landPolygons.length === 0) return false;
    for (const poly of _landPolygons) {
        if (lat < poly.minLat || lat > poly.maxLat || lon < poly.minLon || lon > poly.maxLon) continue;
        if (_pointInPoly(lat, lon, poly.outer)) {
            for (const hole of poly.holes) { if (_pointInPoly(lat, lon, hole)) return false; }
            return true;
        }
    }
    return false;
}

const LAND_BUFFER_DEG = 0.002;
const LAND_BUFFER_DEG_HR = 0.001;
const GG_BOUNDS = { south: 37.78, north: 37.86, west: -122.53, east: -122.42 };

function _isTooCloseToLand(lat, lon) {
    if (_landGrid) return _isLand(lat, lon);
    if (_isLand(lat, lon)) return true;
    const inGG = lat >= GG_BOUNDS.south && lat <= GG_BOUNDS.north &&
                 lon >= GG_BOUNDS.west && lon <= GG_BOUNDS.east;
    const b = inGG ? LAND_BUFFER_DEG_HR : LAND_BUFFER_DEG;
    return _isLand(lat + b, lon) || _isLand(lat - b, lon) ||
           _isLand(lat, lon + b) || _isLand(lat, lon - b);
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

// --- Grid Interpolation ---
function _bilinearGrid(grid, lat, lon) {
    const b = grid.bounds;
    if (lat < b.south || lat > b.north || lon < b.west || lon > b.east) return null;
    const fy = (lat - b.south) / (b.north - b.south) * (grid.ny - 1);
    const fx = (lon - b.west) / (b.east - b.west) * (grid.nx - 1);
    const iy = Math.floor(fy);
    const ix = Math.floor(fx);
    if (iy < 0 || iy >= grid.ny - 1 || ix < 0 || ix >= grid.nx - 1) return null;
    const ty = fy - iy;
    const tx = fx - ix;
    const vx = (1 - ty) * ((1 - tx) * grid.u[iy][ix] + tx * grid.u[iy][ix + 1]) +
               ty * ((1 - tx) * grid.u[iy + 1][ix] + tx * grid.u[iy + 1][ix + 1]);
    const vy = (1 - ty) * ((1 - tx) * grid.v[iy][ix] + tx * grid.v[iy][ix + 1]) +
               ty * ((1 - tx) * grid.v[iy + 1][ix] + tx * grid.v[iy + 1][ix + 1]);
    return { vx, vy };
}

function _interpolateWindSample(grid, lat, lon) {
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
}

// --- Current Interpolation ---
function interpolateCurrent(sfbofsGrids, sfbofsGridsHR, hycomGrids, startTimeMs, lat, lon, timeMs) {
    const hoursFromStart = (timeMs - startTimeMs) / 3600000;
    const h0 = Math.floor(hoursFromStart);
    const h1 = h0 + 1;
    const frac = hoursFromStart - h0;

    const hr0 = sfbofsGridsHR.get(h0);
    const hr1 = sfbofsGridsHR.get(h1);
    if (hr0 || hr1) {
        const hv0 = hr0 ? _bilinearGrid(hr0, lat, lon) : null;
        const hv1 = hr1 ? _bilinearGrid(hr1, lat, lon) : null;
        if (hv0 && hv1) return { vx: _lerp(hv0.vx, hv1.vx, frac), vy: _lerp(hv0.vy, hv1.vy, frac) };
        if (hv0) return hv0;
        if (hv1) return hv1;
    }

    const g0 = sfbofsGrids.get(h0);
    const g1 = sfbofsGrids.get(h1);

    if (!g0 && !g1) return _interpolateHycom(hycomGrids, hoursFromStart, lat, lon);
    if (!g0) return _bilinearGrid(g1, lat, lon) || _interpolateHycom(hycomGrids, hoursFromStart, lat, lon);
    if (!g1) return _bilinearGrid(g0, lat, lon) || _interpolateHycom(hycomGrids, hoursFromStart, lat, lon);

    const v0 = _bilinearGrid(g0, lat, lon);
    const v1 = _bilinearGrid(g1, lat, lon);
    if (!v0 && !v1) return _interpolateHycom(hycomGrids, hoursFromStart, lat, lon);
    if (!v0) return v1;
    if (!v1) return v0;
    return { vx: _lerp(v0.vx, v1.vx, frac), vy: _lerp(v0.vy, v1.vy, frac) };
}

function _interpolateHycom(hycomGrids, hoursFromStart, lat, lon) {
    if (hycomGrids.size === 0) return null;
    const h3 = hoursFromStart / 3;
    const h0 = Math.floor(h3) * 3;
    const h1 = h0 + 3;
    const frac = (hoursFromStart - h0) / 3;
    const g0 = hycomGrids.get(h0);
    const g1 = hycomGrids.get(h1);
    if (!g0 && !g1) {
        const nearest = hycomGrids.get(Math.round(h3) * 3);
        return nearest ? _bilinearGrid(nearest, lat, lon) : null;
    }
    if (!g0) return _bilinearGrid(g1, lat, lon);
    if (!g1) return _bilinearGrid(g0, lat, lon);
    const v0 = _bilinearGrid(g0, lat, lon);
    const v1 = _bilinearGrid(g1, lat, lon);
    if (!v0 && !v1) return null;
    if (!v0) return v1;
    if (!v1) return v0;
    return { vx: _lerp(v0.vx, v1.vx, frac), vy: _lerp(v0.vy, v1.vy, frac) };
}

function interpolateWind(windGrids, startTimeMs, lat, lon, timeMs) {
    const hoursFromStart = (timeMs - startTimeMs) / 3600000;
    const h0 = Math.floor(hoursFromStart);
    const h1 = h0 + 1;
    const frac = hoursFromStart - h0;
    const g0 = windGrids.get(h0);
    const g1 = windGrids.get(h1);
    if (!g0 && !g1) return null;
    if (!g0) return _interpolateWindSample(g1, lat, lon);
    if (!g1) return _interpolateWindSample(g0, lat, lon);
    const w0 = _interpolateWindSample(g0, lat, lon);
    const w1 = _interpolateWindSample(g1, lat, lon);
    if (!w0 && !w1) return null;
    if (!w0) return w1;
    if (!w1) return w0;
    const u = _lerp(w0.u, w1.u, frac);
    const v = _lerp(w0.v, w1.v, frac);
    return { u, v, speed: Math.sqrt(u * u + v * v) };
}

// --- Pruning ---
const NUM_HEADINGS = 72;
const HEADING_STEP = 360 / NUM_HEADINGS;
const TIME_STEP_S = 120;
const TIME_STEP_OPEN_S = 300;
const MAX_TIME_S = 86400;
const DEST_RADIUS_NM = 0.15;
const PRUNE_SECTORS = 180;
const MAX_DIVERSION_DEG = 150;

function _pruneIsochrone(points, startLat, startLon, destLat, destLon) {
    let cLat = 0, cLon = 0;
    for (const pt of points) { cLat += pt.lat; cLon += pt.lon; }
    cLat /= points.length; cLon /= points.length;
    const sectors = new Array(PRUNE_SECTORS).fill(null);
    let bestToDest = null, bestDistToDest = Infinity;
    for (const pt of points) {
        const brg = _bearingDeg(cLat, cLon, pt.lat, pt.lon);
        const sector = Math.floor(brg / (360 / PRUNE_SECTORS)) % PRUNE_SECTORS;
        const dist = _haversineNm(cLat, cLon, pt.lat, pt.lon);
        if (!sectors[sector] || dist > sectors[sector].dist) sectors[sector] = { pt, dist };
        const dDest = _haversineNm(pt.lat, pt.lon, destLat, destLon);
        if (dDest < bestDistToDest) { bestDistToDest = dDest; bestToDest = pt; }
    }
    const result = sectors.filter(s => s !== null).map(s => s.pt);
    if (bestToDest && !result.includes(bestToDest)) result.push(bestToDest);
    return result;
}

function _segmentCrossesLand(lat1, lon1, lat2, lon2) {
    const steps = Math.max(3, Math.ceil(_haversineNm(lat1, lon1, lat2, lon2) / 0.05));
    for (let i = 1; i < steps; i++) {
        const t = i / steps;
        if (_isTooCloseToLand(lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t)) return true;
    }
    return false;
}

function _backtrack(point) {
    const path = [];
    let p = point;
    while (p) {
        path.unshift({
            lat: p.lat, lon: p.lon, timeMs: p.timeMs, heading: p.heading,
            cBenefit: p.cBenefit || 0,
            tws: p.tws || 0, twa: p.twa || 0, bsp: p.bsp || 0,
            aws: p.aws || 0, awa: p.awa || 0,
        });
        p = p.parent;
    }
    return path;
}

function _pathDistance(path) {
    let d = 0;
    for (let i = 1; i < path.length; i++)
        d += _haversineNm(path[i - 1].lat, path[i - 1].lon, path[i].lat, path[i].lon);
    return Math.round(d * 10) / 10;
}

function _smoothPath(path) {
    if (path.length < 3) return path;
    let smoothed = [path[0]];
    let i = 0;
    while (i < path.length - 1) {
        let best = i + 1;
        for (let j = Math.min(i + 8, path.length - 1); j > i + 1; j--) {
            if (!_segmentCrossesLand(path[i].lat, path[i].lon, path[j].lat, path[j].lon)) {
                best = j; break;
            }
        }
        let sumB = 0, sumTws = 0, sumTwa = 0, sumBsp = 0, sumAws = 0, sumAwa = 0;
        for (let k = i + 1; k <= best; k++) {
            sumB += path[k].cBenefit; sumTws += path[k].tws; sumTwa += path[k].twa;
            sumBsp += path[k].bsp; sumAws += path[k].aws; sumAwa += path[k].awa;
        }
        const n = best - i;
        smoothed.push({ ...path[best],
            cBenefit: sumB / n, tws: sumTws / n, twa: sumTwa / n,
            bsp: sumBsp / n, aws: sumAws / n, awa: sumAwa / n,
        });
        i = best;
    }
    return smoothed;
}

// --- Main computation ---
self.onmessage = function(e) {
    const { params, sfbofsGrids: sfRaw, sfbofsGridsHR: sfHRRaw,
            hycomGrids: hyRaw, windGrids: wRaw, landPolygons, landGrid } = e.data;

    _landPolygons = landPolygons;
    _landGrid = landGrid;

    const sfbofsGrids = new Map(sfRaw);
    const sfbofsGridsHR = new Map(sfHRRaw);
    const hycomGrids = new Map(hyRaw);
    const windGrids = new Map(wRaw);

    const { startLat, startLon, endLat, endLon, startTimeMs, perfFactor } = params;

    let wavefront = [{ lat: startLat, lon: startLon, timeMs: startTimeMs, parent: null, heading: -1 }];
    const isochrones = [];
    const destBrg = _bearingDeg(startLat, startLon, endLat, endLon);
    let maxWindSeen = 0;
    let elapsedS = 0;
    let step = 0;
    const maxSteps = 1500;

    while (elapsedS < MAX_TIME_S && step < maxSteps) {
        const nearLand = wavefront.some(pt => _isTooCloseToLand(pt.lat, pt.lon));
        const stepS = nearLand ? TIME_STEP_S : TIME_STEP_OPEN_S;
        const dtHours = stepS / 3600;
        const newPoints = [];

        for (const pt of wavefront) {
            const current = interpolateCurrent(sfbofsGrids, sfbofsGridsHR, hycomGrids, startTimeMs, pt.lat, pt.lon, pt.timeMs);
            const wind = interpolateWind(windGrids, startTimeMs, pt.lat, pt.lon, pt.timeMs);
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

                const gvx = bsp * Math.sin(headingRad) + (current ? current.vx : 0);
                const gvy = bsp * Math.cos(headingRad) + (current ? current.vy : 0);
                const newLat = pt.lat + (gvy / NM_PER_DEG_LAT) * dtHours;
                const newLon = pt.lon + (gvx / (NM_PER_DEG_LAT * Math.cos(pt.lat * DEG2RAD))) * dtHours;

                const cBenefit = current ? current.vx * Math.sin(headingRad) + current.vy * Math.cos(headingRad) : 0;
                const twaRad = twa * DEG2RAD;
                const awx = wind.speed * Math.sin(twaRad);
                const awy = wind.speed * Math.cos(twaRad) - bsp;

                const newPt = {
                    lat: newLat, lon: newLon, timeMs: pt.timeMs + stepS * 1000,
                    parent: pt, heading: headingDeg, cBenefit,
                    tws: wind.speed, twa, bsp,
                    aws: Math.sqrt(awx * awx + awy * awy),
                    awa: Math.atan2(awx, awy) * RAD2DEG,
                };

                if (_haversineNm(newLat, newLon, endLat, endLon) < DEST_RADIUS_NM) {
                    const path = _smoothPath(_backtrack(newPt));
                    self.postMessage({ type: 'result', data: {
                        path, isochrones,
                        elapsedMin: Math.round((newPt.timeMs - startTimeMs) / 60000),
                        distanceNm: _pathDistance(path),
                    }});
                    return;
                }

                if (!_isTooCloseToLand(newLat, newLon) &&
                    !_segmentCrossesLand(pt.lat, pt.lon, newLat, newLon)) {
                    const ptBrg = _bearingDeg(startLat, startLon, newLat, newLon);
                    let brgDiff = Math.abs(ptBrg - destBrg);
                    if (brgDiff > 180) brgDiff = 360 - brgDiff;
                    if (brgDiff <= MAX_DIVERSION_DEG) newPoints.push(newPt);
                }
            }
        }

        if (newPoints.length === 0) {
            const err = maxWindSeen < 3
                ? 'Wind too light for routing (' + maxWindSeen.toFixed(1) + ' kn max)'
                : 'No reachable path \u2014 route may be blocked by land';
            self.postMessage({ type: 'result', data: { error: err } });
            return;
        }

        wavefront = _pruneIsochrone(newPoints, startLat, startLon, endLat, endLon);

        if (step % 5 === 0) {
            isochrones.push(wavefront.map(p => [p.lat, p.lon]));
            self.postMessage({ type: 'progress', elapsedS, maxTimeS: MAX_TIME_S });
        }

        elapsedS += stepS;
        step++;
    }

    const bestDist = Math.min(...wavefront.map(p => _haversineNm(p.lat, p.lon, endLat, endLon)));
    self.postMessage({ type: 'result', data: {
        error: 'Destination not reached within ' + Math.round(MAX_TIME_S / 60) +
               ' min (closest: ' + bestDist.toFixed(1) + ' nm away, wind: ' + maxWindSeen.toFixed(1) + ' kn)'
    }});
};
