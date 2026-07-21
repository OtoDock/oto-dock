import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { setupFirstUser } from '../api/auth'
import PasswordStrengthBar from '../components/PasswordStrengthBar'

export default function SetupWizard() {
  const { setUser } = useAuth()
  const navigate = useNavigate()

  const [displayName, setDisplayName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [urlCaptured, setUrlCaptured] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError('')

    if (password !== confirmPassword) {
      setError('Passwords do not match')
      return
    }

    setLoading(true)
    try {
      const { user, dashboardUrlCaptured } = await setupFirstUser(email, password, displayName)
      setUser(user)
      if (dashboardUrlCaptured) {
        // One-time note: the captured origin applies at the next platform
        // restart (Collabora reads it at container start) — show it before
        // leaving the wizard, there is no other done-screen.
        setUrlCaptured(true)
      } else {
        navigate('/admin/platform', { replace: true })
      }
    } catch (err: any) {
      setError(err.message || 'Setup failed')
    } finally {
      setLoading(false)
    }
  }

  if (urlCaptured) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-p-bg">
        <div className="w-full max-w-md mx-4">
          <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-6 shadow-xs text-center">
            <h1 className="text-2xl font-bold text-p-text">You're all set</h1>
            <p className="text-sm text-p-text-secondary mt-3">
              Document preview was pinned to{' '}
              <span className="font-mono text-p-text">{window.location.origin}</span>.
              To activate it, run <span className="font-mono">docker compose up -d</span>{' '}
              once (or restart the platform). Everything else works right away.
            </p>
            <button
              onClick={() => navigate('/admin/platform', { replace: true })}
              className="mt-5 w-full py-2 text-sm font-medium rounded-lg bg-brand text-white hover:opacity-90 transition-opacity"
            >
              Continue to dashboard
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-p-bg">
      <div className="w-full max-w-md mx-4">
        <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-6 shadow-xs">
          <div className="text-center mb-6">
            <h1 className="text-2xl font-bold text-p-text">Welcome to OtoDock</h1>
            <p className="text-sm text-p-text-secondary mt-2">
              Create your administrator account to get started.
            </p>
          </div>

          {error && <div className="text-sm text-p-accent-red mb-4 p-3 bg-red-50 dark:bg-red-900/20 rounded-lg">{error}</div>}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-p-text-secondary mb-1">Display Name</label>
              <input type="text" value={displayName} onChange={e => setDisplayName(e.target.value)}
                required autoFocus placeholder="John Smith"
                className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
            </div>

            <div>
              <label className="block text-xs font-medium text-p-text-secondary mb-1">Email</label>
              <input type="email" value={email} onChange={e => setEmail(e.target.value)}
                required placeholder="admin@example.com" autoComplete="email"
                className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
            </div>

            <div>
              <label className="block text-xs font-medium text-p-text-secondary mb-1">Password</label>
              <input type="password" value={password} onChange={e => setPassword(e.target.value)}
                required autoComplete="new-password"
                className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
              <PasswordStrengthBar password={password} />
            </div>

            <div>
              <label className="block text-xs font-medium text-p-text-secondary mb-1">Confirm Password</label>
              <input type="password" value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)}
                required autoComplete="new-password"
                className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
            </div>

            <button type="submit" disabled={loading}
              className="w-full px-4 py-2.5 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50 transition-colors">
              {loading ? 'Creating account...' : 'Create Admin Account'}
            </button>
          </form>
        </div>
      </div>
    </div>
  )
}
