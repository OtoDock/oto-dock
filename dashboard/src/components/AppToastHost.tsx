import { useEffect, useState } from 'react'

import type { NotificationDelivery } from '../api/notifications'
import type { ToastItem } from '../hooks/useNotifications'
import { onAppToast } from '../lib/appToasts'
import NotificationToast from './chat/notifications/NotificationToast'

const AUTO_DISMISS_MS: Record<string, number> = { info: 5000, success: 5000, warning: 10000, danger: 0 }
const MAX_TOASTS = 3

let seq = 0

/**
 * The toasts raised outside the component tree (appToasts: the deep-link
 * handler completing a connect on a recreated page), shown on every page.
 * The chat page's own stack carries the notification deliveries; this one
 * carries only these, with no inbox row behind them.
 */
export default function AppToastHost() {
  const [toasts, setToasts] = useState<ToastItem[]>([])

  useEffect(() => onAppToast((t) => {
    const id = `local-${++seq}`
    const delivery: NotificationDelivery = {
      id, notification_id: null, title: t.title, body: t.body, severity: t.severity,
      scope: 'user', source: 'local', delivered_at: new Date().toISOString(),
      read: 1, dismissed: 0, read_at: null, dismissed_at: null, agent_slug: null, chat_id: null,
    }
    // A toast equal to one still shown (a save on every keystroke refused
    // each time, two calls refused alike) is that toast again: no copy stacks.
    setToasts((prev) => prev.some((x) => x.delivery.title === t.title && x.delivery.body === t.body)
      ? prev
      : [{ id, delivery, createdAt: Date.now() }, ...prev].slice(0, MAX_TOASTS))
    const ms = AUTO_DISMISS_MS[t.severity] ?? 5000
    if (ms > 0) setTimeout(() => setToasts((prev) => prev.filter((x) => x.id !== id)), ms)
  }), [])

  if (toasts.length === 0) return null
  return (
    <NotificationToast
      toasts={toasts}
      onDismiss={(id) => setToasts((prev) => prev.filter((x) => x.id !== id))}
    />
  )
}
