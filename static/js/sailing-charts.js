/**
 * Sailing dashboard: instrument panel + Chart.js time-series.
 */

class SailingCharts {
    constructor(store) {
        this.store = store;
        this.chart = null;
        this._windowMs = 30 * 60 * 1000;
        this._updateInterval = null;
        this._gaugeEls = {};

        this._gauges = [
            { id: 'sog', label: 'SOG', unit: 'kn', field: 'sog', decimals: 1 },
            { id: 'bsp', label: 'BSP', unit: 'kn', field: 'bsp', decimals: 1 },
            { id: 'hdg', label: 'HDG', unit: '°', field: 'heading', decimals: 0 },
            { id: 'depth', label: 'DEPTH', unit: 'ft', field: 'depth', decimals: 1, multiply: 3.28084 },
            { id: 'awa', label: 'AWA', unit: '°', field: 'awa', decimals: 0 },
            { id: 'twa', label: 'TWA', unit: '°', field: 'twa', decimals: 0 },
            { id: 'twd', label: 'TWD', unit: '°', field: 'twd', decimals: 0 },
            { id: 'tws', label: 'TWS', unit: 'kn', field: 'tws', decimals: 1 },
        ];
    }

    init() {
        this._buildInstrumentPanel();
        this._buildChart();
        this._bindEvents();
        this._startUpdates();
    }

    _buildInstrumentPanel() {
        const panel = document.getElementById('instrument-panel');
        if (!panel) return;

        for (const g of this._gauges) {
            const div = document.createElement('div');
            div.className = 'instrument-gauge';
            div.innerHTML = `
                <div class="gauge-label">${g.label}</div>
                <div class="gauge-value" id="gauge-${g.id}">---</div>
                <div class="gauge-unit">${g.unit}</div>
            `;
            panel.appendChild(div);
            this._gaugeEls[g.id] = document.getElementById(`gauge-${g.id}`);
        }
    }

    _buildChart() {
        const canvas = document.getElementById('sailing-chart');
        if (!canvas || typeof Chart === 'undefined') return;

        const ctx = canvas.getContext('2d');
        this.chart = new Chart(ctx, {
            type: 'line',
            data: {
                datasets: [
                    { label: 'TWA', borderColor: '#3498db', backgroundColor: 'rgba(52,152,219,0.1)',
                      yAxisID: 'y', borderWidth: 1.5, pointRadius: 0, tension: 0.2, data: [] },
                    { label: 'TWD', borderColor: '#2ecc71', backgroundColor: 'rgba(46,204,113,0.1)',
                      yAxisID: 'y', borderWidth: 1.5, pointRadius: 0, tension: 0.2, data: [] },
                    { label: 'TWS', borderColor: '#9b59b6', backgroundColor: 'rgba(155,89,182,0.1)',
                      yAxisID: 'y1', borderWidth: 1.5, pointRadius: 0, tension: 0.2, data: [] },
                    { label: 'BSP', borderColor: '#f39c12', backgroundColor: 'rgba(243,156,18,0.1)',
                      yAxisID: 'y1', borderWidth: 1.5, pointRadius: 0, tension: 0.2, data: [] },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        labels: { color: '#8395a7', font: { size: 11 }, boxWidth: 12, padding: 8 },
                    },
                },
                scales: {
                    x: {
                        type: 'time',
                        time: { unit: 'minute', displayFormats: { minute: 'HH:mm' } },
                        grid: { color: 'rgba(100,150,200,0.1)' },
                        ticks: { color: '#8395a7', maxTicksLimit: 10 },
                    },
                    y: {
                        position: 'left',
                        title: { display: true, text: 'Degrees', color: '#8395a7' },
                        grid: { color: 'rgba(100,150,200,0.1)' },
                        ticks: { color: '#8395a7' },
                    },
                    y1: {
                        position: 'right',
                        title: { display: true, text: 'Knots', color: '#8395a7' },
                        grid: { drawOnChartArea: false },
                        ticks: { color: '#8395a7' },
                    },
                },
            },
        });
    }

    _bindEvents() {
        document.querySelectorAll('.chart-window-btn').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.chart-window-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                const mins = parseInt(btn.dataset.minutes);
                this._windowMs = mins === 0 ? 0 : mins * 60 * 1000;
                this._updateChart();
            });
        });
    }

    _startUpdates() {
        this.store.addEventListener('update', () => this._updateGauges());
        this._updateInterval = setInterval(() => this._updateChart(), 1000);
    }

    _updateGauges() {
        const s = this.store.state;
        for (const g of this._gauges) {
            const el = this._gaugeEls[g.id];
            if (!el) continue;
            let val = s[g.field];
            if (val === null || val === undefined) {
                el.textContent = '---';
                el.className = 'gauge-value';
                continue;
            }
            if (g.multiply) val *= g.multiply;
            el.textContent = val.toFixed(g.decimals);

            if (g.id === 'twa') {
                const abs = Math.abs(val > 180 ? 360 - val : val);
                if (abs < 15) el.className = 'gauge-value twa-irons';
                else if (abs < 30) el.className = 'gauge-value twa-close';
                else if (abs <= 50) el.className = 'gauge-value twa-optimal';
                else el.className = 'gauge-value';
            }
        }
    }

    _updateChart() {
        if (!this.chart) return;
        const chartsView = document.getElementById('charts-view');
        if (chartsView && chartsView.style.display === 'none') return;

        const fields = ['twa', 'twd', 'tws', 'bsp'];
        const windowMs = this._windowMs || 0;

        for (let i = 0; i < fields.length; i++) {
            const history = this.store.getHistory(fields[i], windowMs);
            this.chart.data.datasets[i].data = history.map(p => ({ x: p.t, y: p.v }));
        }

        this.chart.update('none');
    }

    destroy() {
        if (this._updateInterval) clearInterval(this._updateInterval);
        if (this.chart) this.chart.destroy();
    }
}
