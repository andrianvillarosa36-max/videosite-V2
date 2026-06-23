const CACHE_NAME = 'animecorn-v1';
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
  // Only cache GET requests for static assets, never API calls or video streams
  if (event.request.method !== 'GET') return;
  if (event.request.url.includes('/api/')) return;

  event.respondWith(
    caches.match(event.request).then(function(cached) {
      return cached || fetch(event.request).catch(function() {
        return caches.match('/style.css');
      });
    })
  );
});
