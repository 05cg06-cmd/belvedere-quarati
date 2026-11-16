const CACHE_NAME = 'belvedere-pwa-v2';
const SHELL_ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './data.json'
];

self.addEventListener('install', event => {
  event.waitUntil(
    caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL_ASSETS)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET') return;

  // Network-first per data.json, index.html e root cosi' ogni aggiornamento arriva subito
  const isDynamic =
    url.pathname.endsWith('/data.json') ||
    url.pathname.endsWith('data.json') ||
    url.pathname.endsWith('/index.html') ||
    url.pathname.endsWith('/');

  if (isDynamic) {
    event.respondWith(
      fetch(event.request, { cache: 'no-store' })
        .then(resp => {
          if (resp && resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE_NAME).then(c => c.put(event.request, copy));
          }
          return resp;
        })
        .catch(() => caches.match(event.request))
    );
    return;
  }

  // Cache-first con fallback rete per icone e manifest
  event.respondWith(
    caches.match(event.request).then(cached => cached || fetch(event.request))
  );
});
