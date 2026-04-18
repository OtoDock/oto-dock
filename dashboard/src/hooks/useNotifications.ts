import { useState, useCallback, useRef, useEffect } from 'react'
import type { NotificationDelivery } from '../api/notifications'
import { fetchDeliveries, markRead as apiMarkRead, markAllRead as apiMarkAllRead, dismissDelivery } from '../api/notifications'

export interface ToastItem {
  id: string
  delivery: NotificationDelivery
  createdAt: number
}

const AUTO_DISMISS_MS: Record<string, number> = {
  info: 5000,
  success: 5000,
  warning: 10000,
  danger: 0, // never auto-dismiss
}

const MAX_TOASTS = 3

export function useNotifications() {
  const [unreadCount, setUnreadCount] = useState(0)
  const [deliveries, setDeliveries] = useState<NotificationDelivery[]>([])
  const [toasts, setToasts] = useState<ToastItem[]>([])
  const [panelOpen, setPanelOpen] = useState(false)
  const [loading, setLoading] = useState(false)
  const timers = useRef<Map<string, ReturnType<typeof setTimeout>>>(new Map())

  // Update unread count from WS event
  const setCount = useCallback((count: number) => {
    setUnreadCount(count)
  }, [])

  // Add a toast from a WS notification event
  const addToast = useCallback((delivery: NotificationDelivery) => {
    setUnreadCount(c => c + 1)
    setDeliveries(prev => {
      // Avoid duplicates if the same delivery arrives via both `notification` and
      // a subsequent fetchDeliveries refresh (e.g., on panel open).
      if (prev.some(d => d.id === delivery.id)) return prev
      return [delivery, ...prev]
    })

    const toast: ToastItem = {
      id: delivery.id,
      delivery,
      createdAt: Date.now(),
    }

    setToasts(prev => {
      const next = [toast, ...prev].slice(0, MAX_TOASTS)
      return next
    })

    // Auto-dismiss timer (danger = 0 means never auto-dismiss)
    const ms = AUTO_DISMISS_MS[delivery.severity] ?? 5000
    if (ms > 0) {
      const timer = setTimeout(() => {
        setToasts(prev => prev.filter(t => t.id !== toast.id))
        timers.current.delete(toast.id)
      }, ms)
      timers.current.set(toast.id, timer)
    }
  }, [])

  // Add to inbox + bump badge WITHOUT showing a toast or playing sound.
  // Fires on connected-but-inactive WSes so the inbox stays current while the
  // alert is delivered via native push (FCM / Web Push) on the actually-active
  // device. The backend writes the row to notification_deliveries before
  // firing this event, so the inbox is always consistent on next panel open.
  const addSilent = useCallback((delivery: NotificationDelivery) => {
    setUnreadCount(c => c + 1)
    setDeliveries(prev => {
      if (prev.some(d => d.id === delivery.id)) return prev
      return [delivery, ...prev]
    })
  }, [])

  const dismissToast = useCallback((toastId: string) => {
    setToasts(prev => prev.filter(t => t.id !== toastId))
    const timer = timers.current.get(toastId)
    if (timer) {
      clearTimeout(timer)
      timers.current.delete(toastId)
    }
  }, [])

  // Fetch deliveries when panel opens
  const openPanel = useCallback(async () => {
    setPanelOpen(true)
    setLoading(true)
    try {
      const data = await fetchDeliveries()
      setDeliveries(data)
    } catch (e) {
      console.error('Failed to fetch notifications:', e)
    } finally {
      setLoading(false)
    }
  }, [])

  const closePanel = useCallback(() => setPanelOpen(false), [])
  const togglePanel = useCallback(() => {
    if (panelOpen) closePanel()
    else openPanel()
  }, [panelOpen, openPanel, closePanel])

  // Mark single delivery as read
  const markRead = useCallback(async (deliveryId: string) => {
    try {
      await apiMarkRead(deliveryId)
      setDeliveries(prev =>
        prev.map(d => d.id === deliveryId ? { ...d, read: 1, read_at: new Date().toISOString() } : d)
      )
      setUnreadCount(c => Math.max(0, c - 1))
    } catch (e) {
      console.error('Failed to mark read:', e)
    }
  }, [])

  // Mark all as read
  const markAllRead = useCallback(async () => {
    try {
      await apiMarkAllRead()
      setDeliveries(prev => prev.map(d => ({ ...d, read: 1, read_at: new Date().toISOString() })))
      setUnreadCount(0)
    } catch (e) {
      console.error('Failed to mark all read:', e)
    }
  }, [])

  // Dismiss single delivery — also removes matching toast
  const dismiss = useCallback(async (deliveryId: string) => {
    try {
      await dismissDelivery(deliveryId)
      setDeliveries(prev => prev.filter(d => d.id !== deliveryId))
      // Also remove the matching toast if it's still showing
      setToasts(prev => {
        const removed = prev.find(t => t.id === deliveryId)
        if (removed) {
          const timer = timers.current.get(deliveryId)
          if (timer) { clearTimeout(timer); timers.current.delete(deliveryId) }
        }
        return prev.filter(t => t.id !== deliveryId)
      })
      // If it was unread, decrement
      const was = deliveries.find(d => d.id === deliveryId)
      if (was && !was.read) setUnreadCount(c => Math.max(0, c - 1))
    } catch (e) {
      console.error('Failed to dismiss:', e)
    }
  }, [deliveries])

  // Cleanup timers on unmount
  useEffect(() => {
    return () => {
      timers.current.forEach(t => clearTimeout(t))
    }
  }, [])

  return {
    unreadCount,
    setCount,
    deliveries,
    toasts,
    panelOpen,
    loading,
    addToast,
    addSilent,
    dismissToast,
    togglePanel,
    closePanel,
    markRead,
    markAllRead,
    dismiss,
  }
}
