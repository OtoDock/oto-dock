import { useRef, useEffect, useState, useCallback } from 'react'
import type { NotificationDelivery } from '../../../api/notifications'
import NotificationBody from './NotificationBody'

interface Props {
  deliveries: NotificationDelivery[]
  loading: boolean
  onMarkRead: (id: string) => void
  onMarkAllRead: () => void
  onDismiss: (id: string) => void
  onAcknowledge: (id: string) => void
  onNavigate?: (agentSlug: string, chatId: string) => void
  onClose: () => void
}

const SEVERITY_BORDER: Record<string, string> = {
  info: 'border-l-brand',
  success: 'border-l-p-accent-teal',
  warning: 'border-l-p-accent-yellow',
  danger: 'border-l-p-accent-red',
}

const SEVERITY_ICON: Record<string, string> = {
  info: 'text-brand',
  success: 'text-p-accent-teal',
  warning: 'text-p-accent-yellow',
  danger: 'text-p-accent-red',
}

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

// --- Swipeable row: swipe left to dismiss ---
function SwipeableRow({ onSwipeDismiss, children }: { onSwipeDismiss: () => void; children: React.ReactNode }) {
  const rowRef = useRef<HTMLDivElement>(null)
  const [offsetX, setOffsetX] = useState(0)
  const [swiping, setSwiping] = useState(false)
  const [dismissed, setDismissed] = useState(false)
  const startX = useRef(0)
  const startY = useRef(0)
  const locked = useRef(false)  // true = horizontal swipe confirmed

  const handleTouchStart = useCallback((e: React.TouchEvent) => {
    startX.current = e.touches[0].clientX
    startY.current = e.touches[0].clientY
    locked.current = false
    setSwiping(false)
  }, [])

  const handleTouchMove = useCallback((e: React.TouchEvent) => {
    const dx = e.touches[0].clientX - startX.current
    const dy = e.touches[0].clientY - startY.current

    // Lock direction after 10px of movement
    if (!locked.current && !swiping) {
      if (Math.abs(dx) > 10 && Math.abs(dx) > Math.abs(dy) * 1.5) {
        locked.current = true
        setSwiping(true)
      } else if (Math.abs(dy) > 10) {
        return  // Vertical scroll, don't interfere
      } else {
        return  // Not enough movement yet
      }
    }
    if (!locked.current) return

    // Only allow left swipe (negative dx)
    const clampedX = Math.min(0, dx)
    setOffsetX(clampedX)
  }, [swiping])

  const handleTouchEnd = useCallback(() => {
    if (!locked.current) {
      setOffsetX(0)
      setSwiping(false)
      return
    }
    const width = rowRef.current?.offsetWidth || 300
    if (Math.abs(offsetX) > width * 0.3) {
      // Swiped past threshold — animate out and dismiss
      setDismissed(true)
      setTimeout(onSwipeDismiss, 200)
    } else {
      // Spring back
      setOffsetX(0)
    }
    setSwiping(false)
    locked.current = false
  }, [offsetX, onSwipeDismiss])

  return (
    <div ref={rowRef} className="relative overflow-hidden" style={{ maxHeight: dismissed ? 0 : undefined, transition: dismissed ? 'max-height 200ms ease-out' : undefined }}>
      {/* Red background revealed on swipe */}
      <div className="absolute inset-0 bg-p-accent-red flex items-center justify-end pr-4">
        <svg className="w-4 h-4 text-white" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
        </svg>
      </div>
      {/* Foreground content */}
      <div
        className="relative bg-white dark:bg-p-surface"
        style={{
          transform: dismissed ? 'translateX(-100%)' : `translateX(${offsetX}px)`,
          transition: swiping ? 'none' : 'transform 200ms ease-out',
        }}
        onTouchStart={handleTouchStart}
        onTouchMove={handleTouchMove}
        onTouchEnd={handleTouchEnd}
      >
        {children}
      </div>
    </div>
  )
}

export default function NotificationPanel({ deliveries, loading, onMarkRead, onMarkAllRead, onDismiss, onAcknowledge, onNavigate, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        onClose()
      }
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [onClose])

  const groups = groupByDate(deliveries)

  return (
    <div
      ref={ref}
      className="fixed right-3 left-3 sm:left-auto sm:right-4 sm:w-80 top-12 max-h-[28rem] bg-white dark:bg-p-surface rounded-xl shadow-lg border border-p-border-light z-50 flex flex-col overflow-hidden"
    >
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-p-border-light">
        <span className="text-sm font-medium text-p-text">Notifications</span>
        {deliveries.some(d => !d.read) && (
          <button
            onClick={onMarkAllRead}
            className="text-xs text-brand hover:text-brand-hover transition-colors"
          >
            Mark all read
          </button>
        )}
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto">
        {loading ? (
          <div className="flex items-center justify-center py-8 text-p-text-light text-sm">
            Loading...
          </div>
        ) : groups.length === 0 ? (
          <div className="flex items-center justify-center py-8 text-p-text-light text-sm">
            No notifications
          </div>
        ) : (
          groups.map(group => (
            <div key={group.label}>
              <div className="px-3 py-1.5 text-[11px] font-medium text-p-text-light uppercase tracking-wider bg-p-surface/50">
                {group.label}
              </div>
              {group.items.map(d => (
                <SwipeableRow key={d.id} onSwipeDismiss={() => onDismiss(d.id)}>
                  <div
                    className={`flex items-start gap-2 px-3 py-2.5 border-l-[3px] hover:bg-p-surface/50 transition-colors cursor-pointer
                                ${SEVERITY_BORDER[d.severity] || 'border-l-p-border'}
                                ${!d.read ? 'bg-brand-surface/30' : ''}`}
                    onClick={() => { onAcknowledge(d.id); if (!d.read) onMarkRead(d.id) }}
                  >
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className={`w-1.5 h-1.5 rounded-full shrink-0 ${!d.read ? 'bg-brand' : 'bg-transparent'}`} />
                        <span className="text-sm font-medium text-p-text truncate">{d.title}</span>
                      </div>
                      <div className="ml-3">
                        <NotificationBody body={d.body} clampClass="line-clamp-2" />
                      </div>
                      <div className="flex items-center gap-2 mt-1 ml-3">
                        <span className={`text-[10px] font-medium uppercase ${SEVERITY_ICON[d.severity] || 'text-p-text-light'}`}>
                          {d.severity}
                        </span>
                        <span className="text-[10px] text-p-text-light">{relativeTime(d.delivered_at)}</span>
                      </div>
                    </div>
                    <div className="flex flex-col items-center gap-1 shrink-0 mt-0.5">
                      <button
                        onClick={(e) => { e.stopPropagation(); onDismiss(d.id) }}
                        className="w-5 h-5 flex items-center justify-center text-p-text-light hover:text-p-text-secondary transition-colors"
                        title="Dismiss"
                      >
                        <svg className="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                        </svg>
                      </button>
                      {d.agent_slug && onNavigate && (d.chat_id || d.source === 'file_conflict') && (
                        <button
                          onClick={(e) => { e.stopPropagation(); if (!d.read) onMarkRead(d.id); onNavigate(d.agent_slug!, d.chat_id || '') }}
                          className="w-5 h-5 flex items-center justify-center text-p-text-light hover:text-brand transition-colors"
                          title={d.source === 'file_conflict' ? 'Open Recover bin' : 'Open chat'}
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
  )
}
