#!/usr/bin/env node
/**
 * Cross-file invariants — the traps that live in the gap between two files.
 *
 * These promote four pitfalls from documentation-only to mechanically enforced.
 * Each one shipped a real bug; see docs/pitfalls.md by ID.
 *
 *   P15  Local tiles exist only at z10-15, and the layers must clamp to that
 *        range or zooming past 15 paints a black canvas. The range lives in
 *        download_offline.py; the clamp lives in app.js. Nothing connected them.
 *   P16  Service Worker quota eviction must never target CACHE_NAME (the app
 *        shell) and must drop the FARTHEST forecast hours from DATA_CACHE, not
 *        the oldest — insertion order is ascending hour, so "oldest" is the
 *        hours being sailed.
 *   P18  Tile host matching must be exact or a true subdomain. A bare
 *        endsWith() also matches evilservices.arcgisonline.com.
 *   P24  The Pi's port drifted across five files; the smoke test defaulted to a
 *        scheme and port the server never used and failed all ten of its checks.
 *   P43  Every relative path in sw.js's ASSETS must exist on disk. install does
 *        cache.addAll(ASSETS), which rejects as a UNIT, so one missing file means
 *        the Service Worker never activates and ALL offline capability is lost —
 *        silently, because everything looks normal while online.
 *
 * P16 and P18 are functional: sw.js is loaded in a sandbox with a fake Cache API
 * and its real functions are called. P15 and P24 are source assertions, because
 * the thing being checked IS agreement between two files' literals.
 *
 * Run: node tests/test_invariants.mjs      (no network, no deps)
 */

import { readFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (p) => readFileSync(join(ROOT, p), 'utf8');

let failed = 0;
const ok = (label, cond, detail = '') => {
  if (cond) console.log(`  ok   ${label}`);
  else { console.log(`  FAIL ${label}${detail ? ` — ${detail}` : ''}`); failed++; }
};
const eq = (label, a, b) => ok(label, a === b, `got ${JSON.stringify(a)}, want ${JSON.stringify(b)}`);
const section = (t) => console.log(`${t}:`);

// ─────────────────────────────────────────────────────────────────────────────
// Load sw.js in a sandbox with a fake Cache API.
// ─────────────────────────────────────────────────────────────────────────────

class FakeCache {
  constructor(urls = []) { this.entries = urls.map((url) => ({ url })); }
  async keys() { return this.entries.slice(); }          // insertion order, like the real Cache API
  async delete(key) {
    const i = this.entries.indexOf(key);
    if (i === -1) return false;
    this.entries.splice(i, 1);
    return true;
  }
  async put() { /* not exercised here */ }
  async match() { return undefined; }
  urls() { return this.entries.map((e) => e.url); }
}

function loadSw(caches) {
  const src = read('static/sw.js');
  const self = {
    addEventListener() {},
    skipWaiting() {},
    clients: { claim() {}, async matchAll() { return []; } },
  };
  // Expose the internals under test. sw.js is a classic script, not a module.
  const exportList = [
    'isTileHost', 'isEnvApiHost', 'evictForQuota', 'evictOldestTiles',
    'evictFarthestForecast', 'CACHE_NAME', 'DATA_CACHE', 'TILE_CACHE', 'TILE_HOSTS',
  ];
  const factory = new Function(
    'self', 'caches', 'Response', 'fetch', 'URL',
    `${src}\n;return {${exportList.join(',')}};`
  );
  return factory(self, caches, class {}, () => {}, URL);
}

// ─────────────────────────────────────────────────────────────────────────────
section('P18 — tile host matching (functional)');
// ─────────────────────────────────────────────────────────────────────────────
{
  const sw = loadSw({ async open() { return new FakeCache(); } });

  for (const h of sw.TILE_HOSTS) ok(`accepts the exact host ${h}`, sw.isTileHost(h));
  ok('accepts a true subdomain (OSM a./b./c.)', sw.isTileHost('a.tile.openstreetmap.org'));
  ok('accepts a deep subdomain', sw.isTileHost('x.y.services.arcgisonline.com'));

  // The bug: endsWith() alone matched these.
  for (const evil of [
    'evilservices.arcgisonline.com',
    'nottile.openstreetmap.org',
    'services.arcgisonline.com.attacker.test',
    'xcdn.jsdelivr.net',
  ]) ok(`rejects ${evil}`, !sw.isTileHost(evil), 'a bare endsWith() would match this');

  ok('rejects an unrelated host', !sw.isTileHost('example.test'));
  ok('rejects the empty host', !sw.isTileHost(''));

  // Env API hosts are exact-only by design — no subdomain wildcard at all.
  ok('env API accepts its exact host', sw.isEnvApiHost('api.open-meteo.com'));
  ok('env API rejects a subdomain', !sw.isEnvApiHost('evil.api.open-meteo.com'));
  ok('env API rejects a suffix match', !sw.isEnvApiHost('notapi.open-meteo.com'));
}

// ─────────────────────────────────────────────────────────────────────────────
section('P16 — quota eviction never touches the app shell (functional)');
// ─────────────────────────────────────────────────────────────────────────────
{
  // The app shell, in install order: the oldest entries are the ones that matter.
  const shell = new FakeCache([
    'https://x.test/lib/leaflet.js', 'https://x.test/js/app.js',
    'https://x.test/css/style.css', 'https://x.test/manifest.json',
    'https://x.test/js/router.js',
  ]);
  // Data cache in real insertion order: ascending forecast hour.
  const data = new FakeCache([
    ...Array.from({ length: 10 }, (_, i) => `https://x.test/data/sfbofs/hour_${String(i).padStart(2, '0')}.json`),
    'https://x.test/data/meta.json',
    ...Array.from({ length: 10 }, (_, i) => `https://x.test/data/sfbofs/hour_${39 + i}.json`),
  ]);
  const tiles = new FakeCache(Array.from({ length: 20 }, (_, i) => `https://tile.test/${i}.png`));

  const byName = { 'ais-tracker-dev': shell, 'ais-data-v10': data, 'ais-tiles-v2': tiles };
  const sw = loadSw({ async open(name) { return byName[name] || new FakeCache(); } });

  const shellBefore = shell.urls().length;
  const tilesBefore = tiles.urls().length;

  // Quota hit while writing to the APP SHELL cache — the original bug's trigger.
  await sw.evictForQuota(sw.CACHE_NAME);
  eq('app shell is untouched when the shell cache is full', shell.urls().length, shellBefore);
  ok('app shell still has leaflet.js', shell.urls().some((u) => u.endsWith('leaflet.js')));
  ok('app shell still has app.js', shell.urls().some((u) => u.endsWith('app.js')));
  ok('space is freed from the tile cache instead', tiles.urls().length < tilesBefore);

  // Quota hit while writing DATA: tiles are freed AND the farthest hours go.
  const dataBefore = data.urls().length;
  await sw.evictForQuota(sw.DATA_CACHE);
  ok('data cache shrank', data.urls().length < dataBefore);
  const hours = data.urls()
    .map((u) => /hour_(\d+)/.exec(u))
    .filter(Boolean).map((m) => parseInt(m[1], 10));
  ok('hour_00 survived — it is the hour being sailed', hours.includes(0));
  ok('hour_01 survived', hours.includes(1));
  ok('the farthest hour (48) was evicted first', !hours.includes(48));
  ok('meta.json survived (ranks as hour 0, kept last)',
     data.urls().some((u) => u.endsWith('meta.json')));
  eq('app shell STILL untouched after a data eviction', shell.urls().length, shellBefore);
}

// ─────────────────────────────────────────────────────────────────────────────
section('P15 — local tile zoom range agrees with what is downloaded');
// ─────────────────────────────────────────────────────────────────────────────
{
  const appJs = read('static/js/app.js');
  const dl = read('download_offline.py');

  const minZ = Number(/const LOCAL_TILE_MIN_Z = (\d+);/.exec(appJs)?.[1]);
  const maxZ = Number(/const LOCAL_TILE_MAX_Z = (\d+);/.exec(appJs)?.[1]);
  const range = /DEFAULT_ZOOM_RANGE\s*=\s*\((\d+),\s*(\d+)\)/.exec(dl);

  ok('app.js declares LOCAL_TILE_MIN_Z / MAX_Z', Number.isInteger(minZ) && Number.isInteger(maxZ));
  ok('download_offline.py declares DEFAULT_ZOOM_RANGE', !!range);
  eq('min zoom matches what the downloader fetches', minZ, Number(range?.[1]));
  eq('max zoom matches what the downloader fetches', maxZ, Number(range?.[2]));

  // The clamp has to be applied, not merely declared: Leaflet's default for
  // both is null (no clamping), which is what produced the black canvas.
  ok('the clamp is wired into layer options via minNativeZoom/maxNativeZoom',
     /minNativeZoom:\s*LOCAL_TILE_MIN_Z/.test(appJs) && /maxNativeZoom:\s*LOCAL_TILE_MAX_Z/.test(appJs));
  const optsFn = /function _nativeZoomOpts[\s\S]{0,400}?\n}/.exec(appJs)?.[0] || '';
  ok('_nativeZoomOpts only clamps when serving from disk', /\?/.test(optsFn) && /null/.test(optsFn),
     'CDN mode must stay unclamped or high zoom breaks on GitHub Pages too');

  // localhost must NOT be treated as boat mode, or local dev shows a blank map.
  const fromDisk = /function _serveTilesFromDisk\(\)[\s\S]*?\n}/.exec(appJs)?.[0] || '';
  ok('_serveTilesFromDisk exists', fromDisk.length > 0);
  ok('localhost is excluded from disk-tile mode', /localhost/.test(fromDisk));
  ok('127.0.0.1 is excluded', /127\.0\.0\.1/.test(fromDisk));
  ok('github.io is excluded', /github\.io/.test(fromDisk));
  ok('boat mode is decided by APP_CONFIG first', /APP_CONFIG|cfg\.mode/.test(fromDisk));
}

// ─────────────────────────────────────────────────────────────────────────────
section('P24 — the Pi port and scheme agree across every file that names them');
// ─────────────────────────────────────────────────────────────────────────────
{
  const boatServer = read('pi/boat_server.py');
  const startBoat = read('start_boat.sh');
  const startup = read('pi/startup.sh');
  const smoke = read('pi/test_boat_server.sh');
  const capture = read('nmea_capture.py');
  const claude = read('CLAUDE.md');

  const PORT = 8080;

  const argPort = /--port["']?,\s*type=int,\s*default=(\d+)/.exec(boatServer)
               || /"--port"[\s\S]{0,120}?default=(\d+)/.exec(boatServer);
  eq('boat_server.py --port default', Number(argPort?.[1]), PORT);

  eq('start_boat.sh PORT default', Number(/PORT=\$\{PORT:-(\d+)\}/.exec(startBoat)?.[1]), PORT);
  ok('startup.sh does not hardcode a different port than it launches',
     !/localhost:(?!8080)\d{4}/.test(startup), 'found a port literal that is not 8080');

  // The smoke test has to default to a host:port the server actually serves,
  // and must not hardcode https:// — that is what failed all ten checks.
  // Assert on the TARGET default itself, not on "8080 appears somewhere": the
  // header comment also contains the URL, so a loose match passes even when the
  // real default has drifted.
  const target = /TARGET="\$\{1:-([^}"]+)\}"/.exec(smoke)?.[1];
  eq('smoke test TARGET default', target, `http://localhost:${PORT}`);
  ok('smoke test derives the scheme rather than hardcoding https',
     /BASE="\$TARGET"/.test(smoke) && /BASE="http:\/\/\$\{TARGET\}"/.test(smoke),
     'must accept http://, https:// or a bare host:port');
  ok('smoke test header comment agrees with its default',
     new RegExp(`http://localhost:${PORT}`).test(smoke.split('\n').slice(0, 12).join('\n')));

  // nmea_capture talks to the boat server over ws://, and its own status page
  // must not collide with the server's port.
  const wsUrl = /--ws-url[\s\S]{0,200}?default=["'](ws[s]?:\/\/[^"']+)["']/.exec(capture)?.[1] || '';
  ok('nmea_capture --ws-url default is ws:// on 8080', /^ws:\/\/[^/]*:8080\//.test(wsUrl), wsUrl || '(not found)');
  const webPort = Number(/--web-port[\s\S]{0,160}?default=(\d+)/.exec(capture)?.[1]);
  ok('nmea_capture --web-port does not collide with the boat server',
     Number.isInteger(webPort) && webPort !== PORT, `got ${webPort}`);

  // 8443 may only appear as the optional-TLS path, never as a default.
  ok('8443 is not a default anywhere in boat_server.py',
     !/default=8443/.test(boatServer));
  ok('CLAUDE.md ports table claims 8080', /\*\*8080\*\*/.test(claude));
}

// ─────────────────────────────────────────────────────────────────────────────
section('P22 — CI actually gates the code the tests cover');
// ─────────────────────────────────────────────────────────────────────────────
{
  const wf = read('.github/workflows/deploy.yml');

  // Parse the push `paths:` list from raw text — PyYAML/js-yaml both fold the
  // `on:` key to boolean true (YAML 1.1), so a real parse is more trouble here.
  const pathsBlock = /paths:\s*\n((?:\s*-\s*['"][^'"]+['"]\s*\n)+)/.exec(wf)?.[1] || '';
  const paths = [...pathsBlock.matchAll(/-\s*['"]([^'"]+)['"]/g)].map((m) => m[1]);

  ok('deploy.yml declares a push paths filter', paths.length > 0);
  for (const needed of ['static/**', 'tests/**', 'pi/**', 'download_offline.py', 'nmea_*.py']) {
    ok(`CI triggers on ${needed}`, paths.includes(needed),
       `a change to ${needed} would land without CI (paths: ${paths.join(', ')})`);
  }

  // Every test file that can run headlessly must have a step. If you add a
  // suite and forget this, it silently never runs — which is how the two
  // "Fix flaky staleness test" commits landed unreviewed.
  for (const t of ['tests/test_physics.mjs', 'tests/test_staleness.mjs',
                   'tests/test_invariants.mjs', 'tests/test_boat_server.py']) {
    ok(`CI runs ${t}`, wf.includes(t));
  }
  ok('CI does NOT run the network-dependent route test',
     !wf.includes('tests/test_route.mjs'), 'test_route.mjs needs live SFBOFS + Open-Meteo');
  ok('CI byte-compiles the Pi server', /py_compile[^\n]*pi\/boat_server\.py/.test(wf));
  ok('CI syntax-checks the boat shell scripts', /bash -n[^\n]*start_boat\.sh/.test(wf));
}

// ─────────────────────────────────────────────────────────────────────────────
// P43 — every precached asset must actually exist.
//
// install() is `caches.open(CACHE_NAME).then((c) => c.addAll(ASSETS))`, and
// addAll rejects as a unit: ONE 404 and the Service Worker never activates, so
// the app shell, DATA_CACHE and TILE_CACHE are all gone. It looks completely
// normal online and is discovered offshore.
//
// This nearly shipped on 2026-09-24: a generated file (vessel_names.json) was
// added to ASSETS while still untracked in git, so the deploy would have served
// a 404 for it to every client. Five review agents found it; this check would
// have found it in 0.2 seconds. Prefer the check.
// ─────────────────────────────────────────────────────────────────────────────
section('sw.js precache manifest (P43)');
{
  const src = read('static/sw.js');
  const block = /const ASSETS\s*=\s*\[([\s\S]*?)\]/.exec(src);
  ok('ASSETS array is parseable', !!block);
  if (block) {
    const paths = [...block[1].matchAll(/['"]([^'"]+)['"]/g)].map((m) => m[1]);
    ok('ASSETS is non-empty', paths.length > 0, `found ${paths.length}`);
    for (const rel of paths) {
      if (/^(https?:)?\/\//.test(rel)) continue;         // absolute URL, not ours to check
      if (rel === './' || rel === '.') continue;          // the page itself
      let exists = true;
      try { readFileSync(join(ROOT, 'static', rel)); } catch (e) { exists = false; }
      ok(`precached asset exists: ${rel}`, exists,
        'addAll() rejects as a unit — a missing file kills the whole Service Worker');
      // On disk is not enough: the deploy is a fresh checkout, so an UNTRACKED
      // file is a 404 in production while passing every local check. That is
      // exactly how this nearly shipped.
      let tracked = true;
      try {
        execFileSync('git', ['ls-files', '--error-unmatch', `static/${rel}`],
          { cwd: ROOT, stdio: 'ignore' });
      } catch (e) { tracked = false; }
      ok(`precached asset is tracked in git: ${rel}`, tracked,
        'a fresh deploy checkout would 404 this and the Service Worker would never install');
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
console.log();
console.log(failed ? `${failed} failed` : 'all passed');
if (failed) process.exit(1);
