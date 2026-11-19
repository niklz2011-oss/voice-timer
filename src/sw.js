// Service worker: кэширует оболочку приложения и аудиофайлы для работы офлайн.
const CACHE = 'voice-timer-v6';
// Аудио кэшируется «навсегда» — при перегенерации фраз меняем номер, иначе останутся старые файлы.
const AUDIO_CACHE = 'voice-timer-audio-v2';
const ASSETS = [
  './',
  './index.html',
  './manifest.json',
  './icons/icon.svg',
  './icons/icon-192.png',
  './icons/icon-512.png',
  './icons/icon-maskable-512.png',
  './icons/apple-touch-icon.png',
];

self.addEventListener('install', (e) => {
  // cache: 'reload' — мимо HTTP-кэша, чтобы новая версия SW не сохранила старый index.html.
  e.waitUntil(
    caches.open(CACHE)
      .then((c) => c.addAll(ASSETS.map((u) => new Request(u, { cache: 'reload' }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  const keep = [CACHE, AUDIO_CACHE];
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => !keep.includes(k)).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

async function putIfOk(cacheName, req, res) {
  // Не кэшируем ошибки и «заглушки» SPA-хостинга (index.html вместо аудио).
  const html = (res.headers.get('content-type') || '').includes('text/html');
  if (res.ok && res.type !== 'opaque' && !html) await (await caches.open(cacheName)).put(req, res.clone());
  return res;
}

// Оболочка: отдаём из кэша сразу (работает офлайн), а в фоне обновляем кэш из сети.
async function shell(e) {
  const req = e.request;
  const cache = await caches.open(CACHE);
  const isNav = req.mode === 'navigate';
  const hit = await cache.match(req, { ignoreSearch: isNav }) || (isNav && await cache.match('./index.html'));
  const net = fetch(req)
    .then((res) => (res.ok && new URL(req.url).origin === self.location.origin ? putIfOk(CACHE, req, res) : res))
    .catch(() => null);
  if (hit) {
    e.waitUntil(net);
    return hit;
  }
  return (await net) || Response.error();
}

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  const isAudio = url.pathname.includes('/audio/');

  if (isAudio && url.pathname.endsWith('.json')) {
    // Списки голосов/фраз: сначала сеть (появляются новые голоса), офлайн — из кэша.
    // 404 с сервера отдаём как есть — страница покажет «аудиофайлы отсутствуют».
    e.respondWith(
      fetch(req).then((res) => putIfOk(AUDIO_CACHE, req, res))
        .catch(() => caches.match(req, { ignoreSearch: true }).then((hit) => hit || Response.error()))
    );
    return;
  }
  if (isAudio) {
    // Аудиофайлы неизменны: кэш, затем сеть с сохранением.
    e.respondWith(
      caches.match(req, { ignoreSearch: true })
        .then((hit) => hit || fetch(req).then((res) => putIfOk(AUDIO_CACHE, req, res)))
    );
    return;
  }
  e.respondWith(shell(e));
});

// Страница просит сохранить файлы голоса для офлайна (нужно после первого визита,
// когда аудио было загружено ещё без контроля service worker). Отвечает числом неудач.
self.addEventListener('message', (e) => {
  const data = e.data || {};
  if (data.type !== 'cache-audio' || !Array.isArray(data.urls)) return;
  const port = e.ports && e.ports[0];
  e.waitUntil((async () => {
    const cache = await caches.open(AUDIO_CACHE);
    let failed = 0, i = 0;
    const urls = data.urls.filter((u) => new URL(u).origin === self.location.origin);
    const worker = async () => {
      while (i < urls.length) {
        const u = urls[i++];
        const isJson = u.endsWith('.json');
        try {
          if (!isJson && await cache.match(u)) continue;
          const res = await fetch(u, { cache: isJson ? 'no-cache' : 'default' });
          const html = (res.headers.get('content-type') || '').includes('text/html');
          if (!res.ok || html) throw new Error(res.status);
          await cache.put(u, res);
        } catch (_) {
          // JSON без сети — не страшно, если он уже лежит в кэше.
          if (!(isJson && await cache.match(u))) failed++;
        }
      }
    };
    await Promise.all(Array.from({ length: 6 }, worker));
    if (port) port.postMessage({ failed });
  })());
});
