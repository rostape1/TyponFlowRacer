/**
 * Helm tab: the race steering display. docs/helm.md
 *
 * Draws what HelmLogic decides: a banner instruction, a half-dial of corrected AWA against the
 * target, a speed bar against the ORC target, two tiles and a footer. All numbers are corrected
 * (helm-logic.js); the B&G's raw readings are never shown.
 *
 * The engine runs on every store update even while the tab is hidden, so smoothing and tack
 * detection are warm when it is opened; drawing happens only while visible. A 1 s timer re-checks
 * so a feed that goes silent turns the banner grey instead of freezing the last numbers.
 */
class HelmView {
    constructor(store, client) {
        this.store = store;
        this.client = client;
        this.engine = HelmLogic.create();
        this.visible = false;
        this.vm = null;
        this.mode = null;          // the dial scale currently drawn: 'up' | 'down'
    }

    init() {
        this.root = document.getElementById('helm-view');
        if (!this.root) return;
        this._build();
        this.store.addEventListener('update', () => this._tick());
        setInterval(() => this._tick(), 1000);
        this._tick();
    }

    show() { this.visible = true; this._draw(); }
    hide() { this.visible = false; }

    // stream time: log time in replay (so a paused replay is not "stale"), wall time live
    _now() {
        return this.client && this.client.isReplay ? this.store.state.lastUpdate : Date.now();
    }

    _tick() {
        this.vm = this.engine.update(this.store.state, this._now());
        if (this.visible) this._draw();
    }

    // ------------------------------------------------------------ DOM
    _el(tag, cls, parent, text) {
        const e = document.createElement(tag);
        if (cls) e.className = cls;
        if (text !== undefined) e.textContent = text;
        if (parent) parent.appendChild(e);
        return e;
    }

    _build() {
        const r = this.root;
        this.banner = this._el('div', 'helm-banner', r);
        this.bMain = this._el('span', 'helm-banner-main', this.banner, '—');
        this.bSub = this._el('span', 'helm-banner-sub', this.banner, '');
        const dial = this._el('div', 'helm-dial', r);
        this.svg = document.createElementNS(HelmView.NS, 'svg');
        this.svg.setAttribute('viewBox', '-16 70 432 256');
        this.svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
        dial.appendChild(this.svg);
        this.gScale = this._svg('g', {}, this.svg);
        this.gMarks = this._svg('g', {}, this.svg);
        this.gSpeed = this._svg('g', {}, this.svg);
        const tiles = this._el('div', 'helm-tiles', r);
        const tile = (name) => {
            const t = this._el('div', 'helm-tile', tiles);
            this._el('div', 'helm-tile-k', t, name);
            return { v: this._el('div', 'helm-tile-v', t, '—'), t: this._el('div', 'helm-tile-t', t, '') };
        };
        this.tAwa = tile('AWA (corr.)');
        this.tBsp = tile('BSP (corr.)');
        this.foot = this._el('div', 'helm-foot', r, '');
    }

    _svg(tag, attrs, parent) {
        const e = document.createElementNS(HelmView.NS, tag);
        for (const k in attrs) e.setAttribute(k, attrs[k]);
        if (parent) parent.appendChild(e);
        return e;
    }

    // ------------------------------------------------------------ dial geometry
    _pt(side, a, r) {
        const M = HelmView.MODES[this.mode], C = HelmView.C;
        const t = side * M.off(a) * (90 / (M.hi - M.lo)) * Math.PI / 180;
        return [C.x + r * Math.sin(t), C.y - r * Math.cos(t)];
    }

    _drawScale() {
        const g = this.gScale, M = HelmView.MODES[this.mode], C = HelmView.C;
        HelmView.clear(g);
        const edge = this.mode === 'up' ? M.hi : M.lo;
        const [x0, y0] = this._pt(-1, edge, C.r), [x1, y1] = this._pt(1, edge, C.r);
        this._svg('path', { d: `M${x0},${y0} A${C.r},${C.r} 0 0 1 ${x1},${y1}`, fill: 'none', stroke: '#56627c', 'stroke-width': 3 }, g);
        this._svg('line', { x1: 20, y1: C.y, x2: 380, y2: C.y, stroke: '#3b4660', 'stroke-width': 2 }, g);
        for (const side of [-1, 1]) for (let a = M.lo; a <= M.hi; a++) {
            const top = M.off(a) === 0;
            if (top && side < 0) continue;
            const big = a % 5 === 0;
            const [ax, ay] = this._pt(side, a, C.r - (big ? 16 : 7)), [bx, by] = this._pt(side, a, C.r);
            this._svg('line', { x1: ax, y1: ay, x2: bx, y2: by, stroke: top ? '#ef5f5a' : '#9aa6bd', 'stroke-width': big ? 3 : 1 }, g);
        }
        const text = (x, y, s, attrs) => { this._svg('text', { x, y, ...attrs }, g).textContent = s; };
        text(200, C.y - C.r - 10, M.top, { 'text-anchor': 'middle', fill: '#ef5f5a', 'font-size': 13, 'font-weight': 700 });
        text(22, C.y + 17, 'PORT', { fill: '#ef5f5a', 'font-size': 15, 'font-weight': 700 });
        text(378, C.y + 17, 'STBD', { fill: '#35d49a', 'font-size': 15, 'font-weight': 700, 'text-anchor': 'end' });
        text(200, C.y + 17, M.name, { 'text-anchor': 'middle', fill: '#8a97ad', 'font-size': 14, 'font-weight': 700, 'letter-spacing': 1 });
    }

    // ------------------------------------------------------------ render
    _draw() {
        const vm = this.vm;
        if (!vm) return;
        const mode = vm.ok ? vm.mode : (this.mode || 'up');
        if (mode !== this.mode) { this.mode = mode; this._drawScale(); }
        const col = HelmView.COLORS[vm.kind];
        this.banner.style.background = col;
        this.banner.style.color = vm.ok ? '#0c111b' : '#e8edf5';
        this.bMain.textContent = vm.main;
        this.bSub.textContent = vm.sub;
        HelmView.clear(this.gMarks);
        HelmView.clear(this.gSpeed);
        this._drawLabels(vm);
        if (!vm.ok) {
            // fail visibly: no marker, no numbers, never the last good value
            for (const t of [this.tAwa, this.tBsp]) { t.v.textContent = '—'; t.t.textContent = ''; }
            this.foot.textContent = vm.sub;
            return;
        }
        this._drawTarget(vm, col);
        this._drawSpeed(vm);

        const word = vm.up ? (vm.dA > 0 ? 'wide' : 'high') : (vm.dA > 0 ? 'deep' : 'hot');
        this.tAwa.v.textContent = `${Math.round(vm.awa)}°`;
        HelmView.clear(this.tAwa.t);
        this.tAwa.t.append(vm.reach ? 'reach · no angle target' : `target ${Math.round(vm.tAwa)}°`);
        if (!vm.reach && Math.abs(vm.dA) >= 0.5) {
            const b = this._el('b', null, this.tAwa.t, ` · ${Math.round(Math.abs(vm.dA))}° ${word}`);
            b.style.color = col;
        }
        this.tBsp.v.textContent = vm.stw.toFixed(1);
        HelmView.clear(this.tBsp.t);
        this.tBsp.t.append(`${vm.reach ? 'polar' : 'target'} ${vm.tSpeed.toFixed(1)} · `);
        const d = this._el('b', null, this.tBsp.t, `${vm.dS >= 0 ? '+' : ''}${vm.dS.toFixed(1)}`);
        d.style.color = vm.kind === 'build' ? HelmView.COLORS.build : HelmView.COLORS.groove;

        const parts = [`TWS ${vm.tws10.toFixed(1)} kn`, `TWA ${Math.round(vm.twa)}°`,
            vm.reach ? `${Math.round(vm.polarPct)}% of polar` : `VMG ${Math.round(vm.vmgPct)}% of ORC`];
        if (vm.sinceTack < HelmLogic.C.TACK_S) parts.push(`tacked ${Math.round(vm.sinceTack)} s ago`);
        if (vm.aboveRange) parts.push('above ORC range');
        this.foot.textContent = parts.join(' · ');
    }

    _drawLabels(vm) {
        // scale numbers, except the one under the target pill
        const M = HelmView.MODES[this.mode], g = this.gMarks, z = 90 / (M.hi - M.lo);
        for (const sd of [-1, 1]) for (let a = M.lo; a <= M.hi; a += M.step) {
            if (M.off(a) === 0 || a === M.lo) continue;
            if (vm.ok && vm.tAwa != null && sd === vm.side && Math.abs(a - vm.tAwa) * z < 14) continue;
            const [lx, ly] = this._pt(sd, a, HelmView.C.r - 32);
            this._svg('text', { x: lx, y: ly + 6, 'text-anchor': 'middle', fill: '#aab5c9', 'font-size': 15 }, g).textContent = a;
        }
    }

    _drawTarget(vm, col) {
        const g = this.gMarks, C = HelmView.C, M = HelmView.MODES[this.mode], z = 90 / (M.hi - M.lo);
        const side = vm.side, band = vm.up ? HelmLogic.C.GROOVE_UP : HelmLogic.C.HOT_DOWN;
        const clampA = a => Math.max(M.lo - 1, Math.min(M.hi + 1, a));
        const aA = clampA(vm.awa);
        if (vm.reach) {
            // reaching: the mark sets the course, so only where we are, no target to steer to
            const [ax, ay] = this._pt(side, aA, C.r - 14), [bx, by] = this._pt(side, aA, C.r + 16);
            this._svg('line', { x1: ax, y1: ay, x2: bx, y2: by, stroke: col, 'stroke-width': 12, 'stroke-linecap': 'round' }, g);
            return;
        }
        const tA = clampA(vm.tAwa);
        // groove band
        const [g0x, g0y] = this._pt(side, tA - band, C.r + 4), [g1x, g1y] = this._pt(side, tA + band, C.r + 4);
        const cw = (vm.up ? 1 : -1) * side > 0 ? 1 : 0;
        this._svg('path', { d: `M${g0x},${g0y} A${C.r + 4},${C.r + 4} 0 0 ${cw} ${g1x},${g1y}`, fill: 'none', stroke: '#35d49a', 'stroke-width': 16, opacity: 0.6 }, g);
        // target line and triangle
        const [l0x, l0y] = this._pt(side, tA, C.r - 22), [l1x, l1y] = this._pt(side, tA, C.r + 12);
        this._svg('line', { x1: l0x, y1: l0y, x2: l1x, y2: l1y, stroke: '#ffffff', 'stroke-width': 4 }, g);
        const w = 5 / z;
        const [tx, ty] = this._pt(side, tA, C.r + 12), [lx, ly] = this._pt(side, tA - w, C.r + 38), [rx, ry] = this._pt(side, tA + w, C.r + 38);
        this._svg('polygon', { points: `${tx},${ty} ${lx},${ly} ${rx},${ry}`, fill: '#ffffff', stroke: '#0c111b', 'stroke-width': 2 }, g);
        // actual AWA bar, on the arc
        const [ax, ay] = this._pt(side, aA, C.r - 14), [bx, by] = this._pt(side, aA, C.r + 16);
        this._svg('line', { x1: ax, y1: ay, x2: bx, y2: by, stroke: col, 'stroke-width': 12, 'stroke-linecap': 'round' }, g);
        // target number pill, drawn last so it is never covered
        const [nx, ny] = this._pt(side, tA, C.r - 34), txt = `${Math.round(vm.tAwa)}°`, pw = 14 * txt.length + 10;
        this._svg('rect', { x: nx - pw / 2, y: ny - 16, width: pw, height: 32, rx: 16, fill: '#ffffff' }, g);
        this._svg('text', { x: nx, y: ny + 9, 'text-anchor': 'middle', fill: '#0c111b', 'font-size': 24, 'font-weight': 800 }, g).textContent = txt;
    }

    _drawSpeed(vm) {
        // speed bar: target +-1 kn, target line mid-height, dashed = the floor
        const g = this.gSpeed, X = 165, W = 70, TOP = 175, BOT = 295, H = BOT - TOP;
        const F = HelmLogic.C.FLOOR;
        const y = v => BOT - (Math.max(-1, Math.min(1, v)) + 1) / 2 * H;
        this._svg('rect', { x: X, y: TOP, width: W, height: H, rx: 8, fill: '#1a2233', stroke: '#56627c', 'stroke-width': 2 }, g);
        const fill = vm.kind === 'build' ? HelmView.COLORS.build : HelmView.COLORS.groove;
        this._svg('rect', { x: X + 4, y: y(vm.dS), width: W - 8, height: Math.max(4, BOT - y(vm.dS) - 3), rx: 5, fill, opacity: 0.9 }, g);
        this._svg('line', { x1: X - 12, y1: y(0), x2: X + W + 12, y2: y(0), stroke: '#e8edf5', 'stroke-width': 3 }, g);
        this._svg('line', { x1: X - 6, y1: y(-F), x2: X + W + 6, y2: y(-F), stroke: HelmView.COLORS.build, 'stroke-width': 2.5, 'stroke-dasharray': '5 4' }, g);
        const text = (x, yy, s, attrs) => { this._svg('text', { x, y: yy, ...attrs }, g).textContent = s; };
        text(X + W + 16, y(0) + 6, vm.tSpeed.toFixed(1), { fill: '#e8edf5', 'font-size': 17, 'font-weight': 600 });
        text(X - 16, y(vm.dS) + 6, vm.stw.toFixed(1), { fill: '#e8edf5', 'font-size': 17, 'font-weight': 600, 'text-anchor': 'end' });
        text(X + W + 16, y(-F) + 14, `min ${vm.floor.toFixed(1)}`, { fill: HelmView.COLORS.build, 'font-size': 13, 'font-weight': 700 });
    }
}

// Plain assignments, not `static` class fields: the tab must run on iPads stuck at iOS 12
// (static fields need Safari 14.5). docs/helm.md
// Each mode maps its AWA range onto the half circle; off(a) = degrees from the top of the dial.
HelmView.MODES = {
    up:   { lo: 0,  hi: 45,  step: 10, off: a => a,       top: 'WIND',          name: 'UPWIND' },
    down: { lo: 60, hi: 180, step: 20, off: a => 180 - a, top: 'DEAD DOWNWIND', name: 'DOWNWIND' },
};
HelmView.C = { x: 200, y: 300, r: 185 };
HelmView.NS = 'http://www.w3.org/2000/svg';
HelmView.COLORS = { spend: '#3fe0f5', build: '#ff4fd8', groove: '#35d49a', nodata: '#5d6880' };
HelmView.clear = el => { while (el.firstChild) el.removeChild(el.firstChild); };   // replaceChildren() needs iOS 14
