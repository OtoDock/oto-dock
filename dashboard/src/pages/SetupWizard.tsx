import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { setupFirstUser } from '../api/auth'
import PasswordStrengthBar from '../components/PasswordStrengthBar'
import { errorText } from '../lib/http/apiJson'
import Button from '../components/ui/Button'
import { Input } from '../components/ui/Field'

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
    } catch (err) {
      setError(errorText(err) || 'Setup failed')
    } finally {
      setLoading(false)
    }
  }

  if (urlCaptured) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-hero-bg">
        <div className="w-full max-w-md mx-4">
          <div className="bg-surface-overlay text-primary rounded-hero shadow-elevation-4 p-6 text-center">
            <h1 className="font-display text-2xl font-bold text-primary">You're all set</h1>
            <p className="text-sm text-secondary mt-3">
              Document preview was pinned to{' '}
              <span className="font-mono text-primary">{window.location.origin}</span>.
              To activate it, run <span className="font-mono">docker compose up -d</span>{' '}
              once, or restart the platform. Everything else works right away.
            </p>
            <Button variant="primary" size="lg"
              onClick={() => navigate('/admin/platform', { replace: true })}
              className="mt-5 w-full"
            >
              Continue to dashboard
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-hero-bg">
      <div className="w-full max-w-md mx-4">
        <div className="bg-surface-overlay text-primary rounded-hero shadow-elevation-4 p-6">
          <div className="text-center mb-6">
            <h1 className="font-display text-2xl font-bold text-primary">Welcome to OtoDock</h1>
            <p className="text-sm text-secondary mt-2">
              Create your administrator account to get started.
            </p>
          </div>

          {error && <div className="text-sm text-status-danger-fg mb-4 p-3 bg-status-danger-bg rounded-card">{error}</div>}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-secondary mb-1">Display Name</label>
              <Input type="text" value={displayName} onChange={e => setDisplayName(e.target.value)}
                required autoFocus placeholder="John Smith" className="h-control-lg" />
            </div>

            <div>
              <label className="block text-xs font-medium text-secondary mb-1">Email</label>
              <Input type="email" value={email} onChange={e => setEmail(e.target.value)}
                required placeholder="admin@example.com" autoComplete="email" className="h-control-lg" />
            </div>

            <div>
              <label className="block text-xs font-medium text-secondary mb-1">Password</label>
              <Input type="password" value={password} onChange={e => setPassword(e.target.value)}
                required autoComplete="new-password" className="h-control-lg" />
              <PasswordStrengthBar password={password} />
            </div>

            <div>
              <label className="block text-xs font-medium text-secondary mb-1">Confirm Password</label>
              <Input type="password" value={confirmPassword} onChange={e => setConfirmPassword(e.target.value)}
                required autoComplete="new-password" className="h-control-lg" />
            </div>

            <Button variant="primary" size="lg" type="submit" disabled={loading}
              className="w-full">
              {loading ? 'Creating account...' : 'Create Admin Account'}
            </Button>
          </form>
        </div>
      </div>
    </div>
  )
}
