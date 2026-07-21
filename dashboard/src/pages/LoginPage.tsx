import { useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { type AuthConfig, localLogin, verify2FA, startOidcLogin, forgotPassword } from '../api/auth'
import { passkeyLogin, passkeySecondFactor, passkeySupported } from '../api/webauthn'
import { TurnstileWidget, type TurnstileHandle } from '../components/TurnstileWidget'

interface LoginPageProps {
  authConfig: AuthConfig
}

const INPUT_CLS = 'w-full px-3 py-2.5 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text placeholder:text-p-text-light focus:outline-hidden focus:ring-2 focus:ring-brand/30 focus:border-brand/50 transition-colors'

function PasskeyIcon({ className = 'w-4 h-4' }: { className?: string }) {
  return (
    <svg className={className} fill="none" stroke="currentColor" viewBox="0 0 24 24">
      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
        d="M15 7a2 2 0 012 2m4 0a6 6 0 01-7.743 5.743L11 17H9v2H7v2H4a1 1 0 01-1-1v-2.586a1 1 0 01.293-.707l5.964-5.964A6 6 0 1121 9z" />
    </svg>
  )
}

function Divider({ label }: { label: string }) {
  return (
    <div className="relative my-5">
      <div className="absolute inset-0 flex items-center">
        <div className="w-full border-t border-p-border-light" />
      </div>
      <div className="relative flex justify-center text-xs">
        <span className="px-3 bg-white dark:bg-p-surface text-p-text-light">{label}</span>
      </div>
    </div>
  )
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-center min-h-screen bg-p-bg px-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-6">
          <h1 className="text-2xl font-bold text-p-text tracking-tight">OtoDock</h1>
          <p className="text-sm text-p-text-secondary mt-1">Your AI agents workspace</p>
        </div>
        <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-6 shadow-xs">
          {children}
        </div>
      </div>
    </div>
  )
}

export default function LoginPage({ authConfig }: LoginPageProps) {
  const { setUser } = useAuth()
  const navigate = useNavigate()

  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  // Cloudflare Turnstile (login bot-protection) — only shown when a site key is configured.
  const [turnstileToken, setTurnstileToken] = useState('')
  const turnstileRef = useRef<TurnstileHandle>(null)

  // 2FA state
  const [needs2FA, setNeeds2FA] = useState(false)
  const [totpToken, setTotpToken] = useState('')
  const [totpCode, setTotpCode] = useState('')
  const [secondFactors, setSecondFactors] = useState<string[]>(['totp'])

  // Forgot password
  const [showForgot, setShowForgot] = useState(false)
  const [forgotEmail, setForgotEmail] = useState('')
  const [forgotSent, setForgotSent] = useState(false)

  // Passkeys: passwordless primary sign-in only when the admin mode allows it.
  // The native app webview can't run WebAuthn — there the button opens the
  // system browser at /native-passkey (same SSO rails: openAuthBrowser →
  // deep-link handoff).
  const isNative = !!(window as any).Capacitor?.isNativePlatform?.()
  // NB: bridge methods must be INVOKED ON the injected object —
  // Android.openAuthBrowser(url). A detached reference throws "Java bridge
  // method invoked on an object that is not injected" (silently, in the
  // webview) — that bug shipped once; don't reintroduce it.
  const androidBridge = (window as any).Android
  // Passkeys are bound to the configured public host (the RP): on any OTHER
  // origin — localhost, a LAN IP — the browser refuses the ceremony with its
  // own scary security error. Hide the in-page buttons there and say where
  // passkeys work instead. The native flow is exempt: its system-browser leg
  // opens the public URL, so the origin matches by construction.
  const rpHost = authConfig.passkey_rp_host || ''
  const rpMismatch = !!rpHost && window.location.hostname !== rpHost
  const passwordlessPasskey = authConfig.passkeys_enabled
    && authConfig.passkey_login_mode !== 'second_factor'
    && (isNative ? !!androidBridge?.openAuthBrowser : passkeySupported() && !rpMismatch)
  // 2FA-step passkey: needs in-page WebAuthn (not available in the app webview).
  const stepPasskey = secondFactors.includes('passkey') && !isNative && passkeySupported()
    && !rpMismatch
  // The account HAS a passkey but this origin can't run it — the 2FA step
  // explains instead of dead-ending.
  const stepPasskeyElsewhere = secondFactors.includes('passkey') && !isNative && rpMismatch
  const stepTotp = secondFactors.includes('totp')

  async function handleLogin(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const result = await localLogin(email, password, turnstileToken)
      if (result.requires_2fa && result.totp_session_token) {
        // The token was consumed by this leg's verify; clear it so a return-to-login
        // re-submit doesn't reuse a spent (single-use) token before the widget remounts.
        setTurnstileToken('')
        setNeeds2FA(true)
        setTotpToken(result.totp_session_token)
        setSecondFactors(result.second_factors?.length ? result.second_factors : ['totp'])
      } else if (result.user) {
        setUser(result.user)
        navigate('/', { replace: true })
      }
    } catch (err: any) {
      // Turnstile tokens are single-use — get a fresh one for the retry.
      turnstileRef.current?.reset()
      setTurnstileToken('')
      setError(err.message || 'Login failed')
    } finally {
      setLoading(false)
    }
  }

  async function handle2FA(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const user = await verify2FA(totpToken, totpCode)
      setUser(user)
      navigate('/', { replace: true })
    } catch (err: any) {
      setError(err.message || '2FA verification failed')
    } finally {
      setLoading(false)
    }
  }

  async function handleStepPasskey() {
    setError('')
    setLoading(true)
    try {
      const user = await passkeySecondFactor(totpToken)
      setUser(user)
      navigate('/', { replace: true })
    } catch (err: any) {
      if (err?.name !== 'NotAllowedError') setError(err.message || 'Passkey verification failed')
    } finally {
      setLoading(false)
    }
  }

  async function handleForgotPassword(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await forgotPassword(forgotEmail || email)
      setForgotSent(true)
    } catch {
      setForgotSent(true) // Show success regardless (no enumeration)
    } finally {
      setLoading(false)
    }
  }

  function handleOidcLogin() {
    startOidcLogin().catch((err) => setError(err.message))
  }

  async function handlePasskeyLogin() {
    if (isNative) {
      androidBridge.openAuthBrowser(`${window.location.origin}/native-passkey`)
      return
    }
    setError('')
    setLoading(true)
    try {
      const user = await passkeyLogin()
      setUser(user)
      navigate('/', { replace: true })
    } catch (err: any) {
      // Cancelling the browser dialog is not an error worth showing.
      if (err?.name !== 'NotAllowedError') setError(err.message || 'Passkey sign-in failed')
    } finally {
      setLoading(false)
    }
  }

  // Forgot password form
  if (showForgot) {
    return (
      <Shell>
        <h2 className="text-lg font-semibold text-p-text mb-1">Reset password</h2>
        <p className="text-sm text-p-text-secondary mb-5">
          Enter your email and we'll send you a reset link.
        </p>
        {forgotSent ? (
          <div className="space-y-4">
            <p className="text-sm text-green-600 dark:text-green-400">
              If your email is registered, you'll receive a reset link shortly.
            </p>
            <button onClick={() => { setShowForgot(false); setForgotSent(false) }}
              className="w-full px-4 py-2 text-sm font-medium text-brand hover:underline">
              Back to sign in
            </button>
          </div>
        ) : (
          <form onSubmit={handleForgotPassword} className="space-y-4">
            <input type="email" value={forgotEmail || email}
              onChange={e => setForgotEmail(e.target.value)}
              placeholder="Email address" required
              className={INPUT_CLS} />
            <button type="submit" disabled={loading}
              className="w-full px-4 py-2.5 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50">
              {loading ? 'Sending...' : 'Send reset link'}
            </button>
            <button type="button" onClick={() => setShowForgot(false)}
              className="w-full px-4 py-2 text-sm text-p-text-secondary hover:text-p-text">
              Back to sign in
            </button>
          </form>
        )}
      </Shell>
    )
  }

  // 2FA step — passkey primary when enrolled, authenticator code as the
  // other path (or the only one). Recovery codes ride the code input.
  if (needs2FA) {
    return (
      <Shell>
        <h2 className="text-lg font-semibold text-p-text mb-1">Verify it's you</h2>
        <p className="text-sm text-p-text-secondary mb-5">
          {stepPasskey && stepTotp
            ? 'Use your passkey, or enter a code from your authenticator app.'
            : stepPasskey
              ? 'Confirm with your passkey to finish signing in.'
              : 'Enter the 6-digit code from your authenticator app, or a recovery code.'}
        </p>
        {stepPasskeyElsewhere && (
          <p className="text-xs text-p-text-light mb-4">
            Your passkey works when you open OtoDock at{' '}
            <span className="font-medium text-p-text-secondary">https://{rpHost}</span>
            {stepTotp
              ? ' — here, use your authenticator code instead.'
              : '. This address cannot run it, and this account has no authenticator app — sign in from that address, or ask an admin to reset your second factor.'}
          </p>
        )}
        {error && <div className="text-sm text-p-accent-red mb-4">{error}</div>}

        {stepPasskey && (
          <button onClick={handleStepPasskey} disabled={loading}
            className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50 transition-colors">
            <PasskeyIcon />
            {loading ? 'Waiting for your passkey...' : 'Use your passkey'}
          </button>
        )}

        {stepPasskey && stepTotp && <Divider label="or enter a code" />}

        {stepTotp && (
          <form onSubmit={handle2FA} className="space-y-4">
            <input type="text" value={totpCode}
              onChange={e => setTotpCode(e.target.value)}
              placeholder="000000" required autoFocus={!stepPasskey}
              maxLength={32} autoComplete="one-time-code"
              className={`${INPUT_CLS} text-center tracking-widest font-mono`} />
            <button type="submit" disabled={loading}
              className={`w-full px-4 py-2.5 text-sm font-medium rounded-lg disabled:opacity-50 transition-colors ${
                stepPasskey
                  ? 'border border-p-border-light text-p-text hover:bg-p-surface-hover'
                  : 'text-white bg-brand hover:bg-brand-hover'
              }`}>
              {loading ? 'Verifying...' : 'Verify code'}
            </button>
          </form>
        )}

        <button type="button"
          onClick={() => { setNeeds2FA(false); setTotpCode(''); setSecondFactors(['totp']); setError('') }}
          className="mt-4 w-full px-4 py-2 text-sm text-p-text-secondary hover:text-p-text">
          Back to sign in
        </button>
      </Shell>
    )
  }

  // Main sign-in. SSO-fronted installs put "Continue with <provider>" FIRST
  // and demote local sign-in (passkey + email) under a labeled divider: a
  // user arriving mid-SSO flow who clicks a familiar passkey button signs
  // into whichever LOCAL account owns a passkey on this host — not their
  // IdP account (live-observed admin-vs-test-account trap). Local-only
  // installs keep passkey-first.
  return (
    <Shell>
      {error && <div className="text-sm text-p-accent-red mb-4">{error}</div>}

      {authConfig.oidc_enabled && (
        <>
          <button onClick={handleOidcLogin}
            className="w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg transition-colors">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
            </svg>
            Continue with {authConfig.oidc_provider_name}
          </button>
          <Divider label="or use a local account" />
        </>
      )}

      {passwordlessPasskey && (
        <>
          <button onClick={handlePasskeyLogin} disabled={loading}
            className={`w-full flex items-center justify-center gap-2 px-4 py-2.5 text-sm font-medium rounded-lg disabled:opacity-50 transition-colors ${
              authConfig.oidc_enabled
                ? 'border border-p-border-light text-p-text hover:bg-p-surface-hover'
                : 'text-white bg-brand hover:bg-brand-hover'
            }`}>
            <PasskeyIcon />
            {authConfig.oidc_enabled
              ? 'Sign in with a local-account passkey'
              : 'Sign in with a passkey'}
          </button>
          <Divider label="or use your email" />
        </>
      )}

      <form onSubmit={handleLogin} className="space-y-3.5">
        <div>
          <label className="block text-xs font-medium text-p-text-secondary mb-1.5">Email</label>
          {/* No `webauthn` autocomplete token: it primes passkey autofill in
              the password manager, and without a deliberate conditional-
              mediation ceremony it only invites the wrong-account trap the
              explicit button already guards against. */}
          <input type="email" value={email} onChange={e => setEmail(e.target.value)}
            required autoComplete="email"
            autoFocus={!passwordlessPasskey} placeholder="you@example.com"
            className={INPUT_CLS} />
        </div>
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="block text-xs font-medium text-p-text-secondary">Password</label>
            {authConfig.email_links_available && (
              <button type="button" onClick={() => setShowForgot(true)}
                className="text-xs text-p-text-light hover:text-brand transition-colors">
                Forgot password?
              </button>
            )}
          </div>
          <input type="password" value={password} onChange={e => setPassword(e.target.value)}
            required autoComplete="current-password" placeholder="••••••••"
            className={INPUT_CLS} />
        </div>

        {authConfig.turnstile_site_key && (
          <TurnstileWidget ref={turnstileRef} siteKey={authConfig.turnstile_site_key}
            onToken={setTurnstileToken} />
        )}

        <button type="submit" disabled={loading}
          className={`w-full px-4 py-2.5 text-sm font-medium rounded-lg disabled:opacity-50 transition-colors ${
            passwordlessPasskey || authConfig.oidc_enabled
              ? 'border border-p-border-light text-p-text hover:bg-p-surface-hover'
              : 'text-white bg-brand hover:bg-brand-hover'
          }`}>
          {loading ? 'Signing in...' : 'Sign in'}
        </button>
      </form>
    </Shell>
  )
}
