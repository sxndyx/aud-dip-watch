/* Service worker: receives pushes while the browser is closed and shows the notification. */
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));

self.addEventListener('push', event => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch { d = { body: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(d.title || 'AUD Dip Watch', {
    body: d.body || '',
    tag: d.tag || 'rate',
    renotify: true,
    icon: 'icon-512.png',
    badge: 'icon-512.png',
    data: { url: d.url || '/' },
  }));
});

// tapping the notification focuses an open tab, or opens the app
self.addEventListener('notificationclick', event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
    for (const c of list) if ('focus' in c) return c.focus();
    return clients.openWindow(url);
  }));
});
