/**
 * Replay/scrub tests for nmea-client.js.
 * Run: node tests/test_replay.mjs
 * No dependencies, no live APIs, no network.
 *
 * The invariant that matters: seeking to index N must leave the store in
 * exactly the state it would have been in had the log played straight through
 * to N. Instrument state accumulates (position, heading, wind, AIS targets),
 * so a seek that only moves a counter would show the boat at the new time with
 * stale data from the old one — a plausible-looking replay that is quietly
 * wrong, which is the failure mode this repo cares most about.
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(__dirname, '../static/js/nmea-client.js'), 'utf8');

// nmea-client.js is a plain class declaration; evaluate it and hand the class back.
// performance.now() is controllable so tests can advance replay time deterministically,
// and requestAnimationFrame is counted so tests can tell whether playback was resumed.
let nowMs = 0;
let rafCalls = 0;
const openSockets = [];
const sandbox = {};
new Function('module', 'performance', 'requestAnimationFrame',
    'cancelAnimationFrame', 'WebSocket', 'fetch',
    src + '\nmodule.NmeaClient = NmeaClient;'
)(
    sandbox,
    { now: () => nowMs },
    () => { rafCalls++; return 0; },   // never auto-advance; tests drive the clock
    () => {},
    function FakeWebSocket(url) {
        this.url = url;
        this.closed = false;
        this.close = function () { this.closed = true; };
        openSockets.push(this);
    },
    (...args) => globalThis.fetch(...args),   // late-bound so tests can restub
);
const { NmeaClient } = sandbox;

let passed = 0, failed = 0;
function assert(condition, msg) {
    if (condition) { passed++; }
    else { failed++; console.error(`  FAIL: ${msg}`); }
}

/** Records everything ingested since the last reset, so we can compare states. */
function makeStore() {
    return {
        lines: [],
        resets: 0,
        bulkDepth: 0,
        bulkSpans: [],          // [{from, to}] index range covered by each bulk
        reset() { this.lines = []; this.resets++; },
        ingest(line, ts) { this.lines.push(`${ts}|${line}`); },
        beginBulk() { this.bulkDepth++; this._bulkFrom = this.lines.length; },
        endBulk() {
            this.bulkDepth--;
            this.bulkSpans.push({ from: this._bulkFrom, to: this.lines.length });
        },
    };
}

// A synthetic log: one sentence per second, values that change every line so
// that landing on the wrong index is detectable.
function makeLog(n, startMs = Date.parse('2026-09-13T19:00:00Z')) {
    const lines = [];
    for (let i = 0; i < n; i++) {
        const t = new Date(startMs + i * 1000).toISOString().replace('T', ' ').replace('Z', '');
        lines.push(`${t}  $GPRMC,${String(i).padStart(6, '0')},A,3730.7584,N,12211.5986,W`);
    }
    return lines.join('\n');
}

const LOG_N = 120;
const LOG = makeLog(LOG_N);

// --- loadText ------------------------------------------------------------
console.log('loadText:');

{
    const store = makeStore();
    const c = new NmeaClient(store);
    let reported = null;
    c.loadText(LOG, (n) => { reported = n; });
    assert(reported === LOG_N, `should report ${LOG_N} lines, got ${reported}`);
    assert(c._replayLines.length === LOG_N, 'should hold all lines');
    assert(c._replayTimestamps[0] === Date.parse('2026-09-13T19:00:00Z'),
        'should parse the first timestamp as UTC');
}

{
    const store = makeStore();
    const c = new NmeaClient(store);
    let reported = null;
    c.loadText('# a comment\n\n' + LOG + '\n', (n) => { reported = n; });
    assert(reported === LOG_N, `comments and blanks should be dropped, got ${reported}`);
}

// --- seek ----------------------------------------------------------------
console.log('seek:');

/**
 * Reference implementation of "what the store should hold at index `idx`",
 * produced WITHOUT seek(): start playback, advance the clock, let the real
 * tick loop emit. If seek() agrees with this, seek() is correct.
 *
 * The log is one sentence per second at 1x, so advancing (idx-1) seconds
 * makes exactly `idx` lines due.
 */
function playThroughTo(idx) {
    const store = makeStore();
    const c = new NmeaClient(store);
    nowMs = 0;
    c.loadText(LOG);
    c.startReplay(1);
    nowMs = (idx - 1) * 1000;
    c._replayTick();
    return store.lines.slice(0, idx);
}

{
    const store = makeStore();
    const c = new NmeaClient(store);
    c.loadText(LOG);
    c.startReplay(1);
    c.seek(40);
    assert(store.lines.length === 40, `seek(40) should ingest 40 lines, got ${store.lines.length}`);
    assert(c._replayIdx === 40, `_replayIdx should be 40, got ${c._replayIdx}`);

    const expected = playThroughTo(40);
    assert(JSON.stringify(store.lines) === JSON.stringify(expected),
        'seek(40) state must equal playing through to 40');
}

{
    // The real scrub case: jump forward, then drag back. Backwards is the one
    // that breaks if seek just rewinds a counter without resetting the store.
    const store = makeStore();
    const c = new NmeaClient(store);
    c.loadText(LOG);
    c.startReplay(1);
    c.seek(90);
    c.seek(25);
    assert(store.lines.length === 25,
        `after seek(90) then seek(25), store should hold 25 lines, got ${store.lines.length}`);

    const expected = playThroughTo(25);
    assert(JSON.stringify(store.lines) === JSON.stringify(expected),
        'backward seek must equal playing through to 25 (no stale state left behind)');
}

{
    const store = makeStore();
    const c = new NmeaClient(store);
    c.loadText(LOG);
    c.startReplay(1);
    c.seek(-5);
    assert(c._replayIdx === 0, `seek(-5) should clamp to 0, got ${c._replayIdx}`);
    c.seek(99999);
    assert(c._replayIdx === LOG_N, `seek past the end should clamp to ${LOG_N}, got ${c._replayIdx}`);
}

{
    // Seeking while paused must not silently resume playback. Asserting the
    // flag alone is not enough — it stays true even if the tick loop is
    // rescheduled behind it, so assert the observable consequences too.
    const store = makeStore();
    const c = new NmeaClient(store);
    c.loadText(LOG);
    c.startReplay(1);
    c.pauseReplay();
    const rafBefore = rafCalls;
    c.seek(50);
    assert(c._replayPaused === true, 'seek must preserve the paused state');
    assert(store.lines.length === 50, 'seek while paused should still reposition the store');
    assert(c._status === 'replay-paused',
        `status should stay 'replay-paused' after seeking, got '${c._status}'`);
    assert(rafCalls === rafBefore,
        'seek while paused must not schedule a tick — playback would creep forward');
}

{
    // ...and seeking while playing must keep playing.
    const store = makeStore();
    const c = new NmeaClient(store);
    c.loadText(LOG);
    c.startReplay(1);
    const rafBefore = rafCalls;
    c.seek(50);
    assert(c._status === 'replaying', `status should stay 'replaying', got '${c._status}'`);
    assert(rafCalls > rafBefore, 'seek while playing must reschedule the tick');
}

// --- bulk coalescing ------------------------------------------------------
console.log('bulk coalescing:');

{
    // seek() must re-ingest inside bulk mode. Without it every sentence drives a
    // Leaflet marker update and a full vessel-panel innerHTML rebuild, freezing
    // the tab for minutes on a busy log.
    const store = makeStore();
    const c = new NmeaClient(store);
    nowMs = 0;
    c.loadText(LOG);
    c.startReplay(1);
    store.bulkSpans.length = 0;
    c.seek(60);
    assert(store.bulkSpans.length === 1,
        `seek should open exactly one bulk span, got ${store.bulkSpans.length}`);
    assert(store.bulkDepth === 0, 'bulk must be closed when seek returns');
    const span = store.bulkSpans[0];
    assert(span.from === 0 && span.to === 60,
        `the bulk span should cover the whole re-ingest, got ${span.from}..${span.to}`);
}

{
    // Normal playback must coalesce per frame too — that is what stopped the
    // own-ship icon shaking at 10Hz GPS (and hundreds of Hz at 60x).
    const store = makeStore();
    const c = new NmeaClient(store);
    nowMs = 0;
    c.loadText(LOG);
    c.startReplay(1);
    store.bulkSpans.length = 0;
    nowMs = 30_000;
    c._replayTick();
    assert(store.bulkSpans.length === 1,
        `each playback frame should be one bulk span, got ${store.bulkSpans.length}`);
    assert(store.bulkDepth === 0, 'bulk must be closed when the frame ends');
    assert(store.bulkSpans[0].to > store.bulkSpans[0].from,
        'the frame ingested lines inside the bulk span');
}

{
    // A store without bulk support must still work (the cloud-AIS path, and any
    // future consumer) — feature-detected, not assumed.
    const plain = {
        lines: [], reset() { this.lines = []; },
        ingest(l, t) { this.lines.push(`${t}|${l}`); },
    };
    const c = new NmeaClient(plain);
    c.loadText(LOG);
    c.startReplay(1);
    c.seek(20);
    assert(plain.lines.length === 20,
        `seek must work without beginBulk, got ${plain.lines.length}`);
}

// --- replay lifecycle hooks ----------------------------------------------
console.log('replay lifecycle hooks:');

{
    // onReplayReset is how app.js clears the MAP's vessel layer. nmea-store's
    // own reset() does not touch vesselStore, the markers or the track lines, so
    // without this hook a replayed race's contacts stayed drawn on the chart and
    // survived into Live, stamped as current traffic.
    const store = makeStore();
    const c = new NmeaClient(store);
    let resets = 0;
    c.onReplayReset = () => { resets++; };
    nowMs = 0;
    c.loadText(LOG);
    c.startReplay(1);
    assert(resets === 1, `startReplay must fire onReplayReset, got ${resets}`);
    c.seek(40);
    assert(resets === 2, `seek must fire onReplayReset, got ${resets}`);
}

{
    // onReplayClock publishes the recording's own time, so staleness and track
    // trimming are measured in log time rather than the wall clock.
    const store = makeStore();
    const c = new NmeaClient(store);
    const seen = [];
    c.onReplayClock = (ms) => seen.push(ms);
    nowMs = 0;
    c.loadText(LOG);
    c.startReplay(1);
    c.seek(30);
    assert(seen.length > 0, 'seek must publish a replay clock');
    const last = seen[seen.length - 1];
    assert(last === Date.parse('2026-09-13T19:00:30Z'),
        `clock should be log time at the playhead, got ${new Date(last).toISOString()}`);
    assert(last < Date.now(), 'the published clock is historical, not the wall clock');
}

// --- the live socket must not clobber replay status ----------------------
console.log('stale socket handlers:');

{
    // startReplay() closes the live WebSocket, but the closing socket's onclose
    // fires afterwards. Unguarded, it set the status to 'disconnected' *during*
    // playback — the header read "Disconnected" while the recording played, which
    // is what made the transport look dead.
    const store = makeStore();
    const c = new NmeaClient(store);
    openSockets.length = 0;
    c.connect('ws://test/nmea');
    const sock = openSockets[0];
    assert(!!sock, 'connect() should have created a socket');

    sock.onopen();
    assert(c._status === 'connected', `expected 'connected', got '${c._status}'`);

    nowMs = 0;
    c.loadText(LOG);
    c.startReplay(1);
    assert(c._status === 'replaying', `expected 'replaying' after startReplay, got '${c._status}'`);
    assert(sock.closed, 'startReplay must close the live socket');

    // The old socket now reports its close, late.
    sock.onclose();
    assert(c._status === 'replaying',
        `a stale socket's onclose must not change status, got '${c._status}'`);

    // ...and it must not smuggle sentences into the replay either.
    const before = store.lines.length;
    sock.onmessage({ data: '$GPRMC,999999,A,0000.0000,N,00000.0000,W' });
    assert(store.lines.length === before,
        'a stale socket must not ingest into a running replay');
}

// --- time range ----------------------------------------------------------
console.log('time range:');


{
    const store = makeStore();
    const c = new NmeaClient(store);
    c.loadText(LOG);
    c.startReplay(1);
    c.seek(30);
    const r = c.getReplayTimeRange();
    assert(r.start === Date.parse('2026-09-13T19:00:00Z'), 'range start should be the first timestamp');
    assert(r.end === Date.parse('2026-09-13T19:00:00Z') + (LOG_N - 1) * 1000,
        'range end should be the last timestamp');
    assert(r.current === Date.parse('2026-09-13T19:00:30Z'),
        `current should track the playhead, got ${new Date(r.current).toISOString()}`);
}

{
    const store = makeStore();
    const c = new NmeaClient(store);
    assert(c.getReplayTimeRange() === null, 'no log loaded should give a null range, not a crash');
}

// --- seeking to a moment (the race picker jumps to the gun) --------------
console.log('indexAtTime:');

{
    const c = new NmeaClient(makeStore());
    assert(c.indexAtTime(Date.parse('2026-09-13T19:00:30Z')) === null, 'no recording loaded -> null');
    c.loadText(LOG);
    c.startReplay(1);
    const t0 = Date.parse('2026-09-13T19:00:00Z');
    assert(c.indexAtTime(t0 + 30000) === 30, 'an exact second lands on that line');
    assert(c.indexAtTime(t0 + 30500) === 31, 'between lines -> the first line after, never before the moment');
    assert(c.indexAtTime(t0 - 3600e3) === 0, 'before the recording -> its first line');
    assert(c.indexAtTime(t0 + LOG_N * 1000) === null, 'after the recording ends -> null, not the last line');
    assert(c.indexAtTime(NaN) === null, 'NaN is not a time');
    c.seek(c.indexAtTime(t0 + 45000));
    assert(c.getReplayTimeRange().current === t0 + 45000, 'seek(indexAtTime(t)) puts the clock at t');
}

{
    // Unstamped lines (a raw paste in the middle of a log) must be skipped, not matched.
    const c = new NmeaClient(makeStore());
    c.loadText('2026-09-13 19:00:00.000  $GPRMC,a,A\n$GPRMC,raw,A\n2026-09-13 19:00:02.000  $GPRMC,b,A');
    c.startReplay(1);
    assert(c.indexAtTime(Date.parse('2026-09-13T19:00:01Z')) === 2, 'an unstamped line is never the answer');
}

{
    // warmupMs: re-read only the window before the target (race jumps on ~450k-line recordings).
    const store = makeStore();
    const c = new NmeaClient(store);
    c.loadText(LOG);
    c.startReplay(1);
    c.seek(100, { warmupMs: 30000 });
    assert(store.lines.length === 30, `a 30 s warmup re-reads 30 lines, got ${store.lines.length}`);
    assert(store.lines[0].includes('$GPRMC,000070,'), 'starting at the line 30 s before the target');
    assert(c.getReplayTimeRange().current === Date.parse('2026-09-13T19:01:40Z'), 'the playhead still lands on the target');
    c.seek(10, { warmupMs: 30000 });
    assert(store.lines.length === 10, 'a window reaching back past the start re-reads from line 0');
    c.seek(100);
    assert(store.lines.length === 100, 'without warmupMs a seek still re-reads everything (the scrubber\'s invariant)');
}

// --- race tracks: colour ramp, segments, which recording holds a moment ---
console.log('race tracks:');

{
    const rtSrc = readFileSync(join(__dirname, '../static/js/race-tracks.js'), 'utf8');
    const box = {};
    const served = {
        'races/index.json': { regattas: [{ id: 'a', file: 'a.json' }, { id: 'b', file: 'b.json' }] },
        'races/a.json': { races: [{ id: 'A-R1' }, { id: 'A-R2' }] },
        'races/b.json': { races: [{ id: 'B-R1' }] },
    };
    const fakeFetch = (url) => Promise.resolve(served[url]
        ? { ok: true, json: () => Promise.resolve(served[url]) } : { ok: false, status: 404 });
    new Function('module', 'NmeaClient', 'L', 'fetch', rtSrc + '\nmodule.RaceTracks = RaceTracks;')(
        box, NmeaClient, { canvas: () => ({}) }, fakeFetch);
    const { RaceTracks } = box;
    const hue = (c) => +/hsl\((\d+)/.exec(c)[1];

    assert(hue(RaceTracks.colorFor(85)) === 0 && hue(RaceTracks.colorFor(60)) === 0, '85% and below are pure red');
    assert(hue(RaceTracks.colorFor(97)) === 120 && hue(RaceTracks.colorFor(110)) === 120, '97% and above are pure green');
    assert(hue(RaceTracks.colorFor(91)) === 60, 'halfway (91%) is yellow');
    assert(hue(RaceTracks.colorFor(88)) < hue(RaceTracks.colorFor(94)), 'the ramp rises with %');
    assert(RaceTracks.colorFor(null) === RaceTracks.UNSCORED && RaceTracks.colorFor(NaN) === RaceTracks.UNSCORED,
        'unscored (tack, no data) is grey, never a colour that reads as a score');

    const pt = (t, pct) => [t, 37.8 + t * 1e-5, -122.4, pct];
    const seg = RaceTracks.segments([pt(0, 90), pt(5, 90), pt(10, 70), pt(15, 70), pt(20, null)]);
    assert(seg.length === 3, `three colour runs, got ${seg.length}`);
    assert(seg[1].latlngs[0][0] === pt(5)[1], 'a run starts at the previous run\'s last point, so the line is unbroken');
    const gap = RaceTracks.segments([pt(0, 90), pt(5, 90), pt(500, 90), pt(505, 90)]);
    assert(gap.length === 2 && gap[1].latlngs.length === 2, 'a hole in the data breaks the line instead of drawing across it');
    assert(RaceTracks.segments([pt(0, 90)]).length === 0, 'a single point draws nothing');

    const files = [                                   // newest first, as /api/logs sends them
        { name: 'nmea_2026-09-17_122333.txt' }, { name: 'nmea_2026-09-17_114740.txt' },
        { name: 'nmea_2026-09-17_114737.txt' }, { name: 'nmea_2026-09-17_102333.txt' },
        { name: 'nmea_2026-09-17_092333.txt' },
    ];
    const at = (h, m) => new Date(2026, 8, 17, h, m, 0).getTime();   // local, like the filenames
    assert(RaceTracks.fileForTime(files, at(10, 5)).name === 'nmea_2026-09-17_092333.txt',
        'the gun at 10:05 is in the 09:23 recording');
    assert(RaceTracks.fileForTime(files, at(11, 50)).name === 'nmea_2026-09-17_114740.txt',
        'after a logger restart, the latest recording starting before the moment');
    assert(RaceTracks.fileForTime(files, at(8, 0)) === null, 'before every recording -> null');
    assert(RaceTracks.fileForTime(files, at(18, 0)) === null, 'hours past the last recording\'s start -> null, not a stale file');

    // The race box: angle and speed against the ORC target. Rows: [t, lat, lon, pct, twa, tws, spd, mode, tgt_twa, tgt_spd]
    const v = (twa, mode, tgt) => RaceTracks.angleVerdict([0, 0, 0, 90, twa, 12, 6.5, mode, tgt, 6.6]);
    assert(v(44, 'u', 40).text === '4° low (footing)', `upwind wider than target is low/footing, got "${v(44, 'u', 40).text}"`);
    assert(v(37, 'u', 40).text === '3° high (pinching)', 'upwind tighter than target is high/pinching');
    assert(v(160, 'd', 170).text === '10° high (hotter)', 'downwind hotter than the gybe angle is high');
    assert(v(176, 'd', 170).text === '6° low (deeper)', 'downwind deeper than the gybe angle is low');
    assert(v(41, 'u', 40).text === 'on target', 'within 1.5° is on target');
    assert(v(90, 'r', null) === null, 'a reach has no angle verdict: the course sets it');
    const L1 = RaceTracks.lines({ name: 'Typon', source: 'instruments' }, [0, 0, 0, 88, 44, 12.3, 6.46, 'u', 39.2, 6.64]);
    assert(L1[0] === 'Typon: 88% of ORC VMG upwind', `headline, got "${L1[0]}"`);
    assert(L1[1] === 'TWA 44° vs target 39° → 5° low (footing)', `angle line, got "${L1[1]}"`);
    assert(L1[2] === 'STW 6.46 vs target 6.64 kn → −0.18 kn', `speed line, got "${L1[2]}"`);
    const L2 = RaceTracks.lines({ name: 'Wowla', source: 'ais' }, [0, 0, 0, 101, 95, 14, 7.8, 'r', null, 7.5]);
    assert(L2[1].includes('course sets the angle') && L2[2].endsWith('+0.30 kn'), 'reach: no angle verdict, speed delta still shown');
    assert(RaceTracks.lines({ name: 'X' }, [0, 0, 0, null, null, null, null, '', null, null])[0].includes('not scored'),
        'an unscored point says so');

    const tp = [[100], [105], [110], [140]].map(([t]) => [t, 0, 0, 90]);
    assert(RaceTracks.atTime(tp, 106e3, 10)[0] === 105, 'the nearest point in time');
    assert(RaceTracks.atTime(tp, 99e3, 10)[0] === 100, 'just before the first point');
    assert(RaceTracks.atTime(tp, 125e3, 10) === null, 'a gap wider than the tolerance -> null, never a stale reading');
    assert(RaceTracks.atTime([], 100e3, 10) === null, 'no points -> null');

    // Every regatta in races/index.json is listed, in index order (a new regatta = one more file).
    const loaded = await new RaceTracks({}, null).load();
    assert(loaded.races.map(r => r.id).join(',') === 'A-R1,A-R2,B-R1', `regattas merged in index order, got ${loaded.races.map(r => r.id)}`);
    served['races/index.json'].regattas.push({ id: 'c', file: 'missing.json' });
    let failed404 = false;
    await new RaceTracks({}, null).load().catch(e => { failed404 = /missing\.json: HTTP 404/.test(e.message); });
    assert(failed404, 'a listed file that is missing fails loudly, naming the file');
}

// --- loadUrl -------------------------------------------------------------
console.log('loadUrl:');

{
    const store = makeStore();
    const c = new NmeaClient(store);
    let requested = null;
    globalThis.fetch = async (url) => {
        requested = url;
        return { ok: true, text: async () => LOG };
    };
    const n = await new Promise((res, rej) => c.loadUrl('/logs/nmea_x.txt', res, rej));
    assert(requested === '/logs/nmea_x.txt', `should fetch the given url, got ${requested}`);
    assert(n === LOG_N, `loadUrl should report ${LOG_N} lines, got ${n}`);
}

{
    const store = makeStore();
    const c = new NmeaClient(store);
    globalThis.fetch = async () => ({ ok: false, status: 404, text: async () => '' });
    let errored = null;
    await new Promise((res) => c.loadUrl('/logs/gone.txt', () => res(), (e) => { errored = e; res(); }));
    assert(errored !== null, 'a failed fetch must surface an error, not load an empty log');
}

// --- loadUrl races and timeouts ------------------------------------------
console.log('loadUrl races and timeouts:');

{
    // Pick a big recording, then a small one. The small one resolves first and
    // starts; the big one must NOT then replace it underneath, or the picker
    // names one recording while another plays.
    const store = makeStore();
    const c = new NmeaClient(store);
    const slowLog = makeLog(500);
    let resolveSlow;
    const loaded = [];

    globalThis.fetch = (url) => {
        if (url.includes('slow')) {
            return new Promise((res) => { resolveSlow = () => res({ ok: true, text: async () => slowLog }); });
        }
        return Promise.resolve({ ok: true, text: async () => LOG });
    };

    c.loadUrl('/logs/slow.txt', (n) => loaded.push(['slow', n]));
    await new Promise((res) => c.loadUrl('/logs/fast.txt', (n) => { loaded.push(['fast', n]); res(); }));
    assert(loaded.length === 1 && loaded[0][0] === 'fast',
        `only the newest selection should load, got ${JSON.stringify(loaded)}`);

    resolveSlow();
    await new Promise((r) => setTimeout(r, 10));
    assert(loaded.length === 1,
        `a superseded load must be dropped when it finally resolves, got ${JSON.stringify(loaded)}`);
    assert(c._replayLines.length === LOG_N,
        `the loaded log must still be the fast one, got ${c._replayLines.length} lines`);
}

{
    // A half-open connection never resolves. Without a timeout the clock sat on
    // "loading…" forever while the previous recording kept playing.
    const store = makeStore();
    const c = new NmeaClient(store);
    globalThis.fetch = (url, opts) => new Promise((_res, rej) => {
        // Honour the AbortController the way a real fetch does.
        opts.signal.addEventListener('abort', () => {
            const e = new Error('aborted');
            e.name = 'AbortError';
            rej(e);
        });
    });
    // Outer guard: if loadUrl has no timeout of its own, this must FAIL rather
    // than hang. A test that proves a timeout by hanging forever is useless in
    // CI — it burns the job's whole time limit instead of reporting a failure.
    let err = null;
    let timedOutBySafetyNet = false;
    await Promise.race([
        new Promise((res) => c.loadUrl('/logs/hang.txt', () => res(), (e) => { err = e; res(); }, 40)),
        new Promise((res) => setTimeout(() => { timedOutBySafetyNet = true; res(); }, 2000)),
    ]);
    assert(!timedOutBySafetyNet, 'loadUrl did not time out on its own within 2s');
    assert(err !== null, 'a hung fetch must time out and report an error');
    assert(/timed out/.test(err.message), `the error should name the timeout, got "${err.message}"`);
    assert(c._replayLines === null,
        'a failed load must stop the replay, not leave the previous recording running');
}

// --- guards before a recording is loaded ---------------------------------
console.log('guards before a recording is loaded:');

{
    // The transport shows speed and play/pause the moment playback is opened,
    // i.e. before any recording is chosen. These used to throw a TypeError
    // indexing _replayTimestamps, leaving the bar visibly dead.
    const store = makeStore();
    const c = new NmeaClient(store);
    let threw = null;
    try {
        c.setReplaySpeed(5);
        c.pauseReplay();
        c.resumeReplay();
        c.seek(10);
    } catch (e) { threw = e; }
    assert(threw === null, `transport controls must not throw with no log: ${threw && threw.message}`);
    assert(c._replaySpeed === 5, 'setReplaySpeed should still record the choice');
    assert(c.getReplayTimeRange() === null, 'no range without a log');
}

{
    // A recording with no parseable timestamps must reach a named terminal state,
    // not sit at 0% looking like it is playing.
    const store = makeStore();
    const c = new NmeaClient(store);
    c.loadText('!AIVDM,1,1,,A,15Mue30002o?fR@E`:AaDW>42<3;,0*49\n!AIVDM,1,1,,B,x,0*00');
    assert(c.getReplayTimeRange() === null, 'a log with no timestamps has no range');
    c.startReplay(1);
    assert(c._status === 'replay-empty',
        `expected 'replay-empty', got '${c._status}'`);
}

{
    // A shape-valid but impossible date yields NaN, which is not null and slipped
    // through every guard — making the whole log flush at max speed.
    const store = makeStore();
    const c = new NmeaClient(store);
    c.loadText('2026-13-45 99:99:99.000  $GPRMC,1,A\n2026-09-13 19:00:00.000  $GPRMC,2,A');
    assert(c._replayTimestamps[0] === null,
        `an impossible date must parse to null, got ${c._replayTimestamps[0]}`);
    assert(c._replayFirstTs === Date.parse('2026-09-13T19:00:00Z'),
        'the first *valid* timestamp becomes the range start');
}

{
    // Seeking to the very end is trivially easy with a slider; it must reach the
    // terminal state rather than sitting on 'replaying' with the counter ticking.
    const store = makeStore();
    const c = new NmeaClient(store);
    nowMs = 0;
    c.loadText(LOG);
    c.startReplay(1);
    c.seek(LOG_N);
    assert(c._status === 'replay-done', `expected 'replay-done' at the end, got '${c._status}'`);
    assert(c._rateInterval === null, 'the rate counter must stop at the end, not run forever');
}

{
    // stopReplay must leave nothing behind that could report a stale range.
    const store = makeStore();
    const c = new NmeaClient(store);
    c.loadText(LOG);
    c.startReplay(1);
    c.stopReplay();
    assert(c.getReplayTimeRange() === null,
        'stopReplay must clear the parsed timestamps, not just the lines');
    assert(c._rateInterval === null, 'and stop the rate counter');
}

// --- vessel-store replay isolation ---------------------------------------
console.log('vessel-store replay isolation:');

{
    // Load vessel-store.js in its own sandbox with a fake localStorage and a
    // controllable wall clock. The bug being guarded: replaying a race wrote its
    // contacts to localStorage stamped Date.now(), so after returning to Live —
    // and after a page reload — a historical fleet was drawn as current traffic.
    const vsSrc = readFileSync(join(__dirname, '../static/js/vessel-store.js'), 'utf8');
    const store = {};
    const ls = {
        _d: {},
        getItem(k) { return k in this._d ? this._d[k] : null; },
        setItem(k, v) { this._d[k] = String(v); },
        removeItem(k) { delete this._d[k]; },
    };
    new Function('module', 'localStorage', 'setInterval', 'clearInterval',
        vsSrc + '\nmodule.VesselStore = VesselStore;'
    )(store, ls, () => 0, () => {});
    const { VesselStore } = store;

    const LOG_TIME = Date.parse('2026-05-23T20:05:00Z');   // a historical race
    const vs = new VesselStore({ staleMinutes: 10 });

    vs.setReplayMode(true);
    assert(vs.replayMode === true, 'setReplayMode(true) should enter replay mode');
    vs.setReplayClock(LOG_TIME);
    assert(vs.now() === LOG_TIME,
        'in replay, now() must be the log clock, not the wall clock');

    // A replayed contact carries the sentence's own timestamp.
    vs.upsert({ mmsi: 111, lat: 37.5, lon: -122.2, sog: 6, _reportedAt: LOG_TIME });
    const v = vs.get(111);
    assert(v._lastUpdate === LOG_TIME,
        `_lastUpdate must come from the report, got ${v._lastUpdate}`);
    assert(vs.tracks.get(111).points[0].time === LOG_TIME,
        'track points must be stamped with log time too');
    assert(v._lastUpdate < Date.now() - 86400000,
        'the stamp is historical — this is what stops it reading as live traffic');

    // Measured against the log clock it is fresh, so prune must keep it.
    assert(vs.prune().length === 0,
        'a contact fresh in log time must not be pruned by the wall clock');
    assert(vs.vessels.size === 1, 'the replayed contact survives prune');

    // And nothing may reach localStorage while replaying.
    vs.saveIfNeeded();
    assert(ls.getItem('vesselStore') === null,
        'replay must never persist — a reload would restore it as live traffic');

    // Leaving replay clears everything.
    vs.upsert({ mmsi: 222, lat: 37.6, lon: -122.3, _reportedAt: LOG_TIME });
    assert(vs.vessels.size === 2, 'two contacts before leaving replay');
    vs.setReplayMode(false);
    assert(vs.vessels.size === 0, 'leaving replay must clear every replayed contact');
    assert(vs.tracks.size === 0, 'and their tracks');
    assert(vs.now() > Date.now() - 5000, 'back on the wall clock once live');

    // Live contacts with no _reportedAt still work, and do persist.
    vs.upsert({ mmsi: 333, lat: 37.7, lon: -122.4 });
    assert(Math.abs(vs.get(333)._lastUpdate - Date.now()) < 5000,
        'a live contact with no _reportedAt falls back to the wall clock');
    vs.saveIfNeeded();
    assert(ls.getItem('vesselStore') !== null, 'live mode still persists');

    // clearAll reports what it removed, so app.js can drop the matching markers.
    const removed = vs.clearAll();
    assert(removed.length === 1 && removed[0] === 333,
        `clearAll must return the removed MMSIs, got ${JSON.stringify(removed)}`);
}

// --- auto-advance: picking the next recording ----------------------------
// The picker is populated straight from /api/logs, which returns NEWEST FIRST.
// So the option *below* the current one is an hour EARLIER, and "play the next
// file" implemented as "step down the list" plays the race backwards. Hence
// nextContiguousLog sorts by the filename's own timestamp and never trusts the
// list order.
console.log('auto-advance / nextContiguousLog:');

/** Build an /api/logs-shaped entry. */
function logFile(name) {
    return { name, url: `/logs/${name}`, bytes: 1 };
}

/** Local-time epoch for a log filename, computed independently of the impl. */
function localTs(y, mo, d, h, mi, s) {
    return new Date(y, mo - 1, d, h, mi, s, 0).getTime();
}

{
    const ts = NmeaClient.logStartTime('nmea_2026-09-17_110000.txt');
    assert(ts === localTs(2026, 9, 17, 11, 0, 0),
        'a log filename must be read as LOCAL time — the doc\'s rule is store UTC, display local, ' +
        `and filenames are the local half; got ${ts}`);
    assert(NmeaClient.logStartTime('notalog.txt') === null,
        'an unparseable name must return null rather than NaN, which compares false everywhere');
    assert(NmeaClient.logStartTime('nmea_2026-13-45_110000.txt') === null,
        'an impossible date must return null, not a rolled-over Date');
}

{
    // Real ordering from the Pi: newest first.
    const files = [
        logFile('nmea_2026-09-18_020000.txt'),
        logFile('nmea_2026-09-18_010000.txt'),
        logFile('nmea_2026-09-18_000000.txt'),
    ];
    // Playing the 00:00 file, whose last sentence lands one second before 01:00.
    const endTs = localTs(2026, 9, 18, 0, 59, 59);
    const r = NmeaClient.nextContiguousLog(files, '/logs/nmea_2026-09-18_000000.txt', endTs);
    assert(r.ok === true, `should advance, got ${JSON.stringify(r)}`);
    assert(r.file.name === 'nmea_2026-09-18_010000.txt',
        `must advance FORWARD in time to 01:00, not down the newest-first list; got ${r.file && r.file.name}`);
}

{
    // A capture restart mid-hour: 02:00 rotated, died at 02:14:19, restarted at
    // 02:14:20. "next = +1 hour" rejects this; comparing against the log's real
    // last sentence accepts it.
    const files = [
        logFile('nmea_2026-09-18_021420.txt'),
        logFile('nmea_2026-09-18_020000.txt'),
    ];
    const endTs = localTs(2026, 9, 18, 2, 14, 19);
    const r = NmeaClient.nextContiguousLog(files, '/logs/nmea_2026-09-18_020000.txt', endTs);
    assert(r.ok === true, `a mid-hour restart is contiguous, got ${JSON.stringify(r)}`);
    assert(r.file.name === 'nmea_2026-09-18_021420.txt', 'and is the file to advance to');
    assert(r.gapMs === 1000, `gap should be 1s, got ${r.gapMs}`);
}

{
    // The case that must NOT auto-play: a successor exists, days later. Rolling
    // a Saturday race into an unrelated Tuesday delivery would look continuous
    // and be wrong — the exact failure class this repo is built around.
    const files = [
        logFile('nmea_2026-09-22_220000.txt'),
        logFile('nmea_2026-09-18_020000.txt'),
    ];
    const endTs = localTs(2026, 9, 18, 2, 59, 59);
    const r = NmeaClient.nextContiguousLog(files, '/logs/nmea_2026-09-18_020000.txt', endTs);
    assert(r.ok === false, 'a multi-day gap must not auto-advance');
    assert(r.reason === 'gap', `reason should be 'gap', got '${r.reason}'`);
    assert(r.file.name === 'nmea_2026-09-22_220000.txt',
        'the rejected successor is still reported, so the UI can say how far away it is');
    assert(r.gapMs > 4 * 24 * 3600 * 1000, `gap should be >4 days, got ${r.gapMs}`);
}

{
    // Exactly on the threshold is contiguous; one millisecond past it is not.
    const files = [logFile('nmea_2026-09-18_030000.txt'), logFile('nmea_2026-09-18_020000.txt')];
    const cur = '/logs/nmea_2026-09-18_020000.txt';
    const start = localTs(2026, 9, 18, 3, 0, 0);
    const atLimit = NmeaClient.nextContiguousLog(files, cur, start - NmeaClient.MAX_LOG_GAP_MS);
    assert(atLimit.ok === true, 'a gap exactly at MAX_LOG_GAP_MS is contiguous');
    const pastLimit = NmeaClient.nextContiguousLog(files, cur, start - NmeaClient.MAX_LOG_GAP_MS - 1);
    assert(pastLimit.ok === false, 'one millisecond past the limit is not');
}

{
    const files = [logFile('nmea_2026-09-18_020000.txt'), logFile('nmea_2026-09-18_010000.txt')];
    const r = NmeaClient.nextContiguousLog(
        files, '/logs/nmea_2026-09-18_020000.txt', localTs(2026, 9, 18, 2, 59, 59));
    assert(r.ok === false && r.reason === 'end',
        `the newest recording has no successor, got ${JSON.stringify(r)}`);
}

{
    // A local file opened from disk is not in the Pi's list, and a list that
    // failed to load is empty. Neither may guess at a successor.
    const files = [logFile('nmea_2026-09-18_020000.txt')];
    const r = NmeaClient.nextContiguousLog(files, '/logs/nmea_2026-09-18_010000.txt', 1);
    assert(r.ok === false && r.reason === 'unknown',
        `an unknown current file must not advance, got ${JSON.stringify(r)}`);
    const r2 = NmeaClient.nextContiguousLog([], '/logs/x.txt', 1);
    assert(r2.ok === false, 'an empty list must not advance');
    const r3 = NmeaClient.nextContiguousLog(files, '/logs/nmea_2026-09-18_020000.txt', null);
    assert(r3.ok === false && r3.reason === 'unknown',
        'with no usable end timestamp, contiguity is unknowable — do not guess');
}

{
    // Junk in the directory must not become the successor, nor block the real one.
    const files = [
        logFile('nmea_2026-09-18_020000.txt'),
        logFile('README.txt'),
        logFile('nmea_2026-09-18_010000.txt'),
    ];
    const r = NmeaClient.nextContiguousLog(
        files, '/logs/nmea_2026-09-18_010000.txt', localTs(2026, 9, 18, 1, 59, 59));
    assert(r.ok === true && r.file.name === 'nmea_2026-09-18_020000.txt',
        `unparseable names must be skipped, got ${JSON.stringify(r)}`);
}

// --- auto-advance: what counts as "ran out" ------------------------------
// _finishReplay is reached two ways: playback genuinely running out, and the
// user dragging the scrubber to the end. Only the first may advance — having
// the next hour launch itself because you scrubbed to the end is not a feature.
console.log('auto-advance / end reason:');

{
    const store = makeStore();
    const c = new NmeaClient(store);
    const seen = [];
    c.onReplayEnd = (reason) => seen.push(reason);
    nowMs = 0;
    c.loadText(LOG);
    c.startReplay(1);
    nowMs = LOG_N * 1000;          // well past the last sentence
    c._replayTick();
    assert(c._status === 'replay-done', `should have finished, got '${c._status}'`);
    assert(JSON.stringify(seen) === JSON.stringify(['end']),
        `natural completion must report 'end', got ${JSON.stringify(seen)}`);
}

{
    const store = makeStore();
    const c = new NmeaClient(store);
    const seen = [];
    c.onReplayEnd = (reason) => seen.push(reason);
    nowMs = 0;
    c.loadText(LOG);
    c.startReplay(1);
    c.seek(LOG_N);
    assert(c._status === 'replay-done', `scrubbing to the end still finishes, got '${c._status}'`);
    assert(JSON.stringify(seen) === JSON.stringify(['seek']),
        `scrubbing to the end must report 'seek', not 'end', got ${JSON.stringify(seen)}`);
}

{
    // No listener attached must not throw — the callback is optional, as with
    // onReplayReset and onReplayClock.
    const store = makeStore();
    const c = new NmeaClient(store);
    nowMs = 0;
    c.loadText(LOG);
    c.startReplay(1);
    nowMs = LOG_N * 1000;
    let threw = false;
    try { c._replayTick(); } catch (e) { threw = true; }
    assert(!threw, 'finishing with no onReplayEnd listener must not throw');
}

// --- AIS sentences must be checksum-validated (P40) ----------------------
// Over TCP, framing guaranteed one sentence per line and an unvalidated AIS
// path was merely untidy. Over UDP a single dropped datagram splices the tail
// of one sentence onto the head of the next, and an unvalidated AIVDM decodes
// to a vessel with an arbitrary MMSI at an arbitrary position — drawn on the
// map and radar, fed into CPA/TCPA, and written permanently to the race log.
{
    const parserSrc = readFileSync(join(__dirname, '../static/js/nmea-parser.js'), 'utf8');
    const pbox = {};
    new Function('module', parserSrc + '\nmodule.NmeaParser = NmeaParser;')(pbox);
    const { parseLine, validateChecksum } = pbox.NmeaParser;

    // Real sentences lifted from a Pi capture, checksums verified.
    const goodAis = '!AIVDM,1,1,,A,35O7I1P00ro?bAhEd;qRvBLP00u@,0*3D';
    const otherAis = '!AIVDM,1,1,,A,15O0kl0000o@::0EWL8S12nN0@:I,0*50';
    const ownShip = '!AIVDO,1,1,,,B52cumP00=l:Bl5GL4KQ3w`Ql000,0*77';

    assert(validateChecksum(goodAis),
        'fixture sanity: the good AIVDM must have a valid checksum');
    assert(parseLine(goodAis) !== null, 'a valid AIVDM is still accepted');
    assert(parseLine(goodAis).isAIS === true, 'and is still flagged as AIS');
    assert(parseLine(ownShip) !== null, 'a valid own-ship AIVDO is still accepted');

    // Flip one payload character: structurally perfect, checksum now wrong.
    const corrupted = goodAis.replace('35O7I1P0', '35O7I1P1');
    assert(!validateChecksum(corrupted), 'fixture sanity: corruption breaks the checksum');
    assert(parseLine(corrupted) === null,
        'an AIVDM with a bad checksum must be rejected, not decoded into a phantom vessel');

    // The realistic UDP failure: the head of one sentence joined to the tail of
    // another after a dropped datagram. Syntactically a perfect single-fragment
    // AIVDM; the checksum is the only thing that catches it.
    const spliced = '!AIVDM,1,1,,A,35O7I1P00ro' + '?::0EWL8S12nN0@:I,0*50';
    assert(spliced.indexOf('*') > 0, 'fixture sanity: the splice looks well-formed');
    assert(parseLine(spliced) === null, 'a spliced AIVDM must be rejected');

    // Regression: $ sentences must be unaffected.
    assert(parseLine('$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*47') !== null,
        'valid $ sentences still parse');
    assert(parseLine('$GPGGA,123519,4807.038,N,01131.000,E,1,08,0.9,545.4,M,46.9,M,,*00') === null,
        'invalid $ sentences are still rejected');
}

// --- summary -------------------------------------------------------------
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
console.log('all passed');
// NmeaClient's rate counter is a setInterval that outlives the assertions,
// so exit explicitly rather than waiting on an event loop that never drains.
process.exit(0);
