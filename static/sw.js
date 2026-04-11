const CACHE_NAME = 'ais-tracker-v3';
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
      Promise.all(keys.filter((k) => k !== CACHE_NAME).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);

  // Never cache API calls or WebSocket upgrades
  if (url.pathname.startsWith('/api/') || url.pathname === '/ws') {
    return;
  }

  // Cache-first for tiles only (expensive to re-download)
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
