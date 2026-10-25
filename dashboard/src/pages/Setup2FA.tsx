import { useState, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import QRCode from 'qrcode'
import { useAuth } from '../contexts/AuthContext'
import { apiFetch, fetchCurrentUser, logout } from '../api/auth'
import { errorText, apiOk } from '../lib/http/apiJson'
import Button from '../components/ui/Button'
import { Input } from '../components/ui/Field'

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
  // The QR image of the current secret (another secret shows none).
  const [qr, setQr] = useState<{ uri: string; url: string } | null>(null)
  const qrDataUrl = qr && qr.uri === setup?.qr_uri ? qr.url : ''

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
  const qrUri = setup?.qr_uri
  useEffect(() => {
    if (!qrUri) return
    let cancelled = false
    QRCode.toDataURL(qrUri, { width: 200, margin: 1 })
      .then(url => { if (!cancelled) setQr({ uri: qrUri, url }) })
      .catch(() => { /* no image: the secret is shown as text */ })
    return () => { cancelled = true }
  }, [qrUri])

  async function handleVerify(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    setLoading(true)
    try {
      await apiOk('/v1/users/me/totp/verify', {
        method: 'POST',
        body: JSON.stringify({ code }),
      }, 'Invalid code')
      const u = await fetchCurrentUser()
      if (u) setUser(u)
      navigate('/', { replace: true })
    } catch (err) {
      setError(errorText(err) || 'Verification failed')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-hero-bg">
      <div className="w-full max-w-md mx-4 my-8">
        <div className="bg-surface-overlay text-primary rounded-hero shadow-elevation-4 p-6">
          <h1 className="font-display text-xl font-semibold text-primary mb-1">Set Up Two-Factor Authentication</h1>
          <p className="text-sm text-secondary mb-6">
            Your administrator requires two-factor authentication for this platform.
          </p>

          {error && <div className="text-sm text-status-danger-fg mb-4">{error}</div>}

          {setup ? (
            <div className="space-y-4">
              <div>
                <p className="text-sm text-primary mb-2">Scan this QR code with your authenticator app:</p>
                {/* eslint-disable-next-line otodock/no-raw-palette -- a QR code keeps its light quiet zone in both themes */}
                <div className="bg-white p-4 rounded-card inline-block">
                  {qrDataUrl
                    ? <img src={qrDataUrl} alt="TOTP QR Code" className="w-48 h-48" />
                    : <div className="w-48 h-48" />}
                </div>
              </div>
              <div>
                <p className="text-xs text-secondary mb-1">Or enter this key manually:</p>
                <code className="text-xs font-mono bg-surface-sunken px-2 py-1 rounded-sm select-all">{setup.secret}</code>
              </div>
              <div className="p-3 bg-status-warning-bg border border-status-warning-border rounded-card">
                <p className="text-xs font-medium text-status-warning-fg mb-1">Recovery Codes</p>
                <p className="text-xs text-status-warning-fg mb-2">Save these codes in a safe place. Each can only be used once.</p>
                <div className="grid grid-cols-2 gap-1">
                  {setup.recovery_codes.map((rc, i) => (
                    <code key={i} className="text-xs font-mono bg-surface-card px-2 py-1 rounded-sm">{rc}</code>
                  ))}
                </div>
              </div>
              <form onSubmit={handleVerify} className="space-y-3">
                <div>
                  <label className="block text-xs text-secondary mb-1">Enter verification code</label>
                  <Input type="text" value={code} onChange={e => setCode(e.target.value)}
                    placeholder="000000" maxLength={6} autoComplete="one-time-code" autoFocus
                    className="h-control-lg max-w-32 text-center font-mono" />
                </div>
                <Button variant="primary" size="lg" type="submit" disabled={loading || code.length !== 6}
                  className="w-full">
                  {loading ? 'Verifying...' : 'Verify and Continue'}
                </Button>
              </form>
            </div>
          ) : !error ? (
            <p className="text-sm text-secondary">Preparing setup...</p>
          ) : null}

          <button onClick={() => logout()}
            className="mt-4 w-full text-center text-xs text-secondary hover:text-primary">
            Sign out
          </button>
        </div>
      </div>
    </div>
  )
}
