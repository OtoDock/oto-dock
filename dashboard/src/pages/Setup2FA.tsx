import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import QRCode from 'qrcode'
import { useAuth } from '../contexts/AuthContext'
import { apiFetch, fetchCurrentUser, logout } from '../api/auth'

/**
 * Forced 2FA enrollment (admin require-2FA policy). Mirrors the
 * must-change-password flow: RequireAuth routes here until the account has a
 * second factor. Sign-out stays available so nobody is ever trapped.
 */
export default function Setup2FA() {
  const { setUser } = useAuth()
  const navigate = useNavigate()

  const [setup, setSetup] = useState<{ secret: string; qr_uri: string; recovery_codes: string[] } | null>(null)
  const [code, setCode] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [qrDataUrl, setQrDataUrl] = useState('')

  useEffect(() => {
    let cancelled = false
    apiFetch('/v1/users/me/totp/setup', { method: 'POST' })
      .then(res => { if (!res.ok) throw new Error(); return res.json() })
      .then(data => { if (!cancelled) setSetup(data) })
      .catch(() => { if (!cancelled) setError('Failed to start 2FA setup') })
    return () => { cancelled = true }
  }, [])

  // Render the TOTP QR locally — the otpauth:// URI embeds the shared 2FA
  // secret, so it must never be sent to a third-party QR image service.
  useEffect(() => {
    if (!setup?.qr_uri) { setQrDataUrl(''); return }
    let cancelled = false
    QRCode.toDataURL(setup.qr_uri, { width: 200, margin: 1 })
      .then(url => { if (!cancelled) setQrDataUrl(url) })
      .catch(() => { if (!cancelled) setQrDataUrl('') })
    return () => { cancelled = true }
  }, [setup?.qr_uri])

  async function handleVerify(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      const res = await apiFetch('/v1/users/me/totp/verify', {
        method: 'POST',
        body: JSON.stringify({ code }),
      })
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || 'Invalid code') }
      const u = await fetchCurrentUser()
      if (u) setUser(u)
      navigate('/', { replace: true })
    } catch (err: any) {
      setError(err.message || 'Verification failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-p-bg">
      <div className="w-full max-w-md mx-4 my-8">
        <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-6 shadow-xs">
          <h1 className="text-xl font-semibold text-p-text mb-1">Set Up Two-Factor Authentication</h1>
          <p className="text-sm text-p-text-secondary mb-6">
            Your administrator requires two-factor authentication for this platform.
          </p>

          {error && <div className="text-sm text-p-accent-red mb-4">{error}</div>}

          {setup ? (
            <div className="space-y-4">
              <div>
                <p className="text-sm text-p-text mb-2">Scan this QR code with your authenticator app:</p>
                <div className="bg-white p-4 rounded-lg inline-block border border-p-border-light">
                  {qrDataUrl
                    ? <img src={qrDataUrl} alt="TOTP QR Code" className="w-48 h-48" />
                    : <div className="w-48 h-48" />}
                </div>
              </div>
              <div>
                <p className="text-xs text-p-text-secondary mb-1">Or enter this key manually:</p>
                <code className="text-xs font-mono bg-p-bg px-2 py-1 rounded-sm select-all">{setup.secret}</code>
              </div>
              <div className="p-3 bg-amber-50 dark:bg-amber-900/20 border border-amber-200 dark:border-amber-800 rounded-lg">
                <p className="text-xs font-medium text-amber-800 dark:text-amber-300 mb-1">Recovery Codes</p>
                <p className="text-xs text-amber-700 dark:text-amber-400 mb-2">Save these codes in a safe place. Each can only be used once.</p>
                <div className="grid grid-cols-2 gap-1">
                  {setup.recovery_codes.map((rc, i) => (
                    <code key={i} className="text-xs font-mono bg-white dark:bg-p-surface px-2 py-1 rounded-sm">{rc}</code>
                  ))}
                </div>
              </div>
              <form onSubmit={handleVerify} className="space-y-3">
                <div>
                  <label className="block text-xs text-p-text-secondary mb-1">Enter verification code</label>
                  <input type="text" value={code} onChange={e => setCode(e.target.value)}
                    placeholder="000000" maxLength={6} autoComplete="one-time-code" autoFocus
                    className="w-32 px-3 py-2 text-sm text-center font-mono border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
                </div>
                <button type="submit" disabled={loading || code.length !== 6}
                  className="w-full px-4 py-2 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50 transition-colors">
                  {loading ? 'Verifying...' : 'Verify & Continue'}
                </button>
              </form>
            </div>
          ) : !error ? (
            <p className="text-sm text-p-text-secondary">Preparing setup...</p>
          ) : null}

          <button onClick={() => logout()}
            className="mt-4 w-full text-center text-xs text-p-text-secondary hover:text-p-text">
            Sign out
          </button>
        </div>
      </div>
    </div>
  )
}
