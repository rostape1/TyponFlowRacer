/**
 * Flowracer sailing dashboard: instrument panel with sparklines.
 */

class SailingCharts {
    constructor(store) {
        this.store = store;
        this._sparkInterval = null;
        this._gaugeEls = {};
        this._sparkCanvases = {};
    }

    init() {
        this._bindGauges();
        this._startUpdates();
    }

    _bindGauges() {
        const ids = ['hdg', 'sog', 'bsp', 'twd', 'tws', 'aws', 'awa', 'depth', 'twa'];
        for (const id of ids) {
            this._gaugeEls[id] = document.getElementById(`gauge-${id}`);
        }
        this._gaugeEls['awa-side'] = document.getElementById('gauge-awa-side');

        const sparkIds = ['sog', 'bsp', 'tws', 'aws', 'awa'];
        for (const id of sparkIds) {
            const canvas = document.getElementById(`spark-${id}`);
            if (canvas) this._sparkCanvases[id] = canvas;
        }
        const shiftCanvas = document.getElementById('spark-twd-shift');
        if (shiftCanvas) this._sparkCanvases['twd-shift'] = shiftCanvas;
    }

    _startUpdates() {
        this.store.addEventListener('update', () => this._updateGauges());
        this._sparkInterval = setInterval(() => this._updateSparklines(), 500);
    }

    _updateGauges() {
        const s = this.store.state;

        this._setGauge('hdg', s.heading, 0, v => Math.round(v).toString().padStart(3, '0'));
        this._setGauge('sog', s.sog, 1);
        this._setGauge('bsp', s.bsp, 1);
        this._setGauge('twd', s.twd, 0, v => Math.round(v).toString());
        this._setGauge('tws', s.tws, 1);
        this._setGauge('aws', s.aws, 1);
        this._setGauge('depth', s.depth, 1, v => (v * 3.28084).toFixed(1));

        const awaEl = this._gaugeEls['awa'];
        const awaSideEl = this._gaugeEls['awa-side'];
        if (awaEl) {
            if (s.awa === null || s.awa === undefined) {
                awaEl.textContent = '---';
                if (awaSideEl) awaSideEl.textContent = '°';
            } else {
                awaEl.textContent = Math.abs(Math.round(s.awa));
                if (awaSideEl) awaSideEl.textContent = s.awa >= 0 ? 'R' : 'L';
            }
        }

        const twaEl = this._gaugeEls['twa'];
        if (twaEl) {
            if (s.twa === null || s.twa === undefined) {
                twaEl.textContent = '---';
                twaEl.className = 'fr-small-num';
            } else {
                const abs = Math.abs(s.twa > 180 ? 360 - s.twa : s.twa);
                twaEl.textContent = Math.round(abs);
                if (abs < 15) twaEl.className = 'fr-small-num twa-irons';
                else if (abs < 30) twaEl.className = 'fr-small-num twa-close';
                else if (abs <= 50) twaEl.className = 'fr-small-num twa-optimal';
                else twaEl.className = 'fr-small-num';
            }
        }
    }

    _setGauge(id, val, decimals, formatter) {
        const el = this._gaugeEls[id];
        if (!el) return;
        if (val === null || val === undefined) {
            el.textContent = '---';
            return;
        }
        el.textContent = formatter ? formatter(val) : val.toFixed(decimals);
    }

    _updateSparklines() {
        const chartsView = document.getElementById('charts-view');
        if (chartsView && chartsView.style.display === 'none') return;

        const sparkFields = { sog: 'sog', bsp: 'bsp', tws: 'tws', aws: 'aws', awa: 'awa' };
        const sparkColors = {
            sog: '#00E5FF', bsp: '#3B82F6', tws: '#00E5FF',
            aws: '#FACC15', awa: 'rgba(255,255,255,0.5)'
        };

        for (const [id, field] of Object.entries(sparkFields)) {
            const canvas = this._sparkCanvases[id];
            if (!canvas) continue;
            const history = this.store.getHistory(field, 5 * 60 * 1000);
            this._drawSparkline(canvas, history, sparkColors[id]);
        }

        this._drawShiftChart();
    }

    _drawSparkline(canvas, history, color) {
        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        const w = rect.width;
        const h = rect.height;

        if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
            canvas.width = w * dpr;
            canvas.height = h * dpr;
            canvas.style.width = w + 'px';
            canvas.style.height = h + 'px';
        }

        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);

        if (history.length < 2) return;

        const values = history.map(p => p.v);
        let min = Math.min(...values);
        let max = Math.max(...values);
        if (max - min < 0.5) { min -= 0.5; max += 0.5; }

        const padY = 4;
        const plotH = h - padY * 2;

        ctx.beginPath();
        for (let i = 0; i < history.length; i++) {
            const x = (i / (history.length - 1)) * w;
            const y = padY + plotH - ((values[i] - min) / (max - min)) * plotH;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.lineJoin = 'round';
        ctx.stroke();

        ctx.lineTo(w, h);
        ctx.lineTo(0, h);
        ctx.closePath();

        const fillGrad = ctx.createLinearGradient(0, 0, 0, h);
        fillGrad.addColorStop(0, this._hexToRgba(color, 0.45));
        fillGrad.addColorStop(0.4, this._hexToRgba(color, 0.2));
        fillGrad.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = fillGrad;
        ctx.fill();
    }

    _drawShiftChart() {
        const canvas = this._sparkCanvases['twd-shift'];
        if (!canvas) return;

        const history = this.store.getHistory('twd', 5 * 60 * 1000);
        if (history.length < 3) return;

        const values = history.map(p => p.v);
        const mean = values.reduce((a, b) => a + b, 0) / values.length;
        const shifts = values.map(v => {
            let diff = v - mean;
            if (diff > 180) diff -= 360;
            if (diff < -180) diff += 360;
            return diff;
        });

        const dpr = window.devicePixelRatio || 1;
        const rect = canvas.getBoundingClientRect();
        const w = rect.width;
        const h = rect.height;

        if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
            canvas.width = w * dpr;
            canvas.height = h * dpr;
            canvas.style.width = w + 'px';
            canvas.style.height = h + 'px';
        }

        const ctx = canvas.getContext('2d');
        ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
        ctx.clearRect(0, 0, w, h);

        const cy = h / 2;
        ctx.strokeStyle = 'rgba(255,255,255,0.08)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(0, cy);
        ctx.lineTo(w, cy);
        ctx.stroke();

        const range = 15;
        const padX = 2;

        ctx.beginPath();
        for (let i = 0; i < shifts.length; i++) {
            const x = padX + (i / (shifts.length - 1)) * (w - padX * 2);
            const clamped = Math.max(-range, Math.min(range, shifts[i]));
            const y = cy - (clamped / range) * (cy - 4);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = '#FF4444';
        ctx.lineWidth = 1.5;
        ctx.lineJoin = 'round';
        ctx.stroke();

        const lastIdx = shifts.length - 1;
        const lastX = padX + (lastIdx / (shifts.length - 1)) * (w - padX * 2);
        ctx.lineTo(lastX, cy);
        ctx.lineTo(padX, cy);
        ctx.closePath();
        ctx.fillStyle = 'rgba(255, 68, 68, 0.08)';
        ctx.fill();
    }

    _hexToRgba(color, alpha) {
        if (color.startsWith('rgba') || color.startsWith('rgb')) {
            return color.replace(/[\d.]+\)$/, alpha + ')').replace('rgb(', 'rgba(');
        }
        const hex = color.replace('#', '');
        const r = parseInt(hex.substring(0, 2), 16);
        const g = parseInt(hex.substring(2, 4), 16);
        const b = parseInt(hex.substring(4, 6), 16);
        return `rgba(${r},${g},${b},${alpha})`;
    }

    destroy() {
        if (this._sparkInterval) clearInterval(this._sparkInterval);
    }
}
