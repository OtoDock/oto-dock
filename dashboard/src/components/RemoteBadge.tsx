/**
 * Small dot-style badge indicating the live state of a remote satellite
 * connection or an agent's resolved execution target. Hover (desktop), a
 * tap or a long press (touch) and keyboard focus reveal the detail.
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
 * The detail is the shared Tooltip: portaled and clamped to the viewport,
 * so neither the screen edge nor a transformed or blurred ancestor (the
 * chat top bar's glass) misplaces it.
 */

import Tooltip from './ui/Tooltip'
import { PAIRING_SCOPE } from '../lib/placement'
import { MACHINE_STATE, isReachableMachine } from '../lib/status/machine'
import { FELLBACK_LOCAL, dotColorClass, type BadgeScope, type BadgeState } from './remoteBadgeDot'

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
  fellback_local: 'Running locally while the remote machine is offline',
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
  if (!state) return null

  const dotSize = size === 'xs' ? 'w-2 h-2' : 'w-2.5 h-2.5'
  const colorClass = dotColorClass(state, scope)

  if (noTooltip) {
    return (
      <span className={`inline-flex items-center ${className}`}>
        <span
          className={`inline-block ${dotSize} rounded-full ${colorClass}`}
          aria-label={LABELS[state]}
        />
      </span>
    )
  }

  // --- Tooltip content -----------------------------------------------------
  const reachable = isReachableMachine(state)
  const label = LABELS[state]
  const parts: string[] = [machineName ? `${machineName}: ${label}` : label]
  if (state === MACHINE_STATE.ONLINE && heartbeatAgeS != null) {
    parts.push(`Last ping: ${formatAge(heartbeatAgeS)}`)
  } else if (state === MACHINE_STATE.STALE && heartbeatAgeS != null) {
    parts.push(`Last ping: ${formatAge(heartbeatAgeS)}, likely disconnecting`)
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

  return (
    <Tooltip text={tipText} className={`relative inline-flex items-center ${className}`}>
      <span
        tabIndex={0}
        role="button"
        aria-label={tipText}
        className="inline-flex items-center rounded-full"
        onClick={(e) => {
          // The dot is its own control inside a link or a clickable row: the
          // click neither navigates nor opens what it sits in. A tap focuses
          // the dot and the focus shows the detail; a dot that kept the focus
          // after the detail closed (a scroll, Escape, an outside touch)
          // shows it again.
          e.preventDefault()
          e.stopPropagation()
          const dot = e.currentTarget
          dot.blur()
          dot.focus()
        }}
      >
        <span className={`inline-block ${dotSize} rounded-full ${colorClass}`} aria-hidden="true" />
      </span>
    </Tooltip>
  )
}
