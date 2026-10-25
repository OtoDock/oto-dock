import { useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { acceptInvite } from '../api/auth'
import { errorText } from '../lib/http/apiJson'
import Button from '../components/ui/Button'
import { Input } from '../components/ui/Field'

export default function AcceptInvite() {
  const [searchParams] = useSearchParams()
  const navigate = useNavigate()
  const token = searchParams.get('token') || ''

  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [success, setSuccess] = useState(false)
  const [loading, setLoading] = useState(false)

  if (!token) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-hero-bg">
        <div className="w-full max-w-sm mx-4">
          <div className="bg-surface-overlay text-primary rounded-hero shadow-elevation-4 p-6 text-center">
            <h1 className="font-display text-xl font-semibold text-primary mb-2">Invalid Link</h1>
            <p className="text-sm text-secondary mb-4">This invite link is invalid or has expired. Ask your administrator for a new one.</p>
            <button onClick={() => navigate('/')}
              className="px-4 py-2 text-sm font-medium text-link hover:underline">
              Go to login
            </button>
          </div>
        </div>
      </div>
    )
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError('')
    if (newPassword !== confirmPassword) {
      setError('Passwords do not match')
      return
    }
    setLoading(true)
    try {
      await acceptInvite(token, newPassword)
      setSuccess(true)
    } catch (err) {
      setError(errorText(err) || 'Failed to activate account')
    } finally {
      setLoading(false)
    }
  }

  if (success) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-hero-bg">
        <div className="w-full max-w-sm mx-4">
          <div className="bg-surface-overlay text-primary rounded-hero shadow-elevation-4 p-6 text-center">
            <h1 className="font-display text-xl font-semibold text-primary mb-2">Account Activated</h1>
            <p className="text-sm text-status-success-fg mb-4">
              Your account is ready. Sign in with your email and new password.
            </p>
            <Button variant="primary" size="md" onClick={() => navigate('/')}>
              Sign In
            </Button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-hero-bg">
      <div className="w-full max-w-sm mx-4">
        <div className="bg-surface-overlay text-primary rounded-hero shadow-elevation-4 p-6">
          <h1 className="font-display text-xl font-semibold text-primary mb-1">Welcome to OtoDock</h1>
          <p className="text-sm text-secondary mb-6">Choose a password to activate your account.</p>

          {error && <div className="text-sm text-status-danger-fg mb-4">{error}</div>}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-secondary mb-1">Password</label>
              <Input type="password" value={newPassword}
                onChange={e => setNewPassword(e.target.value)}
                required autoComplete="new-password" autoFocus className="h-control-lg" />
            </div>
            <div>
              <label className="block text-xs font-medium text-secondary mb-1">Confirm Password</label>
              <Input type="password" value={confirmPassword}
                onChange={e => setConfirmPassword(e.target.value)}
                required autoComplete="new-password" className="h-control-lg" />
            </div>
            <Button variant="primary" size="lg" type="submit" disabled={loading}
              className="w-full">
              {loading ? 'Activating...' : 'Activate Account'}
            </Button>
          </form>
        </div>
      </div>
    </div>
  )
}
