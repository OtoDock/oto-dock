/**
 * Small dot-style badge indicating the live state of a remote satellite
 * connection or an agent's resolved execution target. Hover (desktop) or
 * tap (touch) reveals a detail tooltip.
 *
 * Colour depends on state AND `scope`:
 *   online                      → green
 *   stale / paused / fell-back  → amber
 *   disconnected/never (user)   → amber  — the caller's OWN machine soft-falls
 *                                          back to local, so it's not critical
 *   disconnected     (admin)    → red    — blocks everyone on the agent
 *   never_connected  (admin)    → gray   — paired but never came online
 *
 * Renders nothing when `state` is null (local execution — no badge needed).
 *
 * The tooltip is rendered fixed-positioned and horizontally clamped to the
 * viewport, so it never bleeds past the screen edge on mobile.
 */

import { useEffect, useRef, useState } from 'react'
import { PAIRING_SCOPE, type PairingScope } from '../lib/placement'
import { MACHINE_STATE, isReachableMachine, type MachineState } from '../lib/status/machine'

// The machine's live state (lib/status/machine.ts) plus the badge's own
// word for a chat that fell back to the sandbox while its machine is away.
// Exported for the callers that derive a badge state (TopBar).
export const FELLBACK_LOCAL = 'fellback_local'
export type BadgeState = MachineState | typeof FELLBACK_LOCAL | null

// Which kind of target the dot represents. Drives severity colour: a user's
// own machine going offline is a soft fallback (amber), an admin/platform
// target going offline blocks everyone on the agent (red).
type BadgeScope = PairingScope

interface Props {
  state: BadgeState
  scope?: BadgeScope
  machineName?: string
  lastSeenIso?: string | null
  heartbeatAgeS?: number | null
  fallbackReason?: string | null
  size?: 'xs' | 'sm'
  className?: string
  // When true, suppresses the tooltip entirely. Used where a bare dot is
  // wanted (e.g. the task-run header).
  noTooltip?: boolean
}

const LABELS: Record<Exclude<BadgeState, null>, string> = {
  online: 'Connected',
  stale: 'Connection slow',
  paused: 'Paused',
  disconnected: 'Disconnected',
  never_connected: 'Never connected',
  fellback_local: 'Running locally (remote offline)',
}

// Fixed tooltip width (px). Fixed so we can clamp horizontally without a
// measure pass; text wraps within it. Capped to the viewport in `place()`.
const TIP_WIDTH = 224
const TIP_MARGIN = 8

function dotColorClass(state: Exclude<BadgeState, null>, scope?: BadgeScope): string {
  if (state === MACHINE_STATE.ONLINE) return 'bg-green-500'
  if (state === MACHINE_STATE.STALE || state === MACHINE_STATE.PAUSED || state === FELLBACK_LOCAL) return 'bg-amber-400'
  // disconnected | never_connected
  if (scope === PAIRING_SCOPE.USER) return 'bg-amber-400' // own machine → soft fallback
  if (state === MACHINE_STATE.NEVER_CONNECTED) return 'bg-gray-400'
  return 'bg-red-500' // admin/platform target offline → blocks everyone
}

function formatAge(seconds: number): string {
  if (seconds < 60) return `${seconds}s ago`
  const m = Math.floor(seconds / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.floor(m / 60)
  if (h < 24) return `${h}h ago`
  return `${Math.floor(h / 24)}d ago`
}

function formatIso(iso?: string | null): string {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    const now = new Date()
    const ageSec = Math.floor((now.getTime() - d.getTime()) / 1000)
    if (ageSec < 0) return iso
    return formatAge(ageSec)
  } catch {
    return iso
  }
}

export default function RemoteBadge({
  state,
  scope,
  machineName,
  lastSeenIso,
  heartbeatAgeS,
  fallbackReason,
  size = 'sm',
  className = '',
  noTooltip = false,
}: Props) {
  const [showTip, setShowTip] = useState(false)
  const [coords, setCoords] = useState<{ left: number; top: number; width: number } | null>(null)
  const ref = useRef<HTMLSpanElement>(null)

  // Close the tooltip on any outside interaction / scroll / resize so a
  // tap-opened tooltip on mobile doesn't get stranded.
  useEffect(() => {
    if (!showTip) return
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setShowTip(false)
    }
    const onMove = () => setShowTip(false)
    document.addEventListener('mousedown', onDown)
    window.addEventListener('scroll', onMove, true)
    window.addEventListener('resize', onMove)
    return () => {
      document.removeEventListener('mousedown', onDown)
      window.removeEventListener('scroll', onMove, true)
      window.removeEventListener('resize', onMove)
    }
  }, [showTip])

  if (!state) return null

  const dotSize = size === 'xs' ? 'w-2 h-2' : 'w-2.5 h-2.5'
  const colorClass = dotColorClass(state, scope)

  if (noTooltip) {
    return (
      <span className={`inline-flex items-center ${className}`}>
        <span
          className={`inline-block ${dotSize} rounded-full ${colorClass} ring-1 ring-black/10 dark:ring-white/20`}
          aria-label={LABELS[state]}
        />
      </span>
    )
  }

  // --- Tooltip content -----------------------------------------------------
  const reachable = isReachableMachine(state)
  const label = LABELS[state]
  const parts: string[] = [machineName ? `${machineName} — ${label}` : label]
  if (state === MACHINE_STATE.ONLINE && heartbeatAgeS != null) {
    parts.push(`Last ping: ${formatAge(heartbeatAgeS)}`)
  } else if (state === MACHINE_STATE.STALE && heartbeatAgeS != null) {
    parts.push(`Last ping: ${formatAge(heartbeatAgeS)} — likely disconnecting`)
  } else if ((state === MACHINE_STATE.DISCONNECTED || state === MACHINE_STATE.NEVER_CONNECTED
              || state === MACHINE_STATE.PAUSED) && lastSeenIso) {
    parts.push(`Last seen: ${formatIso(lastSeenIso)}`)
  }
  // Scope-aware consequence line so the dot explains what it MEANS.
  if (!reachable && state !== FELLBACK_LOCAL) {
    if (scope === PAIRING_SCOPE.USER) parts.push('Running locally until it reconnects')
    else if (scope === PAIRING_SCOPE.ADMIN) parts.push("Agents here can't run until it reconnects")
  }
  // Session fallback hints (when the dot is fed a resolved-session reason).
  if (fallbackReason === 'user-override-offline') {
    parts.push('Your remote machine is offline')
  } else if (fallbackReason === 'agent-default-offline') {
    parts.push("The agent's remote target is offline")
  } else if (fallbackReason === 'viewer-on-admin-remote') {
    parts.push('Sessions below manager run locally')
  }
  const tipText = parts.join(' · ')

  // --- Positioning: fixed + clamped to the viewport (mobile-safe) ----------
  const place = () => {
    const el = ref.current
    if (!el || typeof window === 'undefined') return
    const r = el.getBoundingClientRect()
    const width = Math.min(TIP_WIDTH, window.innerWidth - TIP_MARGIN * 2)
    const center = r.left + r.width / 2
    const left = Math.min(
      Math.max(TIP_MARGIN, center - width / 2),
      window.innerWidth - width - TIP_MARGIN,
    )
    setCoords({ left, top: r.bottom + 6, width })
  }
  const open = () => {
    place()
    setShowTip(true)
  }

  return (
    <span
      ref={ref}
      className={`relative inline-flex items-center ${className}`}
      onMouseEnter={open}
      onMouseLeave={() => setShowTip(false)}
      onFocus={open}
      onBlur={() => setShowTip(false)}
      onClick={(e) => {
        // Tap toggles on touch; stop the click from triggering an
        // enclosing clickable row/card — the dot is its own affordance.
        e.stopPropagation()
        if (showTip) setShowTip(false)
        else open()
      }}
      tabIndex={0}
      role="button"
      aria-label={tipText}
    >
      <span
        className={`inline-block ${dotSize} rounded-full ${colorClass} ring-1 ring-black/10 dark:ring-white/20`}
        aria-hidden="true"
      />
      {showTip && coords && (
        <span
          role="tooltip"
          style={{ position: 'fixed', left: coords.left, top: coords.top, width: coords.width }}
          className="z-[60] text-xs leading-snug px-2 py-1 rounded-sm break-words bg-p-text text-white dark:bg-p-surface dark:text-p-text border border-p-border-light shadow-lg"
        >
          {tipText}
        </span>
      )}
    </span>
  )
}
