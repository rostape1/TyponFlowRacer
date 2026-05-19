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
            const history = this.store.getHistory(field, 10 * 60 * 1000);
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

        const windowMs = 10 * 60 * 1000;
        const axisH = 10;          // reserved for tick labels
        const plotBottom = h - axisH;

        this._drawTimeAxis(ctx, w, h, plotBottom, windowMs);

        if (history.length < 2) return;

        const values = history.map(p => p.v);
        let min = Math.min(...values);
        let max = Math.max(...values);
        if (max - min < 0.5) { min -= 0.5; max += 0.5; }

        const padY = 4;
        const plotH = plotBottom - padY * 2;
        const yFor = v => padY + plotH - ((v - min) / (max - min)) * plotH;

        this._drawValueGrid(ctx, w, padY, plotH, min, max, yFor);

        const now = Date.now();
        const xFor = t => w - ((now - t) / windowMs) * w;

        ctx.beginPath();
        for (let i = 0; i < history.length; i++) {
            const x = xFor(history[i].t);
            const y = yFor(values[i]);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.5;
        ctx.lineJoin = 'round';
        ctx.stroke();

        ctx.lineTo(xFor(history[history.length - 1].t), plotBottom);
        ctx.lineTo(xFor(history[0].t), plotBottom);
        ctx.closePath();

        const fillGrad = ctx.createLinearGradient(0, 0, 0, plotBottom);
        fillGrad.addColorStop(0, this._hexToRgba(color, 0.45));
        fillGrad.addColorStop(0.4, this._hexToRgba(color, 0.2));
        fillGrad.addColorStop(1, 'rgba(0,0,0,0)');
        ctx.fillStyle = fillGrad;
        ctx.fill();
    }

    /** Vertical tick + label at every minute boundary across the time window. */
    _drawTimeAxis(ctx, w, h, plotBottom, windowMs) {
        const minutes = Math.round(windowMs / 60000);
        ctx.save();
        ctx.font = '8px -apple-system, system-ui, sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,0.45)';
        ctx.strokeStyle = 'rgba(255,255,255,0.10)';
        ctx.lineWidth = 1;
        ctx.textBaseline = 'top';
        for (let m = 0; m <= minutes; m++) {
            const x = w - (m / minutes) * w;
            ctx.beginPath();
            ctx.moveTo(x, plotBottom);
            ctx.lineTo(x, plotBottom + 2);
            ctx.stroke();
            const label = m === 0 ? 'now' : `-${m}m`;
            const tw = ctx.measureText(label).width;
            // Clamp labels so they don't clip past the canvas edges.
            let tx = x - tw / 2;
            if (tx < 0) tx = 0;
            else if (tx + tw > w) tx = w - tw;
            ctx.fillText(label, tx, plotBottom + 2);
        }
        ctx.restore();
    }

    /** Horizontal reference lines + small left-edge labels (kt for speed sparklines). */
    _drawValueGrid(ctx, w, padY, plotH, min, max, yFor) {
        const range = max - min;
        // Pick a "nice" step so we render 3-6 grid lines in the visible band.
        const step = range > 25 ? 10 : range > 10 ? 5 : range > 4 ? 2 : range > 1.5 ? 1 : 0.5;
        const first = Math.ceil(min / step) * step;
        ctx.save();
        ctx.font = '8px -apple-system, system-ui, sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,0.35)';
        ctx.strokeStyle = 'rgba(255,255,255,0.07)';
        ctx.lineWidth = 1;
        ctx.textBaseline = 'middle';
        for (let v = first; v <= max + 1e-9; v += step) {
            const y = yFor(v);
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(w, y);
            ctx.stroke();
            const label = step >= 1 ? Math.round(v).toString() : v.toFixed(1);
            ctx.fillText(label, 2, y - 1);
        }
        ctx.restore();
    }

    _drawShiftChart() {
        const canvas = this._sparkCanvases['twd-shift'];
        if (!canvas) return;

        const windowMs = 10 * 60 * 1000;
        const history = this.store.getHistory('twd', windowMs);

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

        const axisH = 10;
        const plotBottom = h - axisH;
        this._drawTimeAxis(ctx, w, h, plotBottom, windowMs);

        if (history.length < 3) return;

        const values = history.map(p => p.v);
        const mean = values.reduce((a, b) => a + b, 0) / values.length;
        const shifts = values.map(v => {
            let diff = v - mean;
            if (diff > 180) diff -= 360;
            if (diff < -180) diff += 360;
            return diff;
        });

        const cy = plotBottom / 2;
        const range = 15;
        const yForShift = d => cy - (d / range) * (cy - 4);
        // Reference lines at ±5° and ±10° (faint) plus center line.
        ctx.save();
        ctx.font = '8px -apple-system, system-ui, sans-serif';
        ctx.fillStyle = 'rgba(255,255,255,0.35)';
        ctx.lineWidth = 1;
        for (const d of [-10, -5, 5, 10]) {
            const y = yForShift(d);
            ctx.strokeStyle = 'rgba(255,255,255,0.06)';
            ctx.beginPath();
            ctx.moveTo(0, y);
            ctx.lineTo(w, y);
            ctx.stroke();
            ctx.fillText(`${d > 0 ? '+' : ''}${d}°`, 2, y - 1);
        }
        ctx.strokeStyle = 'rgba(255,255,255,0.10)';
        ctx.beginPath();
        ctx.moveTo(0, cy);
        ctx.lineTo(w, cy);
        ctx.stroke();
        ctx.restore();

        const padX = 2;
        const now = Date.now();
        const xFor = t => padX + ((windowMs - (now - t)) / windowMs) * (w - padX * 2);

        ctx.beginPath();
        for (let i = 0; i < shifts.length; i++) {
            const x = xFor(history[i].t);
            const clamped = Math.max(-range, Math.min(range, shifts[i]));
            const y = cy - (clamped / range) * (cy - 4);
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = '#FF4444';
        ctx.lineWidth = 1.5;
        ctx.lineJoin = 'round';
        ctx.stroke();

        const lastX = xFor(history[history.length - 1].t);
        const firstX = xFor(history[0].t);
        ctx.lineTo(lastX, cy);
        ctx.lineTo(firstX, cy);
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
