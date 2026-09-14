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

// --- summary -------------------------------------------------------------
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
console.log('all passed');
// NmeaClient's rate counter is a setInterval that outlives the assertions,
// so exit explicitly rather than waiting on an event loop that never drains.
process.exit(0);
