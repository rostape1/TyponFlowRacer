/**
 * Wind Overlay — animated particle visualization of wind + NDBC station markers.
 * Renders wind particles on a Canvas overlay on top of the Leaflet map.
 * Displays NDBC buoy observations as point markers with measured wind data.
 */

class WindOverlay {
    constructor(map, options = {}) {
        this.map = map;
        this.grid = null;
        this.particles = [];
        this.animating = false;
        this.canvas = null;
        this.ctx = null;

        this.particleCount = options.particleCount || 800;
        this.particleAge = options.particleAge || 120;
        this.lineWidth = options.lineWidth || 1.5;
        this.speedFactor = options.speedFactor || 0.001;
        this.fadeOpacity = options.fadeOpacity || 0.96;

        this.colorSchemes = {
            green: {
                stops: [
                    { speed: 0,  color: [60, 80, 30] },
                    { speed: 3,  color: [80, 140, 20] },
                    { speed: 8,  color: [120, 200, 30] },
                    { speed: 15, color: [170, 230, 50] },
                    { speed: 25, color: [210, 250, 100] },
                    { speed: 35, color: [255, 255, 255] },
                ],
                hex: ['#508c14', '#78c81e', '#aae632', '#d2fa64', '#eeffaa'],
                accent: '#78c81e',
            },
            purple: {
                stops: [
                    { speed: 0,  color: [140, 120, 180] },
                    { speed: 3,  color: [160, 100, 200] },
                    { speed: 8,  color: [190, 80, 220] },
                    { speed: 15, color: [220, 120, 220] },
                    { speed: 25, color: [240, 190, 230] },
                    { speed: 35, color: [255, 255, 255] },
                ],
                hex: ['#8c78b4', '#a064c8', '#aa46be', '#d26ec8', '#ebb4d7'],
                accent: '#aa46be',
            },
        };
        this.scheme = options.colorScheme || 'purple';
        this.colorStops = this.colorSchemes[this.scheme].stops;

        this._initCanvas();
        this._bindEvents();
    }

    setColorScheme(scheme) {
        if (!this.colorSchemes[scheme]) return;
        this.scheme = scheme;
        this.colorStops = this.colorSchemes[scheme].stops;
    }

    _initCanvas() {
        this.canvas = document.createElement('canvas');
        this.canvas.id = 'wind-flow-canvas';
        this.canvas.style.cssText = 'position:absolute;top:0;left:0;pointer-events:none;opacity:0.8;display:none;';
        // Use a custom Leaflet pane so canvas participates in pane z-ordering
        // z-index 450: above overlayPane (400) but below markerPane (600)
        if (!this.map.getPane('windCanvas')) {
            this.map.createPane('windCanvas');
            this.map.getPane('windCanvas').style.zIndex = 450;
        }
        this.map.getPane('windCanvas').appendChild(this.canvas);
        this.ctx = this.canvas.getContext('2d');
    }

    _resize() {
        const size = this.map.getSize();
        this.canvas.width = size.x;
        this.canvas.height = size.y;
        this._repositionCanvas();
        this._resetParticles();
    }

    _repositionCanvas() {
        // Offset canvas to counteract the pane's CSS transform
        const topLeft = this.map.containerPointToLayerPoint([0, 0]);
        L.DomUtil.setPosition(this.canvas, topLeft);
    }

    _bindEvents() {
        this.map.on('moveend', () => this._onMapMove());
        this.map.on('zoomend', () => this._onMapMove());
        this.map.on('resize', () => { if (this.animating) this._resize(); });
    }

    _onMapMove() {
        if (!this.animating) return;
        this._repositionCanvas();
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

    /**
     * Interpolate wind at a given lat/lon from the grid.
     * Public — used by app.js for vessel popup wind data.
     */
    interpolateAt(lat, lon) {
        if (!this.grid) return null;

        const g = this.grid;
        const b = g.bounds;

        if (lat < b.south || lat > b.north || lon < b.west || lon > b.east) {
            return null;
        }

        const fy = (lat - b.south) / (b.north - b.south) * (g.ny - 1);
        const fx = (lon - b.west) / (b.east - b.west) * (g.nx - 1);
        const iy = Math.floor(fy);
        const ix = Math.floor(fx);

        if (iy < 0 || iy >= g.ny - 1 || ix < 0 || ix >= g.nx - 1) {
            return null;
        }

        const ty = fy - iy;
        const tx = fx - ix;

        const u = (1 - ty) * ((1 - tx) * g.u[iy][ix] + tx * g.u[iy][ix + 1]) +
                  ty * ((1 - tx) * g.u[iy + 1][ix] + tx * g.u[iy + 1][ix + 1]);
        const v = (1 - ty) * ((1 - tx) * g.v[iy][ix] + tx * g.v[iy][ix + 1]) +
                  ty * ((1 - tx) * g.v[iy + 1][ix] + tx * g.v[iy + 1][ix + 1]);
        const speed = Math.sqrt(u * u + v * v);

        let gust = 0;
        if (g.gusts) {
            gust = (1 - ty) * ((1 - tx) * g.gusts[iy][ix] + tx * g.gusts[iy][ix + 1]) +
                   ty * ((1 - tx) * g.gusts[iy + 1][ix] + tx * g.gusts[iy + 1][ix + 1]);
        }

        const dir = (Math.atan2(u, v) * 180 / Math.PI + 360) % 360;

        return { u, v, speed, gust, dir };
    }

    _resetParticles() {
        this.particles = [];
        const bounds = this.map.getBounds();
        for (let i = 0; i < this.particleCount; i++) {
            this.particles.push(this._randomParticle(bounds));
        }
    }

    _randomParticle(bounds) {
        const lat = bounds.getSouth() + Math.random() * (bounds.getNorth() - bounds.getSouth());
        const lon = bounds.getWest() + Math.random() * (bounds.getEast() - bounds.getWest());
        return { lat, lon, age: Math.floor(Math.random() * this.particleAge) };
    }

    setGrid(data) {
        if (!data || !data.u || !data.v) {
            this.grid = null;
            return;
        }
        this.grid = {
            bounds: data.bounds,
            nx: data.nx,
            ny: data.ny,
            u: data.u,
            v: data.v,
            gusts: data.gusts || null,
        };
    }

    start() {
        if (this.animating) return;
        this.animating = true;
        this.canvas.style.display = '';
        this._resize();
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

        this._numberLabels = [];

        for (let i = 0; i < this.particles.length; i++) {
            const p = this.particles[i];
            p.age++;

            if (p.age > this.particleAge ||
                p.lat < bounds.getSouth() || p.lat > bounds.getNorth() ||
                p.lon < bounds.getWest() || p.lon > bounds.getEast()) {
                this.particles[i] = this._randomParticle(bounds);
                continue;
            }

            const wind = this.interpolateAt(p.lat, p.lon);
            if (!wind || wind.speed < 0.5) continue;

            const zoomScale = Math.pow(2, 12 - this.map.getZoom());
            const cosLat = Math.cos(p.lat * Math.PI / 180);
            const dLon = (wind.u / 60 / cosLat) * this.speedFactor * zoomScale;
            const dLat = (wind.v / 60) * this.speedFactor * zoomScale;

            const oldPt = this.map.latLngToContainerPoint([p.lat, p.lon]);

            p.lon += dLon;
            p.lat += dLat;

            const newPt = this.map.latLngToContainerPoint([p.lat, p.lon]);

            const [r, g, b] = this._speedToColor(wind.speed);

            // Every 5th particle: flash speed number during age 30-80
            const isNumberParticle = (i % 5 === 0);
            if (isNumberParticle && p.age >= 30 && p.age <= 80) {
                // Collect for drawing after fade pass (so numbers stay crisp)
                let alpha;
                if (p.age < 40) alpha = (p.age - 30) / 10;        // fade in
                else if (p.age <= 70) alpha = 1;                    // hold
                else alpha = (80 - p.age) / 10;                     // fade out
                this._numberLabels.push({ x: newPt.x, y: newPt.y, speed: wind.speed, r, g, b, alpha });
            } else {
                // Normal arrow trail
                ctx.strokeStyle = `rgba(${r},${g},${b},0.9)`;
                ctx.lineWidth = this.lineWidth;
                ctx.beginPath();
                ctx.moveTo(oldPt.x, oldPt.y);
                ctx.lineTo(newPt.x, newPt.y);
                ctx.stroke();

                // Draw arrowhead at particle tip
                const dx = newPt.x - oldPt.x;
                const dy = newPt.y - oldPt.y;
                const len = Math.sqrt(dx * dx + dy * dy);
                if (len > 1) {
                    const angle = Math.atan2(dy, dx);
                    const headLen = 5;
                    ctx.fillStyle = `rgba(${r},${g},${b},0.9)`;
                    ctx.beginPath();
                    ctx.moveTo(newPt.x, newPt.y);
                    ctx.lineTo(newPt.x - headLen * Math.cos(angle - 0.5), newPt.y - headLen * Math.sin(angle - 0.5));
                    ctx.lineTo(newPt.x - headLen * Math.cos(angle + 0.5), newPt.y - headLen * Math.sin(angle + 0.5));
                    ctx.closePath();
                    ctx.fill();
                }
            }
        }

        // Draw speed number labels on top (after fade pass, so they stay crisp)
        for (const lbl of this._numberLabels) {
            const text = Math.round(lbl.speed).toString();
            ctx.font = 'bold 13px -apple-system, BlinkMacSystemFont, sans-serif';
            ctx.textAlign = 'center';
            ctx.textBaseline = 'middle';
            // Dark background pill
            const metrics = ctx.measureText(text);
            const pw = metrics.width + 8;
            const ph = 16;
            ctx.fillStyle = `rgba(10, 22, 40, ${(lbl.alpha * 0.85).toFixed(2)})`;
            const rx = lbl.x - pw / 2, ry = lbl.y - ph / 2, rr = 4;
            ctx.beginPath();
            if (ctx.roundRect) {
                ctx.roundRect(rx, ry, pw, ph, rr);
            } else {
                ctx.moveTo(rx + rr, ry);
                ctx.arcTo(rx + pw, ry, rx + pw, ry + ph, rr);
                ctx.arcTo(rx + pw, ry + ph, rx, ry + ph, rr);
                ctx.arcTo(rx, ry + ph, rx, ry, rr);
                ctx.arcTo(rx, ry, rx + pw, ry, rr);
                ctx.closePath();
            }
            ctx.fill();
            // Text
            ctx.fillStyle = `rgba(${lbl.r},${lbl.g},${lbl.b},${lbl.alpha.toFixed(2)})`;
            ctx.fillText(text, lbl.x, lbl.y);
        }

        requestAnimationFrame(() => this._animate());
    }
}


/**
 * WindStationMarkers — Leaflet markers for NDBC buoy observations.
 */
class WindStationMarkers {
    constructor(map, overlay) {
        this.map = map;
        this.overlay = overlay || null;
        this.layerGroup = L.layerGroup();
        this.stations = [];
        this.visible = false;
    }

    setStations(stations) {
        this.stations = stations || [];
        this._updateMarkers();
    }

    _speedToColor(speed) {
        const h = this.overlay ? this.overlay.colorSchemes[this.overlay.scheme].hex : ['#508c14', '#78c81e', '#aae632', '#d2fa64', '#eeffaa'];
        if (speed < 3) return h[0];
        if (speed < 8) return h[1];
        if (speed < 15) return h[2];
        if (speed < 25) return h[3];
        return h[4];
    }

    _updateMarkers() {
        this.layerGroup.clearLayers();

        for (const s of this.stations) {
            const color = this._speedToColor(s.speed_kn);
            const size = 36;
            const dir = s.direction || 0;
            const arrowDir = (dir + 180) % 360;

            const svg = `<svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" xmlns="http://www.w3.org/2000/svg">
                <circle cx="${size/2}" cy="${size/2}" r="${size/2 - 2}" fill="rgba(10,22,40,0.85)" stroke="${color}" stroke-width="2"/>
                <g transform="rotate(${arrowDir}, ${size/2}, ${size/2})">
                    <line x1="${size/2}" y1="${size/2 + 7}" x2="${size/2}" y2="${size/2 - 9}"
                          stroke="${color}" stroke-width="2.5" stroke-linecap="round"/>
                    <polygon points="${size/2},${size/2 - 9} ${size/2 - 5},${size/2 - 3} ${size/2 + 5},${size/2 - 3}"
                             fill="${color}"/>
                </g>
                <text x="${size/2}" y="${size - 1}" text-anchor="middle" fill="${color}" font-size="8" font-weight="bold">${s.speed_kn.toFixed(0)}</text>
            </svg>`;

            const icon = L.divIcon({
                html: svg,
                className: 'wind-station-icon',
                iconSize: [size, size],
                iconAnchor: [size / 2, size / 2],
            });

            const gustStr = s.gust_kn != null ? `${s.gust_kn.toFixed(1)} kn` : '—';
            const timeStr = s.timestamp ? new Date(s.timestamp).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', timeZone: 'America/Los_Angeles' }) : '—';

            const accent = this.overlay ? this.overlay.colorSchemes[this.overlay.scheme].accent : '#78c81e';

            const marker = L.marker([s.lat, s.lon], {
                icon,
                zIndexOffset: -50,
            }).bindPopup(`<div class="popup-content">
                <h3>${s.name} (${s.id})</h3>
                <div class="popup-row"><span class="popup-label">Type</span><span class="popup-value" style="color:${accent}">NDBC Observation</span></div>
                <div class="popup-row"><span class="popup-label">Wind</span><span class="popup-value" style="color:${accent}">${s.speed_kn.toFixed(1)} kn / ${s.direction.toFixed(0)}°</span></div>
                <div class="popup-row"><span class="popup-label">Gusts</span><span class="popup-value">${gustStr}</span></div>
                <div class="popup-row"><span class="popup-label">Updated</span><span class="popup-value">${timeStr}</span></div>
            </div>`, { className: 'vessel-popup', maxWidth: 220 });

            this.layerGroup.addLayer(marker);
        }
    }

    show() {
        if (!this.visible) {
            this.layerGroup.addTo(this.map);
            this.visible = true;
        }
    }

    hide() {
        if (this.visible) {
            this.map.removeLayer(this.layerGroup);
            this.visible = false;
        }
    }
}
