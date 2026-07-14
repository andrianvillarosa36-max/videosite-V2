const CACHE_NAME = 'animecorn-v23';
const STATIC_ASSETS = [
  '/style.css',
  '/manifest.json',
  '/icon-512.png',
  '/index.html',
  '/adult.html',
  '/profile.html',
  '/login.html'
];

const API_CACHE_NAME = 'animecorn-api-v1';
const CACHEABLE_API_PATHS = ['/api/videos'];

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
  const url = event.request.url;

  // Page navigations: try network first, fall back to cached shell if offline
  if (event.request.mode === 'navigate') {
    event.respondWith(
      fetch(event.request).catch(function() {
        return caches.match(event.request).then(function(cached) {
          return cached || caches.match('/index.html');
        });
      })
    );
    return;
  }

  // Cacheable read-only API calls: network first, cache fallback when offline
  const isCacheableApi = CACHEABLE_API_PATHS.some(function(p) {
    return url.includes(p);
  });
  if (isCacheableApi && event.request.method === 'GET') {
    event.respondWith(
      fetch(event.request).then(function(response) {
        const clone = response.clone();
        caches.open(API_CACHE_NAME).then(function(cache) {
          cache.put(event.request, clone);
        });
        return response;
      }).catch(function() {
        return caches.match(event.request, { cacheName: API_CACHE_NAME });
      })
    );
    return;
  }

  // Static assets: cache first
  const isStaticAsset = STATIC_ASSETS.some(function(asset) {
    return url.endsWith(asset);
  });
  if (isStaticAsset) {
    event.respondWith(
      caches.match(event.request).then(function(cached) {
        return cached || fetch(event.request);
      })
    );
    return;
  }

  // Everything else (other API calls, video streams) - straight to network, no interception
});

