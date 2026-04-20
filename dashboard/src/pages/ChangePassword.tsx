import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { apiFetch, fetchCurrentUser } from '../api/auth'
import PasswordStrengthBar from '../components/PasswordStrengthBar'

export default function ChangePassword() {
  const { user, setUser } = useAuth()
  const navigate = useNavigate()

  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    setError('')

    if (newPassword !== confirmPassword) {
      setError('Passwords do not match')
      return
    }

    setLoading(true)
    try {
      const res = await apiFetch('/v1/users/me/password', {
        method: 'PUT',
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      })
      if (!res.ok) {
        const data = await res.json().catch(() => ({}))
        throw new Error(data.detail || 'Failed to change password')
      }
      // Refresh user to clear must_change_password flag
      const updatedUser = await fetchCurrentUser()
      if (updatedUser) setUser(updatedUser)
      navigate('/', { replace: true })
    } catch (err: any) {
      setError(err.message || 'Failed to change password')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-p-bg">
      <div className="w-full max-w-sm mx-4">
        <div className="border border-p-border-light rounded-xl bg-white dark:bg-p-surface p-6 shadow-xs">
          <h1 className="text-xl font-semibold text-p-text mb-1">Change Password</h1>
          <p className="text-sm text-p-text-secondary mb-6">
            {user?.must_change_password
              ? 'You must set a new password before continuing.'
              : 'Enter a new password for your account.'}
          </p>

          {error && <div className="text-sm text-p-accent-red mb-4">{error}</div>}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-p-text-secondary mb-1">Current Password</label>
              <input type="password" value={currentPassword}
                onChange={e => setCurrentPassword(e.target.value)}
                required autoComplete="current-password" autoFocus
                className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
            </div>

            <div>
              <label className="block text-xs font-medium text-p-text-secondary mb-1">New Password</label>
              <input type="password" value={newPassword}
                onChange={e => setNewPassword(e.target.value)}
                required autoComplete="new-password"
                className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
              <PasswordStrengthBar password={newPassword} />
            </div>

            <div>
              <label className="block text-xs font-medium text-p-text-secondary mb-1">Confirm New Password</label>
              <input type="password" value={confirmPassword}
                onChange={e => setConfirmPassword(e.target.value)}
                required autoComplete="new-password"
                className="w-full px-3 py-2 text-sm border border-p-border-light rounded-lg bg-white dark:bg-p-surface text-p-text focus:outline-hidden focus:ring-2 focus:ring-brand/30" />
            </div>

            <button type="submit" disabled={loading}
              className="w-full px-4 py-2 text-sm font-medium text-white bg-brand hover:bg-brand-hover rounded-lg disabled:opacity-50 transition-colors">
              {loading ? 'Changing...' : 'Change Password'}
            </button>
          </form>
        </div>
      </div>
    </div>
  )
}
