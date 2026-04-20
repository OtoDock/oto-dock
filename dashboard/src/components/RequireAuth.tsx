import { Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useFcmPush } from '../hooks/useFcmPush'
import LoginPage from '../pages/LoginPage'
import SetupWizard from '../pages/SetupWizard'

export default function RequireAuth() {
  const { user, loading, authConfig, login } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()

  // Native FCM registration, mounted once at the authenticated app root so it is
  // route-independent; gated on auth (the subscribe endpoint is auth-only).
  useFcmPush(navigate, !!user)

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-p-bg">
        <p className="text-sm text-p-text-secondary">Loading...</p>
      </div>
    )
  }

  if (!user) {
    // 1. Setup wizard: no users exist yet
    if (authConfig?.setup_required) {
      return <SetupWizard />
    }

    // 2. Bypass mode: go straight to OIDC (existing Authentik behavior)
    if (authConfig?.auth_provider_bypass && authConfig?.oidc_enabled) {
      login()
      return (
        <div className="flex items-center justify-center min-h-screen bg-p-bg">
          <p className="text-sm text-p-text-secondary">Redirecting to login...</p>
        </div>
      )
    }

    // 3. Normal: show login page
    if (authConfig) {
      return <LoginPage authConfig={authConfig} />
    }

    // Fallback while config loads
    return (
      <div className="flex items-center justify-center min-h-screen bg-p-bg">
        <p className="text-sm text-p-text-secondary">Loading...</p>
      </div>
    )
  }

  // Force password change before allowing access (skip if already on the page)
  if (user.must_change_password && location.pathname !== '/change-password') {
    return <Navigate to="/change-password" replace />
  }

  // Require-2FA policy: force enrollment before allowing access (after any
  // password change — the fresh password matters for the 2FA setup confirm).
  if (!user.must_change_password && user.must_enroll_2fa && location.pathname !== '/setup-2fa') {
    return <Navigate to="/setup-2fa" replace />
  }

  return <Outlet />
}
