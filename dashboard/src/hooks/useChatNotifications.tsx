import { useCallback, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import NotificationBell from '../components/chat/notifications/NotificationBell'
import NotificationPanel from '../components/chat/notifications/NotificationPanel'
import NotificationToast from '../components/chat/notifications/NotificationToast'
import { useNotifications } from './useNotifications'
import { useNotificationSound } from './useNotificationSound'
import { usePushSubscription } from './usePushSubscription'

/**
 * Shared notification surface for the chat page (AgentChat).
 * Owns the inbox/toast/badge state (`useNotifications`), the per-severity sounds
 * + danger alarm (`useNotificationSound`), Web Push registration, and the
 * deep-link navigation rule. Returns the bell + toast as ready-to-render nodes
 * plus the `/ws/dashboard` callbacks (wired into `useChatStream`).
 *
 * Native FCM registration is NOT here — it lives at the authenticated app root
 * (`RequireAuth`) so it is route-independent (see `useFcmPush`).
 *
 * Deep-link rule (single source of truth): every notification routes to the
 * chat page — task notifications (`task-…` chat_id) open it with task mode
 * toggled on (?tasks=1). Mirrors the backend `click_url` builder in
 * `proxy/services/notifications/notification_manager.py`.
 */
export function useChatNotifications() {
  const navigate = useNavigate()
  const notif = useNotifications()
  const notifSound = useNotificationSound()
  usePushSubscription()  // Web Push (browsers)

  // Auto-stop the danger alarm once no danger toasts remain (covers every
  // dismiss path).
  useEffect(() => {
    const hasDanger = notif.toasts.some(t => t.delivery.severity === 'danger')
    if (!hasDanger) notifSound.stopDangerAlarm()
  }, [notif.toasts])

  // Navigate to the chat/task that originated a notification.
  const handleNotifNavigate = useCallback((agentSlug: string, chatId: string) => {
    notif.closePanel()
    if (!chatId) {
      // No chat_id (e.g. a file-conflict notification) → open the agent and the
      // workspace Recover bin via the ?recover deep-link.
      navigate(`/chat/${agentSlug}?recover=1`)
    } else if (chatId.startsWith('task-')) {
      // Task chats open on the chat page with task mode toggled on; without
      // an agent slug the /runs resolver redirect figures it out.
      navigate(agentSlug
        ? `/chat/${agentSlug}/${chatId}?tasks=1`
        : `/runs/${chatId.slice(5)}`)
    } else {
      navigate(`/chat/${agentSlug}/${chatId}`)
    }
  }, [navigate, notif.closePanel])

  const notificationBell = (
    <div className="relative">
      <NotificationBell
        unreadCount={notif.unreadCount}
        onClick={notif.togglePanel}
        panelOpen={notif.panelOpen}
      />
      {notif.panelOpen && (
        <NotificationPanel
          deliveries={notif.deliveries}
          loading={notif.loading}
          onMarkRead={notif.markRead}
          onMarkAllRead={notif.markAllRead}
          onDismiss={(id) => { notif.dismiss(id); notifSound.stopDangerAlarm() }}
          onAcknowledge={(id) => notif.dismissToast(id)}
          onNavigate={handleNotifNavigate}
          onClose={notif.closePanel}
        />
      )}
    </div>
  )

  const notificationToast = (
    <NotificationToast
      toasts={notif.toasts}
      onDismiss={(id) => { notif.dismissToast(id); notifSound.stopDangerAlarm() }}
      onStopAlarm={notifSound.stopDangerAlarm}
      onNavigate={handleNotifNavigate}
    />
  )

  // --- /ws/dashboard callbacks (wired into useChatStream options) ---
  const onNotification = useCallback((data: any) => {
    const d = data.delivery
    notif.addToast(d)
    notifSound.playForSeverity(d.severity, d.title, d.body)
  }, [notif, notifSound])
  const onNotificationSilent = useCallback((data: any) => {
    // Silent delivery — fires on this WS when it's connected but inactive
    // (hidden tab / 5-min idle). The alert is delivered via native push on the
    // engaged device; we only refresh the inbox + badge here.
    notif.addSilent(data.delivery)
  }, [notif])
  const onNotificationCount = useCallback((data: any) => {
    notif.setCount(data.count)
  }, [notif])

  return {
    notificationBell,
    notificationToast,
    onNotification,
    onNotificationSilent,
    onNotificationCount,
    /** Turn-complete ping (chat page only — gated on no meeting / no bg agent). */
    playPing: notifSound.playPing,
  }
}
