import { useState } from 'react'
import { NATIVE_HANDOFF_STATE, nativePasskeyLogin, passkeySupported } from '../api/webauthn'

/**
 * System-browser leg of the native-app passkey sign-in. The Android app opens
 * this page via openAuthBrowser (the webview can't run WebAuthn); a successful
 * ceremony deep-links the one-time handoff token back through the existing
 * OIDC-callback rails, and the app's webview exchanges it for its session.
 */
export default function NativePasskey() {
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  const [loading, setLoading] = useState(false)

  async function handleSignIn() {
    setError('')
    setLoading(true)
    try {
      const token = await nativePasskeyLogin()
      setDone(true)
      window.location.href =
        `otodock://auth/callback?code=${encodeURIComponent(token)}&state=${NATIVE_HANDOFF_STATE}`
    } catch (err: any) {
      if (err?.name !== 'NotAllowedError') setError(err.message || 'Passkey sign-in failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-p-bg">
      <div className="w-full max-w-sm mx-4">
        <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-6 shadow-xs text-center">
          <h1 className="text-xl font-semibold text-p-text mb-1">Sign In to the App</h1>
          {done ? (
            <p className="text-sm text-p-text-secondary">
              Signed in — returning to the OtoDock app… If nothing happens, switch back to the app.
            </p>
          ) : (
            <>
              <p className="text-sm text-p-text-secondary mb-6">
                Use your passkey to sign in to the OtoDock app.
              </p>
              {error && <div className="text-sm text-p-accent-red mb-4">{error}</div>}
              {passkeySupported() ? (
                <button onClick={handleSignIn} disabled={loading}
                  className="w-full flex items-center justify-center gap-2 px-4 py-2 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50 transition-colors">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                  </svg>
                  {loading ? 'Waiting for your passkey...' : 'Use your passkey'}
                </button>
              ) : (
                <p className="text-sm text-p-text-secondary">
                  This browser does not support passkeys. Go back to the app and sign in with your password.
                </p>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}
