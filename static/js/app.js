// --- Config ---
const OWN_MMSI = 338361814;
const TRACK_HOURS = 2;
const STALE_MINUTES = 10;

// --- State ---
const vessels = new Map();       // mmsi → vessel data
const markers = new Map();       // mmsi → Leaflet marker
const trackLines = new Map();    // mmsi → Leaflet polyline
const currentMarkers = new Map(); // station_id → Leaflet marker
let currentLayer = null;         // Leaflet layer group for currents
let ownPosition = null;
let ownVessel = null;  // full own vessel data (for SOG/COG)
let messageCount = 0;
const hiddenVessels = new Set();  // mmsi values of vessels hidden from map

// --- Color mapping ---
const TYPE_COLORS = {
    'Sailing/Pleasure': '#3498db',
    'Cargo': '#2ecc71',
    'Tanker': '#e74c3c',
    'Passenger': '#9b59b6',
    'Fishing/Towing/Dredging': '#f1c40f',
    'High Speed Craft': '#e67e22',
    'Wing in Ground': '#1abc9c',
    'Special Craft': '#e67e22',
};
const DEFAULT_COLOR = '#95a5a6';
const OWN_COLOR = '#f39c12';

function getVesselColor(vessel) {
    if (vessel.mmsi === OWN_MMSI) return OWN_COLOR;
    return TYPE_COLORS[vessel.ship_category] || DEFAULT_COLOR;
}

function getTypeCssClass(category) {
    if (!category) return 'type-unknown';
    if (category.includes('Sailing') || category.includes('Pleasure')) return 'type-sailing';
    if (category.includes('Cargo')) return 'type-cargo';
    if (category.includes('Tanker')) return 'type-tanker';
    if (category.includes('Passenger')) return 'type-passenger';
    if (category.includes('Fishing')) return 'type-fishing';
    return 'type-other';
}

// --- Haversine distance (nautical miles) ---
function haversineNm(lat1, lon1, lat2, lon2) {
    const R = 3440.065; // Earth radius in nm
    const dLat = (lat2 - lat1) * Math.PI / 180;
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const a = Math.sin(dLat / 2) ** 2 +
        Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
        Math.sin(dLon / 2) ** 2;
    return 2 * R * Math.asin(Math.sqrt(a));
}

// --- Bearing from own vessel ---
function bearingTo(lat1, lon1, lat2, lon2) {
    const dLon = (lon2 - lon1) * Math.PI / 180;
    const y = Math.sin(dLon) * Math.cos(lat2 * Math.PI / 180);
    const x = Math.cos(lat1 * Math.PI / 180) * Math.sin(lat2 * Math.PI / 180) -
        Math.sin(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) * Math.cos(dLon);
    return (Math.atan2(y, x) * 180 / Math.PI + 360) % 360;
}

// --- CPA / TCPA calculation ---
// Returns { tcpa (minutes), cpa (nm), closing (bool), label (string) }
function computeCpaTcpa(own, target) {
    if (!own || !target) return null;
    if (own.lat == null || target.lat == null) return null;
    if (own.sog == null || target.sog == null) return null;
    if (own.cog == null || target.cog == null) return null;

    // Convert positions to flat nm coordinates (approximate, fine for short distances)
    const cosLat = Math.cos((own.lat + target.lat) / 2 * Math.PI / 180);
    const dx = (target.lon - own.lon) * 60 * cosLat;  // nm east
    const dy = (target.lat - own.lat) * 60;            // nm north

    // Velocity vectors (nm/min)
    const ownVx = own.sog / 60 * Math.sin(own.cog * Math.PI / 180);
    const ownVy = own.sog / 60 * Math.cos(own.cog * Math.PI / 180);
    const tgtVx = target.sog / 60 * Math.sin(target.cog * Math.PI / 180);
    const tgtVy = target.sog / 60 * Math.cos(target.cog * Math.PI / 180);

    // Relative position and velocity
    const rx = dx;
    const ry = dy;
    const rvx = tgtVx - ownVx;
    const rvy = tgtVy - ownVy;

    const rvSq = rvx * rvx + rvy * rvy;

    // Both stationary or same course/speed
    if (rvSq < 0.0000001) {
        const dist = Math.sqrt(dx * dx + dy * dy);
        return { tcpa: Infinity, cpa: dist, closing: false, label: 'Parallel' };
    }

    // TCPA = -(r · rv) / |rv|²
    const tcpa = -(rx * rvx + ry * rvy) / rvSq;  // in minutes

    // CPA distance
    const cpx = rx + rvx * tcpa;
    const cpy = ry + rvy * tcpa;
    const cpa = Math.sqrt(cpx * cpx + cpy * cpy);

    const currentDist = Math.sqrt(dx * dx + dy * dy);
    const closing = tcpa > 0;

    let label;
    if (tcpa <= 0) {
        label = 'Diverging';
    } else if (cpa < 0.1 && tcpa < 30) {
        // Very close approach within 30 min
        label = `COLLISION RISK ${formatTime(tcpa)}`;
    } else if (cpa < 0.5 && tcpa < 60) {
        label = `Close approach ${formatTime(tcpa)} (${cpa.toFixed(1)} nm)`;
    } else {
        label = `CPA ${formatTime(tcpa)} (${cpa.toFixed(1)} nm)`;
    }

    return { tcpa, cpa, closing, label };
}

function formatTime(minutes) {
    if (minutes === Infinity) return '--';
    if (minutes < 1) return '<1 min';
    if (minutes < 60) return `${Math.round(minutes)} min`;
    const h = Math.floor(minutes / 60);
    const m = Math.round(minutes % 60);
    return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

// --- Create vessel arrow icon ---
function createVesselIcon(vessel) {
    const color = getVesselColor(vessel);
    const isOwn = vessel.mmsi === OWN_MMSI;
    const size = isOwn ? 28 : 20;
    const heading = vessel.heading || vessel.cog || 0;

    if (!isOwn) {
        const svg = `<svg width="${size}" height="${size}" viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">
            <g transform="rotate(${heading}, 12, 12)">
                <path d="M12 2 L6 20 L12 16 L18 20 Z" fill="${color}" stroke="rgba(0,0,0,0.4)" stroke-width="1"/>
            </g>
        </svg>`;
        return L.divIcon({
            html: svg,
            className: 'vessel-icon',
            iconSize: [size, size],
            iconAnchor: [size / 2, size / 2],
        });
    }

    // Own vessel: larger canvas with three directional arrows
    const ownSize = 150;
    const cx = ownSize / 2;
    const cy = ownSize / 2;
    const boatR = 14;  // vessel triangle size
    const headingDeg = vessel.heading != null && vessel.heading < 360 ? vessel.heading : null;
    const cogDeg = vessel.cog != null && vessel.cog < 360 ? vessel.cog : null;
    const boatRotation = headingDeg != null ? headingDeg : (cogDeg != null ? cogDeg : 0);

    // Get tidal current at own vessel position
    let tideDeg = null;
    let tideSpeed = 0;
    if (tidalFlow && vessel.lat != null) {
        const tc = tidalFlow._interpolateAt(vessel.lat, vessel.lon);
        if (tc && tc.speed > 0.05) {
            tideDeg = (Math.atan2(tc.vx, tc.vy) * 180 / Math.PI + 360) % 360;
            tideSpeed = tc.speed;
        }
    }

    // Get wind at own vessel position
    let windDeg = null;
    let windSpeed = 0;
    if (windOverlay && vessel.lat != null) {
        const w = windOverlay.interpolateAt(vessel.lat, vessel.lon);
        if (w && w.speed > 0.5) {
            windDeg = w.dir;
            windSpeed = w.speed;
        }
    }

    // Arrow line builder: from center outward at given angle
    const arrowLen = 55;
    function arrowAt(deg, len) {
        const rad = (deg - 90) * Math.PI / 180;  // SVG: 0° = up → rotate from east
        const ex = cx + Math.cos(rad) * len;
        const ey = cy + Math.sin(rad) * len;
        // Arrowhead
        const headLen = 10;
        const headAng = 25 * Math.PI / 180;
        const ax1 = ex - headLen * Math.cos(rad - headAng);
        const ay1 = ey - headLen * Math.sin(rad - headAng);
        const ax2 = ex - headLen * Math.cos(rad + headAng);
        const ay2 = ey - headLen * Math.sin(rad + headAng);
        return `<line x1="${cx}" y1="${cy}" x2="${ex}" y2="${ey}"/>` +
               `<polygon points="${ex},${ey} ${ax1},${ay1} ${ax2},${ay2}"/>`;
    }

    // Build arrow SVG groups
    let arrows = '';
    // 1. Heading arrow (white, dashed) — where the bow is pointing
    if (headingDeg != null) {
        arrows += `<g class="own-arrow-heading" stroke="#ffffff" fill="#ffffff" stroke-width="1.5" stroke-dasharray="3 2" opacity="0.8">${arrowAt(headingDeg, arrowLen)}</g>`;
    }
    // 2. COG arrow (green, solid) — where the vessel is actually moving
    if (cogDeg != null) {
        arrows += `<g class="own-arrow-cog" stroke="#2ecc71" fill="#2ecc71" stroke-width="2" opacity="0.9">${arrowAt(cogDeg, arrowLen)}</g>`;
    }
    // 3. Tide arrow (cyan) — direction of tidal current push
    if (tideDeg != null) {
        const tideLen = Math.min(arrowLen, Math.max(25, tideSpeed * 20));
        arrows += `<g class="own-arrow-tide" stroke="#00d4ff" fill="#00d4ff" stroke-width="1.5" opacity="0.85">${arrowAt(tideDeg, tideLen)}</g>`;
    }
    // 4. Wind arrow (purple, dashed) — wind direction
    if (windDeg != null) {
        const windLen = Math.min(arrowLen, Math.max(25, windSpeed * 2.5));
        arrows += `<g class="own-arrow-wind" stroke="#aa46be" fill="#aa46be" stroke-width="1.5" stroke-dasharray="4 2" opacity="0.8">${arrowAt(windDeg, windLen)}</g>`;
    }

    const svg = `<svg width="${ownSize}" height="${ownSize}" viewBox="0 0 ${ownSize} ${ownSize}" xmlns="http://www.w3.org/2000/svg">
        ${arrows}
        <g transform="translate(${cx}, ${cy}) rotate(${boatRotation}) translate(-12, -12)">
            <path d="M12 2 L6 20 L12 16 L18 20 Z" fill="${color}" stroke="rgba(0,0,0,0.5)" stroke-width="1"/>
        </g>
    </svg>`;

    return L.divIcon({
        html: svg,
        className: 'vessel-icon own-vessel-icon',
        iconSize: [ownSize, ownSize],
        iconAnchor: [cx, cy],
    });
}

// --- Map setup ---
const map = L.map('map', {
    center: [37.81, -122.41],  // SF Bay default
    zoom: 13,
    zoomControl: true,
});

// Tile layers — local tiles with online fallback
// Local tiles served at /static/tiles/{source}/{z}/{x}/{y}.png
const osmLayer = L.tileLayer('/static/tiles/osm/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
    errorTileUrl: '',
});

const darkLayer = L.tileLayer('/static/tiles/dark/{z}/{x}/{y}.png', {
    attribution: '&copy; CartoDB',
    maxZoom: 19,
    errorTileUrl: '',
});

// Fallback online layers (used if no local tiles exist)
const osmOnline = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
});

const darkOnline = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; CartoDB',
    maxZoom: 19,
});

const seaLayer = L.tileLayer('/static/tiles/sea/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenSeaMap',
    maxZoom: 18,
    opacity: 0.8,
});

const seaOnline = L.tileLayer('https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenSeaMap',
    maxZoom: 18,
    opacity: 0.8,
});

// NOAA Nautical Charts — full depth soundings, contours, channels, hazards
const noaaChart = L.tileLayer('https://tileservice.charts.noaa.gov/tiles/50000_1/{z}/{x}/{y}.png', {
    attribution: '&copy; NOAA',
    maxZoom: 18,
    opacity: 0.9,
});

const noaaChartOffline = L.tileLayer('/static/tiles/noaa/{z}/{x}/{y}.png', {
    attribution: '&copy; NOAA',
    maxZoom: 18,
    opacity: 0.9,
    errorTileUrl: '',
});

// Default: dark base + sea overlay
darkLayer.addTo(map);
seaLayer.addTo(map);

// Current arrows layer group
currentLayer = L.layerGroup().addTo(map);

L.control.layers({
    'Dark': darkLayer,
    'Dark (online)': darkOnline,
    'NOAA Chart': noaaChart,
    'NOAA Chart (offline)': noaaChartOffline,
    'Street': osmLayer,
    'Street (online)': osmOnline,
}, {
    'Nautical Marks': seaLayer,
    'Nautical Marks (online)': seaOnline,
    'Currents': currentLayer,
}, { position: 'topleft' }).addTo(map);

// --- Popup content ---
function buildPopupHtml(v) {
    const dist = ownPosition && v.mmsi !== OWN_MMSI
        ? haversineNm(ownPosition.lat, ownPosition.lon, v.lat, v.lon).toFixed(1) + ' nm'
        : '—';
    const bearing = ownPosition && v.mmsi !== OWN_MMSI
        ? bearingTo(ownPosition.lat, ownPosition.lon, v.lat, v.lon).toFixed(0) + '°'
        : '—';

    const speedDiff = (v.mmsi !== OWN_MMSI && ownVessel && ownVessel.sog != null && v.sog != null)
        ? (v.sog - ownVessel.sog) : null;
    const speedDiffStr = speedDiff != null
        ? (speedDiff >= 0 ? '+' : '') + speedDiff.toFixed(1) + ' kn'
        : '—';

    const cpaInfo = (v.mmsi !== OWN_MMSI && ownVessel) ? computeCpaTcpa(ownVessel, v) : null;
    const cpaClass = cpaInfo && cpaInfo.cpa < 0.1 && cpaInfo.tcpa > 0 && cpaInfo.tcpa < 30
        ? 'popup-value cpa-danger'
        : cpaInfo && cpaInfo.cpa < 0.5 && cpaInfo.tcpa > 0 && cpaInfo.tcpa < 60
            ? 'popup-value cpa-warn'
            : 'popup-value';

    // Get tidal current at vessel position (for own vessel popup)
    let tideStr = '—';
    if (v.mmsi === OWN_MMSI && tidalFlow && v.lat != null) {
        const tc = tidalFlow._interpolateAt(v.lat, v.lon);
        if (tc && tc.speed > 0.05) {
            const tidDir = (Math.atan2(tc.vx, tc.vy) * 180 / Math.PI + 360) % 360;
            tideStr = tc.speed.toFixed(1) + ' kn / ' + tidDir.toFixed(0) + '°';
        }
    }

    // Get wind at vessel position (for all vessels)
    let windStr = '—';
    if (windOverlay && v.lat != null) {
        const w = windOverlay.interpolateAt(v.lat, v.lon);
        if (w && w.speed > 0.5) {
            windStr = w.speed.toFixed(1) + ' kn / ' + w.dir.toFixed(0) + '°';
            if (w.gust > w.speed + 1) {
                windStr += ' (G' + w.gust.toFixed(0) + ')';
            }
        }
    }

    return `<div class="popup-content">
        <h3>${v.name || 'MMSI ' + v.mmsi}${v.mmsi === OWN_MMSI ? ' (You)' : ''}</h3>
        <div class="popup-row"><span class="popup-label">MMSI</span><span class="popup-value">${v.mmsi}</span></div>
        <div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">${v.ship_category || 'Unknown'}</span></div>
        <div class="popup-row"><span class="popup-label">SOG</span><span class="popup-value">${v.sog != null ? v.sog.toFixed(1) + ' kn' : '—'}</span></div>
        <div class="popup-row"><span class="popup-label">COG</span><span class="popup-value">${v.cog != null ? v.cog.toFixed(0) + '°' : '—'}</span></div>
        ${v.mmsi === OWN_MMSI ? `<div class="popup-row"><span class="popup-label">Tide</span><span class="popup-value" style="color:#00d4ff">${tideStr}</span></div>` : ''}
        <div class="popup-row"><span class="popup-label">Wind</span><span class="popup-value" style="color:#aa46be">${windStr}</span></div>
        <div class="popup-row"><span class="popup-label">Avg Speed</span><span class="popup-value">${v.avg_speed != null ? v.avg_speed + ' kn' : '—'}</span></div>
        <div class="popup-row"><span class="popup-label">Distance</span><span class="popup-value">${dist}</span></div>
        <div class="popup-row"><span class="popup-label">Bearing</span><span class="popup-value">${bearing}</span></div>
        ${cpaInfo ? `<div class="popup-row"><span class="popup-label">CPA/TCPA</span><span class="${cpaClass}">${cpaInfo.label}</span></div>` : ''}
        ${v.mmsi !== OWN_MMSI ? `<div class="popup-row"><span class="popup-label">Speed Diff</span><span class="popup-value">${speedDiffStr}</span></div>` : ''}
        ${v.destination ? `<div class="popup-row"><span class="popup-label">Dest</span><span class="popup-value">${v.destination}</span></div>` : ''}
        <div class="popup-chart" id="chart-${v.mmsi}">
            <div class="popup-chart-label">Speed History</div>
            <div class="popup-chart-loading">Loading...</div>
        </div>
    </div>`;
}

// --- Speed history chart (pure SVG) ---
function buildSpeedChart(points, color) {
    if (!points || points.length < 2) return '<div class="popup-chart-empty">Not enough data</div>';

    const W = 220, H = 90, PAD = 22;
    const speeds = points.map(p => p.sog != null ? p.sog : 0);
    const times = points.map(p => new Date(p.timestamp + 'Z').getTime());

    // Auto-scale Y axis to actual speed range with 10% padding
    const rawMin = Math.min(...speeds);
    const rawMax = Math.max(...speeds);
    const range = rawMax - rawMin || 1;
    const minSpeed = Math.max(0, rawMin - range * 0.1);
    const maxSpeed = rawMax + range * 0.1;
    const speedRange = maxSpeed - minSpeed || 1;

    const minTime = Math.min(...times);
    const maxTime = Math.max(...times);
    const timeRange = maxTime - minTime || 1;

    // Build individual segments colored by acceleration/deceleration
    const midSpeed = (minSpeed + maxSpeed) / 2;
    const coords = speeds.map((s, i) => ({
        x: PAD + (times[i] - minTime) / timeRange * (W - PAD * 2),
        y: H - PAD - ((s - minSpeed) / speedRange) * (H - PAD * 2),
        speed: s,
    }));

    // Full path for fill area (use neutral color)
    const fullPath = coords.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(' ');
    const fillPath = fullPath + ` L${coords[coords.length - 1].x.toFixed(1)},${H - PAD} L${coords[0].x.toFixed(1)},${H - PAD} Z`;

    // Colored line segments
    const segments = [];
    for (let i = 1; i < coords.length; i++) {
        const diff = coords[i].speed - coords[i - 1].speed;
        const segColor = diff > 0.05 ? '#2ecc71' : diff < -0.05 ? '#e74c3c' : '#7a8fa6';
        segments.push(`<line x1="${coords[i-1].x.toFixed(1)}" y1="${coords[i-1].y.toFixed(1)}" x2="${coords[i].x.toFixed(1)}" y2="${coords[i].y.toFixed(1)}" stroke="${segColor}" stroke-width="1.8" stroke-linecap="round"/>`);
    }

    // Time labels
    const startLabel = new Date(minTime).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', timeZone: 'America/Los_Angeles' });
    const endLabel = new Date(maxTime).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', timeZone: 'America/Los_Angeles' });

    return `<svg width="${W}" height="${H + 14}" viewBox="0 0 ${W} ${H + 14}">
        <!-- Grid lines -->
        <line x1="${PAD}" y1="${PAD}" x2="${W - PAD}" y2="${PAD}" stroke="rgba(100,150,200,0.1)" stroke-width="0.5"/>
        <line x1="${PAD}" y1="${H / 2}" x2="${W - PAD}" y2="${H / 2}" stroke="rgba(100,150,200,0.1)" stroke-width="0.5"/>
        <line x1="${PAD}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}" stroke="rgba(100,150,200,0.15)" stroke-width="0.5"/>
        <!-- Fill -->
        <path d="${fillPath}" fill="${color}" opacity="0.08"/>
        <!-- Colored segments: green=accelerating, red=slowing, grey=steady -->
        ${segments.join('\n        ')}
        <!-- Y axis labels -->
        <text x="${PAD - 3}" y="${PAD + 3}" text-anchor="end" fill="#5a7a9a" font-size="8">${maxSpeed.toFixed(1)}</text>
        <text x="${PAD - 3}" y="${H / 2 + 3}" text-anchor="end" fill="#5a7a9a" font-size="7">${midSpeed.toFixed(1)}</text>
        <text x="${PAD - 3}" y="${H - PAD + 3}" text-anchor="end" fill="#5a7a9a" font-size="8">${minSpeed.toFixed(1)}</text>
        <!-- X axis time labels -->
        <text x="${PAD}" y="${H + 10}" text-anchor="start" fill="#5a7a9a" font-size="8">${startLabel}</text>
        <text x="${W - PAD}" y="${H + 10}" text-anchor="end" fill="#5a7a9a" font-size="8">${endLabel}</text>
        <!-- Unit -->
        <text x="${W / 2}" y="${PAD - 4}" text-anchor="middle" fill="#5a7a9a" font-size="8">kn</text>
    </svg>`;
}

async function loadSpeedChart(mmsi) {
    // Small delay to ensure popup DOM is rendered
    await new Promise(r => setTimeout(r, 50));
    const chartEl = document.getElementById(`chart-${mmsi}`);
    if (!chartEl) return;

    try {
        const res = await fetch(`/api/vessels/${mmsi}/track?hours=2`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const track = await res.json();
        const withSpeed = track.filter(p => p.sog != null);

        const color = getVesselColor(vessels.get(mmsi) || { mmsi });
        chartEl.innerHTML = `<div class="popup-chart-label">Speed History (2h)</div>` + buildSpeedChart(withSpeed, color);
    } catch (e) {
        console.error('Chart load error:', e);
        chartEl.innerHTML = `<div class="popup-chart-empty">Failed to load: ${e.message}</div>`;
    }
}

// --- Update or create marker ---
function updateMarker(v) {
    if (v.lat == null || v.lon == null) return;

    const latlng = [v.lat, v.lon];
    const isHidden = hiddenVessels.has(v.mmsi);

    if (markers.has(v.mmsi)) {
        const marker = markers.get(v.mmsi);
        marker.setLatLng(latlng);
        marker.setIcon(createVesselIcon(v));
        // Only update popup content if it's NOT currently open (avoids resetting the chart)
        if (!marker.isPopupOpen()) {
            marker.getPopup().setContent(buildPopupHtml(v));
        }
        // Sync visibility
        if (isHidden && map.hasLayer(marker)) {
            map.removeLayer(marker);
        } else if (!isHidden && !map.hasLayer(marker)) {
            marker.addTo(map);
        }
    } else {
        const marker = L.marker(latlng, {
            icon: createVesselIcon(v),
            zIndexOffset: v.mmsi === OWN_MMSI ? 1000 : 0,
        })
            .bindPopup(buildPopupHtml(v), { className: 'vessel-popup', maxWidth: 260 })
            .on('popupopen', () => loadSpeedChart(v.mmsi));
        if (!isHidden) marker.addTo(map);
        markers.set(v.mmsi, marker);
    }

    // Update track trail
    if (!trackLines.has(v.mmsi)) {
        const color = getVesselColor(v);
        const line = L.polyline([], {
            color: color,
            weight: v.mmsi === OWN_MMSI ? 2.5 : 1.5,
            opacity: 0.6,
            dashArray: v.mmsi === OWN_MMSI ? null : '4 4',
        });
        if (!isHidden) line.addTo(map);
        trackLines.set(v.mmsi, { line, points: [] });
    }

    const track = trackLines.get(v.mmsi);
    track.points.push({ lat: v.lat, lon: v.lon, time: Date.now() });

    // Prune points older than TRACK_HOURS
    const cutoff = Date.now() - TRACK_HOURS * 3600 * 1000;
    track.points = track.points.filter(p => p.time >= cutoff);
    track.line.setLatLngs(track.points.map(p => [p.lat, p.lon]));
}

// --- Toggle vessel visibility on map ---
function toggleVessel(mmsi) {
    if (hiddenVessels.has(mmsi)) {
        hiddenVessels.delete(mmsi);
        const marker = markers.get(mmsi);
        if (marker) marker.addTo(map);
        const track = trackLines.get(mmsi);
        if (track) track.line.addTo(map);
    } else {
        hiddenVessels.add(mmsi);
        const marker = markers.get(mmsi);
        if (marker) map.removeLayer(marker);
        const track = trackLines.get(mmsi);
        if (track) map.removeLayer(track.line);
    }
    updatePanel();
}

// --- Side panel ---
function updatePanel() {
    const list = document.getElementById('vessel-list');
    const vesselArray = Array.from(vessels.values()).filter(v => v.lat != null);

    // Sort: own vessel first, then by distance
    vesselArray.sort((a, b) => {
        if (a.mmsi === OWN_MMSI) return -1;
        if (b.mmsi === OWN_MMSI) return 1;
        if (!ownPosition) return 0;
        const da = haversineNm(ownPosition.lat, ownPosition.lon, a.lat, a.lon);
        const db = haversineNm(ownPosition.lat, ownPosition.lon, b.lat, b.lon);
        return da - db;
    });

    list.innerHTML = vesselArray.map(v => {
        const isOwn = v.mmsi === OWN_MMSI;
        const lastUpdate = v._lastUpdate || 0;
        const isStale = (Date.now() - lastUpdate) > STALE_MINUTES * 60 * 1000;

        const dist = ownPosition && !isOwn
            ? haversineNm(ownPosition.lat, ownPosition.lon, v.lat, v.lon).toFixed(1)
            : null;
        const bearing = ownPosition && !isOwn
            ? bearingTo(ownPosition.lat, ownPosition.lon, v.lat, v.lon).toFixed(0)
            : null;

        const typeClass = getTypeCssClass(v.ship_category);
        const cpa = (!isOwn && ownVessel) ? computeCpaTcpa(ownVessel, v) : null;
        const cpaHtml = cpa ? `<span class="${cpa.cpa < 0.1 && cpa.tcpa > 0 && cpa.tcpa < 30 ? 'cpa-danger' : cpa.cpa < 0.5 && cpa.tcpa > 0 && cpa.tcpa < 60 ? 'cpa-warn' : ''}">${cpa.label}</span>` : '';

        const isVisible = !hiddenVessels.has(v.mmsi);

        return `<div class="vessel-card ${isOwn ? 'own-vessel' : ''} ${isStale ? 'stale' : ''} ${!isVisible ? 'vessel-hidden' : ''}"
                     data-mmsi="${v.mmsi}">
            <div class="vessel-name">
                ${v.name || 'MMSI ' + v.mmsi}
                <span class="vessel-type-badge ${typeClass}">${v.ship_category || 'Unknown'}</span>
                <button class="vessel-toggle ${isVisible ? '' : 'toggled-off'}" data-mmsi="${v.mmsi}" title="${isVisible ? 'Hide from map' : 'Show on map'}" aria-pressed="${isVisible}" aria-label="${isVisible ? 'Hide' : 'Show'} ${v.name || 'MMSI ' + v.mmsi} on map">
                    ${isVisible ? '&#9673;' : '&#9675;'}
                </button>
            </div>
            <div class="vessel-meta">
                ${v.sog != null ? `<span>${v.sog.toFixed(1)} kn</span>` : ''}
                ${v.avg_speed != null ? `<span>avg ${v.avg_speed} kn</span>` : ''}
                ${dist ? `<span>${dist} nm</span>` : ''}
                ${bearing ? `<span>${bearing}°</span>` : ''}
            </div>
            ${cpaHtml ? `<div class="vessel-cpa">${cpaHtml}</div>` : ''}
        </div>`;
    }).join('');

    // Click handlers — zoom to vessel
    list.querySelectorAll('.vessel-card').forEach(card => {
        card.addEventListener('click', (e) => {
            if (e.target.closest('.vessel-toggle')) return; // don't zoom when toggling
            const mmsi = parseInt(card.dataset.mmsi);
            const v = vessels.get(mmsi);
            if (v && v.lat != null) {
                map.setView([v.lat, v.lon], Math.max(map.getZoom(), 14));
                const marker = markers.get(mmsi);
                if (marker) marker.openPopup();
            }
        });
    });

    // Toggle visibility handlers
    list.querySelectorAll('.vessel-toggle').forEach(btn => {
        btn.addEventListener('click', (e) => {
            e.stopPropagation();
            toggleVessel(parseInt(btn.dataset.mmsi));
        });
    });

    // Update status bar
    document.getElementById('vessel-count').textContent = `${vesselArray.length} vessels`;
    document.getElementById('message-count').textContent = `${messageCount} msgs`;
}

// --- WebSocket ---
function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(`${protocol}//${window.location.host}/ws`);

    ws.onopen = () => {
        document.getElementById('connection-status').textContent = 'Connected';
        document.getElementById('connection-status').className = 'status-connected';
    };

    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        messageCount++;

        // Merge with existing vessel data
        const existing = vessels.get(data.mmsi) || {};
        const merged = { ...existing, ...data, _lastUpdate: Date.now() };
        vessels.set(data.mmsi, merged);

        // Track own position
        if (data.mmsi === OWN_MMSI && data.lat != null) {
            ownPosition = { lat: data.lat, lon: data.lon };
            ownVessel = merged;
        }

        updateMarker(merged);
        updatePanel();
    };

    ws.onclose = () => {
        document.getElementById('connection-status').textContent = 'Disconnected';
        document.getElementById('connection-status').className = 'status-disconnected';
        setTimeout(connectWebSocket, 3000);
    };

    ws.onerror = () => ws.close();
}

// --- Panel toggle ---
document.getElementById('panel-toggle').addEventListener('click', () => {
    const panel = document.getElementById('panel');
    panel.classList.toggle('collapsed');
    const btn = document.getElementById('panel-toggle');
    btn.textContent = panel.classList.contains('collapsed') ? '\u203A' : '\u2039';
});

// --- Load initial data ---
async function loadVessels() {
    try {
        const res = await fetch('/api/vessels');
        const data = await res.json();
        data.forEach(v => {
            v._lastUpdate = v.pos_timestamp ? new Date(v.pos_timestamp + 'Z').getTime() : 0;
            vessels.set(v.mmsi, v);

            if (v.mmsi === OWN_MMSI && v.lat != null) {
                ownPosition = { lat: v.lat, lon: v.lon };
                ownVessel = v;
            }

            updateMarker(v);
        });

        // Center map on own vessel or first vessel with position
        if (ownPosition) {
            map.setView([ownPosition.lat, ownPosition.lon], 13);
        } else if (data.length > 0 && data[0].lat != null) {
            map.setView([data[0].lat, data[0].lon], 13);
        }

        updatePanel();

        // Load tracks for all vessels
        for (const v of data) {
            if (v.lat != null) {
                try {
                    const trackRes = await fetch(`/api/vessels/${v.mmsi}/track?hours=${TRACK_HOURS}`);
                    const trackData = await trackRes.json();
                    if (trackData.length > 1) {
                        const color = getVesselColor(v);
                        const line = L.polyline(
                            trackData.map(p => [p.lat, p.lon]),
                            {
                                color: color,
                                weight: v.mmsi === OWN_MMSI ? 2.5 : 1.5,
                                opacity: 0.6,
                                dashArray: v.mmsi === OWN_MMSI ? null : '4 4',
                            }
                        );
                        if (!hiddenVessels.has(v.mmsi)) line.addTo(map);
                        trackLines.set(v.mmsi, {
                            line,
                            points: trackData.map(p => ({
                                lat: p.lat,
                                lon: p.lon,
                                time: new Date(p.timestamp + 'Z').getTime(),
                            })),
                        });
                    }
                } catch (e) {
                    // Track load failure is non-critical
                }
            }
        }
    } catch (e) {
        console.error('Failed to load vessels:', e);
    }
}

// --- Init ---
loadVessels();
connectWebSocket();

// Refresh panel periodically to update stale status
setInterval(updatePanel, 30000);

// --- Tidal Currents ---
function createCurrentArrow(station) {
    const size = 36;
    const speed = station.speed;
    const dir = station.direction;
    const isFlood = station.type === 'flood';
    const color = isFlood ? '#3498db' : '#e67e22';
    // Arrow length proportional to speed (min 8, max 22)
    const arrowLen = Math.min(22, Math.max(8, speed * 12));

    const svg = `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" xmlns="http://www.w3.org/2000/svg">
        <g transform="rotate(${dir}, ${size/2}, ${size/2})">
            <!-- Arrow shaft -->
            <line x1="${size/2}" y1="${size/2 + arrowLen/2}" x2="${size/2}" y2="${size/2 - arrowLen/2}"
                  stroke="${color}" stroke-width="2.5" stroke-linecap="round"/>
            <!-- Arrow head -->
            <polygon points="${size/2},${size/2 - arrowLen/2} ${size/2 - 5},${size/2 - arrowLen/2 + 7} ${size/2 + 5},${size/2 - arrowLen/2 + 7}"
                     fill="${color}"/>
        </g>
        <!-- Speed label -->
        <text x="${size/2}" y="${size - 2}" text-anchor="middle" fill="${color}" font-size="8" font-weight="bold">${speed.toFixed(1)}</text>
    </svg>`;

    return L.divIcon({
        html: svg,
        className: 'current-icon',
        iconSize: [size, size],
        iconAnchor: [size / 2, size / 2],
    });
}

async function loadCurrents() {
    try {
        const res = await fetch('/api/currents');
        if (!res.ok) return;
        const stations = await res.json();

        // Clear old markers
        currentLayer.clearLayers();
        currentMarkers.clear();

        stations.forEach(s => {
            const marker = L.marker([s.lat, s.lon], {
                icon: createCurrentArrow(s),
                zIndexOffset: -100,
            })
                .bindPopup(`<div class="popup-content">
                    <h3>${s.name}</h3>
                    <div class="popup-row"><span class="popup-label">Speed</span><span class="popup-value">${s.speed.toFixed(2)} kn</span></div>
                    <div class="popup-row"><span class="popup-label">Direction</span><span class="popup-value">${s.direction.toFixed(0)}°</span></div>
                    <div class="popup-row"><span class="popup-label">Type</span><span class="popup-value" style="color: ${s.type === 'flood' ? '#3498db' : '#e67e22'}">${s.type === 'flood' ? 'Flood (incoming)' : 'Ebb (outgoing)'}</span></div>
                </div>`, { className: 'vessel-popup', maxWidth: 200 });

            currentLayer.addLayer(marker);
            currentMarkers.set(s.station_id, marker);
        });

        // Update tidal flow overlay
        if (stations.length > 0 && tidalFlow) {
            tidalFlow.setStations(stations);
            // Show legend with station source if no grid loaded yet
            if (!tidalFlow.grid) {
                const legend = document.getElementById('flow-legend');
                const source = document.getElementById('flow-legend-source');
                if (legend) legend.classList.add('visible');
                if (source && !source.textContent) source.textContent = 'NOAA Tidal Stations';
            }
        }
    } catch (e) {
        // Currents are optional — fail silently
    }
}

// --- Tidal Flow Overlay ---
let tidalFlow = null;
if (typeof TidalFlowOverlay !== 'undefined') {
    tidalFlow = new TidalFlowOverlay(map, {
        particleCount: 3000,
        particleAge: 140,
        speedFactor: 0.003,
        fadeOpacity: 0.96,
        lineWidth: 2.0,
        useWaterMask: false,
    });
    tidalFlow.start();
}

// Load currents on startup and refresh every 60 seconds
loadCurrents();
setInterval(loadCurrents, 60000);

// Load SFBOFS grid data for high-resolution tidal flow
function initFlowLegend() {
    const bar = document.getElementById('flow-legend-bar');
    if (bar) {
        bar.style.background = 'linear-gradient(to right, rgb(15,40,180), rgb(30,110,220), rgb(40,190,220), rgb(50,200,100), rgb(160,220,50), rgb(240,200,30), rgb(240,130,20), rgb(220,50,30), rgb(180,20,60))';
    }
}
initFlowLegend();

async function loadCurrentField() {
    try {
        const res = await fetch('/api/current-field');
        if (!res.ok) return;
        const data = await res.json();
        if (data.error) {
            console.log('SFBOFS not available:', data.error);
            return;
        }
        if (tidalFlow) {
            tidalFlow.setGrid(data);
        }
        // Update legend
        const legend = document.getElementById('flow-legend');
        const source = document.getElementById('flow-legend-source');
        if (legend) legend.classList.add('visible');
        if (source) {
            const src = data.source || 'NOAA Stations';
            const time = data.fetched_at ? new Date(data.fetched_at.replace(' UTC', 'Z')).toLocaleString('en-US', { hour: '2-digit', minute: '2-digit', timeZoneName: 'short', timeZone: 'America/Los_Angeles' }) : '';
            source.textContent = `${src} · ${time}`;
        }
    } catch (e) {
        console.log('Current field fetch failed (optional)');
    }
}

loadCurrentField();
setInterval(loadCurrentField, 300000);  // Refresh every 5 minutes

// Flow toggle button
document.getElementById('flow-toggle').addEventListener('click', () => {
    if (!tidalFlow) return;
    const btn = document.getElementById('flow-toggle');
    const legend = document.getElementById('flow-legend');
    if (tidalFlow.animating) {
        tidalFlow.stop();
        btn.textContent = 'Flow: OFF';
        btn.classList.add('flow-off');
        if (legend) legend.classList.remove('visible');
    } else {
        tidalFlow.start();
        btn.textContent = 'Flow: ON';
        btn.classList.remove('flow-off');
        if (legend) legend.classList.add('visible');
    }
});

// --- Wind Overlay ---
let windOverlay = null;
let windStationMarkers = null;

if (typeof WindOverlay !== 'undefined') {
    windOverlay = new WindOverlay(map, {
        particleCount: 800,
        particleAge: 120,
        speedFactor: 0.001,
        fadeOpacity: 0.96,
        lineWidth: 1.5,
    });
    // Start OFF by default — wind is opt-in
}

if (typeof WindStationMarkers !== 'undefined') {
    windStationMarkers = new WindStationMarkers(map);
}

// Wind legend gradient
function initWindLegend() {
    const bar = document.getElementById('wind-legend-bar');
    if (bar) {
        bar.style.background = 'linear-gradient(to right, rgb(100,100,140), rgb(130,90,170), rgb(170,70,190), rgb(210,110,200), rgb(235,180,215), rgb(255,255,255))';
    }
}
initWindLegend();

async function loadWindField() {
    try {
        const res = await fetch('/api/wind-field');
        if (!res.ok) return;
        const data = await res.json();
        if (data.error) {
            console.log('Wind data not available:', data.error);
            return;
        }

        // Feed grid to wind overlay
        if (windOverlay && data.grid) {
            windOverlay.setGrid(data.grid);
        }

        // Feed stations to markers
        if (windStationMarkers && data.stations) {
            windStationMarkers.setStations(data.stations);
        }

        // Update wind legend source text
        const source = document.getElementById('wind-legend-source');
        if (source && data.grid) {
            const src = data.grid.source || 'HRRR';
            const time = data.grid.fetched_at ? new Date(data.grid.fetched_at.replace(' UTC', 'Z')).toLocaleString('en-US', { hour: '2-digit', minute: '2-digit', timeZoneName: 'short', timeZone: 'America/Los_Angeles' }) : '';
            const stationCount = data.stations ? data.stations.length : 0;
            source.textContent = `${src} · ${time} · ${stationCount} stations`;
        }
    } catch (e) {
        console.log('Wind field fetch failed (optional)');
    }
}

// Load wind data on startup and refresh every 5 minutes
loadWindField();
setInterval(loadWindField, 300000);

// Wind toggle button
const windToggleBtn = document.getElementById('wind-toggle');
if (windToggleBtn) {
    windToggleBtn.classList.add('wind-off');  // Start OFF
    windToggleBtn.addEventListener('click', () => {
        const legend = document.getElementById('wind-legend');
        const isOn = windOverlay && windOverlay.animating;
        if (isOn) {
            if (windOverlay) windOverlay.stop();
            if (windStationMarkers) windStationMarkers.hide();
            windToggleBtn.textContent = 'Wind: OFF';
            windToggleBtn.classList.add('wind-off');
            if (legend) legend.classList.remove('visible');
        } else {
            if (windOverlay) windOverlay.start();
            if (windStationMarkers) windStationMarkers.show();
            windToggleBtn.textContent = 'Wind: ON';
            windToggleBtn.classList.remove('wind-off');
            if (legend) legend.classList.add('visible');
        }
    });
}
