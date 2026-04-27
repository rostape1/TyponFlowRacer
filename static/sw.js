const CACHE_NAME = 'ais-tracker-v10';
const DATA_CACHE = 'ais-data-v2';
const TILE_CACHE = 'ais-tiles-v1';

// External tile CDN hosts to cache
const TILE_HOSTS = [
  'basemaps.cartocdn.com',
  'tile.openstreetmap.org',
  'gis.charttools.noaa.gov',
  'tiles.openseamap.org',
  'cdn.jsdelivr.net',
];

// External environmental API hosts to cache for offline
const ENV_API_HOSTS = [
  'api.tidesandcurrents.noaa.gov',
  'api.open-meteo.com',
];

const ASSETS = [
  './',
  'css/style.css',
  'js/app.js',
  'js/tidal-flow.js',
  'js/wind-overlay.js',
  'js/aisstream.js',
  'js/vessel-store.js',
  'js/data-loader.js',
  'lib/leaflet.js',
  'lib/leaflet.css',
  'lib/images/layers.png',
  'lib/images/layers-2x.png',
  'lib/images/marker-icon.png',
  'lib/images/marker-icon-2x.png',
  'lib/images/marker-shadow.png',
  'manifest.json',
];

// Cache static assets on install
self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => cache.addAll(ASSETS))
  );
  self.skipWaiting();
});

// Clean old caches on activate
self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== CACHE_NAME && k !== DATA_CACHE && k !== TILE_CACHE)
          .map((k) => caches.delete(k))
      )
    )
  );
  self.clients.claim();
});

// Stale-while-revalidate flag for downloaded data
let _dataCacheFirst = false;

self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'CLEAR_ENV_CACHE') {
    _dataCacheFirst = false;
    caches.delete(DATA_CACHE).then(() => {
      if (event.ports[0]) event.ports[0].postMessage({ cleared: true });
    });
  }
  if (event.data && event.data.type === 'ENV_CACHE_READY') {
    _dataCacheFirst = true;
  }
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Data JSON files (data/**/*.json) — stale-while-revalidate when downloaded
  if (url.pathname.includes('/data/') && url.pathname.endsWith('.json')) {
    if (_dataCacheFirst) {
      event.respondWith(
        caches.open(DATA_CACHE).then((cache) =>
          cache.match(event.request).then((cached) => {
            // Background refresh
            const networkFetch = fetch(event.request).then((response) => {
              if (response.ok) cache.put(event.request, response.clone());
              return response;
            }).catch(() => null);

            if (cached) {
              networkFetch; // fire-and-forget
              return cached;
            }
            return networkFetch.then((resp) => {
              if (resp) return resp;
              return new Response('{"error":"offline"}', {
                status: 503,
                headers: { 'Content-Type': 'application/json' },
              });
            });
          })
        )
      );
    } else {
      // Network-first with cache fallback
      event.respondWith(
        fetch(event.request).then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(DATA_CACHE).then((cache) => cache.put(event.request, clone));
          }
          return response;
        }).catch(() =>
          caches.open(DATA_CACHE).then((cache) =>
            cache.match(event.request).then((cached) => {
              if (cached) return cached;
              return new Response('{"error":"offline"}', {
                status: 503,
                headers: { 'Content-Type': 'application/json' },
              });
            })
          )
        )
      );
    }
    return;
  }

  // External environmental APIs — same strategy as data JSON
  if (ENV_API_HOSTS.some(h => url.hostname === h)) {
    if (_dataCacheFirst) {
      event.respondWith(
        caches.open(DATA_CACHE).then((cache) =>
          cache.match(event.request).then((cached) => {
            const networkFetch = fetch(event.request).then((response) => {
              if (response.ok) cache.put(event.request, response.clone());
              return response;
            }).catch(() => null);

            if (cached) {
              networkFetch;
              return cached;
            }
            return networkFetch.then((resp) => {
              if (resp) return resp;
              return new Response('{"error":"offline"}', {
                status: 503,
                headers: { 'Content-Type': 'application/json' },
              });
            });
          })
        )
      );
    } else {
      event.respondWith(
        fetch(event.request).then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(DATA_CACHE).then((cache) => cache.put(event.request, clone));
          }
          return response;
        }).catch(() =>
          caches.open(DATA_CACHE).then((cache) =>
            cache.match(event.request).then((cached) => {
              if (cached) return cached;
              return new Response('{"error":"offline"}', {
                status: 503,
                headers: { 'Content-Type': 'application/json' },
              });
            })
          )
        )
      );
    }
    return;
  }

  // External CDN tiles: cache-first (tiles don't change)
  if (TILE_HOSTS.some(h => url.hostname.endsWith(h))) {
    event.respondWith(
      caches.open(TILE_CACHE).then((cache) =>
        cache.match(event.request).then((cached) => {
          if (cached) return cached;
          return fetch(event.request).then((response) => {
            if (response.ok) cache.put(event.request, response.clone());
            return response;
          }).catch(() => new Response('', { status: 404 }));
        })
      )
    );
    return;
  }

  // Network-first for everything else (HTML, JS, CSS)
  event.respondWith(
    fetch(event.request).then((response) => {
      if (response.ok && url.origin === self.location.origin) {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
      }
      return response;
    }).catch(() => caches.match(event.request))
  );
});
