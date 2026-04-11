/**
 * Tidal Flow Overlay — animated particle visualization of tidal currents
 * Renders on a Canvas overlay on top of the Leaflet map.
 * Interpolates between NOAA station data using inverse distance weighting.
 * Particles are confined to water areas using a SF Bay polygon mask.
 */

// Smaller SF Bay water polygon — pulled inward from shoreline
// Keeps particles safely in open water, OK if some flow over land edges
const SF_BAY_WATER = [
    // Golden Gate channel (narrower)
    [37.8250, -122.4950],
    [37.8150, -122.4800],
    // Central bay
    [37.8300, -122.4400],
    [37.8450, -122.4200],
    // Raccoon Strait area
    [37.8600, -122.4100],
    // Richmond / north bay
    [37.9000, -122.3900],
    [37.9150, -122.3600],
    [37.9050, -122.3400],
    // East bay shore
    [37.8700, -122.3500],
    [37.8500, -122.3500],
    [37.8300, -122.3550],
    // Bay Bridge area
    [37.8100, -122.3600],
    [37.7950, -122.3550],
    // Oakland inner harbor
    [37.7800, -122.3400],
    [37.7600, -122.3100],
    [37.7400, -122.2900],
    // South bay (trimmed)
    [37.7200, -122.2800],
    [37.7000, -122.2600],
    [37.6800, -122.2400],
    [37.6600, -122.2200],
    // South bay west shore (pulled in)
    [37.6700, -122.2600],
    [37.6900, -122.2900],
    [37.7100, -122.3100],
    [37.7300, -122.3300],
    // SF waterfront (pulled offshore)
    [37.7600, -122.3600],
    [37.7800, -122.3800],
    [37.7950, -122.3900],
    // Embarcadero (offshore)
    [37.8050, -122.3950],
    [37.8100, -122.4100],
    [37.8100, -122.4250],
    // Crissy Field (offshore)
    [37.8100, -122.4450],
    [37.8100, -122.4650],
    // Back to Golden Gate
    [37.8150, -122.4800],
    [37.8250, -122.4950],
];

function _pointInPolygon(lat, lon, polygon) {
    let inside = false;
    for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i++) {
        const yi = polygon[i][0], xi = polygon[i][1];
        const yj = polygon[j][0], xj = polygon[j][1];
        if (((yi > lat) !== (yj > lat)) && (lon < (xj - xi) * (lat - yi) / (yj - yi) + xi)) {
            inside = !inside;
        }
    }
    return inside;
}

class TidalFlowOverlay {
    constructor(map, options = {}) {
        this.map = map;
        this.stations = [];
        this.particles = [];
        this.animating = false;
        this.canvas = null;
        this.ctx = null;
        this.waterPolygon = options.waterPolygon || SF_BAY_WATER;

        // Config
        this.particleCount = options.particleCount || 2000;
        this.particleAge = options.particleAge || 80;
        this.lineWidth = options.lineWidth || 1.2;
        this.speedFactor = options.speedFactor || 0.4;
        this.fadeOpacity = options.fadeOpacity || 0.93;
        this.useWaterMask = options.useWaterMask !== undefined ? options.useWaterMask : true;

        // Windy-style color ramp: speed (kn) → color
        this.colorStops = [
            { speed: 0.0, color: [15, 40, 180] },    // deep blue (slack)
            { speed: 0.2, color: [30, 110, 220] },   // blue
            { speed: 0.5, color: [40, 190, 220] },   // cyan
            { speed: 0.8, color: [50, 200, 100] },   // green
            { speed: 1.2, color: [160, 220, 50] },   // yellow-green
            { speed: 1.8, color: [240, 200, 30] },   // yellow
            { speed: 2.5, color: [240, 130, 20] },   // orange
            { speed: 3.5, color: [220, 50, 30] },    // red
            { speed: 5.0, color: [180, 20, 60] },    // dark red
        ];

        this._initCanvas();
        this._bindEvents();
    }

    _isWater(lat, lon) {
        if (!this.useWaterMask) return true;
        return _pointInPolygon(lat, lon, this.waterPolygon);
    }

    _initCanvas() {
        this.canvas = document.createElement('canvas');
        this.canvas.id = 'tidal-flow-canvas';
        this.canvas.style.cssText = 'position:absolute;top:0;left:0;z-index:400;pointer-events:none;opacity:0.8;';
        this.map.getContainer().appendChild(this.canvas);
        this.ctx = this.canvas.getContext('2d');
        this._resize();
    }

    _resize() {
        const size = this.map.getSize();
        this.canvas.width = size.x;
        this.canvas.height = size.y;
        this._resetParticles();
    }

    _bindEvents() {
        this.map.on('moveend', () => this._onMapMove());
        this.map.on('zoomend', () => this._onMapMove());
        this.map.on('resize', () => this._resize());
    }

    _onMapMove() {
        this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
        this._resetParticles();
    }

    _speedToColor(speed) {
        const stops = this.colorStops;
        if (speed <= stops[0].speed) return stops[0].color;
        if (speed >= stops[stops.length - 1].speed) return stops[stops.length - 1].color;

        for (let i = 1; i < stops.length; i++) {
            if (speed <= stops[i].speed) {
                const t = (speed - stops[i-1].speed) / (stops[i].speed - stops[i-1].speed);
                return [
                    Math.round(stops[i-1].color[0] + t * (stops[i].color[0] - stops[i-1].color[0])),
                    Math.round(stops[i-1].color[1] + t * (stops[i].color[1] - stops[i-1].color[1])),
                    Math.round(stops[i-1].color[2] + t * (stops[i].color[2] - stops[i-1].color[2])),
                ];
            }
        }
        return stops[stops.length - 1].color;
    }

    _interpolateAt(lat, lon) {
        if (this.stations.length === 0) return { vx: 0, vy: 0, speed: 0 };

        const cosLat = Math.cos(lat * Math.PI / 180);
        let totalWeight = 0;
        let vx = 0;
        let vy = 0;

        for (const s of this.stations) {
            const dx = (s.lon - lon) * cosLat * 60;
            const dy = (s.lat - lat) * 60;
            const distSq = dx * dx + dy * dy;

            if (distSq < 0.0001) {
                return { vx: s.vx, vy: s.vy, speed: s.speed };
            }

            const w = 1 / distSq;
            vx += s.vx * w;
            vy += s.vy * w;
            totalWeight += w;
        }

        vx /= totalWeight;
        vy /= totalWeight;
        const speed = Math.sqrt(vx * vx + vy * vy);

        return { vx, vy, speed };
    }

    _resetParticles() {
        this.particles = [];
        const bounds = this.map.getBounds();

        for (let i = 0; i < this.particleCount; i++) {
            this.particles.push(this._randomWaterParticle(bounds));
        }
    }

    _randomWaterParticle(bounds) {
        // Try to place particle in water, give up after 20 attempts
        for (let attempt = 0; attempt < 20; attempt++) {
            const lat = bounds.getSouth() + Math.random() * (bounds.getNorth() - bounds.getSouth());
            const lon = bounds.getWest() + Math.random() * (bounds.getEast() - bounds.getWest());
            if (this._isWater(lat, lon)) {
                return { lat, lon, age: Math.floor(Math.random() * this.particleAge) };
            }
        }
        // Fallback: place at a random station location
        if (this.stations.length > 0) {
            const s = this.stations[Math.floor(Math.random() * this.stations.length)];
            return { lat: s.lat + (Math.random() - 0.5) * 0.01, lon: s.lon + (Math.random() - 0.5) * 0.01, age: 0 };
        }
        return { lat: 37.82, lon: -122.42, age: 0 };
    }

    setStations(stations) {
        this.stations = stations.map(s => {
            const dirRad = s.direction * Math.PI / 180;
            return {
                lat: s.lat,
                lon: s.lon,
                speed: s.speed,
                vx: s.speed * Math.sin(dirRad),
                vy: s.speed * Math.cos(dirRad),
            };
        });
    }

    start() {
        if (this.animating) return;
        this.animating = true;
        this.canvas.style.display = '';
        this._animate();
    }

    stop() {
        this.animating = false;
        this.canvas.style.display = 'none';
        this.ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    }

    _animate() {
        if (!this.animating) return;

        const ctx = this.ctx;
        const W = this.canvas.width;
        const H = this.canvas.height;
        const bounds = this.map.getBounds();

        // Fade previous frame
        ctx.globalCompositeOperation = 'destination-in';
        ctx.fillStyle = `rgba(0, 0, 0, ${this.fadeOpacity})`;
        ctx.fillRect(0, 0, W, H);
        ctx.globalCompositeOperation = 'source-over';

        for (let i = 0; i < this.particles.length; i++) {
            const p = this.particles[i];
            p.age++;

            // Reset if too old, out of bounds, or on land
            if (p.age > this.particleAge ||
                p.lat < bounds.getSouth() || p.lat > bounds.getNorth() ||
                p.lon < bounds.getWest() || p.lon > bounds.getEast() ||
                !this._isWater(p.lat, p.lon)) {
                this.particles[i] = this._randomWaterParticle(bounds);
                continue;
            }

            const current = this._interpolateAt(p.lat, p.lon);

            if (current.speed < 0.02) continue;

            // Scale speed by zoom so visual speed stays constant across zoom levels
            // Reference zoom 12: at higher zooms, reduce movement proportionally
            const zoomScale = Math.pow(2, 12 - this.map.getZoom());
            const cosLat = Math.cos(p.lat * Math.PI / 180);
            const dLon = (current.vx / 60 / cosLat) * this.speedFactor * zoomScale;
            const dLat = (current.vy / 60) * this.speedFactor * zoomScale;

            const oldPt = this.map.latLngToContainerPoint([p.lat, p.lon]);

            p.lon += dLon;
            p.lat += dLat;

            const newPt = this.map.latLngToContainerPoint([p.lat, p.lon]);

            const [r, g, b] = this._speedToColor(current.speed);
            const alpha = Math.min(1, 0.4 + current.speed * 0.4);
            ctx.strokeStyle = `rgba(${r},${g},${b},${alpha})`;
            ctx.lineWidth = this.lineWidth;
            ctx.beginPath();
            ctx.moveTo(oldPt.x, oldPt.y);
            ctx.lineTo(newPt.x, newPt.y);
            ctx.stroke();
        }

        requestAnimationFrame(() => this._animate());
    }
}
