const CACHE_NAME = 'animecorn-v2';
const STATIC_ASSETS = [
  '/style.css',
  '/manifest.json',
  '/icon-512.png'
];

self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(CACHE_NAME).then(function(cache) {
      return cache.addAll(STATIC_ASSETS);
    })
  );
});

self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(
        keys.filter(function(k) { return k !== CACHE_NAME; })
            .map(function(k) { return caches.delete(k); })
      );
    })
  );
});

self.addEventListener('fetch', function(event) {
  // Never intercept page navigations - always hit the live server for HTML
  if (event.request.mode === 'navigate') return;

  const url = event.request.url;
  const isStaticAsset = STATIC_ASSETS.some(function(asset) {
    return url.endsWith(asset);
  });

  if (!isStaticAsset) return; // let everything else (API, videos, other JS) go straight to network

  event.respondWith(
    caches.match(event.request).then(function(cached) {
      return cached || fetch(event.request);
    })
  );
});
