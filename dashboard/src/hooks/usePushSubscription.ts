import { useEffect, useRef } from 'react'
import { getVapidPublicKey, subscribePush } from '../api/notifications'

/**
 * Registers the Service Worker and subscribes to Web Push notifications.
 * Only runs in browsers that support Push (not in native Capacitor app).
 * Call once from the root component (e.g., AgentChat).
 */
export function usePushSubscription() {
  const subscribed = useRef(false)

  useEffect(() => {
    if (subscribed.current) return
    if (!('serviceWorker' in navigator) || !('PushManager' in window)) return

    // Skip on native Capacitor — FCM handles push there
    if ((window as any).Capacitor?.isNativePlatform?.()) return

    subscribed.current = true

    const register = async () => {
      try {
        const registration = await navigator.serviceWorker.register('/sw.js')
        await navigator.serviceWorker.ready

        // Check existing subscription
        let subscription = await registration.pushManager.getSubscription()
        if (subscription) {
          // Already subscribed — re-register with backend (in case token changed)
          await subscribePush('web', JSON.stringify(subscription.toJSON()))
          return
        }

        // Request permission
        const permission = await Notification.requestPermission()
        if (permission !== 'granted') return

        // Get VAPID key from server
        let vapidKey: string
        try {
          vapidKey = await getVapidPublicKey()
        } catch {
          // VAPID not configured yet — skip
          return
        }

        // Convert VAPID key to Uint8Array
        const padding = '='.repeat((4 - vapidKey.length % 4) % 4)
        const base64 = (vapidKey + padding).replace(/-/g, '+').replace(/_/g, '/')
        const rawData = window.atob(base64)
        const applicationServerKey = new Uint8Array(rawData.length)
        for (let i = 0; i < rawData.length; ++i) {
          applicationServerKey[i] = rawData.charCodeAt(i)
        }

        // Subscribe
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey,
        })

        // Send to backend
        await subscribePush('web', JSON.stringify(subscription.toJSON()))
      } catch (err) {
        console.warn('Push subscription failed:', err)
      }
    }

    register()
  }, [])
}
