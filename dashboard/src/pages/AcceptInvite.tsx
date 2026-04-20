import { useState } from 'react'
import { useSearchParams, useNavigate } from 'react-router-dom'
import { acceptInvite } from '../api/auth'

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
      <div className="flex items-center justify-center min-h-screen bg-p-bg">
        <div className="w-full max-w-sm mx-4">
          <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-6 shadow-xs text-center">
            <h1 className="text-xl font-semibold text-p-text mb-2">Invalid Link</h1>
            <p className="text-sm text-p-text-secondary mb-4">This invite link is invalid or has expired. Ask your administrator for a new one.</p>
            <button onClick={() => navigate('/')}
              className="px-4 py-2 text-sm font-medium text-brand hover:underline">
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
    } catch (err: any) {
      setError(err.message || 'Failed to activate account')
    } finally {
      setLoading(false)
    }
  }

  if (success) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-p-bg">
        <div className="w-full max-w-sm mx-4">
          <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-6 shadow-xs text-center">
            <h1 className="text-xl font-semibold text-p-text mb-2">Account Activated</h1>
            <p className="text-sm text-green-600 dark:text-green-400 mb-4">
              Your account is ready. Sign in with your email and new password.
            </p>
            <button onClick={() => navigate('/')}
              className="px-4 py-2 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg">
              Sign In
            </button>
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-p-bg">
      <div className="w-full max-w-sm mx-4">
        <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-6 shadow-xs">
          <h1 className="text-xl font-semibold text-p-text mb-1">Welcome to OtoDock</h1>
          <p className="text-sm text-p-text-secondary mb-6">Choose a password to activate your account.</p>

          {error && <div className="text-sm text-p-accent-red mb-4">{error}</div>}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-p-text-secondary mb-1">Password</label>
              <input type="password" value={newPassword}
                onChange={e => setNewPassword(e.target.value)}
                required autoComplete="new-password" autoFocus
                className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
            </div>
            <div>
              <label className="block text-xs font-medium text-p-text-secondary mb-1">Confirm Password</label>
              <input type="password" value={confirmPassword}
                onChange={e => setConfirmPassword(e.target.value)}
                required autoComplete="new-password"
                className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
            </div>
            <button type="submit" disabled={loading}
              className="w-full px-4 py-2 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50 transition-colors">
              {loading ? 'Activating...' : 'Activate Account'}
            </button>
          </form>
        </div>
      </div>
    </div>
  )
}
