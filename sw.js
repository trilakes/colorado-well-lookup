/* Network-first so the page HTML is never served stale from cache. */
self.addEventListener('install', function (e) { self.skipWaiting(); });
self.addEventListener('activate', function (e) { e.waitUntil(self.clients.claim()); });
self.addEventListener('fetch', function (e) {
  var req = e.request;
  if (req.method !== 'GET') return;
  var accept = req.headers.get('accept') || '';
  var isDoc = req.mode === 'navigate' || accept.indexOf('text/html') !== -1;
  if (!isDoc) return; // let the browser handle CSS/JS/images normally
  e.respondWith(
    fetch(req, { cache: 'no-store' }).catch(function () { return caches.match(req); })
  );
});
