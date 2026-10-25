import { useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { type AuthConfig, localLogin, verify2FA, startOidcLogin, forgotPassword } from '../api/auth'
import { passkeyLogin, passkeySecondFactor, passkeySupported, startNativeHandoff } from '../api/webauthn'
import { TurnstileWidget, type TurnstileHandle } from '../components/TurnstileWidget'
import { useBuildWatch } from '../hooks/useBuildWatch'
import { callNative, hasNativeBridge, inAppShell, openNativeBrowser, openPrivacyPolicy } from '../lib/nativeBridge'
import { errorText, errorName } from '../lib/http/apiJson'
import { isNativePlatform } from '../audio/types'
import Button from '../components/ui/Button'
import { Input } from '../components/ui/Field'

interface LoginPageProps {
  authConfig: AuthConfig
}

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
        <div className="w-full border-t border-subtle" />
      </div>
      <div className="relative flex justify-center text-xs">
        <span className="px-3 bg-surface-overlay text-tertiary">{label}</span>
      </div>
    </div>
  )
}

// The mark's master path (design/brand), pasted: the brand module is a build
// tool and is never imported here. White on the navy, as on every dark surface.
const MARK_PATH = 'M 46.5 12.77 A 5 5 0 1 1 49.5 12.77 L 49.5 20.031 A 36 36 0 0 1 81.941 44 L 88 44 A 4 4 0 0 1 92 48 L 92 60 A 4 4 0 0 1 88 64 L 83.1 64 A 36 36 0 0 1 12.9 64 L 8 64 A 4 4 0 0 1 4 60 L 4 48 A 4 4 0 0 1 8 44 L 14.059 44 A 36 36 0 0 1 46.5 20.031 Z M 28 38 L 68 38 A 10 10 0 0 1 78 48 L 78 54 A 10 10 0 0 1 68 64 L 54 64 A 2 2 0 0 0 52 66 A 2 2 0 0 1 50 68 L 28 68 A 10 10 0 0 1 18 58 L 18 48 A 10 10 0 0 1 28 38 Z M 32.4 44.98 L 32.6 42 L 35.4 42 L 35.6 44.98 A 7.2 7.2 0 0 1 37.832 45.905 L 40.081 43.939 L 42.061 45.919 L 40.095 48.168 A 7.2 7.2 0 0 1 41.02 50.4 L 44 50.6 L 44 53.4 L 41.02 53.6 A 7.2 7.2 0 0 1 40.095 55.832 L 42.061 58.081 L 40.081 60.061 L 37.832 58.095 A 7.2 7.2 0 0 1 35.6 59.02 L 35.4 62 L 32.6 62 L 32.4 59.02 A 7.2 7.2 0 0 1 30.168 58.095 L 27.919 60.061 L 25.939 58.081 L 27.905 55.832 A 7.2 7.2 0 0 1 26.98 53.6 L 24 53.4 L 24 50.6 L 26.98 50.4 A 7.2 7.2 0 0 1 27.905 48.168 L 25.939 45.919 L 27.919 43.939 L 30.168 45.905 A 7.2 7.2 0 0 1 32.4 44.98 Z M 30.4 52 A 3.6 3.6 0 1 0 37.6 52 A 3.6 3.6 0 1 0 30.4 52 Z M 57 52 A 5 5 0 1 1 67 52 A 5 5 0 1 1 57 52 Z'

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex items-center justify-center min-h-screen bg-hero-bg px-4">
      <div className="w-full max-w-sm">
        <div className="text-center mb-6">
          <svg viewBox="0 0 96 96" width={48} height={48} className="mx-auto mb-3 text-hero-text" aria-hidden="true">
            <path fill="currentColor" fillRule="evenodd" d={MARK_PATH} />
          </svg>
          <h1 className="font-display text-2xl font-bold text-hero-text tracking-tight">OtoDock</h1>
          <p className="text-sm text-hero-text-muted mt-1">Your AI agents workspace</p>
        </div>
        <div className="bg-surface-overlay text-primary rounded-hero shadow-elevation-4 p-6">
          {children}
          {/* The stores require a link to the privacy policy on every screen a
              person meets before signing in (the sign-in, the reset, the
              second factor); the web dashboard links nothing. */}
          {inAppShell() && (
            <button type="button" onClick={openPrivacyPolicy}
              className="mt-4 w-full text-xs text-tertiary underline hover:text-primary">
              Privacy policy
            </button>
          )}
        </div>
      </div>
    </div>
  )
}

export default function LoginPage({ authConfig }: LoginPageProps) {
  const { setUser } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  // The login page renders IN PLACE of the page the visitor asked for
  // (RequireAuth), so a signed-in visitor lands where they were going — a
  // full-screen app link, a settings tab — never a `next` parameter.
  const returnToPage = () =>
    navigate(location.pathname + location.search + location.hash, { replace: true })

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

  // A signed-out page has no dashboard socket: it learns about a new build
  // on its own, so a login page left open across an upgrade reloads onto
  // the bundle that matches the server. The watch rests while a reload
  // would lose what the person is doing: a typed password, a sign-in
  // request or passkey ceremony in flight, the second-factor step (its
  // token lives only in this component).
  useBuildWatch(!needs2FA && !loading && password === '')

  // Forgot password
  const [showForgot, setShowForgot] = useState(false)
  const [forgotEmail, setForgotEmail] = useState('')
  const [forgotSent, setForgotSent] = useState(false)

  // Passkeys: passwordless primary sign-in only when the admin mode allows it.
  // The native app webview can't run WebAuthn — there the button opens the
  // system browser at /native-passkey (same SSO rails: openAuthBrowser →
  // deep-link handoff).
  const isNative = isNativePlatform()
  const nativeBridge = isNative && hasNativeBridge()
  // Passkeys are bound to the configured public host (the RP): on any OTHER
  // origin — localhost, a LAN IP — the browser refuses the ceremony with its
  // own scary security error. Hide the in-page buttons there and say where
  // passkeys work instead. The native flow is exempt: its system-browser leg
  // opens the public URL, so the origin matches by construction.
  const rpHost = authConfig.passkey_rp_host || ''
  const rpMismatch = !!rpHost && window.location.hostname !== rpHost
  const passwordlessPasskey = authConfig.passkeys_enabled
    && authConfig.passkey_login_mode !== 'second_factor'
    && (isNative ? nativeBridge : passkeySupported() && !rpMismatch)
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
        returnToPage()
      }
    } catch (err) {
      // Turnstile tokens are single-use — get a fresh one for the retry.
      turnstileRef.current?.reset()
      setTurnstileToken('')
      setError(errorText(err) || 'Login failed')
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
      returnToPage()
    } catch (err) {
      setError(errorText(err) || '2FA verification failed')
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
      returnToPage()
    } catch (err) {
      if (errorName(err) !== 'NotAllowedError') setError(errorText(err) || 'Passkey verification failed')
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
      // Bind the sign-in to this webview: the nonce stays in its cookie
      // (persisted before the system browser takes the foreground) and rides
      // to the system browser in the page URL.
      setError('')
      let handoff: string
      try {
        handoff = await startNativeHandoff()
      } catch (err) {
        setError(errorText(err) || 'Passkey sign-in failed')
        return
      }
      callNative('flushCookies')
      void openNativeBrowser(
        `${window.location.origin}/native-passkey?h=${encodeURIComponent(handoff)}`, 'auth')
      return
    }
    setError('')
    setLoading(true)
    try {
      const user = await passkeyLogin()
      setUser(user)
      returnToPage()
    } catch (err) {
      // Cancelling the browser dialog is not an error worth showing.
      if (errorName(err) !== 'NotAllowedError') setError(errorText(err) || 'Passkey sign-in failed')
    } finally {
      setLoading(false)
    }
  }

  // Forgot password form
  if (showForgot) {
    return (
      <Shell>
        <h2 className="font-display text-lg font-semibold text-primary mb-1">Reset password</h2>
        <p className="text-sm text-secondary mb-5">
          Enter your email and we'll send you a reset link.
        </p>
        {forgotSent ? (
          <div className="space-y-4">
            <p className="text-sm text-status-success-fg">
              If your email is registered, you'll receive a reset link shortly.
            </p>
            <button onClick={() => { setShowForgot(false); setForgotSent(false) }}
              className="w-full px-4 py-2 text-sm font-medium text-link hover:underline">
              Back to sign in
            </button>
          </div>
        ) : (
          <form onSubmit={handleForgotPassword} className="space-y-4">
            <Input type="email" value={forgotEmail || email}
              onChange={e => setForgotEmail(e.target.value)}
              placeholder="Email address" required className="h-control-lg" />
            <Button variant="primary" size="lg" type="submit" disabled={loading}
              className="w-full">
              {loading ? 'Sending...' : 'Send reset link'}
            </Button>
            <button type="button" onClick={() => setShowForgot(false)}
              className="w-full px-4 py-2 text-sm text-secondary hover:text-primary">
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
        <h2 className="font-display text-lg font-semibold text-primary mb-1">Verify it's you</h2>
        <p className="text-sm text-secondary mb-5">
          {stepPasskey && stepTotp
            ? 'Use your passkey, or enter a code from your authenticator app.'
            : stepPasskey
              ? 'Confirm with your passkey to finish signing in.'
              : 'Enter the 6-digit code from your authenticator app, or a recovery code.'}
        </p>
        {stepPasskeyElsewhere && (
          <p className="text-xs text-tertiary mb-4">
            Your passkey works when you open OtoDock at{' '}
            <span className="font-medium text-secondary">https://{rpHost}</span>
            {stepTotp
              ? '. Here, use your authenticator code instead.'
              : '. This address cannot use it, and this account has no authenticator app. Sign in from that address, or ask an admin to reset your second factor.'}
          </p>
        )}
        {error && <div className="text-sm text-status-danger-fg mb-4">{error}</div>}

        {stepPasskey && (
          <Button variant="primary" size="lg" onClick={handleStepPasskey} disabled={loading}
            className="w-full">
            <PasskeyIcon />
            {loading ? 'Waiting for your passkey...' : 'Use your passkey'}
          </Button>
        )}

        {stepPasskey && stepTotp && <Divider label="Or enter a code" />}

        {stepTotp && (
          <form onSubmit={handle2FA} className="space-y-4">
            <Input type="text" value={totpCode}
              onChange={e => setTotpCode(e.target.value)}
              placeholder="000000" required autoFocus={!stepPasskey}
              maxLength={32} autoComplete="one-time-code"
              className="h-control-lg text-center tracking-widest font-mono" />
            <Button variant={stepPasskey ? 'secondary' : 'primary'} size="lg" type="submit" disabled={loading}
              className="w-full">
              {loading ? 'Verifying...' : 'Verify code'}
            </Button>
          </form>
        )}

        <button type="button"
          onClick={() => { setNeeds2FA(false); setTotpCode(''); setSecondFactors(['totp']); setError('') }}
          className="mt-4 w-full px-4 py-2 text-sm text-secondary hover:text-primary">
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
      {error && <div className="text-sm text-status-danger-fg mb-4">{error}</div>}

      {authConfig.oidc_enabled && (
        <>
          <Button variant="primary" size="lg" onClick={handleOidcLogin}
            className="w-full">
            <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
            </svg>
            Continue with {authConfig.oidc_provider_name}
          </Button>
          <Divider label="Or use a local account" />
        </>
      )}

      {passwordlessPasskey && (
        <>
          <Button variant={authConfig.oidc_enabled ? 'secondary' : 'primary'} size="lg"
            onClick={handlePasskeyLogin} disabled={loading}
            className="w-full">
            <PasskeyIcon />
            {authConfig.oidc_enabled
              ? 'Sign in with a local-account passkey'
              : 'Sign in with a passkey'}
          </Button>
          <Divider label="Or use your email" />
        </>
      )}

      <form onSubmit={handleLogin} className="space-y-3.5">
        <div>
          <label className="block text-xs font-medium text-secondary mb-1.5">Email</label>
          {/* No `webauthn` autocomplete token: it primes passkey autofill in
              the password manager, and without a deliberate conditional-
              mediation ceremony it only invites the wrong-account trap the
              explicit button already guards against. */}
          <Input type="email" value={email} onChange={e => setEmail(e.target.value)}
            required autoComplete="email"
            autoFocus={!passwordlessPasskey} placeholder="you@example.com" className="h-control-lg" />
        </div>
        <div>
          <div className="flex items-center justify-between mb-1.5">
            <label className="block text-xs font-medium text-secondary">Password</label>
            {authConfig.email_links_available && (
              <button type="button" onClick={() => setShowForgot(true)}
                className="text-xs text-tertiary hover:text-link transition-colors">
                Forgot password?
              </button>
            )}
          </div>
          <Input type="password" value={password} onChange={e => setPassword(e.target.value)}
            required autoComplete="current-password" placeholder="••••••••" className="h-control-lg" />
        </div>

        {authConfig.turnstile_site_key && (
          <TurnstileWidget ref={turnstileRef} siteKey={authConfig.turnstile_site_key}
            onToken={setTurnstileToken} />
        )}

        <Button variant={passwordlessPasskey || authConfig.oidc_enabled ? 'secondary' : 'primary'} size="lg"
          type="submit" disabled={loading} className="w-full">
          {loading ? 'Signing in...' : 'Sign in'}
        </Button>
      </form>
    </Shell>
  )
}
