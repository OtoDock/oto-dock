import { useState, useRef, useEffect } from 'react'

interface Props {
  unreadCount: number
  onClick: () => void
  panelOpen: boolean
}

export default function NotificationBell({ unreadCount, onClick, panelOpen }: Props) {
  const prevCount = useRef(unreadCount)
  const [pulse, setPulse] = useState(false)

  // Detect a new notification (count increased) → pulse the badge for 1s.
  // Must be state, not a ref: a ref mutation doesn't re-render, so the
  // animation class would never actually toggle on/off.
  useEffect(() => {
    const increased = unreadCount > prevCount.current
    prevCount.current = unreadCount
    if (!increased) return
    setPulse(true)
    const t = setTimeout(() => setPulse(false), 1000)
    return () => clearTimeout(t)
  }, [unreadCount])

  return (
    <button
      onClick={onClick}
      className={`relative w-9 h-9 mr-2 rounded-xl backdrop-blur-xs border border-p-border-light/50 dark:border-gray-600
                  flex items-center justify-center text-p-text-secondary transition-colors shadow-xs
                  ${panelOpen ? 'bg-white/90 dark:bg-gray-900/90' : 'bg-white/70 dark:bg-gray-900/70 hover:bg-white/90 dark:hover:bg-gray-900/90'}`}
      title="Notifications"
    >
      <svg className="w-[18px] h-[18px]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
        <path
          strokeLinecap="round" strokeLinejoin="round" strokeWidth={2}
          d="M15 17h5l-1.405-1.405A2.032 2.032 0 0118 14.158V11a6.002 6.002 0 00-4-5.659V5a2 2 0 10-4 0v.341C7.67 6.165 6 8.388 6 11v3.159c0 .538-.214 1.055-.595 1.436L4 17h5m6 0v1a3 3 0 11-6 0v-1m6 0H9"
        />
      </svg>

      {unreadCount > 0 && (
        <span
          className={`absolute -top-1 -right-1 min-w-[18px] h-[18px] px-1 rounded-full
                      bg-p-accent-red text-white text-[10px] font-bold flex items-center justify-center
                      ${pulse ? 'animate-pulse' : ''}`}
        >
          {unreadCount > 99 ? '99+' : unreadCount}
        </span>
      )}
    </button>
  )
}
