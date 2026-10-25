import { useState, useRef, useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import type { User } from '../../api/auth'
import RemoteBadge from '../RemoteBadge'
import { FELLBACK_LOCAL, type BadgeState } from '../remoteBadgeDot'
import { isAdmin } from '../../lib/permissions'
import { isLocalTarget, type PairingScope } from '../../lib/placement'
import { MACHINE_STATE, type MachineState } from '../../lib/status/machine'
import { IconDeviceMobile, IconGear, IconLogout, IconShieldCheck, IconUser } from '../icons/nav'
import { POPOVER } from '../ui/classes'
import { GLASS, GLASS_HOVER, GLASS_PRESSED } from '../ui/glass'

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
  // The target's live state (`lib/status/machine.ts`; `paused` included).
  machineStatus?: MachineState | null
  machineLastHeartbeatAgeS?: number | null
  // 'user' = the caller's own paired machine (amber when offline — soft
  // fallback to local); 'admin' = the agent's platform default (red when
  // offline — blocks everyone).
  machineScope?: PairingScope
  machineLastSeenIso?: string | null
}

// Every control of the bar on one height and one glass recipe.
const ICON_BUTTON = 'flex size-control-md shrink-0 items-center justify-center rounded-xl text-secondary transition-colors duration-fast hover:text-primary'
const AT_REST = `${GLASS} ${GLASS_HOVER}`

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
  let badgeState: BadgeState = null
  if (machineStatus) {
    badgeState = machineStatus
  } else if (!isLocalTarget(executionTarget)) {
    badgeState = MACHINE_STATE.ONLINE
  } else if (fallbackReason) {
    badgeState = FELLBACK_LOCAL
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
    <div className="absolute top-0 left-0 right-0 z-20 flex items-center gap-2 h-12 px-3">
      {/* Leading button: back-arrow (task run) or hamburger (chat history). */}
      {onBack ? (
        <button
          onClick={onBack}
          className={`${ICON_BUTTON} ${AT_REST}`}
          title="Back"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 19l-7-7 7-7" />
          </svg>
        </button>
      ) : (
        <button
          onClick={onToggleHistory}
          className={`${ICON_BUTTON} ${AT_REST}`}
          title="Toggle chat history"
        >
          <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
          </svg>
        </button>
      )}

      {/* The agent pill: the name links to the agents page across the whole
          pill (the link's overlay), the status dot sits above the overlay
          so its own tap shows its detail. The glass is painted on a span
          behind the content, never on the pill: a blur makes an element
          the frame of its fixed children, and the dot's tooltip is fixed
          to the viewport. */}
      <div
        className="group relative isolate flex h-control-md min-w-0 max-w-[45vw] items-center gap-1.5 rounded-xl px-3
                   has-[a:focus-visible]:ring-2 has-[a:focus-visible]:ring-focus-ring sm:max-w-none"
        data-testid="agent-pill"
      >
        <span
          aria-hidden="true"
          className={`pointer-events-none absolute inset-0 -z-10 rounded-[inherit] ${GLASS} transition-colors duration-fast group-has-[a:hover]:bg-glass-hover`}
        />
        <Link
          to="/agents"
          title={displayName || agentName}
          className="min-w-0 truncate text-sm font-medium text-primary focus-visible:outline-none
                     after:absolute after:inset-0 after:content-['']"
        >
          {displayName || agentName}
        </Link>
        {/* Remote execution target indicator. Reflects the caller's
            effective target — their own paired machine ('user' scope, amber
            when offline) or the agent's admin default ('admin' scope, red
            when offline). Hover (desktop) or tap (touch) the dot for detail. */}
        {badgeState && (
          <RemoteBadge
            state={badgeState}
            scope={machineScope}
            machineName={machineName || undefined}
            heartbeatAgeS={machineLastHeartbeatAgeS ?? null}
            lastSeenIso={machineLastSeenIso ?? null}
            fallbackReason={fallbackReason ?? null}
            className="z-[1] shrink-0"
          />
        )}
        <svg className="w-3 h-3 text-tertiary shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </div>

      <div className="flex-1" />

      {/* Notification bell */}
      {notificationBell}

      {/* User menu */}
      {user && (
        <div className="relative" ref={menuRef}>
          <button
            onClick={() => setMenuOpen(!menuOpen)}
            aria-expanded={menuOpen}
            aria-haspopup="menu"
            className={`${ICON_BUTTON} ${menuOpen ? GLASS_PRESSED : AT_REST} text-xs font-bold`}
            title={user.display_name || user.name}
          >
            {getInitials(user.display_name || user.name)}
          </button>

          {menuOpen && (
            <div className={`absolute right-0 top-11 z-50 w-52 py-1 ${POPOVER}`}>
              <div className="px-3 py-2.5 border-b border-subtle">
                {user.display_name && (
                  <p className="text-sm font-medium text-primary truncate">{user.display_name}</p>
                )}
                <p className={`text-xs truncate ${user.display_name ? 'text-tertiary' : 'text-sm font-medium text-primary'}`}>{user.name}</p>
                <p className="text-xs text-tertiary truncate">{user.email}</p>
              </div>

              <button
                onClick={() => { setMenuOpen(false); navigate('/user-settings') }}
                className="w-full text-left px-3 py-2 text-sm text-secondary hover:bg-surface-hover flex items-center gap-2 transition-colors"
              >
                <IconUser className="w-4 h-4 text-tertiary" />
                User Settings
              </button>

              <button
                onClick={() => { setMenuOpen(false); navigate(`/agents/${agentName}`) }}
                className="w-full text-left px-3 py-2 text-sm text-secondary hover:bg-surface-hover flex items-center gap-2 transition-colors"
              >
                <IconGear className="w-4 h-4 text-tertiary" />
                Agent Settings
              </button>

              {onAppSettings && (
                <button
                  onClick={() => { setMenuOpen(false); onAppSettings() }}
                  className="w-full text-left px-3 py-2 text-sm text-secondary hover:bg-surface-hover flex items-center gap-2 transition-colors"
                >
                  <IconDeviceMobile className="w-4 h-4 text-tertiary" />
                  App Settings
                </button>
              )}

              {isAdmin(user) && (
                <button
                  onClick={() => { setMenuOpen(false); navigate('/admin') }}
                  className="w-full text-left px-3 py-2 text-sm text-secondary hover:bg-surface-hover flex items-center gap-2 transition-colors"
                >
                  <IconShieldCheck className="w-4 h-4 text-tertiary" />
                  Admin
                </button>
              )}

              <div className="border-t border-subtle my-1" />

              <button
                onClick={() => { setMenuOpen(false); onLogout() }}
                className="w-full text-left px-3 py-2 text-sm text-secondary hover:bg-surface-hover flex items-center gap-2 transition-colors"
              >
                <IconLogout className="w-4 h-4 text-tertiary" />
                Logout
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
