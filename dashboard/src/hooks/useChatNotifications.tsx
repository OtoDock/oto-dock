import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import NotificationBell from '../components/chat/notifications/NotificationBell'
import NotificationPanel from '../components/chat/notifications/NotificationPanel'
import NotificationToast from '../components/chat/notifications/NotificationToast'
import { useNotifications } from './useNotifications'
import { useNotificationSound } from './useNotificationSound'
import { usePushSubscription } from './usePushSubscription'
import { useAgents } from '../api/agents'
import { INBOX_KEY, useAcceptShare, useDeclineShare, useHideShare, useShareInbox, type ShareInboxItem } from '../api/shares'
import type { ShareInboxFrame } from '../api/wireEvents'
import { isTaskChatId, runIdOfChat } from '../lib/session/kind'

/**
 * Shared notification surface for the chat page (AgentChat).
 * Owns the inbox/toast/badge state (`useNotifications`), the per-severity sounds
 * + danger alarm (`useNotificationSound`), Web Push registration, the
 * "Shared with you" section's data (`useShareInbox`, SHARING.md) and the
 * deep-link navigation rule. Returns the bell + toast as ready-to-render nodes
 * plus the `/ws/dashboard` callbacks (wired into `useChatStream`, and the
 * `share_inbox` frame through the socket's registry).
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
  const qc = useQueryClient()
  const notif = useNotifications()
  const notifSound = useNotificationSound()
  usePushSubscription()  // Web Push (browsers)
  // Sender identity for the cards: the app-wide cached agents list resolves
  // each delivery's `agent_slug` to its avatar color + display name.
  const { data: agents } = useAgents()
  // The shares section: react-query, so the share mutations' `['shares']`
  // invalidation and the frame refresh it; the pending count stays out of
  // the deliveries' arithmetic (mark-all-read must never swallow a decision).
  const { data: inbox, refetch: refetchInbox } = useShareInbox()
  const accept = useAcceptShare()
  const decline = useDeclineShare()
  const hide = useHideShare()
  const [inboxBusyId, setInboxBusyId] = useState('')
  const [inboxError, setInboxError] = useState('')
  useEffect(() => {
    if (notif.panelOpen) void refetchInbox()
  }, [notif.panelOpen, refetchInbox])

  // Auto-stop the danger alarm once no danger toasts remain (covers every
  // dismiss path).
  useEffect(() => {
    const hasDanger = notif.toasts.some(t => t.delivery.severity === 'danger')
    if (!hasDanger) notifSound.stopDangerAlarm()
  }, [notif.toasts])

  // Navigate to the chat/task that originated a notification, or to the
  // page a row names itself (a shared app or chat).
  const handleNotifNavigate = useCallback((agentSlug: string, chatId: string, href?: string) => {
    notif.closePanel()
    if (href && href.startsWith('/') && !href.startsWith('//')) {
      navigate(href)
    } else if (!chatId) {
      // No chat_id (e.g. a file-conflict notification) → open the agent and the
      // workspace Recover bin via the ?recover deep-link.
      navigate(`/chat/${agentSlug}?recover=1`)
    } else if (isTaskChatId(chatId)) {
      // Task chats open on the chat page with task mode toggled on; without
      // an agent slug the /runs resolver redirect figures it out.
      navigate(agentSlug
        ? `/chat/${agentSlug}/${chatId}?tasks=1`
        : `/runs/${runIdOfChat(chatId)}`)
    } else {
      navigate(`/chat/${agentSlug}/${chatId}`)
    }
  }, [navigate, notif.closePanel])

  const runShare = useCallback((item: ShareInboxItem, run: () => Promise<unknown>) => {
    setInboxBusyId(item.id)
    setInboxError('')
    run()
      .catch((e) => setInboxError(e instanceof Error ? e.message : String(e)))
      .finally(() => setInboxBusyId(''))
  }, [])
  const onAcceptShare = useCallback((item: ShareInboxItem, agent: string) =>
    runShare(item, () => accept.mutateAsync({ id: item.id, agent })), [accept, runShare])
  const onDeclineShare = useCallback((item: ShareInboxItem) =>
    runShare(item, () => decline.mutateAsync({ id: item.id })), [decline, runShare])
  const onHideShare = useCallback((item: ShareInboxItem) =>
    runShare(item, () => hide.mutateAsync({ id: item.id })), [hide, runShare])
  const onOpenShare = useCallback((item: ShareInboxItem) => {
    notif.closePanel()
    if (item.href.startsWith('/') && !item.href.startsWith('//')) navigate(item.href)
  }, [navigate, notif.closePanel])

  const notificationBell = (
    <div className="relative">
      <NotificationBell
        unreadCount={notif.unreadCount}
        pendingCount={inbox?.pending ?? 0}
        onClick={notif.togglePanel}
        panelOpen={notif.panelOpen}
      />
      {notif.panelOpen && (
        <NotificationPanel
          deliveries={notif.deliveries}
          loading={notif.loading}
          agents={agents}
          onMarkRead={notif.markRead}
          onMarkAllRead={notif.markAllRead}
          onDismiss={(id) => { notif.dismiss(id); notifSound.stopDangerAlarm() }}
          onAcknowledge={(id) => notif.dismissToast(id)}
          onNavigate={handleNotifNavigate}
          onClose={notif.closePanel}
          inbox={inbox?.items}
          inboxBusyId={inboxBusyId}
          inboxError={inboxError}
          onAcceptShare={onAcceptShare}
          onDeclineShare={onDeclineShare}
          onHideShare={onHideShare}
          onOpenShare={onOpenShare}
        />
      )}
    </div>
  )

  const notificationToast = (
    <NotificationToast
      toasts={notif.toasts}
      agents={agents}
      onDismiss={(id) => { notif.dismissToast(id); notifSound.stopDangerAlarm() }}
      onStopAlarm={notifSound.stopDangerAlarm}
      onNavigate={handleNotifNavigate}
    />
  )

  // --- /ws/dashboard callbacks (wired into useChatStream options) ---
  const refreshInboxFor = useCallback((delivery: { source?: string } | undefined) => {
    if (delivery?.source === 'share') qc.invalidateQueries({ queryKey: INBOX_KEY })
  }, [qc])
  const onNotification = useCallback((data: any) => {
    const d = data.delivery
    notif.addToast(d)
    notifSound.playForSeverity(d.severity, d.title, d.body)
    refreshInboxFor(d)
  }, [notif, notifSound, refreshInboxFor])
  const onNotificationSilent = useCallback((data: any) => {
    // Silent delivery — fires on this WS when it's connected but inactive
    // (hidden tab / 5-min idle). The alert is delivered via native push on the
    // engaged device; we only refresh the inbox + badge here.
    notif.addSilent(data.delivery)
    refreshInboxFor(data.delivery)
  }, [notif, refreshInboxFor])
  const onNotificationCount = useCallback((data: any) => {
    notif.setCount(data.count)
  }, [notif])
  // The section changed (a share to this person, a decision, a hide, a
  // placement in one of their agents): refetch rather than apply the frame.
  const onShareInbox = useCallback((data: ShareInboxFrame) => {
    qc.invalidateQueries({ queryKey: INBOX_KEY })
    for (const agent of data.agents ?? []) qc.invalidateQueries({ queryKey: ['apps', agent] })
  }, [qc])

  return {
    notificationBell,
    notificationToast,
    onNotification,
    onNotificationSilent,
    onNotificationCount,
    onShareInbox,
    /** Turn-complete ping (chat page only — gated on no meeting / no bg agent). */
    playPing: notifSound.playPing,
  }
}
