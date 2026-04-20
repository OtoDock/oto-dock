import { useState, useRef, useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import type { User } from '../../api/auth'
import RemoteBadge from '../RemoteBadge'

interface Props {
  agentName: string
  displayName?: string
  // The header has two leading-button modes: the chat page passes
  // `onToggleHistory` (hamburger → chat-history drawer); the task run page
  // passes `onBack` (back-arrow → task list) instead. Exactly one is set.
  onToggleHistory?: () => void
  onBack?: () => void
  user: User | null
  onLogout: () => void
  notificationBell?: React.ReactNode
  onAppSettings?: () => void
  // Remote execution target for this session (set by warmup_ready). `null`
  // means local — no badge shown.
  executionTarget?: string | null
  fallbackReason?: string | null
  machineName?: string | null
  machineStatus?: 'online' | 'stale' | 'disconnected' | 'never_connected' | null
  machineLastHeartbeatAgeS?: number | null
  // 'user' = the caller's own paired machine (amber when offline — soft
  // fallback to local); 'admin' = the agent's platform default (red when
  // offline — blocks everyone).
  machineScope?: 'admin' | 'user'
  machineLastSeenIso?: string | null
}

function getInitials(name: string): string {
  const parts = name.trim().split(/\s+/)
  if (parts.length >= 2) return (parts[0][0] + parts[1][0]).toUpperCase()
  return name.slice(0, 2).toUpperCase()
}

export default function TopBar({
  agentName, displayName, onToggleHistory, onBack, user, onLogout, notificationBell, onAppSettings,
  executionTarget, fallbackReason, machineName, machineStatus, machineLastHeartbeatAgeS,
  machineScope, machineLastSeenIso,
}: Props) {
  // Derive the badge state from the agent's admin-paired target. We
  // prefer `machineStatus` (live-polled per-agent target status) over
  // `executionTarget` (current session's resolved target) so the dot
  // reflects whether the satellite is reachable RIGHT NOW — not whether
  // the session happens to be on it. This matters most on a brand-new
  // chat page (no warmup_ready yet) where `executionTarget` defaults to
  // 'local' but the agent's actual default target may be offline.
  //
  // Fallthrough: if no admin-paired status is reported (agent is local
  // or user-paired-only) but the session's resolved target is remote,
  // we still show the dot from the session — preserves the prior
  // behavior for legacy machines without status data.
  let badgeState: 'online' | 'stale' | 'disconnected' | 'never_connected' | 'fellback_local' | null = null
  if (machineStatus) {
    badgeState = machineStatus
  } else if (executionTarget && executionTarget !== 'local') {
    badgeState = 'online'
  } else if (fallbackReason) {
    badgeState = 'fellback_local'
  }
  const [menuOpen, setMenuOpen] = useState(false)
  const menuRef = useRef<HTMLDivElement>(null)
  const navigate = useNavigate()

  useEffect(() => {
    if (!menuOpen) return
    const handler = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false)
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [menuOpen])

  return (
    <div className="absolute top-0 left-0 right-0 z-20 flex items-center h-12 px-3">
      {/* Leading button: back-arrow (task run) or hamburger (chat history). */}
      {onBack ? (
        <button
          onClick={onBack}
          className="w-9 h-9 rounded-xl bg-white/70 dark:bg-gray-900/70 backdrop-blur-xs border border-p-border-light/50 dark:border-gray-600 hover:bg-white/90 dark:hover:bg-gray-800/90 flex items-center justify-center text-p-text-secondary hover:text-p-text transition-colors shadow-xs"
          title="Back"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
        </button>
      ) : (
        <button
          onClick={onToggleHistory}
          className="w-9 h-9 rounded-xl bg-white/70 dark:bg-gray-900/70 backdrop-blur-xs border border-p-border-light/50 dark:border-gray-600 hover:bg-white/90 dark:hover:bg-gray-800/90 flex items-center justify-center text-p-text-secondary hover:text-p-text transition-colors shadow-xs"
          title="Toggle chat history"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
      )}

      {/* Agent pill */}
      <Link
        to="/agents"
        className="ml-2 px-3 py-1.5 rounded-xl bg-white/70 dark:bg-gray-900/70 backdrop-blur-xs border border-p-border-light/50 dark:border-gray-600 hover:bg-white/90 dark:hover:bg-gray-800/90
                   text-sm font-medium text-p-text transition-colors flex items-center gap-1.5 shadow-xs min-w-0 max-w-[45vw] sm:max-w-none"
      >
        <span className="truncate">{displayName || agentName}</span>
        <svg className="w-3 h-3 text-p-text-light shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </Link>

      {/* Remote execution target indicator. Reflects the caller's
          effective target — their own paired machine ('user' scope, amber
          when offline) or the agent's admin default ('admin' scope, red
          when offline). Hover (desktop) or tap (touch) the dot for detail;
          the tooltip is its own affordance and won't trigger the adjacent
          agent-name pill. */}
      {badgeState && (
        <span className="ml-2">
          <RemoteBadge
            state={badgeState}
            scope={machineScope}
            machineName={machineName || undefined}
            heartbeatAgeS={machineLastHeartbeatAgeS ?? null}
            lastSeenIso={machineLastSeenIso ?? null}
            fallbackReason={fallbackReason ?? null}
          />
        </span>
      )}

      <div className="flex-1" />

      {/* Notification bell */}
      {notificationBell}

      {/* User menu */}
      {user && (
        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            className="w-9 h-9 rounded-xl bg-white/70 dark:bg-gray-900/70 backdrop-blur-xs border border-p-border-light/50 dark:border-gray-600 hover:bg-white/90 dark:hover:bg-gray-800/90
                       flex items-center justify-center text-xs font-bold text-p-text-secondary transition-colors shadow-xs"
            title={user.display_name || user.name}
          >
            {getInitials(user.display_name || user.name)}
          </button>

          {menuOpen && (
            <div className="absolute right-0 top-11 w-52 bg-white dark:bg-p-surface rounded-xl shadow-lg border border-p-border-light py-1 z-50">
              <div className="px-3 py-2.5 border-b border-p-border-light">
                {user.display_name && (
                  <p className="text-sm font-medium text-p-text truncate">{user.display_name}</p>
                )}
                <p className={`text-xs truncate ${user.display_name ? 'text-p-text-light' : 'text-sm font-medium text-p-text'}`}>{user.name}</p>
                <p className="text-xs text-p-text-light truncate">{user.email}</p>
              </div>

              <button
                onClick={() => { setMenuOpen(false); navigate('/user-settings') }}
                className="w-full text-left px-3 py-2 text-sm text-p-text-secondary hover:bg-p-surface-hover flex items-center gap-2 transition-colors"
              >
                <svg className="w-4 h-4 text-p-text-light" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5.121 17.804A13.937 13.937 0 0112 16c2.5 0 4.847.655 6.879 1.804M15 10a3 3 0 11-6 0 3 3 0 016 0zm6 2a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
                User Settings
              </button>

              <button
                onClick={() => { setMenuOpen(false); navigate(`/agents/${agentName}`) }}
                className="w-full text-left px-3 py-2 text-sm text-p-text-secondary hover:bg-p-surface-hover flex items-center gap-2 transition-colors"
              >
                <svg className="w-4 h-4 text-p-text-light" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
                </svg>
                Agent Settings
              </button>

              {onAppSettings && (
                <button
                  onClick={() => { setMenuOpen(false); onAppSettings() }}
                  className="w-full text-left px-3 py-2 text-sm text-p-text-secondary hover:bg-p-surface-hover flex items-center gap-2 transition-colors"
                >
                  <svg className="w-4 h-4 text-p-text-light" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 18h.01M8 21h8a2 2 0 002-2V5a2 2 0 00-2-2H8a2 2 0 00-2 2v14a2 2 0 002 2z" />
                  </svg>
                  App Settings
                </button>
              )}

              {user.role === 'admin' && (
                <button
                  onClick={() => { setMenuOpen(false); navigate('/admin') }}
                  className="w-full text-left px-3 py-2 text-sm text-p-text-secondary hover:bg-p-surface-hover flex items-center gap-2 transition-colors"
                >
                  <svg className="w-4 h-4 text-p-text-light" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" />
                  </svg>
                  Admin
                </button>
              )}

              <div className="border-t border-p-border-light my-1" />

              <button
                onClick={() => { setMenuOpen(false); onLogout() }}
                className="w-full text-left px-3 py-2 text-sm text-p-text-secondary hover:bg-p-surface-hover flex items-center gap-2 transition-colors"
              >
                <svg className="w-4 h-4 text-p-text-light" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" />
                </svg>
                Logout
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
