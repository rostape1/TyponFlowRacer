// --- Config ---
const APP_BUILD = '6ea2bd0';  // git commit hash — updated on each deploy
const OWN_MMSI = 338361814;
window.OWN_MMSI = OWN_MMSI;
const OWN_NAME = 'TYPON';
const TRACK_HOURS = 0.5;
const STALE_MINUTES = 10;
const MAX_VESSELS = 50;

// --- State ---
const vessels = new Map();       // mmsi → vessel data
const markers = new Map();       // mmsi → Leaflet marker
const trackLines = new Map();    // mmsi → Leaflet polyline
const currentMarkers = new Map(); // station_id → Leaflet marker
let currentStationsData = [];    // latest current station data from API
let currentLayer = null;         // Leaflet layer group for currents
let ownPosition = null;
window.ownPosition = null;
let ownVessel = null;  // full own vessel data (for SOG/COG)
let nmeaOwnPosition = null;  // from NMEA GPS for competitor labels
let competitorLabelsOn = false;  // toggle for competitor labels on map
let messageCount = 0;
const hiddenVessels = new Set();  // mmsi values of vessels hidden from map

// --- Forecast time shift ---
let forecastMinutes = 0;  // 0 = real-time, >0 = minutes into the future
let autoRefreshTimers = { currents: null, field: null, wind: null, tide: null };
let tidalFlow = null;  // initialized later after TidalFlowOverlay loads
let windOverlay = null;  // initialized later after WindOverlay loads

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

function _mapCenterPos() {
    const c = map.getCenter();
    return { lat: c.lat, lon: c.lng };
}

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

function formatAgo(timestamp) {
    const sec = Math.floor((Date.now() - timestamp) / 1000);
    if (sec < 5) return 'just now';
    if (sec < 60) return `${sec}s ago`;
    const min = Math.floor(sec / 60);
    if (min < 60) return `${min}m ago`;
    const hr = Math.floor(min / 60);
    return `${hr}h ${min % 60}m ago`;
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

// Click anywhere on map to see tide, wind, distance info
map.on('click', (e) => {
    const lat = e.latlng.lat;
    const lon = e.latlng.lng;
    let rows = `<div class="popup-row"><span class="popup-label">Position</span><span class="popup-value">${lat.toFixed(5)}°, ${lon.toFixed(5)}°</span></div>`;

    // Forecast time
    if (forecastMinutes > 0) {
        const target = new Date(Date.now() + forecastMinutes * 60000);
        const fTime = target.toLocaleString('en-US', {
            weekday: 'short', month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit',
            timeZone: 'America/Los_Angeles'
        });
        rows += `<div class="popup-row"><span class="popup-label">Forecast time</span><span class="popup-value" style="color:#f39c12">${fTime}</span></div>`;
    } else {
        rows += `<div class="popup-row"><span class="popup-label">Forecast time</span><span class="popup-value">Now</span></div>`;
    }

    // Tidal current
    if (typeof tidalFlow !== 'undefined' && tidalFlow) {
        const tc = tidalFlow._interpolateAt(lat, lon);
        if (tc && tc.speed > 0.01) {
            const dir = (Math.atan2(tc.vx, tc.vy) * 180 / Math.PI + 360) % 360;
            rows += `<div class="popup-row"><span class="popup-label">Current</span><span class="popup-value">${tc.speed.toFixed(2)} kn / ${dir.toFixed(0)}°</span></div>`;
        } else {
            rows += `<div class="popup-row"><span class="popup-label">Current</span><span class="popup-value">—</span></div>`;
        }
    }

    // Wind
    if (forecastMinutes > 48 * 60) {
        rows += `<div class="popup-row"><span class="popup-label">Wind</span><span class="popup-value" style="color:#4a6a8a">Not available beyond 48h</span></div>`;
    } else if (windOverlay) {
        const w = windOverlay.interpolateAt(lat, lon);
        if (w) {
            const gustStr = w.gust > 0 ? ` (gust ${w.gust.toFixed(0)})` : '';
            rows += `<div class="popup-row"><span class="popup-label">Wind</span><span class="popup-value">${w.speed.toFixed(1)} kn / ${w.dir.toFixed(0)}°${gustStr}</span></div>`;
        } else {
            rows += `<div class="popup-row"><span class="popup-label">Wind</span><span class="popup-value">—</span></div>`;
        }
    }

    // Distance & ETA from own vessel
    if (ownPosition) {
        const dist = haversineNm(ownPosition.lat, ownPosition.lon, lat, lon);
        const brg = bearingTo(ownPosition.lat, ownPosition.lon, lat, lon);
        rows += `<div class="popup-row"><span class="popup-label">Distance</span><span class="popup-value">${dist.toFixed(2)} nm / ${brg.toFixed(0)}°</span></div>`;

        if (ownVessel && ownVessel.sog > 0.5) {
            const hours = dist / ownVessel.sog;
            const h = Math.floor(hours);
            const m = Math.round((hours - h) * 60);
            rows += `<div class="popup-row"><span class="popup-label">ETA</span><span class="popup-value">${h}h ${m}m @ ${ownVessel.sog.toFixed(1)} kn</span></div>`;
        }
    }

    L.popup({ className: 'vessel-popup', maxWidth: 240 })
        .setLatLng(e.latlng)
        .setContent(`<div class="popup-content"><h3>Map Info</h3>${rows}</div>`)
        .openOn(map);
});

// Tile layers — external CDN tiles cached by service worker for offline use
const darkLayer = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
    attribution: '&copy; CartoDB',
    maxZoom: 19,
});

const osmLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 19,
});

const seaLayer = L.tileLayer('https://tiles.openseamap.org/seamark/{z}/{x}/{y}.png', {
    attribution: '&copy; OpenSeaMap',
    maxZoom: 18,
    opacity: 0.8,
});

// NOAA Nautical Charts — ENC display with traditional paper chart symbols
// Uses ArcGIS MapServer tile cache; LOD 0 = standard zoom 2 (resolution offset)
const noaaChart = L.TileLayer.extend({
    getTileUrl: function(coords) {
        const lod = coords.z - 2;
        if (lod < 0) return '';
        return `https://gis.charttools.noaa.gov/arcgis/rest/services/MarineChart_Services/NOAACharts/MapServer/tile/${lod}/${coords.y}/${coords.x}`;
    }
});
const noaaChartLayer = new noaaChart({
    attribution: '&copy; NOAA',
    minZoom: 2,
    maxZoom: 16,
    opacity: 0.9,
});


// Current arrows layer group
currentLayer = L.layerGroup().addTo(map);

// Default: dark base + sea marks overlay
darkLayer.addTo(map);
seaLayer.addTo(map);

L.control.layers({
    'Dark': darkLayer,
    'NOAA Chart': noaaChartLayer,
    'Street': osmLayer,
}, {
    'Nautical Marks': seaLayer,
    'Currents': currentLayer,
}, { position: 'topleft' }).addTo(map);

// --- Popup content ---
function buildPopupHtml(v) {
    const distNm = ownPosition && v.mmsi !== OWN_MMSI
        ? haversineNm(ownPosition.lat, ownPosition.lon, v.lat, v.lon) : null;
    const dist = distNm != null ? distNm.toFixed(1) + ' nm' : '—';
    const bearing = ownPosition && v.mmsi !== OWN_MMSI
        ? bearingTo(ownPosition.lat, ownPosition.lon, v.lat, v.lon).toFixed(0) + '°'
        : '—';
    const eta = (distNm != null && ownVessel && ownVessel.sog > 0.5)
        ? formatTime(distNm / ownVessel.sog * 60) : null;

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

    // Get tidal current at vessel position
    let tideStr = '—';
    if (v.lat != null && tidalFlow) {
        const tc = tidalFlow._interpolateAt(v.lat, v.lon);
        if (tc && tc.speed > 0.01) {
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

    const lastPing = v._lastUpdate ? formatAgo(v._lastUpdate) : '—';

    return `<div class="popup-content">
        <h3>${v.mmsi === OWN_MMSI ? OWN_NAME : (v.name || 'MMSI ' + v.mmsi)}${v.mmsi === OWN_MMSI ? ' (You)' : ''}</h3>
        <div class="popup-row"><span class="popup-label">MMSI</span><span class="popup-value">${v.mmsi}</span></div>
        ${v.ship_category && v.ship_category !== 'Unknown' ? `<div class="popup-row"><span class="popup-label">Type</span><span class="popup-value">${v.ship_category}</span></div>` : ''}
        <div class="popup-row"><span class="popup-label">SOG</span><span class="popup-value">${v.sog != null ? v.sog.toFixed(1) + ' kn' : '—'}</span></div>
        <div class="popup-row"><span class="popup-label">COG</span><span class="popup-value">${v.cog != null ? v.cog.toFixed(0) + '°' : '—'}</span></div>
                <div class="popup-row"><span class="popup-label">Current</span><span class="popup-value" style="color:#00d4ff">${tideStr}</span></div>
        <div class="popup-row"><span class="popup-label">Wind</span><span class="popup-value" style="color:#aa46be">${windStr}</span></div>
        <div class="popup-row"><span class="popup-label">Avg Speed</span><span class="popup-value">${(() => { const a = vesselStore.getAvgSpeed(v.mmsi); return a != null ? a + ' kn' : '—'; })()}</span></div>
        <div class="popup-row"><span class="popup-label">Distance</span><span class="popup-value">${dist}</span></div>
        <div class="popup-row"><span class="popup-label">Bearing</span><span class="popup-value">${bearing}</span></div>
        ${eta ? `<div class="popup-row"><span class="popup-label">ETA from ${OWN_NAME}</span><span class="popup-value">${eta}</span></div>` : ''}
        ${cpaInfo ? `<div class="popup-row"><span class="popup-label">CPA/TCPA</span><span class="${cpaClass}">${cpaInfo.label}</span></div>` : ''}
        ${v.mmsi !== OWN_MMSI ? `<div class="popup-row"><span class="popup-label">Speed Diff</span><span class="popup-value">${speedDiffStr}</span></div>` : ''}
        ${v.destination ? `<div class="popup-row"><span class="popup-label">Dest</span><span class="popup-value">${v.destination}</span></div>` : ''}
        <div class="popup-row"><span class="popup-label">Last AIS</span><span class="popup-value">${lastPing}</span></div>
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
    const times = points.map(p => {
        if (p.time) return typeof p.time === 'number' ? p.time : new Date(p.time).getTime();
        if (p.timestamp) return new Date(p.timestamp + 'Z').getTime();
        return Date.now();
    });

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
        const trackPoints = vesselStore.getTrack(mmsi, 2);
        const withSpeed = trackPoints.filter(p => p.sog != null);

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
            .on('popupopen', (e) => {
                // Rebuild popup content on open to get latest current/wind data
                const mmsi = v.mmsi;
                const vessel = vessels.get ? vessels.get(mmsi) : v;
                if (vessel) e.target.getPopup().setContent(buildPopupHtml(vessel));
                loadSpeedChart(mmsi);
            });
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

    if (typeof CompetitorLabels !== 'undefined' && competitorLabelsOn) {
        const own = nmeaOwnPosition || ownPosition || _mapCenterPos();
        if (own) {
            const marker = markers.get(v.mmsi);
            if (marker) CompetitorLabels.update(v, own.lat, own.lon, marker);
        }
    }
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
    const searchInput = document.getElementById('vessel-search');
    const query = (searchInput ? searchInput.value : '').toLowerCase().trim();
    let vesselArray = Array.from(vessels.values()).filter(v => v.lat != null);

    // Filter by search query
    if (query) {
        vesselArray = vesselArray.filter(v => {
            const name = (v.name || v.shipname || '').toLowerCase();
            const mmsi = String(v.mmsi);
            return name.includes(query) || mmsi.includes(query);
        });
    }

    // Sort: own vessel first, then by distance
    vesselArray.sort((a, b) => {
        if (a.mmsi === OWN_MMSI) return -1;
        if (b.mmsi === OWN_MMSI) return 1;
        if (!ownPosition) return 0;
        const da = haversineNm(ownPosition.lat, ownPosition.lon, a.lat, a.lon);
        const db = haversineNm(ownPosition.lat, ownPosition.lon, b.lat, b.lon);
        return da - db;
    });

    // Prune vessels beyond MAX_VESSELS — remove farthest from map and data
    if (vesselArray.length > MAX_VESSELS) {
        const toPrune = vesselArray.splice(MAX_VESSELS);
        for (const v of toPrune) {
            const marker = markers.get(v.mmsi);
            if (marker) { map.removeLayer(marker); markers.delete(v.mmsi); }
            const track = trackLines.get(v.mmsi);
            if (track) { map.removeLayer(track.line); trackLines.delete(v.mmsi); }
            vessels.delete(v.mmsi);
        }
    }

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
                ${v.mmsi === OWN_MMSI ? OWN_NAME : (v.name || 'MMSI ' + v.mmsi)}
                ${v.ship_category && v.ship_category !== 'Unknown' ? `<span class="vessel-type-badge ${typeClass}">${v.ship_category}</span>` : ''}
                <button class="vessel-toggle ${isVisible ? '' : 'toggled-off'}" data-mmsi="${v.mmsi}" title="${isVisible ? 'Hide from map' : 'Show on map'}" aria-pressed="${isVisible}" aria-label="${isVisible ? 'Hide' : 'Show'} ${v.mmsi === OWN_MMSI ? OWN_NAME : (v.name || 'MMSI ' + v.mmsi)} on map">
                    ${isVisible ? '&#9673;' : '&#9675;'}
                </button>
            </div>
            <div class="vessel-meta">
                ${v.sog != null ? `<span>${v.sog.toFixed(1)} kn</span>` : ''}
                ${(() => { const a = vesselStore.getAvgSpeed(v.mmsi); return a != null ? `<span>avg ${a} kn</span>` : ''; })()}
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
    if (_mobileVesselsOn) updateMobileVesselList();
}

// --- AISstream.io Direct WebSocket ---
const vesselStore = new VesselStore({ staleMinutes: STALE_MINUTES, persist: true });

// Restore any persisted vessels to the map
for (const v of vesselStore.getAll()) {
    vessels.set(v.mmsi, v);
    if (v.mmsi === OWN_MMSI && v.lat != null) {
        ownPosition = { lat: v.lat, lon: v.lon };
        window.ownPosition = ownPosition;
        ownVessel = v;
    }
    updateMarker(v);
}

// Default API key — embedded for convenience (free AISstream.io service)
const DEFAULT_AISSTREAM_KEY = '75cbc4d8d7acd0067399b35830a59d4a74696d64';

function getAISStreamApiKey() {
    return localStorage.getItem('aisstream_api_key') || DEFAULT_AISSTREAM_KEY;
}

let aisClient = null;

function connectAISStream() {
    const apiKey = getAISStreamApiKey();
    if (!apiKey) {
        const statusEl = document.getElementById('connection-status');
        statusEl.textContent = 'AIS: tap to connect';
        statusEl.className = 'status-disconnected needs-key';
        statusEl.style.cursor = 'pointer';
        statusEl.onclick = () => {
            const key = prompt('Enter your AISstream.io API key:');
            if (key) {
                localStorage.setItem('aisstream_api_key', key.trim());
                statusEl.onclick = null;
                statusEl.style.cursor = '';
                statusEl.classList.remove('needs-key');
                connectAISStream();
            }
        };
        return;
    }

    aisClient = new AISStreamClient({
        apiKey: apiKey,
        bbox: [[37.4, -122.8], [38.2, -122.0]],
        ownMmsi: OWN_MMSI,
        onMessage: (data) => {
            messageCount++;
            const merged = vesselStore.upsert(data);
            vessels.set(data.mmsi, merged);

            if (data.mmsi === OWN_MMSI && data.lat != null) {
                ownPosition = { lat: data.lat, lon: data.lon };
                window.ownPosition = ownPosition;
                ownVessel = merged;
            }

            // Update track line
            const trackPoints = vesselStore.getTrack(data.mmsi, TRACK_HOURS);
            if (trackPoints.length > 1) {
                const color = getVesselColor(merged);
                const existing = trackLines.get(data.mmsi);
                if (existing) {
                    existing.line.setLatLngs(trackPoints.map(p => [p.lat, p.lon]));
                    existing.points = trackPoints;
                } else {
                    const line = L.polyline(
                        trackPoints.map(p => [p.lat, p.lon]),
                        {
                            color: color,
                            weight: data.mmsi === OWN_MMSI ? 2.5 : 1.5,
                            opacity: 0.6,
                            dashArray: data.mmsi === OWN_MMSI ? null : '4 4',
                        }
                    );
                    if (!hiddenVessels.has(data.mmsi)) line.addTo(map);
                    trackLines.set(data.mmsi, { line, points: trackPoints });
                }
            }

            updateMarker(merged);
            updatePanel();
        },
        onStatus: (status) => {
            const el = document.getElementById('connection-status');
            if (status === 'connected') {
                el.textContent = 'Connected';
                el.className = 'status-connected';
            } else {
                el.textContent = 'Disconnected';
                el.className = 'status-disconnected';
            }
        },
    });
    aisClient.connect();
}

// Periodically save vessel store and prune stale vessels/markers
setInterval(() => {
    vesselStore.saveIfNeeded();
    const pruned = vesselStore.prune();
    for (const mmsi of pruned) {
        vessels.delete(mmsi);
        const m = markers.get(mmsi);
        if (m) { map.removeLayer(m); markers.delete(mmsi); }
        const t = trackLines.get(mmsi);
        if (t) { map.removeLayer(t.line); trackLines.delete(mmsi); }
    }
    if (pruned.length > 0) updatePanel();
}, 30000);

// --- Panel toggle ---
const _isMobile = window.innerWidth <= 600;

function getPanelArrow(collapsed) {
    if (_isMobile) return collapsed ? '\u2630' : '\u2715';  // ☰ / ✕
    return collapsed ? '\u2039' : '\u203A';  // ‹ (open) / › (close)
}

document.getElementById('panel-toggle').addEventListener('click', () => {
    const panel = document.getElementById('panel');
    panel.classList.toggle('collapsed');
    document.body.classList.toggle('panel-collapsed');
    const btn = document.getElementById('panel-toggle');
    btn.textContent = getPanelArrow(panel.classList.contains('collapsed'));
});

// Auto-collapse panel on mobile to maximize map space
if (_isMobile) {
    const panel = document.getElementById('panel');
    panel.classList.add('collapsed');
    document.body.classList.add('panel-collapsed');
    document.getElementById('panel-toggle').textContent = getPanelArrow(true);

    // Hide layers tray on mobile (toggled via layers button)
    const layersTray = document.getElementById('layers-tray');
    if (layersTray && window.innerWidth <= 600) layersTray.classList.remove('hidden');
}

// --- Layers tray + forecast quick buttons toggle (mobile) ---
const _layersBtn = document.getElementById('layers-btn');
const _layersTray = document.getElementById('layers-tray');
const _fcstQuickBtns = document.getElementById('forecast-quick-btns');

// Default: expanded on mobile
if (window.innerWidth <= 600) {
    document.body.classList.add('mobile-controls-open');
}

if (_layersBtn && _layersTray) {
    _layersBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        const isOpen = document.body.classList.toggle('mobile-controls-open');
        _layersTray.classList.toggle('hidden', !isOpen);
        if (_fcstQuickBtns) _fcstQuickBtns.classList.toggle('hidden', !isOpen);
        // Hide vessel list when closing the tray
        if (!isOpen && _mobileVesselsOn) {
            _mobileVesselsOn = false;
            if (_vesselsBtn) {
                _vesselsBtn.textContent = 'Vessels: OFF';
                _vesselsBtn.classList.add('vessels-off');
            }
            if (_mobileVesselList) _mobileVesselList.classList.add('hidden');
        }
        // Also collapse desktop panel if open (handles resize-to-mobile scenario)
        if (!isOpen) {
            const panel = document.getElementById('panel');
            if (panel && !panel.classList.contains('collapsed')) {
                panel.classList.add('collapsed');
                document.body.classList.add('panel-collapsed');
            }
        }
    });
    // Do NOT auto-close on outside click — user must tap ☰ to collapse
}

// --- Responsive resize handling ---
// _isMobile is set once at load; this handler corrects state when window is resized
// across the 600px breakpoint (e.g. DevTools responsive mode, browser drag)
let _wasNarrow = window.innerWidth <= 600;
window.addEventListener('resize', () => {
    const isNarrow = window.innerWidth <= 600;
    if (isNarrow === _wasNarrow) return;
    _wasNarrow = isNarrow;

    const panel = document.getElementById('panel');
    if (isNarrow) {
        // Went mobile: collapse desktop panel so it doesn't overlap mobile UI
        panel.classList.add('collapsed');
        document.body.classList.add('panel-collapsed');
    } else {
        // Went desktop: hide mobile vessel list to avoid duplicate
        if (_mobileVesselsOn) {
            _mobileVesselsOn = false;
            if (_vesselsBtn) { _vesselsBtn.textContent = 'Vessels: OFF'; _vesselsBtn.classList.add('vessels-off'); }
            if (_mobileVesselList) _mobileVesselList.classList.add('hidden');
        }
    }
});

// --- Mobile vessels toggle ---
const _vesselsBtn = document.getElementById('vessels-toggle');
const _mobileVesselList = document.getElementById('mobile-vessel-list');
let _mobileVesselsOn = false;

function updateMobileVesselList() {
    if (!_mobileVesselList || !_mobileVesselsOn) return;
    let vesselArray = Array.from(vessels.values()).filter(v => v.lat != null);
    vesselArray.sort((a, b) => {
        if (a.mmsi === OWN_MMSI) return -1;
        if (b.mmsi === OWN_MMSI) return 1;
        if (!ownPosition) return 0;
        const da = haversineNm(ownPosition.lat, ownPosition.lon, a.lat, a.lon);
        const db = haversineNm(ownPosition.lat, ownPosition.lon, b.lat, b.lon);
        return da - db;
    });
    _mobileVesselList.innerHTML = vesselArray.map(v => {
        const isOwn = v.mmsi === OWN_MMSI;
        const isStale = (Date.now() - (v._lastUpdate || 0)) > STALE_MINUTES * 60 * 1000;
        const typeClass = getTypeCssClass(v.ship_category);
        const dist = ownPosition && !isOwn
            ? haversineNm(ownPosition.lat, ownPosition.lon, v.lat, v.lon).toFixed(1) : null;
        return `<div class="vessel-card ${isOwn ? 'own-vessel' : ''} ${isStale ? 'stale' : ''}" data-mmsi="${v.mmsi}">
            <div class="vessel-name">
                ${isOwn ? OWN_NAME : (v.name || 'MMSI ' + v.mmsi)}
                ${v.ship_category && v.ship_category !== 'Unknown' ? `<span class="vessel-type-badge ${typeClass}">${v.ship_category}</span>` : ''}
            </div>
            <div class="vessel-meta">
                ${v.sog != null ? `<span>${v.sog.toFixed(1)} kn</span>` : ''}
                ${dist ? `<span>${dist} nm</span>` : ''}
            </div>
        </div>`;
    }).join('');
    _mobileVesselList.querySelectorAll('.vessel-card').forEach(card => {
        card.addEventListener('click', () => {
            const mmsi = parseInt(card.dataset.mmsi);
            const v = vessels.get(mmsi);
            if (v && v.lat != null) {
                map.setView([v.lat, v.lon], Math.max(map.getZoom(), 14));
                const marker = markers.get(mmsi);
                if (marker) marker.openPopup();
            }
            closeMobileVesselList();
        });
    });
}

function closeMobileVesselList() {
    _mobileVesselsOn = false;
    if (_vesselsBtn) {
        _vesselsBtn.textContent = 'Vessels: OFF';
        _vesselsBtn.classList.add('vessels-off');
    }
    if (_mobileVesselList) _mobileVesselList.classList.add('hidden');
}

let _vesselListTimer = null;

if (_vesselsBtn) {
    _vesselsBtn.classList.add('vessels-off');
    _vesselsBtn.addEventListener('click', () => {
        _mobileVesselsOn = !_mobileVesselsOn;
        _vesselsBtn.textContent = _mobileVesselsOn ? 'Vessels: ON' : 'Vessels: OFF';
        _vesselsBtn.classList.toggle('vessels-off', !_mobileVesselsOn);
        if (_mobileVesselList) {
            _mobileVesselList.classList.toggle('hidden', !_mobileVesselsOn);
            if (_mobileVesselsOn) {
                updateMobileVesselList();
                // Auto-close after 5 seconds
                clearTimeout(_vesselListTimer);
                _vesselListTimer = setTimeout(() => closeMobileVesselList(), 5000);
            } else {
                clearTimeout(_vesselListTimer);
            }
        }
    });
}

// Reset auto-close timer when interacting with the list
if (_mobileVesselList) {
    _mobileVesselList.addEventListener('touchstart', () => {
        if (_mobileVesselsOn) {
            clearTimeout(_vesselListTimer);
            _vesselListTimer = setTimeout(() => closeMobileVesselList(), 5000);
        }
    });
}

// Close vessel list when tapping the map
map.on('click', (e) => {
    if (_mobileVesselsOn) closeMobileVesselList();
    if (_routeMode) { _handleRouteClick(e); return; }
});
map.on('dragstart', () => {
    if (_mobileVesselsOn) closeMobileVesselList();
});

// --- Competitor Labels Toggle ---
const _labelsToggle = document.getElementById('labels-toggle');
if (_labelsToggle) {
    _labelsToggle.addEventListener('click', () => {
        competitorLabelsOn = !competitorLabelsOn;
        _labelsToggle.textContent = competitorLabelsOn ? 'Labels: ON' : 'Labels: OFF';
        _labelsToggle.className = competitorLabelsOn ? '' : 'labels-off';
        if (!competitorLabelsOn) {
            markers.forEach((marker, mmsi) => {
                if (mmsi !== OWN_MMSI && marker._competitorTooltip) {
                    marker.unbindTooltip();
                    marker._competitorTooltip = false;
                }
            });
        } else {
            const own = nmeaOwnPosition || ownPosition || _mapCenterPos();
            vessels.forEach(v => {
                const marker = markers.get(v.mmsi);
                if (marker && v.lat != null && typeof CompetitorLabels !== 'undefined') {
                    CompetitorLabels.update(v, own.lat, own.lon, marker);
                }
            });
        }
    });
}

// --- Route Planner ---
let _routeMode = false;  // 'start' | 'end' | false
let _routeRenderer = null;
let _routeStart = null;
let _routeEnd = null;

const _routeToggle = document.getElementById('route-toggle');
const _routePanel = document.getElementById('route-panel');
const _routeStatus = document.getElementById('route-status');
const _routeResult = document.getElementById('route-result');
const _routeEta = document.getElementById('route-eta');
const _routeDistance = document.getElementById('route-distance');
const _routeClear = document.getElementById('route-clear');
const _routePerf = document.getElementById('route-perf');
const _routePerfVal = document.getElementById('route-perf-val');
const _routeDetailsBtn = document.getElementById('route-details-btn');
const _routeDetailsModal = document.getElementById('route-details-modal');
const _routeDetailsBody = document.getElementById('route-details-body');
const _routeDetailsClose = document.getElementById('route-details-close');
let _lastRouteResult = null;

if (_routePerf) {
    _routePerf.addEventListener('input', () => {
        if (_routePerfVal) _routePerfVal.textContent = _routePerf.value + '%';
    });
}

if (_routeToggle) {
    _routeToggle.addEventListener('click', () => {
        if (_routeMode) {
            _exitRouteMode();
        } else {
            _enterRouteMode();
        }
    });
}

if (_routeClear) {
    _routeClear.addEventListener('click', () => {
        _clearRoute();
        _routeStatus.textContent = 'Click start point on map';
        _routeMode = 'start';
        map.getContainer().classList.add('route-mode');
    });
}

if (_routeDetailsBtn) {
    _routeDetailsBtn.addEventListener('click', () => {
        if (!_lastRouteResult || !_lastRouteResult.path) return;
        const path = _lastRouteResult.path;
        const startMs = path[0].timeMs;
        const twsColor = s => s < 6 ? '#a0b0c0' : s < 12 ? '#2ecc71' : s < 18 ? '#f39c12' : '#e74c3c';
        const awaColor = a => { a = Math.abs(a); return a < 45 ? '#e74c3c' : a < 70 ? '#f39c12' : a < 110 ? '#2ecc71' : '#3498db'; };
        let html = '<table><thead><tr><th>Time</th><th>BSP</th><th>TWS</th><th>TWA</th><th>AWS</th><th>AWA</th></tr></thead><tbody>';
        for (const pt of path) {
            const min = Math.round((pt.timeMs - startMs) / 60000);
            const awa = Math.round(Math.abs(pt.awa));
            html += `<tr><td>${min}m</td><td>${pt.bsp.toFixed(1)}</td><td style="color:${twsColor(pt.tws)}">${pt.tws.toFixed(1)}</td><td>${Math.round(pt.twa)}\u00b0</td><td style="color:${twsColor(pt.aws)}">${pt.aws.toFixed(1)}</td><td style="color:${awaColor(pt.awa)}">${awa}\u00b0</td></tr>`;
        }
        html += '</tbody></table>';
        _routeDetailsBody.innerHTML = html;
        _routeDetailsModal.classList.remove('hidden');
    });
}

if (_routeDetailsClose) {
    _routeDetailsClose.addEventListener('click', () => {
        _routeDetailsModal.classList.add('hidden');
    });
}

function _enterRouteMode() {
    _routeMode = 'start';
    _routeStart = null;
    _routeEnd = null;
    if (!_routeRenderer) _routeRenderer = new RouteRenderer(map);
    _routeRenderer.clear();
    _routeToggle.textContent = 'Route: ON';
    _routeToggle.classList.remove('route-off');
    if (_routePanel) _routePanel.classList.remove('hidden');
    if (_routeResult) _routeResult.classList.add('hidden');
    if (_routeClear) _routeClear.classList.add('hidden');
    _routeStatus.textContent = 'Click start point on map';
    map.getContainer().classList.add('route-mode');
}

function _exitRouteMode() {
    _routeMode = false;
    _clearRoute();
    _routeToggle.textContent = 'Route';
    _routeToggle.classList.add('route-off');
    if (_routePanel) _routePanel.classList.add('hidden');
    map.getContainer().classList.remove('route-mode');
}

function _clearRoute() {
    if (_routeRenderer) _routeRenderer.clear();
    _routeStart = null;
    _routeEnd = null;
    _lastRouteResult = null;
    if (_routeResult) _routeResult.classList.add('hidden');
    if (_routeClear) _routeClear.classList.add('hidden');
    if (_routeDetailsModal) _routeDetailsModal.classList.add('hidden');
}

async function _handleRouteClick(e) {
    const { lat, lng: lon } = e.latlng;

    if (_routeMode === 'start') {
        _routeStart = { lat, lon };
        _routeRenderer.setStart(lat, lon);
        _routeStatus.textContent = 'Click end point on map';
        _routeMode = 'end';
    } else if (_routeMode === 'end') {
        _routeEnd = { lat, lon };
        _routeRenderer.setEnd(lat, lon);
        map.getContainer().classList.remove('route-mode');
        _routeMode = false;
        await _runRoute();
    }
}

async function _runRoute() {
    const perf = (_routePerf ? parseInt(_routePerf.value) : 85) / 100;
    _routeStatus.innerHTML = '<span style="color:#f39c12">Computing route...</span>';

    const startTime = Date.now() + forecastMinutes * 60000;

    try {
        const result = await computeRoute(
            _routeStart.lat, _routeStart.lon,
            _routeEnd.lat, _routeEnd.lon,
            startTime, perf,
            (step, total) => {
                _routeStatus.innerHTML = `<span style="color:#f39c12">Computing... ${Math.round(step / total * 100)}%</span>`;
            }
        );

        if (result.error) {
            _routeStatus.innerHTML = `<span style="color:#e74c3c">${result.error}</span>`;
            if (_routeClear) _routeClear.classList.remove('hidden');
            return;
        }

        _routeRenderer.drawRoute(result);
        _routeStatus.innerHTML = '<span style="color:#2ecc71">Route computed</span>';
        _lastRouteResult = result;

        if (_routeEta) _routeEta.innerHTML = `<span style="color:#a0b0c0">ETA</span><span style="color:#f39c12;font-weight:600">${result.elapsedMin} min</span>`;
        if (_routeDistance) _routeDistance.innerHTML = `<span style="color:#a0b0c0">Distance</span><span>${result.distanceNm} nm</span>`;
        if (_routeResult) _routeResult.classList.remove('hidden');
        if (_routeClear) _routeClear.classList.remove('hidden');
    } catch (e) {
        _routeStatus.innerHTML = `<span style="color:#e74c3c">Error: ${e.message}</span>`;
        if (_routeClear) _routeClear.classList.remove('hidden');
    }
}

// --- Vessel search ---
document.getElementById('vessel-search').addEventListener('input', () => updatePanel());

// --- Init ---
// Center on SF Bay (vessels will appear as AISstream data arrives)
if (!ownPosition) {
    map.setView([37.81, -122.42], 12);
}
connectAISStream();
updatePanel();

// Auto-download with retry on failure
let _autoRetryTimer = null;
let _autoRetryDelay = 30000; // 30s, doubles each failure up to 5min

function _allCategoriesDone() {
    const s = _getDlStatus();
    const sixHours = 6 * 3600 * 1000;
    return DL_CATEGORIES.every(cat => {
        const ts = s[cat] ? new Date(s[cat]).getTime() : 0;
        return ts && (Date.now() - ts < sixHours);
    });
}

async function _autoDownload() {
    if (_downloadingOffline) return;
    await downloadForOffline(true);
    if (!_allCategoriesDone()) {
        // Some categories failed — schedule retry with backoff
        _autoRetryTimer = setTimeout(() => {
            _autoRetryDelay = Math.min(_autoRetryDelay * 2, 5 * 60000);
            _autoDownload();
        }, _autoRetryDelay);
    } else {
        _autoRetryDelay = 30000; // reset for next session
    }
}

// Retry immediately (after 3s grace) when network comes back
window.addEventListener('online', () => {
    if (_autoRetryTimer) { clearTimeout(_autoRetryTimer); _autoRetryTimer = null; }
    _autoRetryDelay = 30000;
    if (!_allCategoriesDone()) setTimeout(_autoDownload, 3000);
});

// Kick off initial download 8s after page load
setTimeout(_autoDownload, 8000);
console.log(`AIS Tracker build: ${APP_BUILD}`);

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
        const stations = await fetchCurrents(forecastMinutes);
        currentStationsData = stations;

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
autoRefreshTimers.currents = setInterval(loadCurrents, 60000);

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
        // Show loading state in legend immediately
        const flowSourceEl = document.getElementById('flow-legend-source');
        if (flowSourceEl && !flowSourceEl.textContent) {
            flowSourceEl.innerHTML = '<span style="opacity:0.6">Loading current data\u2026</span>';
        }
        const data = await fetchCurrentField(forecastMinutes);
        if (!data) return;
        if (data.error) {
            console.log('SFBOFS not available:', data.error);
            return;
        }

        const legend = document.getElementById('flow-legend');
        const source = document.getElementById('flow-legend-source');

        if (data.unavailable) {
            if (tidalFlow) tidalFlow.setGrid(null);
            if (legend) legend.classList.add('visible');
            if (source) {
                const target = new Date(Date.now() + forecastMinutes * 60000);
                const timeStr = target.toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit', timeZone: 'America/Los_Angeles' });
                source.innerHTML = `<span style="color:#f39c12">No forecast data for ${timeStr}</span>`;
            }
            return;
        }

        if (tidalFlow) {
            tidalFlow.setGrid(data);
        }
        // Update legend
        if (legend) legend.classList.add('visible');
        if (source) {
            const src = data.source || 'NOAA Stations';
            const run = data.model_run ? ` ${data.model_run}` : '';
            if (forecastMinutes > 0) {
                const target = new Date(Date.now() + forecastMinutes * 60000);
                const timeStr = target.toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit', timeZone: 'America/Los_Angeles' });
                source.textContent = `${src}${run} · Forecast: ${timeStr}`;
            } else {
                const age = formatDataAge(data.fetched_at);
                if (age) {
                    source.innerHTML = `${src}${run} · ${age.dot}${age.ageText}`;
                } else {
                    const time = data.fetched_at ? new Date(data.fetched_at.replace(' UTC', 'Z')).toLocaleString('en-US', { hour: '2-digit', minute: '2-digit', timeZoneName: 'short', timeZone: 'America/Los_Angeles' }) : '';
                    source.textContent = `${src} · ${time}`;
                }
            }
        }
    } catch (e) {
        console.log('Current field fetch failed (optional)');
    }
}

loadCurrentField();
autoRefreshTimers.field = setInterval(loadCurrentField, 300000);  // Refresh every 5 minutes

// Tide Flow toggle — controls flow animation + heatmap together
document.getElementById('flow-toggle').addEventListener('click', () => {
    if (!tidalFlow) return;
    const btn = document.getElementById('flow-toggle');
    const legend = document.getElementById('flow-legend');
    if (tidalFlow.animating) {
        tidalFlow.stop();
        tidalFlow.setHeatmapEnabled(false);
        btn.textContent = 'Tide Flow: OFF';
        btn.classList.add('flow-off');
        if (legend) legend.classList.remove('visible');
    } else {
        tidalFlow.start();
        tidalFlow.setHeatmapEnabled(true);
        btn.textContent = 'Tide Flow: ON';
        btn.classList.remove('flow-off');
        if (legend) legend.classList.add('visible');
    }
});

// --- Data freshness indicator ---
function formatDataAge(fetchedAtStr) {
    if (!fetchedAtStr) return null;
    const fetched = new Date(fetchedAtStr.replace(' UTC', 'Z'));
    if (isNaN(fetched)) return null;
    const ageMs = Date.now() - fetched.getTime();
    const ageMins = Math.floor(ageMs / 60000);
    const fresh = ageMins < 45; // green if < 45 min old
    let ageText;
    if (ageMins < 1) ageText = 'just now';
    else if (ageMins < 60) ageText = `${ageMins}m ago`;
    else {
        const h = Math.floor(ageMins / 60);
        const m = ageMins % 60;
        ageText = m > 0 ? `${h}h ${m}m ago` : `${h}h ago`;
    }
    const dotColor = fresh ? '#27ae60' : '#f1c40f';
    const dot = `<span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${dotColor};box-shadow:0 0 4px ${dotColor};margin-right:3px;vertical-align:middle;"></span>`;
    return { dot, ageText, fresh };
}

// --- Wind Overlay ---
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
    windStationMarkers = new WindStationMarkers(map, windOverlay);
}

// Wind legend gradient
function initWindLegend() {
    const bar = document.getElementById('wind-legend-bar');
    if (bar) {
        const scheme = windOverlay ? windOverlay.scheme : 'green';
        if (scheme === 'purple') {
            bar.style.background = 'linear-gradient(to right, rgb(140,120,180), rgb(160,100,200), rgb(190,80,220), rgb(220,120,220), rgb(240,190,230), rgb(255,255,255))';
        } else {
            bar.style.background = 'linear-gradient(to right, rgb(60,80,30), rgb(80,140,20), rgb(120,200,30), rgb(170,230,50), rgb(210,250,100), rgb(255,255,255))';
        }
    }
}
initWindLegend();

// Wind color scheme toggle
const windColorToggle = document.getElementById('wind-color-toggle');
if (windColorToggle) {
    function updateColorDots() {
        windColorToggle.querySelectorAll('.color-dot').forEach(dot => {
            dot.classList.toggle('active', dot.dataset.scheme === (windOverlay ? windOverlay.scheme : 'purple'));
        });
    }
    updateColorDots();

    windColorToggle.addEventListener('click', (e) => {
        const dot = e.target.closest('.color-dot');
        if (!dot || !windOverlay) return;
        windOverlay.setColorScheme(dot.dataset.scheme);
        if (windStationMarkers) windStationMarkers._updateMarkers();
        initWindLegend();
        updateColorDots();
    });
}

async function loadWindField() {
    try {
        // Wind forecasts limited to 48h (HRRR model limit)
        if (forecastMinutes > 48 * 60) {
            // Beyond wind forecast range — show note in legend
            const wsource = document.getElementById('wind-legend-source');
            if (wsource) wsource.textContent = 'Wind forecast unavailable beyond 48h';
            return;
        }
        // Show loading state in legend immediately
        const sourceEl = document.getElementById('wind-legend-source');
        if (sourceEl && !sourceEl.textContent) {
            sourceEl.innerHTML = '<span style="opacity:0.6">Loading wind data\u2026</span>';
        }
        const data = await fetchWindField(forecastMinutes);
        if (!data) return;
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
            if (forecastMinutes > 0) {
                const target = new Date(Date.now() + forecastMinutes * 60000);
                const timeStr = target.toLocaleString('en-US', { weekday: 'short', hour: 'numeric', minute: '2-digit', timeZone: 'America/Los_Angeles' });
                source.textContent = `${src} · Forecast: ${timeStr}`;
            } else {
                const stationCount = data.stations ? data.stations.length : 0;
                const age = formatDataAge(data.grid.fetched_at);
                if (age) {
                    source.innerHTML = `${src} · ${age.dot}${age.ageText} · ${stationCount} stations`;
                } else {
                    const time = data.grid.fetched_at ? new Date(data.grid.fetched_at.replace(' UTC', 'Z')).toLocaleString('en-US', { hour: '2-digit', minute: '2-digit', timeZoneName: 'short', timeZone: 'America/Los_Angeles' }) : '';
                    source.textContent = `${src} · ${time} · ${stationCount} stations`;
                }
            }
        }
    } catch (e) {
        console.log('Wind field fetch failed (optional)');
    }
}

// Load wind data on startup and refresh every 5 minutes
loadWindField();

// --- Tide Height Stations ---
let tideStations = [];  // array of {station_id, name, lat, lon, height_ft, next_extreme, observed_ft?, obs_time?}
let tideStationMarkers = null;  // L.layerGroup
let tideMarkersVisible = false;

async function loadTideHeight() {
    try {
        const data = await fetchTideHeights(forecastMinutes);
        if (Array.isArray(data)) {
            tideStations = data;
            // Fetch real-time observations only in real-time mode
            if (forecastMinutes === 0) {
                try {
                    const obs = await fetchWaterLevels();
                    for (const s of tideStations) {
                        const wl = obs.get(s.station_id);
                        if (wl) {
                            s.observed_ft = wl.value;
                            s.obs_time = wl.time;
                        }
                    }
                } catch (e) {
                    console.log('Water level fetch failed:', e);
                }
            }
            updateTideMarkers();
            updateFlowConfidence();
        }
    } catch (e) {
        console.log('Tide height fetch failed:', e);
    }
}

function updateTideMarkers() {
    if (!tideStationMarkers) {
        tideStationMarkers = L.layerGroup();
    }
    tideStationMarkers.clearLayers();
    console.log('Updating tide markers:', tideStations.length, 'stations');

    for (const s of tideStations) {
        const h = s.height_ft;
        const hasData = h != null;
        const sign = hasData && h >= 0 ? '+' : '';
        const color = !hasData ? '#888' : (h >= 0 ? '#5dade2' : '#e74c3c');
        const size = 44;

        const label = hasData ? `${sign}${h.toFixed(1)}` : 'N/A';
        const textY = size / 2 + (hasData ? 1 : 5);
        const fontSize = hasData ? 11 : 12;
        const ftLine = hasData ? `<text x="${size/2}" y="${size/2 + 13}" text-anchor="middle" fill="${color}" font-size="8">ft</text>` : '';
        const hasGauge = s.observed_ft != null;
        const gaugeRing = hasGauge ? `<circle cx="${size/2}" cy="${size/2}" r="${size/2 - 0.5}" fill="none" stroke="#2ecc71" stroke-width="1.5" stroke-dasharray="3,2"/>` : '';
        const svg = `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" xmlns="http://www.w3.org/2000/svg">
            <circle cx="${size/2}" cy="${size/2}" r="${size/2 - 2}" fill="rgba(10,22,40,0.9)" stroke="${color}" stroke-width="2.5"/>
            ${gaugeRing}
            <text x="${size/2}" y="${textY}" text-anchor="middle" dominant-baseline="middle" fill="${color}" font-size="${fontSize}" font-weight="bold">${label}</text>
            ${ftLine}
        </svg>`;

        const icon = L.divIcon({
            html: svg,
            className: 'tide-station-icon',
            iconSize: [size, size],
            iconAnchor: [size / 2, size / 2],
        });

        // Build popup content
        let popupRows;
        if (!hasData) {
            popupRows = `<div class="popup-row"><span class="popup-label">Tide Height</span><span class="popup-value" style="color:#888">Data unavailable</span></div>`;
        } else {
            popupRows = `<div class="popup-row"><span class="popup-label">Predicted</span><span class="popup-value" style="color:${color}">${sign}${h.toFixed(2)} ft</span></div>`;
            if (s.observed_ft != null) {
                const obsSign = s.observed_ft >= 0 ? '+' : '';
                const obsColor = s.observed_ft >= 0 ? '#5dade2' : '#e74c3c';
                const delta = s.observed_ft - h;
                const deltaSign = delta >= 0 ? '+' : '';
                const deltaColor = Math.abs(delta) <= 0.3 ? '#2ecc71' : '#f39c12';
                popupRows += `<div class="popup-row"><span class="popup-label">Observed</span><span class="popup-value" style="color:${obsColor}">${obsSign}${s.observed_ft.toFixed(2)} ft</span></div>`;
                popupRows += `<div class="popup-row"><span class="popup-label">Difference</span><span class="popup-value" style="color:${deltaColor}">${deltaSign}${delta.toFixed(2)} ft</span></div>`;
            }
            if (s.next_extreme) {
                const ex = s.next_extreme;
                const exTime = new Date(ex.time + 'Z').toLocaleTimeString('en-US', {
                    hour: 'numeric', minute: '2-digit', timeZone: 'America/Los_Angeles'
                });
                const exColor = ex.type === 'High' ? '#5dade2' : '#e74c3c';
                popupRows += `<div class="popup-row"><span class="popup-label">Next ${ex.type}</span><span class="popup-value" style="color:${exColor}">${ex.height_ft.toFixed(2)} ft @ ${exTime}</span></div>`;
            }
        }
        // Show forecast time if in forecast mode
        if (forecastMinutes > 0) {
            const target = new Date(Date.now() + forecastMinutes * 60000);
            const fTime = target.toLocaleString('en-US', {
                weekday: 'short', month: 'short', day: 'numeric',
                hour: 'numeric', minute: '2-digit',
                timeZone: 'America/Los_Angeles'
            });
            popupRows += `<div class="popup-row"><span class="popup-label">Forecast time</span><span class="popup-value" style="color:#f39c12">${fTime}</span></div>`;
        }
        if (hasData) popupRows += `<div class="popup-row"><span class="popup-label">Datum</span><span class="popup-value">MLLW</span></div>`;

        const marker = L.marker([s.lat, s.lon], { icon, zIndexOffset: 500 })
            .bindPopup(`<div class="popup-content"><h3>${s.name}</h3>${popupRows}</div>`, { className: 'vessel-popup', maxWidth: 240 });

        tideStationMarkers.addLayer(marker);
    }

    // Re-add to map if toggle is on
    if (tideMarkersVisible) {
        tideStationMarkers.addTo(map);
    }
}

function updateFlowConfidence() {
    const el = document.getElementById('flow-legend-confidence');
    if (!el) return;

    if (forecastMinutes !== 0) {
        el.innerHTML = '';
        return;
    }

    const gauged = tideStations.filter(s => s.observed_ft != null && s.height_ft != null);
    if (!gauged.length) {
        el.innerHTML = '';
        return;
    }

    const deltas = gauged.map(s => Math.abs(s.observed_ft - s.height_ft));
    const maxDelta = Math.max(...deltas);
    const avgDelta = deltas.reduce((a, b) => a + b, 0) / deltas.length;

    const avgSign = gauged.reduce((a, s) => a + (s.observed_ft - s.height_ft), 0) > 0 ? 'higher' : 'lower';

    let dot, label, detail;
    if (avgDelta <= 0.3) {
        dot = '🟢';
        label = 'High confidence';
        detail = 'Gauges match predictions — current speed & timing reliable';
    } else if (avgDelta <= 0.5) {
        dot = '🟡';
        label = 'Moderate confidence';
        detail = avgSign === 'higher'
            ? 'Water higher than predicted — currents ~10-20% stronger, slack may come earlier'
            : 'Water lower than predicted — currents ~10-20% weaker, slack may come later';
    } else {
        dot = '🔴';
        label = 'Low confidence';
        detail = avgSign === 'higher'
            ? 'Water much higher — expect stronger currents & earlier slack times'
            : 'Water much lower — expect weaker currents & later slack times';
    }

    el.innerHTML = `<span>${dot} ${label}</span> <span style="color:#4a6a8a">(avg Δ${avgDelta.toFixed(1)}ft ${avgSign})</span><br><span style="color:#4a6a8a">${detail}</span>`;
}

loadTideHeight();

// Tide toggle button
const tideToggleBtn = document.getElementById('tide-toggle');
if (tideToggleBtn) {
    tideToggleBtn.classList.add('tide-off');
    tideToggleBtn.addEventListener('click', () => {
        tideMarkersVisible = !tideMarkersVisible;
        if (tideMarkersVisible) {
            if (tideStationMarkers) tideStationMarkers.addTo(map);
            tideToggleBtn.textContent = 'Tide: ON';
            tideToggleBtn.classList.remove('tide-off');
        } else {
            if (tideStationMarkers) map.removeLayer(tideStationMarkers);
            tideToggleBtn.textContent = 'Tide: OFF';
            tideToggleBtn.classList.add('tide-off');
        }
    });
}

autoRefreshTimers.wind = setInterval(loadWindField, 300000);

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
            // Only show station markers in real-time mode
            if (windStationMarkers && forecastMinutes === 0) windStationMarkers.show();
            windToggleBtn.textContent = 'Wind: ON';
            windToggleBtn.classList.remove('wind-off');
            if (legend) legend.classList.add('visible');
        }
    });
}

// --- Forecast Time Shift (Windy-style timeline) ---
const forecastBanner = document.getElementById('forecast-banner');
const forecastTimeDisplay = document.getElementById('forecast-time-display');
const forecastProgressBar = document.getElementById('forecast-progress-bar');
const timelineTrack = document.getElementById('timeline-track');
const timelineScroll = document.getElementById('timeline-scroll');
const timelineGoBtn = document.getElementById('timeline-go');
const timelineNowBtn = document.getElementById('timeline-now');
const timelineCalBtn = document.getElementById('timeline-calendar');
const timePickerPanel = document.getElementById('time-picker-panel');
const forecastDateInput = document.getElementById('forecast-date');
const forecastTimeInput = document.getElementById('forecast-time');
const forecastGoBtn = document.getElementById('forecast-go');
const forecastCancelBtn = document.getElementById('forecast-cancel');

let _forecastLoading = false;
let _selectedHourEl = null;  // currently highlighted hour element
let _windWasOn = false;      // track if wind was on before going beyond 48h

// --- Build the 48-hour timeline ---
function buildTimeline() {
    if (!timelineTrack) return;
    timelineTrack.innerHTML = '';

    const now = new Date();
    // Round down to the current hour
    const startHour = new Date(now.getFullYear(), now.getMonth(), now.getDate(), now.getHours());
    const currentHourMs = startHour.getTime();

    // Group hours by day
    const dayGroups = {};  // key: "YYYY-MM-DD", value: [{hour, label, isNow, minutesFromNow}]
    for (let h = 0; h <= 48; h++) {
        const t = new Date(currentHourMs + h * 3600000);
        const dayKey = t.toLocaleDateString('en-CA', { timeZone: 'America/Los_Angeles' });
        if (!dayGroups[dayKey]) dayGroups[dayKey] = [];
        const hourLabel = t.toLocaleTimeString('en-US', {
            hour: 'numeric', hour12: true, timeZone: 'America/Los_Angeles'
        }).replace(' ', '').toLowerCase(); // "8pm", "11am"
        dayGroups[dayKey].push({
            date: t,
            label: h === 0 ? 'Now' : hourLabel,
            isNow: h === 0,
            minutesFromNow: h * 60
        });
    }

    // Render each day
    for (const [dayKey, hours] of Object.entries(dayGroups)) {
        const dayGroup = document.createElement('div');
        dayGroup.className = 'timeline-day-group';

        // Day label
        const dayLabel = document.createElement('div');
        dayLabel.className = 'timeline-day-label';
        const dayDate = new Date(dayKey + 'T12:00:00');
        dayLabel.textContent = dayDate.toLocaleDateString('en-US', {
            weekday: 'short', month: 'short', day: 'numeric',
            timeZone: 'America/Los_Angeles'
        });
        dayGroup.appendChild(dayLabel);

        // Hours row
        const hoursRow = document.createElement('div');
        hoursRow.className = 'timeline-hours-row';

        for (const hr of hours) {
            const el = document.createElement('div');
            el.className = 'timeline-hour';
            if (hr.isNow) el.classList.add('now');
            el.textContent = hr.label;
            el.dataset.minutes = hr.minutesFromNow;

            el.addEventListener('click', () => {
                selectTimelineHour(el, hr.minutesFromNow);
            });

            hoursRow.appendChild(el);
        }

        dayGroup.appendChild(hoursRow);
        timelineTrack.appendChild(dayGroup);
    }
}

function selectTimelineHour(el, minutes) {
    // Deselect previous
    if (_selectedHourEl) _selectedHourEl.classList.remove('selected');

    if (minutes === 0) {
        // Clicking "Now" resets
        _selectedHourEl = null;
        forecastMinutes = 0;
        timelineGoBtn.classList.add('hidden');
        updateTimeShiftUI();
        reloadEnvironmentalData();
        return;
    }

    _selectedHourEl = el;
    el.classList.add('selected');
    forecastMinutes = minutes;
    timelineGoBtn.classList.remove('hidden');
    updateTimeShiftUI();
    // Do NOT fetch yet — user must click GO
}

function updateTimeShiftUI() {
    const isForecast = forecastMinutes > 0;
    const statusBar = document.getElementById('status-bar');

    if (!isForecast) {
        forecastBanner.classList.add('hidden');
        statusBar.classList.remove('forecast-mode');
        // Restore wind if it was on before going beyond 48h
        if (_windWasOn && windOverlay && !windOverlay.animating) {
            windOverlay.start();
            const windLegend = document.getElementById('wind-legend');
            if (windLegend) windLegend.classList.add('visible');
            _windWasOn = false;
        }
        if (windStationMarkers && windOverlay && windOverlay.animating) {
            windStationMarkers.show();
        }
    } else {
        const target = new Date(Date.now() + forecastMinutes * 60000);
        const fullTime = target.toLocaleString('en-US', {
            weekday: 'long', month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit',
            timeZone: 'America/Los_Angeles'
        });
        forecastTimeDisplay.textContent = fullTime;
        forecastBanner.classList.remove('hidden');
        statusBar.classList.add('forecast-mode');
        if (windStationMarkers) windStationMarkers.hide();

        // Beyond 48h — no wind forecast available, turn off wind overlay
        const windLegend = document.getElementById('wind-legend');
        if (forecastMinutes > 48 * 60) {
            if (windOverlay && windOverlay.animating) {
                _windWasOn = true;
                windOverlay.stop();
                if (windLegend) windLegend.classList.remove('visible');
            }
        } else if (_windWasOn) {
            // Back within 48h — restore wind if user had it on
            if (windOverlay && !windOverlay.animating) {
                windOverlay.start();
                if (windLegend) windLegend.classList.add('visible');
            }
            _windWasOn = false;
        }
    }
}

function setForecastProgress(loaded, total) {
    if (!forecastProgressBar) return;
    const pct = Math.round((loaded / total) * 100);
    forecastProgressBar.style.width = pct + '%';
    if (forecastTimeDisplay && loaded < total) {
        const target = new Date(Date.now() + forecastMinutes * 60000);
        const fullTime = target.toLocaleString('en-US', {
            weekday: 'long', month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit',
            timeZone: 'America/Los_Angeles'
        });
        forecastTimeDisplay.textContent = fullTime + ' — ' + pct + '%';
    }
}

function setForecastLoading(loading) {
    _forecastLoading = loading;
    if (loading && forecastMinutes > 0) {
        forecastBanner.classList.remove('hidden');
        if (forecastProgressBar) forecastProgressBar.style.width = '0%';
        const target = new Date(Date.now() + forecastMinutes * 60000);
        const fullTime = target.toLocaleString('en-US', {
            weekday: 'long', month: 'short', day: 'numeric',
            hour: 'numeric', minute: '2-digit',
            timeZone: 'America/Los_Angeles'
        });
        forecastTimeDisplay.textContent = fullTime + ' — 0%';
    } else {
        if (forecastProgressBar) forecastProgressBar.style.width = '100%';
        if (forecastMinutes > 0) {
            const target = new Date(Date.now() + forecastMinutes * 60000);
            const fullTime = target.toLocaleString('en-US', {
                weekday: 'long', month: 'short', day: 'numeric',
                hour: 'numeric', minute: '2-digit',
                timeZone: 'America/Los_Angeles'
            });
            forecastTimeDisplay.textContent = fullTime + ' \u2714';
        }
        // Fade progress bar after completion
        setTimeout(() => {
            if (forecastProgressBar && !_forecastLoading) forecastProgressBar.style.width = '0%';
        }, 1500);
    }
}

function manageAutoRefresh() {
    clearInterval(autoRefreshTimers.currents);
    clearInterval(autoRefreshTimers.field);
    clearInterval(autoRefreshTimers.wind);
    clearInterval(autoRefreshTimers.tide);

    if (forecastMinutes === 0) {
        autoRefreshTimers.currents = setInterval(loadCurrents, 60000);
        autoRefreshTimers.field = setInterval(loadCurrentField, 300000);
        autoRefreshTimers.wind = setInterval(loadWindField, 300000);
        autoRefreshTimers.tide = setInterval(loadTideHeight, 300000);
    }
}

async function reloadEnvironmentalData() {
    manageAutoRefresh();
    const hasCachedDl = !!_getLastDlTime();
    if (!hasCachedDl) setForecastLoading(true);

    let loaded = 0;
    const total = forecastMinutes > 48 * 60 ? 3 : 4;  // currents + field + tide (+ wind if ≤48h)

    try {
        const promises = [
            loadCurrents().then(() => { loaded++; if (!hasCachedDl) setForecastProgress(loaded, total); }),
            loadCurrentField().then(() => { loaded++; if (!hasCachedDl) setForecastProgress(loaded, total); }),
            loadTideHeight().then(() => { loaded++; if (!hasCachedDl) setForecastProgress(loaded, total); }),
        ];
        if (forecastMinutes <= 48 * 60) {
            promises.push(loadWindField().then(() => { loaded++; if (!hasCachedDl) setForecastProgress(loaded, total); }));
        }
        await Promise.allSettled(promises);
    } finally {
        if (!hasCachedDl) setForecastLoading(false);
    }
}

// --- GO button ---
if (timelineGoBtn) {
    timelineGoBtn.addEventListener('click', () => {
        if (forecastMinutes > 0) {
            reloadEnvironmentalData();
        }
    });
}

// --- NOW button ---
if (timelineNowBtn) {
    timelineNowBtn.addEventListener('click', () => {
        forecastMinutes = 0;
        if (_selectedHourEl) _selectedHourEl.classList.remove('selected');
        _selectedHourEl = null;
        timelineGoBtn.classList.add('hidden');
        updateTimeShiftUI();
        reloadEnvironmentalData();
    });
}

// --- Calendar button — open date/time picker ---
if (timelineCalBtn) {
    timelineCalBtn.addEventListener('click', () => {
        const target = new Date(Date.now() + forecastMinutes * 60000);
        const laDate = target.toLocaleDateString('en-CA', { timeZone: 'America/Los_Angeles' });
        const laTime = target.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', timeZone: 'America/Los_Angeles' });
        if (forecastDateInput) forecastDateInput.value = laDate;
        if (forecastTimeInput) forecastTimeInput.value = laTime;

        // Min = today, no max (tides are unlimited)
        const now = new Date();
        const todayStr = now.toLocaleDateString('en-CA', { timeZone: 'America/Los_Angeles' });
        if (forecastDateInput) {
            forecastDateInput.min = todayStr;
            forecastDateInput.removeAttribute('max');
        }

        if (timePickerPanel) timePickerPanel.classList.remove('hidden');
    });
}

// --- Date/time picker Go button ---
if (forecastGoBtn) {
    forecastGoBtn.addEventListener('click', () => {
        const dateVal = forecastDateInput ? forecastDateInput.value : '';
        const timeVal = forecastTimeInput ? forecastTimeInput.value : '12:00';
        if (!dateVal) return;

        const targetStr = `${dateVal}T${timeVal}:00`;
        const target = new Date(targetStr);
        const nowMs = Date.now();
        const diffMs = target.getTime() - nowMs;

        if (diffMs <= 0) {
            forecastMinutes = 0;
        } else {
            forecastMinutes = Math.round(diffMs / 60000);
        }

        // Highlight corresponding hour on the strip if within 48h
        if (_selectedHourEl) _selectedHourEl.classList.remove('selected');
        _selectedHourEl = null;
        if (forecastMinutes > 0 && forecastMinutes <= 48 * 60) {
            const hourSlot = Math.round(forecastMinutes / 60);
            const hourEls = timelineTrack.querySelectorAll('.timeline-hour');
            if (hourEls[hourSlot]) {
                _selectedHourEl = hourEls[hourSlot];
                _selectedHourEl.classList.add('selected');
            }
        }

        if (timePickerPanel) timePickerPanel.classList.add('hidden');
        timelineGoBtn.classList.add('hidden');
        updateTimeShiftUI();
        reloadEnvironmentalData();
    });
}

// --- Date/time picker Cancel ---
if (forecastCancelBtn) {
    forecastCancelBtn.addEventListener('click', () => {
        if (timePickerPanel) timePickerPanel.classList.add('hidden');
    });
}

// Build the timeline on load
buildTimeline();

// =============================================================================
// MOBILE FORECAST QUICK BUTTONS
// =============================================================================

(function() {
    const fcstNowBtn = document.getElementById('fcst-now');
    const fcstSetTimeBtn = document.getElementById('fcst-set-time');
    const hourBtns = document.querySelectorAll('.fcst-hour-btn');
    const allFcstBtns = document.querySelectorAll('.fcst-quick-btn');

    function clearFcstActive() {
        allFcstBtns.forEach(b => b.classList.remove('active'));
    }

    // NOW button
    if (fcstNowBtn) {
        fcstNowBtn.classList.add('active'); // default active
        fcstNowBtn.addEventListener('click', () => {
            clearFcstActive();
            fcstNowBtn.classList.add('active');
            forecastMinutes = 0;
            if (_selectedHourEl) _selectedHourEl.classList.remove('selected');
            _selectedHourEl = null;
            timelineGoBtn.classList.add('hidden');
            updateTimeShiftUI();
            reloadEnvironmentalData();
        });
    }

    // +1h, +2h, +3h, +4h buttons
    hourBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const hours = parseInt(btn.dataset.hours);
            clearFcstActive();
            btn.classList.add('active');
            forecastMinutes = hours * 60;
            // Also update the desktop timeline if visible
            if (_selectedHourEl) _selectedHourEl.classList.remove('selected');
            _selectedHourEl = null;
            updateTimeShiftUI();
            reloadEnvironmentalData();
        });
    });

    // Set FCST TIME button — opens the date/time picker
    if (fcstSetTimeBtn) {
        fcstSetTimeBtn.addEventListener('click', () => {
            // Same logic as the calendar button
            const target = new Date(Date.now() + forecastMinutes * 60000);
            const laDate = target.toLocaleDateString('en-CA', { timeZone: 'America/Los_Angeles' });
            const laTime = target.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', timeZone: 'America/Los_Angeles' });
            if (forecastDateInput) forecastDateInput.value = laDate;
            if (forecastTimeInput) forecastTimeInput.value = laTime;

            const now = new Date();
            const todayStr = now.toLocaleDateString('en-CA', { timeZone: 'America/Los_Angeles' });
            if (forecastDateInput) {
                forecastDateInput.min = todayStr;
                forecastDateInput.removeAttribute('max');
            }

            if (timePickerPanel) timePickerPanel.classList.remove('hidden');
        });
    }
})();

// =============================================================================
// OFFLINE SUPPORT — pre-fetch environmental data for offline PWA use
// =============================================================================

// --- Download status badges ---
const DL_CATEGORIES = ['flow', 'wind', 'tides', 'currents'];
const DL_BADGE_IDS = { flow: 'dl-flow', wind: 'dl-wind', tides: 'dl-tide', currents: 'dl-curr' };

function _getDlStatus() {
    try { return JSON.parse(localStorage.getItem('ais_dl_status') || '{}'); } catch { return {}; }
}

// Compute hours of forward forecast remaining from stored model_run + file count
function _flowHoursAhead(modelRun, count) {
    if (!modelRun || !count) return null;
    const m = modelRun.match(/t(\d{2})z (\d{2})\/(\d{2})/);
    if (!m) return null;
    const now = new Date();
    let runTime = new Date(Date.UTC(now.getUTCFullYear(), +m[2] - 1, +m[3], +m[1], 0, 0));
    // Guard against Dec/Jan year boundary: if runTime is >12h in future, it's last year
    if (runTime - now > 12 * 3600000) runTime.setUTCFullYear(runTime.getUTCFullYear() - 1);
    const lastHour = new Date(runTime.getTime() + (count - 1) * 3600000);
    return Math.max(0, Math.floor((lastHour - now) / 3600000));
}

function _setDlCategory(cat, success) {
    if (!success) {
        _updateDlBadge(cat, null); // revert to dim — needs retry
        return;
    }
    const s = _getDlStatus();
    s[cat] = new Date().toISOString();
    let hoursAhead = null;
    if (cat === 'flow' && typeof success === 'object') {
        s['flow_count'] = success.count;
        s['flow_model_run'] = success.modelRun || null;
        hoursAhead = _flowHoursAhead(success.modelRun, success.count);
        // If we can't compute hours (broken model_run), treat as failed download
        if (hoursAhead === null) { _updateDlBadge(cat, null); return; }
    }
    localStorage.setItem('ais_dl_status', JSON.stringify(s));
    _updateDlBadge(cat, 'done', hoursAhead);
}

function _updateDlBadge(cat, state, hours) {
    const el = document.getElementById(DL_BADGE_IDS[cat]);
    if (!el) return;
    el.classList.remove('done', 'loading');
    if (state) el.classList.add(state);
    if (cat === 'flow') el.textContent = (hours != null) ? `Flow +${hours}h` : 'Flow';
}

function _initDlBadges() {
    const s = _getDlStatus();
    const sixHours = 6 * 3600 * 1000;
    for (const cat of DL_CATEGORIES) {
        const ts = s[cat] ? new Date(s[cat]).getTime() : 0;
        const isDone = ts && (Date.now() - ts < sixHours);
        // Recompute hoursAhead fresh from stored model_run + count so it stays accurate on reload
        const hoursAhead = (cat === 'flow' && isDone)
            ? _flowHoursAhead(s['flow_model_run'], s['flow_count'])
            : null;
        _updateDlBadge(cat, isDone ? 'done' : null, hoursAhead);
    }
}

_initDlBadges();
const offlineBanner = document.getElementById('offline-banner');
const offlineCacheAge = document.getElementById('offline-cache-age');
const offlineDlBtn = document.getElementById('offline-download');
const offlineDlPanel = document.getElementById('offline-dl-panel');
const offlineDlBar = document.getElementById('offline-dl-bar');
const offlineDlStatus = document.getElementById('offline-dl-status');

let _isOffline = !navigator.onLine;
let _downloadingOffline = false;

// Called by load* functions when they get cached data from the service worker
function _notifyCachedData() {
    // Show banner if offline
    if (_isOffline && offlineBanner) {
        offlineBanner.classList.remove('hidden');
    }
}

// --- Offline/online detection ---
function updateOfflineBanner() {
    if (_isOffline) {
        if (offlineBanner) offlineBanner.classList.remove('hidden');
    } else {
        if (offlineBanner) offlineBanner.classList.add('hidden');
    }
}

window.addEventListener('offline', () => {
    _isOffline = true;
    updateOfflineBanner();
    // Stop auto-refresh timers — no point when offline
    clearInterval(autoRefreshTimers.currents);
    clearInterval(autoRefreshTimers.field);
    clearInterval(autoRefreshTimers.wind);
    clearInterval(autoRefreshTimers.tide);
});

window.addEventListener('online', () => {
    _isOffline = false;
    updateOfflineBanner();
    // Restart auto-refresh if in real-time mode
    if (forecastMinutes === 0) {
        manageAutoRefresh();
        // Refresh data now that we're back online
        reloadEnvironmentalData();
    }
});

updateOfflineBanner();

// --- Download 24h of environmental data for offline use ---

const offlineDlLast = document.getElementById('offline-dl-last');
const offlineDlAge = document.getElementById('offline-dl-age');
const appBuildEl = document.getElementById('app-build');
if (appBuildEl) appBuildEl.textContent = APP_BUILD;

function _notifySwCacheReady() {
    if (navigator.serviceWorker && navigator.serviceWorker.controller) {
        navigator.serviceWorker.controller.postMessage({ type: 'ENV_CACHE_READY' });
    }
}

function _getLastDlTime() {
    return localStorage.getItem('ais_offline_dl_time');
}

// If a previous download exists, tell SW to use cache-first
if (_getLastDlTime()) _notifySwCacheReady();

function _updateLastDlDisplay() {
    const last = _getLastDlTime();
    if (!last) {
        if (offlineDlLast) offlineDlLast.textContent = '';
        if (offlineDlAge) offlineDlAge.textContent = '';
        if (offlineDlBtn) offlineDlBtn.title = 'Download 24h for offline use';
        return;
    }
    const ageMins = Math.floor((Date.now() - new Date(last).getTime()) / 60000);
    let ageText;
    if (ageMins < 1) ageText = 'just now';
    else if (ageMins < 60) ageText = `${ageMins}m ago`;
    else {
        const h = Math.floor(ageMins / 60);
        const m = ageMins % 60;
        ageText = m > 0 ? `${h}h ${m}m ago` : `${h}h ago`;
    }
    if (offlineDlLast) offlineDlLast.textContent = `Last downloaded: ${ageText}`;
    if (offlineDlAge) offlineDlAge.textContent = `DL: ${ageText}`;
    if (offlineDlBtn) offlineDlBtn.title = `Download 24h · Last: ${ageText}`;
}

// Show last download time on load
_updateLastDlDisplay();

async function downloadForOffline(silent = false) {
    if (_downloadingOffline) return;
    _downloadingOffline = true;

    const btn = offlineDlBtn;
    if (btn) btn.classList.add('downloading');
    if (!silent) {
        if (offlineDlPanel) offlineDlPanel.classList.remove('hidden');
        if (offlineDlStatus) offlineDlStatus.textContent = 'Downloading data for offline use\u2026';
        if (offlineDlBar) offlineDlBar.style.width = '0%';
    }

    // Only mark not-yet-done badges as loading (keep green ones green)
    const s0 = _getDlStatus(); const sixHours = 6 * 3600 * 1000;
    for (const cat of DL_CATEGORIES) {
        const ts = s0[cat] ? new Date(s0[cat]).getTime() : 0;
        if (!(ts && (Date.now() - ts < sixHours))) _updateDlBadge(cat, 'loading');
    }
    if (offlineDlAge) offlineDlAge.textContent = 'DL: downloading\u2026';

    try {
        await downloadAllForOffline(
            (completed, total) => {
                if (!silent) {
                    const pct = Math.round((completed / total) * 100);
                    if (offlineDlBar) offlineDlBar.style.width = pct + '%';
                    if (offlineDlStatus) offlineDlStatus.textContent = `${completed} / ${total}`;
                }
            },
            (cat, success) => _setDlCategory(cat, success)
        );
    } catch (e) {
        console.log('Offline download error:', e);
    }

    // Save timestamp only if at least one category succeeded; switch SW to cache-first
    if (_allCategoriesDone() || DL_CATEGORIES.some(cat => { const s = _getDlStatus(); return !!s[cat]; })) {
        localStorage.setItem('ais_offline_dl_time', new Date().toISOString());
    }
    _notifySwCacheReady();

    // Done
    _downloadingOffline = false;
    if (btn) {
        btn.classList.remove('downloading');
        btn.classList.add('done');
        setTimeout(() => btn.classList.remove('done'), 3000);
    }
    if (!silent) {
        if (offlineDlStatus) offlineDlStatus.textContent = 'Done! Data cached for offline use.';
        _updateLastDlDisplay();
        setTimeout(() => {
            if (offlineDlPanel) offlineDlPanel.classList.add('hidden');
        }, 3000);
    } else {
        _updateLastDlDisplay();
    }
}

if (offlineDlBtn) {
    offlineDlBtn.addEventListener('click', downloadForOffline);
}

// === NMEA Sailing Dashboard Integration ===

const nmeaStore = typeof NmeaStore !== 'undefined' ? new NmeaStore() : null;
const nmeaClient = nmeaStore ? new NmeaClient(nmeaStore) : null;
const sailingCharts = nmeaStore && typeof SailingCharts !== 'undefined' ? new SailingCharts(nmeaStore) : null;

if (nmeaStore && nmeaClient) {
    // Tab switching
    document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.addEventListener('click', () => {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            btn.classList.add('active');
            const targetId = btn.dataset.tab;
            document.getElementById('map-view').style.display = targetId === 'map-view' ? '' : 'none';
            document.getElementById('charts-view').style.display = targetId === 'charts-view' ? '' : 'none';
            const radarEl = document.getElementById('radar-view');
            if (radarEl) radarEl.style.display = targetId === 'radar-view' ? '' : 'none';

            // Map elements visibility
            const mapOnly = ['status-bar', 'timeline-strip', 'layers-tray', 'forecast-quick-btns',
                             'flow-legend', 'wind-legend', 'panel', 'panel-toggle'];
            mapOnly.forEach(id => {
                const el = document.getElementById(id);
                if (el) el.style.display = targetId === 'map-view' ? '' : 'none';
            });

            if (targetId === 'map-view') map.invalidateSize();
            if (targetId === 'radar-view' && window._radarView) window._radarView.show();
        });
    });

    // Initialize charts view
    if (sailingCharts) sailingCharts.init();

    // Initialize radar view
    if (typeof RadarView !== 'undefined') {
        window._radarView = new RadarView(vesselStore);
        window._radarView.init();
    }

    // NMEA status display
    const nmeaStatusEl = document.getElementById('nmea-status');
    nmeaClient.setStatusCallback((status, rate) => {
        if (nmeaStatusEl) {
            nmeaStatusEl.className = 'nmea-status ' + status;
            if (status === 'connected') nmeaStatusEl.textContent = `NMEA: ${rate}/s`;
            else if (status === 'replaying') nmeaStatusEl.textContent = `Replay: ${rate}/s`;
            else if (status === 'replay-paused') nmeaStatusEl.textContent = 'Replay: Paused';
            else if (status === 'replay-done') nmeaStatusEl.textContent = 'Replay: Done';
            else nmeaStatusEl.textContent = 'NMEA: Off';
        }
    });

    // NMEA own position → map + competitor labels
    nmeaStore.addEventListener('position', (e) => {
        nmeaOwnPosition = e.detail;
        if (e.detail.lat && e.detail.lon) {
            ownPosition = { lat: e.detail.lat, lon: e.detail.lon };
            window.ownPosition = ownPosition;
            const s = nmeaStore.getState();
            const v = {
                mmsi: OWN_MMSI, name: OWN_NAME, is_own_vessel: true,
                lat: e.detail.lat, lon: e.detail.lon,
                sog: s.sog, cog: s.cog, heading: s.heading,
                ship_category: 'Sailing/Pleasure',
            };
            vesselStore.upsert(v);
            updateMarker(v);
            ownVessel = v;
        }
    });

    // NMEA AIS → vessel store
    nmeaStore.addEventListener('ais', (e) => {
        const v = e.detail;
        if (!v || !v.mmsi) return;
        vesselStore.upsert(v);
        const merged = vesselStore.get(v.mmsi);
        if (merged && merged.lat != null) {
            vessels.set(merged.mmsi, merged);
            updateMarker(merged);
            updatePanel();
            if (merged.is_own_vessel || merged.mmsi === OWN_MMSI) {
                ownPosition = { lat: merged.lat, lon: merged.lon };
                window.ownPosition = ownPosition;
                ownVessel = merged;
            }
        }
    });

    // WebSocket connect button
    const wsUrlInput = document.getElementById('nmea-ws-url');
    const connectBtn = document.getElementById('nmea-connect-btn');
    const savedUrl = localStorage.getItem('nmea_ws_url');

    if (connectBtn) {
        connectBtn.addEventListener('click', () => {
            if (nmeaClient._status === 'connected') {
                nmeaClient.disconnect();
                connectBtn.textContent = 'Connect';
                connectBtn.classList.remove('connected');
            } else {
                const url = wsUrlInput ? wsUrlInput.value.trim() : '';
                if (!url) return;
                localStorage.setItem('nmea_ws_url', url);
                nmeaClient.connect(url);
                connectBtn.textContent = 'Disconnect';
                connectBtn.classList.add('connected');
            }
        });
    }

    // Auto-connect: pick ws:// or wss:// based on page protocol
    const isSecure = location.protocol === 'https:';
    const defaultUrl = isSecure ? 'wss://raspberrypi.local:8766' : 'ws://raspberrypi.local:8765';
    let autoConnectUrl = savedUrl || defaultUrl;
    // Upgrade saved ws:// to wss:// on HTTPS (or downgrade wss:// on HTTP)
    if (isSecure && autoConnectUrl.startsWith('ws://')) {
        autoConnectUrl = autoConnectUrl.replace('ws://', 'wss://').replace(':8765', ':8766');
    } else if (!isSecure && autoConnectUrl.startsWith('wss://')) {
        autoConnectUrl = autoConnectUrl.replace('wss://', 'ws://').replace(':8766', ':8765');
    }
    if (wsUrlInput) wsUrlInput.value = autoConnectUrl;

    // Show cert trust link on HTTPS (first-time setup for self-signed Pi cert)
    const certLink = document.getElementById('cert-trust-link');
    if (certLink) certLink.style.display = location.protocol === 'https:' ? 'inline' : 'none';

    setTimeout(() => {
        try {
            const testWs = new WebSocket(autoConnectUrl);
            testWs.onopen = () => {
                testWs.close();
                nmeaClient.connect(autoConnectUrl);
                localStorage.setItem('nmea_ws_url', autoConnectUrl);
                if (connectBtn) {
                    connectBtn.textContent = 'Disconnect';
                    connectBtn.classList.add('connected');
                }
                if (certLink) certLink.style.display = 'none';
            };
            testWs.onerror = () => { testWs.close(); };
        } catch (e) { /* silently fail */ }
    }, 2000);

    // File replay
    const fileInput = document.getElementById('nmea-file-input');
    const replayControls = document.getElementById('nmea-replay-controls');
    const replayPauseBtn = document.getElementById('replay-pause');
    const replaySpeedSel = document.getElementById('replay-speed');
    const replayProgressEl = document.getElementById('replay-progress');

    if (fileInput) {
        fileInput.addEventListener('change', (e) => {
            const file = e.target.files[0];
            if (!file) return;
            nmeaClient.loadFile(file, (lineCount) => {
                if (replayControls) replayControls.classList.remove('hidden');
                nmeaClient.startReplay(parseInt(replaySpeedSel?.value) || 1);
                // Update progress
                const progressTimer = setInterval(() => {
                    const p = nmeaClient.getReplayProgress();
                    if (!p) { clearInterval(progressTimer); return; }
                    if (replayProgressEl) replayProgressEl.textContent = p.pct + '%';
                    if (p.pct >= 100) clearInterval(progressTimer);
                }, 250);
            });
        });
    }

    if (replayPauseBtn) {
        replayPauseBtn.addEventListener('click', () => {
            if (nmeaClient._replayPaused) {
                nmeaClient.resumeReplay();
                replayPauseBtn.textContent = '⏸';
            } else {
                nmeaClient.pauseReplay();
                replayPauseBtn.textContent = '▶';
            }
        });
    }

    if (replaySpeedSel) {
        replaySpeedSel.addEventListener('change', () => {
            nmeaClient.setReplaySpeed(parseInt(replaySpeedSel.value));
        });
    }
}
