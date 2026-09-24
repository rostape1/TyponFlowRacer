/**
 * Tests for vessel-names.js — the local MMSI→name database.
 * Run: node tests/test_vessel_names.mjs
 * No dependencies, no network.
 *
 * Why this exists. AIS separates identity from position: a name arrives in a
 * Type 5 (Class A, ~every 6 min) or Type 24 (Class B, rarer), while positions
 * arrive every 2-10 seconds. So a vessel is a bare MMSI until its next static
 * broadcast — and in replay it is worse, because startReplay/seek resets the
 * store, so names only accumulate from the start of the current file and are
 * dropped at every file boundary.
 *
 * A scan of the real 84-file archive: 824 distinct vessels, 765 (92.8%) send a
 * name at some point, 59 never do. So remembering what we have already seen is
 * where nearly all the value is.
 */

import { readFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(__dirname, '../static/js/vessel-names.js'), 'utf8');

let passed = 0, failed = 0;
function assert(cond, msg) {
    if (cond) passed++;
    else { failed++; console.error(`  FAIL: ${msg}`); }
}
function section(n) { console.log(`${n}:`); }

/** A localStorage stand-in, so persistence can be inspected and made to fail. */
function makeStorage(initial = {}) {
    return {
        data: { ...initial },
        failWrites: false,
        getItem(k) { return k in this.data ? this.data[k] : null; },
        setItem(k, v) {
            if (this.failWrites) throw new Error('QuotaExceededError');
            this.data[k] = String(v);
        },
        removeItem(k) { delete this.data[k]; },
    };
}

/** Fresh module instance sharing a given storage — models a page reload. */
function load(storage, fetchImpl) {
    const box = {};
    new Function('module', 'localStorage', 'fetch',
        src + '\nmodule.VesselNames = VesselNames;')(
        box, storage, fetchImpl || (() => Promise.reject(new Error('no fetch'))));
    return box.VesselNames;
}

// --- get() precedence ----------------------------------------------------
section('precedence');

{
    const s = makeStorage();
    const V = load(s);
    V._setSeedForTest({ '338361814': 'TYPON', '367123456': 'SEEDED' });

    assert(V.get('338361814') === 'TYPON', 'a seed name is returned');
    assert(V.get(338361814) === 'TYPON', 'a numeric MMSI works as well as a string');
    assert(V.get('999999999') === null, 'an unknown MMSI returns null, not a guess');

    // Learned beats seed: the receiver is more current than a file built weeks ago.
    V.learn('367123456', 'LEARNED LIVE');
    assert(V.get('367123456') === 'LEARNED LIVE',
        `a name heard live must beat the seed file, got ${V.get('367123456')}`);

    // Manual beats everything: overriding a wrong or garbled AIS name is the
    // entire point of typing one in.
    V.setManual('367123456', 'MY OVERRIDE');
    assert(V.get('367123456') === 'MY OVERRIDE',
        `a manual name must beat a learned one, got ${V.get('367123456')}`);
    V.learn('367123456', 'LATER AIS NAME');
    assert(V.get('367123456') === 'MY OVERRIDE',
        'and a later AIS name must NOT clobber the manual override');
}

// --- displayName ---------------------------------------------------------
section('displayName');

{
    const s = makeStorage();
    const V = load(s);
    V._setSeedForTest({ '368309230': 'FINAL FINAL' });

    assert(V.displayName({ mmsi: 368309230 }) === 'FINAL FINAL',
        'a vessel with no name of its own is resolved from the database');
    // A name actually received this session is authoritative over the database.
    assert(V.displayName({ mmsi: 368309230, name: 'FROM AIS' }) === 'FROM AIS',
        'a name received in this session wins over the database');
    assert(V.displayName({ mmsi: 368309230, shipname: 'FROM CLOUD' }) === 'FROM CLOUD',
        'the cloud path uses `shipname`, and that counts as received too');
    // ...but a manual override outranks even that.
    V.setManual('368309230', 'CORRECTED');
    assert(V.displayName({ mmsi: 368309230, name: 'FROM AIS' }) === 'CORRECTED',
        'a manual override outranks a received name');

    assert(V.displayName({ mmsi: 111222333 }) === 'MMSI 111222333',
        'an unknown vessel still gets a label, never blank or undefined');
    assert(V.displayName({}) === 'Unknown vessel',
        'a vessel with no MMSI at all must not render "MMSI undefined"');
    assert(V.displayName(null) === 'Unknown vessel', 'and null must not throw');
}

// --- cleaning ------------------------------------------------------------
section('name cleaning');

{
    const s = makeStorage();
    const V = load(s);
    // AIS pads fixed-width text fields with '@'. ais-decoder strips trailing
    // ones, but padding shows up mid-string from some encoders too.
    V.learn('300000001', 'SEA@@BREEZE@@@');
    assert(V.get('300000001') === 'SEA BREEZE',
        `'@' padding must be cleaned, got ${V.get('300000001')}`);
    V.learn('300000002', '   SPACED   OUT   ');
    assert(V.get('300000002') === 'SPACED OUT',
        `whitespace must be collapsed and trimmed, got ${V.get('300000002')}`);

    // An empty or padding-only name is not a name. Storing it would mask the
    // MMSI fallback with a blank label.
    V.learn('300000003', '@@@@@@');
    assert(V.get('300000003') === null, 'a padding-only name must be ignored');
    V.learn('300000004', '   ');
    assert(V.get('300000004') === null, 'a whitespace-only name must be ignored');
    V.learn('300000005', null);
    assert(V.get('300000005') === null, 'a null name must be ignored, not stored');

    // Control characters would corrupt the label and any log line it lands in.
    V.learn('300000006', 'BAD\x00NAME\x1b[31m');
    assert(!/[\x00-\x1f\x7f-\x9f]/.test(V.get('300000006') || ''),
        `control characters must be stripped, got ${JSON.stringify(V.get('300000006'))}`);
}

// --- MMSI validation -----------------------------------------------------
section('mmsi validation');

{
    const s = makeStorage();
    const V = load(s);
    V.learn('12345', 'TOO SHORT');
    assert(V.get('12345') === null, 'a malformed MMSI must not be stored');
    V.learn('__proto__', 'PROTO');
    assert(V.get('__proto__') === null, 'a prototype-polluting key must be rejected');
    assert(Object.prototype.PROTO === undefined, 'and must not reach Object.prototype');
    V.learn('368309230', 'GOOD');
    assert(V.get('368309230') === 'GOOD', 'a 9-digit MMSI is accepted');
}

// --- persistence ---------------------------------------------------------
section('persistence');

{
    const s = makeStorage();
    const V = load(s);
    V.learn('368309230', 'FINAL FINAL');
    V.setManual('367999888', 'HAND TYPED');
    V.flush();          // explicit: writes are debounced, see the 'debounce' section

    // A reload must not lose what we learned — that is the whole feature.
    const V2 = load(s);
    assert(V2.get('368309230') === 'FINAL FINAL',
        'a learned name must survive a page reload');
    assert(V2.get('367999888') === 'HAND TYPED',
        'and so must a manual one');
    // Precedence must survive too, not collapse into one bucket.
    V2.learn('367999888', 'AIS SAYS OTHERWISE');
    assert(V2.get('367999888') === 'HAND TYPED',
        'the manual/learned distinction must survive persistence, or an override '
        + 'silently degrades into a learned name and gets overwritten');
}

{
    // localStorage full, or disabled in private browsing. Losing a name is
    // acceptable; taking the map down with it is not.
    const s = makeStorage();
    s.failWrites = true;
    const V = load(s);
    let threw = false;
    try { V.learn('368309230', 'FINAL FINAL'); } catch (e) { threw = true; }
    assert(!threw, 'a storage failure must not throw into the AIS message path');
    assert(V.get('368309230') === 'FINAL FINAL',
        'and the name must still work for this session, in memory');
}

{
    // Corrupt stored JSON must not break startup.
    const s = makeStorage({ vesselNamesLearned: '{not json', vesselNamesManual: 'null' });
    let threw = false, V = null;
    try { V = load(s); } catch (e) { threw = true; }
    assert(!threw, 'corrupt stored data must not throw at load');
    assert(V && V.get('368309230') === null, 'and the map still answers queries');
}

// --- replay: names ARE learned, but an older sighting never wins -------
section('replay');

{
    // vessel-store.js refuses to persist while replaying, so a historical
    // contact cannot come back as live traffic. Names are the deliberate
    // exception: identity is timeless, position is not. Learning "368309230 is
    // FINAL FINAL" from a June log is true today; learning where she was is not.
    const s = makeStorage();
    const V = load(s);
    V.learn('368309230', 'FINAL FINAL');
    V.flush();
    assert(V.get('368309230') === 'FINAL FINAL',
        'names must still be learned during replay — identity does not go stale');
    const V2 = load(s);
    assert(V2.get('368309230') === 'FINAL FINAL',
        'and must be persisted, so replaying an old race improves the database');
}

{
    // The trap that makes "learn from replay" safe rather than destructive: a
    // vessel is renamed, today's feed teaches the new name, then you replay a
    // June recording. Without a recency check the June name wins and sticks
    // permanently, so a LIVE contact carries a stale identity with nothing
    // saying so — the house failure mode, on a collision-avoidance display.
    const s = makeStorage();
    const V = load(s);
    const DAY = 20500;
    V.learn('368309230', 'NEW NAME', DAY);
    V.learn('368309230', 'OLD JUNE NAME', DAY - 90);
    assert(V.get('368309230') === 'NEW NAME',
        `replaying an older log must NOT overwrite a newer name, got ${V.get('368309230')}`);
    // ...but a NEWER sighting must still win.
    V.learn('368309230', 'NEWEST', DAY + 1);
    assert(V.get('368309230') === 'NEWEST', 'a newer sighting must still update the name');
}

{
    // Old on-disk shape was {mmsi: "NAME"} with no day. It must still load, or
    // upgrading the app silently discards everything learned so far.
    const s = makeStorage({ vesselNamesLearned: JSON.stringify({ '368309230': 'LEGACY' }) });
    const V = load(s);
    assert(V.get('368309230') === 'LEGACY',
        'the pre-timestamp storage shape must still be readable after upgrade');
}

// --- parsing a pasted reference ------------------------------------------
section('parseVesselRef');

{
    const s = makeStorage();
    const V = load(s);
    const mt = 'https://www.marinetraffic.com/en/ais/details/ships/shipid:7754642/'
        + 'mmsi:368309230/vessel:FINAL_FINAL';
    let r = V.parseVesselRef(mt);
    assert(r && r.mmsi === '368309230' && r.name === 'FINAL FINAL',
        `a MarineTraffic URL must yield both fields, got ${JSON.stringify(r)}`);

    r = V.parseVesselRef('https://www.marinetraffic.com/en/ais/details/ships/'
        + 'mmsi:366123456/vessel:MARY_ELLEN_II/imo:0');
    assert(r && r.name === 'MARY ELLEN II', `extra path segments must not matter, got ${JSON.stringify(r)}`);

    // The obvious thing to type by hand.
    r = V.parseVesselRef('368309230 Final Final');
    assert(r && r.mmsi === '368309230' && r.name === 'Final Final',
        `a plain "<mmsi> <name>" must work, got ${JSON.stringify(r)}`);
    r = V.parseVesselRef('  368309230   Final   Final  ');
    assert(r && r.name === 'Final Final', 'with whitespace tolerated');

    assert(V.parseVesselRef('just some words') === null, 'text with no MMSI is rejected');
    assert(V.parseVesselRef('368309230') === null, 'an MMSI with no name is rejected');
    assert(V.parseVesselRef('') === null, 'empty input is rejected');
    assert(V.parseVesselRef(null) === null, 'null input is rejected rather than throwing');
    assert(V.parseVesselRef('12345 Short') === null, 'a non-9-digit MMSI is rejected');

    // Percent-encoding appears in real URLs.
    r = V.parseVesselRef('https://www.marinetraffic.com/en/ais/details/ships/'
        + 'mmsi:366123456/vessel:ST%20MARY');
    assert(r && r.name === 'ST MARY', `percent-encoding must be decoded, got ${JSON.stringify(r)}`);
}

// --- loading the seed file ----------------------------------------------
section('seed load');

{
    const s = makeStorage();
    const body = { generated: 'x', names: { '338361814': 'TYPON' } };
    const V = load(s, () => Promise.resolve({ ok: true, json: () => Promise.resolve(body) }));
    await V.load('vessel_names.json');
    assert(V.get('338361814') === 'TYPON', 'the seed file is loaded and queryable');
}

{
    // The seed file is an optimisation, not a dependency. A 404 on GitHub Pages,
    // or no network on the boat, must degrade to MMSI labels — never break the map.
    const s = makeStorage();
    const V = load(s, () => Promise.resolve({ ok: false, status: 404 }));
    let threw = false;
    try { await V.load('vessel_names.json'); } catch (e) { threw = true; }
    assert(!threw, 'a missing seed file must not reject');
    assert(V.displayName({ mmsi: 338361814 }) === 'MMSI 338361814',
        'and the app still labels vessels');
}

{
    const s = makeStorage();
    const V = load(s, () => Promise.reject(new Error('offline')));
    let threw = false;
    try { await V.load('vessel_names.json'); } catch (e) { threw = true; }
    assert(!threw, 'a network failure must not reject either');
}

{
    // Hostile / malformed seed content. This file is fetched data, so P19 applies
    // as much here as to the legend.
    const s = makeStorage();
    const body = { names: { '338361814': '<img src=x onerror=alert(1)>', 'bogus': 'X', '367000001': 12345 } };
    const V = load(s, () => Promise.resolve({ ok: true, json: () => Promise.resolve(body) }));
    await V.load('vessel_names.json');
    assert(V.get('bogus') === null, 'a malformed key in the seed file is dropped');
    assert(V.get('367000001') === null, 'a non-string value is dropped');
    const n = V.get('338361814');
    assert(typeof n === 'string' && n.length > 0, 'a hostile-looking name is still a string');
    assert(!/[<>]/.test(n), `angle brackets must be stripped from names: P19 — got ${JSON.stringify(n)}`);
}


// --- escapeHtml: the actual P19 sink ------------------------------------
// vessel-names.js contains no innerHTML (asserted below), but app.js interpolates
// labels into popup HTML and into title="..." / aria-label="..." ATTRIBUTES. The
// AIS 6-bit charset passes values 32-63 straight through, so '"' is a legal name
// byte from a checksum-valid Type 5 — and attribute context needs no angle
// bracket to break out of.
section('escapeHtml');

{
    const s = makeStorage();
    const V = load(s);

    assert(V.escapeHtml('A & B') === 'A &amp; B', 'ampersand escaped');
    assert(V.escapeHtml('<b>') === '&lt;b&gt;', 'angle brackets escaped');
    assert(V.escapeHtml('say "hi"') === 'say &quot;hi&quot;', 'double quote escaped');
    assert(V.escapeHtml("O'BRIEN") === 'O&#39;BRIEN', 'single quote escaped');
    assert(V.escapeHtml(null) === '' && V.escapeHtml(undefined) === '',
        'null/undefined escape to empty rather than "null"');

    // The real payload: 19 characters, fits the 20-char Type 5 name field.
    const attack = 'X" onclick=alert(1)';
    const out = V.escapeHtml(attack);
    assert(!out.includes('"'),
        `a raw double quote must not survive into an attribute, got ${out}`);
    // Simulate the exact sink in app.js.
    const rendered = `<button aria-label="Hide ${out} on map">`;
    assert((rendered.match(/"/g) || []).length === 2,
        `the attribute must still have exactly two quotes; got ${rendered}`);
    // The attribute value is everything between the 2nd and 3rd quote. The payload
    // must be entirely inside it — i.e. it must not have terminated the attribute
    // and started a new one.
    const parts = rendered.split('"');
    assert(parts.length === 3,
        `the tag must parse as exactly one quoted attribute; got ${parts.length - 1} quotes`);
    assert(parts[1].includes('onclick=alert(1)'),
        'the payload must remain INSIDE the attribute value as inert text');
    assert(!parts[2].includes('onclick'),
        'and nothing after the attribute may carry it as a real handler');
}

{
    // Real vessel names contain quotes and ampersands (BAIT & STITCH, O'BRIEN are
    // both in the archive), so cleanName must NOT mangle them — escaping at render
    // is the fix, not stripping at storage.
    const s = makeStorage();
    const V = load(s);
    V.learn('368309231', 'BAIT & STITCH');
    V.learn('368309232', "O'BRIEN");
    assert(V.get('368309231') === 'BAIT & STITCH',
        `an ampersand is a legitimate name character, got ${V.get('368309231')}`);
    assert(V.get('368309232') === "O'BRIEN",
        `an apostrophe is a legitimate name character, got ${V.get('368309232')}`);
}

// --- debounced writes on the AIS hot path -------------------------------
// learn() is called from vessel-store.js upsert() for every AIS message, and
// seek() re-ingests up to 150k lines synchronously in one frame. Serialising the
// whole map per learned name measured 72 ms of JSON.stringify and 11.1 MB of
// garbage over 800 learns — P37's coalescing problem, one layer down.
section('debounce');

{
    const s = makeStorage();
    let writes = 0;
    const orig = s.setItem.bind(s);
    s.setItem = (k, v) => { writes++; return orig(k, v); };
    const V = load(s);

    for (let i = 0; i < 300; i++) V.learn(String(300000000 + i), `BOAT ${i}`);
    assert(writes === 0,
        `300 learns must not write localStorage at all before the flush; got ${writes}`);
    V.flush();
    assert(writes === 1, `one flush must produce exactly one write; got ${writes}`);
    const V2 = load(s);
    assert(V2.get('300000299') === 'BOAT 299', 'and everything must be there afterwards');
}

{
    // A repeated identical name must not mark the map dirty forever — that is the
    // steady state during any replay.
    const s = makeStorage();
    const V = load(s);
    V.learn('368309230', 'FINAL FINAL');
    V.flush();
    assert(V.stats().pendingWrite === false, 'clean after a flush');
    for (let i = 0; i < 50; i++) V.learn('368309230', 'FINAL FINAL');
    assert(V.stats().pendingWrite === false,
        'relearning the same name must not dirty the map, or every message schedules a write');
}

// --- LEARNED is capped --------------------------------------------------
// Unbounded was the original design and it was wrong for P11's reason: the
// localStorage quota is SHARED with vessel-store.js, and this is the cosmetic
// consumer. Overflow would silently stop vessel tracks persisting.
section('cap');

{
    const s = makeStorage();
    const V = load(s);
    const cap = V.MAX_LEARNED;
    assert(Number.isFinite(cap) && cap > 0, 'there must be a cap at all');
    // Insert cap + 50, with the oldest clearly oldest.
    for (let i = 0; i < cap + 50; i++) {
        V.learn(String(300000000 + i).slice(0, 9), `B${i}`, 20000 + i);
    }
    assert(V.stats().learned <= cap,
        `learned must never exceed the cap; got ${V.stats().learned}`);
    // The most recent must survive; the least recent must be the ones dropped.
    assert(V.get(String(300000000 + cap + 49).slice(0, 9)) !== null,
        'the newest entry must survive eviction');
}

// --- displayName keeps odd MMSIs ---------------------------------------
{
    const s = makeStorage();
    const V = load(s);
    // Coast and base stations have identities that are not 9 digits, and the
    // decoder returns an integer so leading zeros are already gone. Rendering
    // several of those as an identical "Unknown vessel" on a collision-avoidance
    // display is worse than showing an odd number.
    assert(V.displayName({ mmsi: 2442800 }) === 'MMSI 2442800',
        `a non-9-digit MMSI must still be shown, got ${V.displayName({ mmsi: 2442800 })}`);
    assert(V.get('2442800') === null, 'but it is still not a valid STORAGE key');
}

// --- the two cleanName copies must agree (P20) --------------------------
{
    const builder = readFileSync(join(__dirname, '../tools/build_vessel_names.mjs'), 'utf8');
    const pick = (t) => {
        const m = /\.replace\(\/\[\\x00[^/]*\/g, ''\)/.exec(t);
        return m ? m[0] : null;
    };
    const a = pick(src), b = pick(builder);
    assert(a && b, 'both files must contain a control-character strip');
    assert(a === b,
        `vessel-names.js and build_vessel_names.mjs must strip identically (P20):\n`
        + `  runtime: ${a}\n  builder: ${b}`);
}

// --- P19: this module must never build HTML ------------------------------
section('P19');

assert(!/innerHTML/.test(src),
    'vessel-names.js must never touch innerHTML — every name here comes from '
    + 'fetched JSON or user paste, which are the two injection sinks P19 names');

// --- summary -------------------------------------------------------------
console.log(`\n${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
console.log('all passed');
