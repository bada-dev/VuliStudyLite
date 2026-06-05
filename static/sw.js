const CACHE_NAME = 'vulistudy-v3';
const SHELL_ASSETS = [
  '/',
  '/static/VuliStudyImg.png',
  '/static/manifest.json',
  'https://cdn.jsdelivr.net/npm/chart.js@4.4.4/dist/chart.umd.min.js'
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache => {
      return cache.addAll(SHELL_ASSETS);
    }).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
    ).then(() => clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // API calls:always go to network, never cache
  const apiPaths = ['/leaderboard', '/sync-score', '/set-username', '/check-password',
                    '/check-active', '/rejoin', '/feedback-cooldown', '/set-feedback-cooldown', '/delete-user'];
  if (apiPaths.some(p => url.pathname === p)) {
    e.respondWith(fetch(e.request).catch(() => new Response(JSON.stringify({error:'offline'}), {
      status: 503,
      headers: {'Content-Type': 'application/json'}
    })));
    return;
  }
  // App shell + static assets: cache first
  e.respondWith(
    caches.match(e.request).then(cached => {
      if (cached) return cached;
      return fetch(e.request).then(response => {
        if (!response || response.status !== 200 || response.type === 'error') return response;
        if (e.request.method !== 'GET') return response;
        const toCache = response.clone();
        caches.open(CACHE_NAME).then(cache => cache.put(e.request, toCache));
        return response;
      }).catch(() => {
        // If it's a navigation request (page load), then do the cool return the cached shell
        if (e.request.mode === 'navigate') {
          return caches.match('/');
        }
        return new Response('Offline', { status: 503 });
      });
    })
  );
});
