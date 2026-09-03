// Service Worker for Web Push notifications
// Handles push events when the tab is not focused or browser is in background

self.addEventListener('push', (event) => {
  if (!event.data) return

  let data
  try {
    data = event.data.json()
  } catch {
    data = { title: 'Notification', body: event.data.text() }
  }

  const options = {
    body: data.body || '',
    icon: '/logo-192.png',
    badge: '/badge-72.png',
    tag: data.delivery_id || `notif-${Date.now()}`,
    data: {
      url: data.click_url || '/',
      severity: data.severity || 'info',
      delivery_id: data.delivery_id,
    },
    // Danger notifications require interaction (stay until dismissed)
    requireInteraction: data.severity === 'danger',
    // Info notifications are silent (app handles sound when visible)
    silent: data.severity === 'info',
    vibrate: data.severity === 'danger' ? [300, 100, 300, 100, 300] :
             data.severity === 'warning' ? [200, 100, 200] :
             [100],
  }

  event.waitUntil(self.registration.showNotification(data.title || 'OtoDock', options))
})

// A click_url is only safe to navigate if it is a plain absolute path — a
// notification's source is untrusted, so reject scheme/host/protocol-relative
// values before navigating the authenticated tab (same rule as the native
// twin's safeClickPath in useFcmPush.ts).
function safeClickPath(u) {
  if (typeof u !== 'string' || !u) return null
  if (!u.startsWith('/') || u.startsWith('//')) return null
  if (u.includes('://') || u.includes('\\')) return null
  return u
}

self.addEventListener('notificationclick', (event) => {
  event.notification.close()

  const url = safeClickPath(event.notification.data?.url) || '/'

  event.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then((windowClients) => {
      // Navigate + focus existing tab if open
      for (const client of windowClients) {
        if (client.url.includes(self.location.origin) && 'navigate' in client) {
          return client.navigate(url).then((c) => c && c.focus())
        }
      }
      // Otherwise open a new tab
      return clients.openWindow(url)
    })
  )
})

// Skip waiting on install for immediate activation
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(clients.claim()))
