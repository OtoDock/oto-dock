import { useEffect, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { handleCallback } from '../api/auth'
import { NATIVE_HANDOFF_STATE, exchangeNativeToken } from '../api/webauthn'
import { useAuth } from '../contexts/AuthContext'

export default function AuthCallback() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const { setUser } = useAuth()
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const code = searchParams.get('code')
    const state = searchParams.get('state')

    if (!code || !state) {
      setError('Missing code or state parameter')
      return
    }

    // Race fix (Android post-auth): after handleCallback resolves the JWT
    // cookie is set in the Set-Cookie response header, but on the Capacitor
    // WebView there's a small window before the cookie lands in the native
    // CookieManager — long enough that an immediate WS open from a chat
    // page tap arrives without auth and the proxy 401s → app falls back to
    // setup. Defense-in-depth: (a) flushCookies bridge call forces the
    // WebView to persist its cookie store; (b) 500ms delay before navigate
    // gives both the auth context and the cookie store time to settle.
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | null = null
    // Native passkey handoff rides the same deep-link rails as the OIDC
    // callback: `code` carries the one-time token, `state` the fixed marker.
    const exchange = state === NATIVE_HANDOFF_STATE
      ? exchangeNativeToken(code)
      : handleCallback(code, state)
    exchange
      .then((user) => {
        if (cancelled) return
        setUser(user)
        const android = (window as { Android?: { flushCookies?: () => void } }).Android
        if (android && typeof android.flushCookies === 'function') {
          try { android.flushCookies() } catch { /* no-op */ }
        }
        timer = setTimeout(() => {
          if (!cancelled) navigate('/', { replace: true })
        }, 500)
      })
      .catch((e) => {
        if (cancelled) return
        if (e.message === 'ACCESS_DENIED') {
          setError('Access denied. You are not a member of any OtoDock group. Contact your administrator.')
        } else {
          setError(e.message || 'Authentication failed')
        }
      })
    return () => {
      cancelled = true
      if (timer) clearTimeout(timer)
    }
  }, [searchParams, navigate, setUser])

  if (error) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-gray-50 dark:bg-gray-900">
        <div className="bg-white dark:bg-p-surface border border-red-200 dark:border-red-800 rounded-lg p-6 max-w-md">
          <h2 className="text-lg font-semibold text-red-700 dark:text-red-400 mb-2">Authentication Failed</h2>
          <p className="text-sm text-gray-700 dark:text-gray-300">{error}</p>
          <button
            onClick={() => (window.location.href = '/')}
            className="mt-4 text-sm text-blue-600 hover:text-blue-800 dark:text-blue-400 dark:hover:text-blue-300"
          >
            Back to home
          </button>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-gray-50 dark:bg-gray-900">
      <p className="text-sm text-gray-500 dark:text-gray-400">Completing login...</p>
    </div>
  )
}
