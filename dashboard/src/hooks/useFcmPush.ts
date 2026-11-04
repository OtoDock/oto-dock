import { useEffect, useRef, useLayoutEffect } from 'react'
import { Capacitor } from '@capacitor/core'
import { PushNotifications } from '@capacitor/push-notifications'
import { subscribePush, unsubscribePush } from '../api/notifications'
import { askNative } from '../lib/nativeBridge'
import { debugWarn } from '../lib/debug/log'
import { noteStoreReviewMoment } from '../lib/device/storeReview'

/** A click_url is only safe to navigate if it is a plain absolute path — a
 *  notification's source server is untrusted, so reject scheme/host/protocol-
 *  relative values before handing them to the router. */
function safeClickPath(u: unknown): string | null {
  if (typeof u !== 'string' || !u) return null
  if (!u.startsWith('/') || u.startsWith('//')) return null
  if (u.includes('://') || u.includes('\\')) return null
  return u
}

/** A notification about an app carries `{kind: 'app', id}` in its data
 *  payload (FCM delivers data, not URLs); the route is built here. */
export function appNotificationPath(data: Record<string, unknown>): string | null {
  if (data.kind !== 'app' || typeof data.id !== 'string') return null
  return /^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$/.test(data.id) ? `/apps/${data.id}` : null
}

/** The push token this page last gave the server: per installation, since
 *  the storage is per origin. It is what "notifications were on" means after
 *  the app was ended, and Android 13 and later ends the app when the person
 *  takes the permission back in the phone's settings. */
export const PUSH_TOKEN_KEY = 'otodock.push.token'

function storedToken(): string | null {
  try {
    return localStorage.getItem(PUSH_TOKEN_KEY)
  } catch {
    return null
  }
}

function keepToken(token: string | null): void {
  try {
    if (token) localStorage.setItem(PUSH_TOKEN_KEY, token)
    else localStorage.removeItem(PUSH_TOKEN_KEY)
  } catch {
    // A page without storage registers again at each start, as before.
  }
}

/** The platform an app's push token is stored under: `ios` in the iOS app
 *  (the relay, or an install's own direct sender, then adds Apple's alert
 *  block to its messages), `android` otherwise. A browser registers `web`
 *  through usePushSubscription. */
export function pushPlatform(): 'ios' | 'android' {
  return Capacitor.getPlatform() === 'ios' ? 'ios' : 'android'
}

/**
 * Registers for FCM push notifications in the app (Capacitor native), and
 * drops this device's registration on the server once the person turns
 * notifications off in the phone's settings. No-op in web browsers:
 * usePushSubscription handles Web Push there.
 *
 * Mounted once at the authenticated app root (RequireAuth) so registration is
 * route-independent: an installation whose last page isn't a chat would
 * otherwise never register its token.
 *
 * One step at a time, at mount and on every return to the page: a
 * permission that is no longer granted while a token is stored drops the
 * server's row for it (kept and asked again when the server does not
 * answer); a grant registers, once per page. The token itself stays with
 * the phone: the app holds one token for every installation it serves, so
 * deleting it would silence the other servers, and a later grant
 * subscribes the same token again.
 *
 * @param navigate - React Router navigate function for deep linking on notification tap
 * @param enabled - register only once authenticated (subscribePush is auth-gated)
 */
export function useFcmPush(navigate?: (path: string) => void, enabled: boolean = true) {
  const page = useRef({ registered: false, listening: false, unavailable: false, chain: Promise.resolve() })
  const navigateRef = useRef(navigate)
  useLayoutEffect(() => { navigateRef.current = navigate })

  useEffect(() => {
    if (!enabled) return
    if (!Capacitor.isNativePlatform()) return
    const s = page.current

    const listen = async () => {
      if (s.listening) return
      s.listening = true

      // Listeners BEFORE register(): once the device holds a cached FCM
      // token the 'registration' event fires as soon as register() runs,
      // and a listener attached after the await missed it, so the server's
      // token row never refreshed after the first install. Capacitor's own
      // docs order it this way.
      await PushNotifications.addListener('registration', async (token) => {
        try {
          await subscribePush(pushPlatform(), token.value)
          keepToken(token.value)
        } catch (err) {
          debugWarn('Failed to send FCM token to server:', err)
        }
      })

      // Registration error
      await PushNotifications.addListener('registrationError', (error) => {
        debugWarn('FCM registration failed:', error)
      })

      PushNotifications.addListener('pushNotificationReceived', () => {
        // In the foreground the socket's toast shows it, never a native notification.
      })

      // User tapped a notification (app was in background)
      PushNotifications.addListener('pushNotificationActionPerformed', async (notification) => {
        const data = notification.notification.data || {}
        const sourceInstall = data.install_id

        // Hand the source install_id to native, which owns the install_id→install
        // binding. It switches to that installation and routes there, returning true
        // iff it actually switched (the source is a known, different install). This
        // works even when THIS (active) install isn't bound yet — only the source
        // needs to be known. If it didn't switch (or did not answer), navigate
        // here; a switch that lands late recreates the page anyway.
        if (typeof sourceInstall === 'string' && sourceInstall && await askNative<boolean>(
          'switchToInstall', [sourceInstall, typeof data.click_url === 'string' ? data.click_url : '/'],
        )) {
          return
        }

        const clickUrl = appNotificationPath(data) ?? safeClickPath(data.click_url)
        if (clickUrl && clickUrl !== '/' && navigateRef.current) {
          navigateRef.current(clickUrl)
          if (clickUrl.startsWith('/chat/')) noteStoreReviewMoment('push')
        }
      })
    }

    const step = async (atMount: boolean) => {
      let permission = (await PushNotifications.checkPermissions()).receive
      // Not granted covers 'prompt' too: a permission taken back in the
      // phone's settings reads as one the app may ask for again.
      if (permission !== 'granted') {
        s.registered = false
        const token = storedToken()
        if (token && await unsubscribePush(token)) keepToken(null)
      }
      // The system asks the person at most once; a page that comes back
      // only reads the answer they gave in the phone's settings.
      if (atMount && permission !== 'granted') {
        permission = (await PushNotifications.requestPermissions()).receive
      }
      if (permission !== 'granted') {
        if (atMount) debugWarn('Push notification permission not granted')
        return
      }
      if (s.registered || s.unavailable) return
      await listen()
      // Marked before the call: the system's permission dialog pauses and
      // resumes the page, and that return must not register a second time.
      s.registered = true
      try {
        await PushNotifications.register()
      } catch (err) {
        // A build without Firebase refuses for the page's life.
        s.unavailable = true
        debugWarn('FCM setup error:', err)
      }
    }

    s.chain = s.chain.then(() => step(true)).catch((err) => debugWarn('FCM setup error:', err))

    // The app posts the health-check event on every resume, and the page's
    // visibility usually changes too: a check still waiting covers both.
    let waiting = false
    const onReturn = () => {
      if (document.hidden || waiting) return
      waiting = true
      s.chain = s.chain
        .then(() => { waiting = false; return step(false) })
        .catch((err) => debugWarn('FCM setup error:', err))
    }
    document.addEventListener('visibilitychange', onReturn)
    window.addEventListener('otodock:force-health-check', onReturn)
    return () => {
      document.removeEventListener('visibilitychange', onReturn)
      window.removeEventListener('otodock:force-health-check', onReturn)
    }
  }, [enabled])
}
