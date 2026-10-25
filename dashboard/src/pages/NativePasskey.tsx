import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { NATIVE_HANDOFF_STATE, nativePasskeyLogin, passkeySupported } from '../api/webauthn'
import { errorText, errorName } from '../lib/http/apiJson'
import Button from '../components/ui/Button'

/**
 * System-browser leg of the native-app passkey sign-in. The Android app opens
 * this page via openAuthBrowser (the webview can't run WebAuthn); a successful
 * ceremony deep-links the one-time handoff token back through the existing
 * OIDC-callback rails, and the app's webview exchanges it for its session.
 * The `h` parameter is the nonce the app's webview holds: the token is bound
 * to it, so no other browser can take the sign-in.
 */
export default function NativePasskey() {
  const [searchParams] = useSearchParams()
  const handoff = searchParams.get('h') || ''
  const [error, setError] = useState('')
  const [done, setDone] = useState(false)
  const [loading, setLoading] = useState(false)

  async function handleSignIn() {
    setError('')
    setLoading(true)
    try {
      const token = await nativePasskeyLogin(handoff)
      setDone(true)
      window.location.href =
        `otodock://auth/callback?code=${encodeURIComponent(token)}&state=${NATIVE_HANDOFF_STATE}`
    } catch (err) {
      if (errorName(err) !== 'NotAllowedError') setError(errorText(err) || 'Passkey sign-in failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-hero-bg">
      <div className="w-full max-w-sm mx-4">
        <div className="bg-surface-overlay text-primary rounded-hero shadow-elevation-4 p-6 text-center">
          <h1 className="font-display text-xl font-semibold text-primary mb-1">Sign In to the App</h1>
          {done ? (
            <p className="text-sm text-secondary">
              Signed in. Returning to the OtoDock app… If nothing happens, switch back to the app.
            </p>
          ) : (
            <>
              <p className="text-sm text-secondary mb-6">
                Use your passkey to sign in to the OtoDock app.
              </p>
              {error && <div className="text-sm text-status-danger-fg mb-4">{error}</div>}
              {!handoff ? (
                // Opened by an app page older than the handoff binding.
                <p className="text-sm text-secondary">
                  Close this page, then close and reopen the OtoDock app and sign in again.
                </p>
              ) : passkeySupported() ? (
                <Button variant="primary" size="md" onClick={handleSignIn} disabled={loading}
                  className="w-full">
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                      d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
                  </svg>
                  {loading ? 'Waiting for your passkey...' : 'Use your passkey'}
                </Button>
              ) : (
                <p className="text-sm text-secondary">
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
