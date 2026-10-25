import { useRef, useEffect, useId, useLayoutEffect } from 'react'
import { createPortal } from 'react-dom'
import type { NotificationDelivery } from '../../../api/notifications'
import type { AgentSummary } from '../../../api/agents'
import type { ShareInboxItem } from '../../../api/shares'
import NotificationBody from './NotificationBody'
import NotificationAgentHeader from './NotificationAgentHeader'
import ShareInboxSection from '../../sharing/ShareInboxSection'
import SwipeableRow from './SwipeableRow'
import { POPOVER } from '../../ui/classes'
import { usePortalRoot } from '../../ui/portalRoot'
import { captureFocusOrigin, restoreFocus, type FocusOrigin } from '../../ui/focusReturn'
import { primaryPointerCoarse } from '../../common/useInvokedForm'
import { pushEscHandler } from '../../../lib/escStack'

interface Props {
  deliveries: NotificationDelivery[]
  loading: boolean
  /** Cached agents list — resolves each row's `agent_slug` to its avatar + name. */
  agents?: AgentSummary[]
  onMarkRead: (id: string) => void
  onMarkAllRead: () => void
  onDismiss: (id: string) => void
  onAcknowledge: (id: string) => void
  onNavigate?: (agentSlug: string, chatId: string, href?: string) => void
  onClose: () => void
  /** "Shared with you" (SHARING.md): the block above the groups, fed by
   * the hook that owns the panel so this component calls no query itself. */
  inbox?: ShareInboxItem[]
  inboxBusyId?: string
  inboxError?: string
  onAcceptShare?: (item: ShareInboxItem, agent: string) => void
  onDeclineShare?: (item: ShareInboxItem) => void
  onHideShare?: (item: ShareInboxItem) => void
  onOpenShare?: (item: ShareInboxItem) => void
}

// A severity's edge is its solid, its word the foreground.
const SEVERITY_BORDER: Record<string, string> = {
  info: 'border-l-status-info-solid',
  success: 'border-l-status-success-solid',
  warning: 'border-l-status-warning-solid',
  danger: 'border-l-status-danger-solid',
}

const SEVERITY_ICON: Record<string, string> = {
  info: 'text-status-info-fg',
  success: 'text-status-success-fg',
  warning: 'text-status-warning-fg',
  danger: 'text-status-danger-fg',
}

// Between the bell's bottom edge and the panel: `top-12` under the bar's
// 36 px controls, 6 px from the top.
const PANEL_GAP = 6

// Unread rows keep a tint on hover: the read rows' hover would make them
// look read under the pointer.
const UNREAD_ROW = 'bg-action-subtle hover:bg-action-subtle-hover'

function relativeTime(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime()
  const mins = Math.floor(ms / 60000)
  if (mins < 1) return 'now'
  if (mins < 60) return `${mins}m`
  const hrs = Math.floor(mins / 60)
  if (hrs < 24) return `${hrs}h`
  const days = Math.floor(hrs / 24)
  return `${days}d`
}

function groupByDate(items: NotificationDelivery[]): { label: string; items: NotificationDelivery[] }[] {
  const today = new Date().toDateString()
  const todayItems: NotificationDelivery[] = []
  const earlierItems: NotificationDelivery[] = []

  for (const d of items) {
    if (new Date(d.delivered_at).toDateString() === today) {
      todayItems.push(d)
    } else {
      earlierItems.push(d)
    }
  }

  const groups: { label: string; items: NotificationDelivery[] }[] = []
  if (todayItems.length) groups.push({ label: 'Today', items: todayItems })
  if (earlierItems.length) groups.push({ label: 'Earlier', items: earlierItems })
  return groups
}


export default function NotificationPanel({
  deliveries, loading, agents, onMarkRead, onMarkAllRead, onDismiss, onAcknowledge, onNavigate, onClose,
  inbox, inboxBusyId, inboxError, onAcceptShare, onDeclineShare, onHideShare, onOpenShare,
}: Props) {
  const ref = useRef<HTMLDivElement>(null)
  const originRef = useRef<FocusOrigin | null>(null)
  const headingId = useId()
  const portalRoot = usePortalRoot()

  useEffect(() => {
    // A press on the bell is the bell's: closing here would let its click
    // open the panel again. A press on the scrim closes on its click, so a
    // tap does not fall through to the page once the scrim is gone.
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (target instanceof Element && target.closest('[data-notification-bell], [data-notification-scrim]')) return
      if (ref.current && !ref.current.contains(target)) {
        onClose()
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  // The panel sits in the body, out of the bell's order: on a desktop the
  // focus moves into it, and Escape closes it and gives the focus back to
  // the bell. A press elsewhere closes it and leaves the focus there.
  useLayoutEffect(() => {
    if (!originRef.current) originRef.current = captureFocusOrigin()
    // The panel opens under the bell: a banner above the chat pushes the bar
    // down, and `top-12` alone would put the panel over it.
    const bell = document.querySelector('[data-notification-bell]')
    if (bell && ref.current) ref.current.style.top = `${bell.getBoundingClientRect().bottom + PANEL_GAP}px`
    if (!primaryPointerCoarse()) ref.current?.focus({ preventScroll: true })
  }, [])
  useEffect(() => pushEscHandler(() => {
    onClose()
    if (originRef.current) restoreFocus(originRef.current)
  }, ref.current), [onClose])

  const groups = groupByDate(deliveries)
  const inboxItems = inbox ?? []

  // Above the composer, below the toast region (ui/Toast.tsx, z-50), so an
  // arriving toast is never covered.
  return createPortal(
    <>
    <div
      data-notification-scrim
      className="fixed inset-0 z-[45] bg-surface-scrim sm:hidden"
      onClick={onClose}
      aria-hidden="true"
    />
    <div
      ref={ref}
      role="dialog"
      aria-labelledby={headingId}
      tabIndex={-1}
      className={`fixed right-3 left-3 sm:left-auto sm:right-4 sm:w-80 top-12 max-h-[28rem] z-[45] flex flex-col overflow-hidden outline-none ${POPOVER}`}
    >
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-subtle">
        <span id={headingId} className="text-sm font-medium text-primary">Notifications</span>
        {deliveries.some(d => !d.read) && (
          <button
            onClick={onMarkAllRead}
            className="text-xs text-link hover:text-link-hover transition-colors"
          >
            Mark all read
          </button>
        )}
      </div>

      {/* Content: the shares block first (it has its own data and shows
          while the deliveries load), then the dated groups. */}
      <div className="flex-1 overflow-y-auto">
        {inboxItems.length > 0 && (
          <ShareInboxSection
            items={inboxItems}
            agents={agents}
            busyId={inboxBusyId}
            error={inboxError}
            onAccept={(item, agent) => onAcceptShare?.(item, agent)}
            onDecline={(item) => onDeclineShare?.(item)}
            onHide={(item) => onHideShare?.(item)}
            onOpen={(item) => onOpenShare?.(item)}
          />
        )}
        {loading ? (
          <div className="flex items-center justify-center py-8 text-tertiary text-sm">
            Loading...
          </div>
        ) : groups.length === 0 ? (
          inboxItems.length === 0 && (
            <div className="flex items-center justify-center py-8 text-tertiary text-sm">
              No notifications
            </div>
          )
        ) : (
          groups.map(group => (
            <div key={group.label}>
              <div className="px-3 py-1.5 text-2xs font-medium text-tertiary uppercase tracking-wider bg-surface-muted">
                {group.label}
              </div>
              {group.items.map(d => (
                <SwipeableRow key={d.id} onSwipeDismiss={() => onDismiss(d.id)}>
                  <div
                    className={`flex items-start gap-2 px-3 py-2.5 border-l-[3px] transition-colors cursor-pointer
                                ${SEVERITY_BORDER[d.severity] || 'border-l-default'}
                                ${!d.read ? UNREAD_ROW : 'hover:bg-surface-hover'}`}
                    onClick={() => { onAcknowledge(d.id); if (!d.read) onMarkRead(d.id) }}
                  >
                    <div className="flex-1 min-w-0">
                      {/* Sender header (agent avatar + name), aligned with the
                          title text below the unread dot. */}
                      <div className="ml-3 mb-0.5">
                        <NotificationAgentHeader agentSlug={d.agent_slug} agents={agents} />
                      </div>
                      <div className="flex items-center gap-1.5">
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${!d.read ? 'bg-action' : 'bg-transparent'}`} />
                        <span className="text-sm font-medium text-primary truncate">{d.title}</span>
                      </div>
                      <div className="ml-3">
                        <NotificationBody body={d.body} clampClass="line-clamp-2" />
                      </div>
                      <div className="flex items-center gap-2 mt-1 ml-3">
                        <span className={`text-3xs font-medium uppercase ${SEVERITY_ICON[d.severity] || 'text-tertiary'}`}>
                          {d.severity}
                        </span>
                        <span className="text-3xs text-tertiary">{relativeTime(d.delivered_at)}</span>
                      </div>
                    </div>
                    <div className="flex flex-col items-center gap-1 shrink-0 mt-0.5">
                      <button
                        onClick={(e) => { e.stopPropagation(); onDismiss(d.id) }}
                        className="w-5 h-5 flex items-center justify-center text-tertiary hover:text-secondary transition-colors"
                        title="Dismiss"
                      >
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                      {onNavigate && (d.href || (d.agent_slug && (d.chat_id || d.source === 'file_conflict'))) && (
                        <button
                          onClick={(e) => { e.stopPropagation(); if (!d.read) onMarkRead(d.id); onNavigate(d.agent_slug || '', d.chat_id || '', d.href || undefined) }}
                          className="w-5 h-5 flex items-center justify-center text-tertiary hover:text-link transition-colors"
                          title={d.href ? 'Open' : d.source === 'file_conflict' ? 'Open Recover bin' : 'Open chat'}
                        >
                          <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M13 7l5 5m0 0l-5 5m5-5H6" />
                          </svg>
                        </button>
                      )}
                    </div>
                  </div>
                </SwipeableRow>
              ))}
            </div>
          ))
        )}
      </div>
    </div>
    </>,
    portalRoot,
  )
}
