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
        this.bVmg = this._el('span', 'helm-banner-vmg', this.banner, '');   // how well it is paying
        const dial = this._el('div', 'helm-dial', r);
        this.svg = document.createElementNS(HelmView.NS, 'svg');
        this.svg.setAttribute('viewBox', '-16 70 432 318');
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
        for (const side of [-1, 1]) for (let a = M.lo; a <= M.hi; a += M.tick) {
            const top = M.off(a) === 0;
            if (top && side < 0) continue;
            const big = a % M.bigTick === 0;
            const [ax, ay] = this._pt(side, a, C.r - (big ? 16 : 7)), [bx, by] = this._pt(side, a, C.r);
            this._svg('line', { x1: ax, y1: ay, x2: bx, y2: by, stroke: top ? HelmView.COLORS.wind : '#9aa6bd', 'stroke-width': big ? 3 : 1 }, g);
        }
        const text = (x, y, s, attrs) => { this._svg('text', { x, y, ...attrs }, g).textContent = s; };
        text(200, C.y - C.r - 10, M.top, { 'text-anchor': 'middle', fill: HelmView.COLORS.wind, 'font-size': 15, 'font-weight': 700 });
        text(200, C.y + 17, M.name, { 'text-anchor': 'middle', fill: '#8a97ad', 'font-size': 14, 'font-weight': 700, 'letter-spacing': 1 });
    }

    // ------------------------------------------------------------ render
    _draw() {
        const vm = this.vm;
        if (!vm) return;
        const mode = HelmLogic.dialScale(this.mode, vm);
        if (mode !== this.mode) { this.mode = mode; this._drawScale(); }
        const col = HelmView.COLORS[vm.kind];
        this.banner.style.backgroundColor = col;   // fades: style.css .helm-banner
        this.banner.style.color = vm.ok ? '#0c111b' : '#e8edf5';
        this.bMain.textContent = vm.main;
        this.bSub.textContent = vm.sub;
        this.bVmg.textContent = !vm.ok ? '' : vm.reach ? `${Math.round(vm.polarPct)}% polar` : `${Math.round(vm.vmgPct)}% VMG`;
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
        this._drawDeltas(vm);

        // the tiles are the raw corrected readings; the differences are on the dial
        this.tAwa.v.textContent = `${Math.round(vm.awa)}°`;
        this.tAwa.t.textContent = vm.reach ? 'reach · no angle target' : `target ${Math.round(vm.tAwa)}°`;
        this.tBsp.v.textContent = vm.stw.toFixed(1);
        this.tBsp.t.textContent = `${vm.reach ? 'polar' : 'target'} ${vm.tSpeed.toFixed(1)}`;

        const parts = [`TWS ${vm.tws10.toFixed(1)} kn`, `TWA ${Math.round(vm.twa)}°`];   // VMG % is in the banner
        if (vm.sinceTack < HelmLogic.C.TACK_S) parts.push(`tacked ${Math.round(vm.sinceTack)} s ago`);
        if (vm.aboveRange) parts.push('above ORC range');
        this.foot.textContent = parts.join(' · ');
    }

    _drawLabels(vm) {
        // scale numbers outside the ring, so the inside is free for the two differences; none under the
        // target wedge. The tack label only on the side we are on.
        const M = HelmView.MODES[this.mode], g = this.gMarks, z = 90 / (M.hi - M.lo), C = HelmView.C;
        const near = (sd, a) => vm.ok && !vm.reach && sd === vm.side && Math.abs(a - vm.tAwa) * z < 10;
        for (const sd of [-1, 1]) for (let a = M.lo; a <= M.hi; a += M.step) {
            if (M.off(a) === 0 || a === M.lo || near(sd, a)) continue;
            const [lx, ly] = this._pt(sd, a, C.r + 20);
            this._svg('text', { x: lx, y: ly + 7, 'text-anchor': 'middle', fill: '#c8d3e6', 'font-size': 18, 'font-weight': 700 }, g).textContent = a;
        }
        if (!vm.ok) return;
        const port = vm.side < 0;
        this._svg('text', { x: port ? 22 : 378, y: C.y + 19, 'text-anchor': port ? 'start' : 'end',
            fill: HelmView.COLORS.text2, 'font-size': 19, 'font-weight': 800 }, g).textContent = port ? 'PORT' : 'STBD';
    }

    _drawTarget(vm, col) {
        // boat = solid wedge inside the ring, coloured like the banner; target = the same wedge, hollow
        // and white, outside it, tip to tip on the ring. Steer the solid tip under the hollow one.
        // Tall and narrow so each reads as pointing along its radius. docs/helm.md
        const g = this.gMarks, C = HelmView.C, M = HelmView.MODES[this.mode];
        const clampA = a => Math.max(M.lo - 1, Math.min(M.hi + 1, a));
        // sized in pixels, not dial degrees, so the outer wedge is not drawn wider than the inner one
        const wedge = (a, rTip, rBase) => {
            const [tx, ty] = this._pt(vm.side, a, rTip), [bx, by] = this._pt(vm.side, a, rBase);
            const len = Math.hypot(bx - tx, by - ty), px = -(by - ty) / len * 11, py = (bx - tx) / len * 11;
            return `${tx},${ty} ${bx + px},${by + py} ${bx - px},${by - py}`;
        };
        if (!vm.reach) {   // reaching: the mark sets the course, so no target to steer to
            const tA = clampA(vm.tAwa), band = vm.up ? HelmLogic.C.GROOVE_UP : HelmLogic.C.HOT_DOWN;
            // the groove: a wide band over the ring, drawn under both wedges; white like the target
            const [g0x, g0y] = this._pt(vm.side, tA - band, C.r), [g1x, g1y] = this._pt(vm.side, tA + band, C.r);
            const cw = (vm.up ? 1 : -1) * vm.side > 0 ? 1 : 0;
            this._svg('path', { d: `M${g0x},${g0y} A${C.r},${C.r} 0 0 ${cw} ${g1x},${g1y}`,
                fill: 'none', stroke: '#ffffff', 'stroke-width': 16, opacity: 0.35 }, g);
            this._svg('polygon', { points: wedge(tA, C.r + 2, C.r + 38),
                fill: 'none', stroke: '#ffffff', 'stroke-width': 3.5, 'stroke-linejoin': 'round' }, g);
        }
        this._svg('polygon', { points: wedge(clampA(vm.awa), C.r - 2, C.r - 38),
            fill: col, stroke: '#0c111b', 'stroke-width': 2, 'stroke-linejoin': 'round' }, g);
    }

    // the banner colour if the instruction is about this (`about` = 'spend' for the angle, 'build'
    // for speed; in the groove, both), otherwise the neutral colour given
    _accent(vm, about, neutral) {
        return vm.kind === 'groove' || vm.kind === about ? HelmView.COLORS[vm.kind] : neutral;
    }

    _drawDeltas(vm) {
        // the helmsman steers on the differences, so they are the big numbers; the raw values are in
        // the tiles. The angle sits under the dial, where no wedge can cover it; the speed under its bar.
        const g = this.gSpeed;
        if (!vm.reach) {
            const n = Math.round(Math.abs(vm.dA));
            const word = vm.up ? (vm.dA > 0 ? 'LOW' : 'HIGH') : (vm.dA > 0 ? 'DEEP' : 'HOT');   // the helm's pairs: high/low, deep/hot
            // in the groove the angle is green only inside the band: "7° HIGH" must never look approved
            const band = vm.up ? HelmLogic.C.GROOVE_UP : HelmLogic.C.HOT_DOWN;
            const off = vm.kind === 'groove' && Math.abs(vm.dA) > band;
            this._svg('text', { x: 200, y: 370, 'text-anchor': 'middle', fill: off ? '#ffffff' : this._accent(vm, 'spend', '#ffffff'), 'font-size': 40, 'font-weight': 800 }, g)
                .textContent = n === 0 ? 'ON ANGLE' : `${n}° ${word}`;
        }
        const sign = vm.dS >= 0 ? '+' : '−';
        this._svg('text', { x: 200, y: 288, 'text-anchor': 'middle', fill: this._accent(vm, 'build', '#ffffff'), 'font-size': 32, 'font-weight': 800 }, g)
            .textContent = `${sign}${Math.abs(vm.dS).toFixed(1)} kn`;
    }

    _drawSpeed(vm) {
        // speed bar: the boat speed itself, filling up from the bottom, zoomed to target +-1 kn so a
        // tenth is visible; clamps at the ends (the difference below says how far). Target line
        // mid-height, labelled right; the dashed speed floor (target - 0.3 kn, docs/polar.md §3)
        // labelled left, so the two labels never collide.
        const g = this.gSpeed, CX = 200, W = 56, TOP = 140, H = 110, COL = HelmView.COLORS;
        const y = d => TOP + H / 2 - Math.max(-1, Math.min(1, d)) * H / 2;   // d = kn from target
        this._svg('rect', { x: CX - W / 2, y: TOP, width: W, height: H, rx: 8, fill: '#1a2233', stroke: '#56627c', 'stroke-width': 2 }, g);
        const top = y(vm.dS);
        this._svg('rect', { x: CX - W / 2 + 4, y: top, width: W - 8, height: Math.max(3, TOP + H - 4 - top), rx: 4,
            fill: this._accent(vm, 'build', COL.text2), opacity: 0.9 }, g);
        const fy = y(-HelmLogic.C.FLOOR);
        this._svg('line', { x1: CX - W / 2 - 8, y1: fy, x2: CX + W / 2, y2: fy, stroke: COL.text2, 'stroke-width': 2.5, 'stroke-dasharray': '4 3' }, g);
        this._svg('line', { x1: CX - W / 2, y1: y(0), x2: CX + W / 2 + 10, y2: y(0), stroke: '#e8edf5', 'stroke-width': 3 }, g);
        this._svg('text', { x: CX - W / 2 - 12, y: fy + 5, 'text-anchor': 'end', fill: COL.text3, 'font-size': 15, 'font-weight': 600 }, g)
            .textContent = `floor ${vm.floor.toFixed(1)}`;   // short: a longer label reached the ticks
        this._svg('text', { x: CX + W / 2 + 14, y: y(0) + 5, 'text-anchor': 'start', fill: COL.text2, 'font-size': 15, 'font-weight': 600 }, g)
            .textContent = `${vm.reach ? 'polar' : 'target'} ${vm.tSpeed.toFixed(1)}`;
    }
}

// Plain assignments, not `static` class fields: the tab must run on iPads stuck at iOS 12
// (static fields need Safari 14.5). docs/helm.md
// Each mode maps its AWA range onto the half circle; off(a) = degrees from the top of the dial.
// Labels every `step`, ticks every `tick`, long ticks every `bigTick`: few enough to stay legible
// through spray and glare (a tick per degree or two was a grey comb).
HelmView.MODES = {
    up:   { lo: 0,  hi: 45,  step: 10, tick: 5, bigTick: 10, off: a => a,       top: 'WIND', name: 'UPWIND' },
    down: { lo: 60, hi: 180, step: 20, tick: 5, bigTick: 10, off: a => 180 - a, top: '180',  name: 'DOWNWIND' },
};
HelmView.C = { x: 200, y: 300, r: 185 };
HelmView.NS = 'http://www.w3.org/2000/svg';
// One accent at a time: the banner's colour (spend / build / groove), used only on what the instruction
// is about (_accent). Everything else is white or one of two greys. docs/helm.md
HelmView.COLORS = { spend: '#3fe0f5', build: '#ff4fd8', groove: '#35d49a', nodata: '#5d6880',
    wind: '#9aa6bd', text2: '#c8d3e6', text3: '#8a97ad' };
HelmView.clear = el => { while (el.firstChild) el.removeChild(el.firstChild); };   // replaceChildren() needs iOS 14
