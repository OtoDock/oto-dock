import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { handleCallback, type CallbackResult } from '../api/auth'
import { NATIVE_HANDOFF_STATE, exchangeNativeToken } from '../api/webauthn'
import { useAuth } from '../contexts/AuthContext'
import { peekPending, saveConfirm } from '../lib/shareConfirm'
import { callNative } from '../lib/nativeBridge'
import { isNativePlatform } from '../audio/types'

export default function AuthCallback() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const { setUser, user, loading } = useAuth()
  const [failure, setError] = useState<string | null>(null)
  // The handoff link opened outside the app: the reason, no exchange.
  const appOnlyLink = searchParams.get('state') === NATIVE_HANDOFF_STATE && !isNativePlatform()
  const error = failure ?? (appOnlyLink ? 'This sign-in link only works in the OtoDock app.' : null)
  // The code is one-shot: the exchange runs once per (code, state), however
  // often the effect re-runs (the account arriving after a login changes
  // `user`, and a second POST would only get "invalid or expired state").
  const startedRef = useRef('')
  const aliveRef = useRef(true)
  useEffect(() => {
    aliveRef.current = true
    return () => { aliveRef.current = false }
  }, [])

  useEffect(() => {
    // The identity-provider confirm lands here while signed in: the account
    // must be known before the answer can be filed under it.
    if (loading) return
    const code = searchParams.get('code')
    const state = searchParams.get('state')

    if (!code || !state) {
      // The provider sent the browser here with no answer. During a share
      // confirm that is the provider "launching" the application at its
      // callback address instead of finishing the request (Authentik's
      // default Launch URL is the redirect URI — seen on the VM pass,
      // 2026-09-18, under the strict confirm): back to the share with the
      // reason. Otherwise a person opened the platform from the provider's
      // portal: the home.
      const pending = peekPending(user?.sub || '')
      if (pending) {
        const reason = 'The identity provider sent you back without an answer. Try again. If it repeats, '
          + 'its application "Launch URL" should point at this dashboard.'
        const sep = pending.return_to.includes('?') ? '&' : '?'
        navigate(`${pending.return_to}${sep}confirm_error=${encodeURIComponent(reason)}`, { replace: true })
        return
      }
      navigate('/', { replace: true })
      return
    }
    const key = `${code}|${state}`
    if (startedRef.current === key) return
    startedRef.current = key

    // Race fix (Android post-auth): after handleCallback resolves the JWT
    // cookie is set in the Set-Cookie response header, but on the Capacitor
    // WebView there's a small window before the cookie lands in the native
    // CookieManager — long enough that an immediate WS open from a chat
    // page tap arrives without auth and the proxy 401s → app falls back to
    // setup. Defense-in-depth: (a) flushCookies bridge call forces the
    // WebView to persist its cookie store; (b) 500ms delay before navigate
    // gives both the auth context and the cookie store time to settle.
    // Native passkey handoff rides the same deep-link rails as the OIDC
    // callback: `code` carries the one-time token, `state` the fixed marker.
    // Only the app's webview ever exchanges one (the token is bound to its
    // cookie): a browser that opens such a link gets the reason, not a call.
    if (appOnlyLink) return
    const exchange: Promise<CallbackResult> = state === NATIVE_HANDOFF_STATE
      ? exchangeNativeToken(code).then((u) => ({ kind: 'login' as const, user: u }))
      : handleCallback(code, state)
    const sub = user?.sub || ''
    exchange
      .then((r) => {
        if (!aliveRef.current) return
        if (r.kind === 'confirm') {
          // No session changes hands (SHARING.md "The confirm"): the
          // one-shot token waits for the share popover on the page that
          // started the round trip.
          saveConfirm(sub, r.token)
          navigate(r.return_to, { replace: true })
          return
        }
        setUser(r.user)
        callNative('flushCookies')
        setTimeout(() => {
          if (aliveRef.current) navigate('/', { replace: true })
        }, 500)
      })
      .catch((e) => {
        if (!aliveRef.current) return
        // A confirm that failed goes back to the share it was for with the
        // reason, never to the failed-login page; the popover says it.
        const pending = peekPending(sub)
        if (pending) {
          const reason = (e as { detail?: string }).detail || e.message || 'The confirmation failed'
          const sep = pending.return_to.includes('?') ? '&' : '?'
          navigate(`${pending.return_to}${sep}confirm_error=${encodeURIComponent(reason)}`, { replace: true })
          return
        }
        if (e.message === 'ACCESS_DENIED') {
          // The server says why (no group, an ID token check, an email that
          // another account uses); the group wording is the fallback.
          setError((e as { detail?: string }).detail
            || 'Access denied. You are not a member of any OtoDock group. Contact your administrator.')
        } else {
          setError(e.message || 'Authentication failed')
        }
      })
  }, [searchParams, navigate, setUser, loading, user?.sub, appOnlyLink])

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-hero-bg">
        <div className="bg-surface-overlay text-primary rounded-hero shadow-elevation-4 p-6 max-w-md">
          <h2 className="font-display text-lg font-semibold text-status-danger-fg mb-2">Authentication Failed</h2>
          <p className="text-sm text-secondary">{error}</p>
          <button
            onClick={() => (window.location.href = '/')}
            className="mt-4 text-sm text-link hover:text-link-hover"
          >
            Back to home
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-hero-bg">
      <p className="text-sm text-hero-text-muted">Completing login...</p>
    </div>
  )
}
