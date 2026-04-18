import { useEffect, useRef } from 'react'
import { Capacitor } from '@capacitor/core'
import { PushNotifications } from '@capacitor/push-notifications'
import { subscribePush } from '../api/notifications'

/** A click_url is only safe to navigate if it is a plain absolute path — a
 *  notification's source server is untrusted, so reject scheme/host/protocol-
 *  relative values before handing them to the router. */
function safeClickPath(u: unknown): string | null {
  if (typeof u !== 'string' || !u) return null
  if (!u.startsWith('/') || u.startsWith('//')) return null
  if (u.includes('://') || u.includes('\\')) return null
  return u
}

/**
 * Registers for FCM push notifications on Android (Capacitor native).
 * No-op in web browsers — usePushSubscription handles Web Push there.
 *
 * Mounted once at the authenticated app root (RequireAuth) so registration is
 * route-independent — an installation whose last page isn't a chat would
 * otherwise never register its token.
 *
 * @param navigate - React Router navigate function for deep linking on notification tap
 * @param enabled - register only once authenticated (subscribePush is auth-gated)
 */
export function useFcmPush(navigate?: (path: string) => void, enabled: boolean = true) {
  const registered = useRef(false)
  const navigateRef = useRef(navigate)
  navigateRef.current = navigate

  useEffect(() => {
    if (!enabled) return
    if (registered.current) return
    if (!Capacitor.isNativePlatform()) return

    registered.current = true

    const setup = async () => {
      try {
        // Request permission
        const result = await PushNotifications.requestPermissions()
        if (result.receive !== 'granted') {
          console.warn('Push notification permission not granted')
          return
        }

        // Register with FCM
        await PushNotifications.register()

        // Listen for registration token
        PushNotifications.addListener('registration', async (token) => {
          try {
            await subscribePush('android', token.value)
          } catch (err) {
            console.error('Failed to send FCM token to server:', err)
          }
        })

        // Registration error
        PushNotifications.addListener('registrationError', (error) => {
          console.error('FCM registration failed:', error)
        })

        // Notification received while app is in foreground
        // We don't show native notification here — the WS toast handles it
        PushNotifications.addListener('pushNotificationReceived', () => {
          // No native notification here — the WS toast handles it.
        })

        // User tapped a notification (app was in background)
        PushNotifications.addListener('pushNotificationActionPerformed', (notification) => {
          const data = notification.notification.data || {}
          const Android = (window as any).Android
          const sourceInstall = data.install_id

          // Hand the source install_id to native, which owns the install_id→install
          // binding. It switches to that installation and routes there, returning true
          // iff it actually switched (the source is a known, different install). This
          // works even when THIS (active) install isn't bound yet — only the source
          // needs to be known. If it didn't switch, navigate here.
          if (sourceInstall && Android?.switchToInstall
              && Android.switchToInstall(sourceInstall, typeof data.click_url === 'string' ? data.click_url : '/')) {
            return
          }

          const clickUrl = safeClickPath(data.click_url)
          if (clickUrl && clickUrl !== '/' && navigateRef.current) {
            navigateRef.current(clickUrl)
          }
        })
      } catch (err) {
        console.error('FCM setup error:', err)
      }
    }

    setup()
  }, [enabled])
}
