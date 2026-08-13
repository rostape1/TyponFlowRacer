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

function apparentWind(twaDeg, tws, bsp) {
    const twaRad = twaDeg * Math.PI / 180;
    const awx = tws * Math.sin(twaRad);
    const awy = tws * Math.cos(twaRad) + bsp; // Boat moves forward into wind, so +bsp on boat-relative y-axis (forward axis)
    return {
        aws: Math.sqrt(awx * awx + awy * awy),
        awa: Math.atan2(awx, awy) * 180 / Math.PI,
    };
}

function getBoatSpeed(twaDeg, tws, perfFactor, polarFalloff = false) {
    if (twaDeg > 180) twaDeg = 360 - twaDeg; // Polar is symmetric port/starboard
    if (twaDeg < POLAR_TWA[0]) return 0;
    if (tws < 1) return 0;
    let lightAirScale = 1.0;
    if (tws < POLAR_TWS[0]) { lightAirScale = tws / POLAR_TWS[0]; tws = POLAR_TWS[0]; } // Linear fade to zero below 6kn (polar undefined in light air)
    const twaForLookup = Math.min(twaDeg, POLAR_TWA[POLAR_TWA.length - 1]);
    const twsClamped = Math.min(tws, POLAR_TWS[POLAR_TWS.length - 1]);
    let ti = 0;
    for (let i = 0; i < POLAR_TWA.length - 1; i++) {
        if (twaForLookup >= POLAR_TWA[i] && twaForLookup <= POLAR_TWA[i + 1]) { ti = i; break; }
    }
    let si = 0;
    for (let i = 0; i < POLAR_TWS.length - 1; i++) {
        if (twsClamped >= POLAR_TWS[i] && twsClamped <= POLAR_TWS[i + 1]) { si = i; break; }
    }
    const tFrac = (twaForLookup - POLAR_TWA[ti]) / (POLAR_TWA[ti + 1] - POLAR_TWA[ti]);
    const sFrac = (twsClamped - POLAR_TWS[si]) / (POLAR_TWS[si + 1] - POLAR_TWS[si]);
    let bsp = _lerp(_lerp(POLAR_BSP[ti][si], POLAR_BSP[ti][si + 1], sFrac),
                       _lerp(POLAR_BSP[ti + 1][si], POLAR_BSP[ti + 1][si + 1], sFrac), tFrac);
    // Falloff variant: linear degrade 1.0 @ TWA 150° → 0.85 @ TWA 180° (~17% loss dead-downwind)
    if (polarFalloff && twaDeg > 150) {
        bsp *= 1 - 0.15 * (twaDeg - 150) / 30;
    }
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
// gridOffsetH is the sub-hour fraction between startTimeMs and the absolute hour
// the first preloaded grid (index 0) actually represents. Without it, any
// non-hour-aligned forecast start would silently use grids up to 59 min off.
function interpolateCurrent(sfbofsGrids, sfbofsGridsHR, hycomGrids, startTimeMs, lat, lon, timeMs, gridOffsetH = 0) {
    const hoursFromStart = (timeMs - startTimeMs) / 3600000 + gridOffsetH;
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

function interpolateWind(windGrids, startTimeMs, lat, lon, timeMs, gridOffsetH = 0) {
    const hoursFromStart = (timeMs - startTimeMs) / 3600000 + gridOffsetH;
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
const NUM_HEADINGS_HR = 144;
const HEADING_STEP = 360 / NUM_HEADINGS;
const HEADING_STEP_HR = 360 / NUM_HEADINGS_HR;
const TIME_STEP_S = 120;
const TIME_STEP_HR_S = 60;
const TIME_STEP_OPEN_S = 300;
const MAX_TIME_S = 172800;
// Success radius: a candidate point within this distance of the destination
// counts as "arrived". Set wide enough that harbor approaches blocked by a
// final headland (e.g. Monterey's Pt Pinos) still terminate cleanly — the
// boat is plainly close enough to declare done and motor in.
const DEST_RADIUS_NM = 1.0;
const PRUNE_SECTORS = 180;
const MAX_DIVERSION_DEG = 180;

// High-resolution zone: Golden Gate + approaches (within ~1nm of narrows)
const HR_ZONE = { south: 37.78, north: 37.86, west: -122.53, east: -122.42 };

function _pruneIsochrone(points, startLat, startLon, destLat, destLon, useVMC) {
    const sectors = new Array(PRUNE_SECTORS).fill(null);
    let bestToDest = null, bestDistToDest = Infinity;
    const destBrgRad = _bearingDeg(startLat, startLon, destLat, destLon) * DEG2RAD;
    for (const pt of points) {
        const brg = _bearingDeg(startLat, startLon, pt.lat, pt.lon);
        const sector = Math.floor(brg / (360 / PRUNE_SECTORS)) % PRUNE_SECTORS;
        const distFromStart = _haversineNm(startLat, startLon, pt.lat, pt.lon);
        // VMC: bias toward sectors on the destination bearing while staying
        // strictly monotonic in distFromStart for any cos value. Earlier
        // formulations either let cos < 0 invert the ordering (`dist * cos`)
        // or hit exactly 0 at cos = -1 (`dist * (1 + cos) / 2`), which made
        // `>` comparisons in back-sectors break ties on iteration order
        // instead of distance — letting an arbitrarily-close-to-land point
        // win and collapsing the wavefront. `1 + 0.5*cos` stays in
        // [0.5, 1.5], always positive, so farther always beats closer.
        const score = useVMC
            ? distFromStart * (1 + 0.5 * Math.cos(brg * DEG2RAD - destBrgRad))
            : distFromStart;
        if (!sectors[sector] || score > sectors[sector].score) sectors[sector] = { pt, score };
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

function _segmentCrossesLandStrict(lat1, lon1, lat2, lon2) {
    const steps = Math.max(3, Math.ceil(_haversineNm(lat1, lon1, lat2, lon2) / 0.05));
    for (let i = 1; i < steps; i++) {
        const t = i / steps;
        if (_isLand(lat1 + (lat2 - lat1) * t, lon1 + (lon2 - lon1) * t)) return true;
    }
    return false;
}

// Within this radius of the destination, the safety buffer is dropped so the
// router can reach harbors, anchorages, and shoreline waypoints. Actual land
// is still avoided.
const DEST_APPROACH_NM = 1.0;

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

    const { startLat, startLon, endLat, endLon, startTimeMs, perfFactor, variant, gridOffsetH = 0 } = params;
    const useVMC = variant === 'vmc' || variant === 'both';
    const polarFalloff = variant === 'falloff' || variant === 'both';

    let wavefront = [{ lat: startLat, lon: startLon, timeMs: startTimeMs, parent: null, heading: -1 }];
    const isochrones = [];
    const destBrg = _bearingDeg(startLat, startLon, endLat, endLon);
    let maxWindSeen = 0;
    let elapsedS = 0;
    let step = 0;
    const maxSteps = 2500;
    // Track the closest-ever approach to the destination across the whole
    // search, not just at the final wavefront. When the wavefront stalls
    // outside a final headland (Monterey/Pt Pinos), the closest point is
    // reached early then gets pushed away as the wavefront thrashes — the
    // useful "best effort" path is the EARLY closest point, not whatever
    // happens to be nearest at MAX_TIME_S.
    let bestEverPt = null, bestEverDist = Infinity;

    while (elapsedS < MAX_TIME_S && step < maxSteps) {
        const nearLand = wavefront.some(pt => _isTooCloseToLand(pt.lat, pt.lon));
        const inHRZone = wavefront.some(pt =>
            pt.lat >= HR_ZONE.south && pt.lat <= HR_ZONE.north &&
            pt.lon >= HR_ZONE.west && pt.lon <= HR_ZONE.east);
        const stepS = inHRZone ? TIME_STEP_HR_S : (nearLand ? TIME_STEP_S : TIME_STEP_OPEN_S);
        const numH = inHRZone ? NUM_HEADINGS_HR : NUM_HEADINGS;
        const hStep = inHRZone ? HEADING_STEP_HR : HEADING_STEP;
        const dtHours = stepS / 3600;
        const newPoints = [];

        for (const pt of wavefront) {
            const current = interpolateCurrent(sfbofsGrids, sfbofsGridsHR, hycomGrids, startTimeMs, pt.lat, pt.lon, pt.timeMs, gridOffsetH);
            const windG = interpolateWind(windGrids, startTimeMs, pt.lat, pt.lon, pt.timeMs, gridOffsetH);

            // Polar takes TWA against wind-over-water (the wind the sails feel).
            // Subtract the current vector from the ground-frame wind. With 2-3 kn
            // currents this shifts effective TWA by 15-30°; using wind-over-ground
            // here was driving spurious detours.
            let tws = 0, windFromDeg = 0;
            if (windG) {
                const windU = windG.u - (current ? current.vx : 0);
                const windV = windG.v - (current ? current.vy : 0);
                tws = Math.sqrt(windU * windU + windV * windV);
                if (tws > maxWindSeen) maxWindSeen = tws;
                windFromDeg = (Math.atan2(-windU, -windV) * RAD2DEG + 360) % 360;
            }

            let anyExpanded = false;

            if (tws >= 0.5) {
                for (let hi = 0; hi < numH; hi++) {
                    const headingDeg = hi * hStep;
                    const headingRad = headingDeg * DEG2RAD;
                    let twa = Math.abs(headingDeg - windFromDeg);
                    if (twa > 180) twa = 360 - twa;
                    const bsp = getBoatSpeed(twa, tws, perfFactor, polarFalloff);
                    if (bsp < 0.5) continue;

                    // Tack/gybe penalty as a speed reduction during the step,
                    // not as extra time on this point. Previously we added
                    // tackTimePenaltyS to newPt.timeMs, which let wavefront
                    // points drift to different absolute times — breaking the
                    // equal-time isochrone invariant that pruning assumes.
                    // Now: the boat stalls for tackTimePenaltyS of the step
                    // and sails the remainder, so the *position* reflects the
                    // penalty but the *time* stays uniform across the wavefront.
                    let tackPenaltyS = 0;
                    if (pt.heading >= 0) {
                        let hdgChange = Math.abs(headingDeg - pt.heading);
                        if (hdgChange > 180) hdgChange = 360 - hdgChange;
                        if (hdgChange > 60) tackPenaltyS = 60;
                        else if (hdgChange > 30) tackPenaltyS = 20;
                    }
                    const sailFraction = Math.max(0, 1 - tackPenaltyS / stepS);
                    const effBsp = bsp * sailFraction;
                    if (effBsp < 0.5) continue;

                    const gvx = effBsp * Math.sin(headingRad) + (current ? current.vx : 0);
                    const gvy = effBsp * Math.cos(headingRad) + (current ? current.vy : 0);
                    const newLat = pt.lat + (gvy / NM_PER_DEG_LAT) * dtHours;
                    const newLon = pt.lon + (gvx / (NM_PER_DEG_LAT * Math.cos(pt.lat * DEG2RAD))) * dtHours;

                    const cBenefit = current ? current.vx * Math.sin(headingRad) + current.vy * Math.cos(headingRad) : 0;
                    const aw = apparentWind(twa, tws, bsp);

                    const newPt = {
                        lat: newLat, lon: newLon, timeMs: pt.timeMs + stepS * 1000,
                        parent: pt, heading: headingDeg, cBenefit,
                        tws, twa, bsp,
                        aws: aw.aws,
                        awa: aw.awa,
                    };

                    const distToDest = _haversineNm(newLat, newLon, endLat, endLon);
                    // Drop the safety buffer near the destination — users intentionally
                    // pick shoreline waypoints (harbors, anchorages) and the buffer would
                    // otherwise stall the wavefront 200m offshore.
                    const nearDest = distToDest < DEST_APPROACH_NM;
                    const landCheck = nearDest ? _isLand : _isTooCloseToLand;
                    const segCheck  = nearDest ? _segmentCrossesLandStrict : _segmentCrossesLand;

                    if (distToDest < DEST_RADIUS_NM &&
                        !_isLand(newLat, newLon) &&
                        !_segmentCrossesLandStrict(pt.lat, pt.lon, newLat, newLon)) {
                        const path = _backtrack(newPt);
                        self.postMessage({ type: 'result', data: {
                            path, isochrones,
                            elapsedMin: Math.round((newPt.timeMs - startTimeMs) / 60000),
                            distanceNm: _pathDistance(path),
                        }});
                        return;
                    }

                    if (!landCheck(newLat, newLon) &&
                        !segCheck(pt.lat, pt.lon, newLat, newLon)) {
                        const ptBrg = _bearingDeg(startLat, startLon, newLat, newLon);
                        let brgDiff = Math.abs(ptBrg - destBrg);
                        if (brgDiff > 180) brgDiff = 360 - brgDiff;
                        if (brgDiff <= MAX_DIVERSION_DEG) {
                            newPoints.push(newPt);
                            anyExpanded = true;
                        }
                    }
                }
            }

            // Drift fallback for dead-air points: if no heading produced a viable
            // expansion (either wind below the polar threshold or every heading
            // hit land), advance this point by the current vector alone so it
            // can wait out the wind hole. Without this, calm patches silently
            // drop points from the wavefront and the engine raises a false
            // "no reachable path" error when the route just needs to drift for
            // a step or two.
            //
            // BUT: only drift if the current is meaningful (>= 0.2 kn). Without
            // this guard, a point cornered against a leeward shore (all 72
            // sailing headings hit land) with no current to carry it re-spawns
            // itself at the same position every iteration forever — a "ghost
            // stuck point" that pollutes the wavefront and produces a backtrack
            // chain with 27+ hours of zero-motion drift. Better to let the
            // point die: the wavefront still has many other points that can
            // make progress, and the route-quality search prefers them.
            const DRIFT_MIN_KN = 0.2;
            const cspd = current ? Math.sqrt(current.vx * current.vx + current.vy * current.vy) : 0;
            if (!anyExpanded && cspd >= DRIFT_MIN_KN) {
                const driftLat = pt.lat + (current.vy / NM_PER_DEG_LAT) * dtHours;
                const driftLon = pt.lon + (current.vx / (NM_PER_DEG_LAT * Math.cos(pt.lat * DEG2RAD))) * dtHours;
                const distToDest = _haversineNm(driftLat, driftLon, endLat, endLon);
                const nearDest = distToDest < DEST_APPROACH_NM;
                const landCheck = nearDest ? _isLand : _isTooCloseToLand;
                const segCheck  = nearDest ? _segmentCrossesLandStrict : _segmentCrossesLand;
                if (!landCheck(driftLat, driftLon) &&
                    !segCheck(pt.lat, pt.lon, driftLat, driftLon)) {
                    newPoints.push({
                        lat: driftLat, lon: driftLon,
                        timeMs: pt.timeMs + stepS * 1000,
                        parent: pt,
                        // Preserve the previous heading so a subsequent sailing
                        // step into wind doesn't get charged a phantom tack
                        // penalty just for coming out of drift.
                        heading: pt.heading,
                        cBenefit: 0, tws, twa: 0, bsp: 0, aws: 0, awa: 0,
                    });
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

        wavefront = _pruneIsochrone(newPoints, startLat, startLon, endLat, endLon, useVMC);

        for (const p of wavefront) {
            const d = _haversineNm(p.lat, p.lon, endLat, endLon);
            if (d < bestEverDist) { bestEverDist = d; bestEverPt = p; }
        }

        if (step % 5 === 0) {
            isochrones.push(wavefront.map(p => [p.lat, p.lon]));
            self.postMessage({ type: 'progress', elapsedS, maxTimeS: MAX_TIME_S });
        }

        elapsedS += stepS;
        step++;
    }

    // Best-effort fallback: the wavefront ran out of time (or stalled outside a
    // headland the buffer won't let it round) but a usable closest-approach
    // exists. If that point sits within SOFT_DEST_RADIUS_NM of the goal,
    // return its path with `partial: true` and `closestNm` set so the UI/log
    // can say "best effort, X nm short" instead of dropping the whole route.
    // Sized to cover the Monterey/Pt Pinos approach (≈2.8 nm gridlock).
    const SOFT_DEST_RADIUS_NM = 3.0;
    if (bestEverPt && bestEverDist <= SOFT_DEST_RADIUS_NM) {
        const path = _backtrack(bestEverPt);
        self.postMessage({ type: 'result', data: {
            path, isochrones,
            elapsedMin: Math.round((bestEverPt.timeMs - startTimeMs) / 60000),
            distanceNm: _pathDistance(path),
            partial: true,
            closestNm: Math.round(bestEverDist * 10) / 10,
        }});
        return;
    }
    self.postMessage({ type: 'result', data: {
        error: 'Destination not reached within ' + Math.round(MAX_TIME_S / 60) +
               ' min (closest: ' + bestEverDist.toFixed(1) + ' nm away, wind: ' + maxWindSeen.toFixed(1) + ' kn)'
    }});
};
