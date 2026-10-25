import { Navigate, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'
import { useFcmPush } from '../hooks/useFcmPush'
import { useWakeWord } from '../hooks/useWakeWord'
import { useBuildWatch } from '../hooks/useBuildWatch'
import { useOpenAppToast } from '../hooks/useOpenAppToast'
import WakeDiagBadge from './WakeDiagBadge'
import LoginPage from '../pages/LoginPage'
import SetupWizard from '../pages/SetupWizard'
import Button from './ui/Button'

export default function RequireAuth() {
  const { user, loading, authConfig, loginError, login } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()

  // Native FCM registration, mounted once at the authenticated app root so it is
  // route-independent; gated on auth (the subscribe endpoint is auth-only).
  useFcmPush(navigate, !!user)

  // Ambient "hey <agent>" listener — same route-independent mount point.
  // Inert unless the user's wake-word opt-in (default OFF) and every other
  // gate in the hook pass; yields the mic to duplex/dictation.
  useWakeWord(navigate, !!user)

  // Pages without a dashboard socket learn about a new build when they
  // return to the foreground (chat pages learn it over the socket).
  useBuildWatch(!!user)

  // An agent's request to show one of its apps that the current page could
  // not handle itself becomes a notice with an Open button, route-independent.
  const openAppToast = useOpenAppToast(navigate, !!user)

  // The boot screen stays over the placeholders (data-boot-wait) and leaves
  // at the first real screen: the wizard, the login page, the redirect
  // notice or the page itself (lib/device/boot.ts watches the root).
  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-surface-page" data-boot-wait="">
        <p className="text-sm text-secondary">Loading...</p>
      </div>
    )
  }

  if (!user) {
    // 1. Setup wizard: no users exist yet
    if (authConfig?.setup_required) {
      return <SetupWizard />
    }

    // 2. Bypass mode: go straight to OIDC (existing Authentik behavior).
    // A refused start says why (the server's reason, a 429's wait
    // included) and is not repeated until the person asks again.
    if (authConfig?.auth_provider_bypass && authConfig?.oidc_enabled) {
      if (loginError) {
        return (
          <div className="flex flex-col items-center justify-center gap-3 min-h-screen bg-surface-page px-4 text-center">
            <p role="alert" className="text-sm text-status-danger-fg max-w-md">{loginError}</p>
            <Button size="sm" onClick={() => window.location.reload()}>Try again</Button>
          </div>
        )
      }
      login()
      return (
        <div className="flex items-center justify-center min-h-screen bg-surface-page">
          <p className="text-sm text-secondary">Redirecting to login...</p>
        </div>
      )
    }

    // 3. Normal: show login page
    if (authConfig) {
      return <LoginPage authConfig={authConfig} />
    }

    // Fallback while config loads
    return (
      <div className="flex items-center justify-center min-h-screen bg-surface-page" data-boot-wait="">
        <p className="text-sm text-secondary">Loading...</p>
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

  // The wake-word diagnostics badge renders beside the outlet (not inside a
  // page) so it survives the wake navigation; it is null unless the
  // recorder is on.
  return (
    <>
      <Outlet />
      <WakeDiagBadge />
      {openAppToast}
    </>
  )
}
