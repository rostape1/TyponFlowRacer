const CACHE_NAME = 'ais-tracker-v5';
const ENV_CACHE = 'ais-env-data-v1';
const TILE_CACHE = 'ais-tiles-v1';

// External tile CDN hosts to cache
const TILE_HOSTS = [
  'basemaps.cartocdn.com',
  'tile.openstreetmap.org',
  'gis.charttools.noaa.gov',
  'tiles.openseamap.org',
];
const ASSETS = [
  '/',
  '/static/css/style.css',
  '/static/js/app.js',
  '/static/js/tidal-flow.js',
  '/static/js/wind-overlay.js',
  '/static/lib/leaflet.js',
  '/static/lib/leaflet.css',
  '/static/lib/images/layers.png',
  '/static/lib/images/layers-2x.png',
  '/static/lib/images/marker-icon.png',
  '/static/lib/images/marker-icon-2x.png',
  '/static/lib/images/marker-shadow.png',
  '/static/manifest.json',
];

// Environmental API paths that should be cached for offline use
const ENV_API_PATHS = ['/api/currents', '/api/current-field', '/api/wind-field', '/api/tide-height'];

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
      Promise.all(keys.filter((k) => k !== CACHE_NAME && k !== ENV_CACHE && k !== TILE_CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// Handle messages from the app (e.g. clear env cache)
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'CLEAR_ENV_CACHE') {
    caches.delete(ENV_CACHE).then(() => {
      if (event.ports[0]) event.ports[0].postMessage({ cleared: true });
    });
  }
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never cache WebSocket upgrades or vessel API calls (real-time data)
  if (url.pathname === '/ws' || url.pathname.startsWith('/api/vessels')) {
    return;
  }

  // Environmental API endpoints: network-first with cache fallback for offline
  if (ENV_API_PATHS.some(p => url.pathname === p)) {
    event.respondWith(
      fetch(event.request).then((response) => {
        if (response.ok) {
          const clone = response.clone();
          caches.open(ENV_CACHE).then((cache) => cache.put(event.request, clone));
        }
        return response;
      }).catch(() => {
        // Offline — try cache
        return caches.open(ENV_CACHE).then((cache) =>
          cache.match(event.request).then((cached) => {
            if (cached) {
              // Clone and rebuild with a header so the app knows it's cached
              return cached.arrayBuffer().then((body) => {
                const headers = new Headers(cached.headers);
                headers.set('X-From-Cache', 'true');
                return new Response(body, {
                  status: cached.status,
                  statusText: cached.statusText,
                  headers: headers,
                });
              });
            }
            // No cached data available
            return new Response(JSON.stringify({ offline: true, error: 'No cached data available' }), {
              status: 503,
              headers: { 'Content-Type': 'application/json', 'X-From-Cache': 'true' },
            });
          })
        );
      })
    );
    return;
  }

  // External CDN tiles: cache-first (tiles don't change), network fallback
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

  // Cache-first for local tiles (expensive to re-download)
  if (url.pathname.startsWith('/static/tiles/')) {
    event.respondWith(
      caches.match(event.request).then((cached) => {
        if (cached) return cached;
        return fetch(event.request).then((response) => {
          if (response.ok) {
            const clone = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
          }
          return response;
        });
      })
    );
    return;
  }

  // Network-first for everything else (HTML, JS, CSS) — always get fresh code
  event.respondWith(
    fetch(event.request).then((response) => {
      if (response.ok && (url.pathname.startsWith('/static/') || url.pathname === '/')) {
        const clone = response.clone();
        caches.open(CACHE_NAME).then((cache) => cache.put(event.request, clone));
      }
      return response;
    }).catch(() => {
      // Offline fallback — serve from cache
      return caches.match(event.request);
    })
  );
});
