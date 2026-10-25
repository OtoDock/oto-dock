import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { fetchCurrentUser } from '../api/auth'
import PasswordStrengthBar from '../components/PasswordStrengthBar'
import { errorText, apiOk } from '../lib/http/apiJson'
import Button from '../components/ui/Button'
import { Input } from '../components/ui/Field'

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
      await apiOk('/v1/users/me/password', {
        method: 'PUT',
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      }, 'Failed to change password')
      // Refresh user to clear must_change_password flag
      const updatedUser = await fetchCurrentUser()
      if (updatedUser) setUser(updatedUser)
      navigate('/', { replace: true })
    } catch (err) {
      setError(errorText(err) || 'Failed to change password')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="flex items-center justify-center min-h-screen bg-hero-bg">
      <div className="w-full max-w-sm mx-4">
        <div className="bg-surface-overlay text-primary rounded-hero shadow-elevation-4 p-6">
          <h1 className="font-display text-xl font-semibold text-primary mb-1">Change Password</h1>
          <p className="text-sm text-secondary mb-6">
            {user?.must_change_password
              ? 'You must set a new password before continuing.'
              : 'Enter a new password for your account.'}
          </p>

          {error && <div className="text-sm text-status-danger-fg mb-4">{error}</div>}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-xs font-medium text-secondary mb-1">Current Password</label>
              <Input type="password" value={currentPassword}
                onChange={e => setCurrentPassword(e.target.value)}
                required autoComplete="current-password" autoFocus className="h-control-lg" />
            </div>

            <div>
              <label className="block text-xs font-medium text-secondary mb-1">New Password</label>
              <Input type="password" value={newPassword}
                onChange={e => setNewPassword(e.target.value)}
                required autoComplete="new-password" className="h-control-lg" />
              <PasswordStrengthBar password={newPassword} />
            </div>

            <div>
              <label className="block text-xs font-medium text-secondary mb-1">Confirm New Password</label>
              <Input type="password" value={confirmPassword}
                onChange={e => setConfirmPassword(e.target.value)}
                required autoComplete="new-password" className="h-control-lg" />
            </div>

            <Button variant="primary" size="lg" type="submit" disabled={loading}
              className="w-full">
              {loading ? 'Changing...' : 'Change Password'}
            </Button>
          </form>
        </div>
      </div>
    </div>
  )
}
