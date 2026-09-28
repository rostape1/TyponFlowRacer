/**
 * Helm tab logic (static/js/helm-logic.js). Run: node tests/test_helm.mjs
 * No dependencies, no network. docs/helm.md
 *
 * 1. Parity with tools/build_polar.py: the calibration chain and the target angle exist twice (P20).
 *    tests/fixtures/helm_parity.json is written by `build_polar.py --write-js` from the Python code;
 *    the JS must reproduce it.
 * 2. The instruction rules, row by row and at their boundaries.
 * 3. The live engine: missing inputs fail visibly, the banner doesn't flicker, tacks and mode
 *    switches are detected, a replay seek starts clean.
 */
import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const targetsSrc = readFileSync(join(root, 'static/js/helm-targets.js'), 'utf8');
const logicSrc = readFileSync(join(root, 'static/js/helm-logic.js'), 'utf8');
const fixture = JSON.parse(readFileSync(join(root, 'tests/fixtures/helm_parity.json'), 'utf8'));

const box = {};
new Function('module', targetsSrc + '\n' + logicSrc + '\nmodule.HELM_TARGETS = HELM_TARGETS; module.HelmLogic = HelmLogic;')(box);
const { HelmLogic: L, HELM_TARGETS: T } = box;
const C = L.C;

let passed = 0, failed = 0;
function assert(cond, msg) {
    if (cond) passed++;
    else { failed++; console.error(`  FAIL: ${msg}`); }
}
const near = (a, b, tol) => Math.abs(a - b) <= tol;

// ---------------------------------------------------------------- 1. parity with Python
console.log('parity with build_polar.py');
{
    let worst = 0, bad = 0;
    for (const f of fixture.chain) {
        const j = L.chain(f.awa, f.aws, f.bsp, f.roll);
        const d = Math.max(Math.abs(j.stw - f.stw), Math.abs(j.awaCorr - f.awaCorr),
            Math.abs(L.signed(j.twa - f.twa)), Math.abs(j.tws10 - f.tws10), Math.abs(j.lee - f.lee));
        worst = Math.max(worst, d);
        if (!(d < 1e-4)) { bad++; if (bad <= 3) console.error('   ', f, j); }
    }
    assert(bad === 0, `calibration chain: ${bad} of ${fixture.chain.length} cases differ from Python (worst ${worst.toExponential(1)})`);
    assert(fixture.chain.length > 500, 'fixture has the full grid of chain cases');

    let tbad = 0;
    for (const f of fixture.target) if (!near(L.awaByBow(f.twa, f.tws10, f.stw, f.lee), f.awa, 1e-4)) tbad++;
    assert(tbad === 0, `target AWA: ${tbad} of ${fixture.target.length} cases differ from Python`);

    let pbad = 0;
    for (const f of fixture.polar) if (!near(L.polarSpeed(f.twa, f.tws10), f.bsp, 1e-4)) pbad++;
    assert(pbad === 0 && fixture.polar.length > 50, `polar speed: ${pbad} of ${fixture.polar.length} cases differ from Python`);
}

// the same sailing on either tack reads the same corrected AWA: the vane offset is really removed.
// Upright (no heel, leeway or paddlewheel term), the raw vane reads A + offset + upwash on starboard
// and -(A) + offset - upwash on port. Only where the upwash taper is off on both tacks: the taper is
// computed from the RAW reading in build_polar.py (offset included), so past ~22 deg it starts ~2.75
// deg earlier on starboard and the tacks differ by up to ~0.5 deg. That is the analysis' model, and
// parity above holds the JS to it.
{
    const up = L.upwashAt(12), off = T.cal.offset;
    let worst = 0;
    for (const A of [16, 19, 22]) {
        const s = L.chain(A + off + up, 16, 6.0, 0), p = L.chain(360 - A + off - up, 16, 6.0, 0);
        worst = Math.max(worst, Math.abs(s.awaCorr + p.awaCorr), Math.abs(Math.abs(s.twa) - Math.abs(p.twa)));
    }
    assert(worst < 0.1, `port and starboard read alike after correction (worst ${worst.toFixed(2)}°)`);
    const raw = L.chain(26 + off + up, 16, 6.0, 0);
    assert(Math.abs(raw.awaCorr - 26) < 0.5, 'starboard raw reading loses offset + upwash');
}

// ---------------------------------------------------------------- targets
console.log('targets');
{
    const t10 = L.targets(10, true);
    assert(near(t10.twa, 40.3, 1e-9) && near(t10.vmg, 4.72, 1e-9), 'upwind target at a certificate column');
    assert(near(t10.speed, 4.72 / Math.cos(40.3 * Math.PI / 180), 1e-9), 'upwind target speed = beat VMG / cos(beat angle)');
    const t11 = L.targets(11, true);
    assert(near(t11.twa, (40.3 + 39.3) / 2, 1e-9), 'interpolates between columns');
    const d12 = L.targets(12, false);
    assert(near(d12.twa, 156.5, 1e-9) && near(d12.speed, 6.36 / -Math.cos(156.5 * Math.PI / 180), 1e-9), 'downwind target');
    assert(near(L.targets(30, true).twa, 38.4, 1e-9) && near(L.targets(2, true).twa, 47.1, 1e-9), 'clamps outside 4-24 kn');
    // slower -> apparent wind moves aft -> the same true angle reads wider
    assert(L.awaByBow(40, 12, 5.5, 0) > L.awaByBow(40, 12, 6.5, 0), 'target AWA widens as boat speed drops');
    assert(L.awaByBow(40, 12, 6, 3) < L.awaByBow(40, 12, 6, 0), 'leeway narrows the target by the bow');
}

// ---------------------------------------------------------------- 2. rules
console.log('rules');
{
    const U = (dA, dS, o = {}) => L.decide({ up: true, dA, dS, vmgPct: 95, tws10: 11, sinceTack: 999, ...o }).key;
    const D = (dA, dS, vmgPct, o = {}) => L.decide({ up: false, dA, dS, vmgPct, tws10: 11, sinceTack: 999, ...o }).key;

    assert(U(3, 0.1) === 'higher', 'upwind: wide and at speed -> point higher');
    assert(U(3, 0) === 'higher', 'upwind: exactly at target speed counts as at speed');
    assert(U(3, -0.05) === 'groove', 'upwind: wide but just under target -> hold (no "down to" while below it)');
    assert(U(1.5, 0.2) === 'groove', 'upwind: 1.5 deg wide is still the groove');
    assert(U(1.51, 0.2) === 'higher', 'upwind: past 1.5 deg wide -> point higher');
    assert(U(0, -0.3) === 'groove', 'upwind: exactly at the floor is not yet slow');
    assert(U(0, -0.31) === 'build', 'upwind: slow on the angle -> build speed, hold angle');
    assert(U(4, -0.5) === 'build', 'upwind: slow and wide -> build speed, NOT bear off (footing lost)');
    assert(U(-1.5, -0.5) === 'build', 'upwind: 1.5 deg high while slow is not yet pinching');
    assert(U(-1.51, -0.5) === 'bearoff-pinch', 'upwind: slow and pinching -> bear off to the target angle');
    assert(U(4, -0.5, { sinceTack: 20 }) === 'bearoff-accel', 'upwind: slow after a tack -> bear off to accelerate');
    assert(U(4, -0.5, { sinceTack: 60 }) === 'build', 'upwind: the post-tack window closes at 60 s');
    assert(U(0, 0, { tws10: 14 }) === 'groove' && L.decide({ up: true, dA: 0, dS: 0, vmgPct: 95, tws10: 14, sinceTack: 999 }).sub === 'flat',
        'upwind: groove in 13+ kn says stay flat, feather');
    assert(L.decide({ up: true, dA: 0, dS: -0.5, vmgPct: 80, tws10: 16, sinceTack: 999 }).sub === 'holdChop', 'upwind: slow in 13+ kn -> flatten, power through chop');

    assert(D(-3, 0.1, 95) === 'deeper', 'downwind: hot and at speed -> sail deeper');
    assert(D(-2, 0.1, 95) === 'groove', 'downwind: 2 deg hot is still the groove');
    assert(D(0, -0.5, 95) === 'heat', 'downwind: slow and VMG under ORC -> heat up');
    assert(D(8, -0.5, 102) === 'groove', 'downwind: slow but deep with VMG over ORC -> hold');
    assert(D(0, 0, 100) === 'groove-deeper', 'downwind: VMG at ORC in 11 kn -> try a degree deeper');
    assert(D(0, 0, 100, { tws10: 8 }) === 'groove', 'downwind: not in light air, where deeper lost');
    assert(D(0, -0.31, 101) === 'groove', 'downwind: under the floor, no "try deeper"');
    assert(D(2, 0.2, 105) === 'groove-deeper', 'downwind: within the groove band, try deeper');
    assert(D(38, 1.0, 111) === 'groove', 'downwind: already 38 deg deeper than target (R6 12:42) -> no "try deeper"');

    const R = (dS) => L.decide({ up: false, reach: true, dA: 0, dS, vmgPct: 16, tws10: 12, sinceTack: 999 });
    assert(R(0.1).key === 'reach' && R(0.1).steer === 0 && R(0.1).kind === 'groove', 'reach at polar speed: no angle advice');
    assert(R(-0.5).key === 'reach-slow' && R(-0.5).kind === 'build' && R(-0.5).steer === 0, 'reach under polar: build colour, still no steering arrow');
    assert(L.decide({ up: true, reach: true, dA: 40, dS: 0.5, vmgPct: 60, tws10: 12, sinceTack: 999 }).key === 'reach',
        'a close reach in upwind mode is a reach, not "point higher by 40"');
    assert(L.subtitle('polar', 7.43, null) === 'polar 7.4 kn', 'reach subtitle names the polar speed');

    // arrows: upwind up/down; downwind the way to turn the bow
    assert(L.label('POINT HIGHER', -1, true, 1) === '▲ POINT HIGHER' && L.label('BEAR OFF', 1, true, -1) === '▼ BEAR OFF', 'upwind arrows');
    assert(L.label('SAIL DEEPER', 1, false, 1) === '◀ SAIL DEEPER', 'stbd gybe, deeper = turn to port');
    assert(L.label('SAIL DEEPER', 1, false, -1) === 'SAIL DEEPER ▶', 'port gybe, deeper = turn to starboard');
    assert(L.label('HEAT UP', -1, false, -1) === '◀ HEAT UP', 'port gybe, heat up = turn to port');
    assert(L.label('IN THE GROOVE', 0, true, 1) === 'IN THE GROOVE', 'no arrow in the groove');
    assert(L.subtitle('down', 6.43, 25.4) === 'down to 6.4 kn' && L.subtitle('toAngle', 6.4, 25.6) === 'to 26°', 'subtitles carry the numbers');
}

// ---------------------------------------------------------------- 3. the live engine
console.log('engine');
{
    const off = T.cal.offset;
    // upright raw readings for a corrected starboard AWA of A (port if A < 0)
    const rawAwa = (A, aws) => {
        const up = L.upwashAt(aws * 0.8);
        return A >= 0 ? A + off + up : 360 + A + off - up;
    };
    const state = (t, o = {}) => ({ awa: rawAwa(o.A ?? 27, o.aws ?? 16), aws: o.aws ?? 16, bsp: o.bsp ?? 6.4, roll: o.roll ?? 0,
        awaAt: 'awaAt' in o ? o.awaAt : t, bspAt: 'bspAt' in o ? o.bspAt : t, rollAt: 'rollAt' in o ? o.rollAt : t });
    const run = (eng, from, to, o, step = 250) => { let v; for (let t = from; t <= to; t += step) v = eng.update(state(t, typeof o === 'function' ? o(t) : o), t); return v; };

    let e = L.create();
    let v = e.update({ awa: null, aws: null, bsp: null, roll: null, awaAt: null, bspAt: null, rollAt: null }, 1000);
    assert(!v.ok && v.main === 'NO NMEA', 'nothing received -> NO NMEA');
    v = e.update(state(10000, { rollAt: null }), 10000);
    assert(!v.ok && v.main === 'NO HEEL', 'no heel -> NO HEEL, not uncorrected numbers');
    v = e.update(state(10000, { awaAt: 4000 }), 10000);
    assert(!v.ok && v.main === 'NO WIND', 'wind 6 s old -> NO WIND');
    v = e.update(state(10000, { bspAt: 4900 }), 10000);
    assert(!v.ok && v.main === 'NO SPEED', 'speed over 5 s old -> NO SPEED');
    v = e.update(state(10000), 17000);
    assert(!v.ok && v.main === 'NO NMEA', 'feed silent 7 s -> NO NMEA (checked against now, not the last sentence)');
    v = e.update(state(10000, { aws: 2, bsp: 1 }), 10000);
    assert(!v.ok && v.main === 'TOO LIGHT', 'under 4 kn -> TOO LIGHT');
    assert(v.stw === undefined && v.awa === undefined, 'a no-data state carries no numbers to draw');

    // a healthy stream gives numbers; target and corrected AWA are in the same frame
    e = L.create();
    v = run(e, 0, 10000, { A: 26, bsp: 6.4 });
    assert(v.ok && v.up && v.side === 1, 'starboard upwind stream');
    assert(Math.abs(v.awa - 26) < 1.0, `corrected AWA ~ what was sailed (${v.awa.toFixed(2)})`);
    assert(v.tAwa > 15 && v.tAwa < 35 && v.tSpeed > 5 && v.tSpeed < 8, 'plausible targets');
    // the target AWA is ORC's true angle at the speed actually sailed (upright: no leeway)
    v = run(e, 10250, 20000, { A: 26, bsp: 5.5 });
    const tg = L.targets(v.tws10, true);
    assert(near(v.tAwa, L.awaByBow(tg.twa, v.tws10, v.stw, 0), 0.01), 'target AWA uses the actual speed');
    assert(v.tAwa - L.awaByBow(tg.twa, v.tws10, tg.speed, 0) > 0.3, 'slow -> target reads wider than at target speed');

    // banner hold: one slow sample does not change the banner; 3 s of it does
    e = L.create();
    run(e, 0, 20000, { A: 26, bsp: 7.5 });
    const k0 = e.update(state(20250, { A: 26, bsp: 7.5 }), 20250).key;
    v = e.update(state(20500, { A: 26, bsp: 5.0 }), 20500);
    assert(v.key === k0, 'a single-sample dip does not flip the banner');
    v = run(e, 20750, 22000, { A: 26, bsp: 5.0 });
    assert(v.key === k0, 'still held under 3 s');
    v = run(e, 22250, 30000, { A: 26, bsp: 5.0 });
    assert(v.key !== k0 && v.kind === 'build', 'after 3 s the build-speed banner shows');

    // tack: the smoothed wind stays on the other side for 10 s; the post-tack window runs from the
    // moment it flipped (the 5 s smoothing puts that ~2.5 s after the helm turned)
    e = L.create();
    run(e, 0, 30000, { A: 26, bsp: 6.4 });
    v = run(e, 30250, 35000, { A: -26, bsp: 6.4 });
    assert(v.side === 1, 'wind on port for 5 s is not yet a tack');
    v = run(e, 35250, 44000, { A: -26, bsp: 6.4 });
    assert(v.side === -1, 'after 10 s on port (smoothed) the side flips');
    assert(v.sinceTack > 10 && v.sinceTack < 13, `the window starts at the flip (${v.sinceTack.toFixed(1)} s)`);
    // slow and a little wide (at 4.8 kn the target AWA widens, so 26 would read as pinching)
    v = run(e, 44250, 60000, { A: -30, bsp: 4.8 });
    assert(v.key === 'bearoff-accel', 'slow within 60 s of the tack -> bear off to accelerate');
    v = run(e, 60250, 100000, { A: -30, bsp: 4.8 });
    assert(v.key === 'build', `past 60 s (+3 s hold) -> build speed, hold angle (${v.key}, dA ${v.dA.toFixed(1)})`);

    // mode hysteresis: an upwind boat stays upwind till TWA > 100, a downwind one till < 80
    e = L.create();
    const aws = 9;
    const mode = A => run(e, e._t ?? 0, (e._t = (e._t ?? 0) + 20000), { A, aws, bsp: 6.5 }).mode;
    // corrected AWA at 9 kn apparent, 6.5 kn boat speed: 36 -> TWA ~85, 46 -> ~96, 70 -> ~112.
    // Points on both sides of 90 inside the band, so a single 90 deg switch fails one of them.
    assert(mode(28) === 'up', 'close-hauled starts upwind');
    assert(mode(46) === 'up', 'TWA ~96 stays upwind (switch at 100, not 90)');
    assert(mode(70) === 'down', 'TWA ~112 -> downwind');
    assert(mode(36) === 'down', 'TWA ~85 stays downwind (switch back under 80, not 90)');
    assert(mode(28) === 'up', 'close-hauled again -> upwind');

    // REACH: TWA more than 25 deg from the target angle. Find the corrected AWA that gives a wanted TWA.
    const awaFor = (twaWanted, aws, bsp) => {
        let lo = 1, hi = 179;
        for (let i = 0; i < 60; i++) {
            const mid = (lo + hi) / 2, c = L.chain(rawAwa(mid, aws), aws, bsp, 0);
            if (Math.abs(c.twa) < twaWanted) lo = mid; else hi = mid;
        }
        return (lo + hi) / 2;
    };
    // corrected AWA whose TWA is `off` deg from the gybe target at that point's own wind (true wind
    // speed changes with the angle here, since the apparent wind is held fixed)
    const awaOff = (off, aws, bsp) => {
        let lo = 61, hi = 179;
        for (let i = 0; i < 60; i++) {
            const mid = (lo + hi) / 2, c = L.chain(rawAwa(mid, aws), aws, bsp, 0);
            if (Math.abs(c.twa) - L.targets(c.tws10, false).twa < off) lo = mid; else hi = mid;
        }
        return (lo + hi) / 2;
    };
    {
        const aws = 9, bsp = 7.0;
        e = L.create();
        v = run(e, 0, 20000, { A: awaFor(100, aws, bsp), aws, bsp });
        assert(v.mode === 'down' && v.reach && v.key.startsWith('reach') && v.tAwa === null,
            `100 TWA, far from the gybe target -> REACH, no angle target (${v.key})`);
        assert(Math.abs(v.tSpeed - L.polarSpeed(v.twa, v.tws10)) < 1e-9, 'reach target speed = ORC polar at the angle sailed');
        const offOf = v => v.twa - L.targets(v.tws10, false).twa;
        v = run(e, 20250, 40000, { A: awaOff(-22, aws, bsp), aws, bsp });
        assert(v.reach, `from a reach, 22 deg off the target stays a reach (out at 20; off ${offOf(v).toFixed(1)})`);
        v = run(e, 40250, 60000, { A: awaOff(-15, aws, bsp), aws, bsp });
        assert(!v.reach && v.mode === 'down', `within 20 deg of the target -> downwind rules again (off ${offOf(v).toFixed(1)})`);
        v = run(e, 60250, 80000, { A: awaOff(-22, aws, bsp), aws, bsp });
        assert(!v.reach, `from a run, 22 deg off the target is still a run (in at 25; off ${offOf(v).toFixed(1)})`);
        v = run(e, 80250, 100000, { A: awaOff(-28, aws, bsp), aws, bsp });
        assert(v.reach, `past 25 deg off -> REACH (off ${offOf(v).toFixed(1)})`);
    }
    {
        // a close reach in upwind mode, then rounding onto the beat: the after-tack window opens
        e = L.create();
        v = run(e, 0, 20000, { A: awaFor(80, 12, 7.0), aws: 12, bsp: 7.0 });
        assert(v.mode === 'up' && v.reach, `close reach (80 TWA) in upwind mode -> REACH (${v.key})`);
        v = run(e, 20250, 40000, { A: 28, aws: 16, bsp: 4.5 });
        assert(!v.reach && v.sinceTack < 25, `rounding from the reach onto the beat opens the post-tack window (${v.sinceTack.toFixed(0)} s)`);
    }

    // a replay seek backwards starts clean: the first banner shows immediately
    e = L.create();
    run(e, 100000, 130000, { A: 26, bsp: 7.5 });
    v = e.update(state(5000, { A: 26, bsp: 5.0 }), 5000);
    assert(v.ok && v.kind === 'build', 'after a seek back, no hold carried over from the other time');
}

// ---------------------------------------------------------------- heel sign, on real log lines
// build_polar.py reads roll from YXXDR field 10 and treats negative as heeled to port. The store
// must keep that signed number (it used to keep only |roll|, which loses the tack).
console.log('heel sign');
{
    const parserSrc = readFileSync(join(root, 'static/js/nmea-parser.js'), 'utf8');
    const storeSrc = readFileSync(join(root, 'static/js/nmea-store.js'), 'utf8');
    const sb = {};
    new Function('module', 'requestAnimationFrame', 'CustomEvent', 'AISDecoder',
        parserSrc + '\n' + storeSrc + '\nmodule.NmeaStore = NmeaStore;')(
        sb, () => 0, class { constructor(type, o) { this.type = type; this.detail = o && o.detail; } }, { processSentence: () => null });
    const withCk = body => { let c = 0; for (const ch of body) c ^= ch.charCodeAt(0); return `$${body}*${c.toString(16).toUpperCase().padStart(2, '0')}`; };
    const store = new sb.NmeaStore();
    // verbatim from nmea_2026-09-19_125203.txt (R5)
    const real = '$YXXDR,A,-110.04,D,Yaw,A,0.06,D,Pitch,A,1.62,D,Roll*4A';
    store.ingest(real, 1000);
    assert(store.state.roll === Number(real.split(',')[10]) && store.state.rollAt === 1000, 'roll = YXXDR field 10, as build_polar.py reads it');
    store.ingest(withCk('YXXDR,A,-100.00,D,Yaw,A,0.50,D,Pitch,A,-24.30,D,Roll'), 2000);
    assert(store.state.roll === -24.3 && store.state.heel === 24.3, 'negative roll kept signed; heel stays the magnitude');
}

console.log(`\n${passed} passed, ${failed} failed`);
if (failed) process.exit(1);
console.log('all passed');
