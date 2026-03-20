/**
 * MyDua.AI — Service Worker
 * ==========================
 * Provides offline caching for the mobile app.
 * Critical for Hajj/Umrah sites where cellular networks are unreliable.
 *
 * STRATEGY:
 * - App shell (HTML, CSS, JS, fonts): Cache-first (install-time)
 * - API responses: Network-first with fallback to cache
 * - Saved du'as & journey data: Cache on read, serve from cache when offline
 * - Audio files: Cache on first play, serve from cache thereafter
 * - CDN resources: Cache-first after first load
 */

const CACHE_VERSION = 'mydua-v2.0.0';
const SHELL_CACHE = `${CACHE_VERSION}-shell`;
const DATA_CACHE = `${CACHE_VERSION}-data`;
const AUDIO_CACHE = `${CACHE_VERSION}-audio`;

// ─── App Shell: cached at install time ───
const SHELL_ASSETS = [
  '/',
  '/index.html',
  // CDN dependencies (cache after first load via fetch handler)
  // 'https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js',
];

// ─── API routes that should be cached for offline access ───
const CACHEABLE_API_ROUTES = [
  '/api/saved/',       // saved du'as
  '/api/journey/',     // journey data (niyyah, phases)
  '/api/gift/',        // gift du'as
];

// ─── API routes that should NEVER be cached ───
const NO_CACHE_ROUTES = [
  '/api/generate-dua',         // always needs fresh AI generation
  '/api/generate-dua-stream',  // SSE streaming
  '/api/create-support-session', // Stripe
  '/api/stripe-webhook',
  '/api/track-',               // analytics
  '/api/auth/',                // authentication
];

// ─────────────────────────────────────
// INSTALL: cache the app shell
// ─────────────────────────────────────
self.addEventListener('install', (event) => {
  console.log('[SW] Installing MyDua.AI service worker');
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_ASSETS))
      .then(() => self.skipWaiting())
  );
});

// ─────────────────────────────────────
// ACTIVATE: clean up old caches
// ─────────────────────────────────────
self.addEventListener('activate', (event) => {
  console.log('[SW] Activating MyDua.AI service worker');
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys
          .filter((key) => key !== SHELL_CACHE && key !== DATA_CACHE && key !== AUDIO_CACHE)
          .map((key) => {
            console.log('[SW] Removing old cache:', key);
            return caches.delete(key);
          })
      ))
      .then(() => self.clients.claim())
  );
});

// ─────────────────────────────────────
// FETCH: route-aware caching strategy
// ─────────────────────────────────────
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  const path = url.pathname;

  // Skip non-GET requests (POST for du'a generation, etc.)
  if (event.request.method !== 'GET') {
    return;
  }

  // Skip routes that should never be cached
  if (NO_CACHE_ROUTES.some((route) => path.startsWith(route))) {
    return;
  }

  // ─── Audio files: cache-first after first play ───
  if (path.endsWith('.mp3') || path.endsWith('.wav') || path.includes('/api/tts')) {
    event.respondWith(audioCacheFirst(event.request));
    return;
  }

  // ─── Cacheable API data: network-first, fallback to cache ───
  if (CACHEABLE_API_ROUTES.some((route) => path.startsWith(route))) {
    event.respondWith(networkFirstWithCache(event.request, DATA_CACHE));
    return;
  }

  // ─── CDN resources: cache-first after first load ───
  if (url.hostname === 'cdnjs.cloudflare.com' || url.hostname === 'fonts.googleapis.com' || url.hostname === 'fonts.gstatic.com') {
    event.respondWith(cacheFirstWithNetwork(event.request, SHELL_CACHE));
    return;
  }

  // ─── App shell: cache-first ───
  event.respondWith(cacheFirstWithNetwork(event.request, SHELL_CACHE));
});

// ─────────────────────────────────────
// STRATEGIES
// ─────────────────────────────────────

/**
 * Cache-first: try cache, fall back to network (and update cache).
 * Used for: app shell, CDN resources, fonts.
 */
async function cacheFirstWithNetwork(request, cacheName) {
  const cached = await caches.match(request);
  if (cached) {
    // Background refresh so next load gets latest
    refreshCache(request, cacheName);
    return cached;
  }

  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    // If both cache and network fail, return offline fallback
    return offlineFallback();
  }
}

/**
 * Network-first: try network, fall back to cache.
 * Used for: API data (saved du'as, journey data).
 */
async function networkFirstWithCache(request, cacheName) {
  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(cacheName);
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    const cached = await caches.match(request);
    if (cached) {
      return cached;
    }
    return new Response(
      JSON.stringify({ error: 'You are offline and this data is not cached yet.' }),
      { status: 503, headers: { 'Content-Type': 'application/json' } }
    );
  }
}

/**
 * Audio cache-first: cache audio after first play for offline Hajj use.
 * Audio files can be large, so we limit cache size.
 */
async function audioCacheFirst(request) {
  const cached = await caches.match(request);
  if (cached) {
    return cached;
  }

  try {
    const response = await fetch(request);
    if (response.ok) {
      const cache = await caches.open(AUDIO_CACHE);
      // Limit audio cache to ~50 entries (trim oldest if exceeded)
      const keys = await cache.keys();
      if (keys.length > 50) {
        await cache.delete(keys[0]);
      }
      cache.put(request, response.clone());
    }
    return response;
  } catch (err) {
    return new Response('Audio not available offline', { status: 503 });
  }
}

/**
 * Background cache refresh (stale-while-revalidate pattern).
 */
function refreshCache(request, cacheName) {
  fetch(request)
    .then((response) => {
      if (response.ok) {
        caches.open(cacheName).then((cache) => cache.put(request, response));
      }
    })
    .catch(() => { /* silently fail — cached version is fine */ });
}

/**
 * Offline fallback page.
 */
function offlineFallback() {
  return new Response(`
    <!DOCTYPE html>
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>MyDua.AI — Offline</title>
      <style>
        body {
          background: #0a0a0f; color: #e8dcc8; font-family: 'Cormorant Garamond', Georgia, serif;
          display: flex; align-items: center; justify-content: center; min-height: 100vh;
          margin: 0; padding: 24px; text-align: center;
        }
        h1 { color: #8b6914; font-size: 1.5rem; margin-bottom: 0.5rem; }
        p { font-size: 1.1rem; line-height: 1.6; max-width: 400px; }
      </style>
    </head>
    <body>
      <div>
        <h1>You are offline</h1>
        <p>MyDua.AI needs an internet connection to generate new du'as.
        Your saved du'as and journey journal are available offline once they've been loaded at least once.</p>
        <p style="margin-top: 2rem; font-style: italic; color: #8b6914;">
          "And when My servants ask you about Me — indeed I am near."
          <br>— Quran 2:186
        </p>
      </div>
    </body>
    </html>
  `, {
    status: 200,
    headers: { 'Content-Type': 'text/html' },
  });
}

// ─────────────────────────────────────
// PUSH NOTIFICATIONS (daily check-in)
// ─────────────────────────────────────
self.addEventListener('push', (event) => {
  const data = event.data ? event.data.json() : {};

  const title = data.title || 'MyDua.AI';
  const options = {
    body: data.body || 'How is your heart today?',
    icon: '/icons/icon-192x192.png',
    badge: '/icons/badge-72x72.png',
    tag: data.tag || 'daily-checkin',
    data: {
      url: data.url || '/',
    },
    // Vibration pattern: gentle double-tap
    vibrate: [100, 50, 100],
  };

  event.waitUntil(
    self.registration.showNotification(title, options)
  );
});

// Handle notification click — open the app
self.addEventListener('notificationclick', (event) => {
  event.notification.close();

  const targetUrl = event.notification.data?.url || '/';

  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      .then((clients) => {
        // Focus existing window if open
        for (const client of clients) {
          if (client.url.includes('mydua') && 'focus' in client) {
            return client.focus();
          }
        }
        // Otherwise open new window
        return self.clients.openWindow(targetUrl);
      })
  );
});
