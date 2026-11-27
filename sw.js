// Watt Watch offline shell (v{{APP_VERSION}}). Lets the app open with no signal after a reload or a
// dead phone, showing the data from the last time it loaded. The server fills in the version, so every
// deploy changes this file and the browser installs the new worker.
//
// Network first, always: the cache is only used when the network fails or is too slow to answer.
// Only the page itself and four read-only API answers are kept. Never cached: any write, appointment
// requests (the public's details), files, the map, sign-in. The page deletes the data cache on sign-out
// and on a 401 (see clearOfflineCache() in generator-manager.html).
const VERSION = '{{APP_VERSION}}';
const SHELL_CACHE = 'ww-shell-' + VERSION;
const DATA_CACHE = 'ww-data';
const SHELL_PATHS = ['/', '/favicon.png'];
const DATA_PATHS = ['/api/me', '/api/customers', '/api/generators', '/api/checklist-versions'];
const SLOW_MS = 6000;  // after this, answer from the cache (if there is one) and let the network finish in the background

self.addEventListener('install', event => {
  event.waitUntil(caches.open(SHELL_CACHE).then(c => c.addAll(SHELL_PATHS)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name.startsWith('ww-shell-') && name !== SHELL_CACHE) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});

self.addEventListener('message', event => {
  if (event.data === 'clear-data') event.waitUntil(caches.delete(DATA_CACHE));
});

self.addEventListener('fetch', event => {
  const req = event.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  if (req.mode === 'navigate' && (url.pathname === '/' || url.pathname === '/generator-manager.html')) {
    event.respondWith(networkFirst(req, SHELL_CACHE, new Request('/')));
  } else if (SHELL_PATHS.includes(url.pathname)) {
    event.respondWith(networkFirst(req, SHELL_CACHE, req));
  } else if (DATA_PATHS.includes(url.pathname)) {
    event.respondWith(networkFirst(req, DATA_CACHE, req, true));
  }
});

async function networkFirst(req, cacheName, cacheKey, isData) {
  const cache = await caches.open(cacheName);
  const network = fetch(req).then(async res => {
    // Only good answers are kept; a 401 means signed out, so the cached data goes too.
    if (res.ok) await cache.put(cacheKey, await stamp(res.clone()));
    else if (isData && res.status === 401) await caches.delete(DATA_CACHE);
    return res;
  });
  const cached = await cache.match(cacheKey);
  if (!cached) return network;  // nothing to fall back on: let the page see the real failure
  const slow = new Promise(resolve => setTimeout(() => resolve('slow'), SLOW_MS));
  try {
    const winner = await Promise.race([network, slow]);
    // 502-504 come from Railway's proxy while the app restarts, not from the app: use the cache then too.
    if (winner !== 'slow') return winner.status >= 502 && winner.status <= 504 ? fromCache(cached) : winner;
    network.catch(() => {});
    return fromCache(cached);
  } catch (e) {
    return fromCache(cached);
  }
}

// When the answer was stored, so the page can say "showing data from 2:10 PM".
async function stamp(res) {
  const headers = new Headers(res.headers);
  headers.set('X-WW-Stored-At', new Date().toISOString());
  return new Response(await res.blob(), {status: res.status, statusText: res.statusText, headers});
}

async function fromCache(cached) {
  const headers = new Headers(cached.headers);
  headers.set('X-WW-From-Cache', '1');
  return new Response(await cached.blob(), {status: cached.status, statusText: cached.statusText, headers});
}
