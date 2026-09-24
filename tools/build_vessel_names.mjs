#!/usr/bin/env node
/**
 * Build static/vessel_names.json — the MMSI→name seed for the local database.
 *
 *   node tools/build_vessel_names.mjs [LOG_DIR] [-o OUT]
 *
 * Defaults to ~/Documents/typon-nmea-logs and static/vessel_names.json.
 *
 * WHY. AIS separates identity from position. Names ride in Type 5 (Class A,
 * ~every 6 min) or Type 24 (Class B, rarer), while positions arrive every 2-10
 * seconds — so a vessel is a bare MMSI until its next static broadcast, and in
 * replay it is worse, because seeking resets the store and names are lost at
 * every file boundary. A seed file makes them available immediately.
 *
 * WHY NODE, not Python. It reuses static/js/ais-decoder.js as-is. A second AIS
 * decoder written in Python would be two implementations that must agree
 * forever, which is exactly the trap P20 is filed against.
 *
 * Hand-known names come from tools/vessel_names_overrides.json and are merged LAST,
 * so they survive a rebuild and beat a garbled broadcast. That is the only durable
 * home for the 7% of vessels that never transmit a name — localStorage is
 * per-browser, and editing the generated file is wiped by the next build.
 *
 * The output is committed. It lives in static/ rather than data/ deliberately:
 * data/ is not in git and static/data/ is gitignored, so a seed placed there
 * would silently never reach the Pi. static/ is served directly by both GitHub
 * Pages and the Pi's add_static.
 */

import { readFileSync, readdirSync, writeFileSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const REPO = join(__dirname, '..');

const args = process.argv.slice(2);
let logDir = null;
let out = join(REPO, 'static', 'vessel_names.json');
for (let i = 0; i < args.length; i++) {
    if (args[i] === '-o' || args[i] === '--out') out = args[++i];
    else if (!args[i].startsWith('-')) logDir = args[i];
}
logDir = logDir || join(process.env.HOME, 'Documents', 'typon-nmea-logs');

// Load the real decoder rather than reimplementing it.
const decSrc = readFileSync(join(REPO, 'static', 'js', 'ais-decoder.js'), 'utf8');
const box = {};
new Function('module', decSrc + '\nmodule.AISDecoder = AISDecoder;')(box);
const AISDecoder = box.AISDecoder;

/**
 * Byte-identical to cleanName() in static/js/vessel-names.js.
 *
 * They diverged once already (\x7f vs \x7f-\x9f), so a name containing U+0085
 * survived the build and was altered at load — the P20 shape, two copies that
 * must agree. tests/test_vessel_names.mjs now asserts the two regexes match.
 * load() cleans again anyway, so this is about the committed file being honest
 * about its own contents, not about trust.
 */
function cleanName(raw) {
    if (typeof raw !== 'string') return null;
    const s = raw
        .replace(/@+/g, ' ')
        // eslint-disable-next-line no-control-regex
        .replace(/[\x00-\x1f\x7f-\x9f]/g, '')
        .replace(/[<>]/g, '')
        .replace(/\s+/g, ' ')
        .trim();
    return s.length ? s.slice(0, 64) : null;
}

let files;
try {
    files = readdirSync(logDir).filter((f) => f.startsWith('nmea_') && f.endsWith('.txt')).sort();
} catch (e) {
    console.error(`cannot read ${logDir}: ${e.message}`);
    process.exit(1);
}
if (!files.length) {
    console.error(`no nmea_*.txt in ${logDir}`);
    process.exit(1);
}

const names = new Map();       // mmsi -> name (last one wins; newest file last)
const positions = new Set();   // every mmsi we ever saw a position for
let lines = 0;

for (const f of files) {
    // latin1, not utf8: NMEA logs carry occasional non-UTF8 bytes and a strict
    // decode would throw or mangle the line. We only need the ASCII payload.
    const text = readFileSync(join(logDir, f), 'latin1');
    for (const line of text.split('\n')) {
        const i = line.indexOf('!AIVD');
        if (i < 0) continue;
        lines++;
        let v;
        try { v = AISDecoder.processSentence(line.slice(i).trim()); } catch (e) { continue; }
        if (!v || v.mmsi == null) continue;
        const m = String(v.mmsi);
        if (!/^\d{9}$/.test(m)) continue;
        if (v.lat != null) positions.add(m);
        const n = cleanName(v.name);
        if (n) names.set(m, n);
    }
    process.stderr.write(`\r${f}  (${names.size} names)          `);
}
process.stderr.write('\n');

// Merge hand-known names LAST, so they win over a garbled broadcast. 59 of the
// archive's 824 vessels never broadcast a name, and no amount of listening fixes
// that — see tools/vessel_names_overrides.json.
let overrides = 0;
try {
    const ov = JSON.parse(readFileSync(join(__dirname, 'vessel_names_overrides.json'), 'utf8'));
    for (const k of Object.keys(ov.names || {})) {
        if (!/^\d{9}$/.test(k)) {
            console.error(`  skipping override with malformed MMSI: ${k}`);
            continue;
        }
        const n = cleanName(ov.names[k]);
        if (!n) { console.error(`  skipping empty override for ${k}`); continue; }
        names.set(k, n);
        overrides++;
    }
} catch (e) {
    // Optional file. Absent is normal; malformed should be loud but not fatal.
    if (e.code !== 'ENOENT') console.error(`  overrides file unusable: ${e.message}`);
}

// Sorted keys so the committed file has a stable diff — otherwise every rebuild
// churns the whole thing and the history becomes unreadable.
const sorted = {};
for (const k of [...names.keys()].sort()) sorted[k] = names.get(k);

const namedWithPosition = [...positions].filter((m) => names.has(m)).length;
const payload = {
    generated: new Date().toISOString(),
    source_files: files.length,
    // Three DIFFERENT denominators, named so they cannot be conflated. Reporting
    // names_known against vessels_seen is what let "765 (92.8%)" and "774" both
    // circulate as the coverage figure.
    vessels_seen: positions.size,            // distinct MMSIs seen with a position
    named_with_position: namedWithPosition,  // ...of which we know a name  <- coverage
    never_named: positions.size - namedWithPosition,
    names_known: names.size,                 // total names, incl. static-only MMSIs
    hand_overrides: overrides,               // from tools/vessel_names_overrides.json
    names: sorted,
};
writeFileSync(out, JSON.stringify(payload, null, 1) + '\n');

const pct = (100 * namedWithPosition / (positions.size || 1)).toFixed(1);
console.log(`${files.length} files, ${lines.toLocaleString()} AIVDM lines`);
console.log(`${positions.size} vessels seen with a position`);
console.log(`  ${namedWithPosition} named (${pct}%), ${positions.size - namedWithPosition} never broadcast a name`);
console.log(`${names.size} names in the file (includes MMSIs seen without a position)`);
console.log(`  ${overrides} from hand overrides`);
console.log(`wrote ${out}`);
