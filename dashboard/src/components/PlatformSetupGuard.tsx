/**
 * Platform setup guard — ensures at least one execution layer subscription
 * is configured before allowing access to agent features.
 *
 * Admin: sees a dismissible banner linking to Platform → Execution Layers.
 * Non-admin without access: sees a full-page setup required screen with
 * link to Settings (to add their own subscription).
 *
 * Settings page is always accessible (bypasses the guard).
 */

import { Outlet, useLocation, Link } from 'react-router-dom'
import { useAuth } from '../contexts/AuthContext'

export function SetupBanner() {
  const { user } = useAuth()

  // Per-user nudge, shown to ANY role until the user connects their OWN AI
  // engine (a personal Claude Code or Codex subscription). It keeps showing even
  // when they can borrow a platform sub, because borrowing covers agent/voice
  // work — their own user-scoped chats need their own engine. Clicks through to
  // User Settings → AI Engines (where admins can also tick "contribute to the
  // platform"). undefined ⇒ older payload / still loading ⇒ stay hidden.
  if (!user || user.has_own_engine !== false) {
    return null
  }

  return (
    <div className="bg-amber-50 dark:bg-amber-900/20 border-b border-amber-200 dark:border-amber-800 px-4 py-2.5 flex items-center justify-between relative z-50">
      <div className="flex items-center gap-2 text-sm text-amber-800 dark:text-amber-300">
        <svg className="w-4 h-4 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.268 16.5c-.77.833.192 2.5 1.732 2.5z" />
        </svg>
        <span>
          <strong>Connect an AI engine</strong> — add Claude Code or Codex in your User Settings to run your own chats and agents.
        </span>
      </div>
      <Link
        to="/user-settings?tab=ai-engines"
        className="shrink-0 px-3 py-1 text-xs font-medium rounded-lg bg-amber-600 text-white hover:bg-amber-700 transition-colors"
      >
        Connect AI engine
      </Link>
    </div>
  )
}

export default function PlatformSetupGuard() {
  const { user, logout } = useAuth()
  const location = useLocation()

  // Always allow settings page (user can add their own subscription)
  if (location.pathname === '/user-settings') {
    return <Outlet />
  }

  // Always allow admin pages (admin needs access to configure)
  if (user?.role === 'admin') {
    return <Outlet />
  }

  // Non-admin without platform configured → block
  if (user && user.platform_configured === false) {
    return (
      <div className="flex items-center justify-center min-h-screen bg-p-bg">
        <div className="max-w-md mx-auto px-6 text-center">
          <div className="w-16 h-16 mx-auto mb-6 rounded-full bg-amber-100 dark:bg-amber-900/30 flex items-center justify-center">
            <svg className="w-8 h-8 text-amber-600 dark:text-amber-400" fill="none" viewBox="0 0 24 24" stroke="currentColor">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
            </svg>
          </div>
          <h1 className="text-xl font-semibold text-p-text mb-2">Platform Setup Required</h1>
          <p className="text-sm text-p-text-secondary mb-6">
            The platform needs to be configured before you can use it. Your administrator needs to set up at least one execution layer subscription, or you can connect your own.
          </p>
          <Link
            to="/user-settings?tab=ai-engines"
            className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg bg-brand text-white hover:bg-brand-hover transition-colors"
          >
            Connect Your Subscription
          </Link>
          {/* This screen replaces the whole app shell (no account menu), so
              it must carry its own exit — without this a gated user has no
              way to sign out. */}
          <div className="mt-4">
            <button
              onClick={logout}
              className="inline-flex items-center gap-2 px-4 py-2 text-sm font-medium rounded-lg border border-p-border-light text-p-text-secondary hover:bg-p-surface-hover hover:text-p-text transition-colors"
            >
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
                  d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
              </svg>
              Sign out
            </button>
          </div>
        </div>
      </div>
    )
  }

  return <Outlet />
}
