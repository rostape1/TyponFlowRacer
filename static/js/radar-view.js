/**
 * Strategic Radar — polar plot of nearby vessels relative to own ship.
 * Draws range rings + crosshairs on canvas, positions vessel labels as DOM overlays.
 */

const RADAR_RANGES = [0.25, 0.5, 1, 2, 4, 8, 16, 32];
const RADAR_RINGS = {
    0.25: [0.05, 0.1, 0.25],
    0.5:  [0.1, 0.25, 0.5],
    1:    [0.25, 0.5, 1],
    2:    [0.5, 1, 2],
    4:    [1, 2, 4],
    8:    [2, 4, 8],
    16:   [4, 8, 16],
    32:   [8, 16, 32],
};

class RadarView {
    constructor(vesselStore) {
        this.store = vesselStore;
        this._rangeIdx = 4; // default 4nm
        this.canvas = document.getElementById('radar-canvas');
        this.vesselLayer = document.getElementById('radar-vessels');
        this._interval = null;
        this._labels = new Map();
        this._needsRedraw = true;
    }

    get maxRange() { return RADAR_RANGES[this._rangeIdx]; }
    get rings() { return RADAR_RINGS[this.maxRange]; }

    _zoomIn() {
        if (this._rangeIdx > 0) {
            this._rangeIdx--;
            this._needsRedraw = true;
            this._update();
        }
    }

    _zoomOut() {
        if (this._rangeIdx < RADAR_RANGES.length - 1) {
            this._rangeIdx++;
            this._needsRedraw = true;
            this._update();
        }
    }

    init() {
        this._resizeBound = () => { this._needsRedraw = true; };
        this._interval = setInterval(() => this._update(), 2000);
        window.addEventListener('resize', this._resizeBound);

        const scope = document.getElementById('radar-scope');
        if (scope) {
            scope.addEventListener('wheel', (e) => {
                e.preventDefault();
                if (e.deltaY > 0) this._zoomOut();
                else if (e.deltaY < 0) this._zoomIn();
            }, { passive: false });
        }

        const zoomIn = document.getElementById('radar-zoom-in');
        const zoomOut = document.getElementById('radar-zoom-out');
        if (zoomIn) zoomIn.addEventListener('click', () => this._zoomIn());
        if (zoomOut) zoomOut.addEventListener('click', () => this._zoomOut());
    }

    show() {
        this._needsRedraw = true;
        setTimeout(() => this._update(), 50);
    }

    _getOwnPosition() {
        if (window.ownPosition) return window.ownPosition;
        if (typeof map !== 'undefined' && map.getCenter) {
            const c = map.getCenter();
            return { lat: c.lat, lon: c.lng };
        }
        return null;
    }

    _getCanvasSize() {
        const scope = this.canvas?.parentElement;
        if (!scope) return 0;
        return Math.min(scope.clientWidth, scope.clientHeight);
    }

    _drawGrid() {
        const canvas = this.canvas;
        if (!canvas) return;

        const size = this._getCanvasSize();
        if (size < 10) return;

        const dpr = window.devicePixelRatio || 1;

        canvas.style.width = size + 'px';
        canvas.style.height = size + 'px';
        canvas.width = size * dpr;
        canvas.height = size * dpr;

        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, size, size);

        const cx = size / 2;
        const cy = size / 2;
        const maxR = size / 2 - 8;

        for (const dist of this.rings) {
            const r = (dist / this.maxRange) * maxR;
            ctx.beginPath();
            ctx.arc(cx, cy, r, 0, Math.PI * 2);
            ctx.strokeStyle = 'rgba(255,255,255,0.08)';
            ctx.lineWidth = 1;
            ctx.stroke();

            ctx.fillStyle = 'rgba(255,255,255,0.15)';
            ctx.font = '10px monospace';
            ctx.textAlign = 'center';
            ctx.fillText(dist + 'nm', cx, cy - r + 14);
        }

        ctx.strokeStyle = 'rgba(255,255,255,0.04)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(cx, 0);
        ctx.lineTo(cx, size);
        ctx.moveTo(0, cy);
        ctx.lineTo(size, cy);
        ctx.stroke();

        ctx.beginPath();
        ctx.arc(cx, cy, 3, 0, Math.PI * 2);
        ctx.fillStyle = '#00E5FF';
        ctx.fill();

        ctx.beginPath();
        ctx.arc(cx, cy, 8, 0, Math.PI * 2);
        const glow = ctx.createRadialGradient(cx, cy, 2, cx, cy, 8);
        glow.addColorStop(0, 'rgba(0,229,255,0.4)');
        glow.addColorStop(1, 'rgba(0,229,255,0)');
        ctx.fillStyle = glow;
        ctx.fill();

        ctx.fillStyle = '#00E5FF';
        ctx.font = '10px monospace';
        ctx.textAlign = 'center';
        ctx.fillText('OWN SHIP', cx, cy + 18);
    }

    _syncOverlay() {
        if (!this.canvas || !this.vesselLayer) return;
        const style = this.vesselLayer.style;
        style.width = this.canvas.style.width;
        style.height = this.canvas.style.height;
        style.left = this.canvas.offsetLeft + 'px';
        style.top = this.canvas.offsetTop + 'px';
    }

    _update() {
        const radarView = document.getElementById('radar-view');
        if (!radarView || radarView.style.display === 'none') return;

        const own = this._getOwnPosition();
        const allVessels = this.store.getAll();
        const countEl = document.getElementById('radar-count');

        if (!own) {
            this._drawGrid();
            this._syncOverlay();
            const sz = this._getCanvasSize();
            if (countEl) countEl.textContent = `No own position · ${allVessels.length} in store · scope ${sz}px`;
            return;
        }

        const withDist = this.store.getAll()
            .filter(v => v.mmsi !== window.OWN_MMSI && v.lat != null && v.lon != null)
            .map(v => ({ ...v, _dist: haversineNm(own.lat, own.lon, v.lat, v.lon) }))
            .sort((a, b) => a._dist - b._dist);

        const vessels = withDist.filter(v => v._dist <= this.maxRange);

        this._drawGrid();
        this._drawTrails(own, vessels);
        this._syncOverlay();
        this._positionLabels(own, vessels);

        if (countEl) {
            const pos = `${own.lat.toFixed(4)},${own.lon.toFixed(4)}`;
            countEl.textContent = `${vessels.length}/${withDist.length} targets · ${this.maxRange}nm · ${pos}`;
        }
    }

    _drawTrails(own, vessels) {
        const canvas = this.canvas;
        if (!canvas) return;

        const size = this._getCanvasSize();
        if (size < 10) return;
        const cx = size / 2;
        const cy = size / 2;
        const maxR = size / 2 - 8;

        const ctx = canvas.getContext('2d');
        const dpr = window.devicePixelRatio || 1;
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

        for (const v of vessels) {
            const track = this.store.getTrack(v.mmsi, 0.25);
            if (track.length < 2) continue;

            for (let i = 1; i < track.length; i++) {
                const prev = track[i - 1];
                const curr = track[i];

                const d1 = haversineNm(own.lat, own.lon, prev.lat, prev.lon);
                const d2 = haversineNm(own.lat, own.lon, curr.lat, curr.lon);

                if (d1 > this.maxRange || d2 > this.maxRange) continue;

                const b1 = bearingTo(own.lat, own.lon, prev.lat, prev.lon);
                const b2 = bearingTo(own.lat, own.lon, curr.lat, curr.lon);

                const rad1 = (b1 - 90) * Math.PI / 180;
                const r1 = (d1 / this.maxRange) * maxR;
                const x1 = cx + Math.cos(rad1) * r1;
                const y1 = cy + Math.sin(rad1) * r1;

                const rad2 = (b2 - 90) * Math.PI / 180;
                const r2 = (d2 / this.maxRange) * maxR;
                const x2 = cx + Math.cos(rad2) * r2;
                const y2 = cy + Math.sin(rad2) * r2;

                ctx.beginPath();
                ctx.moveTo(x1, y1);
                ctx.lineTo(x2, y2);
                ctx.strokeStyle = this._speedColor(curr.sog);
                ctx.globalAlpha = 0.08 + 0.25 * (i / track.length);
                ctx.lineWidth = 1;
                ctx.lineCap = 'round';
                ctx.stroke();
                ctx.globalAlpha = 1;
            }
        }
    }

    _positionLabels(own, vessels) {
        if (!this.canvas || !this.vesselLayer) return;

        const size = parseInt(this.canvas.style.width);
        if (!size) return;
        const cx = size / 2;
        const cy = size / 2;
        const maxR = size / 2 - 8;

        const seen = new Set();

        for (const v of vessels) {
            seen.add(v.mmsi);
            const dist = haversineNm(own.lat, own.lon, v.lat, v.lon);
            const brg = bearingTo(own.lat, own.lon, v.lat, v.lon);
            const rad = (brg - 90) * Math.PI / 180;
            const r = (dist / this.maxRange) * maxR;
            const x = cx + Math.cos(rad) * r;
            const y = cy + Math.sin(rad) * r;

            let el = this._labels.get(v.mmsi);
            if (!el) {
                el = document.createElement('div');
                el.className = 'radar-vessel';
                el.innerHTML = `
                    <svg class="radar-vessel-icon" viewBox="0 0 16 16" width="16" height="16">
                        <path d="M8 2 L12 14 L8 11 L4 14 Z" fill="currentColor"/>
                    </svg>
                    <span class="radar-vessel-name"></span>
                    <span class="radar-vessel-speed"></span>
                `;
                this.vesselLayer.appendChild(el);
                this._labels.set(v.mmsi, el);
            }

            el.style.left = x + 'px';
            el.style.top = y + 'px';

            const icon = el.querySelector('.radar-vessel-icon');
            const color = this._speedColor(v.sog || 0);
            icon.style.color = color;
            icon.style.transform = `rotate(${(v.cog || v.heading || 0)}deg)`;

            el.querySelector('.radar-vessel-name').textContent = v.name || 'MMSI ' + v.mmsi;
            const speedEl = el.querySelector('.radar-vessel-speed');
            speedEl.textContent = (v.sog != null ? v.sog.toFixed(1) : '?') + 'kts';
            speedEl.style.color = color;
        }

        for (const [mmsi, el] of this._labels) {
            if (!seen.has(mmsi)) {
                el.remove();
                this._labels.delete(mmsi);
            }
        }
    }

    _speedColor(sog) {
        if (sog > 10) return '#ef4444';
        if (sog > 8.5) return '#fbbf24';
        if (sog > 7) return '#4ade80';
        return '#3b82f6';
    }

    destroy() {
        if (this._interval) clearInterval(this._interval);
        if (this._resizeBound) window.removeEventListener('resize', this._resizeBound);
    }
}
