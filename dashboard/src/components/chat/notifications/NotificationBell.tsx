import { useState, useRef, useEffect } from 'react'

import { GLASS, GLASS_HOVER, GLASS_PRESSED } from '../../ui/glass'

interface Props {
  unreadCount: number
  /** Shares waiting for a decision (SHARING.md): its own mark on the action fill, since
   * each share also fires a delivery the red number already counts. */
  pendingCount?: number
  onClick: () => void
  panelOpen: boolean
}

export default function NotificationBell({ unreadCount, pendingCount = 0, onClick, panelOpen }: Props) {
  const shown = unreadCount + pendingCount
  const prevCount = useRef(shown)
  const [pulse, setPulse] = useState(false)

  // Detect a new notification (a shown number increased) → pulse the badges
  // for 1s. Must be state, not a ref: a ref mutation doesn't re-render, so
  // the animation class would never actually toggle on/off.
  useEffect(() => {
    const increased = shown > prevCount.current
    prevCount.current = shown
    if (!increased) return
    setPulse(true)
    const t = setTimeout(() => setPulse(false), 1000)
    return () => clearTimeout(t)
  }, [shown])

  return (
    <button
      onClick={onClick}
      className={`relative flex size-control-md shrink-0 items-center justify-center rounded-xl text-secondary
                  transition-colors duration-fast hover:text-primary
                  ${panelOpen ? GLASS_PRESSED : `${GLASS} ${GLASS_HOVER}`}`}
      title="Notifications"
      data-notification-bell
      aria-expanded={panelOpen}
      aria-label={`Notifications${unreadCount > 0 ? `, ${unreadCount} unread` : ''}${pendingCount > 0 ? `, ${pendingCount} shared with you, waiting for you` : ''}`}
    >
      <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
        <path
          strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
          d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
        />
      </svg>

      {unreadCount > 0 && (
        <span
          className={`absolute -top-1 -right-1 min-w-[18px] h-[18px] px-1 rounded-full
                      bg-status-danger-fill text-on-solid text-3xs font-bold flex items-center justify-center
                      ${pulse ? 'animate-pulse' : ''}`}
        >
          {unreadCount > 99 ? '99+' : unreadCount}
        </span>
      )}
      {pendingCount > 0 && (
        <span
          data-testid="bell-pending"
          title={`${pendingCount} shared with you, waiting for you`}
          className={`absolute -bottom-1 -right-1 min-w-[17px] h-[17px] px-1 rounded-full
                      bg-action text-action-fg text-3xs font-bold flex items-center justify-center
                      ${pulse ? 'animate-pulse' : ''}`}
        >
          {pendingCount > 99 ? '99+' : pendingCount}
        </span>
      )}
    </button>
  )
}
